"""处置材料：版本、有效期、降级/撤回、送达留痕、错误科普更正闭环。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import DISPATCH, HOSP

BODY_V1 = (
    "河滨公园亲水平台周边发现疑似隐翅虫聚集暴露线索。"
    "夜间靠近水边绿化带请穿长袖、使用照明，发现虫体不要拍打或压碎，"
    "应吹落或抖落后用清水冲洗接触部位。"
)
MEDICAL = (
    "若已出现条索状红斑、灼痛或水疱，请及时到皮肤科或急诊就诊，由医护处理；"
    "不要自行涂抹刺激性物品，也不要因症状暂时缓解而延误就医。"
)
BODY_BAD = (
    "被隐翅虫爬过后可自行涂牙膏止痒，"
    "牙膏止痒即可，一般不用去医院，症状消了就没事。"
)
# 更正稿必须引用正确口径原文（misinfo_claims 中登记的 advice）
GOOD_ADVICE = "牙膏可能刺激灼伤面，不应涂抹；出现条索状红斑、水疱等应及时就医，由医护处理。"
BODY_V2 = (
    "更正：网传“牙膏止痒”并不正确。" + GOOD_ADVICE +
    "夜间靠近水边绿化带请穿长袖、使用照明，不要拍打虫体，吹落后清水冲洗。"
)


def _make_published(client, body=BODY_V1, medical=MEDICAL, valid_days=7):
    aid = client.post("/api/advisories", user=DISPATCH, body={
        "title": "河滨公园区域防护提示", "area": "河滨公园亲水平台",
    }).json["advisory_id"]
    until = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=valid_days)).isoformat()
    vid = client.post(f"/api/advisories/{aid}/versions", user=DISPATCH, body={
        "body": body, "medical_advice": medical, "valid_until": until,
    }).json["version_id"]
    client.post(f"/api/versions/{vid}/publish", user=DISPATCH, body={})
    return aid, vid


def test_publish_guard_blocks_toothpaste_and_relief_replacing_care(client):
    aid = client.post("/api/advisories", user=DISPATCH, body={
        "title": "提示", "area": "河滨公园",
    }).json["advisory_id"]
    # 缺就医指引
    r1 = client.post(f"/api/advisories/{aid}/versions", user=DISPATCH, body={
        "body": "注意防护", "medical_advice": "回家观察",
    })
    assert r1.status_code == 422
    assert r1.json["error"]["code"] == "medical_advice_required"

    # 含错误说法（登记在案）且未给正确口径
    r2 = client.post(f"/api/advisories/{aid}/versions", user=DISPATCH, body={
        "body": "被咬后涂牙膏止痒即可。", "medical_advice": "严重时请就医。",
    })
    assert r2.status_code == 422
    assert r2.json["error"]["code"] == "misinfo_blocked"

    # 以缓解替代就医
    r3 = client.post(f"/api/advisories/{aid}/versions", user=DISPATCH, body={
        "body": "用清水冲洗，痒止住即可。",
        "medical_advice": "缓解后不用就医。",
    })
    assert r3.status_code == 422


def test_draft_invisible_public_active_visible(client):
    aid = client.post("/api/advisories", user=DISPATCH, body={
        "title": "提示", "area": "河滨公园",
    }).json["advisory_id"]
    vid = client.post(f"/api/advisories/{aid}/versions", user=DISPATCH, body={
        "body": BODY_V1, "medical_advice": MEDICAL,
    }).json["version_id"]

    assert client.get("/api/public/advisories").json["advisories"] == []
    client.post(f"/api/versions/{vid}/publish", user=DISPATCH, body={})
    pub = client.get("/api/public/advisories").json["advisories"]
    assert len(pub) == 1 and pub[0]["version_id"] == vid


def test_versions_and_old_delivery_remain_after_withdraw(client):
    aid, vid = _make_published(client)
    # 送达到两个公开入口 + 一个内部渠道
    client.post(f"/api/versions/{vid}/deliver", user=DISPATCH, body={
        "channel_ids": ["ch-official", "ch-school", "ch-internal"],
    })
    # 撤回：通过新版本实现
    w = client.post(f"/api/advisories/{aid}/withdraw", user=DISPATCH,
                    body={"reason": "后续核查排除该区域暴露源"})
    assert w.status_code == 200
    new_vid = w.json["new_version_id"]
    assert new_vid != vid

    # 公开端不再展示
    assert client.get("/api/public/advisories").json["advisories"] == []

    # 旧版本记录与送达快照仍在（已经送达的旧提醒留痕）
    full = client.get(f"/api/advisories/{aid}", user=DISPATCH).json
    statuses = {v["version_no"]: v["status"] for v in full["versions"]}
    assert statuses[1] == "superseded"
    assert statuses[2] == "withdrawn"
    old = next(v for v in full["versions"] if v["id"] == vid)
    assert old["delivery_count"] == 3
    assert len(full["deliveries"]) == 3
    assert all(d["body_snapshot"] == BODY_V1 for d in full["deliveries"])

    # 撤回版不可再下发
    again = client.post(f"/api/versions/{new_vid}/deliver", user=DISPATCH,
                        body={"channel_ids": ["ch-official"]})
    assert again.status_code == 409


def test_downgrade_when_evidence_insufficient(client):
    aid, vid = _make_published(client)
    r = client.post(f"/api/advisories/{aid}/downgrade", user=DISPATCH,
                    body={"reason": "两例经核实与虫体暴露无关，证据不足"})
    assert r.status_code == 200
    assert client.get("/api/public/advisories").json["advisories"] == []
    full = client.get(f"/api/advisories/{aid}", user=DISPATCH).json
    assert full["current_version_id"] == r.json["new_version_id"]


def test_expired_version_drops_from_public_and_is_listed(client):
    # 正常发布后把有效期回溯到过去，模拟已到期（不允许新建时直接填过去时间）
    aid, vid = _make_published(client, valid_days=7)
    client.conn.execute(
        "UPDATE advisory_versions SET valid_until='2026-09-20T00:00:00+08:00' WHERE id=?",
        (vid,),
    )
    client.conn.commit()
    pub = client.get("/api/public/advisories").json["advisories"]
    assert pub == []
    expired = client.get("/api/advisories/expired", user=DISPATCH).json["expired"]
    assert any(e["advisory_id"] == aid for e in expired)


def _legacy_published_with_bad_content(client):
    """模拟系统上线前（错误说法尚未入库时）已发布并送达三个公开入口的旧材料。"""
    from monitor.db import now_iso
    conn = client.conn
    aid, vid = "adv-legacy", "ver-legacy-1"
    conn.execute(
        "INSERT INTO advisories(id, event_id, area, title, current_version_id, created_at) "
        "VALUES(?, NULL, ?, ?, ?, ?)",
        (aid, "河滨公园", "防护提示（历史版本）", vid, now_iso()),
    )
    conn.execute(
        """INSERT INTO advisory_versions(
            id, advisory_id, version_no, status, body, medical_advice,
            valid_from, valid_until, created_by, created_at
        ) VALUES(?, ?, 1, 'active', ?, ?, ?, NULL, ?, ?)""",
        (vid, aid, BODY_BAD, "出现明显水疱请及时到急诊就诊。",
         now_iso(), "u-dispatch", now_iso()),
    )
    for i, ch in enumerate(("ch-official", "ch-school", "ch-board")):
        conn.execute(
            "INSERT INTO deliveries(id, version_id, channel_id, body_snapshot, "
            "medical_snapshot, delivered_at) VALUES(?, ?, ?, ?, ?, ?)",
            (f"dlv-legacy-{i}", vid, ch, BODY_BAD,
             "出现明显水疱请及时到急诊就诊。", now_iso()),
        )
    conn.commit()
    return aid


def test_correction_requires_every_public_entry_confirmed(client):
    # 1) 历史遗留：错误材料在错误说法入库前已送达三个公开入口
    aid = _legacy_published_with_bad_content(client)
    assert client.get("/api/public/advisories").json["advisories"][0]["body"] == BODY_BAD

    # 2) 发现民间错误科普，登记入库
    reg = client.post("/api/misinfo-claims", user="u-admin", body={
        "claim": "牙膏止痒", "advice": GOOD_ADVICE,
    })
    assert reg.status_code == 200

    # 3) 开更正单：新稿未附正确口径 -> 被守卫拦截
    bad_open = client.post("/api/corrections", user=DISPATCH, body={
        "advisory_id": aid, "bad_claim": "牙膏止痒",
        "corrected_body": "网传牙膏止痒的说法不对，请不要拍打虫体。",
        "corrected_medical_advice": MEDICAL,
    })
    assert bad_open.status_code == 422

    # 4) 正确更正稿发布，三个公开入口逐一待确认
    opened = client.post("/api/corrections", user=DISPATCH, body={
        "advisory_id": aid, "bad_claim": "牙膏止痒",
        "corrected_body": BODY_V2,
        "corrected_medical_advice": MEDICAL,
    }).json
    cid = opened["correction_id"]
    assert sorted(opened["awaiting_channels"]) == ["ch-board", "ch-official", "ch-school"]

    detail = client.get(f"/api/corrections/{cid}", user=DISPATCH).json
    assert detail["all_public_entries_updated"] is False
    # 每个入口都能看到自己当时送达的旧内容快照
    assert all(ch["old_snapshot"] == BODY_BAD for ch in detail["channels"])

    # 只确认两个入口：更正单仍 open
    for ch in ("ch-official", "ch-school"):
        r = client.post(f"/api/corrections/{cid}/confirm", user=DISPATCH,
                        body={"channel_id": ch, "note": "已替换为 v2"})
        assert r.json["resolved"] is False
    assert client.get(f"/api/corrections/{cid}", user=DISPATCH).json["status"] == "open"

    # 重复确认被拒绝
    dup = client.post(f"/api/corrections/{cid}/confirm", user=DISPATCH,
                      body={"channel_id": "ch-official"})
    assert dup.status_code == 409

    # 内部渠道不在公开入口确认清单中
    internal = client.post(f"/api/corrections/{cid}/confirm", user=DISPATCH,
                           body={"channel_id": "ch-internal"})
    assert internal.status_code == 409

    # 5) 最后一个入口确认 -> 更正单关闭，公开端已是新口径
    last = client.post(f"/api/corrections/{cid}/confirm", user=DISPATCH,
                       body={"channel_id": "ch-board", "note": "公告栏已换贴"})
    assert last.json["resolved"] is True
    detail2 = client.get(f"/api/corrections/{cid}", user=DISPATCH).json
    assert detail2["all_public_entries_updated"] is True
    pub = client.get("/api/public/advisories").json["advisories"]
    assert len(pub) == 1
    assert GOOD_ADVICE in pub[0]["body"]
    assert "牙膏止痒即可" not in pub[0]["body"]


def test_reporter_cannot_manage_advisories(client):
    r = client.post("/api/advisories", user=HOSP,
                    body={"title": "x", "area": "y"})
    assert r.status_code == 403
