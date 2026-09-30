"""聚集检测与事件研判。

扫描规则（阈值全部来自配置，可运行时调整）：
- 时间窗：病例暴露/上报锚点落在同一 time_window_hours 内；
- 空间：两病例地点距离 ≤ spatial_radius_m（无坐标时要求同一地点）；
- 共同活动：归一化后的活动描述相同，且涉及病例数 ≥ common_activity_min_cases。

时间窗内通过"空间相邻 或 共同活动"连边，连通分量构成候选群；
群内独立病例数 ≥ min_cases 且独立来源机构数 ≥ min_sources 时，
生成且仅生成一个 **待研判** 事件（绝不自动诊断、不自动确认）。
事件签名为构成报告集合的哈希，重复扫描幂等。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from typing import Any

from . import config
from .db import dumps, get_config, now_iso
from .errors import NotFound, ValidationError
from .reports import parse_dt
from .security import Principal, audit


# ----------------------------------------------------------- 几何/文本

EARTH_R = 6_371_000.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def _norm_activity(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(unicodedata.normalize("NFKC", value).strip().lower().split())


# ----------------------------------------------------------- 数据读取

def _case_anchors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """每个活跃病例一条锚点：时间取最早暴露时间（缺则取最早上报时间），
    活动取该最早时点对应报告的活动，地点取同一时点报告的地点。"""
    rows = conn.execute(
        """
        SELECT r.case_id, r.reported_at, r.exposure_at, r.activity,
               l.id AS loc_id, l.lat AS lat, l.lng AS lng
        FROM reports r
        JOIN cases c ON c.id = r.case_id AND c.merged_into_id IS NULL
        LEFT JOIN locations l ON l.id = r.location_id
        """
    ).fetchall()

    earliest: dict[str, datetime] = {}
    for row in rows:
        anchor = parse_dt(row["exposure_at"] or row["reported_at"])
        cur = earliest.get(row["case_id"])
        if cur is None or anchor < cur:
            earliest[row["case_id"]] = anchor

    anchors: dict[str, dict[str, Any]] = {}
    for row in rows:
        anchor = parse_dt(row["exposure_at"] or row["reported_at"])
        if anchor != earliest[row["case_id"]]:
            continue
        # 多个并列最早报告时，优先保留有坐标/有活动的一条
        existing = anchors.get(row["case_id"])
        candidate = {
            "case_id": row["case_id"],
            "anchor": anchor,
            "loc_id": row["loc_id"],
            "lat": row["lat"],
            "lng": row["lng"],
            "activity": _norm_activity(row["activity"]),
        }
        if existing is None or (
            existing["lat"] is None and candidate["lat"] is not None
        ) or (not existing["activity"] and candidate["activity"]):
            anchors[row["case_id"]] = candidate
    return list(anchors.values())


def _spatially_close(a: dict[str, Any], b: dict[str, Any], radius: float) -> bool:
    if a["loc_id"] and a["loc_id"] == b["loc_id"]:
        return True
    if a["lat"] is None or b["lat"] is None or a["lng"] is None or b["lng"] is None:
        return False
    return haversine_m(a["lat"], a["lng"], b["lat"], b["lng"]) <= radius


def _connected_groups(cases: list[dict[str, Any]], thresholds: dict[str, Any]) -> list[set[str]]:
    """窗内按 空间相邻 或 共同活动连边，返回连通分量。"""
    radius = thresholds["spatial_radius_m"]
    # 统计每个活动涉及的病例数
    activity_count: dict[str, int] = {}
    for c in cases:
        if c["activity"]:
            activity_count[c["activity"]] = activity_count.get(c["activity"], 0) + 1
    act_min = thresholds["common_activity_min_cases"]

    n = len(cases)
    adj: dict[int, set[int]] = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            linked = _spatially_close(cases[i], cases[j], radius)
            if not linked and cases[i]["activity"] and cases[i]["activity"] == cases[j]["activity"]:
                linked = activity_count[cases[i]["activity"]] >= act_min
            if linked:
                adj[i].add(j)
                adj[j].add(i)

    seen: set[int] = set()
    groups: list[set[str]] = []
    for i in range(n):
        if i in seen:
            continue
        stack, comp = [i], {i}
        seen.add(i)
        while stack:
            cur = stack.pop()
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    comp.add(nb)
                    stack.append(nb)
        groups.append({cases[k]["case_id"] for k in comp})
    return groups


# ----------------------------------------------------------- 扫描

def _group_reports(
    conn: sqlite3.Connection, case_ids: set[str], window_start: str, window_end: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.*, a.agency_type, a.name AS agency_name,
               l.name AS location_name, c.is_minor, c.pseudonym
        FROM reports r
        JOIN agencies a ON a.id = r.agency_id
        LEFT JOIN locations l ON l.id = r.location_id
        JOIN cases c ON c.id = r.case_id
        WHERE r.case_id IN (%s)
          AND COALESCE(r.exposure_at, r.reported_at) BETWEEN ? AND ?
        ORDER BY r.reported_at
        """ % ",".join("?" * len(case_ids)),
        (*case_ids, window_start, window_end),
    ).fetchall()


