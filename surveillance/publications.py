"""发布管理：版本化内容、有效期、降级/撤回、送达留痕、公开入口更新。

规则要点：
- 任何发布必须含就医建议——不能用症状缓解替代就医建议。
- 不得出现"确诊"等诊断措辞；事件相关材料只能写"疑似/待核实"。
- 民间偏方（如牙膏止痒）不得作为处置建议，只能出现在"错误科普更正"中。
- 已送达的旧提醒在台账中永久留痕；撤回/降级会推送通知到各公开入口。
"""

from __future__ import annotations

import re
from datetime import datetime

from .models import (
    DeliveryRecord,
    Entrance,
    Publication,
    PublicationKind,
    PublicationStatus,
    ServedNotice,
)
from .store import Store

MEDICAL_ADVICE_KEYWORDS = ("就医", "就诊", "医疗机构", "前往医院")
DIAGNOSIS_WORDS = ("确诊", "诊断为", "鉴定为")
FOLK_REMEDY_WORDS = ("牙膏", "偏方", "祖传", "秘方", "口水涂抹", "酱油")
CORRECTION_MARKERS = ("澄清", "纠正", "更正", "谣言", "不可信", "不科学")
# 辟谣句式中的警示语：同句出现偏方词时必须带有警示，才算"澄清"而非"建议"
CAUTION_MARKERS = ("勿", "不可", "不要", "缺乏依据", "没有依据", "谣言", "澄清", "纠正", "不能替代", "不得")


class PublicationError(ValueError):
    """发布内容或状态流转不合法。"""


def _sentences(content: str) -> list[str]:
    return [s for s in re.split(r"[。！？；\n]", content) if s.strip()]


def lint_content(
    kind: PublicationKind,
    title: str,
    content: str,
    valid_from: datetime,
    valid_until: datetime,
) -> list[str]:
    """发布前校验，返回问题列表（空列表表示通过）。"""
    errors: list[str] = []
    if valid_until <= valid_from:
        errors.append("发布必须设置有效期，且有效期截止须晚于生效时间")
    if not any(word in content for word in MEDICAL_ADVICE_KEYWORDS):
        errors.append("内容必须包含就医建议，不能用症状缓解替代就医")
    if any(word in content for word in DIAGNOSIS_WORDS):
        errors.append("内容不得使用确诊等诊断措辞，系统不作医学诊断")
    if kind != PublicationKind.MISINFO_CORRECTION:
        for sentence in _sentences(content):
            if any(word in sentence for word in FOLK_REMEDY_WORDS) and not any(
                marker in sentence for marker in CAUTION_MARKERS
            ):
                errors.append("不得把民间偏方（如牙膏止痒）作为处置建议；澄清时须在同句附带警示语")
                break
    else:
        if not any(word in (title + content) for word in CORRECTION_MARKERS):
            errors.append("错误科普更正必须在标题或正文中明确标注澄清/纠正")
    return errors


