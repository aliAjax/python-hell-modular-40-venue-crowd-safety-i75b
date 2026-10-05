import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "ledger.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("duty-commander", "coordinator")
        self.supervisor = Actor("safety-supervisor", "supervisor")
        self.operator = Actor("gate-operator", "operator")
        self.medic = Actor("medic-operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _open_zone(self, capacity=100):
        venue = self.service.create(self.coordinator, "venue", {"name": "V", "address": "A"})
        zone = self.service.create(
            self.coordinator, "zone", {"venue_id": venue["id"], "name": "Z", "capacity": capacity}
        )
        gate = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "G", "zone_ids": [zone["id"]]},
        )
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "operator"})
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})
        return venue, zone, gate

    def _record(self, actor, zone_id, source_type, source_ref, count, key=None):
        return self.service.record_zone_entry(
            actor,
            zone_id,
            {"source_type": source_type, "source_ref": source_ref, "count": count},
            idempotency_key=key,
        )

    def test_sources_reconcile_into_single_occupancy(self):
        _, zone, _ = self._open_zone()
        self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 100)
        self._record(self.operator, zone["id"], "return", "task:t7:run-1", 20)
        self._record(self.medic, zone["id"], "intake", "medical:m1:shift-1", 5)
        result = self._record(self.supervisor, zone["id"], "evacuation", "evac:convoy-1", 30)
        self.assertEqual(result["zone"]["data"]["current_occupancy"], 100 + 20 - 5 - 30)

        entries = self.service.list_zone_entries(zone["id"])
        self.assertEqual(len(entries), 4)
        self.assertEqual(
            [entry["source_type"] for entry in entries],
            ["admission", "return", "intake", "evacuation"],
        )
        self.assertEqual(
            [entry["quantity"] for entry in entries], [100, 20, -5, -30]
        )
        self.assertEqual(
            [entry["seq"] for entry in entries], sorted(entry["seq"] for entry in entries)
        )

    def test_duplicate_source_and_idempotent_retry_count_once(self):
        _, zone, _ = self._open_zone()
        first = self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 40, key="k-1")
        self.assertTrue(first["created"])

        # Same source submitted again: still one entry, occupancy unchanged.
        again = self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 40)
        self.assertFalse(again["created"])
        self.assertEqual(again["entry"]["id"], first["entry"]["id"])
        self.assertEqual(again["zone"]["data"]["current_occupancy"], 40)

        # Retry of an incomplete write reuses the same idempotency key.
        retry = self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 40, key="k-1")
        self.assertFalse(retry["created"])
        self.assertEqual(retry["entry"]["id"], first["entry"]["id"])
        entries = self.service.list_zone_entries(zone["id"])
        self.assertEqual(len(entries), 1)

    def test_over_capacity_limits_zone_and_rejects_admission(self):
        _, zone, _ = self._open_zone(capacity=100)
        self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 80)
        result = self._record(self.operator, zone["id"], "return", "task:t1:run-1", 30)
        self.assertEqual(result["zone"]["data"]["current_occupancy"], 110)
        self.assertEqual(result["zone"]["status"], "limited")

        # New admissions are rejected while limited; evacuations still post.
        with self.assertRaises(ConflictError):
            self._record(self.operator, zone["id"], "admission", "gate:g1:batch-2", 1)
        result = self._record(self.supervisor, zone["id"], "evacuation", "evac:convoy-1", 60)
        self.assertEqual(result["zone"]["data"]["current_occupancy"], 50)
        self.assertEqual(result["zone"]["status"], "limited")

        recovered = self.service.transition(
            self.supervisor, zone["id"], "recover", {"checklist": "cleared"}
        )
        self.assertEqual(recovered["status"], "open")
        result = self._record(self.operator, zone["id"], "admission", "gate:g1:batch-3", 10)
        self.assertEqual(result["zone"]["data"]["current_occupancy"], 60)

    def test_adjust_and_void_recalculate_immediately(self):
        _, zone, _ = self._open_zone()
        recorded = self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 60)
        entry_id = recorded["entry"]["id"]

        adjusted = self.service.ledger_entry_action(
            self.operator, entry_id, "adjust", {"count": 75, "reason": "recount at gate"}
        )
        self.assertEqual(adjusted["entry"]["quantity"], 75)
        self.assertEqual(adjusted["zone"]["data"]["current_occupancy"], 75)

        # Only the duty commander may void an entry.
        with self.assertRaises(PermissionDenied):
            self.service.ledger_entry_action(
                self.operator, entry_id, "void", {"reason": "double reported"}
            )
        voided = self.service.ledger_entry_action(
            self.coordinator, entry_id, "void", {"reason": "double reported"}
        )
        self.assertEqual(voided["entry"]["status"], "voided")
        self.assertEqual(voided["entry"]["voided_by"], "duty-commander")
        self.assertEqual(voided["zone"]["data"]["current_occupancy"], 0)

        with self.assertRaises(ConflictError):
            self.service.ledger_entry_action(
                self.coordinator, entry_id, "void", {"reason": "again"}
            )
        with self.assertRaises(ConflictError):
            self.service.ledger_entry_action(
                self.operator, entry_id, "adjust", {"count": 10, "reason": "too late"}
            )

        active = self.service.list_zone_entries(zone["id"], include_voided=False)
        self.assertEqual(active, [])
        everything = self.service.list_zone_entries(zone["id"])
        self.assertEqual(len(everything), 1)

    def test_adjust_can_push_zone_over_capacity(self):
        _, zone, _ = self._open_zone(capacity=100)
        recorded = self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 90)
        adjusted = self.service.ledger_entry_action(
            self.supervisor,
            recorded["entry"]["id"],
            "adjust",
            {"count": 120, "reason": "gate undercounted"},
        )
        self.assertEqual(adjusted["zone"]["data"]["current_occupancy"], 120)
        self.assertEqual(adjusted["zone"]["status"], "limited")

    def test_legacy_zone_backfilled_with_opening_balance(self):
        venue, zone, gate = self._open_zone()
        # Legacy path: occupancy set directly before any ledger entries exist.
        self.service.transition(
            self.operator,
            zone["id"],
            "admit",
            {"gate_id": gate["id"], "count": 40, "admitted_at": "t1"},
        )
        result = self._record(self.supervisor, zone["id"], "evacuation", "evac:convoy-1", 10)
        self.assertEqual(result["zone"]["data"]["current_occupancy"], 30)

        entries = self.service.list_zone_entries(zone["id"])
        self.assertEqual(len(entries), 2)
        opening = entries[0]
        self.assertEqual(opening["source_type"], "opening")
        self.assertEqual(opening["quantity"], 40)
        self.assertEqual(opening["created_by"], "system")

        # Backfill happens once: later entries do not create more openings.
        self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 5)
        entries = self.service.list_zone_entries(zone["id"])
        self.assertEqual(len([e for e in entries if e["source_type"] == "opening"]), 1)
        self.assertEqual(
            self.service.get(zone["id"])["data"]["current_occupancy"], 35
        )

    def test_concurrent_submissions_settle_in_server_order(self):
        _, zone, _ = self._open_zone(capacity=1000)
        errors = []

        def submit(operator, prefix):
            try:
                for index in range(10):
                    self._record(
                        operator, zone["id"], "admission", "%s:%d" % (prefix, index), 1
                    )
            except Exception as exc:  # pragma: no cover - failure assertion path
                errors.append(exc)

        first = threading.Thread(target=submit, args=(self.operator, "gate:g1"))
        second = threading.Thread(target=submit, args=(self.medic, "gate:g2"))
        first.start()
        second.start()
        first.join()
        second.join()

        self.assertEqual(errors, [])
        entries = self.service.list_zone_entries(zone["id"])
        self.assertEqual(len(entries), 20)
        self.assertEqual(len({entry["seq"] for entry in entries}), 20)
        self.assertEqual(
            self.service.get(zone["id"])["data"]["current_occupancy"], 20
        )

    def test_failed_write_preserves_confirmed_entries(self):
        _, zone, _ = self._open_zone()
        confirmed = self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 40, key="k-1")

        with self.assertRaises(ValidationError):
            self._record(self.operator, zone["id"], "admission", "gate:g1:batch-2", -5)
        with self.assertRaises(ValidationError):
            self._record(self.operator, zone["id"], "unknown", "x:1", 5)
        with self.assertRaises(PermissionDenied):
            self._record(Actor("watcher", "viewer"), zone["id"], "admission", "gate:g1:batch-3", 5)

        entries = self.service.list_zone_entries(zone["id"])
        self.assertEqual([entry["id"] for entry in entries], [confirmed["entry"]["id"]])
        self.assertEqual(
            self.service.get(zone["id"])["data"]["current_occupancy"], 40
        )

        # Retrying the failed submission with the confirmed key is a no-op.
        retry = self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 40, key="k-1")
        self.assertFalse(retry["created"])
        self.assertEqual(len(self.service.list_zone_entries(zone["id"])), 1)

    def test_entries_rejected_when_zone_closed(self):
        _, zone, _ = self._open_zone()
        self.service.transition(self.supervisor, zone["id"], "close", {"reason": "event over"})
        with self.assertRaises(ConflictError):
            self._record(self.operator, zone["id"], "admission", "gate:g1:batch-1", 10)
        with self.assertRaises(ConflictError):
            self._record(self.supervisor, zone["id"], "evacuation", "evac:convoy-1", 10)


if __name__ == "__main__":
    unittest.main()
