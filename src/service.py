import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
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

    def _telemetry_series(self, asset_id, metric):
        return [
            item
            for item in self._lookup("telemetry", "*", None) or []
            if item["data"].get("asset_id") == asset_id and item["data"].get("metric") == metric
        ]

    def _accept_telemetry(self, actor, payload, arrival):
        """Merge one reading into the asset/metric series.

        The reading's observed time is folded with the asset clock offset by
        the rule engine. Only a reading with a later folded observation time
        replaces the current value; earlier readings arriving later are kept
        as standalone ``late`` versions and never touch the current reading.
        """
        reading = dict(payload)
        self.rules.prepare_telemetry(actor, reading, self._lookup)
        asset_id = reading["asset_id"]
        metric = reading["metric"]
        revision = int(reading["revision"])
        adjusted = reading["adjusted_observed_at"]
        series = self._telemetry_series(asset_id, metric)

        duplicate = next(
            (
                item
                for item in series
                if int(item["data"].get("revision", 0)) == revision
                and item["data"].get("adjusted_observed_at") == adjusted
            ),
            None,
        )
        if duplicate:
            return duplicate

        current_holders = [item for item in series if item["status"] == "current"]
        holder = max(
            current_holders,
            key=lambda item: item["data"].get("adjusted_observed_at") or item["data"].get("observed_at"),
            default=None,
        )
        if holder is None or adjusted > (
            holder["data"].get("adjusted_observed_at") or holder["data"].get("observed_at")
        ):
            if holder is None:
                entity = self.repository.create_entity(
                    str(reading.pop("id", "") or uuid4()), "telemetry", "current", reading, actor.user_id
                )
                self.audit.record(entity["id"], actor, arrival, None, "current", {"reading": reading})
                return entity
            merged = dict(holder["data"])
            merged.update(reading)
            updated = self.repository.update_entity(holder["id"], holder["version"], "current", merged)
            self.audit.record(
                holder["id"], actor, arrival, "current", "current", {"replaced_with": reading}
            )
            return updated

        late_id = str(reading.pop("id", "") or uuid4())
        late = self.repository.create_entity(late_id, "telemetry", "late", reading, actor.user_id)
        self.audit.record(
            late_id,
            actor,
            arrival,
            None,
            "late",
            {"reading": reading, "current_id": holder["id"]},
        )
        return late

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        if kind == "telemetry":
            entity = self._accept_telemetry(actor, payload, "record_telemetry")
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, entity["id"])
            return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "telemetry" and action == "revise":
            # Revisions are merged by observation time, not by revision number:
            # newer observations replace the current value, older arrivals
            # become late versions.
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), self._lookup
            )
            payload = dict(entity["data"])
            payload.update(patch)
            return self._accept_telemetry(actor, payload, "revise")
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
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

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
