"""脱敏身份：用 HMAC 把原始身份映射为不可逆令牌。

机构侧或平台侧均可调用；后端只保存令牌，不保存原始身份。
"""

from __future__ import annotations

import hashlib
import hmac


def pseudonymize(raw_id: str, salt: str) -> str:
    """同一 (raw_id, salt) 恒定得到同一令牌，便于跨机构去重。

    各机构共享项目盐值时，同一个人在不同机构会得到相同令牌，
    从而支持跨机构去重；盐值不外泄即可保证令牌不可反推。
    """
    if not raw_id or not raw_id.strip():
        raise ValueError("原始身份不能为空")
    digest = hmac.new(salt.encode("utf-8"), raw_id.strip().encode("utf-8"), hashlib.sha256)
    return "tok_" + digest.hexdigest()[:24]
