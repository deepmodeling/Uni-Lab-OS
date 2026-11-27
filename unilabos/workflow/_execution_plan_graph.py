"""执行计划（ExecutionPlan）的纯图归一化内部实现。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid5

from unilabos.workflow.store import StoreConflict


class ExecutionPlanBuildError(StoreConflict):
    """应用图不能安全转换为执行计划（ExecutionPlan）。"""

    def __init__(self, code: str, message: str) -> None:
        """建立计划错误。

        参数：``code`` 是稳定失败码，``message`` 是中文诊断。返回：无。
        异常：无；构造器只保存已经判定的失败关闭结果。
        """

        super().__init__(message)
        self.code = code


class ExecutionPlanGraphNormalizer:
    """收敛虚拟节点、实例化连接点（Handle）并生成确定性拓扑。"""

    def flatten_control_members(self, nodes: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        """把条件中的组合调用替换为真实作业成员，保留原展示父级。"""
        result = deepcopy(dict(nodes))

        def expand(identity: str, boundary: str, visiting: frozenset[str] = frozenset()) -> list[str]:
            node = result.get(identity, {})
            if executor_kind(str(node.get("type") or "")) != "workflow":
                return [identity]
            if identity in visiting:
                raise ExecutionPlanBuildError("invalid_control_region", "组合边界包含循环")
            if boundary == "members":
                # 分支跳过时也必须跳过实验操作内部的嵌套条件及其动作。
                members: list[str] = []
                pending = [identity]
                seen = set(visiting)
                while pending:
                    parent = pending.pop()
                    if parent in seen:
                        raise ExecutionPlanBuildError("invalid_control_region", "组合成员包含循环")
                    seen.add(parent)
                    for child, value in result.items():
                        if value.get("parent_uuid") != parent:
                            continue
                        pending.append(child)
                        if executor_kind(str(value.get("type") or "")) not in {"workflow", "group"}:
                            members.append(child)
                if not members:
                    raise ExecutionPlanBuildError("invalid_control_region", "组合调用缺少可执行成员")
                return members
            else:
                mappings = node.get("meta_data", {}).get("unilab", {}).get("composite", {}).get("structural_mappings", {})
                field = "entry_targets" if boundary == "entry" else "completion_sources"
                children = [str(item["workflow_node_uuid"]) for item in mappings.get(field, [])]
            if not children:
                raise ExecutionPlanBuildError("invalid_control_region", "组合调用缺少可执行边界")
            return list(dict.fromkeys(child for key in children for child in expand(key, boundary, visiting | {identity})))

        for node in result.values():
            if executor_kind(str(node.get("type") or "")) != "condition":
                continue
            params = node.get("param", {})
            for branch in params.get("branches", []):
                for field, boundary in (("node_uuids", "members"), ("entry_node_uuids", "entry"), ("exit_node_uuids", "exit")):
                    branch[field] = list(dict.fromkeys(child for key in branch.get(field, []) for child in expand(str(key), boundary)))
            for field, boundary in (("predecessor_node_uuids", "exit"), ("successor_node_uuids", "entry")):
                if field in params:
                    params[field] = list(dict.fromkeys(child for key in params[field] for child in expand(str(key), boundary)))
        return result

    def flatten_composite_edges(
        self,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """把组合工作流调用（CompositeWorkflowInvocation）边界改写为平面边。

        参数：``nodes``/``edges``/``handles`` 来自同一已应用冻结图。返回：不再
        经过组合调用虚拟节点的业务值边与必须投影到实际动作的静态参数。异常：
        边界映射引用缺失节点、连接点或入参提供者不唯一时抛
        ``ExecutionPlanBuildError``；禁止在运行时猜测组合边界。
        """

        flattened = [dict(edge) for edge in edges]
        param_overrides: dict[str, dict[str, Any]] = defaultdict(dict)
        invocations = [
            (node_uuid, node)
            for node_uuid, node in nodes.items()
            if executor_kind(str(node.get("type") or "")) == "workflow"
        ]
        # 外层先投影参数边；内层随后把这些边继续改写到真实动作。
        # 若先删除内层边界，外层后补的动态参数边会遗留在已经处理过的虚拟节点上。
        invocations.sort(
            key=lambda item: self._composite_depth(item[0], nodes)
        )
        for invocation_uuid, invocation in invocations:
            flattened = self._flatten_invocation(
                invocation_uuid=invocation_uuid,
                invocation=invocation,
                edges=flattened,
                nodes=nodes,
                handles=handles,
                param_overrides=param_overrides,
            )
        return flattened, dict(param_overrides)

    def _flatten_invocation(
        self,
        *,
        invocation_uuid: str,
        invocation: Mapping[str, Any],
        edges: Sequence[Mapping[str, Any]],
        nodes: Mapping[str, Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
        param_overrides: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """收敛一个组合工作流调用（CompositeWorkflowInvocation）的全部边界。

        参数：调用身份、调用节点、当前边集、节点与连接点索引均来自同一平面
        快照；``param_overrides`` 收集静态透传值。返回：删除调用边界边并补齐
        内部值流、入口依赖和完成依赖后的边集。异常：组合元数据不闭合或映射
        引用快照外事实时失败关闭。
        """

        unilab = invocation.get("meta_data")
        unilab = unilab.get("unilab") if isinstance(unilab, Mapping) else None
        composite = unilab.get("composite") if isinstance(unilab, Mapping) else None
        if not isinstance(composite, Mapping):
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid",
                "组合工作流调用缺少冻结边界映射",
            )
        target_mappings = self._mapping_object(
            composite.get("target_mappings"), field="target_mappings"
        )
        source_mappings = self._mapping_object(
            composite.get("source_mappings"), field="source_mappings"
        )
        raw_structural = self._mapping_object(
            composite.get("structural_mappings"), field="structural_mappings"
        )
        structural = self._with_derived_structural_mappings(
            invocation_uuid=invocation_uuid,
            structural=raw_structural,
            nodes=nodes,
            edges=edges,
            handles=handles,
        )
        if structural is not raw_structural:
            # 结构边界解析（structural_boundary_nodes）读取节点上的冻结映射；
            # 补齐后的入口/完成来源须对本次调用可见，否则空映射仍被当作缺失。
            patched = dict(invocation)
            meta = dict(patched.get("meta_data") or {})
            unilab = dict(meta.get("unilab") or {})
            unilab["composite"] = {**composite, "structural_mappings": dict(structural)}
            meta["unilab"] = unilab
            patched["meta_data"] = meta
            nodes = {**nodes, invocation_uuid: patched}
        incoming = [
            edge for edge in edges if edge.get("target_node_uuid") == invocation_uuid
        ]
        outgoing = [
            edge for edge in edges if edge.get("source_node_uuid") == invocation_uuid
        ]
        retained = [
            dict(edge)
            for edge in edges
            if edge.get("target_node_uuid") != invocation_uuid
            and edge.get("source_node_uuid") != invocation_uuid
        ]
        generated: list[dict[str, Any]] = []

        incoming_by_handle: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for edge in incoming:
            incoming_by_handle[str(edge.get("target_handle_uuid") or "")].append(edge)
        contract = composite.get("contract_compatibility")
        contract_parameters = (
            contract.get("parameters") if isinstance(contract, Mapping) else None
        )
        input_handles_by_name = {
            str(item.get("name") or ""): str(item.get("handle_uuid") or "")
            for item in self._mapping_items(
                contract_parameters or [], field="contract.parameters"
            )
        }
        input_names_by_handle = {
            handle_uuid: name for name, handle_uuid in input_handles_by_name.items()
        }
        raw_invocation_param = invocation.get("param")
        invocation_param = (
            raw_invocation_param if isinstance(raw_invocation_param, Mapping) else {}
        )
        for boundary_handle_uuid, mapped_targets in target_mappings.items():
            providers = incoming_by_handle.get(boundary_handle_uuid, [])
            value_providers = self._value_provider_edges(
                providers,
                handles=handles,
            )
            targets = self._mapping_items(mapped_targets, field="target_mappings")
            for provider in providers:
                for target in targets:
                    generated.append(
                        self._rewired_edge(
                            invocation_uuid=invocation_uuid,
                            label="input",
                            source_edge=provider,
                            source_node_uuid=str(
                                provider.get("source_node_uuid") or ""
                            ),
                            source_handle_uuid=str(
                                provider.get("source_handle_uuid") or ""
                            ),
                            target_node_uuid=self._mapped_identity(
                                target,
                                "workflow_node_uuid",
                                nodes,
                            ),
                            target_handle_uuid=self._mapped_handle(
                                target, "target_handle_uuid", handles
                            ),
                        )
                    )
            parameter = input_names_by_handle.get(boundary_handle_uuid, "")
            if not value_providers and parameter in invocation_param:
                for target in targets:
                    self._project_static_parameter(
                        param_overrides=param_overrides,
                        node_uuid=self._mapped_identity(
                            target, "workflow_node_uuid", nodes
                        ),
                        handle_uuid=self._mapped_handle(
                            target, "target_handle_uuid", handles
                        ),
                        handles=handles,
                        value=invocation_param[parameter],
                    )

        from unilabos.workflow.workflow_boundary_binding import (
            WorkflowBoundaryBindingError,
            structural_boundary_nodes,
        )

        self._mapping_items(structural.get("entry_targets", []), field="entry_targets")
        mapped_input_handles = set(target_mappings)
        for edge in incoming:
            argument_edge = (
                str(edge.get("target_handle_uuid") or "") in mapped_input_handles
            )
            try:
                entry_ids = structural_boundary_nodes(
                    nodes, invocation_uuid, "entry_targets", validate_ancestry=False
                )
            except WorkflowBoundaryBindingError as error:
                raise ExecutionPlanBuildError(
                    "composite_boundary_mapping_invalid", str(error)
                ) from error
            for entry_uuid in entry_ids:
                # 参数既是内部值来源，也是整个调用的启动条件。若只连到内部
                # 使用参数的动作，前面的换刀/开夹动作会越过父条件提前派发。
                if argument_edge and any(
                    item.get("source_node_uuid") == edge.get("source_node_uuid")
                    and item.get("target_node_uuid") == entry_uuid
                    for item in generated
                ):
                    continue
                if nodes[entry_uuid].get("type") in {"repeat_until", "condition"}:
                    # A child beginning with a repeat enters through its controller,
                    # not through a fabricated ready Handle on a body template.
                    generated.append(self._rewired_edge(
                        invocation_uuid=invocation_uuid, label="control-entry",
                        source_edge={**edge, "dependency_only": True},
                        source_node_uuid=str(edge.get("source_node_uuid") or ""),
                        source_handle_uuid="", target_node_uuid=entry_uuid,
                        target_handle_uuid="",
                    ))
                    continue
                entry_handles = list(
                    {
                        str(item.get("target_handle_uuid") or "")
                        for candidate in nodes.values()
                        for item in candidate.get("meta_data", {})
                        .get("unilab", {})
                        .get("composite", {})
                        .get("structural_mappings", {})
                        .get("entry_targets", [])
                        if item.get("workflow_node_uuid") == entry_uuid
                    }
                )
                if not entry_handles:
                    entry_handles = [
                        str(h["uuid"])
                        for h in handles.values()
                        if h.get("workflow_node_template_uuid")
                        == nodes[entry_uuid].get("workflow_node_template_uuid")
                        and h.get("io_type") == "target"
                        and handle_data_key(h) == "ready"
                    ]
                if len(entry_handles) != 1:
                    raise ExecutionPlanBuildError(
                        "composite_boundary_mapping_invalid",
                        "child 入口缺少唯一 ready 连接点",
                    )
                generated.append(
                    self._rewired_edge(
                        invocation_uuid=invocation_uuid,
                        label="entry",
                        source_edge={**edge, "dependency_only": True}
                        if argument_edge
                        else edge,
                        source_node_uuid=str(edge.get("source_node_uuid") or ""),
                        source_handle_uuid=""
                        if argument_edge
                        else str(edge.get("source_handle_uuid") or ""),
                        target_node_uuid=entry_uuid,
                        target_handle_uuid=entry_handles[0],
                    )
                )

        completion_sources = self._mapping_items(
            structural.get("completion_sources", []), field="completion_sources"
        )
        for edge in outgoing:
            boundary_handle_uuid = str(edge.get("source_handle_uuid") or "")
            source_mapping = source_mappings.get(boundary_handle_uuid)
            boundary_handle = handles.get(boundary_handle_uuid)
            structural_output = (
                source_mapping is None
                and (
                    (isinstance(boundary_handle, Mapping) and dependency_only(boundary_handle))
                    # 控制区域生成的完成屏障没有值连接点，仍须展开到真实完成节点。
                    or (not boundary_handle_uuid and edge.get("dependency_only") is True)
                )
            )
            if not isinstance(source_mapping, Mapping) and not structural_output:
                raise ExecutionPlanBuildError(
                    "composite_boundary_mapping_invalid",
                    "组合工作流来源边界缺少唯一映射",
                )
            kind = (
                source_mapping.get("kind")
                if isinstance(source_mapping, Mapping)
                else None
            )
            value_providers: list[tuple[str, str, Mapping[str, Any]]] = []
            if structural_output:
                pass
            elif kind == "node_output":
                value_providers.append(
                    (
                        self._mapped_identity(
                            source_mapping, "workflow_node_uuid", nodes
                        ),
                        self._mapped_handle(
                            source_mapping, "source_handle_uuid", handles
                        ),
                        edge,
                    )
                )
            elif kind == "workflow_input":
                parameter = str(source_mapping.get("parameter") or "")
                input_handle_uuid = input_handles_by_name.get(parameter, "")
                providers = self._value_provider_edges(
                    incoming_by_handle.get(input_handle_uuid, []),
                    handles=handles,
                )
                if not providers and parameter in invocation_param:
                    self._project_static_parameter(
                        param_overrides=param_overrides,
                        node_uuid=str(edge.get("target_node_uuid") or ""),
                        handle_uuid=str(edge.get("target_handle_uuid") or ""),
                        handles=handles,
                        value=invocation_param[parameter],
                    )
                elif len(providers) != 1:
                    raise ExecutionPlanBuildError(
                        "composite_boundary_mapping_invalid",
                        "组合工作流透传输出没有唯一边提供者："
                        f"调用 {invocation_uuid} 参数 {parameter}，"
                        f"边提供者数量 {len(providers)}",
                    )
                else:
                    provider = providers[0]
                    value_providers.append(
                        (
                            str(provider.get("source_node_uuid") or ""),
                            str(provider.get("source_handle_uuid") or ""),
                            provider,
                        )
                    )
            else:
                raise ExecutionPlanBuildError(
                    "composite_boundary_mapping_invalid",
                    "组合工作流来源映射种类不受支持",
                )
            for source_node_uuid, source_handle_uuid, identity_edge in value_providers:
                generated.append(
                    self._rewired_edge(
                        invocation_uuid=invocation_uuid,
                        label="output",
                        source_edge=identity_edge,
                        source_node_uuid=source_node_uuid,
                        source_handle_uuid=source_handle_uuid,
                        target_node_uuid=str(edge.get("target_node_uuid") or ""),
                        target_handle_uuid=str(edge.get("target_handle_uuid") or ""),
                    )
                )
            # 透传值仍须等待内部物理动作完成，不能只按原始值提供者提前放行。
            try:
                completion_ids = structural_boundary_nodes(
                    nodes, invocation_uuid, validate_ancestry=False
                )
            except WorkflowBoundaryBindingError as error:
                raise ExecutionPlanBuildError(
                    "composite_boundary_mapping_invalid", str(error)
                ) from error
            for completion_source_uuid in completion_ids:
                completion_handle = next(
                    (
                        self._mapped_handle(item, "source_handle_uuid", handles)
                        for item in completion_sources
                        if item.get("workflow_node_uuid") == completion_source_uuid
                    ),
                    "",
                )
                generated.append(
                    self._rewired_edge(
                        invocation_uuid=invocation_uuid,
                        label="completion",
                        source_edge=edge,
                        source_node_uuid=completion_source_uuid,
                        source_handle_uuid="" if edge.get("dependency_only") is True else completion_handle,
                        target_node_uuid=str(edge.get("target_node_uuid") or ""),
                        target_handle_uuid=str(edge.get("target_handle_uuid") or ""),
                    )
                )
        return self._deduplicate_edges([*retained, *generated])

    def _with_derived_structural_mappings(
        self,
        *,
        invocation_uuid: str,
        structural: Mapping[str, Any],
        nodes: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """空的结构映射按子图根和叶子补齐，避免调用方的 ready 边被丢掉。

        参数：``structural`` 是调用节点上冻结的结构映射；节点、边和连接点来自
        同一平面快照。返回：已有入口或完成来源时原样返回；两边都空时，用该调用
        子孙里的可执行根和叶子补上 ready 连接点。异常：可执行根或叶子没有唯一
        ready 连接点时失败关闭。
        """

        if structural.get("entry_targets") or structural.get("completion_sources"):
            return structural
        descendants: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node_uuid, node in nodes.items():
                if node_uuid in descendants:
                    continue
                parent_uuid = str(node.get("parent_uuid") or "")
                if parent_uuid == invocation_uuid or parent_uuid in descendants:
                    descendants.add(node_uuid)
                    changed = True
        skipped = {"group", "condition", "repeat_until", "workflow"}
        executable = {
            node_uuid
            for node_uuid in descendants
            if str(nodes[node_uuid].get("type") or "").strip().lower() not in skipped
        }
        if not executable:
            return structural
        incoming: set[str] = set()
        outgoing: set[str] = set()
        for edge in edges:
            source_uuid = str(edge.get("source_node_uuid") or "")
            target_uuid = str(edge.get("target_node_uuid") or "")
            if source_uuid == invocation_uuid or target_uuid == invocation_uuid:
                continue
            if source_uuid in executable:
                outgoing.add(source_uuid)
            if target_uuid in executable:
                incoming.add(target_uuid)
        ready_handles = {
            (
                str(handle.get("workflow_node_template_uuid") or ""),
                str(handle.get("io_type") or ""),
            ): handle_uuid
            for handle_uuid, handle in handles.items()
            if str(handle.get("handle_key") or "").strip().lower() == "ready"
        }

        def ready_handle(node_uuid: str, io_type: str) -> str:
            template_uuid = str(
                nodes[node_uuid].get("workflow_node_template_uuid") or ""
            )
            handle_uuid = ready_handles.get((template_uuid, io_type), "")
            if not handle_uuid:
                raise ExecutionPlanBuildError(
                    "composite_boundary_mapping_invalid",
                    "组合工作流可执行节点缺少 ready 连接点："
                    f"{node_uuid}",
                )
            return handle_uuid

        derived = dict(structural)
        derived["entry_targets"] = [
            {
                "workflow_node_uuid": node_uuid,
                "target_handle_uuid": ready_handle(node_uuid, "target"),
            }
            for node_uuid in sorted(executable - incoming)
        ]
        derived["completion_sources"] = [
            {
                "workflow_node_uuid": node_uuid,
                "source_handle_uuid": ready_handle(node_uuid, "source"),
            }
            for node_uuid in sorted(executable - outgoing)
        ]
        return derived

    @staticmethod
    def _value_provider_edges(
        edges: Sequence[Mapping[str, Any]],
        *,
        handles: Mapping[str, Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        """从组合边界入边中排除只表达完成顺序的依赖边。

        组合透传输出会同时生成值边和完成边；当下游也是组合调用时，两条边会
        落到同一个边界输入。唯一值提供者校验只能统计正式值来源，但完成边仍
        必须保留并继续改写到内部动作，以维持物理执行顺序。
        """

        result: list[Mapping[str, Any]] = []
        for edge in edges:
            source_handle_uuid = str(edge.get("source_handle_uuid") or "")
            # 控制区域完成屏障只携带顺序，不参与组合输入的值来源计数。
            if edge.get("dependency_only") is True and not source_handle_uuid:
                continue
            source_handle = handles.get(source_handle_uuid)
            if not isinstance(source_handle, Mapping):
                raise ExecutionPlanBuildError(
                    "composite_boundary_mapping_invalid",
                    "组合工作流入边引用快照外来源连接点",
                )
            if not dependency_only(source_handle):
                result.append(edge)
        return result

    @staticmethod
    def _composite_depth(node_uuid: str, nodes: Mapping[str, Mapping[str, Any]]) -> int:
        """计算组合调用的静态父链深度；父链循环由后续图校验失败关闭。"""

        depth = 0
        current = nodes.get(node_uuid)
        visited = {node_uuid}
        while isinstance(current, Mapping):
            parent_uuid = str(current.get("parent_uuid") or "")
            if not parent_uuid or parent_uuid in visited:
                return depth
            visited.add(parent_uuid)
            current = nodes.get(parent_uuid)
            depth += 1
        return depth

    @staticmethod
    def _mapping_object(raw: Any, *, field: str) -> Mapping[str, Any]:
        """收窄组合映射对象；非法形状以稳定计划错误失败关闭。"""

        if not isinstance(raw, Mapping):
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid", f"组合工作流 {field} 非对象"
            )
        return raw

    @staticmethod
    def _mapping_items(raw: Any, *, field: str) -> list[Mapping[str, Any]]:
        """收窄组合映射数组；非法成员不允许被静默忽略。"""

        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid", f"组合工作流 {field} 非数组"
            )
        if any(not isinstance(item, Mapping) for item in raw):
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid", f"组合工作流 {field} 成员非对象"
            )
        return list(raw)

    @staticmethod
    def _mapped_identity(
        mapping: Mapping[str, Any],
        field: str,
        nodes: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """读取且验证组合映射中的节点身份。"""

        identity = str(mapping.get(field) or "")
        if identity not in nodes:
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid", "组合工作流映射引用快照外节点"
            )
        return identity

    @staticmethod
    def _mapped_handle(
        mapping: Mapping[str, Any],
        field: str,
        handles: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """读取且验证组合映射中的连接点（Handle）身份。"""

        identity = str(mapping.get(field) or "")
        if identity not in handles:
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid",
                "组合工作流映射引用快照外连接点",
            )
        return identity

    @staticmethod
    def _rewired_edge(
        *,
        invocation_uuid: str,
        label: str,
        source_edge: Mapping[str, Any],
        source_node_uuid: str,
        source_handle_uuid: str,
        target_node_uuid: str,
        target_handle_uuid: str,
    ) -> dict[str, Any]:
        """生成可重复构建的平面组合边。"""

        seed = ":".join(
            (
                label,
                str(source_edge.get("uuid") or ""),
                source_node_uuid,
                source_handle_uuid,
                target_node_uuid,
                target_handle_uuid,
            )
        )
        rewired: dict[str, Any] = {
            "uuid": str(uuid5(UUID(invocation_uuid), f"execution-plan:{seed}")),
            "source_node_uuid": source_node_uuid,
            "source_handle_uuid": source_handle_uuid,
            "target_node_uuid": target_node_uuid,
            "target_handle_uuid": target_handle_uuid,
        }
        # 提升到控制区域的完成边没有来源连接点；后续组合改写须保留此语义。
        if source_edge.get("dependency_only") is True or (
            label == "completion" and not source_handle_uuid
        ):
            rewired["dependency_only"] = True
        return rewired

    @staticmethod
    def _project_static_parameter(
        *,
        param_overrides: dict[str, dict[str, Any]],
        node_uuid: str,
        handle_uuid: str,
        handles: Mapping[str, Mapping[str, Any]],
        value: Any,
    ) -> None:
        """把组合静态透传值投影到实际执行节点的最终动作参数键。

        参数：``param_overrides`` 是计划级覆盖集合；节点与连接点身份定位实际
        目标，``handles`` 提供参数路径，``value`` 是调用时已冻结静态值。返回：
        无，原地写入覆盖集合。异常：连接点、参数键或重复写值冲突时失败关闭。
        """

        handle = handles.get(handle_uuid)
        if not isinstance(handle, Mapping):
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid",
                "组合工作流静态透传引用快照外连接点",
            )
        data_key = final_target_data_key(handle_data_key(handle))
        if not data_key:
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid",
                "组合工作流静态透传目标缺少动作参数键",
            )
        existing = param_overrides[node_uuid].get(data_key)
        if existing is not None and existing != value:
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid",
                "组合工作流静态透传向同一动作参数写入冲突值",
            )
        param_overrides[node_uuid][data_key] = value

    @staticmethod
    def _deduplicate_edges(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """按完整端点去重并稳定排序平面边。"""

        unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for edge in edges:
            key = (
                str(edge.get("source_node_uuid") or ""),
                str(edge.get("source_handle_uuid") or ""),
                str(edge.get("target_node_uuid") or ""),
                str(edge.get("target_handle_uuid") or ""),
            )
            unique.setdefault(key, dict(edge))
        return [unique[key] for key in sorted(unique)]

    def runtime_handles(
        self,
        *,
        active: Mapping[str, Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str]]:
        """实例化节点作用域运行连接点（Handle）。

        参数：``active`` 是活动节点，``handles`` 是模板连接点索引。返回：稳定
        排序的运行连接点及 ``(node, template_handle)`` 身份映射。异常：节点或
        模板 UUID 非规范时 ``UUID`` 构造抛 ``ValueError`` 并失败关闭。
        """

        runtime: list[dict[str, Any]] = []
        identities: dict[tuple[str, str], str] = {}
        for node_uuid, node in active.items():
            template_uuid = str(node.get("workflow_node_template_uuid") or "")
            for handle_uuid, handle in handles.items():
                if handle.get("workflow_node_template_uuid") != template_uuid:
                    continue
                # ``runtime_uuid`` 把可复用模板端点变成节点作用域稳定身份。
                runtime_uuid = str(
                    uuid5(UUID(node_uuid), f"runtime-handle:{handle_uuid}")
                )
                identities[(node_uuid, handle_uuid)] = runtime_uuid
                metadata = handle.get("meta_data")
                unilab = (
                    metadata.get("unilab") if isinstance(metadata, Mapping) else None
                )
                site_selector = (
                    unilab.get("site_selector") if isinstance(unilab, Mapping) else None
                )
                runtime.append(
                    {
                        "uuid": runtime_uuid,
                        "node_uuid": node_uuid,
                        "template_handle_uuid": handle_uuid,
                        "data_source": str(handle.get("data_source") or ""),
                        "handle_key": str(handle.get("handle_key") or ""),
                        "data_key": handle_data_key(handle),
                        "io_type": str(handle.get("io_type") or ""),
                        "type": str(handle.get("type") or ""),
                        "required": bool(handle.get("required")),
                        **(
                            {"site_selector": deepcopy(dict(site_selector))}
                            if isinstance(site_selector, Mapping)
                            else {}
                        ),
                    }
                )
        runtime.sort(key=lambda item: (item["node_uuid"], item["uuid"]))
        return runtime, identities

    def contract_edges(
        self,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        active: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
        runtime_handle_ids: Mapping[tuple[str, str], str],
    ) -> list[dict[str, Any]]:
        """收敛虚拟/禁用节点并实例化活动边端点。

        参数：节点、活动节点、原始边、模板连接点和运行身份来自同一冻结图。
        返回：直接活动边与稳定 ``dependency_only`` 旁路边。异常：缺端点、循环
        或连接点引用非法时抛 ``ExecutionPlanBuildError``。
        """

        outgoing: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for edge in edges:
            source = str(edge.get("source_node_uuid") or "")
            target = str(edge.get("target_node_uuid") or "")
            if source not in nodes or target not in nodes:
                raise ExecutionPlanBuildError(
                    "edge_node_identity_mismatch", "工作流边引用快照外节点"
                )
            outgoing[source].append(edge)
        planned: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for source_uuid in active:
            self._walk_contracted_edges(
                source_uuid=source_uuid,
                current_uuid=source_uuid,
                path_edges=(),
                path_nodes=(source_uuid,),
                outgoing=outgoing,
                active=active,
                handles=handles,
                runtime_handle_ids=runtime_handle_ids,
                seen=seen,
                planned=planned,
            )
        planned.sort(
            key=lambda item: (item["source_node_uuid"], item["target_node_uuid"])
        )
        return planned

    def _walk_contracted_edges(
        self,
        *,
        source_uuid: str,
        current_uuid: str,
        path_edges: tuple[str, ...],
        path_nodes: tuple[str, ...],
        outgoing: Mapping[str, Sequence[Mapping[str, Any]]],
        active: Mapping[str, Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
        runtime_handle_ids: Mapping[tuple[str, str], str],
        seen: set[tuple[str, str]],
        planned: list[dict[str, Any]],
    ) -> None:
        """递归收敛一个活动节点之后的虚拟路径。

        参数：``source_uuid`` 是起始活动节点，``current_uuid`` 是当前节点，
        ``path_edges``/``path_nodes`` 是当前路径，其他映射提供图与运行端点，
        ``seen``/``planned`` 收集去重结果。返回：无。异常：路径循环时抛稳定
        ``ExecutionPlanBuildError``，不会递归重放物理执行。
        """

        for edge in outgoing.get(current_uuid, ()):
            target_uuid = str(edge["target_node_uuid"])
            if target_uuid in path_nodes:
                raise ExecutionPlanBuildError(
                    "workflow_cycle", "工作流虚拟节点路径含循环"
                )
            # ``next_path_edges`` 冻结旁路边身份；``next_path_nodes`` 用于查环。
            next_path_edges = (*path_edges, str(edge.get("uuid") or ""))
            next_path_nodes = (*path_nodes, target_uuid)
            if target_uuid not in active:
                self._walk_contracted_edges(
                    source_uuid=source_uuid,
                    current_uuid=target_uuid,
                    path_edges=next_path_edges,
                    path_nodes=next_path_nodes,
                    outgoing=outgoing,
                    active=active,
                    handles=handles,
                    runtime_handle_ids=runtime_handle_ids,
                    seen=seen,
                    planned=planned,
                )
                continue
            if current_uuid == source_uuid and not path_edges:
                planned.append(
                        self._direct_edge(
                            edge,
                            handles=handles,
                            runtime_handle_ids=runtime_handle_ids,
                            target_node=active[target_uuid],
                    )
                )
                continue
            pair = (source_uuid, target_uuid)
            if pair in seen:
                continue
            seen.add(pair)
            planned.append(
                {
                    "uuid": str(
                        uuid5(
                            UUID(source_uuid),
                            f"dependency-bypass:{target_uuid}:{':'.join(next_path_edges)}",
                        )
                    ),
                    "source_node_uuid": source_uuid,
                    "target_node_uuid": target_uuid,
                    "source_handle_uuid": "",
                    "target_handle_uuid": "",
                    "source_data_key": "",
                    "target_data_key": "",
                    "source_type": "",
                    "target_type": "",
                    "dependency_only": True,
                }
            )

    @staticmethod
    def _direct_edge(
        edge: Mapping[str, Any],
        *,
        handles: Mapping[str, Mapping[str, Any]],
        runtime_handle_ids: Mapping[tuple[str, str], str],
        target_node: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """实例化一条直接活动边。

        参数：``edge`` 是模板端点边，``handles`` 提供语义，``runtime_handle_ids``
        提供节点作用域身份。返回：冻结运行边。异常：端点缺失时抛计划错误。
        """

        source = str(edge["source_node_uuid"])
        target = str(edge["target_node_uuid"])
        source_template = str(edge.get("source_handle_uuid") or "")
        target_template = str(edge.get("target_handle_uuid") or "")
        source_handle = handles.get(source_template)
        target_handle = handles.get(target_template)
        if edge.get("dependency_only") is True and not source_template:
            control_target = not target_template and (target_node or {}).get("type") in {"repeat_until", "condition"}
            # Structured controllers have no data Handles; ordinary targets still
            # require a real node-scoped Handle even on dependency-only edges.
            if target_handle is None and not control_target:
                raise ExecutionPlanBuildError(
                    "edge_handle_identity_mismatch", "工作流边引用快照外连接点"
                )
            if not control_target and (target, target_template) not in runtime_handle_ids:
                raise ExecutionPlanBuildError(
                    "edge_handle_identity_mismatch", "工作流边端点不属于对应节点模板"
                )
            return {
                "uuid": str(edge.get("uuid") or ""),
                "source_node_uuid": source,
                "target_node_uuid": target,
                "source_handle_uuid": "",
                "target_handle_uuid": "",
                "source_data_key": "",
                "target_data_key": "",
                "source_type": "",
                "target_type": "",
                "dependency_only": True,
            }
        if source_handle is None or target_handle is None:
            raise ExecutionPlanBuildError(
                "edge_handle_identity_mismatch", "工作流边引用快照外连接点"
            )
        source_runtime_uuid = runtime_handle_ids.get((source, source_template))
        target_runtime_uuid = runtime_handle_ids.get((target, target_template))
        if source_runtime_uuid is None or target_runtime_uuid is None:
            raise ExecutionPlanBuildError(
                "edge_handle_identity_mismatch", "工作流边端点不属于对应节点模板"
            )
        planned = {
            "uuid": str(edge.get("uuid") or ""),
            "source_node_uuid": source,
            "target_node_uuid": target,
            "source_handle_uuid": source_runtime_uuid,
            "target_handle_uuid": target_runtime_uuid,
            "source_data_key": handle_data_key(source_handle),
            "target_data_key": handle_data_key(target_handle),
            "source_type": str(source_handle.get("type") or ""),
            "target_type": str(target_handle.get("type") or ""),
        }
        if dependency_only(source_handle):
            planned["dependency_only"] = True
        return planned

    @staticmethod
    def topological_order(
        active: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        """计算稳定拓扑顺序。

        参数：``active`` 是活动节点，``edges`` 是收敛后依赖。返回：按创建时间与
        UUID 稳定排序的节点身份。异常：存在循环时抛 ``StoreConflict``。
        """

        indegree = {node_uuid: 0 for node_uuid in active}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            source = str(edge["source_node_uuid"])
            target = str(edge["target_node_uuid"])
            outgoing[source].append(target)
            indegree[target] += 1

        def stable(identity: str) -> tuple[str, str]:
            """生成稳定拓扑排序键。

            参数：``identity`` 是活动节点 UUID。返回：创建时间与 UUID 元组。
            异常：无；缺失创建时间按空文本排序。
            """

            return str(active[identity].get("create_time") or ""), identity

        available = sorted(
            (key for key, degree in indegree.items() if degree == 0), key=stable
        )
        ordered: list[str] = []
        while available:
            current = available.pop(0)
            ordered.append(current)
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    available.append(target)
                    available.sort(key=stable)
        if len(ordered) != len(active):
            raise StoreConflict("workflow graph contains a cycle")
        return ordered


def executor_kind(node_type: str) -> str:
    """规范化节点执行种类。

    参数：``node_type`` 是模板或节点类型。返回：计划记录的执行种类。
    异常：未知类型抛 ``StoreConflict``；编译器稍后限制旧调度器支持范围。
    """

    normalized = node_type.strip().lower()
    aliases = {
        "ilab": "device_action",
        "device": "device_action",
        "action": "device_action",
        "resource_action": "device_action",
        "py_script": "script",
        "transfer": "Transfer",
    }
    kind = aliases.get(normalized, normalized)
    allowed = {
        "device_action",
        "compute",
        "workflow_command",
        "condition",
        "repeat_until",
        "script",
        "group",
        "tool_call",
        "manual_confirm",
        "material_io",
        "material_source",
        "Transfer",
        "workflow",
    }
    if kind not in allowed:
        raise StoreConflict(f"unsupported workflow node type {node_type!r}")
    return kind


def handle_data_key(handle: Mapping[str, Any]) -> str:
    """读取连接点数据路径。

    参数：``handle`` 是连接点模板。返回：显式 ``data_key`` 或业务键。异常：无。
    """

    return str(handle.get("data_key") or handle.get("handle_key") or "").strip()


def final_target_data_key(data_key: str) -> str:
    """取得目标最终写入键。

    参数：``data_key`` 是可能含 ``@@@`` 的路径。返回：最后一段。异常：无。
    """

    return data_key.split("@@@")[-1].strip()


def dependency_only(handle: Mapping[str, Any]) -> bool:
    """判断来源连接点是否只表达顺序依赖。

    参数：``handle`` 是来源端点。返回：ready 或非 executor 数据源时为真。
    异常：无。
    """

    if str(handle.get("handle_key") or "").strip().lower() == "ready":
        return True
    source = str(handle.get("data_source") or "").strip().lower()
    # ``result`` 是动作返回值的正式数据提供者；只有 ready/状态等非值来源才是
    # 纯顺序依赖。把 result 降级会令必填动作输入在计划冻结时丢失提供者。
    return bool(source) and source not in {"executor", "result"}


__all__ = [
    "ExecutionPlanBuildError",
    "ExecutionPlanGraphNormalizer",
    "executor_kind",
    "final_target_data_key",
]
