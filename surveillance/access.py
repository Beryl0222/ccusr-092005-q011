"""访问控制与脱敏视图。

核心规则：儿童个案的明细只向"实际处置者"开放。
其余角色（含值班员）只能看到结构化的汇总与分层计数，看不到具体内容。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import PendingEvent, Report, Role

MASK = "（儿童个案，仅实际处置者可见）"


@dataclass(frozen=True)
class Viewer:
    actor_id: str
    role: Role


def can_view_child_detail(viewer: Viewer, event: PendingEvent | None) -> bool:
    """只有被指派到对应事件的实际处置者才能查看儿童明细。"""
    if viewer.role != Role.HANDLER:
        return False
    if event is None:
        return False
    return viewer.actor_id in event.handler_ids


def report_view(report: Report, viewer: Viewer, event: PendingEvent | None) -> dict:
    """按角色生成报告视图；儿童个案对非处置者屏蔽明细。"""
    base = {
        "id": report.id,
        "institution_id": report.institution_id,
        "institution_kind": report.institution_kind.value,
        "is_child": report.is_child,
        "occurred_at": report.occurred_at.isoformat(),
        "reported_at": report.reported_at.isoformat(),
        "batch_id": report.batch_id,
    }
    if report.is_child and not can_view_child_detail(viewer, event):
        base.update(
            {
                "masked": True,
                "location": {"place_name": report.location.place_name, "region": report.location.region},
                "contact_method": MASK,
                "skin_area": MASK,
                "lesion_extent": MASK,
                "care_pathway": MASK,
                "age_band": MASK,
                "activity_tags": list(report.activity_tags),
                "statements": [
                    {
                        "id": s.id,
                        "layer": s.layer.value,
                        "verified": s.verified_at is not None,
                        "text": MASK,
                    }
                    for s in report.statements
                ],
            }
        )
        return base
    base.update(
        {
            "masked": False,
            "location": {
                "place_name": report.location.place_name,
                "place_id": report.location.place_id,
                "region": report.location.region,
                "detail": report.location.detail,
            },
            "contact_method": report.contact_method,
            "skin_area": report.skin_area,
            "lesion_extent": report.lesion_extent,
            "care_pathway": report.care_pathway,
            "age_band": report.age_band,
            "activity_tags": list(report.activity_tags),
            "source_channel": report.source_channel,
            "statements": [
                {
                    "id": s.id,
                    "layer": s.layer.value,
                    "text": s.text,
                    "evidence_channels": list(s.evidence_channels),
                    "attachments": list(s.attachments),
                    "verified": s.verified_at is not None,
                    "verified_by": s.verified_by,
                    "verification_note": s.verification_note,
                }
                for s in report.statements
            ],
        }
    )
    return base
