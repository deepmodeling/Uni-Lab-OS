"""微后端持久化记录共用的严格 Pydantic 配置。"""

from __future__ import annotations

from typing import Annotated, Dict

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
UnixMilliseconds = Annotated[int, Field(ge=0)]
PositiveVersion = Annotated[int, Field(ge=1)]
JsonObject = Dict[str, JsonValue]


class ServerObject(BaseModel):
    """后端表记录的规范对象；未知字段必须在边界处显式处理。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
        protected_namespaces=(),
    )


class SchemaMigrationRecord(ServerObject):
    """每个 SQLite 文件自己的迁移记录。"""

    database_key: NonEmptyStr
    version: PositiveVersion
    name: NonEmptyStr
    checksum: NonEmptyStr
    applied_at_ms: UnixMilliseconds


__all__ = [
    "JsonObject",
    "NonEmptyStr",
    "PositiveVersion",
    "SchemaMigrationRecord",
    "ServerObject",
    "UnixMilliseconds",
]
