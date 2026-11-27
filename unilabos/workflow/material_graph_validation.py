"""工作流物料图（Material Graph）的集中不变量校验。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.workflow.schema import WorkflowValueSchema
from unilabos.workflow.workflow_io import (
    ValidatedWorkflowIO,
    WorkflowIOValidationError,
    handle_value_schema,
    node_output_value_schema,
    schema_contains_resource_slot,
    schema_is_assignable,
    validate_workflow_graph_io,
)


class MaterialGraphValidationError(ValueError):
    """物料图（Material Graph）违反稳定领域不变量。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定机器码与中文诊断。

        参数说明：``code`` 是服务和编译器共同透传的错误码，``message`` 是中文
        用户诊断。返回：无；构造可跨公共接缝转换的领域异常。
        """

        super().__init__(message)
        self.code = code
        self.message = message


_MATERIAL_MOVEMENT_ACTIONS = frozenset({"pick", "place", "transfer_resource"})


def validate_material_graph(
    *,
    nodes: Sequence[Any],
    edges: Sequence[Any],
    templates: Mapping[str, Mapping[str, Any]],
    handles: Mapping[str, Mapping[str, Any]],
    effective_params: Mapping[str, Mapping[str, Any]],
    validated_workflow_io: ValidatedWorkflowIO | None,
) -> None:
    """一次性验证工作流物料图（Material Graph）的安全不变量。

    参数说明：``nodes`` 与 ``edges`` 是完整候选图；``templates`` 与 ``handles``
    是同一快照的目录投影；``effective_params`` 是事务内最终参数；
    ``validated_workflow_io`` 是已验证工作流输入/输出（Workflow I/O）事实。
    本阶段实施物料流线性（MaterialFlowLinearity）与资源模板兼容
    （ResourceTemplate Compatibility），不读取库存（Inventory）、物料实例或
    库位（Site）。返回：无；同一物料占位符（ResourceSlot）输出出现多条物理
    消费边，或生产保证不能赋给消费者时抛出 ``MaterialGraphValidationError``。
    """

    # ``node_by_uuid`` 固定每条物料边的生产节点身份；已验证工作流输入/输出
    # （Workflow I/O）只提供合同和绑定，不触发任何外部存储读取。
    node_by_uuid = {_field(node, "uuid"): node for node in nodes}
    # ``reachability`` 是候选 DAG 的传递可达关系；同一物料来源允许被多个严格
    # 排序的动作复用，但任何一对可并发兄弟消费者仍然关闭失败。
    reachability = _node_reachability(nodes=nodes, edges=edges)
    # 同一条件节点不同分支里的节点运行时至多执行一个，不构成并发物料消费者。
    exclusive_branches = _condition_branch_ancestry(nodes)
    composite_internal_nodes = _composite_internal_node_uuids(nodes)
    _validate_shared_source_movement(
        nodes=nodes,
        edges=edges,
        templates=templates,
        handles=handles,
        effective_params=effective_params,
    )
    _validate_workflow_input_linearity(
        validated_workflow_io,
        reachability=reachability,
        ignored_node_uuids=composite_internal_nodes,
        exclusive_branches=exclusive_branches,
    )
    # ``outgoing_edges`` 按来源节点和来源连接点聚合全部提交边；禁用节点也不能把
    # 非法物料分叉藏进持久图，因此这里不按运行状态过滤。
    outgoing_consumers: dict[tuple[str, str], list[str]] = defaultdict(list)
    for edge in edges:
        source_node_uuid = _field(edge, "source_node_uuid")
        source_handle_uuid = _field(edge, "source_handle_uuid")
        handle = handles.get(source_handle_uuid)
        if handle is None or not _is_resource_slot_handle(handle):
            continue
        outgoing_consumers[(source_node_uuid, source_handle_uuid)].append(
            _field(edge, "target_node_uuid")
        )
    if any(
        not _consumer_nodes_are_strictly_ordered(
            consumer_nodes,
            reachability=reachability,
            exclusive_branches=exclusive_branches,
        )
        for consumer_nodes in outgoing_consumers.values()
    ):
        raise MaterialGraphValidationError(
            "material_flow_fan_out",
            "同一个物料占位符（ResourceSlot）输出不能连接多个物理消费者",
        )
    incoming_by_target = {
        (
            str(_field(edge, "target_node_uuid")),
            str(_field(edge, "target_handle_uuid")),
        ): edge
        for edge in edges
    }
    producer_schema_cache: dict[
        tuple[str, str], WorkflowValueSchema | Mapping[str, Any]
    ] = {}
    for edge in edges:
        source_node_uuid = _field(edge, "source_node_uuid")
        source_handle_uuid = _field(edge, "source_handle_uuid")
        target_handle_uuid = _field(edge, "target_handle_uuid")
        source_handle = handles.get(source_handle_uuid)
        target_handle = handles.get(target_handle_uuid)
        if (
            source_handle is None
            or target_handle is None
            or not _is_resource_slot_handle(source_handle)
            or not _is_resource_slot_handle(target_handle)
        ):
            continue
        producer_schema = _resolved_producer_schema(
            node_uuid=source_node_uuid,
            source_handle_uuid=source_handle_uuid,
            nodes=node_by_uuid,
            templates=templates,
            handles=handles,
            effective_params=effective_params,
            incoming_by_target=incoming_by_target,
            cache=producer_schema_cache,
            resolving=set(),
        )
        consumer_schema = handle_value_schema(target_handle)
        if not schema_is_assignable(producer_schema, consumer_schema):
            raise MaterialGraphValidationError(
                "material_template_mismatch",
                "物料生产者的资源模板保证不能满足消费者约束："
                f"{source_node_uuid}:{source_handle.get('handle_key')} -> "
                f"{_field(edge, 'target_node_uuid')}:{target_handle.get('handle_key')}",
            )


def _validate_shared_source_movement(
    *,
    nodes: Sequence[Any],
    edges: Sequence[Any],
    templates: Mapping[str, Mapping[str, Any]],
    handles: Mapping[str, Mapping[str, Any]],
    effective_params: Mapping[str, Mapping[str, Any]],
) -> None:
    """拒绝共享来源所代表的原始物料身份进入移动动作。

    参数说明：``nodes``/``edges`` 是完整候选图，``templates`` 与
    ``handles`` 是同代目录，``effective_params`` 是最终节点参数。
    返回：无；共享来源沿同一物料占位符（ResourceSlot）到达
    ``pick``、``place`` 或 ``transfer_resource`` 时抛出稳定诊断。
    """

    # ``node_by_uuid`` 与 ``outgoing`` 保留原始物料身份的定向传播路径。
    node_by_uuid = {str(_field(node, "uuid")): node for node in nodes}
    outgoing: dict[str, list[Any]] = defaultdict(list)
    for edge in edges:
        outgoing[str(_field(edge, "source_node_uuid"))].append(edge)

    for source_uuid, source_node in node_by_uuid.items():
        if not _is_material_source_node(source_node, templates=templates):
            continue
        source_param = effective_params.get(source_uuid, {})
        if source_param.get("custody_policy") != "shared_source":
            continue
        # ``queue`` 只沿同一数据键的物料边继续，避免把动作产出的
        # 新容器身份误判为固定试剂来源本身。
        queue = list(outgoing.get(source_uuid, ()))
        visited: set[tuple[str, str]] = set()
        while queue:
            edge = queue.pop(0)
            target_handle = handles.get(str(_field(edge, "target_handle_uuid")))
            if target_handle is None or not _is_resource_slot_handle(target_handle):
                continue
            target_uuid = str(_field(edge, "target_node_uuid"))
            identity_key = _material_handle_data_key(target_handle)
            visit_key = (target_uuid, identity_key)
            if visit_key in visited:
                continue
            visited.add(visit_key)
            target_node = node_by_uuid.get(target_uuid)
            if target_node is None:
                continue
            action_name = _material_action_name(target_node, templates=templates)
            if action_name in _MATERIAL_MOVEMENT_ACTIONS:
                raise MaterialGraphValidationError(
                    "shared_source_movement_forbidden",
                    f"共享物料来源 {source_uuid} 不得进入移动动作 {action_name}；"
                    "请改用任务全程独占（task_exclusive）",
                )
            # 复合工作流（Composite Workflow）的外部输入与冻结子图
            # 之间没有普通 Edge；``target_mappings`` 是跨边界的权威连接。
            # 将它投影为只读到达项，使共享来源不能藏在复合调用
            # 内部进入 pick/place/transfer_resource。
            queue.extend(
                _composite_material_target_arrivals(
                    target_node,
                    target_handle_uuid=str(_field(edge, "target_handle_uuid")),
                    handles=handles,
                )
            )
            for next_edge in outgoing.get(target_uuid, ()):
                source_handle = handles.get(
                    str(_field(next_edge, "source_handle_uuid"))
                )
                if (
                    source_handle is not None
                    and _is_resource_slot_handle(source_handle)
                    and _material_handle_data_key(source_handle) == identity_key
                ):
                    queue.append(next_edge)


def _composite_material_target_arrivals(
    node: Any,
    *,
    target_handle_uuid: str,
    handles: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    """把复合工作流外部物料输入投影到冻结子图输入。

    参数说明：``node`` 是可能的复合调用节点，``target_handle_uuid``
    是当前物料到达的外部输入，``handles`` 是同代句柄目录。
    返回：可由主遍历直接消费的内部目标到达项；结构不完整时
    返回空集合，由其他创作合同校验报错。
    """

    meta_data = (
        node.get("meta_data")
        if isinstance(node, Mapping)
        else getattr(node, "meta_data", None)
    )
    if not isinstance(meta_data, Mapping):
        return []
    unilab = meta_data.get("unilab")
    if not isinstance(unilab, Mapping):
        return []
    composite = unilab.get("composite")
    if not isinstance(composite, Mapping):
        return []
    target_mappings = composite.get("target_mappings")
    if not isinstance(target_mappings, Mapping):
        return []
    raw_mappings = target_mappings.get(target_handle_uuid)
    if not isinstance(raw_mappings, Sequence) or isinstance(raw_mappings, (str, bytes)):
        return []
    arrivals: list[dict[str, str]] = []
    for mapping in raw_mappings:
        if not isinstance(mapping, Mapping):
            continue
        node_uuid = mapping.get("workflow_node_uuid")
        handle_uuid = mapping.get("target_handle_uuid")
        handle = handles.get(str(handle_uuid or ""))
        if (
            not isinstance(node_uuid, str)
            or not node_uuid
            or handle is None
            or not _is_resource_slot_handle(handle)
        ):
            continue
        arrivals.append(
            {
                "target_node_uuid": node_uuid,
                "target_handle_uuid": str(handle_uuid),
            }
        )
    return arrivals


def _material_handle_data_key(handle: Mapping[str, Any]) -> str:
    """返回物料占位符（ResourceSlot）传播使用的稳定数据键。

    参数：``handle`` 是已验证的连接点（Handle）目录投影。
    返回：优先使用非空 ``data_key``，否则回退到 ``handle_key``。
    """

    data_key = handle.get("data_key")
    if isinstance(data_key, str) and data_key.strip():
        return data_key.strip()
    return str(handle.get("handle_key") or "").strip()


def _material_action_name(
    node: Any,
    *,
    templates: Mapping[str, Mapping[str, Any]],
) -> str:
    """解析节点或冻结模板的动作业务名。

    参数：``node`` 是候选工作流节点，``templates`` 是同代目录。
    返回：小写且无首尾空白的动作名；优先节点冻结值。
    """

    action_name = (
        node.get("action_name")
        if isinstance(node, Mapping)
        else getattr(node, "action_name", None)
    )
    if isinstance(action_name, str) and action_name.strip():
        return action_name.strip().lower()
    template_uuid = (
        node.get("workflow_node_template_uuid")
        if isinstance(node, Mapping)
        else getattr(node, "workflow_node_template_uuid", None)
    )
    template = templates.get(str(template_uuid or ""))
    if template is not None:
        template_name = template.get("name")
        if isinstance(template_name, str) and template_name.strip():
            return template_name.strip().lower()
    node_name = node.get("name") if isinstance(node, Mapping) else getattr(node, "name", "")
    return str(node_name or "").strip().lower()


def validate_material_graph_projection(graph: Mapping[str, Any]) -> None:
    """从后端（Backend）形状候选图调用唯一物料图校验入口。

    参数说明：``graph`` 是含节点、边和模板投影的完整五集合；局部索引只把数组
    转换为 ``validate_material_graph`` 所需形状。返回：无；结构错误保留为普通
    ``TypeError``，工作流输入/输出合同错误抛出 ``WorkflowIOValidationError``，
    领域冲突透传 ``MaterialGraphValidationError``。
    """

    nodes = graph.get("nodes")
    edges = graph.get("edges")
    raw_templates = graph.get("node_templates")
    raw_handles = graph.get("handle_templates")
    if not all(
        isinstance(value, list) for value in (nodes, edges, raw_templates, raw_handles)
    ):
        raise TypeError("候选物料图集合无效")
    templates = _identity_index(raw_templates, label="节点模板")
    handles = _identity_index(raw_handles, label="连接点（Handle）")
    effective_params = {
        _field(node, "uuid"): (
            _field(node, "param") if isinstance(_field(node, "param"), Mapping) else {}
        )
        for node in nodes
    }
    validated_workflow_io = validate_workflow_graph_io(graph)
    validate_material_graph(
        nodes=nodes,
        edges=edges,
        templates=templates,
        handles=handles,
        effective_params=effective_params,
        validated_workflow_io=validated_workflow_io,
    )


def _identity_index(
    values: Sequence[Any],
    *,
    label: str,
) -> dict[str, Mapping[str, Any]]:
    """把目录实体数组转换为稳定 UUID 索引。

    参数说明：``values`` 是待索引对象，``label`` 是中文错误对象名；局部
    ``result`` 保存唯一身份。返回：UUID 到实体映射；非对象或重复身份抛出
    ``TypeError``。
    """

    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise TypeError(f"{label}投影无效")
        identity = _field(value, "uuid")
        if identity in result:
            raise TypeError(f"{label} UUID 重复")
        result[identity] = value
    return result


def _field(entity: Any, name: str) -> str | Mapping[str, Any] | None:
    """兼容读取 Pydantic 实体和普通图字典字段。

    参数说明：``entity`` 是边、节点或模板实体，``name`` 是代码字段名。返回：
    原始映射字段或对象属性；UUID 字段要求非空字符串，``param`` 可返回映射或
    ``None``，非法形状抛出 ``TypeError``。
    """

    value = entity.get(name) if isinstance(entity, Mapping) else getattr(entity, name)
    if name == "param":
        return value
    if not isinstance(value, str) or not value:
        raise TypeError(f"物料图字段 {name} 无效")
    return value


def _is_resource_slot_handle(handle: Mapping[str, Any]) -> bool:
    """判断连接点（Handle）是否传递物料占位符（ResourceSlot）。

    参数说明：``handle`` 是冻结目录连接点投影。返回：规范值 Schema 含
    ``ResourceSlot`` 时为真；Schema 本身非法时返回假，原图校验器仍负责报告
    通用合同错误，避免错误码被物料分叉覆盖。
    """

    try:
        schema = handle_value_schema(handle).to_dict()
    except WorkflowIOValidationError:
        return False
    return schema_contains_resource_slot(schema)


def _producer_schema(
    *,
    node: Any,
    source_handle: Mapping[str, Any],
    templates: Mapping[str, Mapping[str, Any]],
    handles: Mapping[str, Mapping[str, Any]],
    effective_param: Mapping[str, Any],
) -> WorkflowValueSchema | Mapping[str, Any]:
    """解析一条物料边的生产端规范保证。

    参数说明：``node`` 是生产节点；``source_handle`` 是实际输出连接点；
    ``templates`` 与 ``handles`` 是同一目录快照；``effective_param`` 是最终节点
    参数。局部 ``resource_template_uuid`` 是物料来源（MaterialSource）选择器的
    精确模板，``passthrough_input`` 是隐式同名透传输入。返回：可传给
    ``schema_is_assignable`` 的规范 Schema；目录关系不完整时抛出 ``TypeError``。
    """

    if _is_material_source_node(node, templates=templates):
        resource_template_uuid = effective_param.get("resource_template_uuid")
        if not isinstance(resource_template_uuid, str) or not resource_template_uuid:
            raise TypeError("物料来源缺少精确资源模板身份")
        return {
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": [resource_template_uuid],
        }
    node_uuid = str(_field(node, "uuid"))
    meta_data = (
        node.get("meta_data")
        if isinstance(node, Mapping)
        else getattr(node, "meta_data", {})
    )
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    overrides = (
        unilab.get("output_schema_overrides")
        if isinstance(unilab, Mapping)
        else None
    )
    source_handle_uuid = str(_field(source_handle, "uuid"))
    if isinstance(overrides, Mapping) and source_handle_uuid in overrides:
        return node_output_value_schema(
            node_uuid=node_uuid,
            handle_uuid=source_handle_uuid,
            handle=source_handle,
            handles=handles,
            node_meta_data={node_uuid: meta_data},
        )
    if _is_implicit_passthrough(source_handle):
        passthrough_input = _same_name_input(
            source_handle,
            handles=handles,
        )
        return handle_value_schema(passthrough_input)
    return handle_value_schema(source_handle)


def _resolved_producer_schema(
    *,
    node_uuid: str,
    source_handle_uuid: str,
    nodes: Mapping[str, Any],
    templates: Mapping[str, Mapping[str, Any]],
    handles: Mapping[str, Mapping[str, Any]],
    effective_params: Mapping[str, Mapping[str, Any]],
    incoming_by_target: Mapping[tuple[str, str], Any],
    cache: dict[tuple[str, str], WorkflowValueSchema | Mapping[str, Any]],
    resolving: set[tuple[str, str]],
) -> WorkflowValueSchema | Mapping[str, Any]:
    """解析组合调用显式同名输出沿父图输入继承的精确物料保证。"""

    key = (node_uuid, source_handle_uuid)
    if key in cache:
        return cache[key]
    if key in resolving:
        raise TypeError("组合工作流物料透传形成循环")
    node = nodes[node_uuid]
    source_handle = handles[source_handle_uuid]
    resolving.add(key)
    try:
        # 实验操作可以在内部选料并显式返回物料，不一定有同名输入。
        # 只有存在同名输入时才沿父图追溯；隐式透传缺失输入仍由下方校验拒绝。
        has_same_name_input = any(
            handle.get("workflow_node_template_uuid") == source_handle.get("workflow_node_template_uuid")
            and handle.get("handle_key") == source_handle.get("handle_key")
            and handle.get("io_type") == "target"
            for handle in handles.values()
        )
        if has_same_name_input and (
            _field(node, "type") == "workflow"
            or _material_io_mode(node, templates=templates) in {"unload", "refill"}
        ):
            target_handle = _same_name_input(source_handle, handles=handles)
            target_handle_uuid = str(_field(target_handle, "uuid"))
            incoming = incoming_by_target.get((node_uuid, target_handle_uuid))
            if incoming is not None:
                schema = _resolved_producer_schema(
                    node_uuid=str(_field(incoming, "source_node_uuid")),
                    source_handle_uuid=str(_field(incoming, "source_handle_uuid")),
                    nodes=nodes,
                    templates=templates,
                    handles=handles,
                    effective_params=effective_params,
                    incoming_by_target=incoming_by_target,
                    cache=cache,
                    resolving=resolving,
                )
                cache[key] = schema
                return schema
        schema = _producer_schema(
            node=node,
            source_handle=source_handle,
            templates=templates,
            handles=handles,
            effective_param=effective_params[node_uuid],
        )
        cache[key] = schema
        return schema
    finally:
        resolving.remove(key)


def _validate_workflow_input_linearity(
    validated_workflow_io: ValidatedWorkflowIO | None,
    *,
    reachability: Mapping[str, frozenset[str]],
    ignored_node_uuids: frozenset[str] = frozenset(),
    exclusive_branches: Mapping[str, frozenset[tuple[str, int]]] | None = None,
) -> None:
    """把工作流输入物料绑定计入物理消费路径。

    参数说明：``validated_workflow_io`` 是公共校验器产生的规范合同和节点绑定，
    ``reachability`` 是候选 DAG 的传递可达关系；局部 ``schemas`` 按参数名索引
    输入 Schema，``consumers`` 收集每个物料输入的节点消费身份。返回：无；同一
    物料输入存在任意一对无严格先后关系的动作时抛出稳定物料流分叉异常。缺失
    规范事实只用于旧内部调用兼容，不产生推测。
    """

    if validated_workflow_io is None:
        return
    schemas = {
        parameter["name"]: parameter["schema"]
        for parameter in validated_workflow_io.input_contract.to_dict()["parameters"]
    }
    consumers: dict[str, list[str]] = defaultdict(list)
    for node_uuid, bindings in validated_workflow_io.input_bindings.items():
        # 组合工作流内部节点的绑定是执行计划（ExecutionPlan）
        # 展开事实；父图物料流权威仍由调用边界节点承担，
        # 不能把两者重复计为两个物理消费者。
        if node_uuid in ignored_node_uuids:
            continue
        for binding in bindings.values():
            parameter = binding["parameter"]
            if schema_contains_resource_slot(schemas[parameter]):
                consumers[parameter].append(node_uuid)
    unordered_parameters = sorted(
        parameter
        for parameter, consumer_nodes in consumers.items()
        if not _consumer_nodes_are_strictly_ordered(
            consumer_nodes,
            reachability=reachability,
            exclusive_branches=exclusive_branches,
        )
    )
    if unordered_parameters:
        raise MaterialGraphValidationError(
            "material_flow_fan_out",
            "工作流输入物料不能绑定多个无序物理消费者："
            + ", ".join(unordered_parameters),
        )


def _composite_internal_node_uuids(nodes: Sequence[Any]) -> frozenset[str]:
    """找出展示父级链中位于组合调用边界下的内部节点。

    参数：``nodes`` 是候选图全部节点。返回：仅含展开内部节点
    UUID 的不可变集合，组合调用边界本身不在其中。异常：无；图形状
    仍由上层通用校验器负责。
    """

    def value(node: Any, name: str) -> Any:
        """兼容读取普通映射和 Pydantic 节点字段。"""

        return node.get(name) if isinstance(node, Mapping) else getattr(node, name, None)

    by_uuid = {
        str(value(node, "uuid")): node
        for node in nodes
        if isinstance(value(node, "uuid"), str)
    }
    composite_roots: set[str] = set()
    for node_uuid, node in by_uuid.items():
        meta_data = value(node, "meta_data")
        unilab = (
            meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        )
        if isinstance(unilab, Mapping) and isinstance(
            unilab.get("composite"), Mapping
        ):
            composite_roots.add(node_uuid)
    internal: set[str] = set()
    for node_uuid, node in by_uuid.items():
        parent_uuid = value(node, "parent_uuid")
        visited: set[str] = set()
        while isinstance(parent_uuid, str) and parent_uuid not in visited:
            if parent_uuid in composite_roots:
                internal.add(node_uuid)
                break
            visited.add(parent_uuid)
            parent = by_uuid.get(parent_uuid)
            parent_uuid = value(parent, "parent_uuid") if parent is not None else None
    return frozenset(internal)


def _node_reachability(
    *,
    nodes: Sequence[Any],
    edges: Sequence[Any],
) -> dict[str, frozenset[str]]:
    """计算候选 DAG 中每个工作流节点（WorkflowNode）的严格后继闭包。

    参数说明：``nodes`` 和 ``edges`` 是同一候选五集合中的节点与有向边；局部
    ``successors`` 保存直接后继，``pending`` 保存尚未展开的节点。返回：节点
    UUID 到全部可达后继 UUID 的不可变集合；字段形状非法时由 ``_field`` 抛出
    ``TypeError``，环由候选图的通用 DAG 校验负责拒绝，本函数不掩盖该诊断。
    """

    successors: dict[str, set[str]] = {
        str(_field(node, "uuid")): set() for node in nodes
    }
    for edge in edges:
        source_node_uuid = str(_field(edge, "source_node_uuid"))
        target_node_uuid = str(_field(edge, "target_node_uuid"))
        successors.setdefault(source_node_uuid, set()).add(target_node_uuid)
        successors.setdefault(target_node_uuid, set())
    reachability: dict[str, frozenset[str]] = {}
    for node_uuid, direct_successors in successors.items():
        visited: set[str] = set()
        pending = list(direct_successors)
        while pending:
            successor_uuid = pending.pop()
            if successor_uuid in visited:
                continue
            visited.add(successor_uuid)
            pending.extend(successors.get(successor_uuid, ()))
        reachability[node_uuid] = frozenset(visited)
    return reachability


def _condition_branch_ancestry(nodes: Sequence[Any]) -> dict[str, frozenset[tuple[str, int]]]:
    """为每个节点求出它所在的全部（条件节点，分支序号）。

    参数：``nodes`` 是候选图全部节点；条件节点 ``param.branches[i].node_uuids`` 列出该
    分支的直接成员，嵌套在成员（分组、内层条件、循环）之下的节点沿 ``parent_uuid``
    继承同一归属。返回：节点 UUID 到归属集合的映射；不在任何分支内的节点为空集合。
    """

    def value(node: Any, name: str) -> Any:
        return node.get(name) if isinstance(node, Mapping) else getattr(node, name, None)

    direct: dict[str, set[tuple[str, int]]] = defaultdict(set)
    parents: dict[str, str] = {}
    for node in nodes:
        node_uuid = value(node, "uuid")
        if not isinstance(node_uuid, str):
            continue
        parent = value(node, "parent_uuid")
        if isinstance(parent, str) and parent:
            parents[node_uuid] = parent
        if str(value(node, "type") or "") != "condition":
            continue
        param = value(node, "param")
        branches = param.get("branches") if isinstance(param, Mapping) else None
        for index, branch in enumerate(branches if isinstance(branches, list) else []):
            members = branch.get("node_uuids") if isinstance(branch, Mapping) else None
            for member in members if isinstance(members, list) else []:
                if isinstance(member, str):
                    direct[member].add((node_uuid, index))
    ancestry: dict[str, frozenset[tuple[str, int]]] = {}
    for node in nodes:
        node_uuid = value(node, "uuid")
        if not isinstance(node_uuid, str):
            continue
        found: set[tuple[str, int]] = set()
        current: str | None = node_uuid
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            found |= direct.get(current, set())
            current = parents.get(current)
        ancestry[node_uuid] = frozenset(found)
    return ancestry


def _mutually_exclusive(
    left: str, right: str, exclusive_branches: Mapping[str, frozenset[tuple[str, int]]] | None
) -> bool:
    """两节点位于同一条件节点的不同分支时，运行时至多执行其中一个。"""

    if not exclusive_branches:
        return False

    def by_condition(node_uuid: str) -> dict[str, set[int]]:
        grouped: dict[str, set[int]] = defaultdict(set)
        for condition, branch in exclusive_branches.get(node_uuid, frozenset()):
            grouped[condition].add(branch)
        return grouped

    left_groups, right_groups = by_condition(left), by_condition(right)
    for condition, left_set in left_groups.items():
        right_set = right_groups.get(condition)
        # 只在两边各自唯一归属于该条件的一个分支且分支不同时判定互斥；
        # 同一节点出现在多个分支属于异常图，不据此放行。
        if right_set and len(left_set) == 1 and len(right_set) == 1 and left_set != right_set:
            return True
    return False


def _consumer_nodes_are_strictly_ordered(
    consumer_nodes: Sequence[str],
    *,
    reachability: Mapping[str, frozenset[str]],
    exclusive_branches: Mapping[str, frozenset[tuple[str, int]]] | None = None,
) -> bool:
    """判断同一物料来源的所有消费者是否形成严格全序。

    参数说明：``consumer_nodes`` 保留每条物料消费绑定的节点身份，
    ``reachability`` 提供候选 DAG 的传递可达关系。返回：零或一个消费者时为真；
    多个消费者仅在每一对节点恰有一个方向可达时为真。相同节点重复消费、无序
    兄弟或环中的双向可达都返回假，继续由调用者报告 ``material_flow_fan_out``。
    """

    for index, left_node_uuid in enumerate(consumer_nodes):
        for right_node_uuid in consumer_nodes[index + 1 :]:
            if left_node_uuid != right_node_uuid and _mutually_exclusive(
                left_node_uuid, right_node_uuid, exclusive_branches
            ):
                continue
            left_before_right = right_node_uuid in reachability.get(
                left_node_uuid,
                frozenset(),
            )
            right_before_left = left_node_uuid in reachability.get(
                right_node_uuid,
                frozenset(),
            )
            if left_before_right == right_before_left:
                return False
    return True


def _material_io_mode(
    node: Any,
    *,
    templates: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """读取物料装卸节点的 mode；不是 material_io 时返回 None。"""

    template_uuid = (
        node.get("workflow_node_template_uuid")
        if isinstance(node, Mapping)
        else getattr(node, "workflow_node_template_uuid", None)
    )
    template = (
        templates.get(template_uuid)
        if isinstance(template_uuid, str) and template_uuid
        else None
    )
    template_type = None
    if template is not None:
        template_type = template.get("node_type") or template.get("type")
    node_type = node.get("type") if isinstance(node, Mapping) else getattr(node, "type", None)
    if template_type != "material_io" and node_type != "material_io":
        return None
    param = node.get("param") if isinstance(node, Mapping) else getattr(node, "param", None)
    if not isinstance(param, Mapping):
        return None
    mode = param.get("mode")
    return mode if isinstance(mode, str) else None


def _is_material_source_node(
    node: Any,
    *,
    templates: Mapping[str, Mapping[str, Any]],
) -> bool:
    """判断生产节点是否为框架物料来源（MaterialSource）。

    参数说明：``node`` 是普通图字典或 Pydantic 节点，``templates`` 是目录索引。
    局部 ``template`` 提供权威节点种类。返回：模板或旧节点类型明确为
    ``material_source`` 时为真。
    """

    # 计算节点等内置节点允许不绑定节点模板；缺少模板不是物料图错误，只表示
    # 不能从模板目录判定为物料来源，随后继续兼容旧 ``type`` 字段。
    template_uuid = (
        node.get("workflow_node_template_uuid")
        if isinstance(node, Mapping)
        else getattr(node, "workflow_node_template_uuid", None)
    )
    template = (
        templates.get(template_uuid)
        if isinstance(template_uuid, str) and template_uuid
        else None
    )
    if template is not None:
        template_type = template.get("node_type") or template.get("type")
        if template_type == "material_source":
            return True
        if template_type == "material_io":
            param = node.get("param") if isinstance(node, Mapping) else node.param
            return isinstance(param, Mapping) and param.get("mode") == "load"
    node_type = node.get("type") if isinstance(node, Mapping) else node.type
    if node_type == "material_io":
        param = node.get("param") if isinstance(node, Mapping) else node.param
        return isinstance(param, Mapping) and param.get("mode") == "load"
    return node_type == "material_source"


def _is_implicit_passthrough(handle: Mapping[str, Any]) -> bool:
    """读取连接点（Handle）的服务端隐式透传标记。

    参数说明：``handle`` 是来源连接点目录投影；局部 ``meta_data`` 与 ``unilab``
    是闭合元数据层。返回：仅精确布尔值 ``True`` 表示隐式透传。
    """

    meta_data = handle.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return isinstance(unilab, Mapping) and unilab.get("implicit_passthrough") is True


def _same_name_input(
    source_handle: Mapping[str, Any],
    *,
    handles: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """查找隐式物料输出对应的唯一同名输入连接点。

    参数说明：``source_handle`` 提供父模板和业务键，``handles`` 是完整目录索引；
    局部 ``matches`` 收集同父模板、同 ``handle_key`` 的目标连接点。返回：唯一
    输入连接点；缺失或歧义时抛出 ``TypeError``，禁止猜测物料保证。
    """

    matches = [
        handle
        for handle in handles.values()
        if handle.get("workflow_node_template_uuid")
        == source_handle.get("workflow_node_template_uuid")
        and handle.get("handle_key") == source_handle.get("handle_key")
        and handle.get("io_type") == "target"
    ]
    if len(matches) != 1:
        raise TypeError("隐式物料输出缺少唯一同名输入连接点")
    return matches[0]


__all__ = [
    "MaterialGraphValidationError",
    "validate_material_graph",
    "validate_material_graph_projection",
]
