"""上报、夜间集中批次、跨机构去重与证据分层。"""
from __future__ import annotations

from tests.conftest import (
    COMM, DISPATCH, DOCTOR, HOSP, SCHOOL, register_locations,
)


def _report(location_id, when, *, identity="child-0919-a", age=13,
            narrative=None, activity=None, care="区人民医院急诊",
            contact=None, skin=None):
    return {
        "identity_token": identity,
        "age": age,
        "location_id": location_id,
        "exposure_at": when,
        "reported_at": when,
        "contact_methods": contact or ["拍打虫体"],
        "skin_area": skin or ["前臂", "面颊"],
        "activity": activity,
        "care_destination": care,
        "narrative": narrative or "夜间在河边后出现条索状红斑，线上问诊群有人说是隐翅虫",
    }


def test_night_batch_accepts_multiple_and_keeps_indexed_errors(client):
    locs = register_locations(client)
    body = {
        "note": "9月19日夜间接诊集中上报",
        "reports": [
            _report(locs["dorm"], "2026-09-19T22:15:00+08:00"),
            _report(locs["park"], "2026-09-19T22:40:00+08:00",
                    identity="person-b", age=34),
            {"reported_at": "2026-09-19T23:00:00+08:00"},  # 缺地点等必填业务信息
        ],
    }
    resp = client.post("/api/batches", user=HOSP, body=body)
    assert resp.status_code == 200
    assert resp.json["accepted"] == 2
    assert resp.json["rejected"] == 1
    assert resp.json["errors"][0]["index"] == 2

    detail = client.get(f"/api/batches/{resp.json['batch_id']}", user=DISPATCH)
    assert detail.status_code == 200
    assert detail.json["report_count"] == 3
    assert len(detail.json["reports"]) == 2  # 只落库被接受的两条


def test_same_identity_across_agencies_merges_case_but_keeps_sources(client):
    locs = register_locations(client)
    # 医院夜间先报（接诊医护，含医护观察）
    r1 = client.post("/api/reports", user=DOCTOR, body={
        **_report(locs["dorm"], "2026-09-19T22:15:00+08:00"),
        "clinician_observations": [
            {"kind": "lesion", "content": "右前臂条索状红斑伴水疱，灼痛，未见脓性渗出"}
        ],
    })
    assert r1.status_code == 200, r1.json
    case_id = r1.json["case_id"]
    assert r1.json["case_created"] is True

    # 学校次晨用同一脱敏令牌上报：病例复用，不新建
    r2 = client.post("/api/reports", user=SCHOOL, body=_report(
        locs["dorm"], "2026-09-19T22:20:00+08:00",
        narrative="家长群照片显示孩子手臂起疱，昨晚宿舍熄灯后有飞虫",
    ))
    assert r2.status_code == 200
    assert r2.json["case_id"] == case_id
    assert r2.json["case_created"] is False
    assert r2.json["report_id"] != r1.json["report_id"]

    # 两条报告来源机构都保留
    rows = client.conn.execute(
        "SELECT agency_id FROM reports WHERE case_id=?", (case_id,)
    ).fetchall()
    assert {r["agency_id"] for r in rows} == {"ag-hospital", "ag-school"}


def test_evidence_three_layers_are_distinct(client):
    locs = register_locations(client)
    rid = client.post("/api/reports", user=DOCTOR, body=_report(
        locs["park"], "2026-09-19T22:40:00+08:00", identity="person-b", age=34
    )).json["report_id"]

    # 学校上报员不能登记医护观察（角色越权）
    forbidden = client.post(f"/api/reports/{rid}/evidence", user=SCHOOL, body={
        "level": "clinician_obs", "kind": "lesion", "content": "红斑",
    })
    assert forbidden.status_code == 403

    # 医护观察
    ok = client.post(f"/api/reports/{rid}/evidence", user=DOCTOR, body={
        "level": "clinician_obs", "kind": "lesion",
        "content": "颈部条索状红斑，患者诉灼痛",
    })
    assert ok.status_code == 200

    # 上报员不能把观察直接登记成"已核实事实"
    ev = ok.json["evidence_id"]
    cheat = client.post(f"/api/evidence/{ev}/verify", user=DOCTOR, body={"accept": True})
    assert cheat.status_code == 403

    # 流调核实后才升级
    verified = client.post(f"/api/evidence/{ev}/verify",
                           user="u-invest", body={"accept": True, "note": "现场核实"})
    assert verified.status_code == 200
    level = client.conn.execute(
        "SELECT level FROM evidence_items WHERE id=?", (ev,)
    ).fetchone()["level"]
    assert level == "verified_fact"

    # 自述、观察、核实事实三层数量分别可溯
    counts = {
        r["level"]: r["n"]
        for r in client.conn.execute(
            "SELECT level, COUNT(*) n FROM evidence_items WHERE report_id=? GROUP BY level",
            (rid,),
        ).fetchall()
    }
    assert counts["self_report"] >= 1
    assert counts["verified_fact"] >= 1


def test_location_reuse_by_normalized_name(client):
    a = client.post("/api/locations", user=COMM,
                    body={"name": "河滨公园 亲水平台", "lat": 30.0, "lng": 120.0})
    b = client.post("/api/locations", user=HOSP,
                    body={"name": "河滨公园亲水平台", "lat": 30.00001, "lng": 120.0})
    assert a.json["location_id"] == b.json["location_id"]


def test_manual_merge_preserves_each_report_source(client):
    locs = register_locations(client)
    r1 = client.post("/api/reports", user=COMM, body={
        **_report(locs["park"], "2026-09-19T22:40:00+08:00", identity=None),
        "narrative": "居民自述被虫爬，涂了牙膏止痒",
    })
    r2 = client.post("/api/reports", user=HOSP, body={
        **_report(locs["park"], "2026-09-19T23:10:00+08:00", identity=None),
        "narrative": "同一居民来急诊，登记方式不同未自动匹配",
    })
    dup, master = r1.json["case_id"], r2.json["case_id"]
    assert dup != master

    merged = client.post("/api/cases/merge", user=DISPATCH,
                         body={"duplicate_id": dup, "master_id": master})
    assert merged.status_code == 200

    rows = client.conn.execute(
        "SELECT agency_id FROM reports WHERE case_id=?", (master,)
    ).fetchall()
    assert {r["agency_id"] for r in rows} == {"ag-community", "ag-hospital"}
    assert client.conn.execute(
        "SELECT merged_into_id FROM cases WHERE id=?", (dup,)
    ).fetchone()["merged_into_id"] == master
