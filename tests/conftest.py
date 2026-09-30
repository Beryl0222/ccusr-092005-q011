"""测试夹具：内存应用 + 极简 WSGI 客户端。"""
from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest

from monitor.http import create_app


class Client:
    def __init__(self, app):
        self.app = app
        self.conn = app.conn

    def request(self, method, path, *, user=None, body=None, query=""):
        if "?" in path and not query:
            path, query = path.split("?", 1)
        payload = b""
        environ_extra: dict[str, str] = {}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            environ_extra["CONTENT_TYPE"] = "application/json"
        environ_extra["CONTENT_LENGTH"] = str(len(payload))
        if user:
            environ_extra["HTTP_X_USER_ID"] = user
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "wsgi.input": io.BytesIO(payload),
            "wsgi.errors": sys.stderr,
            "SERVER_NAME": "test",
            "SERVER_PORT": "80",
            **environ_extra,
        }
        captured: dict[str, object] = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        raw = b"".join(self.app(environ, start_response))
        code = int(str(captured["status"]).split()[0])
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            data = None
        return SimpleNamespace(status_code=code, json=data, headers=captured["headers"])

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)


# 种子账号
HOSP = "u-reporter-hosp"
DOCTOR = "u-doctor"
SCHOOL = "u-reporter-school"
COMM = "u-reporter-comm"
DISPATCH = "u-dispatch"
INVEST = "u-invest"
ADMIN = "u-admin"

# 同一夜、河边 500 米内的三个地点
LOC_DORM = {"name": "河畔中学学生宿舍", "lat": 30.00050, "lng": 120.00000,
            "place_kind": "宿舍"}
LOC_PARK = {"name": "河滨公园亲水平台", "lat": 30.00000, "lng": 120.00000,
            "place_kind": "河边"}
LOC_TRAIN = {"name": "河畔中学操场（临河）", "lat": 30.00030, "lng": 120.00010,
             "place_kind": "操场"}


@pytest.fixture
def client():
    return Client(create_app(":memory:"))


def register_locations(c: Client) -> dict[str, str]:
    ids = {}
    for key, loc in (("dorm", LOC_DORM), ("park", LOC_PARK), ("train", LOC_TRAIN)):
        resp = c.post("/api/locations", user=COMM, body=loc)
        assert resp.status_code == 200, resp.json
        ids[key] = resp.json["location_id"]
    return ids
