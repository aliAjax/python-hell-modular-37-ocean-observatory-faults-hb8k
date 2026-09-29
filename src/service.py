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
            return self._create_telemetry(actor, payload, idempotency_key)
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

    def _create_telemetry(self, actor, payload, idempotency_key=None):
        self.rules.validate_create(actor, "telemetry", payload, self._lookup)
        prepared, current, _corrected, is_late = self.rules.prepare_telemetry_create(payload, self._lookup)
        prepared.pop("id", None)
        if current:
            if is_late:
                entity = self.repository.create_entity(
                    str(uuid4()), "telemetry", "late", prepared, actor.user_id
                )
                self.audit.record(entity["id"], actor, "late_telemetry", None, "late", {"current_id": current["id"]})
            else:
                entity = self.repository.update_entity(current["id"], None, "current", prepared)
                self.audit.record(entity["id"], actor, "update_current", "current", "current", {})
        else:
            entity_id = str(prepared.pop("id", "") or uuid4())
            if self.repository.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            entity = self.repository.create_entity(entity_id, "telemetry", "current", prepared, actor.user_id)
            self.audit.record(entity_id, actor, "create", None, "current", {"kind": "telemetry"})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity["id"])
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if self.rules.normalize_kind(entity["kind"]) == "telemetry" and action == "revise":
            return self._revise_telemetry(actor, entity, dict(data or {}), expected)
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

    def _revise_telemetry(self, actor, entity, data, expected_version):
        if entity["version"] != expected_version:
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, entity["version"])
            )
        baseline, patch, _corrected, mode = self.rules.prepare_telemetry_revision(
            actor, entity, data, self._lookup
        )
        if mode == "late":
            late = self.repository.create_entity(str(uuid4()), "telemetry", "late", patch, actor.user_id)
            self.audit.record(
                late["id"], actor, "revise_late", entity["status"], "late",
                {"current_id": baseline["id"]},
            )
            return late
        if mode == "create_current":
            current = self.repository.create_entity(str(uuid4()), "telemetry", "current", patch, actor.user_id)
            self.audit.record(
                current["id"], actor, "revive_current", entity["status"], "current",
                {"source_id": entity["id"]},
            )
            return current
        if mode == "replace_current":
            updated = self.repository.update_entity(entity["id"], expected_version, "current", patch)
            self.audit.record(
                entity["id"], actor, "restore_current", entity["status"], "current", {}
            )
            return updated
        if baseline["id"] != entity["id"]:
            updated = self.repository.update_entity(baseline["id"], None, "current", patch)
            self.audit.record(
                baseline["id"], actor, "update_current", "current", "current",
                {"source_id": entity["id"]},
            )
            return updated

        next_status, generic_patch = self.rules.validate_transition(
            actor, entity, "revise", data, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(generic_patch)
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected_version, "current", merged)
        self.audit.record(
            entity["id"], actor, "revise", entity["status"], updated["status"], {"patch": patch}
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
