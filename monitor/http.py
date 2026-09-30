"""标准库 WSGI HTTP 层：零第三方依赖，便于复现与审计。

认证：请求头 X-User-Id 对应用户表（演示用；生产替换为真实鉴权）。
所有响应为 JSON；错误统一经 errors.MonitorError 映射。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable
from urllib.parse import parse_qs

from . import advisories as adv_service
from . import config as cfg
from . import db as dbmod
from . import events as event_service
from . import reports as report_service
from .errors import MonitorError
from .security import Principal, assign_handler, load_principal, require, view_case

Handler = Callable[[sqlite3.Connection, Principal, dict[str, Any], dict[str, str]], Any]


def _json_default(obj: Any) -> Any:
    from datetime import datetime
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class App:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.conn = dbmod.connect(db_path)
        dbmod.init_db(self.conn)
        self.routes: list[tuple[re.Pattern[str], str, Handler]] = []
        self._register()

    # ---------------------------------------------------------- 路由
    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            rx = re.compile("^" + re.sub(r"{(\w+)}", r"(?P<\1>[^/]+)", pattern) + "$")
            self.routes.append((rx, method, fn))
            return fn
        return deco

    def _register(self) -> None:
        r = self.route

        # 地点 / 上报
        r("POST", "/api/locations")(self._create_location)
        r("POST", "/api/reports")(self._submit_report)
        r("POST", "/api/batches")(self._submit_batch)
        r("GET", "/api/batches/{id}")(self._get_batch)
        r("POST", "/api/reports/{id}/evidence")(self._add_evidence)
        r("POST", "/api/evidence/{id}/verify")(self._verify_evidence)
        r("POST", "/api/cases/merge")(self._merge_cases)
        r("POST", "/api/cases/{id}/handlers")(self._assign_handler)
        r("GET", "/api/cases/{id}")(self._get_case)

        # 聚集检测 / 事件研判
        r("POST", "/api/events/scan")(self._scan)
        r("GET", "/api/events")(self._list_events)
        r("GET", "/api/events/{id}")(self._get_event)
        r("POST", "/api/events/{id}/review")(self._review_event)
        r("POST", "/api/fact-checks/{id}/resolve")(self._resolve_fact)

        # 阈值配置
        r("GET", "/api/thresholds")(self._get_thresholds)
        r("PUT", "/api/thresholds")(self._set_thresholds)
        r("POST", "/api/misinfo-claims")(self._add_misinfo)

        # 处置材料
        r("POST", "/api/advisories")(self._create_advisory)
        r("GET", "/api/advisories/expired")(self._expired)
        r("GET", "/api/advisories/{id}")(self._get_advisory)
        r("POST", "/api/advisories/{id}/versions")(self._create_version)
        r("POST", "/api/versions/{id}/publish")(self._publish_version)
        r("POST", "/api/advisories/{id}/downgrade")(self._downgrade)
        r("POST", "/api/advisories/{id}/withdraw")(self._withdraw)
        r("POST", "/api/versions/{id}/deliver")(self._deliver)

        # 更正闭环
        r("POST", "/api/corrections")(self._open_correction)
        r("POST", "/api/corrections/{id}/confirm")(self._confirm_correction)
        r("GET", "/api/corrections/{id}")(self._get_correction)

        # 公开端（无需登录，仅见行有效版本）
        r("GET", "/api/public/advisories")(self._public_advisories)

    # ---------------------------------------------------------- WSGI
    def __call__(self, environ: dict, start_response: Callable) -> list[bytes]:
        try:
            principal = load_principal(
                self.conn,
                environ.get("HTTP_X_USER_ID") or environ.get("HTTP_X_USER"),
            )
        except MonitorError as exc:
            # 公开端匿名放行；其余接口要求登录
            path = environ.get("PATH_INFO", "")
            if path.startswith("/api/public/"):
                principal = None
            else:
                return self._error(start_response, exc)

        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET")
        for rx, verb, handler in self.routes:
            m = rx.match(path)
            if m and verb == method:
                break
        else:
            return self._json(start_response, {
                "error": {"code": "not_found", "message": f"无此接口: {method} {path}"}
            }, 404)

        try:
            payload: dict[str, Any] = {}
            if method in ("POST", "PUT", "PATCH"):
                length = int(environ.get("CONTENT_LENGTH") or 0)
                raw = environ["wsgi.input"].read(length) if length else b""
                if raw:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError:
                        raise MonitorError("请求体不是合法 JSON", code="bad_json")
                    if not isinstance(payload, dict):
                        raise MonitorError("请求体必须是 JSON 对象", code="bad_json")
            query = {
                k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()
            }
            result = handler(self.conn, principal, payload, {**m.groupdict(), **query})
            status = 201 if method == "POST" and isinstance(result, dict) and result.get("_created") else 200
            if isinstance(result, dict):
                result.pop("_created", None)
            return self._json(start_response, result, status)
        except MonitorError as exc:
            return self._error(start_response, exc)

    def _json(self, start_response: Callable, body: Any, status: int) -> list[bytes]:
        data = json.dumps(body, ensure_ascii=False, default=_json_default).encode("utf-8")
        start_response(
            f"{status} {'OK' if 200 <= status < 300 else 'ERROR'}",
            [("Content-Type", "application/json; charset=utf-8"),
             ("Content-Length", str(len(data)))],
        )
        return [data]

    def _error(self, start_response: Callable, exc: MonitorError) -> list[bytes]:
        return self._json(start_response,
                          {"error": {"code": exc.code, "message": exc.message}},
                          exc.http_status)

    # ---------------------------------------------------------- handlers
    def _create_location(self, conn, p: Principal, body, q):
        require(p, "location.register")
        loc_id = report_service.register_location(
            conn,
            name=body["name"],
            lat=body.get("lat"),
            lng=body.get("lng"),
            place_kind=body.get("place_kind"),
        )
        return {"location_id": loc_id}

    def _submit_report(self, conn, p: Principal, body, q):
        require(p, "report.submit")
        return report_service.submit_report(conn, p, body)

    def _submit_batch(self, conn, p: Principal, body, q):
        require(p, "report.submit")
        return report_service.submit_batch(conn, p, body)

    def _get_batch(self, conn, p: Principal, body, q):
        require(p, "report.view")
        row = conn.execute("SELECT * FROM batches WHERE id=?", (q["id"],)).fetchone()
        if not row:
            from .errors import NotFound
            raise NotFound("批次不存在")
        result = dict(row)
        result["reports"] = [
            dict(r) for r in conn.execute(
                "SELECT id, case_id, agency_id, reported_at, location_id FROM reports "
                "WHERE batch_id=? ORDER BY reported_at", (q["id"],)
            ).fetchall()
        ]
        return result

    def _add_evidence(self, conn, p: Principal, body, q):
        level = body.get("level", cfg.EVIDENCE_SELF_REPORT)
        if level == cfg.EVIDENCE_CLINICIAN_OBS:
            require(p, "evidence.clinician")
        evidence_id = report_service.add_evidence(
            conn, p, q["id"],
            level=level,
            kind=body.get("kind", "note"),
            content=body["content"],
            verified=body.get("verified", False),
        )
        conn.commit()
        return {"evidence_id": evidence_id}

    def _verify_evidence(self, conn, p: Principal, body, q):
        require(p, "evidence.verify")
        report_service.verify_evidence(
            conn, p, q["id"], accept=bool(body.get("accept", True)), note=body.get("note")
        )
        return {"ok": True}

    def _merge_cases(self, conn, p: Principal, body, q):
        require(p, "event.review")  # 合并影响研判构成，由值班/流调执行
        report_service.merge_cases(conn, p, body["duplicate_id"], body["master_id"])
        return {"ok": True, "master_id": body["master_id"]}

    def _assign_handler(self, conn, p: Principal, body, q):
        require(p, "case.serve")
        assign_handler(conn, q["id"], body["user_id"], p)
        return {"ok": True}

    def _get_case(self, conn, p: Principal, body, q):
        require(p, "report.view")
        row = conn.execute("SELECT * FROM cases WHERE id=?", (q["id"],)).fetchone()
        if not row:
            from .errors import NotFound
            raise NotFound("病例不存在")
        want_identity = q.get("identity") in ("1", "true", "yes")
        return view_case(conn, p, row, identity_fields=want_identity)

    def _scan(self, conn, p: Principal, body, q):
        require(p, "event.view")
        return event_service.scan_events(conn, p)

    def _list_events(self, conn, p: Principal, body, q):
        require(p, "event.view")
        return {"events": event_service.list_events(conn, q.get("status"))}

    def _get_event(self, conn, p: Principal, body, q):
        require(p, "event.view")
        return event_service.get_event(conn, q["id"])

    def _review_event(self, conn, p: Principal, body, q):
        require(p, "event.review")
        event_service.review_event(
            conn, p, q["id"], decision=body["decision"], note=body.get("note")
        )
        return {"ok": True}

    def _resolve_fact(self, conn, p: Principal, body, q):
        require(p, "fact.check")
        event_service.resolve_fact_check(
            conn, p, q["id"], status=body["status"], note=body.get("note")
        )
        return {"ok": True}

    def _get_thresholds(self, conn, p: Principal, body, q):
        require(p, "threshold.view")
        return {"thresholds": dbmod.get_config(conn)}

    def _set_thresholds(self, conn, p: Principal, body, q):
        if p.role != cfg.ROLE_ADMIN:
            from .errors import PermissionDenied
            raise PermissionDenied("仅管理员可调整阈值")
        allowed = {
            "spatial_radius_m", "time_window_hours", "min_cases",
            "min_sources", "common_activity_min_cases",
        }
        thresholds = body.get("thresholds", body)
        for key, value in thresholds.items():
            if key not in allowed:
                from .errors import ValidationError
                raise ValidationError(f"未知阈值项: {key}")
            if key in ("min_cases", "min_sources", "common_activity_min_cases"):
                if not isinstance(value, int) or value < 1:
                    from .errors import ValidationError
                    raise ValidationError(f"{key} 必须是 ≥1 的整数")
            elif not isinstance(value, (int, float)) or value <= 0:
                from .errors import ValidationError
                raise ValidationError(f"{key} 必须是正数")
            dbmod.set_config_value(conn, key, value)
        from .security import audit
        audit(conn, p, "threshold.update", "config", None, {"values": thresholds})
        conn.commit()
        return {"thresholds": dbmod.get_config(conn)}

    def _add_misinfo(self, conn, p: Principal, body, q):
        if p.role != cfg.ROLE_ADMIN:
            from .errors import PermissionDenied
            raise PermissionDenied("仅管理员可登记错误说法")
        adv_service.add_misinfo_claim(conn, body["claim"], body["advice"])
        return {"ok": True}

    def _create_advisory(self, conn, p: Principal, body, q):
        require(p, "advisory.manage")
        aid = adv_service.create_advisory(
            conn, p,
            title=body["title"], area=body["area"],
            event_id=body.get("event_id"),
        )
        return {"advisory_id": aid, "_created": True}

    def _get_advisory(self, conn, p: Principal, body, q):
        require(p, "advisory.manage")
        return adv_service.get_advisory_full(conn, q["id"])

    def _create_version(self, conn, p: Principal, body, q):
        require(p, "advisory.manage")
        vid = adv_service.create_version(
            conn, p,
            advisory_id=q["id"],
            body=body["body"],
            medical_advice=body["medical_advice"],
            valid_until=body.get("valid_until"),
            change_reason=body.get("change_reason"),
        )
        return {"version_id": vid, "_created": True}

    def _publish_version(self, conn, p: Principal, body, q):
        require(p, "advisory.publish")
        adv_service.publish_version(conn, p, q["id"])
        return {"ok": True}

    def _downgrade(self, conn, p: Principal, body, q):
        require(p, "advisory.publish")
        vid = adv_service.downgrade_version(conn, p, q["id"], body.get("reason", "证据不足"))
        return {"new_version_id": vid, "status": cfg.ADVISORY_DOWNGRADED}

    def _withdraw(self, conn, p: Principal, body, q):
        require(p, "advisory.publish")
        vid = adv_service.withdraw_version(conn, p, q["id"], body.get("reason", "撤回"))
        return {"new_version_id": vid, "status": cfg.ADVISORY_WITHDRAWN}

    def _deliver(self, conn, p: Principal, body, q):
        require(p, "advisory.deliver")
        ids = adv_service.deliver(
            conn, p, version_id=q["id"], channel_ids=body["channel_ids"]
        )
        return {"delivery_ids": ids, "_created": True}

    def _expired(self, conn, p: Principal, body, q):
        require(p, "advisory.manage")
        return {"expired": adv_service.list_expired(conn)}

    def _open_correction(self, conn, p: Principal, body, q):
        require(p, "correction.open")
        result = adv_service.open_correction(
            conn, p,
            advisory_id=body["advisory_id"],
            bad_claim=body["bad_claim"],
            corrected_body=body["corrected_body"],
            corrected_medical_advice=body["corrected_medical_advice"],
            valid_until=body.get("valid_until"),
        )
        result["_created"] = True
        return result

    def _confirm_correction(self, conn, p: Principal, body, q):
        require(p, "correction.confirm")
        return adv_service.confirm_correction(
            conn, p, q["id"], channel_id=body["channel_id"], note=body.get("note")
        )

    def _get_correction(self, conn, p: Principal, body, q):
        require(p, "correction.open")
        return adv_service.get_correction(conn, q["id"])

    def _public_advisories(self, conn, p: Principal | None, body, q):
        return {"advisories": adv_service.public_listing(conn, area=q.get("area"))}


def create_app(db_path: str = ":memory:") -> App:
    return App(db_path)


def main() -> None:  # pragma: no cover
    import os
    from wsgiref.simple_server import make_server

    db_path = os.environ.get("MONITOR_DB", "monitor.db")
    app = create_app(db_path)
    host, port = os.environ.get("MONITOR_HOST", "127.0.0.1"), int(
        os.environ.get("MONITOR_PORT", "8080")
    )
    print(f"监测后端监听 http://{host}:{port} （数据库 {db_path}）")
    make_server(host, port, app).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
