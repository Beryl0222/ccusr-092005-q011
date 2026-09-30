"""HTTP 调度层冒烟测试（不起真实端口，直接打 dispatch）。"""

import unittest

from surveillance import MonitoringService, ThresholdConfig
from surveillance.api import Api

from factories import NIGHT, dt, report_payload, statement

H_REPORTER = {"x-actor-id": "nurse-1", "x-actor-role": "reporter"}
H_VERIFIER = {"x-actor-id": "cdc-1", "x-actor-role": "verifier"}
H_PUBLISHER = {"x-actor-id": "pub-1", "x-actor-role": "publisher"}
H_DUTY = {"x-actor-id": "duty-1", "x-actor-role": "duty_officer"}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [NIGHT]
        self.service = MonitoringService(
            clock=lambda: self.now[0],
            thresholds=ThresholdConfig(time_window_hours=6, min_cases=3, min_institutions=2),
        )
        self.api = Api(self.service)

    def _seed_reports(self) -> None:
        for rid, inst, kind, tok in [
            ("r1", "hospital-a", "hospital", "tok-1"),
            ("r2", "school-a", "school", "tok-2"),
            ("r3", "community-a", "community", "tok-3"),
        ]:
            status, _ = self.api.dispatch(
                "POST", "/reports",
                report_payload(rid, inst, kind, tok, dt(21, 0),
                               statements=[statement("s1", "medical_observation", "红斑", ("门诊检查",))]),
                H_REPORTER,
            )
            self.assertEqual(status, 200)

    def test_report_detect_alert_flow(self) -> None:
        self._seed_reports()
        status, body = self.api.dispatch("POST", "/detection/run", {"now": dt(23, 0).isoformat()}, H_DUTY)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["events"]), 1)
        event_id = body["events"][0]

        status, view = self.api.dispatch("GET", f"/events/{event_id}/alert", None, H_DUTY)
        self.assertEqual(status, 200)
        self.assertEqual(view["event"]["case_count"], 3)
        self.assertEqual(len(view["constituent_cases"]), 3)

    def test_verify_forbidden_for_reporter(self) -> None:
        self._seed_reports()
        status, body = self.api.dispatch(
            "POST", "/reports/r1/statements/s1/verify", {"note": "x"}, H_REPORTER
        )
        self.assertEqual(status, 403)

    def test_verify_ok_for_verifier(self) -> None:
        self._seed_reports()
        status, body = self.api.dispatch(
            "POST", "/reports/r1/statements/s1/verify", {"note": "复核一致"}, H_VERIFIER
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["layer"], "verified_fact")

    def test_unknown_route_404(self) -> None:
        status, _ = self.api.dispatch("GET", "/nope", None, H_DUTY)
        self.assertEqual(status, 404)

    def test_bad_payload_400(self) -> None:
        status, body = self.api.dispatch("POST", "/reports", {"id": "x"}, H_REPORTER)
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_batch_endpoint_idempotent(self) -> None:
        payload = {
            "institution_id": "school-a",
            "batch_id": "batch-night-1",
            "submitted_at": dt(23, 0).isoformat(),
            "reports": [report_payload(f"rb-{i}", "school-a", "school", f"tok-b{i}", dt(22, i)) for i in range(2)],
        }
        status1, body1 = self.api.dispatch("POST", "/batches", payload, H_REPORTER)
        status2, body2 = self.api.dispatch("POST", "/batches", payload, H_REPORTER)
        self.assertEqual((status1, status2), (200, 200))
        self.assertFalse(body1["idempotent_replay"])
        self.assertTrue(body2["idempotent_replay"])

    def test_entrance_and_material_endpoints(self) -> None:
        self._seed_reports()
        self.api.dispatch("POST", "/entrances", {"id": "ent-1", "region": "滨河街道", "label": "公告栏"}, H_PUBLISHER)
        _, body = self.api.dispatch("POST", "/detection/run", {"now": dt(23, 0).isoformat()}, H_DUTY)
        event_id = body["events"][0]
        status, material = self.api.dispatch("POST", f"/events/{event_id}/material", {}, H_PUBLISHER)
        self.assertEqual(status, 200)
        status, pub = self.api.dispatch("POST", f"/publications/{material['publication_id']}/publish", {}, H_PUBLISHER)
        self.assertEqual(status, 200)
        status, diss = self.api.dispatch("GET", f"/series/{pub['series_id']}/dissemination", None, H_DUTY)
        self.assertEqual(status, 200)
        self.assertTrue(diss["all_entrances_updated"])


if __name__ == "__main__":
    unittest.main()
