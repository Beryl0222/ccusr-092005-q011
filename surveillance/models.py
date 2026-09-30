"""公共卫生事件监测后端的领域模型。

设计原则：
- 身份一律脱敏：报告只保存 pseudonymize 之后的令牌，不落原始身份。
- 信息分层：自述 / 医护观察 / 已核实事实，三者严格分开存放与展示。
- 系统不作医学诊断：事件只表达"疑似、待核实"的研判假设。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class InstitutionKind(str, Enum):
    HOSPITAL = "hospital"      # 医院
    SCHOOL = "school"          # 学校
    COMMUNITY = "community"    # 社区
    CDC = "cdc"                # 疾控
    OTHER = "other"


class InfoLayer(str, Enum):
    """信息分层：自述、医护观察、已核实事实。"""

    SELF_REPORT = "self_report"                  # 自述
    MEDICAL_OBSERVATION = "medical_observation"  # 医护观察
    VERIFIED_FACT = "verified_fact"              # 已核实事实


class Role(str, Enum):
    REPORTER = "reporter"            # 机构上报员
    DUTY_OFFICER = "duty_officer"    # 值班员
    HANDLER = "handler"              # 实际处置者
    VERIFIER = "verifier"            # 核实员（疾控）
    PUBLISHER = "publisher"          # 发布员
    ANALYST = "analyst"              # 分析员


class EventStatus(str, Enum):
    PENDING_REVIEW = "pending_review"    # 待研判
    UNDER_RESPONSE = "under_response"    # 处置中
    DOWNGRADED = "downgraded"            # 已降级（证据不足）
    CLOSED = "closed"                    # 已关闭
    RETRACTED = "retracted"              # 已撤回


class PublicationKind(str, Enum):
    DISPOSAL_GUIDANCE = "disposal_guidance"    # 区域科学处置材料
    PUBLIC_NOTICE = "public_notice"            # 公众提示
    MISINFO_CORRECTION = "misinfo_correction"  # 错误科普更正


class PublicationStatus(str, Enum):
    DRAFT = "draft"            # 草稿
    PUBLISHED = "published"    # 已发布
    DOWNGRADED = "downgraded"  # 已被降级版本取代
    SUPERSEDED = "superseded"  # 已被新版本取代
    RETRACTED = "retracted"    # 已撤回
    EXPIRED = "expired"        # 已过有效期


# 仅来自线上渠道的证据，不能单独作为核实依据
ONLINE_ONLY_CHANNELS = frozenset({"线上照片", "网络图片", "社交媒体"})

# 民间偏方关键词：命中即登记为待更正线索，且不得核实为事实
FOLK_REMEDY_KEYWORDS = ("牙膏", "偏方", "祖传", "秘方", "口水", "酱油", "风油精")


@dataclass
class Location:
    place_name: str
    place_id: str | None = None        # 已知地点（风险场所）标识
    lat: float | None = None
    lon: float | None = None
    region: str | None = None          # 行政区划/责任区，用于按区域发布
    detail: str | None = None          # 详细地址，儿童个案中属敏感信息


@dataclass
class Statement:
    """一条分层信息。线上照片、民间说法只能作为自述线索，不能当作确诊依据。"""

    id: str
    layer: InfoLayer
    text: str
    evidence_channels: list[str] = field(default_factory=list)  # 门诊检查/现场查看/电话/线上照片
    attachments: list[str] = field(default_factory=list)        # 附件引用（如照片），不直接构成诊断依据
    recorded_by: str = ""
    recorded_at: datetime | None = None
    verified_by: str | None = None
    verified_at: datetime | None = None
    verification_note: str | None = None


@dataclass
class Report:
    """单机构上报的一条暴露记录（脱敏）。"""

    id: str
    institution_id: str
    institution_kind: InstitutionKind
    subject_token: str                 # 脱敏后的当事人身份令牌
    reporter_token: str                # 脱敏后的上报人令牌
    occurred_at: datetime              # 暴露发生时间
    reported_at: datetime              # 上报时间
    location: Location
    contact_method: str                # 接触方式，如 拍打虫体/皮肤接触/不明
    skin_area: str                     # 皮损部位
    lesion_extent: str                 # 皮损范围，如 点状/条索状/大片
    care_pathway: str                  # 就医去向，如 社区医院/综合医院/未就医
    is_child: bool = False
    age_band: str | None = None        # 粗粒度年龄段，不记录具体年龄
    activity_tags: list[str] = field(default_factory=list)  # 共同活动，如 河边活动
    statements: list[Statement] = field(default_factory=list)
    source_channel: str = "现场"        # 上报渠道
    batch_id: str | None = None        # 夜间集中上报批次


@dataclass
class Case:
    """重复上报合并后的个案。report_ids 保留各自来源，永不丢弃。"""

    id: str
    subject_token: str
    report_ids: list[str]
    institution_ids: list[str]
    is_child: bool
    first_occurred_at: datetime
    last_occurred_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass
class ThresholdConfig:
    """事件生成阈值：空间、时间、共同活动均可配置。"""

    time_window_hours: float = 24.0     # 时间阈值
    spatial_radius_m: float = 500.0     # 空间阈值（未知地点按网格归并）
    min_cases: int = 3                  # 最少个案数（按去重后的个案计）
    min_institutions: int = 2           # 最少独立上报机构数（跨机构印证）
    activity_tag: str | None = None     # 若设置，则要求共同活动标签


@dataclass
class PendingEvent:
    """待研判事件：达到阈值后由系统生成，供人工研判，不是诊断结论。"""

    id: str
    cluster_key: str
    region: str
    case_ids: list[str]
    institution_ids: list[str]
    activity_tag: str | None
    window_start: datetime
    window_end: datetime
    location_summary: str
    hypothesis: str                    # 研判假设，措辞必须保留"疑似/待核实"
    status: EventStatus
    version: int
    created_at: datetime
    updated_at: datetime
    handler_ids: list[str] = field(default_factory=list)   # 实际处置者
    assessment_notes: list[str] = field(default_factory=list)


@dataclass
class Publication:
    """一份发布内容的某个版本。同一 series 下版本递增，旧版本永久留痕。"""

    id: str
    series_id: str
    version: int
    kind: PublicationKind
    region: str
    event_id: str | None
    title: str
    content: str
    severity: str                      # 预警 / 提示
    status: PublicationStatus
    valid_from: datetime
    valid_until: datetime              # 有效期
    created_by: str
    created_at: datetime
    published_at: datetime | None = None
    change_reason: str | None = None


@dataclass
class DeliveryRecord:
    """送达台账：每一次送达（含撤回通知、降级更新）都追加留痕，不删除。"""

    id: str
    series_id: str
    version: int
    entrance_id: str
    region: str
    delivered_at: datetime
    status: PublicationStatus          # 送达时内容所处状态
    note: str = ""


@dataclass
class ServedNotice:
    """入口当前展示的某一系列内容。"""

    version: int
    status: PublicationStatus
    updated_at: datetime


@dataclass
class Entrance:
    """公开入口：社区公告栏、学校通知群、公众号等。

    一个入口可同时展示多个系列（处置材料、更正公告……），
    按 series_id 分别记录展示版本，便于逐系列核对"是否已更新"。
    """

    id: str
    region: str
    label: str
    served: dict[str, ServedNotice] = field(default_factory=dict)  # series_id -> 展示内容


@dataclass
class MisinfoLead:
    """民间说法等待更正线索（如"牙膏止痒"）。"""

    id: str
    claim: str
    region: str
    report_id: str
    noted_at: datetime
    corrected_by: str | None = None    # 更正发布的 series_id


@dataclass
class Batch:
    """夜间集中上报批次，按批次号幂等。"""

    id: str
    institution_id: str
    submitted_at: datetime
    report_ids: list[str]
    night_batch: bool


@dataclass
class AuditEntry:
    id: str
    at: datetime
    actor: str
    action: str
    detail: str
