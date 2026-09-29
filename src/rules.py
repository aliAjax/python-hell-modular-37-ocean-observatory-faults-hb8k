from datetime import datetime, timedelta, timezone

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _number(value, field):
    number = _signed_number(value, field)
    if number < 0:
        raise ValidationError(field + " must be non-negative")
    return number


def _signed_number(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    return number


def _parse_timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(field + " is required")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(field + " must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _asset_for_telemetry(data, lookup):
    asset = _find_one(lookup, "asset", "id", data.get("asset_id"))
    if not asset:
        raise ValidationError("telemetry requires asset")
    return asset


def _corrected_observed_at(data, asset, lookup=None):
    observed_at = _parse_timestamp(data.get("observed_at"), "observed_at")
    try:
        offset = float(asset["data"].get("clock_offset_seconds") or 0)
    except (TypeError, ValueError):
        offset = 0
    return observed_at - timedelta(seconds=offset)


def _current_telemetry(asset_id, metric, lookup):
    for item in _all(lookup, "telemetry"):
        if (
            item["status"] == "current"
            and item["data"].get("asset_id") == asset_id
            and item["data"].get("metric") == metric
        ):
            return item
    return None


def _telemetry_observed_time(item):
    try:
        return _parse_timestamp(item["data"].get("corrected_observed_at"), "corrected_observed_at")
    except ValidationError:
        return _parse_timestamp(item["data"].get("observed_at"), "observed_at")


def _prepare_telemetry(data, lookup):
    asset = _asset_for_telemetry(data, lookup)
    _number(data.get("value"), "value")
    try:
        revision = int(data.get("revision"))
    except (TypeError, ValueError):
        raise ValidationError("revision must be an integer")
    if revision < 1:
        raise ValidationError("revision must be positive")

    raw_observed_at = data.get("observed_at")
    corrected = _corrected_observed_at(data, asset, lookup)
    payload = dict(data)
    payload.pop("id", None)
    payload["revision"] = revision
    payload["observed_at"] = raw_observed_at
    payload["corrected_observed_at"] = _format_timestamp(corrected)
    payload["clock_offset_seconds"] = float(asset["data"].get("clock_offset_seconds") or 0)
    current = _current_telemetry(payload["asset_id"], payload["metric"], lookup)
    is_late = bool(current and corrected <= _telemetry_observed_time(current))
    if is_late:
        payload["late"] = True
    return payload, current, corrected, is_late


def _validate_asset(data, lookup):
    if not _find_one(lookup, "station", "id", data.get("station_id")):
        raise ValidationError("asset requires station")
    if data.get("clock_offset_seconds") not in (None, ""):
        _signed_number(data.get("clock_offset_seconds"), "clock_offset_seconds")


def _validate_link(data, lookup):
    if not _find_one(lookup, "station", "id", data.get("station_id")):
        raise ValidationError("link requires station")
    if not _find_one(lookup, "asset", "id", data.get("asset_id")):
        raise ValidationError("link requires asset")
    _number(data.get("capacity"), "capacity")


def _validate_telemetry(data, lookup):
    _prepare_telemetry(data, lookup)


def _validate_incident(data, lookup):
    if not data.get("station_id") and not data.get("asset_id") and not data.get("link_id"):
        raise ValidationError("incident requires station_id, asset_id or link_id")
    if data.get("severity") not in ("low", "medium", "high", "critical"):
        raise ValidationError("invalid incident severity")
    for item in _all(lookup, "incident"):
        if item["status"] in ("open", "diagnosing", "recovery_planned", "recovering") and item["data"].get("asset_id") == data.get("asset_id") and item["data"].get("kind") == data.get("kind"):
            raise ConflictError("active incident already exists for asset and kind")


def _validate_action(data, lookup):
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident or incident["status"] in ("resolved", "closed"):
        raise ValidationError("recovery action requires an active incident")
    if data.get("action_type") not in ("remote_restart", "switch_backup", "firmware_rollback", "dispatch_mission"):
        raise ValidationError("invalid action_type")
    key = data.get("dedupe_key")
    for item in _all(lookup, "recovery_action"):
        if item["data"].get("dedupe_key") == key and item["status"] not in ("succeeded", "failed", "cancelled"):
            raise ConflictError("active recovery action already exists for dedupe_key")


def _validate_mission(data, lookup):
    if not _find_one(lookup, "station", "id", data.get("station_id")):
        raise ValidationError("mission requires station")
    if not data.get("window_start") or not data.get("window_end"):
        raise ValidationError("mission window is required")


def _validate_gap(data, lookup):
    if not _find_one(lookup, "incident", "id", data.get("incident_id")):
        raise ValidationError("data gap requires incident")
    if not data.get("start_at") or not data.get("end_at"):
        raise ValidationError("gap window is required")


def _incident_asset_ids(entity, lookup):
    asset_ids = set()
    direct_asset_id = entity["data"].get("asset_id")
    if direct_asset_id:
        asset_ids.add(direct_asset_id)
    link_id = entity["data"].get("link_id")
    if link_id:
        link = _find_one(lookup, "link", "id", link_id)
        if link and link["data"].get("asset_id"):
            asset_ids.add(link["data"]["asset_id"])
    return asset_ids


def _has_usable_telemetry_after_start(asset_id, started_at, lookup):
    for item in _all(lookup, "telemetry"):
        if item["status"] != "current" or item["data"].get("asset_id") != asset_id:
            continue
        observed_at = _telemetry_observed_time(item)
        if observed_at > started_at:
            return True
    return False


def _resolve_incident(actor, entity, data, lookup):
    actions = [a for a in _all(lookup, "recovery_action") if a["data"].get("incident_id") == entity["id"] and a["status"] not in ("succeeded", "failed", "cancelled")]
    if actions:
        raise ConflictError("incident cannot resolve while recovery actions are active")
    gaps = [g for g in _all(lookup, "gap") if g["data"].get("incident_id") == entity["id"] and g["status"] not in ("filled", "accepted", "closed")]
    if gaps:
        raise ConflictError("incident cannot resolve while data gaps remain open")
    asset_ids = _incident_asset_ids(entity, lookup)
    if asset_ids:
        started_at = _parse_timestamp(
            entity["data"].get("started_at") or entity["data"].get("start_at") or entity.get("created_at"),
            "started_at",
        )
        faulty_assets = {a["id"] for a in _all(lookup, "asset") if a["status"] in ("faulty", "offline", "rebooting")}
        for asset_id in asset_ids:
            if asset_id in faulty_assets:
                raise ConflictError("affected asset is still unavailable")
            if not _has_usable_telemetry_after_start(asset_id, started_at, lookup):
                raise ConflictError("affected asset requires usable telemetry after incident start")
    return {"resolved_by": actor.user_id}


def _complete_action(actor, entity, data, lookup):
    if not data.get("outcome"):
        raise ValidationError("outcome is required")
    return {"completed_by": actor.user_id}


def _complete_mission(actor, entity, data, lookup):
    if not data.get("report"):
        raise ValidationError("report is required")
    return {"completed_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "stations": "station", "assets": "asset", "links": "link", "telemetries": "telemetry",
        "incidents": "incident", "recovery_actions": "recovery_action", "missions": "mission",
        "gaps": "gap",
    }
    INITIAL_STATUS = {
        "station": "online", "asset": "healthy", "link": "up", "telemetry": "current",
        "incident": "open", "recovery_action": "proposed", "mission": "planned", "gap": "open",
    }
    TRANSITIONS = {
        "station": {
            "degrade": (("online",), "degraded"),
            "go_offline": (("online", "degraded"), "offline"),
            "resume": (("degraded", "offline"), "online"),
        },
        "asset": {
            "degrade": (("healthy",), "degraded"),
            "fail": (("healthy", "degraded"), "faulty"),
            "start_reboot": (("faulty",), "rebooting"),
            "finish_reboot": (("rebooting",), "healthy"),
            "restore": (("faulty",), "healthy"),
        },
        "link": {
            "degrade": (("up",), "degraded"),
            "fail": (("up", "degraded"), "down"),
            "activate_backup": (("down", "degraded"), "backup_active"),
            "restore": (("down", "backup_active", "degraded"), "up"),
        },
        "telemetry": {
            "mark_stale": (("current",), "stale"),
            "quarantine": (("current", "stale"), "quarantined"),
            "revise": (("current", "stale"), "current"),
            "clear": (("stale",), "current"),
        },
        "incident": {
            "diagnose": (("open",), "diagnosing"),
            "plan_recovery": (("diagnosing",), "recovery_planned"),
            "start_recovery": (("recovery_planned",), "recovering"),
            "resolve": (("recovering",), "resolved"),
            "close": (("resolved",), "closed"),
            "reopen": (("resolved", "closed"), "open"),
        },
        "recovery_action": {
            "approve": (("proposed",), "approved"),
            "start": (("approved",), "running"),
            "succeed": (("running",), "succeeded"),
            "fail": (("running",), "failed"),
            "cancel": (("proposed", "approved", "running"), "cancelled"),
        },
        "mission": {
            "approve": (("planned",), "approved"),
            "depart": (("approved",), "underway"),
            "complete": (("underway",), "completed"),
            "cancel": (("planned", "approved", "underway"), "cancelled"),
        },
        "gap": {
            "estimate": (("open",), "estimated"),
            "fill": (("estimated",), "filled"),
            "accept": (("filled", "open"), "accepted"),
        },
    }
    CREATE_REQUIRED = {
        "station": ("name", "region"),
        "asset": ("station_id", "asset_type", "serial_no", "last_seen"),
        "link": ("station_id", "asset_id", "link_type", "capacity"),
        "telemetry": ("asset_id", "metric", "value", "observed_at", "revision"),
        "incident": ("kind", "severity", "summary"),
        "recovery_action": ("incident_id", "action_type", "dedupe_key"),
        "mission": ("station_id", "purpose", "window_start", "window_end"),
        "gap": ("incident_id", "start_at", "end_at"),
    }
    ACTION_REQUIRED = {
        ("station", "degrade"): ("reason",),
        ("link", "fail"): ("reason",),
        ("telemetry", "revise"): ("value", "observed_at", "revision"),
        ("recovery_action", "succeed"): ("outcome",),
        ("mission", "complete"): ("report",),
        ("gap", "fill"): ("estimate",),
        ("incident", "resolve"): ("summary",),
    }
    CREATE_ROLES = {
        "station": ("admin", "engineer"),
        "asset": ("admin", "engineer"),
        "link": ("admin", "engineer"),
        "telemetry": ("admin", "operator", "engineer"),
        "incident": ("admin", "operator", "engineer"),
        "recovery_action": ("admin", "operator", "engineer"),
        "mission": ("admin", "engineer"),
        "gap": ("admin", "operator", "engineer"),
    }
    ROLE_ACTIONS = {
        "degrade": ("admin", "engineer", "operator"),
        "go_offline": ("admin", "engineer", "operator"),
        "resume": ("admin", "engineer", "operator"),
        "fail": ("admin", "engineer", "operator"),
        "start_reboot": ("admin", "engineer", "operator"),
        "finish_reboot": ("admin", "engineer", "operator"),
        "restore": ("admin", "engineer", "operator"),
        "activate_backup": ("admin", "engineer", "operator"),
        "mark_stale": ("admin", "operator", "engineer"),
        "quarantine": ("admin", "engineer", "operator"),
        "revise": ("admin", "operator", "engineer"),
        "clear": ("admin", "operator", "engineer"),
        "diagnose": ("admin", "operator", "engineer"),
        "plan_recovery": ("admin", "operator", "engineer"),
        "start_recovery": ("admin", "operator", "engineer"),
        "resolve": ("admin", "engineer"),
        "close": ("admin", "engineer"),
        "reopen": ("admin", "engineer", "operator"),
        "approve": ("admin", "engineer"),
        "start": ("admin", "engineer", "operator"),
        "succeed": ("admin", "engineer", "operator"),
        "cancel": ("admin", "engineer", "operator"),
        "depart": ("admin", "engineer", "operator"),
        "estimate": ("admin", "engineer", "operator"),
        "fill": ("admin", "engineer", "operator"),
        "accept": ("admin", "engineer", "operator"),
    }
    CUSTOM_CREATE = {
        "asset": lambda a, d, l: _validate_asset(d, l),
        "link": lambda a, d, l: _validate_link(d, l),
        "telemetry": lambda a, d, l: _validate_telemetry(d, l),
        "incident": lambda a, d, l: _validate_incident(d, l),
        "recovery_action": lambda a, d, l: _validate_action(d, l),
        "mission": lambda a, d, l: _validate_mission(d, l),
        "gap": lambda a, d, l: _validate_gap(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("incident", "resolve"): _resolve_incident,
        ("recovery_action", "succeed"): _complete_action,
        ("mission", "complete"): _complete_mission,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def prepare_telemetry_create(self, data, lookup):
        return _prepare_telemetry(data, lookup)

    def prepare_telemetry_revision(self, actor, entity, data, lookup):
        _ensure_role(actor, self.ROLE_ACTIONS.get("revise"))
        _require(data, self.ACTION_REQUIRED.get(("telemetry", "revise"), ()))
        payload = dict(data)
        payload["asset_id"] = entity["data"].get("asset_id")
        payload["metric"] = entity["data"].get("metric")
        prepared, current, corrected, is_late = _prepare_telemetry(payload, lookup)
        baseline = current
        mode = "late" if is_late else "update"
        if not current:
            baseline = entity
            existing_time = _telemetry_observed_time(entity)
            is_late = corrected <= existing_time
            if is_late:
                mode = "late"
            elif entity["status"] == "late":
                mode = "create_current"
            elif entity["status"] == "stale":
                mode = "replace_current"
            else:
                mode = "late"
        prepared["revised_by"] = actor.user_id
        return baseline, prepared, corrected, mode

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
