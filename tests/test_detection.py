"""阈值事件检测：空间/时间/共同活动达标才生成待研判事件，且不是诊断。"""

import unittest

from surveillance import MonitoringService, ThresholdConfig

from factories import NIGHT, dt, report_payload


def three_institution_reports(service: MonitoringService) -> None:
    service.submit_report(report_payload("r1", "hospital-a", "hospital", "tok-1", dt(21, 0)), "a")
    service.submit_report(report_payload("r2", "school-a", "school", "tok-2", dt(21, 30)), "b")
    service.submit_report(report_payload("r3", "community-a", "community", "tok-3", dt(22, 0)), "c")


class DetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [dt(23, 0)]
        self.config = ThresholdConfig(
            time_window_hours=6, min_cases=3, min_institutions=2, activity_tag="河边活动"
        )
        self.service = MonitoringService(clock=lambda: self.now[0], thresholds=self.config)

    def test_event_generated_when_thresholds_met(self) -> None:
        three_institution_reports(self.service)
        events = self.service.run_detection()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.status.value, "pending_review")
        self.assertEqual(len(event.case_ids), 3)
        self.assertEqual(len(event.institution_ids), 3)
        self.assertEqual(event.region, "滨河街道")
        self.assertEqual(event.activity_tag, "河边活动")

    def test_hypothesis_is_not_diagnosis(self) -> None:
        three_institution_reports(self.service)
        (event,) = self.service.run_detection()
        self.assertIn("疑似", event.hypothesis)
        self.assertIn("待核实", event.hypothesis)
        self.assertNotIn("确诊", event.hypothesis)

    def test_below_min_cases_no_event(self) -> None:
        self.service.submit_report(report_payload("r1", "hospital-a", "hospital", "tok-1"), "a")
        self.service.submit_report(report_payload("r2", "school-a", "school", "tok-2"), "b")
        self.assertEqual(self.service.run_detection(), [])

    def test_single_institution_no_event(self) -> None:
        """同一机构扎堆上报不足以生成事件（需跨机构印证）。"""
        for i in range(3):
            self.service.submit_report(
                report_payload(f"r{i}", "school-a", "school", f"tok-{i}", dt(21, i)), "a"
            )
        self.assertEqual(self.service.run_detection(), [])

    def test_common_activity_required(self) -> None:
        self.service.submit_report(report_payload("r1", "hospital-a", "hospital", "tok-1", tags=()), "a")
        self.service.submit_report(report_payload("r2", "school-a", "school", "tok-2", tags=()), "b")
        self.service.submit_report(report_payload("r3", "community-a", "community", "tok-3", tags=()), "c")
        self.assertEqual(self.service.run_detection(), [])

    def test_time_window_separates_clusters(self) -> None:
        three_institution_reports(self.service)
        # 三天后另一波，超出 6 小时时间窗
        self.service.submit_report(report_payload("r4", "hospital-a", "hospital", "tok-4", dt(21, 0, day=22)), "a")
        self.service.submit_report(report_payload("r5", "school-a", "school", "tok-5", dt(21, 30, day=22)), "b")
        self.service.submit_report(report_payload("r6", "community-a", "community", "tok-6", dt(22, 0, day=22)), "c")
        events = self.service.run_detection()
        self.assertEqual(len(events), 2)

    def test_duplicate_reports_count_once(self) -> None:
        """同一人被两机构重复上报，合并后只占一个个案名额。"""
        self.service.submit_report(report_payload("r1", "school-a", "school", "tok-1", dt(21, 0)), "a")
        self.service.submit_report(report_payload("r2", "hospital-a", "hospital", "tok-1", dt(21, 10)), "b")
        self.service.submit_report(report_payload("r3", "community-a", "community", "tok-2", dt(21, 20)), "c")
        # 去重后只有 2 个个案，不足 min_cases=3
        self.assertEqual(self.service.run_detection(), [])

    def test_rerun_absorbs_new_cases_without_duplicate_event(self) -> None:
        three_institution_reports(self.service)
        first_run = self.service.run_detection()
        self.assertEqual(len(first_run), 1)
        # 无新增 → 幂等
        self.assertEqual(self.service.run_detection(), [])
        # 新增个案并入既有事件
        self.service.submit_report(report_payload("r4", "hospital-b", "hospital", "tok-9", dt(22, 30)), "d")
        second_run = self.service.run_detection()
        self.assertEqual(len(second_run), 1)
        event = second_run[0]
        self.assertEqual(event.id, first_run[0].id)
        self.assertEqual(len(event.case_ids), 4)
        self.assertEqual(event.version, 2)

    def test_spatial_separation(self) -> None:
        """不同地点（不同已知场所）不并入同一事件。"""
        three_institution_reports(self.service)
        for i, inst in enumerate(["hospital-a", "school-a", "community-a"]):
            self.service.submit_report(
                report_payload(
                    f"far-{i}", inst, "community", f"tok-far-{i}", dt(21, i),
                    place="城西体育场", place_id="location-west-stadium", region="城西街道",
                ),
                "x",
            )
        events = self.service.run_detection()
        self.assertEqual(len(events), 2)
        regions = sorted(e.region for e in events)
        self.assertEqual(regions, ["城西街道", "滨河街道"])


if __name__ == "__main__":
    unittest.main()
