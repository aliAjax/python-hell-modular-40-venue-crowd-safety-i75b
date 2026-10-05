from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, ENTRY_VOID_ROLES, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine, validate_entry


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
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None, idempotency_key=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
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
        refreshed = self._after_transition(actor, updated, action, data or {}, idempotency_key)
        return refreshed or updated

    def _after_transition(self, actor, entity, action, data, idempotency_key):
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "zone" and action == "admit":
            self._post_admission(actor, entity, data, idempotency_key)
            return self.repository.get_entity(entity["id"])
        elif kind == "zone" and action == "evacuate":
            self._post_evacuation(actor, entity, data)
            return self.repository.get_entity(entity["id"])
        elif kind == "task" and action == "bring_back":
            self._post_bringback(actor, entity, data)
            return self.repository.get_entity(entity["id"])
        elif kind == "medical_point" and action == "admit_patient":
            self._post_medical_admit(actor, entity, data)
            return self.repository.get_entity(entity["id"])
        return None

    def _post_admission(self, actor, zone, data, idempotency_key):
        count = int(data["count"])
        source_ref = (
            data.get("source_ref")
            or data.get("entry_ref")
            or "admit:%s:%s" % (data.get("gate_id"), data.get("admitted_at"))
        )
        self.record_entry(
            actor,
            zone["id"],
            "admission",
            source_ref,
            count,
            occurred_at=data.get("admitted_at"),
            idempotency_key=idempotency_key,
            extra={"gate_id": data.get("gate_id")},
        )

    def _post_evacuation(self, actor, zone, data):
        current = self.repository.headcount_for_zone(zone["id"])["headcount"]
        raw_count = data.get("count")
        if raw_count is None or raw_count == "":
            quantity = -current
        else:
            count = int(raw_count)
            if count <= 0 or count > current:
                raise ValidationError("evacuation count must be between 1 and current headcount")
            quantity = -count
        if quantity == 0:
            return
        source_ref = "evacuate:%s:%s" % (zone["id"], data.get("evacuated_at") or data.get("reason"))
        self.record_entry(
            actor,
            zone["id"],
            "evacuation",
            source_ref,
            quantity,
            occurred_at=data.get("evacuated_at"),
        )

    def _post_bringback(self, actor, task, data):
        count = int(data["count"])
        zone_id = task["data"].get("zone_id")
        source_ref = (
            data.get("source_ref")
            or "bringback:%s:%s" % (task["id"], data.get("brought_at") or data.get("completed_at"))
        )
        self.record_entry(
            actor,
            zone_id,
            "bringback",
            source_ref,
            count,
            occurred_at=data.get("brought_at"),
            extra={"task_id": task["id"], "team_id": task["data"].get("team_id")},
        )

    def _post_medical_admit(self, actor, medical_point, data):
        count = int(data["count"])
        zone_id = medical_point["data"].get("zone_id")
        source_ref = (
            data.get("source_ref")
            or "medical:%s:%s" % (medical_point["id"], data.get("admitted_at") or data.get("reported_at"))
        )
        self.record_entry(
            actor,
            zone_id,
            "medical",
            source_ref,
            count,
            occurred_at=data.get("admitted_at"),
            extra={"medical_point_id": medical_point["id"]},
        )

    def record_entry(
        self, actor, zone_id, source_type, source_ref, quantity,
        occurred_at=None, idempotency_key=None, extra=None,
    ):
        """落账一条来源分录。同一来源重复提交只算一条，重试沿用同一幂等键。"""
        zone = self.repository.get_entity(zone_id)
        if not zone or self.rules.normalize_kind(zone["kind"]) != "zone":
            raise NotFoundError("zone not found: " + zone_id)
        if not source_ref or not str(source_ref).strip():
            raise ValidationError("source_ref is required")
        quantity = validate_entry(actor, source_type, quantity)
        if source_type in ("admission", "bringback", "medical"):
            current = self.repository.headcount_for_zone(zone_id)["headcount"]
            capacity = int(zone["data"].get("capacity", 0) or 0)
            if current + quantity > capacity:
                raise ConflictError("zone capacity would be exceeded")
        result = self.repository.record_headcount_entry(
            actor.user_id,
            zone_id,
            source_type,
            str(source_ref),
            quantity,
            occurred_at,
            idempotency_key,
            extra,
        )
        entry = result["entry"]
        if result["duplicated"]:
            self.audit.record(
                entry["id"], actor, "entry_duplicate", entry["status"], entry["status"],
                {"source_type": source_type, "source_ref": source_ref},
            )
        else:
            self.audit.record(
                entry["id"], actor, "entry_post", None, entry["status"],
                {"source_type": source_type, "source_ref": source_ref, "quantity": quantity},
            )
        return result

    def void_entry(self, actor, entry_id, reason):
        """作废一条来源分录并立即重算人数。只有值班指挥员能作废。"""
        if actor.role not in ENTRY_VOID_ROLES:
            raise PermissionDenied("only the duty commander can void entries")
        if not reason or not str(reason).strip():
            raise ValidationError("void reason is required")
        entry = self.repository.get_headcount_entry(entry_id)
        if not entry:
            raise NotFoundError("headcount entry not found: " + entry_id)
        updated = self.repository.void_headcount_entry(entry_id, actor.user_id, reason)
        self.audit.record(
            entry_id, actor, "entry_void", entry["status"], updated["status"],
            {"reason": reason, "zone_id": entry["zone_id"]},
        )
        return updated

    def update_entry(self, actor, entry_id, quantity=None, occurred_at=None, reason=None):
        """更新一条来源分录并立即重算人数。"""
        entry = self.repository.get_headcount_entry(entry_id)
        if not entry:
            raise NotFoundError("headcount entry not found: " + entry_id)
        if entry["status"] != "confirmed":
            raise ConflictError("cannot update a void entry: " + entry_id)
        if actor.role not in ("coordinator", "admin") and entry["created_by"] != actor.user_id:
            raise PermissionDenied("only the creator or a commander can update this entry")
        if quantity is not None:
            validate_entry(actor, entry["source_type"], quantity)
        updated = self.repository.update_headcount_entry(
            entry_id, quantity, occurred_at, actor.user_id
        )
        self.audit.record(
            entry_id, actor, "entry_update", entry["status"], updated["status"],
            {"quantity": quantity, "occurred_at": occurred_at, "reason": reason},
        )
        return updated

    def list_entries(self, zone_id=None, status=None):
        return self.repository.list_headcount_entries(zone_id=zone_id, status=status)

    def headcount(self, zone_id):
        zone = self.repository.get_entity(zone_id)
        if not zone or self.rules.normalize_kind(zone["kind"]) != "zone":
            raise NotFoundError("zone not found: " + zone_id)
        summary = self.repository.headcount_for_zone(zone_id)
        summary["capacity"] = int(zone["data"].get("capacity", 0))
        summary["zone_status"] = zone["status"]
        summary["over_capacity"] = summary["headcount"] > summary["capacity"]
        return summary

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
