"""上报、地点复用与跨机构去重服务。

核心约束：
- reports 只增不改不删；重复上报合并的是"病例（cases）"，
  每条报告的来源机构、原始证据层级永久保留。
- 证据严格三层：self_report（自述/线上照片转述）、
  clinician_obs（医护观察）、verified_fact（已核实事实）。
- 支持机构在夜间把多条记录作为一个批次集中上报。
"""
from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from datetime import datetime
from typing import Any

from . import config
from .db import dumps, now_iso
from .errors import ConflictError, NotFound, ValidationError
from .security import Principal, assign_handler, audit

_ID_SALT_KEY = "identity_salt"
_ID_SALT_DEFAULT = "dev-only-salt-change-in-production"


# ---------------------------------------------------------------- 时间

def parse_dt(value: str) -> datetime:
    """解析 ISO8601；朴素时间按本地时区（+08:00）处理。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


# ------------------------------------------------------------ 脱敏身份

def _salt(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM config WHERE key=?", (_ID_SALT_KEY,)).fetchone()
    return row["value"] if row else _ID_SALT_DEFAULT


def hash_identity(conn: sqlite3.Connection, token: str) -> str:
    """对机构提交的脱敏身份令牌做单向哈希。

    token 由各机构按约定生成（如出生日期+姓氏拼音+联系电话后四位的盐化摘要），
    后端不接触姓名、证件号等明文。
    """
    return hashlib.sha256(f"{_salt(conn)}|{token.strip().lower()}".encode()).hexdigest()


def _pseudonym(conn: sqlite3.Connection, identity_hash: str | None) -> str:
    if identity_hash:
        return "P-" + identity_hash[:10].upper()
    n = conn.execute("SELECT COUNT(*) AS c FROM cases").fetchone()["c"]
    return f"P-N{n + 1:04d}"


# -------------------------------------------------------------- 地点

def normalize_name(name: str) -> str:
    key = unicodedata.normalize("NFKC", name).strip().lower()
    return "".join(key.split())


def register_location(
    conn: sqlite3.Connection,
    *,
    name: str,
    lat: float | None = None,
    lng: float | None = None,
    place_kind: str | None = None,
) -> str:
    """登记或复用地点：归一化名相同即视为同一地点（跨机构去重）。"""
    if not name or not name.strip():
        raise ValidationError("地点名称不能为空")
    key = normalize_name(name)
    row = conn.execute("SELECT id FROM locations WHERE name_key=?", (key,)).fetchone()
    if row:
        return row["id"]
    loc_id = "loc-" + hashlib.sha1(key.encode()).hexdigest()[:12]
    conn.execute(
        "INSERT INTO locations(id, name, name_key, lat, lng, place_kind, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (loc_id, name.strip(), key, lat, lng, place_kind, now_iso()),
    )
    conn.commit()
    return loc_id


def _resolve_location(conn: sqlite3.Connection, payload: dict[str, Any]) -> str | None:
    if payload.get("location_id"):
        row = conn.execute(
            "SELECT id FROM locations WHERE id=?", (payload["location_id"],)
        ).fetchone()
        if not row:
            raise ValidationError(f"地点不存在: {payload['location_id']}")
        return row["id"]
    if payload.get("location"):
        loc = payload["location"]
        if isinstance(loc, str):
            return register_location(conn, name=loc)
        return register_location(
            conn,
            name=loc["name"],
            lat=loc.get("lat"),
            lng=loc.get("lng"),
            place_kind=loc.get("place_kind"),
        )
    return None


# -------------------------------------------------------------- 病例

def find_or_create_case(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> tuple[str, bool]:
    """按脱敏身份令牌匹配活跃病例；返回 (case_id, 是否新建)。"""
    token = payload.get("identity_token")
    identity_hash = hash_identity(conn, token) if token else None
    if identity_hash:
        row = conn.execute(
            "SELECT id FROM cases WHERE identity_hash=? AND merged_into_id IS NULL",
            (identity_hash,),
        ).fetchone()
        if row:
            return row["id"], False

    age = payload.get("age")
    is_minor = 1 if (isinstance(age, int) and age < config.MINOR_AGE) else 0
    if identity_hash:
        case_id = "case-" + identity_hash[:12]
    else:
        import uuid
        case_id = "case-" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO cases(id, pseudonym, identity_hash, age, is_minor, gender, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            case_id,
            _pseudonym(conn, identity_hash),
            identity_hash,
            age,
            is_minor,
            payload.get("gender"),
            now_iso(),
        ),
    )
    return case_id, True


def merge_cases(
    conn: sqlite3.Connection, principal: Principal, duplicate_id: str, master_id: str
) -> None:
    """把重复病例合并入主病例：报告归属改挂主病例，重复病例标记合并。

    报告本身不删除，来源机构与证据层级原样保留。
    """
    if duplicate_id == master_id:
        raise ValidationError("不能将病例合并到自身")
    for cid in (duplicate_id, master_id):
        if not conn.execute("SELECT 1 FROM cases WHERE id=?", (cid,)).fetchone():
            raise NotFound(f"病例不存在: {cid}")
    conn.execute(
        "UPDATE reports SET case_id=? WHERE case_id=?", (master_id, duplicate_id)
    )
    # 证据随报告走（evidence_items.report_id 未变），无需改动
    conn.execute(
        "UPDATE cases SET merged_into_id=? WHERE id=?", (master_id, duplicate_id)
    )
    # 处置者授权并入主病例，保证合并后实际处置者仍可访问
    conn.execute(
        "INSERT OR IGNORE INTO case_handlers(case_id, user_id, granted_at, granted_by) "
        "SELECT ?, user_id, granted_at, granted_by FROM case_handlers WHERE case_id=?",
        (master_id, duplicate_id),
    )
    audit(conn, principal, "case.merge", "case", master_id, {"merged": duplicate_id})
    conn.commit()


def master_case_id(conn: sqlite3.Connection, case_id: str) -> str:
    row = conn.execute(
        "SELECT id, merged_into_id FROM cases WHERE id=?", (case_id,)
    ).fetchone()
    if row is None:
        return case_id
    return row["merged_into_id"] or row["id"]


# -------------------------------------------------------------- 上报

def _as_str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValidationError(f"{field} 必须是非空字符串数组")
    return [v.strip() for v in value]


def submit_report(
    conn: sqlite3.Connection,
    principal: Principal,
    payload: dict[str, Any],
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """提交一条暴露报告。必须携带时间；地点/接触方式/皮损范围/就医去向按实记录。"""
    reported_at = payload.get("reported_at") or now_iso()
    # 校验时间格式
    parse_dt(reported_at)
    if payload.get("exposure_at"):
        parse_dt(payload["exposure_at"])

    location_id = _resolve_location(conn, payload)
    if not location_id:
        raise ValidationError("暴露地点为必填项（location_id 或 location）")
    contact_methods = _as_str_list(payload.get("contact_methods"), "contact_methods")
    skin_area = _as_str_list(payload.get("skin_area"), "skin_area")
    has_core_info = bool(
        contact_methods or skin_area
        or (payload.get("activity") or "").strip()
        or (payload.get("narrative") or "").strip()
        or payload.get("care_destination")
    )
    if not has_core_info:
        raise ValidationError(
            "报告至少应包含接触方式、皮损范围、共同活动、自述或就医去向之一"
        )
    case_id, case_new = find_or_create_case(conn, payload)

    report_id = "rpt-" + hashlib.sha1(
        f"{case_id}|{principal.agency_id}|{reported_at}|{now_iso()}".encode()
    ).hexdigest()[:12]

    conn.execute(
        """INSERT INTO reports(
            id, case_id, agency_id, source_ref, batch_id, reported_at, exposure_at,
            location_id, contact_methods, skin_area, activity, care_destination,
            narrative, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            report_id, case_id, principal.agency_id, payload.get("source_ref"),
            batch_id, reported_at, payload.get("exposure_at"), location_id,
            dumps(_as_str_list(payload.get("contact_methods"), "contact_methods")),
            dumps(_as_str_list(payload.get("skin_area"), "skin_area")),
            (payload.get("activity") or "").strip() or None,
            payload.get("care_destination"),
            (payload.get("narrative") or "").strip() or None,
            now_iso(),
        ),
    )

    # 自述天然成为 self_report 层证据（含线上照片转述，明确不算确诊）
    if payload.get("narrative"):
        add_evidence(
            conn, principal, report_id,
            level=config.EVIDENCE_SELF_REPORT,
            kind="self_narrative",
            content=payload["narrative"],
        )
    for obs in payload.get("clinician_observations", []) or []:
        add_evidence(
            conn, principal, report_id,
            level=config.EVIDENCE_CLINICIAN_OBS,
            kind=obs.get("kind", "clinical_note"),
            content=obs["content"],
        )
    for fact in payload.get("verified_facts", []) or []:
        add_evidence(
            conn, principal, report_id,
            level=config.EVIDENCE_VERIFIED_FACT,
            kind=fact.get("kind", "verified"),
            content=fact["content"],
            verified=fact.get("verified", True),
        )

    # 接诊医护即实际处置者：自动获得该病例身份信息访问权（学校/社区上报员不获得）
    if case_id and principal.role in (config.ROLE_CLINICIAN,):
        assign_handler(conn, case_id, principal.id, principal)

    conn.commit()
    return {
        "report_id": report_id,
        "case_id": case_id,
        "case_created": case_new,
        "location_id": location_id,
        "batch_id": batch_id,
    }


