"""微后端 SQLModel 表的严格配置；共享标量与 DTO 基类来自协议层。"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Text, text
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

from unilabos.protocol.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    UnixMilliseconds,
)


_VALIDATING_TABLES: ContextVar[frozenset[type[SQLModel]]] = ContextVar(
    "unilabos_validating_sqlmodel_tables",
    default=frozenset(),
)


class JsonText(TypeDecorator[Any]):
    """SQLite TEXT JSON 列；ORM 使用时自动编码/解码，原始 sqlite3 仍兼容。"""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> str | None:
        if value is None:
            return None

        def default(item: Any) -> Any:
            if isinstance(item, BaseModel):
                return item.model_dump(mode="json")
            raise TypeError(f"{type(item).__name__} is not JSON serializable")

        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=default)

    def process_result_value(self, value: Any, dialect) -> Any:
        if value is None or not isinstance(value, str):
            return value
        return json.loads(value)


def json_text_column(
    name: str,
    *,
    default_json: str,
    nullable: bool = False,
) -> Column[Any]:
    """声明与 v1 ``*_json TEXT`` 兼容的 SQLModel 列。"""

    return Column(
        name,
        JsonText(),
        nullable=nullable,
        server_default=None if nullable else text(f"'{default_json}'"),
    )


class TableObject(SQLModel):
    """SQLModel 表基类；ORM 分字段 hydration 时不执行半成品赋值校验。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=False,
        validate_default=True,
        allow_inf_nan=False,
        protected_namespaces=(),
    )

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> "TableObject":
        """让 SQLModel 创建 ORM 空壳时跳过自定义构造校验的递归入口。"""

        active = _VALIDATING_TABLES.get()
        token = _VALIDATING_TABLES.set(active | {cls})
        try:
            return super().model_validate(obj, **kwargs)
        finally:
            _VALIDATING_TABLES.reset(token)

    def __init__(self, **data: Any) -> None:
        """恢复 table=True 模型的严格 Pydantic 构造校验并保留 ORM state。"""

        table = getattr(type(self), "__table__", None)
        if table is None:
            super().__init__(**data)
            return
        active = _VALIDATING_TABLES.get()
        if type(self) in active:
            super().__init__(**data)
            return
        token = _VALIDATING_TABLES.set(active | {type(self)})
        try:
            validated = type(self).model_validate(data)
        finally:
            _VALIDATING_TABLES.reset(token)
        values = {
            name: getattr(validated, name)
            for name in type(self).model_fields
            if hasattr(validated, name)
        }
        super().__init__(**values)


class SchemaIdentityRecord(TableObject, table=True):
    """每个 SQLite 文件自己的 schema 身份记录。"""

    __tablename__: ClassVar[str] = "schema_identity"

    database_key: NonEmptyStr = Field(primary_key=True)
    checksum: NonEmptyStr
    applied_at_ms: UnixMilliseconds


__all__ = [
    "JsonObject",
    "JsonText",
    "NonEmptyStr",
    "PositiveVersion",
    "SchemaIdentityRecord",
    "ServerObject",
    "TableObject",
    "UnixMilliseconds",
    "json_text_column",
]
