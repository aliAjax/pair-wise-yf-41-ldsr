from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "revision_order" and action == "approve":
            return self._approve_revision(actor, entity, data, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        snapshot = None
        if entity["kind"] == "event" and action == "publish":
            snapshot = {"source": "publish"}
        updated = self.repository.update_entity(
            entity_id, expected, next_status, merged, snapshot=snapshot
        )
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _approve_revision(self, actor, order, data, expected_version):
        expected = int(expected_version) if expected_version is not None else order["version"]
        next_status, patch = self.rules.validate_transition(
            actor, order, "approve", dict(data or {}), self._lookup
        )
        merged_order = dict(order["data"])
        merged_order.update(patch)
        event = self.repository.get_entity(order["data"].get("event_id"))
        if not event:
            raise NotFoundError(
                "entity not found: " + str(order["data"].get("event_id"))
            )
        event_data = dict(event["data"])
        for field in ("magnitude", "location", "depth"):
            event_data[field] = order["data"][field]
        event_data["revision_basis"] = order["data"].get("basis")
        event_data["last_revision_order"] = order["id"]
        result = self.repository.apply_approved_revision(
            order_id=order["id"],
            expected_order_version=expected,
            order_status=next_status,
            order_data=merged_order,
            event_id=event["id"],
            expected_event_version=order["data"].get("event_version"),
            event_status="revised",
            event_data=event_data,
        )
        self.audit.record(
            order["id"],
            actor,
            "approve",
            order["status"],
            next_status,
            {
                "patch": patch,
                "event_id": event["id"],
                "catalog_version": result["catalog_version"],
            },
        )
        self.audit.record(
            event["id"],
            actor,
            "revise",
            event["status"],
            "revised",
            {
                "order_id": order["id"],
                "basis": order["data"].get("basis"),
                "magnitude": order["data"].get("magnitude"),
                "location": order["data"].get("location"),
                "depth": order["data"].get("depth"),
                "catalog_version": result["catalog_version"],
            },
        )
        return result["order"]

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def current_catalog(self):
        return self.repository.list_snapshots(current_only=True)

    def event_snapshots(self, event_id):
        event = self.repository.get_entity(event_id)
        if not event or event["kind"] != "event":
            raise NotFoundError("event not found: " + event_id)
        return self.repository.list_snapshots(event_id=event_id)
