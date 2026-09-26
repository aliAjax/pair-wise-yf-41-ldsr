import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS catalog_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    catalog_version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    source TEXT NOT NULL,
                    order_id TEXT,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, catalog_version)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_event
                    ON catalog_snapshots(event_id, catalog_version);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data, snapshot=None):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            if snapshot is not None:
                self._insert_snapshot(
                    connection, entity_id, data, snapshot["source"], snapshot.get("order_id"), now
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    @staticmethod
    def _insert_snapshot(connection, event_id, data, source, order_id, now):
        row = connection.execute(
            "SELECT COALESCE(MAX(catalog_version), 0) AS v FROM catalog_snapshots WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        next_version = int(row["v"]) + 1
        connection.execute(
            "UPDATE catalog_snapshots SET is_current = 0 WHERE event_id = ? AND is_current = 1",
            (event_id,),
        )
        connection.execute(
            "INSERT INTO catalog_snapshots(event_id, catalog_version, data, source, order_id, is_current, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (
                event_id,
                next_version,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                source,
                order_id,
                now,
            ),
        )
        return next_version

    def apply_approved_revision(
        self,
        order_id,
        expected_order_version,
        order_status,
        order_data,
        event_id,
        expected_event_version,
        event_status,
        event_data,
    ):
        """Approve a revision order and cut the catalog over in one transaction."""
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (order_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + order_id)
            order_version = int(row["version"])
            if order_version != int(expected_order_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_order_version, order_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (
                    order_status,
                    json.dumps(order_data, ensure_ascii=False, sort_keys=True),
                    now,
                    order_id,
                    order_version,
                ),
            )
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (event_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + event_id)
            event_version = int(row["version"])
            if event_version != int(expected_event_version):
                raise ConflictError(
                    "event version expired: expected %s, found %s"
                    % (expected_event_version, event_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (
                    event_status,
                    json.dumps(event_data, ensure_ascii=False, sort_keys=True),
                    now,
                    event_id,
                    event_version,
                ),
            )
            catalog_version = self._insert_snapshot(
                connection, event_id, event_data, "revision_order", order_id, now
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "order": self.get_entity(order_id),
            "event": self.get_entity(event_id),
            "catalog_version": catalog_version,
        }

    @staticmethod
    def _snapshot_from_row(row):
        return {
            "event_id": row["event_id"],
            "catalog_version": int(row["catalog_version"]),
            "data": json.loads(row["data"]),
            "source": row["source"],
            "order_id": row["order_id"],
            "is_current": bool(row["is_current"]),
            "created_at": row["created_at"],
        }

    def list_snapshots(self, event_id=None, current_only=False):
        clauses = []
        params = []
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if current_only:
            clauses.append("is_current = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM catalog_snapshots" + where
                + " ORDER BY event_id, catalog_version",
                params,
            ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
