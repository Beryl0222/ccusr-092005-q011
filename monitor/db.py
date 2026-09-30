"""SQLite 存储层：建表、连接与基础种子数据。

设计要点：
- 报告（reports）一经写入不可删除，病例（cases）可跨机构合并，
  合并后每条报告仍保留各自来源机构与原始证据层级。
- 发布版本（advisory_versions）与送达记录（deliveries）只增不改，
  撤回/降级通过新版本状态实现，旧提醒留痕不被覆盖。
- 儿童身份信息访问经 case_handlers 授权并写 audit_log。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 机构与账号 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS agencies (
    id          TEXT PRIMARY KEY,
    agency_type TEXT NOT NULL,            -- hospital/school/community/cdc
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    agency_id  TEXT REFERENCES agencies(id),
    role       TEXT NOT NULL,
    name       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

-- 地点 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS locations (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL UNIQUE,      -- 归一化名，跨机构复用
    lat        REAL,
    lng        REAL,
    place_kind TEXT,                      -- 河边/宿舍/绿化带……
    created_at TEXT NOT NULL
);

-- 脱敏病例（跨机构去重后的"人"）---------------------------------------
CREATE TABLE IF NOT EXISTS cases (
    id              TEXT PRIMARY KEY,
    pseudonym       TEXT NOT NULL UNIQUE, -- 脱敏化名/编号（面向处置者展示）
    identity_hash   TEXT,                 -- 脱敏身份令牌哈希（跨机构匹配用）
    age             INTEGER,
    is_minor        INTEGER NOT NULL DEFAULT 0,
    gender          TEXT,
    merged_into_id  TEXT REFERENCES cases(id),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_identity ON cases(identity_hash)
    WHERE identity_hash IS NOT NULL AND merged_into_id IS NULL;

-- 实际处置者名单：儿童身份信息只对名单内人员开放 ----------------------
CREATE TABLE IF NOT EXISTS case_handlers (
    case_id    TEXT NOT NULL REFERENCES cases(id),
    user_id    TEXT NOT NULL REFERENCES users(id),
    granted_at TEXT NOT NULL,
    granted_by TEXT REFERENCES users(id),
    PRIMARY KEY (case_id, user_id)
);

-- 夜间集中上报批次 ----------------------------------------------------
CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    agency_id   TEXT NOT NULL REFERENCES agencies(id),
    submitted_at TEXT NOT NULL,
    note        TEXT,
    report_count INTEGER NOT NULL DEFAULT 0
);

-- 暴露报告：永不物理删除，合并只改病例归属 ----------------------------
CREATE TABLE IF NOT EXISTS reports (
    id               TEXT PRIMARY KEY,
    case_id          TEXT NOT NULL REFERENCES cases(id),
    agency_id        TEXT NOT NULL REFERENCES agencies(id),
    source_ref       TEXT,                -- 机构内部单号/线上线索编号
    batch_id         TEXT REFERENCES batches(id),
    reported_at      TEXT NOT NULL,      -- 机构上报时间
    exposure_at      TEXT,               -- 自述/推断暴露时间
    location_id      TEXT REFERENCES locations(id),
    contact_methods  TEXT NOT NULL DEFAULT '[]',   -- JSON 数组：拍打虫体/接触虫液……
    skin_area        TEXT NOT NULL DEFAULT '[]',   -- JSON 数组：皮损范围
    activity         TEXT,               -- 共同活动（如：河边夜钓/操场晚训）
    care_destination TEXT,               -- 就医去向
    narrative        TEXT,               -- 自述原文（含线上照片转述）
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_case ON reports(case_id);
CREATE INDEX IF NOT EXISTS idx_reports_time ON reports(reported_at);
CREATE INDEX IF NOT EXISTS idx_reports_loc ON reports(location_id);

-- 三层证据：自述 / 医护观察 / 已核实事实 ------------------------------
CREATE TABLE IF NOT EXISTS evidence_items (
    id          TEXT PRIMARY KEY,
    report_id   TEXT NOT NULL REFERENCES reports(id),
    level       TEXT NOT NULL,            -- self_report/clinician_obs/verified_fact
    kind        TEXT NOT NULL,            -- lesion_photo/activity/lab/field_visit...
    content     TEXT NOT NULL,
    recorded_by TEXT REFERENCES users(id),
    recorded_at TEXT NOT NULL,
    verifier_id TEXT REFERENCES users(id),
    verified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_report ON evidence_items(report_id);
CREATE INDEX IF NOT EXISTS idx_evidence_level ON evidence_items(level);

-- 待研判事件 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'pending_review',
    signature     TEXT NOT NULL UNIQUE,  -- 构成报告集合的哈希，保证扫描幂等
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL,
    centroid_lat  REAL,
    centroid_lng  REAL,
    radius_m      REAL,
    reasons       TEXT NOT NULL DEFAULT '[]',   -- 触发依据（空间/时间/共同活动）
    thresholds    TEXT NOT NULL,            -- 生成时阈值快照 JSON
    created_at    TEXT NOT NULL,
    reviewed_by   TEXT REFERENCES users(id),
    reviewed_at   TEXT,
    review_note   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);

CREATE TABLE IF NOT EXISTS event_reports (
    event_id  TEXT NOT NULL REFERENCES events(id),
    report_id TEXT NOT NULL REFERENCES reports(id),
    PRIMARY KEY (event_id, report_id)
);

-- 待核实事实清单（值班员视图中"尚待核实"的来源）---------------------
CREATE TABLE IF NOT EXISTS fact_checks (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events(id),
    evidence_id TEXT NOT NULL REFERENCES evidence_items(id),
    claim       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending/verified/rejected
    checked_by  TEXT REFERENCES users(id),
    checked_at  TEXT,
    note        TEXT
);

-- 处置材料（面向区域的科学处置建议，非诊断）---------------------------
CREATE TABLE IF NOT EXISTS advisories (
    id          TEXT PRIMARY KEY,
    event_id    TEXT REFERENCES events(id),
    area        TEXT NOT NULL,            -- 对应区域
    title       TEXT NOT NULL,
    current_version_id TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advisory_versions (
    id             TEXT PRIMARY KEY,
    advisory_id    TEXT NOT NULL REFERENCES advisories(id),
    version_no     INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft', -- draft/active/downgraded/withdrawn
    body           TEXT NOT NULL,
    medical_advice TEXT NOT NULL,         -- 必须含明确就医指引
    valid_from     TEXT NOT NULL,
    valid_until    TEXT,                  -- 有效期；NULL 表示需人工续期
    supersedes_id  TEXT REFERENCES advisory_versions(id),
    change_reason  TEXT,
    created_by     TEXT REFERENCES users(id),
    created_at     TEXT NOT NULL,
    UNIQUE (advisory_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_advver_status ON advisory_versions(status);

-- 公开入口/下发渠道 ---------------------------------------------------
CREATE TABLE IF NOT EXISTS channels (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    is_public    INTEGER NOT NULL DEFAULT 1   -- 公开入口：更正须逐一口径确认
);

-- 送达记录：只增留痕。版本事后撤回也不修改或删除本行 -----------------
CREATE TABLE IF NOT EXISTS deliveries (
    id             TEXT PRIMARY KEY,
    version_id     TEXT NOT NULL REFERENCES advisory_versions(id),
    channel_id     TEXT NOT NULL REFERENCES channels(id),
    body_snapshot  TEXT NOT NULL,         -- 实际送达内容快照
    medical_snapshot TEXT NOT NULL,
    delivered_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_version ON deliveries(version_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_channel ON deliveries(channel_id);

-- 错误科普更正单 ------------------------------------------------------
CREATE TABLE IF NOT EXISTS corrections (
    id                   TEXT PRIMARY KEY,
    advisory_id          TEXT NOT NULL REFERENCES advisories(id),
    bad_claim            TEXT NOT NULL,    -- 如"牙膏止痒"
    affected_version_id  TEXT NOT NULL REFERENCES advisory_versions(id),
    new_version_id       TEXT REFERENCES advisory_versions(id),
    status               TEXT NOT NULL DEFAULT 'open', -- open/resolved
    created_at           TEXT NOT NULL,
    resolved_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_corrections_status ON corrections(status);

-- 每个曾送达旧口径的公开入口都必须确认已更新 --------------------------
CREATE TABLE IF NOT EXISTS correction_confirmations (
    correction_id TEXT NOT NULL REFERENCES corrections(id),
    channel_id    TEXT NOT NULL REFERENCES channels(id),
    confirmed_by  TEXT REFERENCES users(id),
    confirmed_at  TEXT,
    note          TEXT,
    PRIMARY KEY (correction_id, channel_id)
);

-- 错误说法清单：发布守卫据此拦截"以缓解替代就医"的内容 ---------------
CREATE TABLE IF NOT EXISTS misinfo_claims (
    claim      TEXT PRIMARY KEY,
    advice     TEXT NOT NULL
);

-- 运行期配置（阈值等）-------------------------------------------------
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 审计日志（重点记录儿童身份信息访问）---------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    user_id   TEXT REFERENCES users(id),
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT,
    detail    TEXT
);
"""

