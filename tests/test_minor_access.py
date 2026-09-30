"""儿童身份信息仅向实际处置者开放；访问与拒绝均留痕。"""
from __future__ import annotations

from tests.conftest import COMM, DISPATCH, DOCTOR, HOSP, INVEST, SCHOOL, register_locations


def _child_report(loc, when="2026-09-19T22:15:00+08:00"):
    return {
        "identity_token": "minor-x-001",
        "age": 13,
        "gender": "女",
        "location_id": loc,
        "exposure_at": when,
        "reported_at": when,
        "contact_methods": ["拍打虫体"],
        "skin_area": ["前臂"],
        "care_destination": "区人民医院急诊",
        "narrative": "宿舍熄灯后飞虫落脸，拍打后晨起起疱",
    }


def test_non_handler_cannot_view_minor_identity_and_denial_audited(client):
    locs = register_locations(client)
    created = client.post("/api/reports", user=SCHOOL, body=_child_report(locs["dorm"]))
    case_id = created.json["case_id"]

    # 无 identity 参数：只返回脱敏信息，任何值班员可见
    base = client.get(f"/api/cases/{case_id}", user=DISPATCH)
    assert base.status_code == 200
    assert "age" not in base.json
    assert base.json["is_minor"] is True

    # 值班员不是处置者：拒绝查看身份
    denied = client.get(f"/api/cases/{case_id}?identity=1", user=DISPATCH)
    assert denied.status_code == 403
    assert denied.json["error"]["code"] == "minor_protected"

    # 学校上报员即便来自发现机构，也无权查看身份
    denied2 = client.get(f"/api/cases/{case_id}?identity=1", user=SCHOOL)
    assert denied2.status_code == 403

    log = client.conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE entity='case' AND action='case.identity_denied'"
    ).fetchone()["c"]
    assert log >= 2


def test_actual_handler_sees_minor_identity_and_access_audited(client):
    locs = register_locations(client)
    # 经接诊医护上报，医护自动成为处置者
    created = client.post("/api/reports", user=DOCTOR, body=_child_report(locs["dorm"]))
    case_id = created.json["case_id"]

    ok = client.get(f"/api/cases/{case_id}?identity=1", user=DOCTOR)
    assert ok.status_code == 200
    assert ok.json["age"] == 13
    assert ok.json["gender"] == "女"

    viewed = client.conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE action='case.identity_view' AND entity_id=?",
        (case_id,),
    ).fetchone()["c"]
    assert viewed == 1


def test_dispatcher_can_be_assigned_as_handler_after_field_dispatch(client):
    locs = register_locations(client)
    created = client.post("/api/reports", user=COMM, body=_child_report(locs["park"]))
    case_id = created.json["case_id"]

    # 值班员不能自行授权；需有处置权的角色（流调）登记其为实际到场处置者
    self_grant = client.post(f"/api/cases/{case_id}/handlers",
                             user=DISPATCH, body={"user_id": DISPATCH})
    assert self_grant.status_code == 403

    grant = client.post(f"/api/cases/{case_id}/handlers",
                        user=INVEST, body={"user_id": DISPATCH})
    assert grant.status_code == 200

    ok = client.get(f"/api/cases/{case_id}?identity=1", user=DISPATCH)
    assert ok.status_code == 200
    assert ok.json["age"] == 13


def test_unauthenticated_requests_rejected_except_public(client):
    resp = client.get("/api/events")
    assert resp.status_code == 401
    pub = client.get("/api/public/advisories")
    assert pub.status_code == 200


def test_reporter_cannot_review_events(client):
    resp = client.post("/api/events/scan", user=HOSP, body={})
    assert resp.status_code == 403
