"""聚集阈值检测、事件构成与人工研判（系统不自动诊断）。"""
from __future__ import annotations

from tests.conftest import (
    COMM, DISPATCH, DOCTOR, HOSP, INVEST, SCHOOL, register_locations,
)


def _night_report(loc, when, identity, *, age=13, activity=None,
                  agency_narrative=None, care="区人民医院急诊"):
    return {
        "identity_token": identity,
        "age": age,
        "location_id": loc,
        "exposure_at": when,
        "reported_at": when,
        "contact_methods": ["拍打虫体"],
        "skin_area": ["前臂"],
        "activity": activity,
        "care_destination": care,
        "narrative": agency_narrative or "夜间河边活动后出现红斑水疱",
    }


def _seed_cluster(client):
    """同一夜：医院、学校、社区各报一例，地点在 500 米内。"""
    locs = register_locations(client)
    t = "2026-09-19T22:15:00+08:00"
    client.post("/api/reports", user=DOCTOR, body={
        **_night_report(locs["park"], t, "person-a"),
        "clinician_observations": [
            {"kind": "lesion", "content": "前臂条索状红斑伴水疱"}
        ],
    })
    client.post("/api/reports", user=SCHOOL, body=_night_report(
        locs["dorm"], "2026-09-19T22:30:00+08:00", "student-b",
        agency_narrative="家长群照片转述，未见本人"))
    client.post("/api/reports", user=COMM, body=_night_report(
        locs["train"], "2026-09-19T22:50:00+08:00", "resident-c", age=41,
        agency_narrative="居民自述操场夜跑后颈部灼痛"))
    return locs


def test_scan_creates_one_pending_event_with_component_reports(client):
    _seed_cluster(client)
    resp = client.post("/api/events/scan", user=DISPATCH, body={})
    assert resp.status_code == 200
    assert len(resp.json["created"]) == 1
    event_id = resp.json["created"][0]

    event = client.get(f"/api/events/{event_id}", user=DISPATCH).json
    assert event["status"] == "pending_review"
    assert event["independent_cases"] == 3
    assert event["independent_sources"] == 3
    # 构成报告各自带来源机构
    agencies = {r["agency_id"] for r in event["component_reports"]}
    assert agencies == {"ag-hospital", "ag-school", "ag-community"}
    # 触发依据包含时间与空间两个维度
    dims = {r["dimension"] for r in event["reasons"]}
    assert {"time", "space", "sources"} <= dims
    # 阈值快照随事件留档
    assert event["thresholds"]["min_cases"] == 3


def test_scan_is_idempotent(client):
    _seed_cluster(client)
    first = client.post("/api/events/scan", user=DISPATCH, body={}).json
    second = client.post("/api/events/scan", user=DISPATCH, body={}).json
    assert first["created"] == second["created"] or second["created"] == []
    total = client.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    assert total == 1


def test_dispatcher_sees_pending_facts_and_only_verified_promotes(client):
    _seed_cluster(client)
    event_id = client.post("/api/events/scan", user=DISPATCH, body={}).json["created"][0]
    event = client.get(f"/api/events/{event_id}", user=DISPATCH).json

    # 学校与社区的说法、医护观察都尚未核实
    assert event["pending_fact_count"] >= 3
    pending = [f for f in event["fact_checks"] if f["status"] == "pending"]
    assert any(f["evidence_level"] == "clinician_obs" for f in pending)
    assert any(f["evidence_level"] == "self_report" for f in pending)

    # 流调核实其中一条
    fc = next(f for f in pending if f["evidence_level"] == "clinician_obs")
    done = client.post(f"/api/fact-checks/{fc['id']}/resolve", user=INVEST, body={
        "status": "verified", "note": "复诊病历吻合",
    })
    assert done.status_code == 200

    event2 = client.get(f"/api/events/{event_id}", user=DISPATCH).json
    assert event2["pending_fact_count"] == event["pending_fact_count"] - 1
    # 被核实的底层证据已升级
    ev = client.conn.execute(
        "SELECT level FROM evidence_items WHERE id=?", (fc["evidence_id"],)
    ).fetchone()["level"]
    assert ev == "verified_fact"

    # 系统不自动确认事件：仍待人工研判
    assert event2["status"] == "pending_review"


def test_event_requires_multiple_independent_sources(client):
    locs = register_locations(client)
    t = "2026-09-19T22:15:00+08:00"
    # 三例但全部来自同一家机构
    for i, ident in enumerate(["p1", "p2", "p3"]):
        client.post("/api/reports", user=SCHOOL, body=_night_report(
            locs["dorm"], t, ident, activity="宿舍熄灯后自习"))
    resp = client.post("/api/events/scan", user=DISPATCH, body={})
    assert resp.json["created"] == []


def test_common_activity_links_reports_without_close_coordinates(client):
    register_locations(client)
    far = client.post("/api/locations", user=COMM, body={
        "name": "校外河边夜钓点", "lat": 30.05000, "lng": 120.05000,
        "place_kind": "河边",
    }).json["location_id"]
    # 三例均为"河边夜钓"：第一例只给地点名（无坐标），后两例在远处坐标点
    client.post("/api/reports", user=SCHOOL, body={
        "identity_token": "s1", "age": 15,
        "exposure_at": "2026-09-19T21:00:00+08:00",
        "reported_at": "2026-09-19T22:00:00+08:00",
        "contact_methods": ["接触虫液"], "skin_area": ["颈部"],
        "activity": "河边夜钓", "care_destination": "门诊", "narrative": "夜钓后起疱",
        "location": {"name": "北岸夜钓区"},
    })
    for user, ident in ((HOSP, "s2"), (COMM, "s3")):
        client.post("/api/reports", user=user, body={
            "identity_token": ident, "age": 30, "location_id": far,
            "exposure_at": "2026-09-19T21:20:00+08:00",
            "reported_at": "2026-09-19T22:30:00+08:00",
            "contact_methods": ["拍打虫体"], "skin_area": ["面颊"],
            "activity": "河边夜钓", "care_destination": "急诊",
            "narrative": "同一夜钓活动",
        })
    resp = client.post("/api/events/scan", user=DISPATCH, body={})
    assert len(resp.json["created"]) == 1
    event = client.get(f"/api/events/{resp.json['created'][0]}", user=DISPATCH).json
    dims = {r["dimension"] for r in event["reasons"]}
    assert "common_activity" in dims


def test_below_threshold_no_event(client):
    locs = register_locations(client)
    t = "2026-09-19T22:15:00+08:00"
    client.post("/api/reports", user=DOCTOR,
                body=_night_report(locs["park"], t, "only-one", age=40))
    resp = client.post("/api/events/scan", user=DISPATCH, body={})
    assert resp.json["created"] == []


def test_human_review_confirms_or_dismisses(client):
    _seed_cluster(client)
    event_id = client.post("/api/events/scan", user=DISPATCH, body={}).json["created"][0]

    # 上报员不能研判
    denied = client.post(f"/api/events/{event_id}/review", user=HOSP,
                         body={"decision": "confirmed"})
    assert denied.status_code == 403

    ok = client.post(f"/api/events/{event_id}/review", user=DISPATCH, body={
        "decision": "confirmed", "note": "经流调核实为同一暴露源，启动处置",
    })
    assert ok.status_code == 200
    event = client.get(f"/api/events/{event_id}", user=DISPATCH).json
    assert event["status"] == "confirmed"

    # 不可重复研判
    again = client.post(f"/api/events/{event_id}/review", user=DISPATCH,
                        body={"decision": "dismissed"})
    assert again.status_code in (409, 422)
