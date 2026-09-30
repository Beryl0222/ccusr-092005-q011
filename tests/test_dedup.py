"""跨机构去重：合并保留来源、情节窗口、夜间批次幂等。"""

import unittest

from surveillance import MonitoringService

from factories import NIGHT, dt, report_payload


class DedupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [NIGHT]
        self.service = MonitoringService(clock=lambda: self.now[0])

    def test_cross_institution_merge_keeps_sources(self) -> None:
        """同一脱敏令牌被学校和医院分别上报 → 合并为一个个案，来源都保留。"""
        r1 = self.service.submit_report(
            report_payload("rpt-school-1", "school-a", "school", "tok-child-1", dt(21, 0)),
            "teacher-1",
        )
        r2 = self.service.submit_report(
            report_payload("rpt-hosp-1", "hospital-a", "hospital", "tok-child-1", dt(22, 30)),
            "nurse-1",
        )
        self.assertEqual(r1["case_id"], r2["case_id"])
        self.assertFalse(r2["created_case"])
        case = self.service.store.cases[r1["case_id"]]
        self.assertEqual(case.report_ids, ["rpt-school-1", "rpt-hosp-1"])
        self.assertEqual(sorted(case.institution_ids), ["hospital-a", "school-a"])

    def test_new_episode_after_window_creates_new_case(self) -> None:
        self.service.submit_report(
            report_payload("rpt-1", "school-a", "school", "tok-1", dt(21, 0, day=1)), "a"
        )
        result = self.service.submit_report(
            report_payload("rpt-2", "school-a", "school", "tok-1", dt(21, 0, day=20)), "a"
        )
        self.assertTrue(result["created_case"])
        self.assertEqual(len(self.service.store.cases), 2)

    def test_resubmit_same_report_is_idempotent(self) -> None:
        payload = report_payload("rpt-dup", "school-a", "school", "tok-1")
        first = self.service.submit_report(payload, "a")
        second = self.service.submit_report(payload, "a")
        self.assertEqual(first["case_id"], second["case_id"])
        self.assertEqual(len(self.service.store.reports), 1)
        case = self.service.store.cases[first["case_id"]]
        self.assertEqual(case.report_ids, ["rpt-dup"])

    def test_night_batch_flag_and_idempotency(self) -> None:
        """夜间集中上报：整批幂等，重放不产生重复。"""
        payloads = [
            report_payload(f"rpt-b{i}", "school-a", "school", f"tok-b{i}", dt(22, i))
            for i in range(3)
        ]
        first = self.service.submit_batch(
            "school-a", "batch-20260919-night", payloads, "teacher-1", submitted_at=dt(23, 10)
        )
        self.assertTrue(first["night_batch"])
        replay = self.service.submit_batch(
            "school-a", "batch-20260919-night", payloads, "teacher-1", submitted_at=dt(23, 40)
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["report_ids"], replay["report_ids"])
        self.assertEqual(len(self.service.store.reports), 3)

    def test_daytime_batch_not_flagged_night(self) -> None:
        result = self.service.submit_batch(
            "community-a",
            "batch-day",
            [report_payload("rpt-d1", "community-a", "community", "tok-d1", dt(10, 0))],
            "worker-1",
            submitted_at=dt(10, 30),
        )
        self.assertFalse(result["night_batch"])


if __name__ == "__main__":
    unittest.main()
