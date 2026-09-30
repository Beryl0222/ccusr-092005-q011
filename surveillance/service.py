"""应用服务门面：把上报、去重、核实、检测、发布、研判视图串成业务流。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from . import serde
from .access import MASK, Viewer, can_view_child_detail, report_view
from .crypto import pseudonymize
from .dedup import DedupService
from .detection import DetectionService
from .models import (
    FOLK_REMEDY_KEYWORDS,
    ONLINE_ONLY_CHANNELS,
    EventStatus,
    InfoLayer,
    MisinfoLead,
    PendingEvent,
    Publication,
    PublicationKind,
    Report,
    Role,
    ThresholdConfig,
    Batch,
)
from .publications import PublicationError, PublicationService
from .store import Store

# 只允许脱敏身份：出现这些字段直接拒收
FORBIDDEN_IDENTITY_FIELDS = {"name", "real_name", "id_card", "phone", "student_name", "patient_name"}

DEFAULT_REGION = "未分区"


class ServiceError(ValueError):
    pass


class PermissionDenied(ServiceError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MonitoringService:
    def __init__(
        self,
        store: Store | None = None,
        thresholds: ThresholdConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        salt: str = "surveillance-dev-salt",
    ) -> None:
        self.store = store or Store()
        self.clock = clock or _utcnow
        self.salt = salt
        self.dedup = DedupService(self.store)
        self.detection = DetectionService(self.store, thresholds)
        self.publications = PublicationService(self.store)

    # ---- 脱敏 ----------------------------------------------------------

    def tokenize(self, raw_id: str) -> str:
        return pseudonymize(raw_id, self.salt)

    # ---- 上报 ----------------------------------------------------------

    def submit_report(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.clock()
        report = self._parse_report(payload)
        self._fill_region(report)
        case, created = self.dedup.ingest(report, now)
        leads = self._note_misinfo(report, now)
        self.store.log(
            now, actor, "submit_report",
            f"报告 {report.id} 归入个案 {case.id}（{'新建' if created else '合并'}）",
        )
        return {
            "report_id": report.id,
            "case_id": case.id,
            "created_case": created,
            "case_report_count": len(case.report_ids),
            "misinfo_leads": [lead.id for lead in leads],
        }

    def submit_batch(
        self,
        institution_id: str,
        batch_id: str,
        payloads: Iterable[dict[str, Any]],
        actor: str,
        submitted_at: datetime | None = None,
    ) -> dict[str, Any]:
        """夜间集中上报：整批幂等，重复提交同一批次直接返回原结果。"""
        now = submitted_at or self.clock()
        existing = self.store.batches.get(batch_id)
        if existing is not None:
            return {
                "batch_id": existing.id,
                "report_ids": list(existing.report_ids),
                "night_batch": existing.night_batch,
                "idempotent_replay": True,
            }
        report_ids: list[str] = []
        for payload in payloads:
            payload = dict(payload)
            payload["institution_id"] = institution_id
            payload["batch_id"] = batch_id
            result = self.submit_report(payload, actor)
            report_ids.append(result["report_id"])
        night = now.hour >= 18 or now.hour < 6
        batch = Batch(
            id=batch_id,
            institution_id=institution_id,
            submitted_at=now,
            report_ids=report_ids,
            night_batch=night,
        )
        self.store.batches[batch_id] = batch
        self.store.log(now, actor, "submit_batch", f"批次 {batch_id} 收到 {len(report_ids)} 条")
        return {
            "batch_id": batch_id,
            "report_ids": report_ids,
            "night_batch": night,
            "idempotent_replay": False,
        }

    # ---- 信息分层与核实 --------------------------------------------------

    def verify_statement(self, report_id: str, statement_id: str, viewer: Viewer, note: str) -> dict:
        if viewer.role != Role.VERIFIER:
            raise PermissionDenied("只有疾控核实员可以把信息核实为事实")
        report = self._report(report_id)
        statement = self._statement(report, statement_id)
        if statement.layer == InfoLayer.VERIFIED_FACT:
            raise ServiceError("该信息已是已核实事实")
        if any(word in statement.text for word in FOLK_REMEDY_KEYWORDS):
            raise ServiceError("民间偏方说法不能核实为事实，应登记为待更正线索")
        channels = set(statement.evidence_channels)
        if channels and channels <= ONLINE_ONLY_CHANNELS:
            raise ServiceError("仅有线上照片等网络证据，不能作为核实依据")
        if not channels:
            raise ServiceError("缺少证据渠道，不能核实")
        now = self.clock()
        statement.layer = InfoLayer.VERIFIED_FACT
        statement.verified_by = viewer.actor_id
        statement.verified_at = now
        statement.verification_note = note
        self.store.log(now, viewer.actor_id, "verify_statement", f"{report_id}/{statement_id} 核实：{note}")
        return {"statement_id": statement_id, "layer": statement.layer.value}

    # ---- 事件检测与研判 --------------------------------------------------

    def run_detection(self, now: datetime | None = None) -> list[PendingEvent]:
        now = now or self.clock()
        events = self.detection.run(now)
        for event in events:
            self.store.log(now, "system", "detect", f"事件 {event.id} v{event.version}：{event.hypothesis}")
        return events

    def assign_handler(self, event_id: str, handler_id: str, viewer: Viewer) -> PendingEvent:
        if viewer.role not in (Role.DUTY_OFFICER, Role.VERIFIER):
            raise PermissionDenied("只有值班员或核实员可以指派处置者")
        event = self._event(event_id)
        if handler_id not in event.handler_ids:
            event.handler_ids.append(handler_id)
            event.updated_at = self.clock()
        self.store.log(self.clock(), viewer.actor_id, "assign_handler", f"{event_id} 指派 {handler_id}")
        return event

    def set_event_status(self, event_id: str, status: EventStatus, viewer: Viewer, note: str) -> PendingEvent:
        if viewer.role not in (Role.DUTY_OFFICER, Role.VERIFIER):
            raise PermissionDenied("只有值班员或核实员可以变更事件状态")
        event = self._event(event_id)
        event.status = status
        event.version += 1
        event.updated_at = self.clock()
        event.assessment_notes.append(f"{self.clock().isoformat()} {viewer.actor_id}：{note}")
        self.store.log(self.clock(), viewer.actor_id, "event_status", f"{event_id} -> {status.value}：{note}")
        return event

    # ---- 发布 ------------------------------------------------------------

    def draft_regional_material(
        self, event_id: str, viewer: Viewer, valid_hours: float = 72.0
    ) -> Publication:
        """为待研判事件起草对应区域的科学处置材料（草稿，不自动发布）。"""
        if viewer.role not in (Role.PUBLISHER, Role.VERIFIER, Role.DUTY_OFFICER):
            raise PermissionDenied("无权起草处置材料")
        event = self._event(event_id)
        now = self.clock()
        activity = f"共同活动为{event.activity_tag}。" if event.activity_tag else ""
        content = (
            f"一、事件概况：{event.hypothesis}{activity}\n"
            "二、防护建议：傍晚至夜间减少在水边、绿化带等虫体活跃区域长时间停留；"
            "外出穿长袖衣裤，住处关好纱窗；发现虫体落在皮肤上时吹走或拨落，勿徒手拍打。\n"
            "三、皮损处理：可用清水或生理盐水轻柔冲洗，避免抓挠；"
            "症状缓解不能替代就医，如出现条索状红斑、水疱、糜烂或发热，"
            "请及时前往医疗机构就诊并告知暴露史。\n"
            "四、风险提示：网传“牙膏止痒”等偏方缺乏依据，请勿用于替代正规诊疗。\n"
            "五、本材料为阶段性风险提示，将随核实进展更新版本。"
        )
        pub = self.publications.draft(
            kind=PublicationKind.DISPOSAL_GUIDANCE,
            region=event.region,
            event_id=event.id,
            title=f"{event.region}疑似聚集性皮肤损伤处置材料（待研判事件 {event.id}）",
            content=content,
            severity="预警",
            valid_from=now,
            valid_until=now + timedelta(hours=valid_hours),
            actor=viewer.actor_id,
            now=now,
        )
        self.store.log(now, viewer.actor_id, "draft_material", f"{event_id} -> {pub.series_id} v{pub.version}")
        return pub

    def publish(self, pub_id: str, viewer: Viewer) -> Publication:
        if viewer.role != Role.PUBLISHER:
            raise PermissionDenied("只有发布员可以发布内容")
        return self.publications.publish(pub_id, viewer.actor_id, self.clock())

    def downgrade(self, series_id: str, reason: str, viewer: Viewer) -> Publication:
        if viewer.role != Role.PUBLISHER:
            raise PermissionDenied("只有发布员可以降级发布")
        return self.publications.downgrade(series_id, reason=reason, actor=viewer.actor_id, now=self.clock())

    def retract(self, series_id: str, reason: str, viewer: Viewer) -> Publication:
        if viewer.role != Role.PUBLISHER:
            raise PermissionDenied("只有发布员可以撤回发布")
        return self.publications.retract(series_id, reason=reason, actor=viewer.actor_id, now=self.clock())

    def correct_misinformation(
        self,
        lead_id: str,
        *,
        title: str,
        content: str,
        viewer: Viewer,
        valid_hours: float = 72.0,
    ) -> Publication:
        """针对待更正线索发布错误科普更正，并同步到该区域所有公开入口。"""
        if viewer.role != Role.PUBLISHER:
            raise PermissionDenied("只有发布员可以发布更正")
        lead = self.store.misinfo_leads.get(lead_id)
        if lead is None:
            raise ServiceError(f"线索 {lead_id} 不存在")
        now = self.clock()
        pub = self.publications.draft(
            kind=PublicationKind.MISINFO_CORRECTION,
            region=lead.region,
            title=title,
            content=content,
            severity="提示",
            valid_from=now,
            valid_until=now + timedelta(hours=valid_hours),
            actor=viewer.actor_id,
            now=now,
        )
        published = self.publications.publish(pub.id, viewer.actor_id, now)
        lead.corrected_by = published.series_id
        self.store.log(now, viewer.actor_id, "correct_misinfo", f"线索 {lead_id} 由 {published.series_id} 更正")
        return published

    # ---- 值班员预警视图 --------------------------------------------------

    def open_alert(self, event_id: str, viewer: Viewer) -> dict[str, Any]:
        """值班员打开一次预警：看到独立报告构成、待核实事实、入口更新状态。"""
        event = self._event(event_id)
        now = self.clock()
        constituent: list[dict[str, Any]] = []
        pending_facts: list[dict[str, Any]] = []
        verified_facts: list[dict[str, Any]] = []
        child_detail_accessed = False
        for case_id in event.case_ids:
            case = self.store.cases[case_id]
            reports = [self.store.reports[rid] for rid in case.report_ids if rid in self.store.reports]
            constituent.append(
                {
                    "case_id": case.id,
                    "is_child": case.is_child,
                    "source_count": len(reports),
                    "institutions": sorted({r.institution_id for r in reports}),
                    "institution_kinds": sorted({r.institution_kind.value for r in reports}),
                    "reports": [report_view(r, viewer, event) for r in reports],
                }
            )
            for report in reports:
                show_text = not report.is_child or can_view_child_detail(viewer, event)
                if report.is_child and show_text:
                    child_detail_accessed = True
                for statement in report.statements:
                    item = {
                        "report_id": report.id,
                        "statement_id": statement.id,
                        "layer": statement.layer.value,
                        "text": statement.text if show_text else MASK,
                    }
                    if statement.layer == InfoLayer.VERIFIED_FACT:
                        verified_facts.append(item)
                    else:
                        pending_facts.append(item)
        if child_detail_accessed:
            self.store.log(now, viewer.actor_id, "view_child_detail", f"事件 {event_id} 儿童明细")

        series_ids = sorted(
            {p.series_id for p in self.store.publications.values() if p.region == event.region}
        )
        disseminations = [
            self.publications.dissemination_status(sid)
            for sid in series_ids
            if self.publications.deliveries_of(sid)
        ]
        all_updated = all(d["all_entrances_updated"] for d in disseminations) if disseminations else True
        corrections = [
            {
                "lead_id": lead.id,
                "claim": lead.claim,
                "corrected_by": lead.corrected_by,
            }
            for lead in self.store.misinfo_leads.values()
            if lead.region == event.region
        ]
        self.store.log(now, viewer.actor_id, "open_alert", f"打开预警 {event_id}")
        return {
            "event": {
                "id": event.id,
                "status": event.status.value,
                "region": event.region,
                "location_summary": event.location_summary,
                "hypothesis": event.hypothesis,
                "activity_tag": event.activity_tag,
                "window_start": event.window_start.isoformat(),
                "window_end": event.window_end.isoformat(),
                "case_count": len(event.case_ids),
                "institution_count": len(event.institution_ids),
                "version": event.version,
            },
            "constituent_cases": constituent,
            "pending_facts": pending_facts,
            "verified_facts": verified_facts,
            "disseminations": disseminations,
            "all_entrances_updated": all_updated,
            "misinfo_corrections": corrections,
        }

    # ---- 种子数据 --------------------------------------------------------

    def import_seed(self, data: dict[str, Any], actor: str = "seed-import") -> dict[str, int]:
        """导入既有地点与暴露记录，使其参与夜间集中上报与跨机构去重。"""
        places = 0
        exposures = 0
        for record in data.get("records", []):
            kind = record.get("kind")
            if kind == "risk_site":
                self.store.places[record["id"]] = {
                    "name": record.get("place", record["id"]),
                    "features": list(record.get("features", [])),
                    "season": record.get("season", ""),
                    "region": record.get("region", DEFAULT_REGION),
                }
                places += 1
            elif kind == "exposure":
                occurred = datetime.fromisoformat(record["reported_at"])
                payload = {
                    "id": record["id"],
                    "institution_id": record.get("institution_id", "legacy-import"),
                    "institution_kind": record.get("institution_kind", "other"),
                    "subject_token": self.tokenize(f"legacy:{record['id']}"),
                    "reporter_token": self.tokenize(f"reporter:{record['id']}"),
                    "occurred_at": record["reported_at"],
                    "reported_at": record["reported_at"],
                    "location": {"place_name": record["place"]},
                    "contact_method": record.get("contact", "不明"),
                    "skin_area": record.get("skin_area", "未记录"),
                    "lesion_extent": record.get("lesion_extent", "未记录"),
                    "care_pathway": record.get("care_pathway", "未记录"),
                }
                self.submit_report(payload, actor)
                exposures += 1
        return {"places": places, "exposures": exposures}

    # ---- 内部 ------------------------------------------------------------

    def _parse_report(self, payload: dict[str, Any]) -> Report:
        forbidden = FORBIDDEN_IDENTITY_FIELDS & set(payload)
        if forbidden:
            raise ServiceError(f"只允许脱敏身份，禁止字段：{sorted(forbidden)}")
        try:
            report = serde.decode(Report, payload)
        except Exception as exc:
            raise ServiceError(f"报告格式不合法：{exc}") from exc
        if not report.subject_token:
            raise ServiceError("缺少脱敏身份令牌 subject_token")
        return report

    def _fill_region(self, report: Report) -> None:
        loc = report.location
        if loc.region:
            return
        if loc.place_id and loc.place_id in self.store.places:
            loc.region = self.store.places[loc.place_id].get("region", DEFAULT_REGION)
        else:
            loc.region = DEFAULT_REGION

    def _note_misinfo(self, report: Report, now: datetime) -> list[MisinfoLead]:
        leads: list[MisinfoLead] = []
        for statement in report.statements:
            if any(word in statement.text for word in FOLK_REMEDY_KEYWORDS):
                lead = MisinfoLead(
                    id=self.store.next_id("lead"),
                    claim=statement.text,
                    region=report.location.region or DEFAULT_REGION,
                    report_id=report.id,
                    noted_at=now,
                )
                self.store.misinfo_leads[lead.id] = lead
                leads.append(lead)
        return leads

    def _report(self, report_id: str) -> Report:
        report = self.store.reports.get(report_id)
        if report is None:
            raise ServiceError(f"报告 {report_id} 不存在")
        return report

    def _event(self, event_id: str) -> PendingEvent:
        event = self.store.events.get(event_id)
        if event is None:
            raise ServiceError(f"事件 {event_id} 不存在")
        return event

    @staticmethod
    def _statement(report: Report, statement_id: str):
        for statement in report.statements:
            if statement.id == statement_id:
                return statement
        raise ServiceError(f"报告 {report.id} 中不存在信息 {statement_id}")


__all__ = [
    "MonitoringService",
    "ServiceError",
    "PermissionDenied",
    "PublicationError",
]
