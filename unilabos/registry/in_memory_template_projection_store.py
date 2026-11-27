"""设备与动作模板完整代际的进程内原子存储。"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import UUID

from unilabos.registry.template_delta import (
    TemplateProjectionDelta,
    TemplateProjectionDeltaError,
    TemplateProjectionMember,
    build_template_projection_delta,
    semantic_template_hash,
    stable_projection_key,
)
from unilabos.registry.template_identity import (
    TemplateIdentityError,
    action_handle_uuid,
)
from unilabos.workflow.template_projection_generation import (
    RegistryTemplateProjectionGeneration,
)
from unilabos.workflow.template_projection_store import (
    TemplateProjectionIdentityConflict,
)

_SYNTHETIC_TIME = "1970-01-01T00:00:00Z"


class InMemoryRegistryTemplateProjectionStore:
    """只在进程内保存最近一次完整、已验证的模板目录代际。"""

    def __init__(self) -> None:
        """建立空代际；构造过程不访问文件系统或数据库。"""

        self._generation = 0
        self._node_templates: list[dict[str, Any]] = []
        self._handle_templates: list[dict[str, Any]] = []
        self._resource_template_symbols: dict[str, str] = {}
        self._members: tuple[TemplateProjectionMember, ...] = ()

    def load_generation(
        self,
        *,
        authority_id: str,
    ) -> RegistryTemplateProjectionGeneration:
        """返回当前进程内已发布代际的分离副本。

        参数：``authority_id`` 必须与调用投影权威一致。返回：完整节点、Handle、
        资源模板源码身份及单调代际；空目录返回代际零。
        """

        self._validate_authority(authority_id)
        return RegistryTemplateProjectionGeneration(
            authority_id=authority_id,
            generation=self._generation,
            node_templates=copy.deepcopy(self._node_templates),
            handle_templates=copy.deepcopy(self._handle_templates),
            resource_template_symbols=dict(self._resource_template_symbols),
        )

    def replace_generation_with_delta(
        self,
        *,
        authority_id: str,
        node_templates: Sequence[Mapping[str, Any]],
        handle_templates: Sequence[Mapping[str, Any]],
        resource_template_symbols: Mapping[str, str],
        validate_generation: (
            Callable[[RegistryTemplateProjectionGeneration], None] | None
        ) = None,
    ) -> tuple[RegistryTemplateProjectionGeneration, TemplateProjectionDelta]:
        """完整校验候选后一次替换内存代际。

        参数：三个候选集合描述本轮完整目录；``validate_generation`` 在发布前读取
        分离候选。返回：已发布代际与差量。异常：任何身份、引用或目录校验失败时
        保留上一代，禁止发布部分结果。
        """

        self._validate_authority(authority_id)
        symbols = self._normalize_symbols(resource_template_symbols)
        nodes, node_members, node_uuid_by_key = self._normalize_nodes(
            authority_id,
            node_templates,
        )
        handles, handle_members = self._normalize_handles(
            authority_id,
            handle_templates,
            node_uuid_by_key=node_uuid_by_key,
            nodes_by_uuid={str(node["uuid"]): node for node in nodes},
        )
        symbol_members = tuple(
            TemplateProjectionMember(
                authority_id=authority_id,
                projection_kind="resource_template_symbol",
                source_definition_key=source_symbol,
                business_key=stable_projection_key((source_symbol,)),
                target_uuid=template_uuid,
                semantic_hash=semantic_template_hash(
                    "resource_template_symbol",
                    template_uuid,
                ),
                generation=0,
            )
            for source_symbol, template_uuid in symbols.items()
        )
        candidate_members = (*node_members, *handle_members, *symbol_members)
        try:
            delta = build_template_projection_delta(
                authority_id=authority_id,
                current_generation=self._generation,
                previous_members=self._members,
                candidate_members=candidate_members,
            )
        except TemplateProjectionDeltaError as error:
            raise TemplateProjectionIdentityConflict(str(error)) from error
        candidate = RegistryTemplateProjectionGeneration(
            authority_id=authority_id,
            generation=delta.generation,
            node_templates=copy.deepcopy(nodes),
            handle_templates=copy.deepcopy(handles),
            resource_template_symbols=dict(symbols),
        )
        if validate_generation is not None:
            validate_generation(candidate)
        # 所有规范化、差量和完整目录校验成功后才替换公开状态。
        self._generation = delta.generation
        self._node_templates = nodes
        self._handle_templates = handles
        self._resource_template_symbols = symbols
        self._members = delta.active_members
        return self.load_generation(authority_id=authority_id), delta

    def close(self) -> None:
        """释放内存目录所有权；本实现没有外部资源需要关闭。"""

    @staticmethod
    def _validate_authority(authority_id: str) -> None:
        """校验非空投影权威。"""

        if not isinstance(authority_id, str) or not authority_id.strip():
            raise ValueError("模板投影权威不能为空")

    @staticmethod
    def _normalize_symbols(raw: Mapping[str, str]) -> dict[str, str]:
        """规范资源模板源码身份映射并拒绝 UUID 复用。"""

        if not isinstance(raw, Mapping):
            raise TypeError("资源模板源码身份映射必须是对象")
        result: dict[str, str] = {}
        seen_uuids: set[str] = set()
        for symbol, template_uuid in sorted(raw.items()):
            if not isinstance(symbol, str) or not symbol:
                raise TemplateProjectionIdentityConflict("资源模板源码身份不能为空")
            try:
                normalized_uuid = str(UUID(template_uuid))
            except (AttributeError, TypeError, ValueError):
                raise TemplateProjectionIdentityConflict(
                    "资源模板源码身份必须映射到合法 UUID"
                ) from None
            if normalized_uuid in seen_uuids:
                raise TemplateProjectionIdentityConflict(
                    "资源模板 UUID 不得绑定多个源码身份"
                )
            result[symbol] = normalized_uuid
            seen_uuids.add(normalized_uuid)
        return result

    @staticmethod
    def _normalize_nodes(
        authority_id: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        tuple[TemplateProjectionMember, ...],
        dict[tuple[str, str], str],
    ]:
        """规范节点全集并建立业务键到显式确定性 UUID 的唯一索引。"""

        nodes: list[dict[str, Any]] = []
        members: list[TemplateProjectionMember] = []
        uuid_by_key: dict[tuple[str, str], str] = {}
        seen_uuids: set[str] = set()
        for raw in candidates:
            candidate = copy.deepcopy(dict(raw))
            resource_uuid = candidate.get("resource_template_uuid")
            name = candidate.get("name")
            if not isinstance(resource_uuid, str) or not resource_uuid:
                raise TemplateProjectionIdentityConflict("节点模板缺少资源模板 UUID")
            if not isinstance(name, str) or not name:
                raise TemplateProjectionIdentityConflict("节点模板缺少动作业务名")
            business_key = (resource_uuid, name)
            if business_key in uuid_by_key:
                raise TemplateProjectionIdentityConflict(
                    "完整模板投影包含重复节点活动业务唯一键"
                )
            try:
                template_uuid = str(UUID(candidate["uuid"]))
            except (KeyError, AttributeError, TypeError, ValueError):
                raise TemplateProjectionIdentityConflict(
                    "内存节点模板必须声明确定性 UUID"
                ) from None
            if template_uuid in seen_uuids:
                raise TemplateProjectionIdentityConflict("节点模板 UUID 重复")
            class_identity = candidate.get("class")
            if not isinstance(class_identity, str) or not class_identity:
                raise TemplateProjectionIdentityConflict("节点模板缺少来源类身份")
            candidate["uuid"] = template_uuid
            candidate.setdefault("create_time", _SYNTHETIC_TIME)
            candidate.setdefault("update_time", _SYNTHETIC_TIME)
            schema = candidate.get("schema")
            if schema is not None and not isinstance(schema, str):
                candidate["schema"] = json.dumps(
                    schema,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            uuid_by_key[business_key] = template_uuid
            seen_uuids.add(template_uuid)
            nodes.append(candidate)
            members.append(
                TemplateProjectionMember(
                    authority_id=authority_id,
                    projection_kind="node_template",
                    source_definition_key=stable_projection_key((class_identity, name)),
                    business_key=stable_projection_key(business_key),
                    target_uuid=template_uuid,
                    semantic_hash=semantic_template_hash(
                        "node_template",
                        _semantic_entity(candidate),
                    ),
                    generation=0,
                )
            )
        nodes.sort(key=lambda item: str(item["uuid"]))
        return nodes, tuple(members), uuid_by_key

    @staticmethod
    def _normalize_handles(
        authority_id: str,
        candidates: Sequence[Mapping[str, Any]],
        *,
        node_uuid_by_key: Mapping[tuple[str, str], str],
        nodes_by_uuid: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], tuple[TemplateProjectionMember, ...]]:
        """规范 Handle 全集并解析父动作身份。"""

        handles: list[dict[str, Any]] = []
        members: list[TemplateProjectionMember] = []
        seen_business_keys: set[tuple[str, str, str]] = set()
        seen_uuids: set[str] = set()
        for raw in candidates:
            candidate = copy.deepcopy(dict(raw))
            parent_key = candidate.pop("node_business_key", None)
            if (
                not isinstance(parent_key, (list, tuple))
                or len(parent_key) != 2
                or not all(isinstance(part, str) and part for part in parent_key)
            ):
                raise TemplateProjectionIdentityConflict("句柄模板缺少节点业务身份")
            normalized_parent_key = (str(parent_key[0]), str(parent_key[1]))
            try:
                parent_uuid = node_uuid_by_key[normalized_parent_key]
            except KeyError:
                raise TemplateProjectionIdentityConflict(
                    "句柄模板引用了本轮投影之外的节点模板"
                ) from None
            handle_key = candidate.get("handle_key")
            io_type = candidate.get("io_type")
            if not isinstance(handle_key, str) or not handle_key:
                raise TemplateProjectionIdentityConflict("句柄模板缺少连接点业务名")
            if io_type not in {"source", "target"}:
                raise TemplateProjectionIdentityConflict(
                    "句柄模板方向必须是 source 或 target"
                )
            business_key = (parent_uuid, handle_key, str(io_type))
            if business_key in seen_business_keys:
                raise TemplateProjectionIdentityConflict(
                    "完整模板投影包含重复句柄活动业务唯一键"
                )
            explicit_uuid = candidate.get("uuid")
            if explicit_uuid is None:
                try:
                    template_uuid = action_handle_uuid(
                        parent_uuid,
                        io_type=str(io_type),
                        handle_key=handle_key,
                    )
                except TemplateIdentityError as error:
                    raise TemplateProjectionIdentityConflict(str(error)) from error
            else:
                try:
                    template_uuid = str(UUID(explicit_uuid))
                except (AttributeError, TypeError, ValueError):
                    raise TemplateProjectionIdentityConflict(
                        "句柄模板显式 UUID 非法"
                    ) from None
            if template_uuid in seen_uuids:
                raise TemplateProjectionIdentityConflict("句柄模板 UUID 重复")
            candidate["uuid"] = template_uuid
            candidate["workflow_node_template_uuid"] = parent_uuid
            candidate.setdefault("create_time", _SYNTHETIC_TIME)
            candidate.setdefault("update_time", _SYNTHETIC_TIME)
            seen_business_keys.add(business_key)
            seen_uuids.add(template_uuid)
            handles.append(candidate)
            parent = nodes_by_uuid[parent_uuid]
            members.append(
                TemplateProjectionMember(
                    authority_id=authority_id,
                    projection_kind="handle_template",
                    source_definition_key=stable_projection_key(
                        (
                            str(parent["class"]),
                            str(parent["name"]),
                            str(io_type),
                            handle_key,
                        )
                    ),
                    business_key=stable_projection_key(business_key),
                    target_uuid=template_uuid,
                    semantic_hash=semantic_template_hash(
                        "handle_template",
                        _semantic_entity(candidate),
                    ),
                    generation=0,
                )
            )
        handles.sort(key=lambda item: str(item["uuid"]))
        return handles, tuple(members)


def _semantic_entity(value: Mapping[str, Any]) -> dict[str, Any]:
    """移除内存兼容时间字段后返回规范语义实体。"""

    return {
        str(key): copy.deepcopy(item)
        for key, item in value.items()
        if key not in {"create_time", "update_time", "deleted_at"}
    }


__all__ = ["InMemoryRegistryTemplateProjectionStore"]
