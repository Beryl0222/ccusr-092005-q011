"""测试共用的数据构造工具。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))

# 入秋后的同一夜：2026-09-19 傍晚到深夜
NIGHT = datetime(2026, 9, 19, 20, 0, tzinfo=TZ)


def dt(hour: int, minute: int = 0, day: int = 19) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


def statement(
    sid: str,
    layer: str,
    text: str,
    channels: tuple[str, ...] = ("门诊检查",),
) -> dict:
    return {
        "id": sid,
        "layer": layer,
        "text": text,
        "evidence_channels": list(channels),
        "recorded_by": "tester",
        "recorded_at": NIGHT.isoformat(),
    }


def report_payload(
    rid: str,
    institution_id: str,
    kind: str,
    subject_token: str,
    occurred: datetime | None = None,
    *,
    place: str = "滨河公园",
    place_id: str | None = "location-riverside-park",
    region: str | None = "滨河街道",
    is_child: bool = False,
    age_band: str | None = None,
    tags: tuple[str, ...] = ("河边活动",),
    statements: list[dict] | None = None,
    contact: str = "拍打虫体",
    skin_area: str = "前臂",
    extent: str = "条索状",
    care: str = "社区医院",
    channel: str = "现场",
) -> dict:
    occurred = occurred or NIGHT
    return {
        "id": rid,
        "institution_id": institution_id,
        "institution_kind": kind,
        "subject_token": subject_token,
        "reporter_token": f"reporter-{rid}",
        "occurred_at": occurred.isoformat(),
        "reported_at": occurred.isoformat(),
        "location": {
            "place_name": place,
            "place_id": place_id,
            "region": region,
            "detail": "滨河路 12 号",
        },
        "contact_method": contact,
        "skin_area": skin_area,
        "lesion_extent": extent,
        "care_pathway": care,
        "is_child": is_child,
        "age_band": age_band,
        "activity_tags": list(tags),
        "statements": statements or [],
        "source_channel": channel,
    }
