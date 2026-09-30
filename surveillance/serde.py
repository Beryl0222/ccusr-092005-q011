"""通用序列化：dataclass <-> JSON 友好的 dict。

支持 datetime（ISO8601）、Enum（取值）、set（排序列表）、嵌套 dataclass、
Optional / list / set / dict 组合。保持显式、可解释，不依赖第三方库。
"""

from __future__ import annotations

import types
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints


def encode(value: Any) -> Any:
    """把领域对象递归转成 JSON 可序列化结构。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(encode(v) for v in value)
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    return value


def _decode_scalar(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    if tp is datetime:
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if isinstance(tp, type) and is_dataclass(tp):
        return decode(tp, value)
    return value


def _decode(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if value is None:
            return None
        # 依次尝试各候选类型，取第一个不抛异常的解码结果
        last_err: Exception | None = None
        for arg in args:
            try:
                return _decode(arg, value)
            except Exception as exc:  # noqa: BLE001 - 尝试下一个候选
                last_err = exc
        raise last_err if last_err else ValueError(f"无法解码 {tp}")
    if origin in (list, tuple):
        (item_tp,) = get_args(tp) or (Any,)
        return [_decode(item_tp, v) for v in value]
    if origin in (set, frozenset):
        (item_tp,) = get_args(tp) or (Any,)
        return {_decode(item_tp, v) for v in value}
    if origin is dict:
        key_tp, val_tp = get_args(tp) or (str, Any)
        return {_decode(key_tp, k): _decode(val_tp, v) for k, v in value.items()}
    return _decode_scalar(tp, value)


def decode(cls: type, data: dict) -> Any:
    """按类型标注把 dict 还原为 dataclass 实例。"""
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = _decode(hints[f.name], data[f.name])
    return cls(**kwargs)
