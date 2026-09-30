"""访问控制：儿童信息只向实际处置者开放。"""

import unittest

from surveillance import MonitoringService, Role, Viewer, ThresholdConfig

from factories import NIGHT, dt, report_payload, statement


class ChildAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [dt(23, 0)]
        self.service = MonitoringService(
            clock=lambda: self.now[0],
            thresholds=ThresholdConfig(time_window_hours=6, min_cases=3, min_institutions=2),
        )
        self.service.submit_report(
            report_payload(
                "rpt-child", "school-a", "school", "tok-child", dt(21, 0),
                is_child=True, age_band="6-12",
                statements=[statement("s1", "self_report", "孩子说胳膊疼")],
            ),
            "teacher-1",
        )
        self.service.submit_report(report_payload("rpt-a", "hospital-a", "hospital", "tok-a", dt(21, 30)), "n")
        self.service.submit_report(report_payload("rpt-b", "community-a", "community", "tok-b", dt(22, 0)), "w")
        (self.event,) = self.service.run_detection()

    def test_duty_officer_sees_masked_child_detail(self) -> None:
        view = self.service.open_alert(self.event.id, Viewer("duty-1", Role.DUTY_OFFICER))
        child_case = next(c for c in view["constituent_cases"] if c["is_child"])
        child_report = child_case["reports"][0]
        self.assertTrue(child_report["masked"])
        self.assertNotIn("孩子说胳膊疼", str(child_report["statements"]))
        # 结构信息仍可见：分层与核实状态不泄露内容
        self.assertEqual(child_report["statements"][0]["layer"], "self_report")
        pending = [f for f in view["pending_facts"] if f["report_id"] == "rpt-child"]
        self.assertEqual(len(pending), 1)
        self.assertIn("仅实际处置者可见", pending[0]["text"])

    def test_assigned_handler_sees_child_detail(self) -> None:
        duty = Viewer("duty-1", Role.DUTY_OFFICER)
        self.service.assign_handler(self.event.id, "handler-7", duty)
        view = self.service.open_alert(self.event.id, Viewer("handler-7", Role.HANDLER))
        child_case = next(c for c in view["constituent_cases"] if c["is_child"])
        self.assertFalse(child_case["reports"][0]["masked"])
        self.assertIn("孩子说胳膊疼", str(child_case["reports"][0]["statements"]))

    def test_unassigned_handler_still_masked(self) -> None:
        duty = Viewer("duty-1", Role.DUTY_OFFICER)
        self.service.assign_handler(self.event.id, "handler-7", duty)
        view = self.service.open_alert(self.event.id, Viewer("handler-8", Role.HANDLER))
        child_case = next(c for c in view["constituent_cases"] if c["is_child"])
        self.assertTrue(child_case["reports"][0]["masked"])

    def test_child_detail_access_is_audited(self) -> None:
        duty = Viewer("duty-1", Role.DUTY_OFFICER)
        self.service.assign_handler(self.event.id, "handler-7", duty)
        self.service.open_alert(self.event.id, Viewer("handler-7", Role.HANDLER))
        actions = [a.action for a in self.service.store.audit]
        self.assertIn("view_child_detail", actions)

    def test_assign_handler_requires_privileged_role(self) -> None:
        from surveillance.service import PermissionDenied

        with self.assertRaises(PermissionDenied):
            self.service.assign_handler(self.event.id, "handler-9", Viewer("nurse", Role.REPORTER))


if __name__ == "__main__":
    unittest.main()
