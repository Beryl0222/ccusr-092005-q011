"""发布生命周期：版本、有效期、降级、撤回、送达留痕、入口更新确认。"""

import unittest
from datetime import timedelta

from surveillance import (
    MonitoringService,
    PublicationError,
    PublicationKind,
    Role,
    Viewer,
    lint_content,
)

from factories import NIGHT, dt


def guidance_content() -> str:
    return (
        "滨河街道出现疑似聚集性皮肤损伤，暴露因素待核实。\n"
        "注意防虫，症状缓解不能替代就医，请及时前往医疗机构就诊。"
    )


class LintTest(unittest.TestCase):
    def test_medical_advice_required(self) -> None:
        errors = lint_content(
            PublicationKind.PUBLIC_NOTICE, "提示", "涂点牙膏止痒即可，不用去医院。",
            NIGHT, NIGHT + timedelta(hours=24),
        )
        self.assertTrue(any("就医" in e for e in errors))

    def test_relief_cannot_replace_medical_advice(self) -> None:
        """只讲症状缓解、不讲就医 → 拒绝。"""
        errors = lint_content(
            PublicationKind.PUBLIC_NOTICE, "提示", "冷敷可缓解瘙痒，注意别抓挠。",
            NIGHT, NIGHT + timedelta(hours=24),
        )
        self.assertTrue(any("不能用症状缓解替代就医" in e for e in errors))

    def test_diagnosis_wording_rejected(self) -> None:
        errors = lint_content(
            PublicationKind.PUBLIC_NOTICE, "通报", "已确诊为隐翅虫皮炎，请及时就医。",
            NIGHT, NIGHT + timedelta(hours=24),
        )
        self.assertTrue(any("诊断" in e for e in errors))

    def test_folk_remedy_as_advice_rejected(self) -> None:
        errors = lint_content(
            PublicationKind.DISPOSAL_GUIDANCE, "处置", "推荐用牙膏涂抹止痒，并及时就医。",
            NIGHT, NIGHT + timedelta(hours=24),
        )
        self.assertTrue(any("偏方" in e for e in errors))

    def test_folk_remedy_debunk_with_caution_allowed(self) -> None:
        errors = lint_content(
            PublicationKind.DISPOSAL_GUIDANCE, "处置",
            "网传牙膏止痒缺乏依据，请勿使用；如症状加重请及时就医。",
            NIGHT, NIGHT + timedelta(hours=24),
        )
        self.assertEqual(errors, [])

    def test_validity_required(self) -> None:
        errors = lint_content(
            PublicationKind.PUBLIC_NOTICE, "提示", "请及时就医。",
            NIGHT, NIGHT - timedelta(hours=1),
        )
        self.assertTrue(any("有效期" in e for e in errors))

    def test_correction_needs_marker(self) -> None:
        errors = lint_content(
            PublicationKind.MISINFO_CORRECTION, "牙膏与止痒", "牙膏不能治病，请及时就医。",
            NIGHT, NIGHT + timedelta(hours=24),
        )
        self.assertTrue(any("澄清" in e or "纠正" in e for e in errors))


class PublicationLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [NIGHT]
        self.service = MonitoringService(clock=lambda: self.now[0])
        self.publisher = Viewer("publisher-1", Role.PUBLISHER)
        self.pubs = self.service.publications
        for eid, label in [("ent-board", "社区公告栏"), ("ent-school", "学校通知群"), ("ent-wechat", "公众号")]:
            self.pubs.register_entrance(eid, "滨河街道", label)

    def _publish_guidance(self) -> str:
        pub = self.pubs.draft(
            kind=PublicationKind.DISPOSAL_GUIDANCE,
            region="滨河街道",
            title="滨河街道风险提示",
            content=guidance_content(),
            severity="预警",
            valid_from=self.now[0],
            valid_until=self.now[0] + timedelta(hours=48),
            actor="publisher-1",
            now=self.now[0],
        )
        return self.pubs.publish(pub.id, "publisher-1", self.now[0]).series_id

    def test_publish_pushes_to_all_region_entrances(self) -> None:
        series_id = self._publish_guidance()
        status = self.pubs.dissemination_status(series_id)
        self.assertTrue(status["all_entrances_updated"])
        self.assertEqual(len(status["entrances"]), 3)
        self.assertEqual(len(self.pubs.deliveries_of(series_id)), 3)

    def test_new_version_supersedes_and_keeps_trail(self) -> None:
        series_id = self._publish_guidance()
        v2 = self.pubs.draft(
            kind=PublicationKind.DISPOSAL_GUIDANCE, region="滨河街道",
            title="滨河街道风险提示", content=guidance_content() + "（更新）",
            severity="预警", valid_from=self.now[0], valid_until=self.now[0] + timedelta(hours=72),
            actor="publisher-1", now=self.now[0], series_id=series_id,
        )
        self.pubs.publish(v2.id, "publisher-1", self.now[0])
        versions = [p for p in self.service.store.publications.values() if p.series_id == series_id]
        by_version = {p.version: p.status.value for p in versions}
        self.assertEqual(by_version, {1: "superseded", 2: "published"})
        # 旧版本送达记录仍留痕
        notes = [d.note for d in self.pubs.deliveries_of(series_id)]
        self.assertTrue(any("取代" in n for n in notes))
        self.assertTrue(self.pubs.dissemination_status(series_id)["all_entrances_updated"])

    def test_downgrade_when_evidence_insufficient(self) -> None:
        series_id = self._publish_guidance()
        downgraded = self.pubs.downgrade(
            series_id, reason="样本量不足，暴露因素未证实", actor="publisher-1", now=self.now[0]
        )
        self.assertEqual(downgraded.severity, "提示")
        self.assertEqual(downgraded.version, 2)
        versions = {p.version: p.status.value for p in self.service.store.publications.values()}
        self.assertEqual(versions[1], "downgraded")
        self.assertEqual(versions[2], "published")
        # 入口已更新到降级版本，旧提醒留痕
        status = self.pubs.dissemination_status(series_id)
        self.assertTrue(status["all_entrances_updated"])
        notes = [d.note for d in self.pubs.deliveries_of(series_id)]
        self.assertTrue(any("降级" in n for n in notes))

    def test_retract_keeps_delivered_alerts_traceable(self) -> None:
        series_id = self._publish_guidance()
        self.pubs.retract(series_id, reason="区域核实排除聚集", actor="publisher-1", now=self.now[0])
        status = self.pubs.dissemination_status(series_id)
        self.assertEqual(status["latest_status"], "retracted")
        self.assertTrue(status["all_entrances_updated"])
        for item in status["entrances"]:
            self.assertEqual(item["serving_status"], "retracted")
        # 已送达的旧提醒与撤回通知都在台账中
        records = self.pubs.deliveries_of(series_id)
        self.assertTrue(any(d.status.value == "published" for d in records))
        self.assertTrue(any("撤回通知" in d.note for d in records))

    def test_expire_after_validity(self) -> None:
        series_id = self._publish_guidance()
        self.now[0] = self.now[0] + timedelta(hours=49)
        expired = self.pubs.expire_sweep(self.now[0])
        self.assertEqual(len(expired), 1)
        status = self.pubs.dissemination_status(series_id)
        self.assertEqual(status["latest_status"], "expired")
        self.assertTrue(status["all_entrances_updated"])

    def test_publish_invalid_content_rejected(self) -> None:
        pub = self.pubs.draft(
            kind=PublicationKind.PUBLIC_NOTICE, region="滨河街道",
            title="偏方推荐", content="涂牙膏止痒即可。",
            severity="提示", valid_from=self.now[0], valid_until=self.now[0] + timedelta(hours=24),
            actor="publisher-1", now=self.now[0],
        )
        with self.assertRaises(PublicationError):
            self.pubs.publish(pub.id, "publisher-1", self.now[0])

    def test_publish_requires_publisher_role(self) -> None:
        from surveillance.service import PermissionDenied

        pub = self.pubs.draft(
            kind=PublicationKind.PUBLIC_NOTICE, region="滨河街道",
            title="提示", content=guidance_content(),
            severity="提示", valid_from=self.now[0], valid_until=self.now[0] + timedelta(hours=24),
            actor="x", now=self.now[0],
        )
        with self.assertRaises(PermissionDenied):
            self.service.publish(pub.id, Viewer("nurse", Role.REPORTER))


if __name__ == "__main__":
    unittest.main()
