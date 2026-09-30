"""阈值事件检测：空间、时间、共同活动达到配置阈值时生成待研判事件。

- 输入是去重后的个案（同一人的重复上报不会重复计数）。
- 事件只表达"疑似、待核实"的研判假设，系统不作医学诊断。
- 重复运行幂等：同一聚集窗口的活跃事件会被更新而非重复创建。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import Case, EventStatus, PendingEvent, Report, ThresholdConfig
from .store import Store

_ACTIVE_STATUSES = (EventStatus.PENDING_REVIEW, EventStatus.UNDER_RESPONSE)
_M_PER_DEG_LAT = 111_320.0


class DetectionService:
    def __init__(self, store: Store, config: ThresholdConfig | None = None) -> None:
        self.store = store
        self.config = config or ThresholdConfig()

    # ---- 个案画像 ------------------------------------------------------

    def _case_reports(self, case: Case) -> list[Report]:
        return [self.store.reports[rid] for rid in case.report_ids if rid in self.store.reports]

    def _site_key(self, report: Report) -> str:
        loc = report.location
        if loc.place_id:
            return f"place:{loc.place_id}"
        if loc.lat is not None and loc.lon is not None:
            size = max(self.config.spatial_radius_m, 1.0) / _M_PER_DEG_LAT
            return f"grid:{int(loc.lat // size)}:{int(loc.lon // size)}"
        return f"name:{loc.place_name}"

    def _case_profile(self, case: Case) -> dict:
        reports = self._case_reports(case)
        latest = max(reports, key=lambda r: r.occurred_at)
        tags: set[str] = set()
        for r in reports:
            tags.update(r.activity_tags)
        return {
            "case": case,
            "site_key": self._site_key(latest),
            "location": latest.location,
            "activity_tags": tags,
            "institutions": set(case.institution_ids),
            "occurred_at": case.last_occurred_at,
        }

    # ---- 分组与窗口 ----------------------------------------------------

    def _group_cases(self) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        for case in self.store.cases.values():
            profile = self._case_profile(case)
            if not profile["activity_tags"] and self.config.activity_tag:
                continue
            if self.config.activity_tag and self.config.activity_tag not in profile["activity_tags"]:
                continue
            tag = self.config.activity_tag or "-"
            key = f"{profile['site_key']}|{tag}"
            groups.setdefault(key, []).append(profile)
        return groups

    def _windows(self, profiles: list[dict]) -> list[list[dict]]:
        """按发生时间排序后切出互不重叠的时间窗。"""
        ordered = sorted(profiles, key=lambda p: p["occurred_at"])
        span = timedelta(hours=self.config.time_window_hours)
        windows: list[list[dict]] = []
        current: list[dict] = []
        anchor: datetime | None = None
        for profile in ordered:
            if anchor is None or profile["occurred_at"] - anchor <= span:
                if anchor is None:
                    anchor = profile["occurred_at"]
                current.append(profile)
            else:
                windows.append(current)
                current = [profile]
                anchor = profile["occurred_at"]
        if current:
            windows.append(current)
        return windows

    # ---- 事件生成 ------------------------------------------------------

    def run(self, now: datetime) -> list[PendingEvent]:
        """执行一次检测，返回本次新建或更新的事件。"""
        changed: list[PendingEvent] = []
        for cluster_key, profiles in sorted(self._group_cases().items()):
            for window in self._windows(profiles):
                institutions = set().union(*(p["institutions"] for p in window))
                if len(window) < self.config.min_cases:
                    continue
                if len(institutions) < self.config.min_institutions:
                    continue
                event = self._find_active_event(cluster_key, window)
                if event is None:
                    event = self._create_event(cluster_key, window, institutions, now)
                    changed.append(event)
                else:
                    if self._absorb(event, window, institutions, now):
                        changed.append(event)
        return changed

    def _find_active_event(
        self, cluster_key: str, window: list[dict]
    ) -> PendingEvent | None:
        window_case_ids = {p["case"].id for p in window}
        window_start = min(p["occurred_at"] for p in window)
        window_end = max(p["occurred_at"] for p in window)
        candidates = [
            e
            for e in self.store.events.values()
            if e.cluster_key == cluster_key and e.status in _ACTIVE_STATUSES
        ]
        for event in sorted(candidates, key=lambda e: e.created_at, reverse=True):
            if window_case_ids & set(event.case_ids):
                return event
            if event.window_start <= window_end and window_start <= event.window_end:
                return event
        return None

    def _create_event(
        self, cluster_key: str, window: list[dict], institutions: set[str], now: datetime
    ) -> PendingEvent:
        location = window[-1]["location"]
        region = self._region_of(location)
        case_ids = [p["case"].id for p in window]
        activity = self.config.activity_tag
        activity_desc = f"，共同活动：{activity}" if activity else ""
        hypothesis = (
            f"{location.place_name}一带出现疑似聚集性皮肤损伤："
            f"{len(case_ids)}名个案、{len(institutions)}家机构上报{activity_desc}，"
            f"暴露因素待核实；本事件为研判线索，不构成医学诊断。"
        )
        event = PendingEvent(
            id=self.store.next_id("event"),
            cluster_key=cluster_key,
            region=region,
            case_ids=case_ids,
            institution_ids=sorted(institutions),
            activity_tag=activity,
            window_start=min(p["occurred_at"] for p in window),
            window_end=max(p["occurred_at"] for p in window),
            location_summary=location.place_name,
            hypothesis=hypothesis,
            status=EventStatus.PENDING_REVIEW,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self.store.events[event.id] = event
        return event

    def _absorb(
        self, event: PendingEvent, window: list[dict], institutions: set[str], now: datetime
    ) -> bool:
        """把新个案并入活跃事件；有变化才更新版本。"""
        new_case_ids = [p["case"].id for p in window if p["case"].id not in event.case_ids]
        new_institutions = sorted(institutions - set(event.institution_ids))
        if not new_case_ids and not new_institutions:
            return False
        event.case_ids.extend(new_case_ids)
        event.institution_ids = sorted(set(event.institution_ids) | institutions)
        event.window_start = min(event.window_start, min(p["occurred_at"] for p in window))
        event.window_end = max(event.window_end, max(p["occurred_at"] for p in window))
        event.version += 1
        event.updated_at = now
        return True

    def _region_of(self, location) -> str:
        if location.region:
            return location.region
        if location.place_id and location.place_id in self.store.places:
            return self.store.places[location.place_id].get("region", "未分区")
        return "未分区"
