"""协议层基础件：严格 DTO 基类、共享标量类型与规范 JSON 工具。

协议对象与微后端 SQLModel 表共用这些定义；表侧经
``unilabos.server.database.tables.base`` 转发引用，保证两侧约束一致。
本模块不依赖 ``unilabos.server``，契约层可独立加载。
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Dict, List

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints
from sqlalchemy import Integer, Text
from sqlmodel import Field


NonEmptyStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    Field(sa_type=Text),
]
UnixMilliseconds = Annotated[int, Field(ge=0, sa_type=Integer)]
PositiveVersion = Annotated[int, Field(ge=1, sa_type=Integer)]


def _ensure_json_compatible(value: Any) -> Any:
    """JSON 兼容性探针：一次 C 层 ``json.dumps``，代替 pydantic ``JsonValue`` 逐节点的 Python 递归校验。

    注册表模板 definition 全量有十几 MB；按 ``JsonValue`` 校验一遍要秒级（每个节点都进
    Python 的 tagged-union 判别），启动时同步 130 个模板就要花掉数秒，前端每隔几秒轮询
    一次模板列表也是同样的开销。``json.dumps`` 在几十毫秒内给出同样的判定：只允许 JSON
    标量 / 列表 / 对象，拒绝 NaN / Inf 与不可序列化对象。

    与 ``JsonValue`` 的差别：嵌套字符串不再被模型的 ``str_strip_whitespace`` 顺带修剪
    （JSON 载荷里的字面值应原样保存），以及嵌套对象的非字符串键会被序列化为字符串而不是报错。
    """

    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not JSON-compatible: {exc}") from None
    return value


JsonObject = Annotated[Dict[str, Any], AfterValidator(_ensure_json_compatible)]
JsonArray = Annotated[List[Any], AfterValidator(_ensure_json_compatible)]


class ServerObject(BaseModel):
    """协议 DTO 和内嵌值对象的严格 Pydantic 基类。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
        protected_namespaces=(),
    )


def canonical_json(value: Any) -> str:
    """返回跨进程稳定的 JSON；哈希、幂等请求和快照均使用这一实现。"""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "JsonArray",
    "JsonObject",
    "NonEmptyStr",
    "PositiveVersion",
    "ServerObject",
    "UnixMilliseconds",
    "canonical_hash",
    "canonical_json",
]
