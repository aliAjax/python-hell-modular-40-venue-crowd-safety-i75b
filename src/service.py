from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .repository import utcnow
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

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
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
        return updated

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

    def _get_zone(self, zone_id):
        zone = self.repository.get_entity(zone_id)
        if not zone or zone["kind"] != "zone":
            raise NotFoundError("zone not found: " + str(zone_id))
        return zone

    @staticmethod
    def _opening_entry(zone):
        """Backfill an opening-balance entry for legacy zones that have an
        occupancy figure but no ledger entries yet."""
        occupancy = int(zone["data"].get("current_occupancy", 0) or 0)
        if occupancy <= 0:
            return None
        now = utcnow()
        return {
            "id": str(uuid4()),
            "zone_id": zone["id"],
            "source_type": "opening",
            "source_ref": "opening-backfill",
            "quantity": occupancy,
            "status": "active",
            "note": "backfilled from legacy current_occupancy",
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        }

    def record_zone_entry(self, actor, zone_id, data, idempotency_key=None):
        """Record one source entry and immediately recalculate the zone.

        Confirmed entries survive later failures; a retry of an incomplete
        submission reuses the same idempotency key and returns the original
        entry instead of duplicating it.
        """
        zone = self._get_zone(zone_id)
        validated = self.rules.validate_ledger_record(actor, dict(data or {}))
        now = utcnow()
        entry = {
            "id": str(uuid4()),
            "zone_id": zone["id"],
            "source_type": validated["source_type"],
            "source_ref": validated["source_ref"],
            "quantity": validated["quantity"],
            "status": "active",
            "note": validated["note"],
            "created_by": actor.user_id,
            "created_at": now,
            "updated_at": now,
        }
        entry, updated_zone, created = self.repository.record_zone_entry(
            entry,
            actor.user_id,
            idem_key=idempotency_key,
            check_zone=lambda z: self.rules.check_ledger_zone_status(z, validated["source_type"]),
            make_opening=self._opening_entry,
            recalc=self.rules.recalculate_zone,
        )
        self.audit.record(
            zone["id"],
            actor,
            "ledger_record",
            zone["status"],
            updated_zone["status"],
            {"entry": entry, "created": created},
        )
        return {"entry": entry, "zone": updated_zone, "created": created}

    def adjust_zone_entry(self, actor, entry_id, data):
        validated = self.rules.validate_ledger_adjust(actor, dict(data or {}))

        def mutate(entry):
            if entry["status"] != "active":
                raise ConflictError("cannot adjust a voided entry")
            sign = self.rules.LEDGER_SOURCE_SIGNS[entry["source_type"]]
            changes = {"quantity": sign * validated["count"]}
            if validated["note"] is not None:
                changes["note"] = validated["note"]
            return changes

        entry, zone = self.repository.mutate_zone_entry(
            entry_id, mutate, self.rules.recalculate_zone
        )
        self.audit.record(
            entry["zone_id"],
            actor,
            "ledger_adjust",
            None,
            zone["status"],
            {"entry_id": entry_id, "quantity": entry["quantity"], "reason": validated["reason"]},
        )
        return {"entry": entry, "zone": zone}

    def void_zone_entry(self, actor, entry_id, data):
        validated = self.rules.validate_ledger_void(actor, dict(data or {}))

        def mutate(entry):
            if entry["status"] != "active":
                raise ConflictError("ledger entry is already voided")
            return {
                "status": "voided",
                "voided_by": actor.user_id,
                "voided_at": utcnow(),
                "void_reason": validated["reason"],
            }

        entry, zone = self.repository.mutate_zone_entry(
            entry_id, mutate, self.rules.recalculate_zone
        )
        self.audit.record(
            entry["zone_id"],
            actor,
            "ledger_void",
            None,
            zone["status"],
            {"entry_id": entry_id, "reason": validated["reason"]},
        )
        return {"entry": entry, "zone": zone}

    def ledger_entry_action(self, actor, entry_id, action, data=None):
        if action == "adjust":
            return self.adjust_zone_entry(actor, entry_id, data)
        if action == "void":
            return self.void_zone_entry(actor, entry_id, data)
        raise ValidationError("unknown ledger action: " + str(action))

    def get_zone_entry(self, entry_id):
        entry = self.repository.get_zone_entry(entry_id)
        if not entry:
            raise NotFoundError("ledger entry not found: " + str(entry_id))
        return entry

    def list_zone_entries(self, zone_id, include_voided=True):
        self._get_zone(zone_id)
        return self.repository.list_zone_entries(zone_id, include_voided=include_voided)