DEFAULT_MISINFO = [
    ("牙膏止痒", "牙膏可能刺激灼伤面，不应涂抹；出现条索状红斑、水疱等应及时就医，由医护处理。"),
    ("牙膏可以治隐翅虫皮炎", "牙膏无治疗作用，且可能加重皮肤损伤；症状明显应尽快到皮肤科或急诊就诊。"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, seed: bool = True) -> None:
    conn.executescript(SCHEMA)
    if seed:
        _seed_config(conn)
        _seed_misinfo(conn)
        _seed_orgs(conn)
    conn.commit()


def _seed_config(conn: sqlite3.Connection) -> None:
    defaults = {
        "spatial_radius_m": config.DEFAULT_SPATIAL_RADIUS_M,
        "time_window_hours": config.DEFAULT_TIME_WINDOW_HOURS,
        "min_cases": config.DEFAULT_MIN_CASES,
        "min_sources": config.DEFAULT_MIN_SOURCES,
        "common_activity_min_cases": config.DEFAULT_COMMON_ACTIVITY_MIN_CASES,
    }
    conn.executemany(
        "INSERT OR IGNORE INTO config(key, value) VALUES(?, ?)",
        list(defaults.items()),
    )


def _seed_misinfo(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO misinfo_claims(claim, advice) VALUES(?, ?)",
        DEFAULT_MISINFO,
    )


def _seed_orgs(conn: sqlite3.Connection) -> None:
    agencies = [
        ("ag-hospital", config.AGENCY_HOSPITAL, "区人民医院（夜间急诊）"),
        ("ag-school", config.AGENCY_SCHOOL, "河畔中学"),
        ("ag-community", config.AGENCY_COMMUNITY, "河滨社区卫生服务中心"),
        ("ag-cdc", config.AGENCY_CDC, "区疾控中心"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO agencies(id, agency_type, name) VALUES(?, ?, ?)",
        agencies,
    )
    users = [
        ("u-reporter-hosp", "ag-hospital", config.ROLE_REPORTER, "医院上报员"),
        ("u-doctor", "ag-hospital", config.ROLE_CLINICIAN, "急诊医生"),
        ("u-reporter-school", "ag-school", config.ROLE_REPORTER, "学校校医"),
        ("u-reporter-comm", "ag-community", config.ROLE_REPORTER, "社区网格员"),
        ("u-dispatch", "ag-cdc", config.ROLE_DISPATCHER, "疾控值班员"),
        ("u-invest", "ag-cdc", config.ROLE_INVESTIGATOR, "流调人员"),
        ("u-admin", "ag-cdc", config.ROLE_ADMIN, "系统管理员"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO users(id, agency_id, role, name) VALUES(?, ?, ?, ?)",
        users,
    )
    channels = [
        ("ch-official", "疾控公众号", 1),
        ("ch-school", "学校家长群", 1),
        ("ch-board", "社区公告栏", 1),
        ("ch-internal", "值班内部工单", 0),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO channels(id, name, is_public) VALUES(?, ?, ?)",
        channels,
    )


def get_config(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    raw = {r["key"]: r["value"] for r in rows}
    return {
        "spatial_radius_m": float(raw["spatial_radius_m"]),
        "time_window_hours": float(raw["time_window_hours"]),
        "min_cases": int(raw["min_cases"]),
        "min_sources": int(raw["min_sources"]),
        "common_activity_min_cases": int(raw["common_activity_min_cases"]),
    }


def set_config_value(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO config(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
