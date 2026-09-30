"""处置材料的发布、版本、有效期、送达留痕与更正闭环。

关键纪律：
- 材料是**科学处置建议**，不是诊断结论；每条版本必须携带明确就医指引，
  禁止以"症状缓解"替代就医建议（发布守卫强制校验）。
- 版本只增不改：降级/撤回通过新版本实现；旧版本状态与送达快照永久保留。
- 公开端只暴露"当前版本 + active + 未过有效期"的材料。
- 错误科普（如"牙膏止痒"）触发更正单：凡送达过旧口径的**公开入口**，
  必须逐一口径确认已更新，更正单才能关闭。
"""
from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from typing import Any

from . import config
from .db import now_iso
from .errors import ConflictError, NotFound, ValidationError
from .reports import parse_dt
from .security import Principal, audit

SEEK_MEDICAL_KEYWORDS = ("就医", "就诊", "急诊", "门诊", "医院")
FORBIDDEN_MEDICAL_PHRASES = (
    "无需就医", "不用就医", "不必就医", "不要就医", "无须就医",
    "缓解后无需就医", "缓解后不用就医", "止痒即可，不必",
)
SUPERSEDED = "superseded"


# ------------------------------------------------------------ 内容守卫

def _normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).lower().split())


def screen_content(conn: sqlite3.Connection, body: str, medical_advice: str) -> None:
    """发布前内容守卫：拦截错误说法与"以缓解替代就医"。

    更正类材料允许引用错误说法进行驳斥，但必须同时逐条附上
    misinfo_claims 中登记的正确口径原文，以防只引用不纠正。
    """
    if not body or not body.strip():
        raise ValidationError("材料正文不能为空")
    if not medical_advice or not medical_advice.strip():
        raise ValidationError("必须提供就医指引（medical_advice）")

    norm_body = _normalize(body)
    rows = conn.execute("SELECT claim, advice FROM misinfo_claims").fetchall()
    for row in rows:
        bad = _normalize(row["claim"])
        if bad in norm_body:
            good = _normalize(row["advice"])
            if good not in norm_body:
                raise ValidationError(
                    f"正文引用了错误说法“{row['claim']}”，必须同时附上正确口径：{row['advice']}",
                    code="misinfo_blocked",
                )

    norm_advice = _normalize(medical_advice)
    for phrase in FORBIDDEN_MEDICAL_PHRASES:
        if _normalize(phrase) in norm_advice or _normalize(phrase) in norm_body:
            raise ValidationError(
                f"不得出现“{phrase}”等以症状缓解替代就医的表述",
                code="medical_advice_required",
            )
    if not any(k in medical_advice for k in SEEK_MEDICAL_KEYWORDS):
        raise ValidationError(
            "就医指引中必须明确提示就医/就诊（如：症状明显请及时到皮肤科或急诊就诊）",
            code="medical_advice_required",
        )


def add_misinfo_claim(conn: sqlite3.Connection, claim: str, advice: str) -> None:
    conn.execute(
        "INSERT INTO misinfo_claims(claim, advice) VALUES(?, ?) "
        "ON CONFLICT(claim) DO UPDATE SET advice=excluded.advice",
        (claim.strip(), advice.strip()),
    )
    conn.commit()


# ------------------------------------------------------------ 材料与版本

def create_advisory(
    conn: sqlite3.Connection,
    principal: Principal,
    *,
    title: str,
    area: str,
    event_id: str | None = None,
) -> str:
    if not title.strip() or not area.strip():
        raise ValidationError("标题与区域不能为空")
    if event_id and not conn.execute(
        "SELECT 1 FROM events WHERE id=?", (event_id,)
    ).fetchone():
        raise NotFound(f"事件不存在: {event_id}")
    advisory_id = "adv-" + hashlib.sha1(
        f"{area}|{title}|{now_iso()}".encode()
    ).hexdigest()[:12]
    conn.execute(
        "INSERT INTO advisories(id, event_id, area, title, created_at) VALUES(?, ?, ?, ?, ?)",
        (advisory_id, event_id, area.strip(), title.strip(), now_iso()),
    )
    audit(conn, principal, "advisory.create", "advisory", advisory_id, {"title": title})
    conn.commit()
    return advisory_id


