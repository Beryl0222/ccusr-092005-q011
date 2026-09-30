"""stdlib HTTP API：把 MonitoringService 暴露为 JSON 接口。

仅依赖标准库，保证可复现。`dispatch` 是纯函数式路由，便于测试；
`python3 -m surveillance.api` 可启动真实服务。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .access import Viewer
from .models import EventStatus, Role
from .publications import PublicationError
from .service import MonitoringService, PermissionDenied, ServiceError


class Api:
    """把 HTTP 语义从业务中剥离：dispatch(method, path, body, headers) -> (status, json)。"""

    def __init__(self, service: MonitoringService) -> None:
        self.service = service
        self.routes: list[tuple[str, re.Pattern[str], Callable]] = [
            ("POST", re.compile(r"^/reports$"), self._post_report),
            ("POST", re.compile(r"^/batches$"), self._post_batch),
            ("POST", re.compile(r"^/reports/(?P<rid>[^/]+)/statements/(?P<sid>[^/]+)/verify$"), self._verify),
            ("POST", re.compile(r"^/detection/run$"), self._run_detection),
            ("GET", re.compile(r"^/events$"), self._list_events),
            ("GET", re.compile(r"^/events/(?P<eid>[^/]+)/alert$"), self._open_alert),
            ("POST", re.compile(r"^/events/(?P<eid>[^/]+)/handlers$"), self._assign_handler),
            ("POST", re.compile(r"^/events/(?P<eid>[^/]+)/status$"), self._set_status),
            ("POST", re.compile(r"^/events/(?P<eid>[^/]+)/material$"), self._draft_material),
            ("POST", re.compile(r"^/publications/(?P<pid>[^/]+)/publish$"), self._publish),
            ("POST", re.compile(r"^/series/(?P<sid>[^/]+)/downgrade$"), self._downgrade),
            ("POST", re.compile(r"^/series/(?P<sid>[^/]+)/retract$"), self._retract),
            ("GET", re.compile(r"^/series/(?P<sid>[^/]+)/dissemination$"), self._dissemination),
            ("POST", re.compile(r"^/corrections$"), self._correct),
            ("POST", re.compile(r"^/entrances$"), self._register_entrance),
            ("POST", re.compile(r"^/seed/import$"), self._import_seed),
        ]

    # ---- 基础设施 ------------------------------------------------------

    def _viewer(self, headers: dict[str, str]) -> Viewer:
        actor = headers.get("x-actor-id", "anonymous")
        try:
            role = Role(headers.get("x-actor-role", "reporter"))
        except ValueError:
            role = Role.REPORTER
        return Viewer(actor_id=actor, role=role)

    def dispatch(
        self, method: str, path: str, body: dict[str, Any] | None, headers: dict[str, str]
    ) -> tuple[int, Any]:
        viewer = self._viewer(headers)
        for verb, pattern, handler in self.routes:
            if verb != method:
                continue
            match = pattern.match(path)
            if match:
                try:
                    return 200, handler(body or {}, viewer, **match.groupdict())
                except PermissionDenied as exc:
                    return 403, {"error": str(exc)}
                except (ServiceError, PublicationError) as exc:
                    return 400, {"error": str(exc)}
                except KeyError as exc:
                    return 400, {"error": f"缺少字段：{exc}"}
        return 404, {"error": f"未知接口：{method} {path}"}

    # ---- 路由处理 ------------------------------------------------------

    def _post_report(self, body: dict, viewer: Viewer, **_: str) -> Any:
        return self.service.submit_report(body, viewer.actor_id)

    def _post_batch(self, body: dict, viewer: Viewer, **_: str) -> Any:
        submitted_at = body.get("submitted_at")
        return self.service.submit_batch(
            institution_id=body["institution_id"],
            batch_id=body["batch_id"],
            payloads=body.get("reports", []),
            actor=viewer.actor_id,
            submitted_at=datetime.fromisoformat(submitted_at) if submitted_at else None,
        )

    def _verify(self, body: dict, viewer: Viewer, rid: str, sid: str) -> Any:
        return self.service.verify_statement(rid, sid, viewer, body.get("note", ""))

    def _run_detection(self, body: dict, viewer: Viewer, **_: str) -> Any:
        now = datetime.fromisoformat(body["now"]) if body.get("now") else None
        events = self.service.run_detection(now)
        return {"events": [e.id for e in events]}

    def _list_events(self, body: dict, viewer: Viewer, **_: str) -> Any:
        return {
            "events": [
                {
                    "id": e.id,
                    "status": e.status.value,
                    "region": e.region,
                    "case_count": len(e.case_ids),
                    "hypothesis": e.hypothesis,
                }
                for e in self.service.store.events.values()
            ]
        }

    def _open_alert(self, body: dict, viewer: Viewer, eid: str) -> Any:
        return self.service.open_alert(eid, viewer)

    def _assign_handler(self, body: dict, viewer: Viewer, eid: str) -> Any:
        event = self.service.assign_handler(eid, body["handler_id"], viewer)
        return {"event_id": event.id, "handler_ids": event.handler_ids}

    def _set_status(self, body: dict, viewer: Viewer, eid: str) -> Any:
        event = self.service.set_event_status(
            eid, EventStatus(body["status"]), viewer, body.get("note", "")
        )
        return {"event_id": event.id, "status": event.status.value}

    def _draft_material(self, body: dict, viewer: Viewer, eid: str) -> Any:
        pub = self.service.draft_regional_material(eid, viewer)
        return {"publication_id": pub.id, "series_id": pub.series_id, "version": pub.version}

    def _publish(self, body: dict, viewer: Viewer, pid: str) -> Any:
        pub = self.service.publish(pid, viewer)
        return {"series_id": pub.series_id, "version": pub.version, "status": pub.status.value}

    def _downgrade(self, body: dict, viewer: Viewer, sid: str) -> Any:
        pub = self.service.downgrade(sid, body.get("reason", "证据不足"), viewer)
        return {"series_id": pub.series_id, "version": pub.version, "severity": pub.severity}

    def _retract(self, body: dict, viewer: Viewer, sid: str) -> Any:
        pub = self.service.retract(sid, body.get("reason", ""), viewer)
        return {"series_id": pub.series_id, "status": pub.status.value}

    def _dissemination(self, body: dict, viewer: Viewer, sid: str) -> Any:
        return self.service.publications.dissemination_status(sid)

    def _correct(self, body: dict, viewer: Viewer, **_: str) -> Any:
        pub = self.service.correct_misinformation(
            body["lead_id"], title=body["title"], content=body["content"], viewer=viewer
        )
        return {"series_id": pub.series_id, "version": pub.version, "status": pub.status.value}

    def _register_entrance(self, body: dict, viewer: Viewer, **_: str) -> Any:
        entrance = self.service.publications.register_entrance(
            body["id"], body["region"], body.get("label", body["id"])
        )
        return {"entrance_id": entrance.id, "region": entrance.region}

    def _import_seed(self, body: dict, viewer: Viewer, **_: str) -> Any:
        return self.service.import_seed(body, viewer.actor_id)


def make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                self._respond(400, {"error": "请求体不是合法 JSON"})
                return
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload = api.dispatch(self.command, self.path.split("?")[0], body, headers)
            self._respond(status, payload)

        do_GET = _handle
        do_POST = _handle

        def _respond(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: Any) -> None:  # 静默
            return

    return Handler


def serve(service: MonitoringService, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(Api(service)))
    print(f"公共卫生事件监测后端已启动：http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    serve(MonitoringService())
