"""认证、授权与儿童身份信息保护。

- 演示环境用固定用户表（X-User-Id 头），生产应替换为真实鉴权中间件。
- 病例的身份字段（年龄、脱敏化名以外的可识别信息）默认不可见；
  儿童病例仅 case_handlers 名单内的"实际处置者"可读，
  每次访问写 audit_log，拒绝访问同样留痕。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import config
from .db import now_iso
from .errors import AuthError, PermissionDenied

# 各角色可执行动作
_PERMISSIONS: dict[str, set[str]] = {
    config.ROLE_REPORTER: {
        "report.submit", "report.attach_evidence", "location.register", "report.view",
    },
    config.ROLE_CLINICIAN: {
        "report.submit", "report.attach_evidence", "location.register",
        "evidence.clinician", "case.view_identity", "case.serve", "report.view",
    },
    config.ROLE_DISPATCHER: {
        "event.view", "event.review", "advisory.manage", "advisory.publish",
        "advisory.deliver", "correction.open", "correction.confirm",
        "threshold.view", "report.view",
    },
    config.ROLE_INVESTIGATOR: {
        "report.view", "event.view", "event.review",
        "evidence.verify", "fact.check", "case.serve", "case.view_identity",
    },
    config.ROLE_ADMIN: {"*"},
}


@dataclass(frozen=True)
class Principal:
    id: str
    role: str
    agency_id: str | None
    name: str


def load_principal(conn: sqlite3.Connection, user_id: str | None) -> Principal:
    if not user_id:
        raise AuthError("缺少 X-User-Id")
    row = conn.execute(
        "SELECT id, role, agency_id, name FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if row is None:
        raise AuthError("用户不存在", code="unknown_user")
    return Principal(row["id"], row["role"], row["agency_id"], row["name"])


def require(principal: Principal, action: str) -> None:
    perms = _PERMISSIONS.get(principal.role, set())
    if "*" in perms or action in perms:
        return
    raise PermissionDenied(f"角色 {principal.role} 无权执行 {action}")


def can(principal: Principal, action: str) -> bool:
    return _can(principal, action)


def audit(
    conn: sqlite3.Connection,
    principal: Principal | None,
    action: str,
    entity: str,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, user_id, action, entity, entity_id, detail) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            principal.id if principal else None,
            action,
            entity,
            entity_id,
            None if detail is None else __import__("json").dumps(detail, ensure_ascii=False),
        ),
    )
    conn.commit()


def is_handler(conn: sqlite3.Connection, case_id: str, user_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM case_handlers WHERE case_id=? AND user_id=?",
        (case_id, user_id),
    ).fetchone() is not None


def assign_handler(
    conn: sqlite3.Connection, case_id: str, user_id: str, granted_by: Principal
) -> None:
    """登记实际处置者（接诊医护/到场流调），登记后才可看该病例身份信息。"""
    conn.execute(
        "INSERT OR IGNORE INTO case_handlers(case_id, user_id, granted_at, granted_by) "
        "VALUES(?, ?, ?, ?)",
        (case_id, user_id, now_iso(), granted_by.id),
    )
    audit(conn, granted_by, "case.grant_handler", "case", case_id, {"grantee": user_id})
    conn.commit()


def view_case(
    conn: sqlite3.Connection,
    principal: Principal,
    case_row: sqlite3.Row,
    *,
    identity_fields: bool,
) -> dict[str, Any]:
    """按授权裁剪病例视图。

    identity_fields=True 表示调用方需要身份字段；若病例是儿童且访问者
    不是实际处置者，抛 PermissionDenied（并留痕）。成人病例要求具备
    case.view_identity 权限且为处置者（流调/医护场景一致处理）。
    """
    case_id = case_row["id"]
    minor = bool(case_row["is_minor"])
    data = {
        "case_id": case_id,
        "pseudonym": case_row["pseudonym"],
        "is_minor": minor,
    }
    if not identity_fields:
        return data

    # 处置者名单是"实际处置者"授权的唯一依据；名单仅可由具备
    # case.serve 的角色（接诊医护/到场流调）写入，不能自行登记。
    handler = is_handler(conn, case_id, principal.id)
    if not handler:
        audit(
            conn, principal, "case.identity_denied", "case", case_id,
            {"is_minor": minor, "reason": "not_handler"},
        )
        conn.commit()
        raise PermissionDenied(
            "儿童信息仅向实际处置者开放" if minor else "非该病例处置者，不可查看身份信息",
            code="minor_protected" if minor else "identity_protected",
        )
    data.update({"age": case_row["age"], "gender": case_row["gender"]})
    audit(conn, principal, "case.identity_view", "case", case_id, {"is_minor": minor})
    return data


def _can(principal: Principal, action: str) -> bool:
    perms = _PERMISSIONS.get(principal.role, set())
    return "*" in perms or action in perms