class PublicationService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ---- 起草与发布 ----------------------------------------------------

    def draft(
        self,
        *,
        kind: PublicationKind,
        region: str,
        title: str,
        content: str,
        severity: str,
        valid_from: datetime,
        valid_until: datetime,
        actor: str,
        now: datetime,
        event_id: str | None = None,
        series_id: str | None = None,
    ) -> Publication:
        version = 1
        if series_id is None:
            series_id = self.store.next_id("series")
        else:
            version = self._latest_version(series_id) + 1
        pub = Publication(
            id=self.store.next_id("pub"),
            series_id=series_id,
            version=version,
            kind=kind,
            region=region,
            event_id=event_id,
            title=title,
            content=content,
            severity=severity,
            status=PublicationStatus.DRAFT,
            valid_from=valid_from,
            valid_until=valid_until,
            created_by=actor,
            created_at=now,
        )
        self.store.publications[pub.id] = pub
        return pub

    def publish(self, pub_id: str, actor: str, now: datetime) -> Publication:
        pub = self._get(pub_id)
        if pub.status != PublicationStatus.DRAFT:
            raise PublicationError(f"只有草稿可以发布，当前状态：{pub.status.value}")
        errors = lint_content(pub.kind, pub.title, pub.content, pub.valid_from, pub.valid_until)
        if errors:
            raise PublicationError("；".join(errors))
        previous = self._latest_effective(pub.series_id)
        if previous is not None:
            previous.status = PublicationStatus.SUPERSEDED
            self._record_delivery(previous, now, note=f"被 v{pub.version} 取代")
        pub.status = PublicationStatus.PUBLISHED
        pub.published_at = now
        self._push_to_entrances(pub, now, note=f"发布 v{pub.version}")
        self.store.log(now, actor, "publish", f"{pub.series_id} v{pub.version} 发布于 {pub.region}")
        return pub

    # ---- 降级与撤回 ----------------------------------------------------

    def downgrade(
        self,
        series_id: str,
        *,
        reason: str,
        actor: str,
        now: datetime,
        severity: str = "提示",
    ) -> Publication:
        """证据不足时降级：生成同系列新版本（默认降为"提示"），旧版本留痕。"""
        current = self._latest_effective(series_id)
        if current is None:
            raise PublicationError("该系列没有可降级的已发布版本")
        new_version = self.draft(
            kind=current.kind,
            region=current.region,
            event_id=current.event_id,
            title=current.title,
            content=current.content,
            severity=severity,
            valid_from=now,
            valid_until=current.valid_until,
            actor=actor,
            now=now,
            series_id=series_id,
        )
        new_version.change_reason = reason
        new_version.status = PublicationStatus.PUBLISHED
        new_version.published_at = now
        current.status = PublicationStatus.DOWNGRADED
        self._record_delivery(current, now, note=f"证据不足降级：{reason}")
        self._push_to_entrances(new_version, now, note=f"降级更新 v{new_version.version}")
        self.store.log(now, actor, "downgrade", f"{series_id} 降级为{severity}：{reason}")
        return new_version

    def retract(self, series_id: str, *, reason: str, actor: str, now: datetime) -> Publication:
        """撤回：最新版本标记撤回，各公开入口收到撤回通知，送达记录留痕。"""
        current = self._latest_effective(series_id)
        if current is None:
            raise PublicationError("该系列没有可撤回的已发布版本")
        current.status = PublicationStatus.RETRACTED
        current.change_reason = reason
        for entrance in self._region_entrances(current.region):
            if not self._ever_received(entrance.id, series_id):
                continue
            notice = entrance.served.get(series_id)
            if notice is not None:
                notice.status = PublicationStatus.RETRACTED
                notice.updated_at = now
            self._append_delivery(
                series_id, current.version, entrance, now,
                PublicationStatus.RETRACTED, note=f"撤回通知：{reason}",
            )
        self.store.log(now, actor, "retract", f"{series_id} 撤回：{reason}")
        return current

    def expire_sweep(self, now: datetime) -> list[Publication]:
        """过有效期的发布标记失效，并同步各入口。"""
        expired: list[Publication] = []
        for pub in self.store.publications.values():
            if pub.status == PublicationStatus.PUBLISHED and pub.valid_until <= now:
                pub.status = PublicationStatus.EXPIRED
                expired.append(pub)
                for entrance in self._region_entrances(pub.region):
                    notice = entrance.served.get(pub.series_id)
                    if notice is not None and notice.version == pub.version:
                        notice.status = PublicationStatus.EXPIRED
                        notice.updated_at = now
                        self._append_delivery(
                            pub.series_id, pub.version, entrance, now,
                            PublicationStatus.EXPIRED, note="有效期届满",
                        )
        return expired

    # ---- 入口与传播状态 ------------------------------------------------

    def register_entrance(self, entrance_id: str, region: str, label: str) -> Entrance:
        entrance = self.store.entrances.get(entrance_id)
        if entrance is None:
            entrance = Entrance(id=entrance_id, region=region, label=label)
            self.store.entrances[entrance_id] = entrance
        return entrance

    def dissemination_status(self, series_id: str) -> dict:
        """某一系列在各公开入口的传播状态：是否全部入口均已更新到最新有效版本。"""
        versions = self._series_versions(series_id)
        if not versions:
            raise PublicationError(f"系列 {series_id} 不存在")
        effective = [p for p in versions if p.status != PublicationStatus.DRAFT]
        if not effective:
            return {
                "series_id": series_id,
                "latest_version": None,
                "latest_status": None,
                "all_entrances_updated": True,
                "entrances": [],
                "note": "系列尚未对外发布",
            }
        latest = effective[-1]
        entrances = [
            e for e in self._region_entrances(latest.region)
            if self._ever_received(e.id, series_id)
        ]
        items = []
        all_updated = True
        for entrance in entrances:
            notice = entrance.served.get(series_id)
            up_to_date = (
                notice is not None
                and notice.version == latest.version
                and notice.status == latest.status
            )
            all_updated = all_updated and up_to_date
            items.append(
                {
                    "entrance_id": entrance.id,
                    "label": entrance.label,
                    "serving_version": notice.version if notice else None,
                    "serving_status": notice.status.value if notice else None,
                    "up_to_date": up_to_date,
                }
            )
        return {
            "series_id": series_id,
            "latest_version": latest.version,
            "latest_status": latest.status.value,
            "all_entrances_updated": all_updated if entrances else False,
            "entrances": items,
        }

    def deliveries_of(self, series_id: str) -> list[DeliveryRecord]:
        return [d for d in self.store.deliveries if d.series_id == series_id]

    # ---- 内部 ----------------------------------------------------------

    def _get(self, pub_id: str) -> Publication:
        pub = self.store.publications.get(pub_id)
        if pub is None:
            raise PublicationError(f"发布 {pub_id} 不存在")
        return pub

    def _series_versions(self, series_id: str) -> list[Publication]:
        versions = [p for p in self.store.publications.values() if p.series_id == series_id]
        return sorted(versions, key=lambda p: p.version)

    def _latest_version(self, series_id: str) -> int:
        versions = self._series_versions(series_id)
        return versions[-1].version if versions else 0

    def _latest_effective(self, series_id: str) -> Publication | None:
        for pub in reversed(self._series_versions(series_id)):
            if pub.status == PublicationStatus.PUBLISHED:
                return pub
        return None

    def _region_entrances(self, region: str) -> list[Entrance]:
        return [e for e in self.store.entrances.values() if e.region == region]

    def _ever_received(self, entrance_id: str, series_id: str) -> bool:
        return any(
            d.entrance_id == entrance_id and d.series_id == series_id
            for d in self.store.deliveries
        )

    def _push_to_entrances(self, pub: Publication, now: datetime, note: str) -> None:
        for entrance in self._region_entrances(pub.region):
            entrance.served[pub.series_id] = ServedNotice(
                version=pub.version, status=pub.status, updated_at=now
            )
            self._append_delivery(pub.series_id, pub.version, entrance, now, pub.status, note)

    def _record_delivery(self, pub: Publication, now: datetime, note: str) -> None:
        """旧版本状态变化时，向曾送达的入口补一条留痕（不改变入口当前内容）。"""
        for entrance in self._region_entrances(pub.region):
            if self._ever_received(entrance.id, pub.series_id):
                self._append_delivery(
                    pub.series_id, pub.version, entrance, now, pub.status, note
                )

    def _append_delivery(
        self,
        series_id: str,
        version: int,
        entrance: Entrance,
        now: datetime,
        status: PublicationStatus,
        note: str,
    ) -> None:
        self.store.deliveries.append(
            DeliveryRecord(
                id=self.store.next_id("dlv"),
                series_id=series_id,
                version=version,
                entrance_id=entrance.id,
                region=entrance.region,
                delivered_at=now,
                status=status,
                note=note,
            )
        )