def submit_batch(
    conn: sqlite3.Connection, principal: Principal, payload: dict[str, Any]
) -> dict[str, Any]:
    """夜间集中上报：一个机构一次提交多条记录，整体成批、逐条可溯。"""
    items = payload.get("reports")
    if not isinstance(items, list) or not items:
        raise ValidationError("reports 必须是非空数组")
    batch_id = "batch-" + hashlib.sha1(
        f"{principal.agency_id}|{now_iso()}".encode()
    ).hexdigest()[:10]
    conn.execute(
        "INSERT INTO batches(id, agency_id, submitted_at, note, report_count) "
        "VALUES(?, ?, ?, ?, ?)",
        (batch_id, principal.agency_id, now_iso(), payload.get("note"), len(items)),
    )
    results = []
    errors = []
    for idx, item in enumerate(items):
        try:
            results.append(submit_report(conn, principal, item, batch_id=batch_id))
        except ValidationError as exc:
            errors.append({"index": idx, "error": exc.message})
    conn.commit()
    return {
        "batch_id": batch_id,
        "accepted": len(results),
        "rejected": len(errors),
        "errors": errors,
        "reports": results,
    }


def add_evidence(
    conn: sqlite3.Connection,
    principal: Principal,
    report_id: str,
    *,
    level: str,
    kind: str,
    content: str,
    verified: bool = False,
) -> str:
    if level not in config.EVIDENCE_LEVELS:
        raise ValidationError(f"证据层级非法: {level}")
    if not content or not content.strip():
        raise ValidationError("证据内容不能为空")
    report = conn.execute("SELECT id FROM reports WHERE id=?", (report_id,)).fetchone()
    if not report:
        raise NotFound(f"报告不存在: {report_id}")

    evidence_id = "ev-" + hashlib.sha1(
        f"{report_id}|{level}|{content}|{now_iso()}".encode()
    ).hexdigest()[:12]

    verifier_id = None
    verified_at = None
    if level == config.EVIDENCE_VERIFIED_FACT:
        if not verified:
            raise ValidationError("已核实事实必须经核实流程确认")
        if principal.role not in (config.ROLE_INVESTIGATOR, config.ROLE_ADMIN):
            raise ConflictError("仅流调人员可登记已核实事实", code="fact_verification_required")
        verifier_id, verified_at = principal.id, now_iso()
    elif level == config.EVIDENCE_CLINICIAN_OBS and principal.role not in (
        config.ROLE_CLINICIAN, config.ROLE_ADMIN
    ):
        raise ConflictError("医护观察须由医护角色登记", code="clinician_only")

    conn.execute(
        """INSERT INTO evidence_items(
            id, report_id, level, kind, content, recorded_by, recorded_at, verifier_id, verified_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (evidence_id, report_id, level, kind, content.strip(),
         principal.id, now_iso(), verifier_id, verified_at),
    )
    return evidence_id


def verify_evidence(
    conn: sqlite3.Connection, principal: Principal, evidence_id: str, *, accept: bool, note: str | None = None
) -> None:
    """流调人员把一条观察/自述核实为事实，或驳回。"""
    row = conn.execute(
        "SELECT * FROM evidence_items WHERE id=?", (evidence_id,)
    ).fetchone()
    if not row:
        raise NotFound(f"证据不存在: {evidence_id}")
    if accept:
        conn.execute(
            "UPDATE evidence_items SET level=?, verifier_id=?, verified_at=? WHERE id=?",
            (config.EVIDENCE_VERIFIED_FACT, principal.id, now_iso(), evidence_id),
        )
    audit(conn, principal, "evidence.verify", "evidence", evidence_id,
          {"accept": accept, "previous_level": row["level"], "note": note})
    conn.commit()
