"""Registry Authority 四表的 SQLModel 表记录与建表规格（落 ``runtime.db``）。

表规格经 ``tables/runtime.py`` 聚合进 ``RUNTIME_DATABASE``，不存在独立的
registry 数据库。Registry Authority 采用条目级版本模型（上报替换）：

- ``registry_entry``：每个模板条目的每个版本一行完整 copy（不可变），
  任何字段变化都会为该条目自增一个新版本；
- ``registry_entry_state``：每个条目一行可变状态——当前生效版本
  （active）、待确认版本（pending：不满足自动接受规则的新增 / 变化，或待确认的
  移除，由前端确认或忽略）、生效内容所属驱动包版本（规则 A 的基准）、软移除与不可用标记；
- ``registry_report``：每次 Edge 上报的批次统计（新增/更新/挂起/移除/
  不可用），供前端展示与审计；
- ``registry_remote_slave``：Host 报来的、不在权威这台机器上的 Slave。只要有一行，
  规则 B（单机部署全部自动接受）就不成立；用户确认不再使用后经 API 删除。
"""

from __future__ import annotations

from typing import ClassVar, Optional

from sqlmodel import Field

from unilabos.protocol.base import JsonArray
from unilabos.server.database.schema import (
    SCHEMA_IDENTITY_TABLE,
    TableSpec,
)
from unilabos.server.database.tables.base import (
    JsonObject,
    NonEmptyStr,
    TableObject,
    json_text_column,
)


class RegistryEntryRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "registry_entry"

    name: NonEmptyStr = Field(primary_key=True)
    version: int = Field(primary_key=True, ge=1)
    created_at_ms: int = Field(ge=0)
    # edge-report：Edge 上报；restore：从历史版本还原
    source: NonEmptyStr
    edge_uuid: str = ""
    restored_from: Optional[int] = Field(default=None, ge=1)
    content_sha256: NonEmptyStr
    # 上报这个版本时条目所属的驱动包与包版本（不进内容哈希）
    package_name: str = ""
    package_version: str = ""
    payload: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("payload", default_json="{}"),
    )


class RegistryEntryStateRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "registry_entry_state"

    name: NonEmptyStr = Field(primary_key=True)
    template_uuid: NonEmptyStr
    active_version: Optional[int] = Field(default=None, ge=1)
    pending_version: Optional[int] = Field(default=None, ge=1)
    pending_conflicts: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("pending_conflicts", default_json="[]"),
    )
    unusable_reason: str = ""
    removed_at_ms: Optional[int] = Field(default=None, ge=0)
    updated_at_ms: int = Field(ge=0)
    # 生效内容最近一次被上报时所属的驱动包与包版本：规则 A 拿新上报的包版本与它比较
    package_name: str = ""
    package_version: str = ""
    # 挂起的原因（cross-machine-reporter / cross-machine-slave）；没有挂起时为空
    pending_reason: str = ""
    # Host 不再上报该条目、移除待确认的时刻；确认后软移除，忽略或再次上报即清除
    pending_removal_at_ms: Optional[int] = Field(default=None, ge=0)


class RegistryReportRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "registry_report"

    report_id: Optional[int] = Field(default=None, ge=1, primary_key=True)
    created_at_ms: int = Field(ge=0)
    edge_uuid: str = ""
    summary: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("summary", default_json="{}"),
    )


class RegistryRemoteSlaveRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "registry_remote_slave"

    machine_name: NonEmptyStr = Field(primary_key=True)
    # Slave 自报的机器标识；旧 Slave 没有时为空（同样算跨机器）
    machine_id: str = ""
    # 经由哪个 Host 报来（上报的 edge_uuid）
    edge_uuid: str = ""
    first_seen_at_ms: int = Field(ge=0)
    last_seen_at_ms: int = Field(ge=0)


REGISTRY_TABLE_MODELS = (
    RegistryEntryRecord,
    RegistryEntryStateRecord,
    RegistryReportRecord,
    RegistryRemoteSlaveRecord,
)


REGISTRY_TABLES = (
    SCHEMA_IDENTITY_TABLE,
    TableSpec(
        "registry_entry",
        """
        CREATE TABLE IF NOT EXISTS registry_entry (
            name TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 1),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            source TEXT NOT NULL,
            edge_uuid TEXT NOT NULL DEFAULT '',
            restored_from INTEGER CHECK (restored_from >= 1),
            content_sha256 TEXT NOT NULL,
            package_name TEXT NOT NULL DEFAULT '',
            package_version TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL CHECK (
                json_valid(payload) AND json_type(payload) = 'object'
            ),
            PRIMARY KEY (name, version)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_registry_entry_created
            ON registry_entry(created_at_ms DESC)
            """,
        ),
    ),
    TableSpec(
        "registry_entry_state",
        """
        CREATE TABLE IF NOT EXISTS registry_entry_state (
            name TEXT PRIMARY KEY,
            template_uuid TEXT NOT NULL,
            active_version INTEGER CHECK (active_version >= 1),
            pending_version INTEGER CHECK (pending_version >= 1),
            pending_conflicts TEXT NOT NULL CHECK (
                json_valid(pending_conflicts)
                AND json_type(pending_conflicts) = 'array'
            ),
            unusable_reason TEXT NOT NULL DEFAULT '',
            removed_at_ms INTEGER CHECK (removed_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
            package_name TEXT NOT NULL DEFAULT '',
            package_version TEXT NOT NULL DEFAULT '',
            pending_reason TEXT NOT NULL DEFAULT '',
            pending_removal_at_ms INTEGER CHECK (pending_removal_at_ms >= 0)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_registry_entry_state_pending
            ON registry_entry_state(name)
            WHERE pending_version IS NOT NULL OR pending_removal_at_ms IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "registry_report",
        """
        CREATE TABLE IF NOT EXISTS registry_report (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            edge_uuid TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL CHECK (
                json_valid(summary) AND json_type(summary) = 'object'
            )
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_registry_report_created
            ON registry_report(created_at_ms DESC, report_id DESC)
            """,
        ),
    ),
    TableSpec(
        "registry_remote_slave",
        """
        CREATE TABLE IF NOT EXISTS registry_remote_slave (
            machine_name TEXT PRIMARY KEY CHECK (TRIM(machine_name) <> ''),
            machine_id TEXT NOT NULL DEFAULT '',
            edge_uuid TEXT NOT NULL DEFAULT '',
            first_seen_at_ms INTEGER NOT NULL CHECK (first_seen_at_ms >= 0),
            last_seen_at_ms INTEGER NOT NULL CHECK (last_seen_at_ms >= 0)
        )
        """,
        (),
    ),
)


__all__ = [
    "REGISTRY_TABLES",
    "REGISTRY_TABLE_MODELS",
    "RegistryEntryRecord",
    "RegistryEntryStateRecord",
    "RegistryRemoteSlaveRecord",
    "RegistryReportRecord",
]
