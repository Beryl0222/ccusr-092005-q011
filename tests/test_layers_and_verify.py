"""信息分层与核实规则：自述/医护观察/已核实事实严格分层。"""

import unittest

from surveillance import MonitoringService, Role, Viewer
from surveillance.service import PermissionDenied, ServiceError

from factories import NIGHT, report_payload, statement


class LayeringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [NIGHT]
        self.service = MonitoringService(clock=lambda: self.now[0])
        self.verifier = Viewer("cdc-verifier", Role.VERIFIER)

    def _submit(self, statements) -> str:
        payload = report_payload(
            "rpt-1", "hospital-a", "hospital", "tok-subject-1", statements=statements
        )
        return self.service.submit_report(payload, "nurse-1")["report_id"]

    def test_layers_kept_separate(self) -> None:
        rid = self._submit(
            [
                statement("s1", "self_report", "昨晚在河边被虫爬过"),
                statement("s2", "medical_observation", "前臂条索状红斑伴水疱"),
            ]
        )
        report = self.service.store.reports[rid]
        layers = [s.layer.value for s in report.statements]
        self.assertEqual(layers, ["self_report", "medical_observation"])

    def test_verify_medical_observation(self) -> None:
        rid = self._submit(
            [statement("s1", "medical_observation", "前臂条索状红斑", ("门诊检查", "现场查看"))]
        )
        result = self.service.verify_statement(rid, "s1", self.verifier, "门诊与现场一致")
        self.assertEqual(result["layer"], "verified_fact")
        statement_obj = self.service.store.reports[rid].statements[0]
        self.assertEqual(statement_obj.verified_by, "cdc-verifier")
        self.assertIsNotNone(statement_obj.verified_at)

    def test_photo_only_cannot_be_verified(self) -> None:
        """线上照片不能当作确诊依据。"""
        rid = self._submit(
            [statement("s1", "self_report", "网友看照片说是隐翅虫皮炎", ("线上照片",))]
        )
        with self.assertRaises(ServiceError):
            self.service.verify_statement(rid, "s1", self.verifier, "看图判断")

    def test_folk_remedy_cannot_be_verified(self) -> None:
        """民间"牙膏止痒"说法不能核实为事实。"""
        rid = self._submit(
            [statement("s1", "self_report", "老人说涂牙膏止痒就行", ("电话",))]
        )
        with self.assertRaises(ServiceError):
            self.service.verify_statement(rid, "s1", self.verifier, "民间经验")

    def test_folk_remedy_registered_as_misinfo_lead(self) -> None:
        self._submit([statement("s1", "self_report", "群里说牙膏可以止痒", ("社交媒体",))])
        leads = list(self.service.store.misinfo_leads.values())
        self.assertEqual(len(leads), 1)
        self.assertIn("牙膏", leads[0].claim)
        self.assertEqual(leads[0].region, "滨河街道")

    def test_verify_requires_verifier_role(self) -> None:
        rid = self._submit([statement("s1", "medical_observation", "红斑", ("门诊检查",))])
        with self.assertRaises(PermissionDenied):
            self.service.verify_statement(rid, "s1", Viewer("nurse", Role.REPORTER), "越权")

    def test_verify_requires_evidence_channel(self) -> None:
        rid = self._submit([statement("s1", "self_report", "听说很多人中招", ())])
        with self.assertRaises(ServiceError):
            self.service.verify_statement(rid, "s1", self.verifier, "无凭据")

    def test_report_rejects_raw_identity(self) -> None:
        payload = report_payload("rpt-x", "school-a", "school", "tok-1")
        payload["student_name"] = "张某"
        with self.assertRaises(ServiceError):
            self.service.submit_report(payload, "teacher-1")


if __name__ == "__main__":
    unittest.main()
