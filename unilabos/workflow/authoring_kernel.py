"""可信工作流创作编译内核（Authoring Kernel）的窄公共接口。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from unilabos.workflow.models import CandidateCompilation, validate_uuid
from unilabos.workflow.source_identity import (
    PythonSourceIdentityError,
    canonical_python_source_identity,
)


class AuthoringCatalogError(ValueError):
    """工作流创作目录（Authoring Catalog）不完整或不一致。"""


@dataclass(frozen=True, slots=True)
class AuthoringCatalogAction:
    """一个动作节点模板及其不可变连接点（Handle）集合。"""

    template: Mapping[str, Any]
    handles: tuple[Mapping[str, Any], ...]

    def detached_template(self) -> dict[str, Any]:
        """返回不共享容器的节点模板投影。

        返回值：可安全写入候选图的普通 JSON 字典；修改它不会污染目录快照。
        """

        return _detach(self.template)

    def detached_handles(self) -> list[dict[str, Any]]:
        """返回不共享容器的连接点（Handle）模板投影列表。"""

        return [_detach(handle) for handle in self.handles]


@dataclass(frozen=True, slots=True)
class AuthoringCatalogSnapshot:
    """编译期间使用的不可变、带指纹目录快照（Catalog Snapshot）。"""

    fingerprint: str
    actions: tuple[AuthoringCatalogAction, ...]
    _by_business_key: Mapping[tuple[str, str], AuthoringCatalogAction]
    _by_template_uuid: Mapping[str, AuthoringCatalogAction]
    _resource_template_uuid_by_symbol: Mapping[str, str]
    _resource_template_symbol_by_uuid: Mapping[str, str]

    @classmethod
    def from_entities(
        cls,
        node_templates: Sequence[Mapping[str, Any]],
        handle_templates: Sequence[Mapping[str, Any]],
        *,
        resource_template_symbols: Mapping[str, str] | None = None,
    ) -> AuthoringCatalogSnapshot:
        """从后端形状目录实体建立不可变快照。

        参数说明：``node_templates`` 是完整节点模板集合，``handle_templates``
        是完整连接点集合。返回值按稳定 JSON 计算 SHA-256 指纹；重复业务身份、
        重复 UUID 或孤儿连接点会抛出 ``AuthoringCatalogError``；
        ``resource_template_symbols`` 把资源模板源码符号冻结到本地模板 UUID。
        """

        nodes = [_json_mapping(item, "节点模板") for item in node_templates]
        handles = [_json_mapping(item, "连接点模板") for item in handle_templates]
        node_ids = [_required_uuid(item, "uuid") for item in nodes]
        handle_ids = [_required_uuid(item, "uuid") for item in handles]
        if len(set(node_ids)) != len(node_ids) or len(set(handle_ids)) != len(handle_ids):
            raise AuthoringCatalogError("工作流创作目录包含重复 UUID")

        handles_by_parent: dict[str, list[dict[str, Any]]] = {
            node_uuid: [] for node_uuid in node_ids
        }
        for handle in handles:
            parent_uuid = _required_uuid(handle, "workflow_node_template_uuid")
            if parent_uuid not in handles_by_parent:
                raise AuthoringCatalogError("连接点模板引用未知节点模板")
            handles_by_parent[parent_uuid].append(handle)

        actions: list[AuthoringCatalogAction] = []
        by_business_key: dict[tuple[str, str], AuthoringCatalogAction] = {}
        by_template_uuid: dict[str, AuthoringCatalogAction] = {}
        for node in sorted(nodes, key=lambda item: str(item["uuid"])):
            class_identity = node.get("class")
            action_name = node.get("name")
            if not isinstance(class_identity, str) or not class_identity.strip():
                raise AuthoringCatalogError("节点模板缺少设备类身份")
            if not isinstance(action_name, str) or not action_name.strip():
                raise AuthoringCatalogError("节点模板缺少动作业务名")
            node_uuid = str(node["uuid"])
            action = AuthoringCatalogAction(
                template=_freeze(node),
                handles=tuple(
                    _freeze(handle)
                    for handle in sorted(
                        handles_by_parent[node_uuid],
                        key=_catalog_handle_order,
                    )
                ),
            )
            business_key = (class_identity, action_name)
            if business_key in by_business_key:
                raise AuthoringCatalogError("工作流创作目录动作业务身份重复")
            by_business_key[business_key] = action
            by_template_uuid[node_uuid] = action
            actions.append(action)

        resource_uuid_by_symbol: dict[str, str] = {}
        resource_symbol_by_uuid: dict[str, str] = {}
        for raw_symbol, raw_uuid in sorted(
            (resource_template_symbols or {}).items(),
            key=lambda item: str(item[0]),
        ):
            try:
                symbol = canonical_python_source_identity(raw_symbol)
            except PythonSourceIdentityError as error:
                raise AuthoringCatalogError(
                    "资源模板源码身份不能安全生成 Python import"
                ) from error
            try:
                template_uuid = validate_uuid(raw_uuid)
            except (TypeError, ValueError):
                raise AuthoringCatalogError("资源模板源码符号映射到非法 UUID") from None
            if symbol in resource_uuid_by_symbol:
                raise AuthoringCatalogError("资源模板源码符号重复")
            previous_symbol = resource_symbol_by_uuid.get(template_uuid)
            if previous_symbol is not None and previous_symbol != symbol:
                raise AuthoringCatalogError("资源模板 UUID 绑定了多个源码符号")
            resource_uuid_by_symbol[symbol] = template_uuid
            resource_symbol_by_uuid[template_uuid] = symbol

        payload = {
            "node_templates": sorted(
                (_catalog_semantic_entity(item) for item in nodes),
                key=lambda item: str(item["uuid"]),
            ),
            "handle_templates": sorted(
                (_catalog_semantic_entity(item) for item in handles),
                key=lambda item: str(item["uuid"]),
            ),
            "resource_template_symbols": resource_uuid_by_symbol,
        }
        fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            fingerprint=fingerprint,
            actions=tuple(actions),
            _by_business_key=MappingProxyType(by_business_key),
            _by_template_uuid=MappingProxyType(by_template_uuid),
            _resource_template_uuid_by_symbol=MappingProxyType(
                resource_uuid_by_symbol
            ),
            _resource_template_symbol_by_uuid=MappingProxyType(
                resource_symbol_by_uuid
            ),
        )

    def require_material_source(self) -> AuthoringCatalogAction:
        """取得唯一框架物料来源（MaterialSource）节点模板。

        参数：无。返回：带单一 source 物料占位符（ResourceSlot）的不可变
        框架模板；模板不存在或合同被污染时抛出 ``AuthoringCatalogError``。
        """

        framework = self.require_action(
            "unilabos.workflow.authoring:material_source",
            "material_source",
        )
        # ``source_handles`` 是框架对外发布的物料流出口，必须严格唯一。
        source_handles = tuple(
            handle
            for handle in framework.handles
            if handle.get("handle_key") == "material"
            and handle.get("io_type") == "source"
            and handle.get("type") == "ResourceSlot"
        )
        if (
            framework.template.get("type") != "material_source"
            or framework.template.get("node_type") != "material_source"
            or len(framework.handles) != 1
            or len(source_handles) != 1
        ):
            raise AuthoringCatalogError("物料来源框架模板合同不完整")
        return framework

    def require_resource_template_uuid(self, source_symbol: str) -> str:
        """把资源模板源码符号解析为冻结的本地 UUID。

        参数说明：``source_symbol`` 是工作流源码中的资源模板稳定符号。
        返回：本次目录代际冻结的资源模板 UUID；未知符号关闭式失败。
        """

        try:
            return self._resource_template_uuid_by_symbol[source_symbol]
        except (KeyError, TypeError):
            raise AuthoringCatalogError("工作流创作目录缺少资源模板源码身份") from None

    def require_resource_template_symbol(self, template_uuid: str) -> str:
        """把本地资源模板 UUID 反解为冻结的源码符号。

        参数说明：``template_uuid`` 来自候选工作流图。返回：规范源码符号；
        UUID 非法或不在本次目录代际时抛出 ``AuthoringCatalogError``。
        """

        try:
            return self._resource_template_symbol_by_uuid[
                validate_uuid(template_uuid)
            ]
        except (KeyError, TypeError, ValueError):
            raise AuthoringCatalogError("工作流创作目录缺少资源模板 UUID 身份") from None

    def require_action(
        self,
        class_identity: str,
        action_name: str,
    ) -> AuthoringCatalogAction:
        """按设备类和动作业务名取得唯一目录动作。

        参数说明：两个字符串来自纯 AST（抽象语法树）静态解析；返回不可变动作
        aggregate，缺失时抛出 ``AuthoringCatalogError``，不进行模糊匹配。
        """

        try:
            return self._by_business_key[(class_identity, action_name)]
        except (KeyError, TypeError):
            raise AuthoringCatalogError("工作流创作目录缺少动作身份") from None

    def require_template(self, template_uuid: str) -> AuthoringCatalogAction:
        """按节点模板 UUID 取得唯一目录动作。

        参数说明：``template_uuid`` 来自候选图；返回不可变目录动作，未知或非法
        UUID 抛出 ``AuthoringCatalogError``。
        """

        try:
            return self._by_template_uuid[validate_uuid(template_uuid)]
        except (KeyError, TypeError, ValueError):
            raise AuthoringCatalogError("工作流创作目录缺少模板 UUID") from None


class AuthoringKernel(Protocol):
    """工作流服务（WorkflowService）可依赖的纯创作编译接口。"""

    compiler_version: str
    template_catalog_fingerprint: str

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """把作者源码静态编译为候选结果（CandidateCompilation）。"""

    def generate_python(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        graph: dict[str, Any],
        source_uri: str,
    ) -> CandidateCompilation:
        """把候选图确定性生成规范 Python 源码。"""

    def validate(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        graph: dict[str, Any],
        python_source: str,
        source_uri: str,
    ) -> CandidateCompilation:
        """共同验证候选图和源码是否语义等价。"""


def _required_uuid(entity: Mapping[str, Any], field: str) -> str:
    """读取目录实体中的必填 UUID。

    参数说明：``entity`` 是目录实体，``field`` 是字段名；返回规范 UUID，缺失
    或非法时抛出 ``AuthoringCatalogError``。
    """

    try:
        return validate_uuid(entity[field])
    except (KeyError, TypeError, ValueError):
        raise AuthoringCatalogError(f"目录字段 {field} 不是有效 UUID") from None


def _catalog_handle_order(handle: Mapping[str, Any]) -> tuple[int, int, str]:
    """取得句柄模板的稳定动作合同顺序。

    参数说明：``handle`` 是已验证句柄实体；F03 投影在元数据中记录跨输入/输出的
    ``contract_order``。历史实体没有该字段时按 UUID 稳定排序，保持兼容。
    """

    meta_data = handle.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    order = unilab.get("contract_order") if isinstance(unilab, Mapping) else None
    if isinstance(order, int) and order >= 0:
        return 0, order, str(handle["uuid"])
    return 1, 0, str(handle["uuid"])


def _catalog_semantic_entity(entity: Mapping[str, Any]) -> dict[str, Any]:
    """移除不参与模板目录指纹的数据库操作字段。

    参数说明：``entity`` 是节点或句柄模板；返回新字典并排除创建、更新时间与软
    删除标记，使相同合同的重复刷新和重启保持同一目录指纹。
    """

    operational_fields = {"create_time", "update_time", "deleted_at"}
    return {
        key: value
        for key, value in entity.items()
        if key not in operational_fields
    }


def _json_mapping(entity: Mapping[str, Any], label: str) -> dict[str, Any]:
    """把调用方目录实体复制为普通 JSON 字典。

    参数说明：``entity`` 是待复制映射，``label`` 用于错误说明；返回完全分离的
    字典，不可 JSON 编码时抛出 ``AuthoringCatalogError``。
    """

    if not isinstance(entity, Mapping):
        raise AuthoringCatalogError(f"{label}必须是对象")
    try:
        return json.loads(
            json.dumps(
                dict(entity),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
        )
    except (TypeError, ValueError):
        raise AuthoringCatalogError(f"{label}必须是 JSON 对象") from None


def _freeze(value: Any) -> Any:
    """递归冻结 JSON 值。

    参数说明：``value`` 是已验证 JSON 值；对象变为只读映射、数组变为元组，
    标量保持不变，返回值不与调用方共享可变容器。
    """

    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _detach(value: Any) -> Any:
    """把冻结 JSON 值递归还原为普通容器。

    参数说明：``value`` 来自目录快照；返回可写字典/列表副本，供候选图持有。
    """

    if isinstance(value, Mapping):
        return {key: _detach(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_detach(child) for child in value]
    return value


__all__ = [
    "AuthoringCatalogAction",
    "AuthoringCatalogError",
    "AuthoringCatalogSnapshot",
    "AuthoringKernel",
]
