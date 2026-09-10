"""从应用工作流图构造唯一、不可变的执行计划（ExecutionPlan）。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from unilabos.workflow._execution_plan_graph import (
    ExecutionPlanBuildError,
    ExecutionPlanGraphNormalizer,
    executor_kind,
    final_target_data_key,
)
from unilabos.workflow.store import StoreConflict

PLAN_VERSION = 1


class ExecutionPlanBuilder:
    """隐藏拓扑收敛、运行连接点实例化和短期物料需求投影。"""

    def build(
        self,
        graph: Mapping[str, Any],
        *,
        run_mode: str,
        target_node_uuid: str | None,
        input_value: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """构造版本化执行计划和首次作业集合。

        参数：``graph`` 是冻结应用图，``run_mode`` 是任务运行模式，
        ``target_node_uuid`` 是单节点模式目标，``input_value`` 是本 Task 的
        已提交输入（用于来源数量绑定）。返回：不含实时库存、预留、执行
        占用或物理结果的计划与作业。异常：图、物料来源（MaterialSource）或
        单节点选择非法时抛 ``ExecutionPlanBuildError``/``StoreConflict``。
        """

        nodes = self._index(graph.get("nodes"), "nodes")
        templates = self._index(graph.get("node_templates", []), "node_templates")
        handles = self._index(graph.get("handle_templates", []), "handle_templates")
        edges = self._objects(graph.get("edges", []), "edges")
        # ``kinds`` 是每个冻结节点的规范执行责任；虚拟节点不创建作业。
        kinds = {
            node_uuid: executor_kind(
                str(
                    (templates.get(node.get("workflow_node_template_uuid")) or {}).get(
                        "node_type"
                    )
                    or node.get("type")
                    or ""
                )
            )
            for node_uuid, node in nodes.items()
        }
        # ``material_sources`` 是由协调器承担的物料来源解析作业，不进入设备派发图。
        material_sources = {
            node_uuid: node
            for node_uuid, node in nodes.items()
            if node.get("disabled") is not True
            and kinds[node_uuid] == "material_source"
        }
        # ``active`` 只包含既有本地调度器能够执行的普通节点；物料来源由任务桥
        # 在普通动作之前统一完成任务物料准入（TaskMaterialAdmission）。组合
        # 组合工作流调用（CompositeWorkflowInvocation）仅保留父图层级与边界映射，
        # 其展开内部节点直接归属父工作流任务（WorkflowTask），自身不创建作业。
        active = {
            node_uuid: node
            for node_uuid, node in nodes.items()
            if node.get("disabled") is not True
            and kinds[node_uuid] not in {"group", "material_source", "workflow"}
        }
        # ``planned_graph_nodes`` 同时保留协调责任与普通执行责任，使来源运行连接点
        # 和来源到首消费动作的直连边成为冻结执行计划（ExecutionPlan）事实。
        planned_graph_nodes = {**material_sources, **active}
        graph_normalizer = ExecutionPlanGraphNormalizer()
        flattened_edges, composite_params = graph_normalizer.flatten_composite_edges(
            nodes=nodes,
            edges=edges,
            handles=handles,
        )
        runtime_handles, runtime_handle_ids = graph_normalizer.runtime_handles(
            active=planned_graph_nodes,
            handles=handles,
        )
        planned_edges = graph_normalizer.contract_edges(
            nodes=nodes,
            active=planned_graph_nodes,
            edges=flattened_edges,
            handles=handles,
            runtime_handle_ids=runtime_handle_ids,
        )
        graph_order = graph_normalizer.topological_order(
            planned_graph_nodes,
            planned_edges,
        )
        requirements, material_params, material_binding_targets = (
            self._material_source_inputs(
                nodes=nodes,
                active=active,
                kinds=kinds,
                edges=planned_edges,
                topological_order=graph_order,
                input_value=input_value or {},
            )
        )
        # 来源与普通节点都先遵守冻结图拓扑，再由计划作业序列保证全部协调责任先于
        # 任何物理动作；这样不会让无边来源受创建时间影响而落到动作之后。
        ordered_sources = [
            node_uuid for node_uuid in graph_order if node_uuid in material_sources
        ]
        ordered = [node_uuid for node_uuid in graph_order if node_uuid in active]
        if run_mode == "single_node":
            if target_node_uuid is None:
                if not graph_order:
                    raise StoreConflict("workflow has no enabled nodes")
                target_node_uuid = graph_order[0]
            if target_node_uuid in material_sources:
                ordered_sources = [target_node_uuid]
                ordered = []
            elif target_node_uuid in active:
                ordered_sources = []
                ordered = [target_node_uuid]
            else:
                raise StoreConflict("single_node target is not enabled")
            planned_edges = []
            runtime_handles = [
                handle
                for handle in runtime_handles
                if handle["node_uuid"] == target_node_uuid
            ]

        planned_nodes: list[dict[str, Any]] = []
        jobs: list[dict[str, Any]] = []
        handles_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for handle in runtime_handles:
            handles_by_node[handle["node_uuid"]].append(handle)
        # ``planned_order`` 把协调器责任和物理执行责任放进同一持久作业序列。
        planned_order = [*ordered_sources, *ordered]
        for index, node_uuid in enumerate(planned_order):
            node = nodes[node_uuid]
            kind = kinds[node_uuid]
            template = templates.get(node.get("workflow_node_template_uuid")) or {}
            policy = dict(node.get("execution_policy") or {})
            # ``planned_param`` 是任务提交时冻结的动作输入，不是运行时回退视图。
            planned_param = dict(node.get("param") or {})
            planned_param.update(composite_params.get(node_uuid, {}))
            # ``fixed_params`` 是固定的 ``existing`` 物料来源（MaterialSource）沿物料占位符链投影的实例引用。
            fixed_params = material_params.get(node_uuid, {})
            planned_param.update(fixed_params)
            node_handles = handles_by_node.get(node_uuid, [])
            planned_node: dict[str, Any] = {
                "uuid": node_uuid,
                "topological_index": index,
                "kind": kind,
                "param": planned_param,
                "execution_policy": policy,
                "inputs": [
                    {
                        "handle_uuid": handle["uuid"],
                        "data_key": final_target_data_key(handle["data_key"]),
                        "type": handle["type"],
                        "required": handle["required"],
                    }
                    for handle in node_handles
                    if handle["io_type"] == "target"
                ],
                "source_handle_uuids": [
                    handle["uuid"]
                    for handle in node_handles
                    if handle["io_type"] == "source"
                ],
            }
            if kind == "material_source":
                # ``custody_policy`` is authored as workflow metadata rather than
                # selector ``param`` (the backend selector schema is deliberately
                # closed).  Freeze it into the execution plan so the scheduler can
                # distinguish task-exclusive labware from reusable reagent sources
                # without reopening the applied graph at dispatch time.
                raw_meta_data = node.get("meta_data")
                unilab_meta = (
                    raw_meta_data.get("unilab", {})
                    if isinstance(raw_meta_data, Mapping)
                    else {}
                )
                if isinstance(unilab_meta, Mapping):
                    custody_policy = str(unilab_meta.get("custody_policy") or "").strip()
                    if custody_policy:
                        planned_node["custody_policy"] = custody_policy
            if kind == "device_action":
                planned_node.update(
                    self._device_action_contract(node, template=template)
                )
                # ``action_contract`` 来自模板投影保留元数据，而 ``template.schema``
                # 只承载 Backend 规范的 Goal 参数子模式。
                action_contract = self._frozen_action_contract(
                    template,
                    node_uuid=node_uuid,
                )
                planned_node["param_schema"] = action_contract
                raw_outputs = node.get("material_outputs", [])
                if raw_outputs:
                    if not isinstance(raw_outputs, Sequence) or isinstance(
                        raw_outputs, (str, bytes)
                    ) or any(not isinstance(item, Mapping) for item in raw_outputs):
                        raise ExecutionPlanBuildError(
                            "invalid_material_output",
                            f"节点输出物料声明必须是对象数组：{node_uuid}",
                        )
                    planned_node["material_outputs"] = [
                        deepcopy(dict(item)) for item in raw_outputs
                    ]
                for field_name in ("material_preconditions", "material_effects"):
                    raw_conditions = node.get(field_name, [])
                    if raw_conditions:
                        if not isinstance(raw_conditions, Sequence) or isinstance(
                            raw_conditions, (str, bytes)
                        ) or any(not isinstance(item, Mapping) for item in raw_conditions):
                            raise ExecutionPlanBuildError(
                                "invalid_material_condition",
                                f"节点 {field_name} 必须是对象数组：{node_uuid}",
                            )
                        planned_node[field_name] = [
                            deepcopy(dict(item)) for item in raw_conditions
                        ]
            if node.get("material_uuid") is not None:
                planned_node["material_uuid"] = node["material_uuid"]
            if node.get("script") is not None:
                planned_node["script"] = node["script"]
            if kind != "device_action" and template.get("schema") is not None:
                planned_node["param_schema"] = template["schema"]
            if requirements.get(node_uuid):
                planned_node["material_requirements"] = requirements[node_uuid]
            if material_binding_targets.get(node_uuid):
                planned_node["material_binding_targets"] = (
                    material_binding_targets[node_uuid]
                )
            planned_nodes.append(planned_node)
            jobs.append(
                {
                    "uuid": str(uuid4()),
                    "workflow_node_uuid": node_uuid,
                    "topological_index": index,
                    "executor_kind": kind,
                    "execution_policy": policy,
                    "execution_timeout_seconds": 0,
                    "param": planned_param,
                }
            )
        plan: dict[str, Any] = {
            "version": PLAN_VERSION,
            "run_mode": run_mode,
            "nodes": planned_nodes,
            "edges": planned_edges,
            "handles": runtime_handles,
        }
        if target_node_uuid is not None:
            plan["target_node_uuid"] = target_node_uuid
        return plan, jobs

    def _material_source_inputs(
        self,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        active: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        edges: Sequence[Mapping[str, Any]],
        topological_order: Sequence[str],
        input_value: Mapping[str, Any],
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[str, dict[str, dict[str, str]]],
        dict[str, list[dict[str, str]]],
    ]:
        """投影 ``existing`` 物料来源（MaterialSource）的静态准入输入。

        参数：完整节点、活动节点、执行种类、平面计划边与拓扑顺序来自同一应用
        图。返回：遗留库存预留（inventory_reservation）需求、仅固定来源可提前
        写入的最终物料引用参数，以及自动来源成功准入后的作业参数目标；它不是
        任务物料预留（TaskMaterialReservation）。异常：create_new、选择器 UUID
        非法或物料流分叉/循环时抛稳定计划错误。
        """

        # ``outgoing`` 保留每条物料边的目标节点与最终参数键。
        outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for edge in edges:
            if (
                edge.get("dependency_only") is not True
                and edge.get("source_type") == "ResourceSlot"
                and edge.get("target_type") == "ResourceSlot"
            ):
                outgoing[str(edge["source_node_uuid"])].append(
                    (
                        str(edge["target_node_uuid"]),
                        final_target_data_key(str(edge.get("target_data_key") or "")),
                    )
                )
        order = {node_uuid: index for index, node_uuid in enumerate(topological_order)}
        requirements: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # ``material_params`` 把具体物料身份绑定到首消费动作的参数键。
        material_params: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
        # ``binding_targets`` 只描述运行时绑定应写到哪里，不包含库存选择结果。
        binding_targets: dict[str, list[dict[str, str]]] = defaultdict(list)
        target_owners: dict[tuple[str, str], str] = {}
        for source_uuid, node in nodes.items():
            if kinds[source_uuid] != "material_source" or node.get("disabled") is True:
                continue
            selector = node.get("param")
            if not isinstance(selector, Mapping):
                raise ExecutionPlanBuildError(
                    "invalid_material_source_selector", "物料来源选择器必须是对象"
                )
            if selector.get("mode") != "existing":
                raise ExecutionPlanBuildError(
                    "unsupported_material_source_mode",
                    "短期调度桥只接受固定的 existing 物料来源",
                )
            material_uuid = str(selector.get("material_uuid") or "")
            if material_uuid:
                material_uuid = self._selector_uuid(
                    material_uuid,
                    code="invalid_material_uuid",
                    message="物料来源 UUID 非法",
                )
                requirement = {"instance_uuid": material_uuid}
            else:
                mount = selector.get("mount")
                if not isinstance(mount, Mapping):
                    raise ExecutionPlanBuildError(
                        "invalid_material_source_selector",
                        "自动物料来源缺少挂载点",
                    )
                template_uuid = self._selector_uuid(
                    selector.get("resource_template_uuid"),
                    code="invalid_material_source_selector",
                    message="物料来源资源模板 UUID 非法",
                )
                mount_uuid = self._selector_uuid(
                    mount.get("uuid"),
                    code="invalid_material_source_selector",
                    message="物料来源挂载点 UUID 非法",
                )
                site_uuid = ""
                if selector.get("site") is not None:
                    site_uuid = self._selector_uuid(
                        selector.get("site"),
                        code="invalid_material_source_selector",
                        message="物料来源库位 UUID 非法",
                    )
                raw_slot_uuids = selector.get("slot_range") or []
                if not isinstance(raw_slot_uuids, Sequence) or isinstance(
                    raw_slot_uuids, (str, bytes)
                ):
                    raise ExecutionPlanBuildError(
                        "invalid_material_source_selector",
                        "物料来源库位范围必须是数组",
                    )
                slot_uuids = [
                    self._selector_uuid(
                        value,
                        code="invalid_material_source_selector",
                        message="物料来源库位范围 UUID 非法",
                    )
                    for value in raw_slot_uuids
                ]
                if site_uuid and slot_uuids:
                    raise ExecutionPlanBuildError(
                        "invalid_material_source_selector",
                        "物料来源不能同时指定库位和库位范围",
                    )
                requirement = {
                    "template_id": template_uuid,
                    "mount_uuid": mount_uuid,
                    "site_uuid": site_uuid,
                    "slot_uuids": slot_uuids,
                }
            unilab_meta = node.get("meta_data", {}).get("unilab", {})
            quantity_parameter = (
                unilab_meta.get("quantity_parameter")
                if isinstance(unilab_meta, Mapping)
                else None
            )
            if quantity_parameter:
                quantity_value = input_value.get(quantity_parameter)
                if isinstance(quantity_value, bool) or not isinstance(
                    quantity_value, (int, float)
                ) or quantity_value <= 0:
                    raise ExecutionPlanBuildError(
                        "invalid_material_quantity",
                        f"物料数量参数必须是正数：{quantity_parameter}",
                    )
                requirements[source_uuid].append(
                    {
                        "template_id": template_uuid,
                        "quantity": float(quantity_value),
                        "unit": str(
                            unilab_meta.get("quantity_unit") or ""
                        ),
                    }
                )
            # 短期遗留预留（inventory_reservation）归属来源协调责任；普通设备
            # 动作只消费已经准入的稳定物料引用，不再次取得任务级预留。
            requirements[source_uuid].append(requirement)
            consumer_bindings = self._device_consumer_bindings(
                source_uuid,
                active=active,
                kinds=kinds,
                outgoing=outgoing,
                order=order,
            )
            for consumer, param_key in consumer_bindings:
                if not param_key:
                    raise ExecutionPlanBuildError(
                        "invalid_execution_graph",
                        "物料占位符目标连接点缺少参数键",
                    )
                target_key = (consumer, param_key)
                existing_owner = target_owners.get(target_key)
                if existing_owner is not None and existing_owner != source_uuid:
                    raise ExecutionPlanBuildError(
                        "invalid_execution_graph",
                        "多个 existing 物料来源冲突写入同一动作参数",
                    )
                target_owners[target_key] = source_uuid
                binding_targets[source_uuid].append(
                    {"workflow_node_uuid": consumer, "param_key": param_key}
                )
                if material_uuid:
                    material_params[consumer][param_key] = {"uuid": material_uuid}
        return dict(requirements), dict(material_params), dict(binding_targets)

    @staticmethod
    def _selector_uuid(value: Any, *, code: str, message: str) -> str:
        """把选择器身份规范为非 nil UUID。

        参数：``value`` 是冻结选择器字段；``code``/``message`` 是稳定计划诊断。
        返回：规范小写 UUID。异常：空值、非法值或 nil UUID 抛计划错误。
        """

        try:
            identity = UUID(str(value or ""))
        except ValueError as exc:
            raise ExecutionPlanBuildError(code, message) from exc
        if identity.int == 0:
            raise ExecutionPlanBuildError(code, message)
        return str(identity)

    @staticmethod
    def _device_consumer_bindings(
        source_uuid: str,
        *,
        active: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        outgoing: Mapping[str, Sequence[tuple[str, str]]],
        order: Mapping[str, int],
    ) -> list[tuple[str, str]]:
        """沿物料占位符（ResourceSlot）链寻找首层设备动作集合。

        参数：来源 UUID、活动节点、执行种类、带目标参数键的邻接表和稳定拓扑
        排名描述一条冻结物料链。返回：按拓扑稳定排序的首层启用
        ``device_action`` UUID 及其物料参数键；复合工作流展开形成隐式透传时，
        同一来源可直接绑定多个严格有序消费者。异常：循环时抛计划错误。
        """

        pending = [source_uuid]
        visited: set[str] = set()
        consumers: list[tuple[str, str]] = []
        while pending:
            current = pending.pop(0)
            if current in visited:
                raise ExecutionPlanBuildError(
                    "material_flow_not_linear", "物料占位符链含循环"
                )
            visited.add(current)
            targets = list(outgoing.get(current, ()))
            targets.sort(
                key=lambda target: (
                    order.get(target[0], len(order)),
                    target[0],
                    target[1],
                )
            )
            for target_uuid, target_param_key in targets:
                if target_uuid in active and kinds[target_uuid] == "device_action":
                    binding = (target_uuid, target_param_key)
                    if binding not in consumers:
                        consumers.append(binding)
                    continue
                pending.append(target_uuid)
        return consumers

    @staticmethod
    def _frozen_action_contract(
        template: Mapping[str, Any], *, node_uuid: str
    ) -> dict[str, Any]:
        """从节点模板保留元数据冻结完整动作合同（Action Contract）。

        参数：``template`` 是应用图冻结的工作流节点模板，``node_uuid`` 是诊断
        使用的动作节点身份。返回：与模板容器隔离的完整动作 Schema envelope。
        异常：保留元数据、完整合同或 ``properties.goal`` 缺失/非对象时抛
        ``ExecutionPlanBuildError``；禁止回退实时注册表或 Goal 子模式。
        """

        # ``metadata``/``unilab`` 定位模板投影保留的 Uni-Lab 执行合同边界。
        metadata = template.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        # ``contract`` 是本工作流任务（WorkflowTask）唯一可冻结的完整动作合同。
        contract = (
            unilab.get("action_contract_schema")
            if isinstance(unilab, Mapping)
            else None
        )
        properties = (
            contract.get("properties") if isinstance(contract, Mapping) else None
        )
        # ``goal_schema`` 只用于证明完整合同能被动作物料锁编译器安全消费。
        goal_schema = (
            properties.get("goal") if isinstance(properties, Mapping) else None
        )
        if not isinstance(goal_schema, Mapping):
            raise ExecutionPlanBuildError(
                "invalid_action_contract",
                f"设备动作模板缺少完整动作合同：{node_uuid}",
            )
        return deepcopy(dict(contract))

    @staticmethod
    def _device_action_contract(
        node: Mapping[str, Any],
        *,
        template: Mapping[str, Any],
    ) -> dict[str, Any]:
        """冻结设备动作执行器和动作合同。

        参数：``node`` 是应用图设备动作节点。返回：显式固定执行器
        （Executor）身份、动作名与规范动作类型。异常：执行器绑定
        （ExecutorBinding）缺失、非 fixed 或设备身份为空时失败关闭；
        ``material_uuid`` 是物料身份，不得作为执行器回退。
        """

        metadata = node.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        binding = (
            unilab.get("executor_binding") if isinstance(unilab, Mapping) else None
        )
        device_id = ""
        if isinstance(binding, Mapping) and binding.get("mode") == "fixed":
            device_id = str(binding.get("device_id") or "").strip()
        if not device_id:
            raise ExecutionPlanBuildError(
                "invalid_executor_binding",
                "设备动作缺少显式固定执行器绑定",
            )
        action_type = str(node.get("action_type") or "").strip()
        frozen_template_type = str(template.get("type") or "").strip()
        if not action_type and frozen_template_type.startswith("UniLabJsonCommand"):
            action_type = frozen_template_type
        return {
            "device_id": device_id,
            "action_name": node.get("action_name"),
            "action_type": action_type or "UniLabJsonCommand",
        }

    @staticmethod
    def _index(value: Any, field: str) -> dict[str, Mapping[str, Any]]:
        """按 UUID 索引对象序列。

        参数：``value`` 是边界数组，``field`` 是诊断字段。返回：身份映射。
        异常：非数组、非对象、空身份或重复身份时抛计划错误。
        """

        objects = ExecutionPlanBuilder._objects(value, field)
        result: dict[str, Mapping[str, Any]] = {}
        for item in objects:
            identity = str(item.get("uuid") or "")
            if not identity or identity in result:
                raise ExecutionPlanBuildError(
                    "invalid_execution_graph", f"{field} 身份缺失或重复"
                )
            result[identity] = item
        return result

    @staticmethod
    def _objects(value: Any, field: str) -> list[Mapping[str, Any]]:
        """校验对象数组。

        参数：``value`` 是边界值，``field`` 是诊断字段。返回：对象列表。
        异常：类型不符时抛 ``ExecutionPlanBuildError``。
        """

        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ExecutionPlanBuildError(
                "invalid_execution_graph", f"{field} 必须是数组"
            )
        if any(not isinstance(item, Mapping) for item in value):
            raise ExecutionPlanBuildError(
                "invalid_execution_graph", f"{field} 成员必须是对象"
            )
        return list(value)


__all__ = [
    "PLAN_VERSION",
    "ExecutionPlanBuildError",
    "ExecutionPlanBuilder",
]
