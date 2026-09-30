"""统一的领域/接口错误类型。"""
from __future__ import annotations


class MonitorError(Exception):
    """业务错误基类，携带对外可见的 code 与 http 状态。"""

    http_status = 400

    def __init__(self, message: str, *, code: str = "bad_request", http_status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        if http_status is not None:
            self.http_status = http_status


class AuthError(MonitorError):
    http_status = 401

    def __init__(self, message: str = "未认证", *, code: str = "unauthenticated"):
        super().__init__(message, code=code, http_status=401)


class PermissionDenied(MonitorError):
    http_status = 403

    def __init__(self, message: str = "无权访问", *, code: str = "forbidden"):
        super().__init__(message, code=code, http_status=403)


class NotFound(MonitorError):
    http_status = 404

    def __init__(self, message: str = "记录不存在", *, code: str = "not_found"):
        super().__init__(message, code=code, http_status=404)


class ConflictError(MonitorError):
    http_status = 409

    def __init__(self, message: str, *, code: str = "conflict"):
        super().__init__(message, code=code, http_status=409)


class ValidationError(MonitorError):
    http_status = 422

    def __init__(self, message: str, *, code: str = "validation_error"):
        super().__init__(message, code=code, http_status=422)
