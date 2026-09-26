from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)

REVISABLE_EVENT_STATUSES = ("published", "revised")


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")
    return dict(data)


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")
    return dict(data)


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    return {"associated_count": len(reports)}


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


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate}


def _find_event(lookup, event_id):
    rows = lookup('event', 'id', event_id) if lookup else []
    return rows[0] if rows else None


def _as_number(value, field, low=None, high=None):
    if isinstance(value, bool):
        raise ValidationError("%s must be a number" % field)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be a number" % field)
    if low is not None and number < low:
        raise ValidationError("%s must be >= %s" % (field, low))
    if high is not None and number > high:
        raise ValidationError("%s must be <= %s" % (field, high))
    return number


def _validate_proposal(data):
    for field in ("basis", "proposed_magnitude", "proposed_location", "proposed_depth_km"):
        if not data.get(field) and data.get(field) != 0:
            raise ValidationError("missing required field: " + field)
    _as_number(data["proposed_magnitude"], "proposed_magnitude", low=0, high=10)
    _as_number(data["proposed_depth_km"], "proposed_depth_km", low=0, high=800)


def _validate_revision_order_create(actor, data, lookup):
    event_id = data.get("event_id")
    if not event_id:
        raise ValidationError("missing required field: event_id")
    event = _find_event(lookup, event_id)
    if not event:
        raise NotFoundError("event not found: " + str(event_id))
    if event["status"] not in REVISABLE_EVENT_STATUSES:
        raise InvalidTransition(
            "revision orders can only target published or revised events; event is %s"
            % event["status"]
        )
    _validate_proposal(data)
    payload = dict(data)
    payload["event_version"] = event["version"]
    payload["resubmit_count"] = 0
    payload["comments"] = []
    return payload


def _validate_revision_order_return(actor, entity, data, lookup):
    if not data.get("comment"):
        raise ValidationError("missing required field: comment")
    stamped = {
        "comment": data["comment"],
        "by": actor.user_id,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    comments = list(entity["data"].get("comments") or []) + [stamped]
    return {"comments": comments}


def _validate_revision_order_resubmit(actor, entity, data, lookup):
    _validate_proposal(data)
    event = _find_event(lookup, entity["data"]["event_id"])
    if not event:
        raise NotFoundError("event not found: " + entity["data"]["event_id"])
    if event["status"] not in REVISABLE_EVENT_STATUSES:
        raise InvalidTransition(
            "event is no longer revisable (status: %s)" % event["status"]
        )
    patch = dict(data)
    patch["resubmit_count"] = int(entity["data"].get("resubmit_count") or 0) + 1
    patch["event_version"] = event["version"]
    return patch


def _validate_revision_order_approve(actor, entity, data, lookup):
    event = _find_event(lookup, entity["data"]["event_id"])
    if not event:
        raise NotFoundError("event not found: " + entity["data"]["event_id"])
    if event["status"] not in REVISABLE_EVENT_STATUSES:
        raise InvalidTransition(
            "event is no longer revisable (status: %s)" % event["status"]
        )
    if event["version"] != int(entity["data"]["event_version"]):
        raise ConflictError(
            "revision order is stale: event version %s, order based on %s"
            % (event["version"], entity["data"]["event_version"])
        )
    return {}


CUSTOM_CREATE['revision_order'] = _validate_revision_order_create
CUSTOM_TRANSITIONS[('revision_order', 'return')] = _validate_revision_order_return
CUSTOM_TRANSITIONS[('revision_order', 'resubmit')] = _validate_revision_order_resubmit
CUSTOM_TRANSITIONS[('revision_order', 'approve')] = _validate_revision_order_approve


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event', 'revision_orders': 'revision_order'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate', 'revision_order': 'pending'}
    TRANSITIONS = {
        'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')},
        'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed', 'revised'), 'published'), 'withdraw': (('published', 'revised'), 'withdrawn')},
        'revision_order': {'return': (('pending',), 'returned'), 'resubmit': (('returned',), 'pending'), 'approve': (('pending',), 'approved')},
    }
    CREATE_REQUIRED = {
        'station': ('code', 'lat', 'lon'),
        'event': ('title', 'origin_time', 'location', 'reports'),
        'revision_order': ('event_id', 'basis', 'proposed_magnitude', 'proposed_location', 'proposed_depth_km'),
    }
    ACTION_REQUIRED = {
        ('station', 'offline'): ('reason',),
        ('event', 'review'): ('reviewer', 'magnitude'),
        ('event', 'publish'): ('communication_id',),
        ('event', 'withdraw'): ('reason',),
        ('revision_order', 'return'): ('comment',),
        ('revision_order', 'resubmit'): ('basis', 'proposed_magnitude', 'proposed_location', 'proposed_depth_km'),
        ('revision_order', 'approve'): ('communication_id',),
    }
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst'), 'revision_order': ('admin', 'analyst')}
    ROLE_ACTIONS = {
        'offline': ('admin', 'station'),
        'online': ('admin', 'station'),
        'associate': ('admin', 'analyst'),
        'review': ('admin', 'reviewer'),
        'publish': ('admin', 'reviewer'),
        'withdraw': ('admin', 'reviewer'),
        'return': ('admin', 'reviewer'),
        'resubmit': ('admin', 'analyst'),
        'approve': ('admin', 'reviewer'),
    }

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
            data = custom(actor, data, lookup)
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


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
