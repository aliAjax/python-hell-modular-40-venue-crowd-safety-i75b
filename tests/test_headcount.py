import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HeadcountLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "headcount.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("venue-commander", "coordinator")
        self.supervisor = Actor("safety-supervisor", "supervisor")
        self.operator = Actor("gate-operator", "operator")
        self.viewer = Actor("onlooker", "viewer")
        self._venue, self.zone, self.gate = self._venue_zone_gate(capacity=100)

    def tearDown(self):
        self.tmp.cleanup()

    def _venue_zone_gate(self, capacity=100):
        venue = self.service.create(
            self.coordinator, "venue", {"name": "V", "address": "A"}
        )
        zone = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": "Z", "capacity": capacity},
        )
        gate = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "G", "zone_ids": [zone["id"]]},
        )
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "op"})
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})
        return venue, zone, gate

    def test_every_source_posts_an_entry(self):
        # 入场
        self.service.transition(
            self.operator,
            self.zone["id"],
            "admit",
            {"gate_id": self.gate["id"], "count": 40, "admitted_at": "t1"},
        )
        # 现场任务带回
        incident = self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": self._venue["id"],
                "zone_id": self.zone["id"],
                "source_ref": "radio-1",
                "incident_type": "crowd",
                "severity": "medium",
                "reported_at": "t1",
            },
        )
        task = self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": incident["id"],
                "venue_id": self._venue["id"],
                "zone_id": self.zone["id"],
                "team_id": "team-1",
                "task_type": "crowd",
            },
        )
        self.service.transition(
            self.coordinator, task["id"], "assign", {"assigned_at": "t1"}
        )
        self.service.transition(
            self.operator, task["id"], "bring_back", {"count": 6, "brought_at": "t2"}
        )
        # 医疗点收治
        medical = self.service.create(
            self.supervisor,
            "medical_point",
            {"venue_id": self._venue["id"], "zone_id": self.zone["id"], "capacity": 10, "equipment_level": "basic"},
        )
        self.service.transition(
            self.supervisor, medical["id"], "activate", {"activated_at": "t1"}
        )
        self.service.transition(
            self.operator, medical["id"], "admit_patient", {"count": 4, "admitted_at": "t3"}
        )
        # 撤离
        self.service.transition(
            self.supervisor,
            self.zone["id"],
            "evacuate",
            {"reason": "drill", "evacuated_at": "t4"},
        )
        entries = self.service.list_entries(zone_id=self.zone["id"])
        by_source = {}
        for entry in entries:
            by_source.setdefault(entry["source_type"], 0)
            by_source[entry["source_type"]] += entry["quantity"]
        self.assertEqual(by_source["admission"], 40)
        self.assertEqual(by_source["bringback"], 6)
        self.assertEqual(by_source["medical"], 4)
        self.assertEqual(by_source["evacuation"], -50)
        # 40 + 6 + 4 - 50 = 0
        summary = self.service.headcount(self.zone["id"])
        self.assertEqual(summary["headcount"], 0)

    def test_duplicate_source_counts_once(self):
        first = self.service.record_entry(
            self.operator,
            self.zone["id"],
            "admission",
            "gate-a:t1",
            10,
            idempotency_key="idem-1",
        )
        self.assertFalse(first["duplicated"])
        # 同一来源引用重复提交
        second = self.service.record_entry(
            self.operator,
            self.zone["id"],
            "admission",
            "gate-a:t1",
            10,
            idempotency_key="idem-1",
        )
        self.assertTrue(second["duplicated"])
        self.assertEqual(first["entry"]["id"], second["entry"]["id"])
        entries = self.service.list_entries(zone_id=self.zone["id"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.service.headcount(self.zone["id"])["headcount"], 10)

    def test_concurrent_posts_are_serialized(self):
        results = []
        errors = []

        def post(i):
            try:
                results.append(
                    self.service.record_entry(
                        self.operator,
                        self.zone["id"],
                        "admission",
                        "gate-a:conc:%d" % i,
                        1,
                    )
                )
            except Exception as exc:  # pragma: no cover - diagnostic
                errors.append(exc)

        threads = [threading.Thread(target=post, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 20)
        self.assertTrue(all(not r["duplicated"] for r in results))
        self.assertEqual(self.service.headcount(self.zone["id"])["headcount"], 20)

    def test_concurrent_duplicate_source_counts_once(self):
        barrier = threading.Barrier(2)
        results = []

        def post():
            barrier.wait()
            results.append(
                self.service.record_entry(
                    self.operator,
                    self.zone["id"],
                    "admission",
                    "same-source",
                    5,
                )
            )

        threads = [threading.Thread(target=post) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["entry"]["id"] == results[1]["entry"]["id"])
        self.assertEqual(self.service.headcount(self.zone["id"])["headcount"], 5)

    def test_update_entry_recalculates_and_limits(self):
        result = self.service.record_entry(
            self.operator, self.zone["id"], "admission", "a:1", 60
        )
        entry = result["entry"]
        self.assertEqual(self.service.headcount(self.zone["id"])["headcount"], 60)
        # 更新数量到超出容量
        self.service.update_entry(self.coordinator, entry["id"], quantity=150)
        summary = self.service.headcount(self.zone["id"])
        self.assertEqual(summary["headcount"], 150)
        self.assertTrue(summary["over_capacity"])
        zone = self.service.get(self.zone["id"])
        self.assertEqual(zone["status"], "limited")
        # 受限后拒绝新的入场
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator,
                self.zone["id"],
                "admit",
                {"gate_id": self.gate["id"], "count": 1, "admitted_at": "t2"},
            )

    def test_void_entry_recalculates(self):
        result = self.service.record_entry(
            self.operator, self.zone["id"], "admission", "a:1", 30
        )
        entry = result["entry"]
        self.service.record_entry(
            self.operator, self.zone["id"], "admission", "a:2", 20
        )
        self.assertEqual(self.service.headcount(self.zone["id"])["headcount"], 50)
        self.service.void_entry(self.coordinator, entry["id"], "duplicate")
        summary = self.service.headcount(self.zone["id"])
        self.assertEqual(summary["headcount"], 20)
        voided = self.service.repository.get_headcount_entry(entry["id"])
        self.assertEqual(voided["status"], "void")

    def test_only_commander_can_void(self):
        result = self.service.record_entry(
            self.operator, self.zone["id"], "admission", "a:1", 10
        )
        entry = result["entry"]
        with self.assertRaises(PermissionDenied):
            self.service.void_entry(self.operator, entry["id"], "oops")
        with self.assertRaises(PermissionDenied):
            self.service.void_entry(self.supervisor, entry["id"], "oops")
        # 作废仍然成功
        self.service.void_entry(self.coordinator, entry["id"], "ok")

    def test_confirmed_entries_survive_failed_write(self):
        # 先落账一条确认分录
        self.service.record_entry(
            self.operator, self.zone["id"], "admission", "a:1", 10, idempotency_key="k1"
        )
        # 后续非法写入失败
        with self.assertRaises((ValidationError, ConflictError)):
            self.service.record_entry(
                self.operator, self.zone["id"], "admission", "a:2", -5
            )
        # 已确认分录保留
        self.assertEqual(self.service.headcount(self.zone["id"])["headcount"], 10)
        # 重试沿用同一幂等键，不重复落账
        retry = self.service.record_entry(
            self.operator, self.zone["id"], "admission", "a:1", 10, idempotency_key="k1"
        )
        self.assertTrue(retry["duplicated"])
        self.assertEqual(self.service.headcount(self.zone["id"])["headcount"], 10)

    def test_backfill_opening_balance(self):
        # 模拟旧数据：区域已有人数但没有分录
        self.service.transition(
            self.operator,
            self.zone["id"],
            "admit",
            {"gate_id": self.gate["id"], "count": 30, "admitted_at": "t1"},
        )
        # 直接清空分录表，制造“无分录”状态，并把人数改成 70
        with self.service.repository._connect() as connection:
            connection.execute("DELETE FROM headcount_entries")
            connection.execute(
                "UPDATE entities SET data = ? WHERE id = ?",
                ('{"current_occupancy": 70, "capacity": 100}', self.zone["id"]),
            )
        # 新的入场触发期初回填
        result = self.service.record_entry(
            self.operator, self.zone["id"], "admission", "a:2", 5
        )
        self.assertTrue(result["opening_created"])
        summary = self.service.headcount(self.zone["id"])
        self.assertEqual(summary["headcount"], 75)
        entries = self.service.list_entries(zone_id=self.zone["id"])
        opening = [e for e in entries if e["source_type"] == "opening"]
        self.assertEqual(len(opening), 1)
        self.assertEqual(opening[0]["quantity"], 70)

    def test_evacuation_rejects_more_than_present(self):
        self.service.record_entry(
            self.operator, self.zone["id"], "admission", "a:1", 20
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.supervisor,
                self.zone["id"],
                "evacuate",
                {"reason": "x", "count": 50, "evacuated_at": "t"},
            )


if __name__ == "__main__":
    unittest.main()
