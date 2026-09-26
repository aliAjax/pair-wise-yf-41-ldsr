from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    return {"associated_count": len(reports)}


def _ensure_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(field + " must be a number")


def _published_event_or_error(lookup, event_id):
    event = _find_one(lookup, "event", "id", event_id)
    if event is None:
        raise ValidationError("target event not found: " + str(event_id))
    if event["status"] not in ("published", "revised"):
        raise ValidationError("revision order requires a published or revised event")
    return event


def _validate_revision_order(actor, data, lookup):
    event = _published_event_or_error(lookup, data.get("event_id"))
    _ensure_number(data.get("magnitude"), "magnitude")
    _ensure_number(data.get("depth"), "depth")
    data["event_version"] = event["version"]
    data["resubmit_count"] = 0


def _validate_order_resubmit(actor, entity, data, lookup):
    event = _published_event_or_error(lookup, entity["data"].get("event_id"))
    _ensure_number(data.get("magnitude"), "magnitude")
    _ensure_number(data.get("depth"), "depth")
    return {
        "event_version": event["version"],
        "resubmit_count": int(entity["data"].get("resubmit_count", 0)) + 1,
    }


def _validate_order_approve(actor, entity, data, lookup):
    event = _find_one(lookup, "event", "id", entity["data"].get("event_id"))
    if event is None:
        raise ValidationError(
            "target event not found: " + str(entity["data"].get("event_id"))
        )
    if event["status"] not in ("published", "revised"):
        raise InvalidTransition("target event is no longer published")
    base_version = entity["data"].get("event_version")
    if base_version is None or int(base_version) != int(event["version"]):
        raise ConflictError(
            "event version expired: order bases on %s, current is %s"
            % (base_version, event["version"])
        )
    return {}


def associate_reports(reports, max_delta=120, max_distance=3.0):
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(report.get("distance_km", 0)) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event, 'revision_order': _validate_revision_order}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate, ('revision_order', 'resubmit'): _validate_order_resubmit, ('revision_order', 'approve'): _validate_order_approve}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event', 'revision_orders': 'revision_order'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate', 'revision_order': 'submitted'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'withdraw': (('published', 'revised'), 'withdrawn')}, 'revision_order': {'reject': (('submitted',), 'rejected'), 'resubmit': (('rejected',), 'submitted'), 'approve': (('submitted',), 'approved')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports'), 'revision_order': ('event_id', 'basis', 'magnitude', 'location', 'depth')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'withdraw'): ('reason',), ('revision_order', 'reject'): ('opinion',), ('revision_order', 'resubmit'): ('basis', 'magnitude', 'location', 'depth')}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst'), 'revision_order': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer'), 'reject': ('admin', 'reviewer'), 'resubmit': ('admin', 'analyst'), 'approve': ('admin', 'reviewer')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
