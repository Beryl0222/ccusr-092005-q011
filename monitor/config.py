"""运行配置：聚集检测阈值与发布内容守卫。

阈值可通过数据库 config 表在运行时调整（管理端），此处给出保守默认值。
系统只在阈值满足时生成"待研判事件"，从不自动诊断。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- 聚集检测默认阈值（可在 config 表覆盖） ----
DEFAULT_SPATIAL_RADIUS_M = 500.0        # 地点之间多大距离算同一区域
DEFAULT_TIME_WINDOW_HOURS = 36.0        # 暴露/上报时间窗（"同一夜"为核心场景）
DEFAULT_MIN_CASES = 3                   # 窗口内独立病例数下限
DEFAULT_MIN_SOURCES = 2                 # 独立上报机构数下限（防单一来源误报）
DEFAULT_COMMON_ACTIVITY_MIN_CASES = 2   # 共同活动成为关联依据所需病例数

# ---- 证据分层 ----
EVIDENCE_SELF_REPORT = "self_report"        # 居民自述（含线上照片转述）
EVIDENCE_CLINICIAN_OBS = "clinician_obs"   # 医护观察
EVIDENCE_VERIFIED_FACT = "verified_fact"   # 已核实事实（实验室/现场核实）
EVIDENCE_LEVELS = (EVIDENCE_SELF_REPORT, EVIDENCE_CLINICIAN_OBS, EVIDENCE_VERIFIED_FACT)

# 只有该层级及以上可以作为"已核实事实"支撑研判结论
VERIFICATION_REQUIRED_FOR_FACT = EVIDENCE_VERIFIED_FACT

# ---- 发布生命周期 ----
ADVISORY_DRAFT = "draft"
ADVISORY_ACTIVE = "active"
ADVISORY_DOWNGRADED = "downgraded"   # 证据不足，降级
ADVISORY_WITHDRAWN = "withdrawn"     # 撤回
TERMINAL_PUBLIC_STATES = (ADVISORY_DOWNGRADED, ADVISORY_WITHDRAWN)

# ---- 事件研判状态 ----
EVENT_PENDING = "pending_review"
EVENT_CONFIRMED = "confirmed"
EVENT_DISMISSED = "dismissed"

# ---- 角色 ----
ROLE_REPORTER = "reporter"        # 机构上报员（医院/学校/社区）
ROLE_DISPATCHER = "dispatcher"    # 值班员
ROLE_INVESTIGATOR = "investigator"  # 流调人员
ROLE_CLINICIAN = "clinician"      # 医护处置者
ROLE_ADMIN = "admin"

ALL_ROLES = (ROLE_REPORTER, ROLE_DISPATCHER, ROLE_INVESTIGATOR, ROLE_CLINICIAN, ROLE_ADMIN)

# 机构类型
AGENCY_HOSPITAL = "hospital"
AGENCY_SCHOOL = "school"
AGENCY_COMMUNITY = "community"
AGENCY_CDC = "cdc"

MINOR_AGE = 18  # 未满 18 岁按儿童处理


@dataclass(frozen=True)
class Thresholds:
    """一次聚集扫描使用的阈值快照。"""

    spatial_radius_m: float = DEFAULT_SPATIAL_RADIUS_M
    time_window_hours: float = DEFAULT_TIME_WINDOW_HOURS
    min_cases: int = DEFAULT_MIN_CASES
    min_sources: int = DEFAULT_MIN_SOURCES
    common_activity_min_cases: int = DEFAULT_COMMON_ACTIVITY_MIN_CASES

    def to_dict(self) -> dict:
        return {
            "spatial_radius_m": self.spatial_radius_m,
            "time_window_hours": self.time_window_hours,
            "min_cases": self.min_cases,
            "min_sources": self.min_sources,
            "common_activity_min_cases": self.common_activity_min_cases,
        }
