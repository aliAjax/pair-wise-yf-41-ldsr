from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
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
        payload = self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        detail = {"kind": kind}
        if kind == "revision_order":
            detail.update(
                {
                    "event_id": payload["event_id"],
                    "event_version": payload["event_version"],
                    "basis": payload["basis"],
                    "proposal": {
                        "magnitude": payload["proposed_magnitude"],
                        "location": payload["proposed_location"],
                        "depth_km": payload["proposed_depth_km"],
                    },
                }
            )
        self.audit.record(entity_id, actor, "create", None, status, detail)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "revision_order":
            if action == "approve":
                return self.approve_revision_order(
                    actor, entity_id, dict(data or {}), expected_version
                )
            return self._revision_order_transition(
                actor, entity, action, dict(data or {}), expected_version
            )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if kind == "event" and action == "publish":
            revision = self.repository.save_event_snapshot(
                updated, "publish", actor.user_id,
                communication_id=patch.get("communication_id"),
            )
            self.audit.record(
                entity_id, actor, "catalog_snapshot", updated["status"],
                updated["status"], {"revision": revision, "source": "publish"},
            )
        return updated

    def _revision_order_transition(self, actor, order, action, data, expected_version):
        expected = (
            int(expected_version)
            if expected_version is not None
            else order["version"]
        )
        next_status, patch = self.rules.validate_transition(
            actor, order, action, data, self._lookup
        )
        merged = dict(order["data"])
        merged.update(patch)
        updated = self.repository.update_entity(order["id"], expected, next_status, merged)
        if action == "return":
            detail = {"comment": data["comment"]}
        elif action == "resubmit":
            detail = {
                "basis": patch["basis"],
                "proposal": {
                    "magnitude": patch["proposed_magnitude"],
                    "location": patch["proposed_location"],
                    "depth_km": patch["proposed_depth_km"],
                },
                "resubmit_count": patch["resubmit_count"],
                "event_version": patch["event_version"],
            }
        else:
            detail = {"patch": patch}
        self.audit.record(
            order["id"], actor, action, order["status"], updated["status"], detail
        )
        return updated

    def approve_revision_order(self, actor, order_id, data, expected_version=None):
        order = self.repository.get_entity(order_id)
        if not order:
            raise NotFoundError("entity not found: " + order_id)
        if self.rules.normalize_kind(order["kind"]) != "revision_order":
            raise InvalidTransition("entity is not a revision_order: " + order_id)
        expected_order_version = (
            int(expected_version) if expected_version is not None else order["version"]
        )
        # Rules engine owns role/state/required-field/stale checks.
        self.rules.validate_transition(actor, order, "approve", data, self._lookup)
        event_id = order["data"]["event_id"]
        event_version = int(order["data"]["event_version"])
        proposal = {
            "proposed_magnitude": order["data"]["proposed_magnitude"],
            "proposed_location": order["data"]["proposed_location"],
            "proposed_depth_km": order["data"]["proposed_depth_km"],
        }
        # Repository re-checks every version under a single write transaction.
        updated_order, updated_event, revision, changes = (
            self.repository.approve_revision_order(
                order_id,
                expected_order_version,
                event_id,
                event_version,
                proposal,
                data["communication_id"],
                actor.user_id,
            )
        )
        self.audit.record(
            order_id, actor, "approve", "pending", "approved",
            {
                "event_id": event_id,
                "event_version_before": event_version,
                "applied_revision": revision,
                "communication_id": data["communication_id"],
                "basis": order["data"].get("basis"),
                "changes": changes,
            },
        )
        self.audit.record(
            event_id, actor, "revised_by_order", "published", "revised",
            {"order_id": order_id, "revision": revision, "changes": changes},
        )
        self.audit.record(
            event_id, actor, "catalog_snapshot", "revised", "revised",
            {"revision": revision, "source": "revision_order", "order_id": order_id},
        )
        return updated_order

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def event_versions(self, event_id):
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("entity not found: " + event_id)
        return self.repository.list_event_snapshots(event_id)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