def _signature(report_ids: list[str]) -> str:
    return "evt-" + hashlib.sha1("|".join(sorted(report_ids)).encode()).hexdigest()[:14]


def scan_events(conn: sqlite3.Connection, principal: Principal) -> dict[str, Any]:
    """执行一次聚集扫描，创建新达到阈值的待研判事件；幂等。"""
    thresholds = get_config(conn)
    window_h = thresholds["time_window_hours"]
    anchors = _case_anchors(conn)

    created: list[str] = []
    seen_signatures: set[str] = set()
    # 已有事件覆盖的病例集合：候选组与其相同或是其子集时不重复告警
    existing = conn.execute(
        """SELECT er.event_id, r.case_id FROM event_reports er
           JOIN reports r ON r.id = er.report_id
           JOIN events e ON e.id = er.event_id AND e.status != 'dismissed'"""
    ).fetchall()
    by_event: dict[str, set[str]] = {}
    for row in existing:
        by_event.setdefault(row["event_id"], set()).add(row["case_id"])
    covered_case_sets: list[set[str]] = list(by_event.values())

    # 以每个病例锚点为窗起点滑动
    for start in sorted(a["anchor"] for a in anchors):
        end = start + timedelta(hours=window_h)
        window_cases = [
            a for a in anchors
            if start <= a["anchor"] <= end
        ]
        if len(window_cases) < thresholds["min_cases"]:
            continue

        for group in _connected_groups(window_cases, thresholds):
            if len(group) < thresholds["min_cases"]:
                continue
            ws, we = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
            rows = _group_reports(conn, group, ws, we)
            report_ids = [r["id"] for r in rows]
            if len(report_ids) < thresholds["min_cases"]:
                continue
            source_agencies = {r["agency_id"] for r in rows}
            if len(source_agencies) < thresholds["min_sources"]:
                continue

            sig = _signature(report_ids)
            if sig in seen_signatures:
                continue
            if conn.execute("SELECT 1 FROM events WHERE signature=?", (sig,)).fetchone():
                continue
            # 病例集合已被某未驳回事件覆盖（相同或为其子集）则不重复建
            if any(group <= covered for covered in covered_case_sets):
                continue
            seen_signatures.add(sig)
            covered_case_sets.append(set(group))

            reasons = _build_reasons(conn, rows, thresholds)
            event_id = sig
            # 质心经地点表坐标计算（报告行本身不带坐标）
            loc_ids = {r["location_id"] for r in rows if r["location_id"]}
            loc_rows = conn.execute(
                f"SELECT lat, lng FROM locations WHERE id IN "
                f"({','.join('?' * len(loc_ids))})",
                tuple(loc_ids),
            ).fetchall() if loc_ids else []
            lats = [r["lat"] for r in loc_rows if r["lat"] is not None]
            lngs = [r["lng"] for r in loc_rows if r["lng"] is not None]
            centroid = (sum(lats) / len(lats), sum(lngs) / len(lngs)) if lats else (None, None)

            conn.execute(
                """INSERT INTO events(
                    id, status, signature, window_start, window_end,
                    centroid_lat, centroid_lng, radius_m, reasons, thresholds, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, config.EVENT_PENDING, sig, ws, we,
                    centroid[0], centroid[1], thresholds["spatial_radius_m"],
                    dumps(reasons), json.dumps(thresholds, ensure_ascii=False), now_iso(),
                ),
            )
            conn.executemany(
                "INSERT INTO event_reports(event_id, report_id) VALUES(?, ?)",
                [(event_id, rid) for rid in report_ids],
            )
            _create_fact_checks(conn, event_id, rows)
            created.append(event_id)

    conn.commit()
    audit(conn, principal, "event.scan", "event", None,
          {"created": len(created), "thresholds": thresholds})
    conn.commit()
    return {"created": created, "scanned_cases": len(anchors), "thresholds": thresholds}


def _build_reasons(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], thresholds: dict[str, Any]
) -> list[dict[str, Any]]:
    case_ids = {r["case_id"] for r in rows}
    reasons = [{
        "dimension": "time",
        "detail": f"{len(case_ids)} 名病例落在 "
                  f"{thresholds['time_window_hours']} 小时窗内",
    }]
    locs = {r["location_name"] for r in rows if r["location_name"]}
    if locs:
        reasons.append({
            "dimension": "space",
            "detail": f"地点相距 ≤ {thresholds['spatial_radius_m']} 米：{sorted(locs)}",
        })
    activities: dict[str, int] = {}
    for r in rows:
        act = _norm_activity(r["activity"]) if "activity" in r.keys() else None
        if act:
            activities[act] = activities.get(act, 0) + 1
    for act, count in activities.items():
        if count >= thresholds["common_activity_min_cases"]:
            reasons.append({
                "dimension": "common_activity",
                "detail": f"{count} 名病例存在共同活动：{act}",
            })
    source_ids = {r["agency_id"] for r in rows}
    reasons.append({
        "dimension": "sources",
        "detail": f"涉及 {len(source_ids)} 家独立上报机构",
    })
    return reasons


def _create_fact_checks(conn: sqlite3.Connection, event_id: str, rows: list[sqlite3.Row]) -> None:
    """所有尚未到 verified_fact 层的证据进入"待核实事实"清单。"""
    report_ids = [r["id"] for r in rows]
    evidences = conn.execute(
        f"SELECT * FROM evidence_items WHERE report_id IN "
        f"({','.join('?' * len(report_ids))}) AND level != ?",
        (*report_ids, config.EVIDENCE_VERIFIED_FACT),
    ).fetchall()
    for ev in evidences:
        fc_id = "fc-" + hashlib.sha1(
            f"{event_id}|{ev['id']}".encode()
        ).hexdigest()[:12]
        conn.execute(
            "INSERT OR IGNORE INTO fact_checks(id, event_id, evidence_id, claim, status) "
            "VALUES(?, ?, ?, ?, 'pending')",
            (fc_id, event_id, ev["id"], ev["content"][:120]),
        )


# ----------------------------------------------------------- 查询/研判

def list_events(conn: sqlite3.Connection, status: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT e.*, "
        "(SELECT COUNT(*) FROM event_reports er WHERE er.event_id=e.id) AS report_count, "
        "(SELECT COUNT(*) FROM fact_checks fc WHERE fc.event_id=e.id AND fc.status='pending') "
        "AS pending_facts "
        "FROM events e"
    )
    params: tuple[Any, ...] = ()
    if status:
        sql += " WHERE e.status=?"
        params = (status,)
    sql += " ORDER BY e.created_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        raise NotFound(f"事件不存在: {event_id}")
    data = dict(event)
    data["thresholds"] = json.loads(event["thresholds"])
    data["reasons"] = json.loads(event["reasons"])

    # 由哪些独立报告构成：保留各报告的来源机构与证据分层
    reports = conn.execute(
        """
        SELECT r.id, r.case_id, r.agency_id, a.name AS agency_name, a.agency_type,
               r.source_ref, r.reported_at, r.exposure_at, r.contact_methods,
               r.skin_area, r.activity, r.care_destination, r.location_id,
               l.name AS location_name, c.pseudonym, c.is_minor
        FROM event_reports er
        JOIN reports r ON r.id = er.report_id
        JOIN agencies a ON a.id = r.agency_id
        LEFT JOIN locations l ON l.id = r.location_id
        JOIN cases c ON c.id = r.case_id
        WHERE er.event_id=?
        ORDER BY a.agency_type, r.reported_at
        """,
        (event_id,),
    ).fetchall()
    components = []
    for r in reports:
        item = dict(r)
        for key in ("contact_methods", "skin_area"):
            item[key] = json.loads(r[key])
        levels = conn.execute(
            "SELECT level, COUNT(*) AS n FROM evidence_items WHERE report_id=? GROUP BY level",
            (r["id"],),
        ).fetchall()
        item["evidence_by_level"] = {row["level"]: row["n"] for row in levels}
        item["has_verified_fact"] = item["evidence_by_level"].get(
            config.EVIDENCE_VERIFIED_FACT, 0
        ) > 0
        components.append(item)
    data["component_reports"] = components
    data["independent_cases"] = len({r["case_id"] for r in reports})
    data["independent_sources"] = len({r["agency_id"] for r in reports})

    checks = conn.execute(
        """
        SELECT fc.*, ev.level AS evidence_level, ev.kind AS evidence_kind,
               a.name AS agency_name, r.id AS report_id
        FROM fact_checks fc
        JOIN evidence_items ev ON ev.id = fc.evidence_id
        JOIN reports r ON r.id = ev.report_id
        JOIN agencies a ON a.id = r.agency_id
        WHERE fc.event_id=?
        ORDER BY CASE fc.status WHEN 'pending' THEN 0 ELSE 1 END, fc.id
        """,
        (event_id,),
    ).fetchall()
    data["fact_checks"] = [dict(r) for r in checks]
    data["pending_fact_count"] = sum(1 for r in checks if r["status"] == "pending")
    return data


def review_event(
    conn: sqlite3.Connection,
    principal: Principal,
    event_id: str,
    *,
    decision: str,
    note: str | None = None,
) -> None:
    """值班员/流调人工研判：confirmed 或 dismissed。系统不提供自动确诊。"""
    if decision not in (config.EVENT_CONFIRMED, config.EVENT_DISMISSED):
        raise ValidationError("decision 必须是 confirmed 或 dismissed")
    event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        raise NotFound(f"事件不存在: {event_id}")
    if event["status"] != config.EVENT_PENDING:
        raise ValidationError(f"事件已研判，当前状态：{event['status']}")
    conn.execute(
        "UPDATE events SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
        (decision, principal.id, now_iso(), note, event_id),
    )
    audit(conn, principal, "event.review", "event", event_id,
          {"decision": decision, "note": note})
    conn.commit()


def resolve_fact_check(
    conn: sqlite3.Connection,
    principal: Principal,
    fact_check_id: str,
    *,
    status: str,
    note: str | None = None,
) -> None:
    if status not in ("verified", "rejected"):
        raise ValidationError("核实结论必须是 verified 或 rejected")
    row = conn.execute("SELECT * FROM fact_checks WHERE id=?", (fact_check_id,)).fetchone()
    if not row:
        raise NotFound(f"待核实事实不存在: {fact_check_id}")
    conn.execute(
        "UPDATE fact_checks SET status=?, checked_by=?, checked_at=?, note=? WHERE id=?",
        (status, principal.id, now_iso(), note, fact_check_id),
    )
    if status == "verified":
        # 核实通过：底层证据升级为已核实事实
        conn.execute(
            "UPDATE evidence_items SET level=?, verifier_id=?, verified_at=? WHERE id=?",
            (config.EVIDENCE_VERIFIED_FACT, principal.id, now_iso(), row["evidence_id"]),
        )
    audit(conn, principal, "fact_check.resolve", "fact_check", fact_check_id,
          {"status": status, "note": note})
    conn.commit()
