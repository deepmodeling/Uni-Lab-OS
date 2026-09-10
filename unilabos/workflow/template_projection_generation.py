"""设备注册表模板投影代际的 OS 私有 SQLite 持久化。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from unilabos.workflow.store import WorkflowStore


class TemplateProjectionGenerationDataError(ValueError):
    """持久化的模板投影代际或资源身份映射已损坏。"""


@dataclass(frozen=True, slots=True)
class RegistryTemplateProjectionGeneration:
    """一个已原子提交的设备注册表模板投影代际。

    字段说明：``authority_id`` 标识投影权威（Authority）；``generation`` 是本地
    单调代际号；``node_templates`` 与 ``handle_templates`` 是活动节点和连接点
    （Handle）模板全集；``resource_template_symbols`` 是资源模板
    （ResourceTemplate）源码身份到 UUID 的完整映射。
    """

    authority_id: str
    generation: int
    node_templates: list[dict[str, Any]]
    handle_templates: list[dict[str, Any]]
    resource_template_symbols: dict[str, str]


class RegistryTemplateGenerationPersistence:
    """封装 OS 私有投影代际表、序列化与单调推进规则。"""

    def __init__(self, workflow_store: WorkflowStore) -> None:
        """绑定工作流存储并建立私有代际表。

        参数说明：``workflow_store`` 持有模板表共用的 SQLite 连接与事务锁。返回：
        无；本模块不创建第二个数据库。异常：SQLite 初始化失败原样传播，调用方
        必须关闭式失败。
        """

        self._workflow_store = workflow_store
        self._initialize_schema()

    def normalize_symbols(self, raw_mapping: Any) -> dict[str, str]:
        """分离并验证资源模板源码身份映射的持久化形状。

        参数说明：``raw_mapping`` 是编译结果或 SQLite JSON 解码值。返回：按源码
        身份稳定排序的新字典；非对象、空身份、非字符串 UUID 或多个源码身份复用
        同一 UUID 时抛出 ``TypeError`` 或 ``ValueError``。本函数只保护持久化
        形状，Python 源码身份与 UUID 领域规则由工作流创作目录
        （Authoring Catalog）校验。
        """

        if not isinstance(raw_mapping, Mapping):
            raise TypeError("资源模板源码身份映射必须是对象")
        normalized: dict[str, str] = {}
        seen_uuids: set[str] = set()
        for raw_symbol, raw_uuid in sorted(
            raw_mapping.items(),
            key=lambda item: str(item[0]),
        ):
            if not isinstance(raw_symbol, str) or not raw_symbol:
                raise ValueError("资源模板源码身份不能为空")
            if not isinstance(raw_uuid, str) or not raw_uuid:
                raise ValueError("资源模板源码身份必须映射到 UUID 字符串")
            if raw_uuid in seen_uuids:
                raise ValueError("资源模板 UUID 不得绑定多个源码身份")
            normalized[raw_symbol] = raw_uuid
            seen_uuids.add(raw_uuid)
        return normalized

    def upsert_in_transaction(
        self,
        connection: Any,
        *,
        authority_id: str,
        resource_template_symbols: Mapping[str, str],
        now: str,
    ) -> int:
        """在模板替换事务内推进并保存完整资源身份代际。

        参数说明：``connection`` 是模板替换的唯一写事务；``authority_id`` 是
        投影来源；``resource_template_symbols`` 是已规范化的完整资源模板
        （ResourceTemplate）源码身份映射；``now`` 是统一事务时间。返回：首次为
        ``1``、以后单调递增的代际号。异常：SQLite 写入冲突原样传播，由外层
        模板替换事务统一回滚。
        """

        row = connection.execute(
            """
            SELECT generation
            FROM registry_template_projection_generation
            WHERE authority_id = ?
            """,
            (authority_id,),
        ).fetchone()
        next_generation = 1 if row is None else int(row["generation"]) + 1
        connection.execute(
            """
            INSERT INTO registry_template_projection_generation(
                authority_id, generation, resource_template_symbols,
                create_time, update_time
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(authority_id) DO UPDATE SET
                generation = excluded.generation,
                resource_template_symbols = excluded.resource_template_symbols,
                update_time = excluded.update_time
            """,
            (
                authority_id,
                next_generation,
                _stable_json(resource_template_symbols),
                now,
                now,
            ),
        )
        return next_generation

    def load_in_transaction(
        self,
        connection: Any,
        *,
        authority_id: str,
    ) -> tuple[int, dict[str, str]]:
        """在模板读取事务内恢复代际号和完整资源身份映射。

        参数说明：``connection`` 是节点、连接点（Handle）共用的读取事务；
        ``authority_id`` 限定投影来源。返回：代际号和分离的资源模板
        （ResourceTemplate）源码身份映射；尚未发布时返回 ``(0, {})``。异常：
        JSON 或映射形状损坏时抛出 ``TemplateProjectionGenerationDataError``。
        """

        row = connection.execute(
            """
            SELECT generation, resource_template_symbols
            FROM registry_template_projection_generation
            WHERE authority_id = ?
            """,
            (authority_id,),
        ).fetchone()
        if row is None:
            return 0, {}
        try:
            raw_symbols = json.loads(row["resource_template_symbols"])
            symbols = self.normalize_symbols(raw_symbols)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise TemplateProjectionGenerationDataError(
                "资源模板源码身份投影已损坏"
            ) from error
        return int(row["generation"]), symbols

    def _initialize_schema(self) -> None:
        """建立设备侧私有的设备注册表模板投影代际表。

        参数：无。返回：无；表只保存 OS 本地投影权威、代际与资源模板
        （ResourceTemplate）源码身份映射，不改变后端（Backend）共享逻辑表。
        异常：SQLite 建表失败原样传播，调用方不得继续使用不完整投影。
        """

        with self._workflow_store.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS registry_template_projection_generation (
                    authority_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL CHECK(generation >= 1),
                    resource_template_symbols TEXT NOT NULL,
                    create_time TEXT NOT NULL,
                    update_time TEXT NOT NULL
                )
                """
            )


def _stable_json(value: Any) -> str:
    """把 OS 私有代际值编码为稳定 JSON 文本。

    参数说明：``value`` 是资源模板（ResourceTemplate）源码身份映射。返回：键
    排序、无 NaN 且无非必要空白的 UTF-8 JSON 文本；非法 JSON 值抛出标准编码
    异常并由外层事务回滚。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "RegistryTemplateGenerationPersistence",
    "RegistryTemplateProjectionGeneration",
    "TemplateProjectionGenerationDataError",
]
