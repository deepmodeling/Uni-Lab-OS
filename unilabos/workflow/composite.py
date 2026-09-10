"""组合工作流调用（CompositeWorkflowInvocation）的公共深模块门面。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import rfc8785

from unilabos.workflow.catalog import PublishedWorkflowSource
from unilabos.workflow.composite_expansion import (
    CompositeAuthoring,
    CompositeExpansion,
    PublishedWorkflowResolver,
    PublishedWorkflowSnapshotProvider,
)
from unilabos.workflow.handle_projection import (
    resource_slot_schema,
    structural_ready_handle,
    workflow_handle_type,
)
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PublishedWorkflowContractError(ValueError):
    """已发布工作流合同不能从权威快照安全投影。"""

    def __init__(self, code: str, path: str) -> None:
        """保存稳定错误码和诊断路径。

        参数：``code`` 是关闭失败分类，``path`` 是 JSON Pointer。返回：无。
        异常：无；构造器只保存已判定的失败结果。
        """

        self.code = code
        self.path = path
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PublishedWorkflowContract:
    """一个节点模板及其完整边界连接点（Handle）候选。"""

    template: dict[str, Any]
    handles: tuple[dict[str, Any], ...]


def project_published_workflow_contract(
    *,
    source: PublishedWorkflowSource,
    applied_snapshot: Mapping[str, Any],
    host_node_resource_template: Mapping[str, Any] | None,
) -> PublishedWorkflowContract | None:
    """把同修订已应用工作流投影为封闭目录合同。

    参数：``source`` 来自已发布源码目录（PublishedSourceCatalog）；
    ``applied_snapshot`` 是同一工作流存储视图中的图与应用源码；
    ``host_node_resource_template`` 是框架渲染所有者摘要。返回：可与设备模板同代
    原子发布的节点/连接点候选；未应用或应用修订陈旧时返回 ``None``。异常：
    来源、图、合同、哈希或宿主身份不一致时抛出
    ``PublishedWorkflowContractError``，函数本身从不写入。
    """

    if not isinstance(source, PublishedWorkflowSource):
        raise TypeError("source 必须是 PublishedWorkflowSource")
    if not isinstance(applied_snapshot, Mapping):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow",
        )
    workflow = applied_snapshot.get("workflow")
    applied_source = applied_snapshot.get("applied_source")
    if not isinstance(workflow, Mapping):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/workflow",
        )
    workflow_uuid = _uuid(
        workflow.get("uuid"),
        "/published_workflow/workflow/uuid",
    )
    if workflow_uuid != source.workflow_uuid:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/source/workflow_uuid",
        )
    revision = workflow.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/workflow/revision",
        )
    if not isinstance(applied_source, Mapping):
        return None
    if applied_source.get("workflow_revision") != revision:
        return None
    applied_source_hash = _digest(
        applied_source.get("source_hash"),
        "/published_workflow/applied_source/source_hash",
    )
    host_summary = _host_summary(host_node_resource_template)

    graph = {
        "workflow": _plain(workflow),
        "nodes": _array(applied_snapshot.get("nodes"), "/published_workflow/nodes"),
        "edges": _array(applied_snapshot.get("edges"), "/published_workflow/edges"),
        "node_templates": _array(
            applied_snapshot.get("node_templates"),
            "/published_workflow/node_templates",
        ),
        "handle_templates": _array(
            applied_snapshot.get("handle_templates"),
            "/published_workflow/handle_templates",
        ),
    }
    try:
        workflow_io = validate_workflow_graph_io(graph)
    except (WorkflowIOValidationError, KeyError, TypeError, ValueError):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/io_contract",
        ) from None
    inputs = workflow_io.input_contract.to_dict()["parameters"]
    outputs = workflow_io.output_contract.to_dict()["outputs"]
    transparent = _composition_mode(workflow)
    contract_digest = _contract_digest(
        inputs=inputs,
        outputs=outputs,
        composition_allow_transparent=transparent,
    )
    schema = _workflow_schema(
        inputs=inputs,
        outputs=outputs,
        workflow_uuid=workflow_uuid,
        workflow_revision=revision,
        applied_source_hash=applied_source_hash,
        contract_digest=contract_digest,
        composition_allow_transparent=transparent,
    )
    node_business_key = (host_summary["uuid"], f"workflow:{workflow_uuid}")
    handles = tuple(
        [
            _value_handle(item, io_type="target", node_business_key=node_business_key)
            for item in inputs
        ]
        + [
            _value_handle(item, io_type="source", node_business_key=node_business_key)
            for item in outputs
        ]
        + [
            _ready_handle("target", node_business_key=node_business_key),
            _ready_handle("source", node_business_key=node_business_key),
        ]
    )
    return PublishedWorkflowContract(
        template={
            "resource_template_uuid": host_summary["uuid"],
            "name": f"workflow:{workflow_uuid}",
            "display_name": str(workflow.get("name") or source.symbol),
            "description": str(workflow.get("description") or ""),
            "class": f"{source.module}:{source.symbol}",
            "type": "workflow",
            "node_type": "workflow",
            "goal": {str(item["name"]): str(item["name"]) for item in inputs},
            "goal_default": {
                str(item["name"]): _plain(item["default"])
                for item in inputs
                if "default" in item
            },
            "feedback": {},
            "result": {str(item["name"]): str(item["name"]) for item in outputs},
            "schema": schema,
            "meta_data": {
                "resource_template": host_summary,
                "unilab": {
                    "framework_owner_only": True,
                    "workflow_source": {
                        "kind": "package",
                        "definition_fqid": source.definition_fqid,
                        "module": source.module,
                        "symbol": source.symbol,
                        "package_catalog_digest": source.package_catalog_digest,
                        "definition_content_hash": source.definition_content_hash,
                    },
                },
            },
        },
        handles=handles,
    )


def _workflow_schema(
    *,
    inputs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Mapping[str, Any]],
    workflow_uuid: str,
    workflow_revision: int,
    applied_source_hash: str,
    contract_digest: str,
    composition_allow_transparent: bool,
) -> dict[str, Any]:
    """构造前端与 OS 共同验证的封闭工作流节点 Schema。

    参数：``inputs``/``outputs`` 是规范边界描述，工作流身份、修订、源码摘要、
    合同摘要和组合模式构成冻结 pin。返回：封闭节点 JSON Schema。异常：描述符
    缺少规范名称或 Schema 时抛出 ``KeyError``。
    """

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goal": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    str(item["name"]): _input_property_schema(item)
                    for item in inputs
                },
                "required": [
                    str(item["name"]) for item in inputs if item.get("required") is True
                ],
            },
            "result": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    str(item["name"]): _plain(item["schema"]) for item in outputs
                },
                "required": [str(item["name"]) for item in outputs],
            },
        },
        "required": ["goal", "result"],
        "x-unilabos-workflow-contract": {
            "version": 1,
            "compatibility_version": 1,
            "workflow_uuid": workflow_uuid,
            "workflow_revision": workflow_revision,
            "applied_source_hash": applied_source_hash,
            "contract_digest": contract_digest,
            "composition_allow_transparent": composition_allow_transparent,
            "input_order": [str(item["name"]) for item in inputs],
            "output_order": [str(item["name"]) for item in outputs],
        },
    }


def _input_property_schema(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """构造包含可选默认值的已发布工作流输入属性 Schema。

    参数：``descriptor`` 是规范输入描述符。返回：独立值 Schema；有默认值时
    写入 JSON Schema ``default``。异常：Schema 不是对象时抛出 ``TypeError``。
    """

    schema = _plain(descriptor["schema"])
    if not isinstance(schema, dict):
        raise TypeError("已发布工作流输入 Schema 无效")
    if "default" in descriptor:
        schema["default"] = _plain(descriptor["default"])
    return schema


def _contract_digest(
    *,
    inputs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Mapping[str, Any]],
    composition_allow_transparent: bool,
) -> str:
    """计算 C1 v1 字节稳定合同摘要，排除展示文字与运行身份。

    参数：``inputs``/``outputs`` 是边界描述，``composition_allow_transparent``
    是冻结组合模式。返回：带 ``sha256:`` 前缀的规范摘要。异常：描述含不可规范
    JSON 值时由 RFC 8785 编码器抛出。
    """

    payload = {
        "version": 1,
        "composition_allow_transparent": composition_allow_transparent,
        "inputs": [_semantic_descriptor(item) for item in inputs],
        "outputs": [_semantic_descriptor(item) for item in outputs],
    }
    return "sha256:" + hashlib.sha256(rfc8785.dumps(payload)).hexdigest()


def _semantic_descriptor(raw: Mapping[str, Any]) -> dict[str, Any]:
    """移除不影响兼容性的标题和说明并返回分离 JSON 对象。

    参数：``raw`` 是规范边界描述符。返回：去除展示字段的递归副本。异常：无。
    """

    return {
        str(key): _plain(value)
        for key, value in raw.items()
        if key not in {"title", "description"}
    }


def _value_handle(
    descriptor: Mapping[str, Any],
    *,
    io_type: str,
    node_business_key: tuple[str, str],
) -> dict[str, Any]:
    """把一个规范输入或输出描述符投影为业务连接点候选。

    参数：``descriptor`` 是规范边界描述，``io_type`` 是方向，
    ``node_business_key`` 是父模板业务身份。返回：业务连接点候选。异常：描述符
    缺少名称或 Schema 时抛出 ``KeyError``。
    """

    name = str(descriptor["name"])
    schema = _plain(descriptor["schema"])
    slot_schema = resource_slot_schema(schema)
    allowlist = (
        _plain(slot_schema.get("allowed_resource_template_uuids"))
        if slot_schema is not None
        else None
    )
    implicit = bool(descriptor.get("implicit", False)) if io_type == "source" else False
    return {
        "node_business_key": node_business_key,
        "handle_key": name,
        "io_type": io_type,
        "display_name": str(descriptor.get("title") or name),
        "description": str(descriptor.get("description") or ""),
        "type": workflow_handle_type(schema),
        "required": bool(descriptor.get("required", False))
        if io_type == "target"
        else False,
        "data_source": "goal" if io_type == "target" else "result",
        "data_key": name,
        "meta_data": {
            "unilab": {
                "value_schema": schema,
                "editor_control": (
                    "material_port" if slot_schema is not None else "variable_selector"
                ),
                "allowed_resource_template_uuids": allowlist,
                "implicit_passthrough": implicit,
            }
        },
    }


def _ready_handle(
    io_type: str,
    *,
    node_business_key: tuple[str, str],
) -> dict[str, Any]:
    """为结构性 ready 连接点补充父节点稳定业务身份。

    参数：``io_type`` 是方向，``node_business_key`` 是父模板业务身份。返回：
    带父身份的 ready 连接点候选。异常：方向不合法时由投影器抛出 ``ValueError``。
    """

    handle = structural_ready_handle(io_type)
    handle["node_business_key"] = node_business_key
    return handle


def _composition_mode(workflow: Mapping[str, Any]) -> bool:
    """读取当前仅允许布尔值的透明组合声明。

    参数：``workflow`` 是已应用工作流投影。返回：透明组合布尔声明。异常：声明
    非布尔值时抛出 ``PublishedWorkflowContractError``。
    """

    meta_data = workflow.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    value = (
        unilab.get("composition_allow_transparent", False)
        if isinstance(unilab, Mapping)
        else False
    )
    if not isinstance(value, bool):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/published_workflow/composition_allow_transparent",
        )
    return value


def _host_summary(value: Mapping[str, Any] | None) -> dict[str, str]:
    """校验并复制宿主节点资源模板的最小摘要。

    参数：``value`` 是宿主节点资源模板摘要。返回：只含规范身份和名称的副本。
    异常：字段集、UUID 或名称无效时抛出 ``PublishedWorkflowContractError``。
    """

    if not isinstance(value, Mapping) or set(value) != {
        "uuid",
        "name",
        "display_name",
    }:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/host_node/resource_template_uuid",
        )
    identity = _uuid(value.get("uuid"), "/host_node/resource_template_uuid")
    name = value.get("name")
    display_name = value.get("display_name")
    if not isinstance(name, str) or not name or not isinstance(display_name, str):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            "/host_node/resource_template",
        )
    return {"uuid": identity, "name": name, "display_name": display_name}


def _uuid(value: Any, path: str) -> str:
    """校验规范 UUID 并把失败映射为发布合同错误。

    参数：``value`` 是待校验身份，``path`` 是诊断路径。返回：规范 UUID 字符串。
    异常：身份无效或非规范表示时抛出 ``PublishedWorkflowContractError``。
    """

    try:
        identity = validate_uuid(value)
    except (TypeError, ValueError):
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            path,
        ) from None
    if value != identity:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            path,
        )
    return identity


def _digest(value: Any, path: str) -> str:
    """校验小写 SHA-256 wire 字符串。

    参数：``value`` 是待校验摘要，``path`` 是诊断路径。返回：规范摘要字符串。
    异常：类型或格式无效时抛出 ``PublishedWorkflowContractError``。
    """

    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PublishedWorkflowContractError(
            "composite_catalog_mismatch",
            path,
        )
    return value


def _array(value: Any, path: str) -> list[Any]:
    """复制必填数组外壳并为非法结构提供稳定路径。

    参数：``value`` 是数组候选，``path`` 是诊断路径。返回：递归分离的列表。
    异常：候选不是列表时抛出 ``PublishedWorkflowContractError``。
    """

    if not isinstance(value, list):
        raise PublishedWorkflowContractError("composite_catalog_mismatch", path)
    return _plain(value)


def _plain(value: Any) -> Any:
    """递归复制 JSON 映射与数组，避免发布候选共享调用方容器。

    参数：``value`` 是 JSON 兼容值。返回：容器递归分离后的等价值。异常：无；
    标量按原值返回。
    """

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "CompositeAuthoring",
    "CompositeExpansion",
    "PublishedWorkflowContract",
    "PublishedWorkflowContractError",
    "PublishedWorkflowResolver",
    "PublishedWorkflowSnapshotProvider",
    "project_published_workflow_contract",
]