def create_version(
    conn: sqlite3.Connection,
    principal: Principal,
    *,
    advisory_id: str,
    body: str,
    medical_advice: str,
    valid_until: str | None = None,
    change_reason: str | None = None,
) -> str:
    advisory = conn.execute(
        "SELECT * FROM advisories WHERE id=?", (advisory_id,)
    ).fetchone()
    if not advisory:
        raise NotFound(f"材料不存在: {advisory_id}")
    if valid_until:
        expires = parse_dt(valid_until)
        if expires <= parse_dt(now_iso()):
            raise ValidationError("有效期不能早于当前时间")
    screen_content(conn, body, medical_advice)

    next_no = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM advisory_versions WHERE advisory_id=?",
        (advisory_id,),
    ).fetchone()["n"]
    version_id = "ver-" + hashlib.sha1(
        f"{advisory_id}|{next_no}|{now_iso()}".encode()
    ).hexdigest()[:12]
    conn.execute(
        """INSERT INTO advisory_versions(
            id, advisory_id, version_no, status, body, medical_advice,
            valid_from, valid_until, supersedes_id, change_reason, created_by, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            version_id, advisory_id, next_no, config.ADVISORY_DRAFT,
            body.strip(), medical_advice.strip(), now_iso(), valid_until, None,
            change_reason, principal.id, now_iso(),
        ),
    )
    audit(conn, principal, "advisory.version_create", "advisory_version", version_id,
          {"advisory_id": advisory_id, "version_no": next_no})
    conn.commit()
    return version_id


def publish_version(
    conn: sqlite3.Connection, principal: Principal, version_id: str
) -> None:
    """草稿发布为当前生效版本；旧当前版本标记 superseded。"""
    ver = conn.execute(
        "SELECT * FROM advisory_versions WHERE id=?", (version_id,)
    ).fetchone()
    if not ver:
        raise NotFound(f"版本不存在: {version_id}")
    if ver["status"] != config.ADVISORY_DRAFT:
        raise ConflictError(f"仅草稿可发布，当前状态：{ver['status']}")
    screen_content(conn, ver["body"], ver["medical_advice"])

    conn.execute(
        "UPDATE advisory_versions SET status=? WHERE id=?",
        (config.ADVISORY_ACTIVE, version_id),
    )
    prev = conn.execute(
        "SELECT current_version_id FROM advisories WHERE id=?", (ver["advisory_id"],)
    ).fetchone()["current_version_id"]
    if prev:
        conn.execute(
            "UPDATE advisory_versions SET status=? WHERE id=? AND status=?",
            (SUPERSEDED, prev, config.ADVISORY_ACTIVE),
        )
        conn.execute(
            "UPDATE advisory_versions SET supersedes_id=? WHERE id=?",
            (prev, version_id),
        )
    conn.execute(
        "UPDATE advisories SET current_version_id=? WHERE id=?",
        (version_id, ver["advisory_id"]),
    )
    audit(conn, principal, "advisory.publish", "advisory_version", version_id,
          {"supersedes": prev})
    conn.commit()


def _new_terminal_version(
    conn: sqlite3.Connection,
    principal: Principal,
    advisory_id: str,
    terminal_status: str,
    reason: str,
) -> str:
    """降级/撤回统一通过新版本留痕：新版本直接为终态，旧版本 superseded。"""
    advisory = conn.execute(
        "SELECT * FROM advisories WHERE id=?", (advisory_id,)
    ).fetchone()
    if not advisory:
        raise NotFound(f"材料不存在: {advisory_id}")
    current_id = advisory["current_version_id"]
    if not current_id:
        raise ConflictError("材料尚未发布过，无需降级/撤回")
    current = conn.execute(
        "SELECT * FROM advisory_versions WHERE id=?", (current_id,)
    ).fetchone()
    if current["status"] != config.ADVISORY_ACTIVE:
        raise ConflictError(f"当前版本非生效状态：{current['status']}")

    notice = {
        config.ADVISORY_DOWNGRADED: "【已降级】此前建议的证据强度不足，本提示降级为一般性观察提醒，不作风险结论。",
        config.ADVISORY_WITHDRAWN: "【已撤回】此前提示予以撤回，请勿继续据此采取处置行动。",
    }[terminal_status]
    next_no = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM advisory_versions WHERE advisory_id=?",
        (advisory_id,),
    ).fetchone()["n"]
    version_id = "ver-" + hashlib.sha1(
        f"{advisory_id}|{next_no}|{terminal_status}|{now_iso()}".encode()
    ).hexdigest()[:12]
    # 终态版本保留就医提醒纪律
    medical = ("如已出现明显皮肤症状，请及时到皮肤科或急诊就诊，"
               "不要因本提示变化而延误就医。")
    conn.execute(
        """INSERT INTO advisory_versions(
            id, advisory_id, version_no, status, body, medical_advice,
            valid_from, valid_until, supersedes_id, change_reason, created_by, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (version_id, advisory_id, next_no, terminal_status,
         notice, medical, now_iso(), now_iso(), current_id, reason,
         principal.id, now_iso()),
    )
    conn.execute(
        "UPDATE advisory_versions SET status=? WHERE id=?", (SUPERSEDED, current_id)
    )
    conn.execute(
        "UPDATE advisories SET current_version_id=? WHERE id=?",
        (version_id, advisory_id),
    )
    audit(conn, principal, f"advisory.{terminal_status}", "advisory_version",
          version_id, {"advisory_id": advisory_id, "reason": reason})
    conn.commit()
    return version_id


def downgrade_version(conn, principal, advisory_id, reason) -> str:
    return _new_terminal_version(conn, principal, advisory_id,
                                 config.ADVISORY_DOWNGRADED, reason)


def withdraw_version(conn, principal, advisory_id, reason) -> str:
    return _new_terminal_version(conn, principal, advisory_id,
                                 config.ADVISORY_WITHDRAWN, reason)


# ------------------------------------------------------------ 送达

def deliver(
    conn: sqlite3.Connection,
    principal: Principal,
    *,
    version_id: str,
    channel_ids: list[str],
) -> list[str]:
    """把生效版本送达渠道；内容快照落库，事后撤回也不改写本行。"""
    ver = conn.execute(
        "SELECT * FROM advisory_versions WHERE id=?", (version_id,)
    ).fetchone()
    if not ver:
        raise NotFound(f"版本不存在: {version_id}")
    if ver["status"] != config.ADVISORY_ACTIVE:
        raise ConflictError("仅生效版本可送达（撤回/降级版本不可下发）")
    if not channel_ids:
        raise ValidationError("至少指定一个渠道")

    delivery_ids = []
    for channel_id in channel_ids:
        ch = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        if not ch:
            raise NotFound(f"渠道不存在: {channel_id}")
        did = "dlv-" + hashlib.sha1(
            f"{version_id}|{channel_id}|{now_iso()}".encode()
        ).hexdigest()[:12]
        conn.execute(
            """INSERT INTO deliveries(
                id, version_id, channel_id, body_snapshot, medical_snapshot, delivered_at
            ) VALUES(?, ?, ?, ?, ?, ?)""",
            (did, version_id, channel_id, ver["body"], ver["medical_advice"], now_iso()),
        )
        delivery_ids.append(did)
    audit(conn, principal, "advisory.deliver", "advisory_version", version_id,
          {"channels": channel_ids})
    conn.commit()
    return delivery_ids


# ------------------------------------------------------------ 更正闭环

def open_correction(
    conn: sqlite3.Connection,
    principal: Principal,
    *,
    advisory_id: str,
    bad_claim: str,
    corrected_body: str,
    corrected_medical_advice: str,
    valid_until: str | None = None,
) -> dict[str, Any]:
    """发现错误科普：登记更正单，生成纠正版并发布。

    之后所有**曾送达旧版本的公开入口**都必须确认更新，更正单才能关闭。
    """
    advisory = conn.execute(
        "SELECT * FROM advisories WHERE id=?", (advisory_id,)
    ).fetchone()
    if not advisory:
        raise NotFound(f"材料不存在: {advisory_id}")
    affected_id = advisory["current_version_id"]
    if not affected_id:
        raise ConflictError("材料尚无已发布版本")

    new_version_id = create_version(
        conn, principal,
        advisory_id=advisory_id,
        body=corrected_body,
        medical_advice=corrected_medical_advice,
        valid_until=valid_until,
        change_reason=f"更正错误说法：{bad_claim}",
    )
    publish_version(conn, principal, new_version_id)

    cid = "cor-" + hashlib.sha1(
        f"{advisory_id}|{bad_claim}|{now_iso()}".encode()
    ).hexdigest()[:12]
    conn.execute(
        """INSERT INTO corrections(
            id, advisory_id, bad_claim, affected_version_id, new_version_id,
            status, created_at
        ) VALUES(?, ?, ?, ?, ?, 'open', ?)""",
        (cid, advisory_id, bad_claim.strip(), affected_id, new_version_id, now_iso()),
    )

    # 凡送达过旧版本的公开渠道，逐一生成待确认项（correction_confirmations
    # 中 confirmed_at 为 NULL 即待确认）
    channels = conn.execute(
        "SELECT DISTINCT d.channel_id FROM deliveries d JOIN channels ch ON ch.id=d.channel_id "
        "WHERE d.version_id=? AND ch.is_public=1",
        (affected_id,),
    ).fetchall()
    for row in channels:
        conn.execute(
            "INSERT OR IGNORE INTO correction_confirmations"
            "(correction_id, channel_id, confirmed_at) VALUES(?, ?, NULL)",
            (cid, row["channel_id"]),
        )
    audit(conn, principal, "correction.open", "correction", cid,
          {"bad_claim": bad_claim, "new_version": new_version_id})
    conn.commit()
    return {
        "correction_id": cid,
        "new_version_id": new_version_id,
        "awaiting_channels": [r["channel_id"] for r in channels],
    }


def confirm_correction(
    conn: sqlite3.Connection,
    principal: Principal,
    correction_id: str,
    *,
    channel_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    """公开入口责任人确认该入口的旧科普已替换为新版本口径。"""
    cor = conn.execute(
        "SELECT * FROM corrections WHERE id=?", (correction_id,)
    ).fetchone()
    if not cor:
        raise NotFound(f"更正单不存在: {correction_id}")
    row = conn.execute(
        "SELECT * FROM correction_confirmations WHERE correction_id=? AND channel_id=?",
        (correction_id, channel_id),
    ).fetchone()
    if not row:
        raise ConflictError("该渠道未送达过旧口径，无需确认（或渠道不存在）")
    if row["confirmed_at"]:
        raise ConflictError("该入口已确认过更新")
    conn.execute(
        "UPDATE correction_confirmations SET confirmed_by=?, confirmed_at=?, note=? "
        "WHERE correction_id=? AND channel_id=?",
        (principal.id, now_iso(), note, correction_id, channel_id),
    )

    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM correction_confirmations "
        "WHERE correction_id=? AND confirmed_at IS NULL",
        (correction_id,),
    ).fetchone()["c"]
    if pending == 0:
        conn.execute(
            "UPDATE corrections SET status='resolved', resolved_at=? WHERE id=?",
            (now_iso(), correction_id),
        )
    audit(conn, principal, "correction.confirm", "correction", correction_id,
          {"channel_id": channel_id, "remaining": pending})
    conn.commit()
    return {"remaining_pending": pending, "resolved": pending == 0}


def get_correction(conn: sqlite3.Connection, correction_id: str) -> dict[str, Any]:
    cor = conn.execute(
        "SELECT * FROM corrections WHERE id=?", (correction_id,)
    ).fetchone()
    if not cor:
        raise NotFound(f"更正单不存在: {correction_id}")
    data = dict(cor)
    rows = conn.execute(
        """SELECT cc.channel_id, ch.name, ch.is_public, cc.confirmed_at, cc.confirmed_by,
                  cc.note,
                  (SELECT body_snapshot FROM deliveries d
                    WHERE d.version_id=? AND d.channel_id=cc.channel_id
                    ORDER BY d.delivered_at LIMIT 1) AS old_snapshot
           FROM correction_confirmations cc
           JOIN channels ch ON ch.id = cc.channel_id
           WHERE cc.correction_id=?""",
        (cor["affected_version_id"], correction_id),
    ).fetchall()
    data["channels"] = [dict(r) for r in rows]
    data["all_public_entries_updated"] = all(r["confirmed_at"] for r in rows)
    return data


# ------------------------------------------------------------ 读取

def _is_effective(ver: sqlite3.Row, at: datetime | None = None) -> bool:
    if ver["status"] != config.ADVISORY_ACTIVE:
        return False
    if ver["valid_until"]:
        at = at or parse_dt(now_iso())
        if parse_dt(ver["valid_until"]) < at:
            return False
    return True


def public_listing(conn: sqlite3.Connection, *, area: str | None = None) -> list[dict[str, Any]]:
    """公开入口：仅当前、生效且未过有效期的版本；不返回撤回/降级/草稿。"""
    sql = """
        SELECT a.id AS advisory_id, a.area, a.title, a.event_id,
               v.id AS version_id, v.version_no, v.status, v.body, v.medical_advice,
               v.valid_from, v.valid_until
        FROM advisories a
        JOIN advisory_versions v ON v.id = a.current_version_id
        WHERE v.status = ?
    """
    params: list[Any] = [config.ADVISORY_ACTIVE]
    if area:
        sql += " AND a.area = ?"
        params.append(area)
    sql += " ORDER BY v.valid_from DESC"
    out = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        if not _is_effective(row):
            continue
        out.append(item)
    return out


def get_advisory_full(conn: sqlite3.Connection, advisory_id: str) -> dict[str, Any]:
    advisory = conn.execute(
        "SELECT * FROM advisories WHERE id=?", (advisory_id,)
    ).fetchone()
    if not advisory:
        raise NotFound(f"材料不存在: {advisory_id}")
    data = dict(advisory)
    versions = conn.execute(
        "SELECT * FROM advisory_versions WHERE advisory_id=? ORDER BY version_no",
        (advisory_id,),
    ).fetchall()
    data["versions"] = []
    for ver in versions:
        v = dict(ver)
        v["effective"] = _is_effective(ver) and ver["id"] == advisory["current_version_id"]
        v["delivery_count"] = conn.execute(
            "SELECT COUNT(*) AS c FROM deliveries WHERE version_id=?", (ver["id"],)
        ).fetchone()["c"]
        data["versions"].append(v)
    data["deliveries"] = [
        dict(r) for r in conn.execute(
            """SELECT d.*, ch.name AS channel_name, ch.is_public
               FROM deliveries d JOIN channels ch ON ch.id=d.channel_id
               WHERE d.version_id IN (
                   SELECT id FROM advisory_versions WHERE advisory_id=?
               ) ORDER BY d.delivered_at""",
            (advisory_id,),
        ).fetchall()
    ]
    return data


def list_expired(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """列出当前版本已过有效期的材料，提醒值班员续期（公开端已自动不展示）。"""
    rows = conn.execute(
        """SELECT a.id AS advisory_id, a.area, a.title, v.id AS version_id,
                  v.version_no, v.valid_until, v.status
           FROM advisories a JOIN advisory_versions v ON v.id=a.current_version_id
           WHERE v.valid_until IS NOT NULL AND v.valid_until < ?""",
        (now_iso(),),
    ).fetchall()
    return [dict(r) for r in rows]
