"""不可变已发布工作流合同（PublishedWorkflowContract）的本地权威。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4, uuid5

import rfc8785

from unilabos.workflow.center_capabilities import capabilities_from_graph
from unilabos.workflow.handle_projection import resource_slot_schema, workflow_handle_type
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.material_ports import MaterialPortError, ports_from_graph
from unilabos.workflow.store import WorkflowStore, utc_now
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)

_DIGEST_PREFIX = "sha256:"
_COMPOSITE_HOST_RESOURCE_TEMPLATE_UUID = "00000000-0000-5000-8000-000000000052"


class PublishedContractConflict(RuntimeError):
    """发布修订或既有不可变内容发生冲突。"""


class PublishedContractInvalid(ValueError):
    """来源工作流不能形成完整的发布合同。"""


def _json(value: Any) -> str:
    """把 JSON 值编码为稳定 SQLite 文本。"""

    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value: str) -> Any:
    """从 SQLite 文本恢复 JSON 值。"""

    return decode_json_bytes(value.encode("utf-8"))


def _plain(value: Any) -> Any:
    """递归复制冻结映射和序列为普通 JSON 容器。

    参数说明：``value`` 是合同中的 JSON 兼容值。返回：与原值等价、但不携带
    ``mappingproxy`` 等冻结容器的独立值；本函数不抛出业务异常。
    """

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _digest(value: Any) -> str:
    """按 RFC 8785 规范 JSON 计算带算法前缀的 SHA-256。"""

    encoded = rfc8785.dumps(value)
    return _DIGEST_PREFIX + hashlib.sha256(encoded).hexdigest()


def _canonical_graph(graph: Mapping[str, Any]) -> dict[str, Any]:
    """复制并稳定排序完整工作流图，避免摘要依赖数据库返回顺序。

    数量型库存需求只在非空时计入：没有需求的工作流摘要保持不变，已发布合同
    无需重发；带需求的实验操作被组合调用时，父图据此携带子需求。
    """

    canonical = {
        "workflow": dict(graph["workflow"]),
        "nodes": sorted(
            (dict(item) for item in graph.get("nodes", [])),
            key=lambda item: str(item["uuid"]),
        ),
        "edges": sorted(
            (dict(item) for item in graph.get("edges", [])),
            key=lambda item: str(item["uuid"]),
        ),
        "node_templates": sorted(
            (dict(item) for item in graph.get("node_templates", [])),
            key=lambda item: str(item["uuid"]),
        ),
        "handle_templates": sorted(
            (dict(item) for item in graph.get("handle_templates", [])),
            key=lambda item: str(item["uuid"]),
        ),
    }
    requirements = graph.get("inventory_requirements") or []
    if requirements:
        canonical["inventory_requirements"] = sorted(
            (dict(item) for item in requirements),
            key=lambda item: str(item.get("uuid") or item.get("requirement_key") or ""),
        )
    return canonical


def published_graph_source_hash(graph: Mapping[str, Any]) -> str:
    """计算发布合同使用的冻结工作流图指纹。

    参数：``graph`` 是包含工作流、节点、连线及模板的完整候选图。返回：与
    ``PublishedWorkflowContractStore.publish`` 完全相同的图/执行器摘要；该摘要
    与领域包中的原始源码字节哈希不同，不能替代 ``source_draft_hash``。异常：图
    缺少发布合同所需字段或执行器要求非法时抛 ``PublishedContractInvalid``。

    该公共接缝供冷启动恢复比较“同一源码是否仍生成同一发布图”，避免仅凭源码
    字节和工作流修订就把模板目录代际变化误认为不可变合同仍然成立。
    """

    snapshot = _canonical_graph(graph)
    executor_requirements, executor_binding_mapping = (
        _abstract_executor_requirements(snapshot)
    )
    return _digest(
        {
            "graph": snapshot,
            "executor_requirements": executor_requirements,
            "executor_binding_mapping": executor_binding_mapping,
        }
        if executor_requirements or executor_binding_mapping
        else snapshot
    )


def published_graph_semantic_hash(graph: Mapping[str, Any]) -> str:
    """计算忽略存储时间戳和包目录摘要后的发布图语义指纹。

    参数：``graph`` 是发布时或重新编译得到的完整工作流图。返回：忽略工作流、
    节点和连线上的 ``create_time``/``update_time``，以及只记录构建来源的
    ``package_catalog_digest`` 后的稳定摘要。异常：图结构或执行器要求非法时抛
    ``PublishedContractInvalid``。

    时间戳属于当前定义存储的生命周期事实，不是发布合同语义；冷启动编译必然
    会生成新的时间戳。该摘要仅用于确认冷启动候选仍与历史合同是同一张图，持久
    合同中的 ``source_hash`` 仍保持原有兼容格式。
    """

    snapshot = _strip_graph_lifecycle_timestamps(_canonical_graph(graph))
    executor_requirements, executor_binding_mapping = (
        _abstract_executor_requirements(snapshot)
    )
    return _digest(
        {
            "graph": snapshot,
            "executor_requirements": executor_requirements,
            "executor_binding_mapping": executor_binding_mapping,
        }
        if executor_requirements or executor_binding_mapping
        else snapshot
    )


def published_contract_semantic_hash(contract: Mapping[str, Any]) -> str:
    """计算已保存发布合同与重新编译工作流可比较的语义摘要。

    参数：``contract`` 是发布目录中的完整合同。返回：与
    :func:`published_graph_semantic_hash` 相同规则的摘要。发布时为避免把具体
    设备身份暴露给父工作流，合同图会把设备 ``material_uuid`` 抽象为空值，
    而重新编译得到的候选图仍带有具体设备 UUID；此函数用合同已有的执行器绑定
    临时还原这一层信息，使两种表示可以得到同一个摘要。异常：合同字段缺失、
    绑定结构非法或图不完整时抛出 ``PublishedContractInvalid``。
    """

    try:
        graph = _plain(contract["graph_snapshot"])
        binding_mapping = _plain(contract["executor_binding_mapping"])
    except (KeyError, TypeError, ValueError):
        raise PublishedContractInvalid("发布合同图摘要字段无效") from None
    if not isinstance(graph, dict) or not isinstance(binding_mapping, Mapping):
        raise PublishedContractInvalid("发布合同图摘要字段无效")
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        raise PublishedContractInvalid("发布合同图摘要字段无效")
    # 合同图里的设备身份已被发布投影清空；填入按绑定 key 区分的稳定占位符后，
    # ``published_graph_semantic_hash`` 会重新生成与候选图一致的执行器要求摘要。
    for node in nodes:
        if not isinstance(node, dict):
            raise PublishedContractInvalid("发布合同图摘要字段无效")
        node_uuid = node.get("uuid")
        binding_key = binding_mapping.get(str(node_uuid))
        if binding_key is not None:
            node["material_uuid"] = f"__published_executor__:{binding_key}"
    return published_graph_semantic_hash(graph)


@dataclass(frozen=True)
class _ValidatedContract:
    snapshot: dict[str, Any]
    input_contract: dict[str, Any]
    output_contract: dict[str, Any]
    executor_requirements: list[dict[str, Any]]
    executor_binding_mapping: dict[str, str]
    contract_uuid: str
    node_template_uuid: str
    template: dict[str, Any]
    handles: list[dict[str, Any]]
    boundary_mapping: dict[str, Any]


def _validate_contract(contract: Mapping[str, Any]) -> _ValidatedContract:
    """恢复活动定义和读取历史共用同一纯校验，不写入数据库或修改公开可见性。"""
    required = {
        "uuid",
        "create_time",
        "update_time",
        "meta_data",
        "workflow_uuid",
        "workflow_revision",
        "version",
        "name",
        "tags",
        "node_template_uuid",
        "input_contract",
        "output_contract",
        "executor_requirements",
        "executor_binding_mapping",
        "boundary_mapping",
        "graph_snapshot",
        "source_hash",
        "contract_digest",
        "node_count",
        "edge_count",
    }
    if not isinstance(contract, Mapping) or not required.issubset(contract):
        raise PublishedContractInvalid("发布合同字段不完整")
    # ``snapshot`` 是合同冻结的完整图；``workflow_uuid`` 是其来源定义稳定
    # 身份，必须与合同顶层身份一致，不能按名称或当前修订猜测来源。
    snapshot = _canonical_graph(contract["graph_snapshot"])
    workflow = snapshot["workflow"]
    workflow_uuid = str(workflow["uuid"])
    if (
        workflow_uuid != str(contract["workflow_uuid"])
        or int(workflow["revision"]) != int(contract["workflow_revision"])
        or len(snapshot["nodes"]) != int(contract["node_count"])
        or len(snapshot["edges"]) != int(contract["edge_count"])
    ):
        raise PublishedContractInvalid("发布合同与冻结图不一致")
    input_contract, output_contract, workflow_io = _boundary_contracts(snapshot)
    try:
        declared_ports = ports_from_graph(snapshot)
        declared_capabilities = capabilities_from_graph(snapshot)
    except (MaterialPortError, ValueError) as error:
        raise PublishedContractInvalid(str(error)) from error
    if "capabilities" in contract and contract["capabilities"] != declared_capabilities:
        raise PublishedContractInvalid("公开能力与冻结图不一致")
    if "material_ports" in contract and contract["material_ports"] != declared_ports:
        raise PublishedContractInvalid("公开物料端口与冻结图不一致")
    if (
        input_contract != contract["input_contract"]
        or output_contract != contract["output_contract"]
    ):
        raise PublishedContractInvalid("发布合同输入输出不一致")
    executor_requirements = list(contract["executor_requirements"])
    executor_binding_mapping = dict(contract["executor_binding_mapping"])
    # 两个 expected 摘要均从冻结图重新计算：前者证明执行器抽象后的完整来源
    # 未被改写，后者证明前端可见参数、输出和执行器要求仍是发布时合同。
    expected_source_hash = _digest(
        {
            "graph": snapshot,
            "executor_requirements": executor_requirements,
            "executor_binding_mapping": executor_binding_mapping,
        }
        if executor_requirements or executor_binding_mapping
        else snapshot
    )
    expected_contract_digest = _digest(
        {
            "version": 1,
            "parameters": input_contract["parameters"],
            "outputs": output_contract["outputs"],
            "executor_requirements": executor_requirements,
        }
    )
    if (
        expected_source_hash != contract["source_hash"]
        or expected_contract_digest != contract["contract_digest"]
    ):
        raise PublishedContractInvalid("发布合同摘要不一致")

    # ``contract_uuid`` 固定不可变发布合同身份，``node_template_uuid`` 固定
    # 前端拖入引用方图时使用的实验操作节点模板身份；恢复绝不重新生成。
    contract_uuid = str(UUID(str(contract["uuid"])))
    node_template_uuid = str(UUID(str(contract["node_template_uuid"])))
    template, handles, boundary_mapping = _published_template_projection(
        contract_uuid=contract_uuid,
        node_template_uuid=node_template_uuid,
        graph=snapshot,
        input_contract=input_contract,
        output_contract=output_contract,
        workflow_io=workflow_io,
        source_hash=str(contract["source_hash"]),
        contract_digest=str(contract["contract_digest"]),
    )
    if boundary_mapping != contract["boundary_mapping"]:
        raise PublishedContractInvalid("发布合同边界映射不一致")
    return _ValidatedContract(
        snapshot=snapshot,
        input_contract=input_contract,
        output_contract=output_contract,
        executor_requirements=executor_requirements,
        executor_binding_mapping=executor_binding_mapping,
        contract_uuid=contract_uuid,
        node_template_uuid=node_template_uuid,
        template=template,
        handles=handles,
        boundary_mapping=boundary_mapping,
    )


def validate_historical_contract(contract: Mapping[str, Any]) -> None:
    """校验保留历史的完整合同、边界与原摘要，不恢复已下架定义。"""
    try:
        _validate_contract(contract)
    except (KeyError, TypeError, ValueError) as error:
        raise PublishedContractInvalid("历史发布合同与冻结执行图不一致") from error


def _strip_graph_lifecycle_timestamps(value: Any) -> Any:
    """递归移除图投影中的存储时间戳和包目录构建来源摘要。"""

    if isinstance(value, dict):
        return {
            key: _strip_graph_lifecycle_timestamps(item)
            for key, item in value.items()
            if key not in {
                "create_time",
                "update_time",
                # 该摘要覆盖整个包的发现结果；新增无关工作流也会改变它，
                # 不能作为当前工作流图发生语义变化的证据。
                "package_catalog_digest",
            }
        }
    if isinstance(value, list):
        return [_strip_graph_lifecycle_timestamps(item) for item in value]
    return value


def _abstract_executor_requirements(
    snapshot: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """把来源图中的具体设备物料抽象为父调用必须提供的执行器要求。

    参数：``snapshot`` 是独立冻结图，会原地清除设备节点的 ``material_uuid``。
    返回稳定要求数组和“来源节点→要求 key”映射；缺少模板或同一设备跨不兼容
    资源模板使用时抛 ``PublishedContractInvalid``。
    """

    templates = {
        str(item["uuid"]): item
        for item in snapshot["node_templates"]
        if isinstance(item, Mapping) and isinstance(item.get("uuid"), str)
    }
    groups: dict[str, dict[str, Any]] = {}
    requirements: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for node in snapshot["nodes"]:
        material_uuid = node.get("material_uuid")
        if material_uuid is None:
            continue
        template_uuid = node.get("workflow_node_template_uuid")
        template = templates.get(str(template_uuid))
        resource_template_uuid = (
            template.get("resource_template_uuid")
            if isinstance(template, Mapping)
            else None
        )
        if not isinstance(resource_template_uuid, str):
            raise PublishedContractInvalid("设备节点缺少可发布的资源模板")
        group = groups.get(str(material_uuid))
        if group is None:
            position = len(requirements) + 1
            group = {
                "key": f"executor_{position}",
                "display_name": f"执行设备 {position}",
                "resource_template_uuid": resource_template_uuid,
                "required": True,
            }
            groups[str(material_uuid)] = group
            requirements.append(group)
        elif group["resource_template_uuid"] != resource_template_uuid:
            raise PublishedContractInvalid("同一设备通过不兼容资源模板参与工作流")
        mapping[str(node["uuid"])] = str(group["key"])
        node["material_uuid"] = None
    return requirements, mapping


def _boundary_contracts(
    graph: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """从冻结图投影 Backend 形状的输入、输出合同。

    参数：``graph`` 是同一修订的完整工作流图。返回 Schema v1 输入与输出对象；图的
    连接点不闭合或类型不兼容时抛 ``PublishedContractInvalid``。
    """

    try:
        workflow_io = validate_workflow_graph_io(graph)
    except (KeyError, TypeError, ValueError, WorkflowIOValidationError):
        raise PublishedContractInvalid("工作流输入输出合同不完整") from None
    parameters = workflow_io.input_contract.to_dict().get("parameters", [])
    outputs = workflow_io.output_contract.to_dict().get("outputs", [])
    return (
        {"version": 1, "parameters": parameters},
        {"version": 1, "outputs": outputs},
        workflow_io,
    )


def _handle_uuid(template_uuid: str, io_type: str, key: str) -> str:
    """为发布模板的连接点生成确定性 UUID。"""

    return str(uuid5(UUID(template_uuid), f"published-handle:{io_type}:{key}"))


def _contract_property_schema(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """把输入或输出描述转换为带展示单位的独立 JSON Schema。

    参数说明：descriptor 是通过工作流输入输出合同校验的描述。返回：不修改原
    描述、并在存在单位时附加 x-unilabos-unit 的属性 Schema。异常：Schema 不是
    对象时抛出 TypeError。
    """

    schema = _plain(descriptor["schema"])
    if not isinstance(schema, dict):
        raise TypeError("发布工作流属性 Schema 无效")
    if "unit" in descriptor:
        schema["x-unilabos-unit"] = _plain(descriptor["unit"])
    return schema


def _published_template_projection(
    *,
    contract_uuid: str,
    node_template_uuid: str,
    graph: Mapping[str, Any],
    input_contract: Mapping[str, Any],
    output_contract: Mapping[str, Any],
    workflow_io: Any,
    source_hash: str,
    contract_digest: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """构造发布模板、公开连接点及服务端边界映射。

    参数：两个 UUID 固定发布合同与节点模板身份；``graph`` 是冻结工作流图；输入、
    输出合同和 ``workflow_io`` 提供已校验的参数及连接关系，合同中的可选单位会同时
    写入连接点元数据与属性 Schema；两个摘要固定来源与合同内容。返回节点模板、
    连接点列表和边界映射。异常：合同缺少参数、连接点或绑定
    时抛出 ``KeyError``，非法模板 UUID 由 ``UUID`` 构造器抛出 ``ValueError``。
    """

    workflow = graph["workflow"]
    handles: list[dict[str, Any]] = []
    input_handle_by_name: dict[str, str] = {}
    output_handle_by_name: dict[str, str] = {}
    for descriptor in input_contract["parameters"]:
        name = str(descriptor["name"])
        schema_value = descriptor["schema"]
        slot_schema = resource_slot_schema(schema_value)
        handle_uuid = _handle_uuid(node_template_uuid, "target", name)
        input_handle_by_name[name] = handle_uuid
        unilab_metadata = {
            "value_schema": schema_value,
            "editor_control": (
                "material_port"
                if slot_schema is not None
                else "variable_selector"
            ),
            "allowed_resource_template_uuids": (
                slot_schema.get("allowed_resource_template_uuids")
                if slot_schema is not None
                else None
            ),
            "implicit_passthrough": False,
        }
        if "unit" in descriptor:
            unilab_metadata["unit"] = _plain(descriptor["unit"])
        handles.append(
            {
                "uuid": handle_uuid,
                "handle_key": name,
                "io_type": "target",
                "display_name": str(descriptor.get("title") or name),
                "description": descriptor.get("description"),
                "type": workflow_handle_type(schema_value),
                "required": bool(descriptor.get("required", False)),
                "data_source": "goal",
                "data_key": name,
                "meta_data": {"unilab": unilab_metadata},
            }
        )
    for descriptor in output_contract["outputs"]:
        name = str(descriptor["name"])
        schema_value = descriptor["schema"]
        slot_schema = resource_slot_schema(schema_value)
        handle_uuid = _handle_uuid(node_template_uuid, "source", name)
        output_handle_by_name[name] = handle_uuid
        unilab_metadata = {
            "value_schema": schema_value,
            "editor_control": (
                "material_port"
                if slot_schema is not None
                else "variable_selector"
            ),
            "allowed_resource_template_uuids": (
                slot_schema.get("allowed_resource_template_uuids")
                if slot_schema is not None
                else None
            ),
            "implicit_passthrough": bool(descriptor.get("implicit", False)),
        }
        if "unit" in descriptor:
            unilab_metadata["unit"] = _plain(descriptor["unit"])
        handles.append(
            {
                "uuid": handle_uuid,
                "handle_key": name,
                "io_type": "source",
                "display_name": str(descriptor.get("title") or name),
                "description": descriptor.get("description"),
                "type": workflow_handle_type(schema_value),
                "required": False,
                "data_source": "result",
                "data_key": name,
                "meta_data": {"unilab": unilab_metadata},
            }
        )
    for io_type in ("target", "source"):
        handles.append(
            {
                "uuid": _handle_uuid(node_template_uuid, io_type, "ready"),
                "handle_key": "ready",
                "io_type": io_type,
                "display_name": "ready",
                "description": None,
                "type": "default",
                "required": False,
                "data_source": None,
                "data_key": None,
                "meta_data": {},
            }
        )

    target_mappings: dict[str, list[dict[str, str]]] = {
        handle_uuid: [] for handle_uuid in input_handle_by_name.values()
    }
    for node_uuid, bindings in workflow_io.input_bindings.items():
        for target_handle_uuid, binding in bindings.items():
            parameter = str(binding["parameter"])
            boundary_handle_uuid = input_handle_by_name[parameter]
            target_mappings[boundary_handle_uuid].append(
                {
                    "workflow_node_uuid": node_uuid,
                    "target_handle_uuid": target_handle_uuid,
                }
            )
    source_mappings = {
        output_handle_by_name[name]: dict(binding)
        for name, binding in workflow_io.output_bindings.items()
        if name in output_handle_by_name
    }
    boundary_mapping = {
        "target_mappings": target_mappings,
        "source_mappings": source_mappings,
        "structural_mappings": {
            "entry_targets": [],
            "completion_sources": [],
        },
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goal": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    str(item["name"]): _contract_property_schema(item)
                    for item in input_contract["parameters"]
                },
                "required": [
                    str(item["name"])
                    for item in input_contract["parameters"]
                    if item.get("required") is True
                ],
            },
            "result": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    str(item["name"]): _contract_property_schema(item)
                    for item in output_contract["outputs"]
                },
                "required": [str(item["name"]) for item in output_contract["outputs"]],
            },
        },
        "required": ["goal", "result"],
        "x-unilabos-workflow-contract": {
            "version": 1,
            "workflow_uuid": workflow["uuid"],
            "workflow_revision": workflow["revision"],
            "source_hash": source_hash,
            "contract_digest": contract_digest,
        },
    }
    template = {
        "uuid": node_template_uuid,
        "description": workflow.get("description"),
        "meta_data": {
            "unilab": {
                "framework_owner_only": True,
                "workflow_contract": {
                    "version": 1,
                    "contract_uuid": contract_uuid,
                    "workflow_uuid": workflow["uuid"],
                    "workflow_revision": workflow["revision"],
                    "source_hash": source_hash,
                    "contract_digest": contract_digest,
                },
            }
        },
        "resource_template_uuid": _COMPOSITE_HOST_RESOURCE_TEMPLATE_UUID,
        "name": f"workflow:{workflow['uuid']}:r{workflow['revision']}",
        "display_name": workflow["name"],
        "class": None,
        "goal": {
            str(item["name"]): str(item["name"])
            for item in input_contract["parameters"]
        },
        "goal_default": {},
        "feedback": {},
        "result": {
            str(item["name"]): str(item["name"]) for item in output_contract["outputs"]
        },
        "schema": schema,
        "type": "workflow",
        "node_type": "workflow",
    }
    return template, handles, boundary_mapping


class PublishedWorkflowContractStore:
    """在定义目录中维护不可变发布合同的当前进程投影。

    Local 模式的定义目录位于内存；领域包文件中的发布目录负责跨重启恢复。
    使用持久定义库的兼容装配仍可直接把本仓储作为持久权威。
    """

    def __init__(self, store: WorkflowStore) -> None:
        """创建发布仓储并幂等补齐独立表。

        参数：``store`` 是本地工作流唯一写模型。返回：无。异常：DDL 或事务失败
        原样传播，禁止在表未就绪时继续提供发布接口。
        """

        self._store = store
        with store.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS published_workflow_contract (
                    uuid TEXT PRIMARY KEY,
                    create_time TEXT NOT NULL,
                    update_time TEXT NOT NULL,
                    deleted_at TEXT,
                    description TEXT,
                    meta_data TEXT NOT NULL,
                    workflow_uuid TEXT NOT NULL,
                    workflow_revision INTEGER NOT NULL CHECK (workflow_revision > 0),
                    version INTEGER NOT NULL CHECK (version > 0),
                    name TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    node_template_uuid TEXT NOT NULL,
                    input_contract TEXT NOT NULL,
                    output_contract TEXT NOT NULL,
                    executor_requirements TEXT NOT NULL,
                    executor_binding_mapping TEXT NOT NULL,
                    boundary_mapping TEXT NOT NULL,
                    graph_snapshot TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    contract_digest TEXT NOT NULL,
                    node_count INTEGER NOT NULL CHECK (node_count > 0),
                    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
                    FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid),
                    UNIQUE(workflow_uuid, workflow_revision),
                    UNIQUE(workflow_uuid, version)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_published_workflow_contract_latest
                    ON published_workflow_contract(
                        workflow_uuid, version DESC, create_time DESC
                    ) WHERE deleted_at IS NULL
                """
            )

    def publish(
        self,
        *,
        graph: Mapping[str, Any],
        expected_revision: int,
    ) -> dict[str, Any]:
        """幂等冻结当前修订，并在同一工作流内递增发布合同序号。

        参数：``graph`` 是服务层验证后的完整图，``expected_revision`` 是调用方
        乐观并发修订。返回包含私有冻结字段的合同；当前修订变化或同修订内容漂移
        时抛 ``PublishedContractConflict``。
        """

        snapshot = _canonical_graph(graph)
        workflow = snapshot["workflow"]
        workflow_uuid = str(workflow["uuid"])
        if int(workflow["revision"]) != expected_revision:
            raise PublishedContractConflict("工作流修订已变化")
        if not snapshot["nodes"]:
            raise PublishedContractInvalid("已发布工作流至少需要一个节点")
        input_contract, output_contract, workflow_io = _boundary_contracts(snapshot)
        try:
            ports_from_graph(snapshot)
            from unilabos.workflow.center_sites import site_bindings_from_graph
            site_bindings_from_graph(snapshot)
        except MaterialPortError as error:
            raise PublishedContractInvalid(str(error)) from error
        except ValueError as error:
            raise PublishedContractInvalid(str(error)) from error
        executor_requirements, executor_binding_mapping = (
            _abstract_executor_requirements(snapshot)
        )
        from unilabos.workflow.transport_profile import validate_transport_contract
        try:
            validate_transport_contract({"graph_snapshot": snapshot, "executor_requirements": executor_requirements,
                                         "executor_binding_mapping": executor_binding_mapping})
        except ValueError as error:
            raise PublishedContractInvalid(str(error)) from error
        boundary_mapping: dict[str, Any] = {}
        source_hash = _digest(
            {
                "graph": snapshot,
                "executor_requirements": executor_requirements,
                "executor_binding_mapping": executor_binding_mapping,
            }
            if executor_requirements or executor_binding_mapping
            else snapshot
        )
        contract_digest = _digest(
            {
                "version": 1,
                "parameters": input_contract["parameters"],
                "outputs": output_contract["outputs"],
                "executor_requirements": executor_requirements,
            }
        )

        with self._store.transaction() as connection:
            source = connection.execute(
                """
                SELECT revision FROM workflow
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (workflow_uuid,),
            ).fetchone()
            if source is None or int(source["revision"]) != expected_revision:
                raise PublishedContractConflict("工作流修订已变化")
            existing = connection.execute(
                """
                SELECT * FROM published_workflow_contract
                WHERE workflow_uuid = ? AND workflow_revision = ?
                  AND deleted_at IS NULL
                """,
                (workflow_uuid, expected_revision),
            ).fetchone()
            if existing is not None:
                result = self._row(existing)
                if (
                    result["source_hash"] != source_hash
                    or result["contract_digest"] != contract_digest
                ):
                    raise PublishedContractConflict("同一修订的发布内容不一致")
                return result
            # 兼容列名 version 保存合同发布序号，仅用于排序与恢复。
            latest_version = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version), 0)
                    FROM published_workflow_contract
                    WHERE workflow_uuid = ? AND deleted_at IS NULL
                    """,
                    (workflow_uuid,),
                ).fetchone()[0]
            )
            now = utc_now()
            contract_uuid = str(uuid4())
            node_template_uuid = str(uuid4())
            template, handles, boundary_mapping = _published_template_projection(
                contract_uuid=contract_uuid,
                node_template_uuid=node_template_uuid,
                graph=snapshot,
                input_contract=input_contract,
                output_contract=output_contract,
                workflow_io=workflow_io,
                source_hash=source_hash,
                contract_digest=contract_digest,
            )
            authority_id = f"published-workflow-contract:{contract_uuid}"
            connection.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, resource_template_uuid, name,
                    display_name, class, goal, goal_default, feedback, result,
                    schema, type, icon, header, footer, node_type
                ) VALUES (
                    ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, NULL, NULL, ?
                )
                """,
                (
                    node_template_uuid,
                    now,
                    now,
                    template["description"],
                    _json(template["meta_data"]),
                    authority_id,
                    template["resource_template_uuid"],
                    template["name"],
                    template["display_name"],
                    template["class"],
                    _json(template["goal"]),
                    _json(template["goal_default"]),
                    _json(template["feedback"]),
                    _json(template["result"]),
                    _json(template["schema"]),
                    template["type"],
                    template["node_type"],
                ),
            )
            for handle in handles:
                connection.execute(
                    """
                    INSERT INTO workflow_handle_template(
                        uuid, create_time, update_time, deleted_at, description,
                        meta_data, authority_id, workflow_node_template_uuid,
                        handle_key, io_type, display_name, type, required,
                        data_source, data_key
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        handle["uuid"],
                        now,
                        now,
                        handle["description"],
                        _json(handle["meta_data"]),
                        authority_id,
                        node_template_uuid,
                        handle["handle_key"],
                        handle["io_type"],
                        handle["display_name"],
                        handle["type"],
                        int(handle["required"]),
                        handle["data_source"],
                        handle["data_key"],
                    ),
                )
            values = (
                contract_uuid,
                now,
                now,
                workflow.get("description"),
                _json({}),
                workflow_uuid,
                expected_revision,
                latest_version + 1,
                str(workflow["name"]),
                _json(workflow.get("tags", [])),
                node_template_uuid,
                _json(input_contract),
                _json(output_contract),
                _json(executor_requirements),
                _json(executor_binding_mapping),
                _json(boundary_mapping),
                _json(snapshot),
                source_hash,
                contract_digest,
                len(snapshot["nodes"]),
                len(snapshot["edges"]),
            )
            connection.execute(
                """
                INSERT INTO published_workflow_contract(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, workflow_revision, version, name,
                    tags, node_template_uuid, input_contract, output_contract,
                    executor_requirements, executor_binding_mapping,
                    boundary_mapping, graph_snapshot, source_hash,
                    contract_digest, node_count, edge_count
                ) VALUES (
                    ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM published_workflow_contract WHERE uuid = ?",
                (contract_uuid,),
            ).fetchone()
            assert row is not None
            return self._row(row)

    def restore(self, contract: Mapping[str, Any]) -> dict[str, Any]:
        """把领域包文件中的不可变合同恢复到当前进程定义目录。

        参数：``contract`` 是此前发布后完整落盘的私有合同。返回：恢复后的独立
        合同。异常：身份、摘要、边界或来源工作流不一致时关闭启动；同一合同重复
        恢复幂等成功，不重新生成 UUID、合同发布序号或时间。
        """

        validated = _validate_contract(contract)
        snapshot = validated.snapshot
        input_contract = validated.input_contract
        output_contract = validated.output_contract
        executor_requirements = validated.executor_requirements
        executor_binding_mapping = validated.executor_binding_mapping
        contract_uuid = validated.contract_uuid
        node_template_uuid = validated.node_template_uuid
        template = validated.template
        handles = validated.handles
        boundary_mapping = validated.boundary_mapping
        workflow_uuid = str(snapshot["workflow"]["uuid"])
        # ``authority_id`` 把恢复出的节点模板与连接点归到同一发布合同，供失败
        # 补偿精确删除，不能复用工作流 UUID 造成多个发布合同互相覆盖。
        authority_id = f"published-workflow-contract:{contract_uuid}"
        with self._store.transaction() as connection:
            source = connection.execute(
                "SELECT 1 FROM workflow WHERE uuid = ? AND deleted_at IS NULL",
                (workflow_uuid,),
            ).fetchone()
            if source is None:
                raise PublishedContractInvalid("发布合同来源工作流不存在")
            existing = connection.execute(
                "SELECT * FROM published_workflow_contract WHERE uuid = ?",
                (contract_uuid,),
            ).fetchone()
            if existing is not None:
                restored = self._row(existing)
                if restored != dict(contract):
                    raise PublishedContractConflict("发布合同身份内容冲突")
                return restored
            now = str(contract["create_time"])
            update_time = str(contract["update_time"])
            connection.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, resource_template_uuid, name,
                    display_name, class, goal, goal_default, feedback, result,
                    schema, type, icon, header, footer, node_type
                ) VALUES (
                    ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, NULL, NULL, ?
                )
                """,
                (
                    node_template_uuid,
                    now,
                    update_time,
                    template["description"],
                    _json(template["meta_data"]),
                    authority_id,
                    template["resource_template_uuid"],
                    template["name"],
                    template["display_name"],
                    template["class"],
                    _json(template["goal"]),
                    _json(template["goal_default"]),
                    _json(template["feedback"]),
                    _json(template["result"]),
                    _json(template["schema"]),
                    template["type"],
                    template["node_type"],
                ),
            )
            for handle in handles:
                connection.execute(
                    """
                    INSERT INTO workflow_handle_template(
                        uuid, create_time, update_time, deleted_at, description,
                        meta_data, authority_id, workflow_node_template_uuid,
                        handle_key, io_type, display_name, type, required,
                        data_source, data_key
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        handle["uuid"],
                        now,
                        update_time,
                        handle["description"],
                        _json(handle["meta_data"]),
                        authority_id,
                        node_template_uuid,
                        handle["handle_key"],
                        handle["io_type"],
                        handle["display_name"],
                        handle["type"],
                        int(handle["required"]),
                        handle["data_source"],
                        handle["data_key"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO published_workflow_contract(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, workflow_revision, version, name,
                    tags, node_template_uuid, input_contract, output_contract,
                    executor_requirements, executor_binding_mapping,
                    boundary_mapping, graph_snapshot, source_hash,
                    contract_digest, node_count, edge_count
                ) VALUES (
                    ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    contract_uuid,
                    now,
                    update_time,
                    contract.get("description"),
                    _json(contract["meta_data"]),
                    workflow_uuid,
                    int(contract["workflow_revision"]),
                    int(contract["version"]),
                    str(contract["name"]),
                    _json(contract["tags"]),
                    node_template_uuid,
                    _json(input_contract),
                    _json(output_contract),
                    _json(executor_requirements),
                    _json(executor_binding_mapping),
                    _json(boundary_mapping),
                    _json(snapshot),
                    str(contract["source_hash"]),
                    str(contract["contract_digest"]),
                    int(contract["node_count"]),
                    int(contract["edge_count"]),
                ),
            )
            row = connection.execute(
                "SELECT * FROM published_workflow_contract WHERE uuid = ?",
                (contract_uuid,),
            ).fetchone()
            assert row is not None
            return self._row(row)

    def discard(self, contract_uuid: str) -> None:
        """撤销尚未对外发布成功的新进程内合同投影。

        参数：``contract_uuid`` 是本次刚生成的合同身份。返回：无；不存在时幂等
        成功。异常：数据库错误原样传播。调用方只可在领域包文件发布失败且尚未
        刷新引用方时调用。
        """

        # ``identity`` 是待补偿合同的规范 UUID；``authority_id`` 只匹配该合同
        # 投影出的模板与连接点，避免删除同一工作流的旧发布合同。
        identity = str(UUID(contract_uuid))
        authority_id = f"published-workflow-contract:{identity}"
        with self._store.transaction() as connection:
            row = connection.execute(
                """
                SELECT node_template_uuid FROM published_workflow_contract
                WHERE uuid = ?
                """,
                (identity,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "DELETE FROM workflow_handle_template WHERE authority_id = ?",
                (authority_id,),
            )
            connection.execute(
                "DELETE FROM published_workflow_contract WHERE uuid = ?",
                (identity,),
            )
            connection.execute(
                "DELETE FROM workflow_node_template WHERE uuid = ? AND authority_id = ?",
                (row["node_template_uuid"], authority_id),
            )

    def discard_workflow(self, workflow_uuid: str) -> None:
        """下架已删除工作流的发布合同与模板，保留不可变历史正文。

        参数：``workflow_uuid`` 是已经失去活动领域来源的工作流稳定身份。返回：
        无；没有发布合同时幂等成功。异常：UUID 或数据库错误原样传播。
        内存定义 purge 已由领域包文件保留历史；持久定义兼容模式必须保留唯一
        数据库合同权威，不能物理删除已冻结中心调用仍可能使用的合同。
        """

        # ``identity`` 是来源工作流稳定 UUID；每个 ``authority_id`` 精确对应
        # 一个不可变发布合同，先删连接点和合同再删节点模板，避免跨合同误删。
        identity = str(UUID(workflow_uuid))
        with self._store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT uuid, node_template_uuid FROM published_workflow_contract
                WHERE workflow_uuid = ?
                """,
                (identity,),
            ).fetchall()
            for row in rows:
                authority_id = f"published-workflow-contract:{row['uuid']}"
                connection.execute(
                    "DELETE FROM workflow_handle_template WHERE authority_id = ?",
                    (authority_id,),
                )
                connection.execute(
                    "UPDATE published_workflow_contract SET deleted_at = ? WHERE uuid = ?",
                    (utc_now(), row["uuid"]),
                )
                connection.execute(
                    """
                    DELETE FROM workflow_node_template
                    WHERE uuid = ? AND authority_id = ?
                    """,
                    (row["node_template_uuid"], authority_id),
                )

    def get(self, contract_uuid: str) -> dict[str, Any]:
        """读取含冻结图的单个合同；合同不存在时抛 ``KeyError``。"""

        with self._store.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM published_workflow_contract
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (contract_uuid,),
            ).fetchone()
        if row is None:
            raise KeyError(contract_uuid)
        return self._row(row)

    def frozen_dependency(self, contract_uuid: str) -> dict[str, Any]:
        """读取冻结父合同引用的历史子合同；不得作为新领取或发布准入入口。"""
        with self._store.read() as connection:
            row = connection.execute(
                "SELECT * FROM published_workflow_contract WHERE uuid = ?",
                (contract_uuid,),
            ).fetchone()
        if row is None:
            raise KeyError(contract_uuid)
        return self._row(row)

    def latest_for_workflow(self, workflow_uuid: str) -> dict[str, Any] | None:
        """读取一个工作流当前最新的已发布合同；没有合同则返回 ``None``。

        参数：``workflow_uuid`` 是工作流稳定身份。返回：按内部发布合同序号倒序选出的
        完整合同；调用方只应使用其发布修订和合同摘要判断工作流状态。异常：
        数据库读取错误原样传播，不把缺少合同误报为数据库故障。
        """

        with self._store.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM published_workflow_contract
                WHERE workflow_uuid = ? AND deleted_at IS NULL
                ORDER BY version DESC, create_time DESC, uuid DESC
                LIMIT 1
                """,
                (workflow_uuid,),
            ).fetchone()
        return None if row is None else self._row(row)

    def get_by_revision_fingerprint(
        self,
        *,
        workflow_uuid: str,
        revision_fingerprint: str,
    ) -> dict[str, Any]:
        """按工作流稳定身份和完整图指纹读取一个不可变发布修订。

        参数：``workflow_uuid`` 是公开工作流身份，``revision_fingerprint`` 是
        发布合同的 ``source_hash``。返回含冻结图的合同；组合不存在时抛
        ``KeyError``，禁止调用方退回当前名称或最新修订。
        """

        with self._store.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM published_workflow_contract
                WHERE workflow_uuid = ? AND source_hash = ?
                  AND deleted_at IS NULL
                """,
                (workflow_uuid, revision_fingerprint),
            ).fetchone()
        if row is None:
            raise KeyError((workflow_uuid, revision_fingerprint))
        return self._row(row)

    def historical_by_revision_fingerprint(
        self, *, workflow_uuid: str, revision_fingerprint: str,
    ) -> dict[str, Any] | None:
        """读取保留历史；服务仅在已确认中心 claim 校验后使用，不能授予新准入。"""
        with self._store.read() as connection:
            row = connection.execute(
                """SELECT * FROM published_workflow_contract
                   WHERE workflow_uuid=? AND source_hash=?""",
                (workflow_uuid, revision_fingerprint),
            ).fetchone()
        return None if row is None else self._row(row)

    def list_latest(
        self,
        *,
        page: int,
        page_size: int,
        keyword: str,
    ) -> dict[str, Any]:
        """分页返回每个来源工作流的最新完整发布合同。

        参数：``page`` 与 ``page_size`` 决定分页窗口，``keyword`` 按名称模糊过滤。
        返回公开合同列表和分页信息。异常：数据库读取失败时原样传播；本方法不吞掉
        权威存储故障，也不返回同一工作流的旧发布合同。
        """

        where = """
            published.deleted_at IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM published_workflow_contract AS newer
                WHERE newer.workflow_uuid = published.workflow_uuid
                  AND newer.version > published.version
                  AND newer.deleted_at IS NULL
            )
        """
        values: list[Any] = []
        normalized_keyword = keyword.strip().lower()
        if normalized_keyword:
            where += " AND LOWER(published.name) LIKE ?"
            values.append(f"%{normalized_keyword}%")
        offset = (page - 1) * page_size
        with self._store.read() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM published_workflow_contract AS published "
                    f"WHERE {where}",
                    values,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT published.*
                FROM published_workflow_contract AS published
                WHERE {where}
                ORDER BY published.create_time DESC, published.uuid DESC
                LIMIT ? OFFSET ?
                """,
                (*values, page_size, offset),
            ).fetchall()
        return {
            "items": [self._public(self._row(row)) for row in rows],
            "has_more": page * page_size < total,
            "page": page,
            "page_size": page_size,
        }

    def snapshot_latest(self) -> list[dict[str, Any]]:
        """一次数据库读取返回全部活动工作流的最新公开合同，禁止拼接分页。"""

        with self._store.read() as connection:
            rows = connection.execute(
                """SELECT published.* FROM published_workflow_contract AS published
                   JOIN workflow ON workflow.uuid = published.workflow_uuid
                   WHERE published.deleted_at IS NULL AND workflow.deleted_at IS NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM published_workflow_contract AS newer
                       WHERE newer.workflow_uuid = published.workflow_uuid
                         AND newer.version > published.version AND newer.deleted_at IS NULL)
                   ORDER BY published.workflow_uuid"""
            ).fetchall()
        return [self._public(self._row(row)) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        """把 SQLite 行恢复为完整领域合同。"""

        result = {
            "uuid": row["uuid"],
            "create_time": row["create_time"],
            "update_time": row["update_time"],
            "meta_data": _load(row["meta_data"]),
            "workflow_uuid": row["workflow_uuid"],
            "workflow_revision": row["workflow_revision"],
            "version": row["version"],  # 兼容元数据：合同发布序号。
            "name": row["name"],
            "tags": _load(row["tags"]),
            "node_template_uuid": row["node_template_uuid"],
            "input_contract": _load(row["input_contract"]),
            "output_contract": _load(row["output_contract"]),
            "executor_requirements": _load(row["executor_requirements"]),
            "executor_binding_mapping": _load(row["executor_binding_mapping"]),
            "boundary_mapping": _load(row["boundary_mapping"]),
            "graph_snapshot": _load(row["graph_snapshot"]),
            "source_hash": row["source_hash"],
            "contract_digest": row["contract_digest"],
            "node_count": row["node_count"],
            "edge_count": row["edge_count"],
        }
        if row["description"] is not None:
            result["description"] = row["description"]
        return result

    @staticmethod
    def _public(contract: Mapping[str, Any]) -> dict[str, Any]:
        """隐藏组合展开私有映射与冻结图，只返回 Backend 公共字段。"""

        hidden = {
            "executor_binding_mapping",
            "boundary_mapping",
            "graph_snapshot",
            "material_ports",
            "capabilities",
        }
        result = {key: value for key, value in contract.items() if key not in hidden}
        result["revision_fingerprint"] = contract["source_hash"]
        capabilities = capabilities_from_graph(contract["graph_snapshot"])
        if capabilities:
            result["capabilities"] = capabilities
        from unilabos.workflow.center_sites import site_bindings_from_graph
        site_bindings = site_bindings_from_graph(contract["graph_snapshot"])
        if site_bindings:
            result["site_bindings"] = site_bindings
        ports = ports_from_graph(contract["graph_snapshot"])
        if ports:
            result["material_ports"] = ports
        from unilabos.workflow.transport_profile import validate_transport_contract
        profile = validate_transport_contract(contract)
        if profile is not None:
            result["transport_allocation"] = profile
            if contract["graph_snapshot"].get("workflow", {}).get("meta_data", {}).get("center_transport_pickup") is not None:
                result["transport_pickup"] = True
        operations = contract["graph_snapshot"].get("workflow", {}).get("meta_data", {}).get("center_handoff_operations")
        if operations is not None:
            if operations != {"schema_version":1}:
                raise ValueError("工站交接操作能力声明无效")
            result["handoff_operations"] = operations
        return result

    def public(self, contract: Mapping[str, Any]) -> dict[str, Any]:
        """返回一个不暴露内部冻结字段的独立公共投影。"""

        return self._public(contract)


__all__ = [
    "PublishedContractConflict",
    "PublishedContractInvalid",
    "PublishedWorkflowContractStore",
    "published_contract_semantic_hash",
    "published_graph_semantic_hash",
    "published_graph_source_hash",
    "validate_historical_contract",
]
