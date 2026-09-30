"""端到端：入秋同一夜，医院/学校/社区集中上报 → 去重 → 待研判事件 →
区域处置材料 → 错误科普更正 → 值班员确认所有公开入口已更新。"""

import unittest
from datetime import timedelta

from surveillance import MonitoringService, Role, ThresholdConfig, Viewer
from surveillance.store import Store

from factories import NIGHT, dt, report_payload, statement

SEED = {
    "project": "隐翅虫暴露事件观察",
    "records": [
        {
            "id": "incident-campus-001", "kind": "exposure", "place": "学校宿舍",
            "contact": "拍打虫体", "skin_area": "前臂", "reported_at": "2026-09-19T22:15:00+08:00",
        },
        {
            "id": "location-riverside-park", "kind": "risk_site",
            "features": ["水边", "绿化带", "夜间照明"], "season": "5月至10月中下旬",
        },
    ],
}


class AutumnNightFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [dt(20, 0)]
        self.service = MonitoringService(
            clock=lambda: self.now[0],
            thresholds=ThresholdConfig(
                time_window_hours=8, min_cases=3, min_institutions=2, activity_tag="河边活动"
            ),
        )
        self.duty = Viewer("duty-1", Role.DUTY_OFFICER)
        self.verifier = Viewer("cdc-verifier", Role.VERIFIER)
        self.publisher = Viewer("publisher-1", Role.PUBLISHER)
        # 既有地点与暴露记录导入，并补登责任区
        imported = self.service.import_seed(SEED)
        self.assertEqual(imported, {"places": 1, "exposures": 1})
        self.service.store.places["location-riverside-park"]["region"] = "滨河街道"
        # 公开入口
        for eid, label in [("ent-board", "社区公告栏"), ("ent-school", "学校通知群"), ("ent-wechat", "公众号")]:
            self.service.publications.register_entrance(eid, "滨河街道", label)

    def _night_reports(self) -> None:
        # 学校夜间集中上报（含一名儿童，与医院重复）
        self.now[0] = dt(22, 30)
        self.service.submit_batch(
            "school-a",
            "batch-school-0919",
            [
                report_payload(
                    "rpt-sch-1", "school-a", "school", "tok-child-1", dt(21, 0),
                    is_child=True, age_band="6-12",
                    statements=[statement("st-sch-1", "self_report", "学生说晚自习后胳膊起红印")],
                ),
                report_payload(
                    "rpt-sch-2", "school-a", "school", "tok-stu-2", dt(21, 20),
                    statements=[statement("st-sch-2", "self_report", "群里说牙膏可以止痒", ("社交媒体",))],
                ),
            ],
            "teacher-1",
            submitted_at=dt(22, 30),
        )
        # 医院门诊上报（含同一儿童的就诊记录 → 跨机构去重）
        self.now[0] = dt(23, 0)
        self.service.submit_report(
            report_payload(
                "rpt-hosp-1", "hospital-a", "hospital", "tok-child-1", dt(21, 0),
                is_child=True, age_band="6-12", care="综合医院",
                statements=[
                    statement("st-hosp-1", "medical_observation", "前臂条索状红斑伴水疱", ("门诊检查",)),
                    statement("st-hosp-2", "self_report", "家长提供的线上照片", ("线上照片",)),
                ],
            ),
            "nurse-1",
        )
        # 社区上报
        self.service.submit_report(
            report_payload(
                "rpt-com-1", "community-a", "community", "tok-resident-1", dt(21, 40),
                statements=[statement("st-com-1", "self_report", "河边散步后皮肤灼痛", ("电话",))],
            ),
            "worker-1",
        )

    def test_full_flow(self) -> None:
        svc = self.service
        self._night_reports()

        # 去重：儿童个案合并学校与医院两个来源
        child_case = next(c for c in svc.store.cases.values() if c.subject_token == "tok-child-1")
        self.assertEqual(sorted(child_case.report_ids), ["rpt-hosp-1", "rpt-sch-1"])
        self.assertEqual(sorted(child_case.institution_ids), ["hospital-a", "school-a"])

        # 分层核实：医护观察可核实；线上照片不行；牙膏说法登记为待更正线索
        svc.verify_statement("rpt-hosp-1", "st-hosp-1", self.verifier, "门诊记录复核一致")
        with self.assertRaises(Exception):
            svc.verify_statement("rpt-hosp-1", "st-hosp-2", self.verifier, "仅凭照片")
        self.assertEqual(len(svc.store.misinfo_leads), 1)

        # 阈值触发：同一夜、同一地点、共同活动 → 待研判事件（非诊断）
        self.now[0] = dt(23, 30)
        events = svc.run_detection()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.status.value, "pending_review")
        self.assertIn("疑似", event.hypothesis)
        self.assertNotIn("确诊", event.hypothesis)
        # 去重后的 3 个个案（儿童合并计一次），3 家机构
        self.assertEqual(len(event.case_ids), 3)
        self.assertEqual(len(event.institution_ids), 3)

        # 生成对应区域的科学处置材料并发布
        material = svc.draft_regional_material(event.id, self.publisher)
        svc.publish(material.id, self.publisher)
        status = svc.publications.dissemination_status(material.series_id)
        self.assertTrue(status["all_entrances_updated"])

        # 值班员打开预警：构成、待核实事实、儿童脱敏
        view = svc.open_alert(event.id, self.duty)
        self.assertEqual(view["event"]["case_count"], 3)
        self.assertEqual(len(view["constituent_cases"]), 3)
        child_block = next(c for c in view["constituent_cases"] if c["is_child"])
        self.assertEqual(child_block["source_count"], 2)  # 两个独立来源
        pending_ids = {f["statement_id"] for f in view["pending_facts"]}
        self.assertIn("st-hosp-2", pending_ids)   # 线上照片仍待核实
        self.assertIn("st-sch-2", pending_ids)    # 牙膏说法仍待核实（且不可核实）
        self.assertIn("st-sch-1", pending_ids)
        verified_ids = {f["statement_id"] for f in view["verified_facts"]}
        self.assertEqual(verified_ids, {"st-hosp-1"})

        # 错误科普更正：牙膏止痒 → 所有公开入口更新
        (lead,) = svc.store.misinfo_leads.values()
        correction = svc.correct_misinformation(
            lead.id,
            title="澄清：牙膏止痒不科学",
            content="澄清：网传牙膏止痒缺乏依据，请勿使用；出现皮损请及时前往医疗机构就诊。",
            viewer=self.publisher,
        )
        corr_status = svc.publications.dissemination_status(correction.series_id)
        self.assertTrue(corr_status["all_entrances_updated"])
        self.assertEqual(len(corr_status["entrances"]), 3)

        # 值班员确认：更正后所有公开入口均已更新
        view2 = svc.open_alert(event.id, self.duty)
        self.assertTrue(view2["all_entrances_updated"])
        corrected = [c for c in view2["misinfo_corrections"] if c["corrected_by"]]
        self.assertEqual(len(corrected), 1)

        # 证据不足 → 降级；旧提醒留痕
        downgraded = svc.downgrade(material.series_id, "暴露因素未证实", self.publisher)
        self.assertEqual(downgraded.severity, "提示")
        notes = [d.note for d in svc.publications.deliveries_of(material.series_id)]
        self.assertTrue(any("降级" in n for n in notes))
        view3 = svc.open_alert(event.id, self.duty)
        self.assertTrue(view3["all_entrances_updated"])

        # 撤回 → 入口收到撤回通知，已送达记录仍留痕
        svc.retract(correction.series_id, "措辞需修订", self.publisher)
        corr_status2 = svc.publications.dissemination_status(correction.series_id)
        self.assertEqual(corr_status2["latest_status"], "retracted")
        self.assertTrue(corr_status2["all_entrances_updated"])
        records = svc.publications.deliveries_of(correction.series_id)
        self.assertTrue(any(d.status.value == "published" for d in records))
        self.assertTrue(any("撤回通知" in d.note for d in records))

        # 快照持久化往返不丢信息
        restored = Store.restore(svc.store.snapshot())
        self.assertEqual(len(restored.reports), len(svc.store.reports))
        self.assertEqual(len(restored.deliveries), len(svc.store.deliveries))
        self.assertEqual(len(restored.audit), len(svc.store.audit))

    def test_seed_exposure_participates_in_monitoring(self) -> None:
        """既有暴露记录进入统一格式，可参与后续检测。"""
        svc = self.service
        self.assertIn("incident-campus-001", svc.store.reports)
        self.assertIn("location-riverside-park", svc.store.places)
        # 同一夜在学校宿舍再报两例（不同机构），与既有记录共同达到阈值
        svc2 = MonitoringService(
            clock=lambda: self.now[0],
            thresholds=ThresholdConfig(time_window_hours=8, min_cases=3, min_institutions=2),
        )
        svc2.import_seed(SEED)
        svc2.submit_report(
            report_payload(
                "rpt-dorm-1", "hospital-b", "hospital", "tok-dorm-1", dt(22, 0),
                place="学校宿舍", place_id=None, region="校园街道", tags=(),
            ),
            "nurse-2",
        )
        svc2.submit_report(
            report_payload(
                "rpt-dorm-2", "community-b", "community", "tok-dorm-2", dt(22, 45),
                place="学校宿舍", place_id=None, region="校园街道", tags=(),
            ),
            "worker-2",
        )
        events = svc2.run_detection()
        dorm_events = [e for e in events if "学校宿舍" in e.location_summary]
        self.assertEqual(len(dorm_events), 1)
        self.assertEqual(len(dorm_events[0].case_ids), 3)  # 含既有记录 incident-campus-001


if __name__ == "__main__":
    unittest.main()
