"""跨机构去重：同一脱敏令牌的重复上报合并为个案，来源全部保留。

- 合并键：subject_token（各机构共享脱敏盐值，同一人令牌一致）。
- 情节窗口：同一令牌的两次暴露间隔超过 episode_window_days 视为新情节，另立个案。
- 幂等：同一 report.id 重复提交直接返回已有个案，不产生副作用。
"""

from __future__ import annotations

from datetime import timedelta

from .models import Case, Report
from .store import Store


class DedupService:
    def __init__(self, store: Store, episode_window_days: float = 7.0) -> None:
        self.store = store
        self.episode_window = timedelta(days=episode_window_days)

    def ingest(self, report: Report, now) -> tuple[Case, bool]:
        """登记报告并归入个案。返回 (个案, 是否新建个案)。"""
        existing_report = self.store.reports.get(report.id)
        if existing_report is not None:
            # 幂等：重复提交同一报告，直接定位其所属个案
            for case in self.store.cases_of_subject(existing_report.subject_token):
                if report.id in case.report_ids:
                    return case, False
            raise ValueError(f"报告 {report.id} 已存在但未归属任何个案，数据不一致")

        self.store.reports[report.id] = report
        case = self._find_mergeable_case(report)
        if case is None:
            case = Case(
                id=self.store.next_id("case"),
                subject_token=report.subject_token,
                report_ids=[report.id],
                institution_ids=[report.institution_id],
                is_child=report.is_child,
                first_occurred_at=report.occurred_at,
                last_occurred_at=report.occurred_at,
                created_at=now,
                updated_at=now,
            )
            self.store.add_case(case)
            return case, True

        case.report_ids.append(report.id)
        if report.institution_id not in case.institution_ids:
            case.institution_ids.append(report.institution_id)
        case.is_child = case.is_child or report.is_child
        case.first_occurred_at = min(case.first_occurred_at, report.occurred_at)
        case.last_occurred_at = max(case.last_occurred_at, report.occurred_at)
        case.updated_at = now
        return case, False

    def _find_mergeable_case(self, report: Report) -> Case | None:
        for case in self.store.cases_of_subject(report.subject_token):
            if (
                case.first_occurred_at - self.episode_window
                <= report.occurred_at
                <= case.last_occurred_at + self.episode_window
            ):
                return case
        return None
