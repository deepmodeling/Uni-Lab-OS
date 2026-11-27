"""摘要计算。

中心用 RFC 8785（JCS）规范化后再做 SHA-256，工站侧必须完全一致：摘要对不上时中心会
把 claim 判为"任务包已变更"并拒绝，而不是给一个含糊的错误。因此这里不自己拼 JSON，
而是用与中心同一个规范化实现。

摘要的作用不是校验完整性那么单纯：它同时是"这一份任务包"的标识，工站用它证明自己
领取的正是中心冻结的那一版输入。因此摘要计算必须对键序、空白、数字表示都不敏感，
否则同一份语义相同的数据会算出不同的摘要。
"""

from __future__ import annotations

import hashlib
from typing import Any

#: 中心与工站共用的摘要前缀。
DIGEST_PREFIX = "sha256:"


class DigestError(RuntimeError):
    """输入无法被规范化。"""


def canonical_json(value: Any) -> bytes:
    """按 RFC 8785 规范化。

    不能序列化的输入（例如 NaN、自定义对象）在这里就失败，而不是被静默替换成某个
    近似值——那会让两份不同的输入得到同一个摘要。
    """
    # 仅在规范化时加载实现；导入事务边界模块本身不初始化协议依赖。
    import rfc8785

    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError) as error:
        raise DigestError(f"无法规范化输入：{error}") from error


def digest(value: Any) -> str:
    """返回 ``sha256:<hex>`` 形式的摘要。"""
    return DIGEST_PREFIX + hashlib.sha256(canonical_json(value)).hexdigest()


def hex_digest(value: Any) -> str:
    """不带前缀的十六进制摘要，用于本地记录。"""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def is_digest(value: object) -> bool:
    """判断是否为合法摘要字符串。"""
    if not isinstance(value, str) or not value.startswith(DIGEST_PREFIX):
        return False
    suffix = value[len(DIGEST_PREFIX) :]
    return len(suffix) == 64 and all(c in "0123456789abcdef" for c in suffix)
