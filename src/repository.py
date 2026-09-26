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
                CREATE TABLE IF NOT EXISTS event_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_order_id TEXT,
                    communication_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_event
                    ON event_snapshots(event_id, revision);
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

    def update_entity(self, entity_id, expected_version, status, data):
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
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def approve_revision_order(self, order_id, expected_order_version, event_id,
                               expected_event_version, proposal, communication_id,
                               reviewer_id):
        """Atomically apply an approved order and cut a new catalog snapshot.

        Succeeds only when both the order and the target event still match the
        versions the decision was based on; otherwise a ConflictError is raised
        and nothing is written.
        """
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            order_row = connection.execute(
                "SELECT version, status, data FROM entities WHERE id = ?",
                (order_id,),
            ).fetchone()
            if not order_row:
                raise NotFoundError("entity not found: " + order_id)
            if int(order_row["version"]) != int(expected_order_version):
                raise ConflictError(
                    "version conflict: order expected %s, found %s"
                    % (expected_order_version, order_row["version"])
                )
            if order_row["status"] != "pending":
                raise ConflictError("order is not pending: " + order_row["status"])

            event_row = connection.execute(
                "SELECT version, status, data FROM entities WHERE id = ?",
                (event_id,),
            ).fetchone()
            if not event_row:
                raise NotFoundError("entity not found: " + event_id)
            event_version = int(event_row["version"])
            if event_version != int(expected_event_version):
                raise ConflictError(
                    "version conflict: event expected %s, found %s"
                    % (expected_event_version, event_version)
                )
            if event_row["status"] not in ("published", "revised"):
                raise ConflictError("event is no longer revisable: " + event_row["status"])

            event_data = json.loads(event_row["data"])
            changes = {
                "magnitude": [event_data.get("magnitude"), float(proposal["proposed_magnitude"])],
                "location": [event_data.get("location"), proposal["proposed_location"]],
                "depth_km": [event_data.get("depth_km"), float(proposal["proposed_depth_km"])],
            }
            event_data["magnitude"] = float(proposal["proposed_magnitude"])
            event_data["location"] = proposal["proposed_location"]
            event_data["depth_km"] = float(proposal["proposed_depth_km"])
            event_data["revision_order_id"] = order_id
            event_data["communication_id"] = communication_id
            next_event_version = event_version + 1
            connection.execute(
                "UPDATE entities SET status = 'revised', version = ?, data = ?, "
                "updated_at = ? WHERE id = ? AND version = ?",
                (
                    next_event_version,
                    json.dumps(event_data, ensure_ascii=False, sort_keys=True),
                    now,
                    event_id,
                    event_version,
                ),
            )

            revision = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision "
                "FROM event_snapshots WHERE event_id = ?",
                (event_id,),
            ).fetchone()["next_revision"]
            connection.execute(
                "INSERT INTO event_snapshots(event_id, revision, version, status, data, "
                "source, source_order_id, communication_id, created_by, created_at) "
                "VALUES (?, ?, ?, 'revised', ?, 'revision_order', ?, ?, ?, ?)",
                (
                    event_id,
                    revision,
                    next_event_version,
                    json.dumps(event_data, ensure_ascii=False, sort_keys=True),
                    order_id,
                    communication_id,
                    reviewer_id,
                    now,
                ),
            )

            order_data = json.loads(order_row["data"])
            order_data["applied_revision"] = revision
            order_data["communication_id"] = communication_id
            order_data["applied_at"] = now
            next_order_version = int(order_row["version"]) + 1
            connection.execute(
                "UPDATE entities SET status = 'approved', version = ?, data = ?, "
                "updated_at = ? WHERE id = ? AND version = ?",
                (
                    next_order_version,
                    json.dumps(order_data, ensure_ascii=False, sort_keys=True),
                    now,
                    order_id,
                    order_row["version"],
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        order = self.get_entity(order_id)
        event = self.get_entity(event_id)
        return order, event, revision, changes

    def save_event_snapshot(self, entity, source, created_by, communication_id=None):
        """Snapshot an event entity at its current version (e.g. on publish)."""
        now = utcnow()
        with self._connect() as connection:
            revision = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision "
                "FROM event_snapshots WHERE event_id = ?",
                (entity["id"],),
            ).fetchone()["next_revision"]
            connection.execute(
                "INSERT INTO event_snapshots(event_id, revision, version, status, data, "
                "source, source_order_id, communication_id, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    entity["id"],
                    revision,
                    entity["version"],
                    entity["status"],
                    json.dumps(entity["data"], ensure_ascii=False, sort_keys=True),
                    source,
                    communication_id,
                    created_by,
                    now,
                ),
            )
        return revision

    def list_event_snapshots(self, event_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM event_snapshots WHERE event_id = ? ORDER BY revision",
                (event_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "revision": int(row["revision"]),
                "version": int(row["version"]),
                "status": row["status"],
                "data": json.loads(row["data"]),
                "source": row["source"],
                "source_order_id": row["source_order_id"],
                "communication_id": row["communication_id"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

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
