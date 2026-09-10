"""工作流输入/输出（Workflow I/O）合同、绑定身份与类型可赋值规则。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from unilabos.workflow.models import WorkflowNodeWrite, validate_uuid
from unilabos.workflow.schema import (
    WorkflowInputContract,
    WorkflowOutputContract,
    WorkflowSchemaError,
    WorkflowValueSchema,
    normalize_value,
    parse_input_contract,
    parse_output_contract,
    parse_value_schema,
)
from unilabos.workflow.site_selector_value_schema import (
    SiteSelectorValueSchemaError,
    normalize_projected_site_selector_schema,
)

_EMPTY_INPUT_CONTRACT = {"version": 1, "parameters": []}
_EMPTY_OUTPUT_CONTRACT = {"version": 1, "outputs": []}


class WorkflowIOValidationError(ValueError):
    """工作流图（Workflow Graph）的输入/输出权威事实不自洽。"""


@dataclass(frozen=True)
class ValidatedWorkflowIO:
    """一次公共校验产生的不可变工作流输入/输出规范事实。"""

    input_contract: WorkflowInputContract
    output_contract: WorkflowOutputContract
    input_bindings: Mapping[str, Mapping[str, Mapping[str, str]]]
    output_bindings: Mapping[str, Mapping[str, str]]


def validate_workflow_io(
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    handles: Mapping[str, Mapping[str, Any]],
    workflow_meta_data: Mapping[str, Any],
    node_meta_data: Mapping[str, Mapping[str, Any]],
) -> ValidatedWorkflowIO:
    """在传输层之外验证完整工作流输入/输出权威事实。

    参数说明：`nodes` 和 `handles` 是按 UUID 索引的冻结图实体；
    `workflow_meta_data` 保存输入/输出合同，`node_meta_data` 保存输入绑定。
    返回值是只读的规范合同和绑定；任何不一致统一抛出
    `WorkflowIOValidationError`。
    """

    try:
        unilab = _unilab_metadata(workflow_meta_data, label="工作流")
        input_contract = parse_input_contract(
            unilab.get("input_contract", _EMPTY_INPUT_CONTRACT)
        )
        output_contract = parse_output_contract(
            unilab.get("output_contract", _EMPTY_OUTPUT_CONTRACT)
        )
        input_parameters = {
            item["name"]: item
            for item in input_contract.to_dict()["parameters"]
        }
        input_bindings = _validate_input_bindings(
            nodes=nodes,
            handles=handles,
            node_meta_data=node_meta_data,
            input_parameters=input_parameters,
        )
        _validate_node_output_schema_overrides(
            nodes=nodes,
            handles=handles,
            node_meta_data=node_meta_data,
        )
        output_bindings = _validate_output_bindings(
            nodes=nodes,
            handles=handles,
            node_meta_data=node_meta_data,
            raw_bindings=unilab.get("output_bindings", {}),
            input_parameters=input_parameters,
            output_contract=output_contract,
        )
    except WorkflowIOValidationError:
        raise
    except (KeyError, TypeError, ValueError, WorkflowSchemaError) as exc:
        raise WorkflowIOValidationError("工作流输入/输出合同无效") from exc
    return ValidatedWorkflowIO(
        input_contract=input_contract,
        output_contract=output_contract,
        input_bindings=MappingProxyType(input_bindings),
        output_bindings=MappingProxyType(output_bindings),
    )


def validate_workflow_graph_io(
    graph: Mapping[str, Any],
) -> ValidatedWorkflowIO:
    """从后端（Backend）形状的工作流图构造并验证唯一输入/输出权威事实。

    参数说明：`graph` 必须包含工作流、节点和连接点（Handle）模板投影。
    返回值与 `validate_workflow_io` 相同；此适配器（Adapter）只负责 DTO
    转换，不另写校验规则。
    """

    try:
        workflow = graph.get("workflow")
        raw_nodes = graph.get("nodes")
        raw_handles = graph.get("handle_templates")
        if (
            not isinstance(workflow, Mapping)
            or not isinstance(raw_nodes, list)
            or not isinstance(raw_handles, list)
        ):
            raise WorkflowIOValidationError("工作流图输入/输出投影无效")

        nodes: dict[str, WorkflowNodeWrite] = {}
        node_meta_data: dict[str, Mapping[str, Any]] = {}
        for raw_node in raw_nodes:
            if not isinstance(raw_node, Mapping):
                raise WorkflowIOValidationError("工作流节点投影无效")
            node = WorkflowNodeWrite.model_validate(raw_node)
            if node.uuid in nodes:
                raise WorkflowIOValidationError("工作流节点 UUID 重复")
            nodes[node.uuid] = node
            node_meta_data[node.uuid] = node.meta_data

        handles: dict[str, Mapping[str, Any]] = {}
        for handle in raw_handles:
            if not isinstance(handle, Mapping):
                raise WorkflowIOValidationError("工作流连接点（Handle）投影无效")
            handle_uuid = handle.get("uuid")
            if not isinstance(handle_uuid, str):
                raise WorkflowIOValidationError(
                    "工作流连接点（Handle）UUID 无效或重复"
                )
            canonical_uuid = validate_uuid(handle_uuid)
            if canonical_uuid != handle_uuid or handle_uuid in handles:
                raise WorkflowIOValidationError(
                    "工作流连接点（Handle）UUID 无效或重复"
                )
            handles[handle_uuid] = handle

        return validate_workflow_io(
            nodes=nodes,
            handles=handles,
            workflow_meta_data=workflow.get("meta_data", {}),
            node_meta_data=node_meta_data,
        )
    except WorkflowIOValidationError:
        raise
    except (KeyError, TypeError, ValueError, WorkflowSchemaError) as exc:
        raise WorkflowIOValidationError("工作流图输入/输出投影无效") from exc


def handle_value_schema(handle: Mapping[str, Any]) -> WorkflowValueSchema:
    """读取连接点（Handle）的规范值 Schema。

    参数：``handle`` 是目录中的连接点（Handle）投影，优先携带规范
    ``value_schema``，旧 ``type`` 只作为兼容输入。
    返回：经过可空联合规范化、物料占位符（ResourceSlot）投影和严格解析的
    不可变工作流值 Schema。
    异常：连接点元数据、可空联合、物料模板约束或值 Schema 非法时抛出
    ``WorkflowIOValidationError``，不会按旧显示类型猜测有效合同。
    """

    meta_data = handle.get("meta_data", {})
    unilab = _unilab_metadata(meta_data, label="连接点（Handle）")
    raw_schema = unilab.get("value_schema")
    if raw_schema is None:
        raw_schema = _legacy_handle_schema(handle.get("type"))
    if not isinstance(raw_schema, Mapping):
        raise WorkflowIOValidationError("连接点（Handle）value_schema 无效")
    # ``plain_schema`` 是与冻结目录分离的动作字段 JSON Schema；物料字段需先
    # 投影成工作流唯一的物料占位符（ResourceSlot）值 Schema，再做 I/O 校验。
    plain_schema = _normalize_nullable_json_type(_plain_mapping(raw_schema))
    schema = _projected_material_value_schema(plain_schema)
    if schema is None and str(handle.get("type") or "").lower() == "resourceslot":
        schema = {"$slot": "ResourceSlot"}
    if schema is None:
        schema = _value_set_schema(plain_schema)
    allowlist = unilab.get("allowed_resource_template_uuids")
    if allowlist is not None:
        schema, applied = _apply_placeholder_allowlist(schema, allowlist)
        if not applied:
            raise WorkflowIOValidationError(
                "非物料占位符（ResourceSlot）连接点（Handle）携带物料模板约束"
            )
    try:
        return parse_value_schema(schema)
    except WorkflowSchemaError as exc:
        raise WorkflowIOValidationError(
            "连接点（Handle）value_schema 无效"
        ) from exc


def schema_is_assignable(
    producer: WorkflowValueSchema | Mapping[str, Any],
    consumer: WorkflowValueSchema | Mapping[str, Any],
) -> bool:
    """判断生产端全部规范值是否都可赋给消费端。

    参数说明：`producer` 是值来源保证，`consumer` 是接收方允许集合。任一
    Schema 非法时失败关闭并返回 `False`，不把解析错误当成宽松兼容。
    """

    try:
        producer_schema = (
            producer
            if isinstance(producer, WorkflowValueSchema)
            else parse_value_schema(producer)
        )
        consumer_schema = (
            consumer
            if isinstance(consumer, WorkflowValueSchema)
            else parse_value_schema(consumer)
        )
    except WorkflowSchemaError:
        return False
    return _schema_dict_is_assignable(
        producer_schema.to_dict(),
        consumer_schema.to_dict(),
    )


def resource_slot_passthrough_is_compatible(
    input_schema: WorkflowValueSchema | Mapping[str, Any],
    output_schema: WorkflowValueSchema | Mapping[str, Any],
    *,
    exact: bool = False,
) -> bool:
    """验证同名物料占位符（ResourceSlot）输入/输出能否安全透传。

    参数说明：`input_schema` 是进入工作流的物料集合，`output_schema` 是离开
    工作流的承诺；`exact=True` 要求服务端隐式输出完全相等，否则允许扩宽。
    """

    try:
        parsed_input = (
            input_schema
            if isinstance(input_schema, WorkflowValueSchema)
            else parse_value_schema(input_schema)
        )
        parsed_output = (
            output_schema
            if isinstance(output_schema, WorkflowValueSchema)
            else parse_value_schema(output_schema)
        )
    except WorkflowSchemaError:
        return False
    input_dict = parsed_input.to_dict()
    output_dict = parsed_output.to_dict()
    if not _schema_contains_resource_slot(input_dict):
        return False
    if exact:
        return input_dict == output_dict
    return _schema_dict_is_assignable(input_dict, output_dict)


def schema_contains_resource_slot(
    schema: WorkflowValueSchema | Mapping[str, Any],
) -> bool:
    """判断规范 Schema 是否在根、可空成员或数组成员中承载物料占位符。

    参数说明：``schema`` 是已解析的工作流值 Schema 或待解析映射。返回：只有
    完整合法 Schema 包含物料占位符（ResourceSlot）时为真；非法 Schema 失败
    关闭并返回假，不向调用者暴露第三套递归规则。
    """

    try:
        parsed = (
            schema
            if isinstance(schema, WorkflowValueSchema)
            else parse_value_schema(schema)
        )
    except WorkflowSchemaError:
        return False
    return _schema_contains_resource_slot(parsed.to_dict())


def _value_set_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """把动作字段 JSON Schema 规范化为闭合的工作流值 Schema。

    参数：``schema`` 是调用者已深复制的普通字典；库位选择（SiteSelection）
    先交给严格适配器，其他字段再删除展示/默认值注解并规范可空 ``type`` 联合。
    返回：不改变 required 语义的闭合值集合，合法的单一非空类型加 ``null``
    确定性转成非空成员在前的 ``anyOf``。

    异常：库位选择合同或可空联合非法时抛出 ``WorkflowIOValidationError``；
    其他未知字段保留给第 1 版 parser 失败关闭，不交给物料投影猜测。
    """

    try:
        site_selector_schema = normalize_projected_site_selector_schema(schema)
    except SiteSelectorValueSchemaError as error:
        raise WorkflowIOValidationError(
            f"连接点（Handle）库位选择合同无效: {error.message}"
        ) from error
    if site_selector_schema is not None:
        return site_selector_schema
    for key in ("default", "title", "description"):
        schema.pop(key, None)
    schema = _normalize_nullable_json_type(schema)
    if "anyOf" in schema:
        members = schema.get("anyOf")
        if not isinstance(members, list):
            return schema
        schema["anyOf"] = [
            _value_set_schema(member) if isinstance(member, dict) else member
            for member in members
        ]
    items = schema.get("items")
    if isinstance(items, dict):
        schema["items"] = _value_set_schema(items)
    if schema.get("type") == "object" and schema.get("additionalProperties") is True:
        schema.pop("additionalProperties")
    return schema


def _normalize_nullable_json_type(schema: dict[str, Any]) -> dict[str, Any]:
    """严格规范化单个 JSON Schema 的可空 ``type`` 联合。

    参数：``schema`` 是已与冻结目录分离的动作字段 JSON Schema 普通字典。
    返回：无 ``type`` 数组时返回原字典；合法的唯一非空类型加唯一 ``null``
    联合返回非空成员在前的规范 ``anyOf``。
    异常：联合成员数、``null`` 基数或非空成员类型非法时抛出
    ``WorkflowIOValidationError``，确保任何物料语义投影前已经失败关闭。
    """

    # ``json_types`` 是 Pydantic 动作合同输出的联合成员；列表出现就必须完整
    # 满足当前可空闭集，不能因后续识别出物料 UUID 外形而跳过基数校验。
    json_types = schema.get("type")
    if not isinstance(json_types, (list, tuple)):
        return schema
    non_null_types = [item for item in json_types if item != "null"]
    if (
        len(json_types) != 2
        or len(non_null_types) != 1
        or json_types.count("null") != 1
        or not isinstance(non_null_types[0], str)
    ):
        raise WorkflowIOValidationError("连接点（Handle）value_schema 无效")
    schema["type"] = non_null_types[0]
    return {
        "anyOf": [
            schema,
            {"type": "null"},
        ]
    }


def _validate_input_bindings(
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    handles: Mapping[str, Mapping[str, Any]],
    node_meta_data: Mapping[str, Mapping[str, Any]],
    input_parameters: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Mapping[str, str]]]:
    """校验每个节点的工作流输入绑定并生成只读规范映射。

    参数说明：四个映射分别提供节点、连接点（Handle）、节点元数据和已解析
    输入参数。返回值按节点 UUID、目标连接点（Handle）UUID 两级索引绑定。
    """

    result: dict[str, Mapping[str, Mapping[str, str]]] = {}
    for node_uuid, node in nodes.items():
        unilab = _unilab_metadata(
            node_meta_data.get(node_uuid, {}),
            label="节点",
        )
        raw_bindings = unilab.get("input_bindings", {})
        if not isinstance(raw_bindings, Mapping):
            raise WorkflowIOValidationError("input_bindings 必须是对象")
        if raw_bindings and node.workflow_node_template_uuid is None:
            raise WorkflowIOValidationError("无模板节点不能声明 input_bindings")
        bindings: dict[str, Mapping[str, str]] = {}
        for handle_uuid, raw_binding in raw_bindings.items():
            handle = handles.get(handle_uuid)
            if (
                not isinstance(handle_uuid, str)
                or handle is None
                or handle.get("workflow_node_template_uuid")
                != node.workflow_node_template_uuid
                or handle.get("io_type") != "target"
            ):
                raise WorkflowIOValidationError(
                    "input_binding 未引用本节点的目标连接点（Handle）"
                )
            if (
                not isinstance(raw_binding, Mapping)
                or set(raw_binding) != {"parameter"}
                or not isinstance(raw_binding.get("parameter"), str)
            ):
                raise WorkflowIOValidationError("input_binding 必须是闭合对象")
            parameter_name = str(raw_binding["parameter"])
            parameter = input_parameters.get(parameter_name)
            if parameter is None or not schema_is_assignable(
                parameter["schema"],
                handle_value_schema(handle),
            ):
                raise WorkflowIOValidationError(
                    "input_binding "
                    f"{node_uuid}:{handle.get('handle_key')} 与工作流参数 "
                    f"{parameter_name} 类型不兼容"
                )
            bindings[handle_uuid] = MappingProxyType(
                {"parameter": parameter_name}
            )
        result[node_uuid] = MappingProxyType(bindings)
    return result


def _validate_output_bindings(
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    handles: Mapping[str, Mapping[str, Any]],
    node_meta_data: Mapping[str, Mapping[str, Any]],
    raw_bindings: Any,
    input_parameters: Mapping[str, Mapping[str, Any]],
    output_contract: WorkflowOutputContract,
) -> dict[str, Mapping[str, str]]:
    """校验工作流输出根绑定的完整性、身份和类型承诺。

    参数说明：`raw_bindings` 是未信任元数据，其余参数提供图身份、输入参数和
    已解析输出合同。返回值按输出名称索引规范闭合绑定。
    """

    outputs = {
        item["name"]: item for item in output_contract.to_dict()["outputs"]
    }
    if not isinstance(raw_bindings, Mapping) or set(raw_bindings) != set(outputs):
        raise WorkflowIOValidationError("工作流输出绑定不完整")
    _validate_resource_slot_output_authority(
        input_parameters=input_parameters,
        outputs=outputs,
        raw_bindings=raw_bindings,
    )
    result: dict[str, Mapping[str, str]] = {}
    for output_name, output in outputs.items():
        binding = raw_bindings[output_name]
        if not isinstance(binding, Mapping):
            raise WorkflowIOValidationError("工作流输出绑定必须是对象")
        kind = binding.get("kind")
        if kind == "workflow_input":
            if set(binding) != {"kind", "parameter"}:
                raise WorkflowIOValidationError("workflow_input binding 不闭合")
            parameter_name = binding.get("parameter")
            parameter = (
                input_parameters.get(parameter_name)
                if isinstance(parameter_name, str)
                else None
            )
            if parameter is None or not schema_is_assignable(
                parameter["schema"],
                output["schema"],
            ):
                raise WorkflowIOValidationError(
                    "工作流输入不能满足输出 Schema"
                )
            normalized = {
                "kind": "workflow_input",
                "parameter": parameter_name,
            }
        elif kind == "node_output":
            if set(binding) != {
                "kind",
                "workflow_node_uuid",
                "source_handle_uuid",
            }:
                raise WorkflowIOValidationError("node_output binding 不闭合")
            node_uuid = binding.get("workflow_node_uuid")
            handle_uuid = binding.get("source_handle_uuid")
            node = nodes.get(node_uuid) if isinstance(node_uuid, str) else None
            handle = handles.get(handle_uuid) if isinstance(handle_uuid, str) else None
            if (
                node is None
                or handle is None
                or handle.get("workflow_node_template_uuid")
                != node.workflow_node_template_uuid
                or handle.get("io_type") != "source"
            ):
                raise WorkflowIOValidationError(
                    "node_output 未引用本节点的来源连接点（Handle）"
                )
            producer_schema = node_output_value_schema(
                node_uuid=node_uuid,
                handle_uuid=handle_uuid,
                handle=handle,
                handles=handles,
                node_meta_data=node_meta_data,
            )
            if not schema_is_assignable(producer_schema, output["schema"]):
                raise WorkflowIOValidationError(
                    "节点输出不能满足工作流输出 Schema"
                )
            normalized = {
                "kind": "node_output",
                "workflow_node_uuid": node_uuid,
                "source_handle_uuid": handle_uuid,
            }
        else:
            raise WorkflowIOValidationError("未知工作流输出绑定 kind")
        result[output_name] = MappingProxyType(normalized)
    return result


def node_output_value_schema(
    *,
    node_uuid: str,
    handle_uuid: str,
    handle: Mapping[str, Any],
    handles: Mapping[str, Mapping[str, Any]],
    node_meta_data: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """读取节点输出的有效 Schema，并验证组合透传类型覆盖。

    参数：节点与来源连接点（Handle）身份、目录连接点和节点元数据。
    返回：默认为目录 Schema；经证明的隐式物料透传则返回窄化 Schema。
    异常：覆盖形状、来源映射或可赋值关系不合法时抛出
    ``WorkflowIOValidationError``。
    """

    base_schema = handle_value_schema(handle)
    unilab = _unilab_metadata(
        node_meta_data.get(node_uuid, {}),
        label="节点",
    )
    raw_overrides = unilab.get("output_schema_overrides", {})
    if not isinstance(raw_overrides, Mapping):
        raise WorkflowIOValidationError("output_schema_overrides 必须是对象")
    override = raw_overrides.get(handle_uuid)
    if override is None:
        return base_schema
    composite = unilab.get("composite")
    source_mappings = (
        composite.get("source_mappings")
        if isinstance(composite, Mapping)
        else None
    )
    source_mapping = (
        source_mappings.get(handle_uuid)
        if isinstance(source_mappings, Mapping)
        else None
    )
    composite_passthrough = (
        isinstance(source_mapping, Mapping)
        and source_mapping.get("kind") == "workflow_input"
    )
    raw_passthroughs = unilab.get("material_passthrough_handles", {})
    target_uuid = (
        raw_passthroughs.get(handle_uuid)
        if isinstance(raw_passthroughs, Mapping)
        else None
    )
    target_handle = handles.get(target_uuid) if isinstance(target_uuid, str) else None
    action_passthrough = (
        isinstance(override, Mapping)
        and isinstance(target_handle, Mapping)
        and target_handle.get("workflow_node_template_uuid")
        == handle.get("workflow_node_template_uuid")
        and target_handle.get("io_type") == "target"
        and target_handle.get("handle_key") == handle.get("handle_key")
        and schema_is_assignable(override, handle_value_schema(target_handle))
    )
    if (
        not isinstance(override, Mapping)
        or not (composite_passthrough or action_passthrough)
        or not schema_is_assignable(override, base_schema)
    ):
        raise WorkflowIOValidationError("组合工作流输出类型覆盖无效")
    return _plain_mapping(override)


def _validate_node_output_schema_overrides(
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    handles: Mapping[str, Mapping[str, Any]],
    node_meta_data: Mapping[str, Mapping[str, Any]],
) -> None:
    """对所有节点输出类型覆盖执行一次全图闭合验证。"""

    for node_uuid, node in nodes.items():
        unilab = _unilab_metadata(
            node_meta_data.get(node_uuid, {}),
            label="节点",
        )
        overrides = unilab.get("output_schema_overrides", {})
        if not isinstance(overrides, Mapping):
            raise WorkflowIOValidationError("output_schema_overrides 必须是对象")
        for handle_uuid in overrides:
            handle = handles.get(handle_uuid) if isinstance(handle_uuid, str) else None
            if (
                handle is None
                or handle.get("workflow_node_template_uuid")
                != node.workflow_node_template_uuid
                or handle.get("io_type") != "source"
            ):
                raise WorkflowIOValidationError(
                    "output_schema_overrides 未引用本节点来源连接点（Handle）"
                )
            node_output_value_schema(
                node_uuid=node_uuid,
                handle_uuid=handle_uuid,
                handle=handle,
                handles=handles,
                node_meta_data=node_meta_data,
            )


def _validate_resource_slot_output_authority(
    *,
    input_parameters: Mapping[str, Mapping[str, Any]],
    outputs: Mapping[str, Mapping[str, Any]],
    raw_bindings: Mapping[str, Any],
) -> None:
    """保证物料占位符（ResourceSlot）输入具有唯一同名透传输出。

    参数说明：`input_parameters` 和 `outputs` 是规范合同实体，`raw_bindings`
    是待校验根绑定。函数只验证不变量，不产生第二份投影。
    """

    for output_name, output in outputs.items():
        if not output.get("implicit", False):
            continue
        parameter = input_parameters.get(output_name)
        binding = raw_bindings.get(output_name)
        if (
            parameter is None
            or not resource_slot_passthrough_is_compatible(
                parameter["schema"],
                output["schema"],
                exact=True,
            )
            or not isinstance(binding, Mapping)
            or dict(binding)
            != {"kind": "workflow_input", "parameter": output_name}
        ):
            raise WorkflowIOValidationError(
                "隐式输出必须是服务端管理的同名物料占位符（ResourceSlot）透传"
            )

    for parameter_name, parameter in input_parameters.items():
        if not _schema_contains_resource_slot(parameter["schema"]):
            continue
        output = outputs.get(parameter_name)
        if output is None or not resource_slot_passthrough_is_compatible(
            parameter["schema"],
            output["schema"],
            exact=bool(output.get("implicit", False)),
        ):
            raise WorkflowIOValidationError(
                "物料占位符（ResourceSlot）输入缺少兼容的同名输出"
            )


def _schema_dict_is_assignable(
    producer: Mapping[str, Any],
    consumer: Mapping[str, Any],
) -> bool:
    """递归判断两个已解析 Schema 的值集合包含关系。

    参数说明：`producer` 是来源值集合，`consumer` 是目标集合。返回 `True`
    仅表示生产端集合是消费端集合的子集。
    """

    producer_base, producer_nullable = _unwrap_nullable(producer)
    consumer_base, consumer_nullable = _unwrap_nullable(consumer)
    if producer_nullable and not consumer_nullable:
        return False

    if "$slot" in producer_base or "$slot" in consumer_base:
        if (
            producer_base.get("$slot") != "ResourceSlot"
            or consumer_base.get("$slot") != "ResourceSlot"
        ):
            return False
        producer_allowed = producer_base.get("allowed_resource_template_uuids")
        consumer_allowed = consumer_base.get("allowed_resource_template_uuids")
        if consumer_allowed is None:
            return True
        if producer_allowed is None:
            return False
        return set(producer_allowed).issubset(consumer_allowed)

    producer_kind = producer_base.get("type")
    consumer_kind = consumer_base.get("type")
    if producer_kind != consumer_kind and not (
        producer_kind == "integer" and consumer_kind == "number"
    ):
        return False
    if producer_kind == "array":
        producer_items = producer_base.get("items")
        consumer_items = consumer_base.get("items")
        if not isinstance(producer_items, Mapping) or not isinstance(
            consumer_items, Mapping
        ):
            return False
        if not _schema_dict_is_assignable(producer_items, consumer_items):
            return False
        return _bounds_are_subset(
            producer_base,
            consumer_base,
            minimum="minItems",
            maximum="maxItems",
        )
    if producer_kind == "object":
        return consumer_kind == "object"

    producer_enum = producer_base.get("enum")
    consumer_enum = consumer_base.get("enum")
    if producer_enum is not None:
        return all(
            _value_satisfies_schema(value, consumer_base)
            for value in producer_enum
        )
    if consumer_enum is not None:
        return False
    if producer_kind in {"integer", "number"}:
        return _bounds_are_subset(
            producer_base,
            consumer_base,
            minimum="minimum",
            maximum="maximum",
        )
    if producer_kind == "string":
        return _bounds_are_subset(
            producer_base,
            consumer_base,
            minimum="minLength",
            maximum="maxLength",
        )
    return producer_kind == consumer_kind


def _bounds_are_subset(
    producer: Mapping[str, Any],
    consumer: Mapping[str, Any],
    *,
    minimum: str,
    maximum: str,
) -> bool:
    """判断生产端上下界是否不宽于消费端。

    参数说明：`minimum` 与 `maximum` 是当前类型使用的约束键名；两个 Schema
    已通过严格解析。返回值表示边界集合包含关系成立。
    """

    consumer_minimum = consumer.get(minimum)
    producer_minimum = producer.get(minimum)
    if consumer_minimum is not None and (
        producer_minimum is None or producer_minimum < consumer_minimum
    ):
        return False
    consumer_maximum = consumer.get(maximum)
    producer_maximum = producer.get(maximum)
    return not (
        consumer_maximum is not None
        and (producer_maximum is None or producer_maximum > consumer_maximum)
    )


def _value_satisfies_schema(value: Any, schema: Mapping[str, Any]) -> bool:
    """判断单个枚举值是否满足消费端 Schema；非法值返回 `False`。"""

    try:
        normalize_value(parse_value_schema(schema), value)
    except WorkflowSchemaError:
        return False
    return True


def _unwrap_nullable(
    schema: Mapping[str, Any],
) -> tuple[Mapping[str, Any], bool]:
    """拆出严格可空 Schema 的实体成员，并返回是否允许空值。"""

    members = schema.get("anyOf")
    if not isinstance(members, list):
        return schema, False
    return members[0], True


def _schema_contains_resource_slot(schema: Mapping[str, Any]) -> bool:
    """递归判断 Schema 是否承载物料占位符（ResourceSlot）值。"""

    base, _ = _unwrap_nullable(schema)
    if base.get("$slot") == "ResourceSlot":
        return True
    items = base.get("items")
    return (
        base.get("type") == "array"
        and isinstance(items, Mapping)
        and _schema_contains_resource_slot(items)
    )


def _unilab_metadata(
    meta_data: Mapping[str, Any],
    *,
    label: str,
) -> Mapping[str, Any]:
    """读取闭合领域元数据；`label` 用于形成可定位的中文错误。"""

    if not isinstance(meta_data, Mapping):
        raise WorkflowIOValidationError(f"{label} meta_data 必须是对象")
    unilab = meta_data.get("unilab", {})
    if not isinstance(unilab, Mapping):
        raise WorkflowIOValidationError(f"{label} meta_data.unilab 必须是对象")
    return unilab


def _plain_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """深复制动作合同映射和数组。

    参数：``value`` 是调用方持有的目录投影映射。返回：递归分离映射、列表和
    元组容器后的普通字典。异常：无；非容器叶值按原值保留。
    """

    return {
        str(key): (
            _plain_mapping(item)
            if isinstance(item, Mapping)
            else [
                _plain_mapping(child) if isinstance(child, Mapping) else child
                for child in item
            ]
            if isinstance(item, (list, tuple))
            else item
        )
        for key, item in value.items()
    }


def _projected_material_value_schema(
    schema: Mapping[str, Any],
) -> dict[str, Any] | None:
    """把动作字段 JSON Schema 投影为规范物料占位符值 Schema。

    参数：``schema`` 是动作合同投影保留的对象、数组或可空物料引用。
    返回：对应的物料占位符（ResourceSlot）、物料数组或可空规范 Schema；
    非物料字段返回 ``None``。动作物料锁（Action Material Lock）标记只决定
    执行占用，不进入工作流值类型。
    异常：嵌套物料 Schema 的可空 ``type`` 联合非法时抛出
    ``WorkflowIOValidationError``，无法识别的合法 Schema 则关闭式返回 ``None``。
    """

    # ``normalized_schema`` 确保数组成员或 anyOf 成员递归进入物料投影前也使用
    # 与根连接点（Handle）相同的严格可空联合规则。
    normalized_schema = _normalize_nullable_json_type(_plain_mapping(schema))
    schema = normalized_schema

    # 已规范化工作流连接点（Handle）会直接携带物料占位符
    # （ResourceSlot）；不能把它降级为旧 ``type`` 推断，否则数组/可空包装会丢失。
    if schema.get("$slot") == "ResourceSlot":
        return _plain_mapping(schema)

    # ``members`` 接受动作合同的标准 ``anyOf`` 可空形态，并只保留一个非空成员。
    members = schema.get("anyOf")
    if isinstance(members, (list, tuple)):
        non_null_members = [
            member
            for member in members
            if isinstance(member, Mapping) and member.get("type") != "null"
        ]
        has_null = any(
            isinstance(member, Mapping) and member.get("type") == "null"
            for member in members
        )
        if len(non_null_members) == 1 and has_null:
            projected = _projected_material_value_schema(non_null_members[0])
            if projected is not None:
                return {"anyOf": [projected, {"type": "null"}]}

    json_type = schema.get("type")
    if json_type == "array" and isinstance(schema.get("items"), Mapping):
        projected_items = _projected_material_value_schema(schema["items"])
        if projected_items is None:
            return None
        result: dict[str, Any] = {"type": "array", "items": projected_items}
        for bound in ("minItems", "maxItems"):
            if bound in schema:
                result[bound] = schema[bound]
        return result

    # ``properties`` 的唯一 UUID 字段是无锁动作结果的稳定物料引用形态；输入还
    # 可以用 true/false 锁标记显式表达默认占用或 ``free``。
    properties = schema.get("properties")
    required = schema.get("required") or []
    uuid_schema = properties.get("uuid") if isinstance(properties, Mapping) else None
    is_material_reference = isinstance(
        schema.get("x-unilabos-material-lock"), bool
    ) or (
        isinstance(properties, Mapping)
        and set(properties) == {"uuid"}
        and isinstance(uuid_schema, Mapping)
        and uuid_schema.get("type") == "string"
        and uuid_schema.get("format") == "uuid"
        and "uuid" in required
    )
    return {"$slot": "ResourceSlot"} if is_material_reference else None


def _apply_placeholder_allowlist(
    schema: dict[str, Any],
    allowlist: Any,
) -> tuple[dict[str, Any], bool]:
    """把旧连接点（Handle）物料模板允许集合投影到物料占位符 Schema。

    参数说明：`schema` 是规范值 Schema，`allowlist` 是旧目录旁路字段。返回
    新 Schema 与是否找到物料占位符；双重声明不一致时失败关闭。
    """

    result = _plain_mapping(schema)
    if result.get("$slot") == "ResourceSlot":
        existing = result.get("allowed_resource_template_uuids")
        if existing is not None and existing != allowlist:
            raise WorkflowIOValidationError(
                "物料占位符（ResourceSlot）允许集合存在双重事实冲突"
            )
        result["allowed_resource_template_uuids"] = allowlist
        return result, True
    if result.get("type") == "array" and isinstance(result.get("items"), dict):
        items, applied = _apply_placeholder_allowlist(result["items"], allowlist)
        result["items"] = items
        return result, applied
    if isinstance(result.get("anyOf"), list):
        members: list[Any] = []
        applied = False
        for member in result["anyOf"]:
            if isinstance(member, dict) and member.get("type") != "null":
                member, applied = _apply_placeholder_allowlist(member, allowlist)
            members.append(member)
        result["anyOf"] = members
        return result, applied
    return result, False


def _legacy_handle_schema(value: Any) -> dict[str, Any]:
    """把旧连接点（Handle）`type` 映射为最小规范 Schema。

    参数说明：`value` 是未信任旧类型文本。未知类型失败关闭；返回值仅是兼容
    适配器（Adapter），不成为第二套工作流输入/输出类型规范。
    """

    raw = str(value or "").strip().lower()
    scalars = {
        "str": "string",
        "string": "string",
        "int": "integer",
        "integer": "integer",
        "float": "number",
        "number": "number",
        "bool": "boolean",
        "boolean": "boolean",
        "dict": "object",
        "object": "object",
        "json": "object",
    }
    if raw == "resourceslot":
        return {"$slot": "ResourceSlot"}
    if raw.startswith("list[") and raw.endswith("]"):
        return {
            "type": "array",
            "items": _legacy_handle_schema(raw[5:-1]),
        }
    if raw in scalars:
        return {"type": scalars[raw]}
    raise WorkflowIOValidationError("连接点（Handle）缺少规范 value_schema")


__all__ = [
    "ValidatedWorkflowIO",
    "WorkflowIOValidationError",
    "handle_value_schema",
    "node_output_value_schema",
    "resource_slot_passthrough_is_compatible",
    "schema_contains_resource_slot",
    "schema_is_assignable",
    "validate_workflow_graph_io",
    "validate_workflow_io",
]
