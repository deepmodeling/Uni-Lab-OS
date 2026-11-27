"""已发布工作流合同在父图中的确定性组合展开。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from unilabos.workflow.authoring_identity import (
    authoring_edge_uuid,
    expanded_node_uuid,
)
from unilabos.workflow.composite_graph_rewrite import (
    CompositeGraphRewriteError,
    materialize_control_arguments,
    merge_expanded_resource_scopes,
    project_experiment_operation_resource_scopes,
    remap_control_references,
)
from unilabos.workflow.workflow_type import WORKFLOW_TYPE_EXPERIMENT_OPERATION


class CompositeInvocationInvalid(ValueError):
    """组合调用输入或冻结合同不满足安全展开条件。"""


@dataclass(frozen=True, slots=True)
class CompositeInvocationExpansion:
    """组合调用生成的节点、边和可原子提交的工作流元数据补丁。"""

    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    resource_scopes: tuple[dict[str, Any], ...]
    workflow_meta_data: dict[str, Any]
    # 子实验操作的数量型库存需求，已改写到本次展开的节点身份并按调用实参重绑。
    inventory_requirements: tuple[dict[str, Any], ...] = ()


_remap_control_references = remap_control_references


def _remap_boundary_value(value: Any, node_uuid_map: Mapping[str, str]) -> Any:
    """递归替换边界映射里的来源节点 UUID。"""

    if isinstance(value, list):
        return [_remap_boundary_value(item, node_uuid_map) for item in value]
    if not isinstance(value, Mapping):
        return deepcopy(value)
    result = {
        str(key): _remap_boundary_value(item, node_uuid_map)
        for key, item in value.items()
    }
    node_uuid = result.get("workflow_node_uuid")
    if isinstance(node_uuid, str) and node_uuid in node_uuid_map:
        result["workflow_node_uuid"] = node_uuid_map[node_uuid]
    return result


def _remap_nested_composite_metadata(
    meta_data: Mapping[str, Any],
    node_uuid_map: Mapping[str, str],
) -> dict[str, Any]:
    """复制节点元数据，并重写嵌套组合调用的私有边界引用。"""

    result = _remap_control_references(meta_data, node_uuid_map)
    unilab = result.get("unilab")
    composite = unilab.get("composite") if isinstance(unilab, dict) else None
    if not isinstance(composite, dict):
        return result
    for key in ("target_mappings", "source_mappings", "structural_mappings"):
        if key in composite:
            composite[key] = _remap_boundary_value(composite[key], node_uuid_map)
    return result


def _translated_pose(
    pose: Mapping[str, Any],
    *,
    minimum_x: float,
    minimum_y: float,
) -> dict[str, Any]:
    """把子图顶层节点平移到调用节点内部并保留其他布局字段。"""

    result = deepcopy(dict(pose))
    for key, minimum, offset in (
        ("x", minimum_x, 40.0),
        ("y", minimum_y, 64.0),
    ):
        raw = result.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise CompositeInvocationInvalid(f"节点 pose.{key} 必须是数值")
        result[key] = raw - minimum + offset
    return result


def _top_level_origin(nodes: list[Mapping[str, Any]]) -> tuple[float, float]:
    """返回子图顶层节点坐标的左上原点。"""

    coordinates: list[tuple[float, float]] = []
    for node in nodes:
        if node.get("parent_uuid") is not None:
            continue
        pose = node.get("pose") or {}
        if not isinstance(pose, Mapping):
            raise CompositeInvocationInvalid("节点 pose 必须是对象")
        x = pose.get("x", 0)
        y = pose.get("y", 0)
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
        ):
            raise CompositeInvocationInvalid("节点坐标必须是数值")
        coordinates.append((float(x), float(y)))
    if not coordinates:
        return 0.0, 0.0
    return min(item[0] for item in coordinates), min(item[1] for item in coordinates)


def _references_workflow(nodes: list[Mapping[str, Any]], workflow_uuid: str) -> bool:
    """判断冻结子树是否已经引用待插入的父工作流。"""

    for node in nodes:
        meta_data = node.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        composite = unilab.get("composite") if isinstance(unilab, Mapping) else None
        if (
            isinstance(composite, Mapping)
            and composite.get("child_workflow_uuid") == workflow_uuid
        ):
            return True
    return False


def _invocation_param(
    contract: Mapping[str, Any],
    param: Mapping[str, Any],
) -> dict[str, Any]:
    """合并调用参数并在展开前执行发布合同的参数边界校验。

    参数：``contract`` 是待展开发布合同，``param`` 是父调用节点已填写的参数；
    用户值始终优先。返回：只包含合同声明键、且已补齐可选默认值的完整调用参数。
    异常：调用参数不是对象、包含未知键、缺少必填参数、输入合同损坏，或可选参数
    没有规范默认值时抛 ``CompositeInvocationInvalid``，禁止生成运行时缺值或静默
    丢弃用户输入。状态不变量：函数返回后每个参数都能在冻结合同中找到对应声明。
    """

    if not isinstance(param, Mapping):
        raise CompositeInvocationInvalid("组合调用参数必须是对象")
    envelope = contract.get("input_contract")
    descriptors = envelope.get("parameters") if isinstance(envelope, Mapping) else None
    if not isinstance(descriptors, list):
        raise CompositeInvocationInvalid("发布合同输入定义损坏")
    declared_names: set[str] = set()
    for descriptor in descriptors:
        if (
            not isinstance(descriptor, Mapping)
            or not isinstance(descriptor.get("name"), str)
            or not isinstance(descriptor.get("required"), bool)
        ):
            raise CompositeInvocationInvalid("发布合同输入参数损坏")
        name = str(descriptor["name"])
        if not name or name in declared_names:
            raise CompositeInvocationInvalid("发布合同输入参数名称重复或为空")
        declared_names.add(name)

    unknown_names = set(param) - declared_names
    if unknown_names:
        unknown = ", ".join(sorted(str(name) for name in unknown_names))
        raise CompositeInvocationInvalid(f"组合调用包含未知输入参数: {unknown}")

    result: dict[str, Any] = {}
    for descriptor in descriptors:
        name = str(descriptor["name"])
        if name in param:
            result[name] = deepcopy(param[name])
            continue
        if descriptor["required"]:
            raise CompositeInvocationInvalid(f"组合调用缺少必填输入参数: {name}")
        if "default" not in descriptor:
            raise CompositeInvocationInvalid("发布合同可选输入缺少默认值")
        if name not in result:
            result[name] = deepcopy(descriptor["default"])
    return result


def _published_boundary_handle_uuid(node_template_uuid: str, name: str) -> str:
    """计算发布合同中某个工作流输入连接点的稳定身份。

    参数：``node_template_uuid`` 是发布合同生成的宿主节点模板身份，``name`` 是
    输入参数名。返回：与发布投影相同的连接点 UUID。异常：模板 UUID 非法时抛出
    ``ValueError``，由上层收敛为组合调用输入错误。
    """

    return str(uuid5(UUID(node_template_uuid), f"published-handle:target:{name}"))


def _materialize_published_arguments(
    *,
    expanded_nodes: list[dict[str, Any]],
    source_nodes: list[Mapping[str, Any]],
    contract: Mapping[str, Any],
    normalized_param: Mapping[str, Any],
    parent_handles: Sequence[Mapping[str, Any]] = (),
) -> None:
    """把发布合同实参下推到展开后的内部节点。

    参数：``expanded_nodes`` 是已重写身份的内部节点，``source_nodes`` 是冻结图
    原节点，``contract`` 含输入合同、边界映射和连接点快照，``normalized_param``
    是合并默认值后的实参。返回：原地写入固定值或父参数绑定。异常：合同映射、
    连接点或节点结构损坏时抛 ``CompositeInvocationInvalid``。静态实参必须清除
    子图原有的工作流参数绑定，否则父图没有同名参数时会被整体 I/O 校验拒绝。
    """

    boundary_mapping = contract.get("boundary_mapping")
    input_contract = contract.get("input_contract")
    snapshot = contract.get("graph_snapshot")
    if (
        not isinstance(boundary_mapping, Mapping)
        or not isinstance(input_contract, Mapping)
        or not isinstance(snapshot, Mapping)
    ):
        raise CompositeInvocationInvalid("发布合同缺少输入边界映射")
    target_mappings = boundary_mapping.get("target_mappings")
    descriptors = input_contract.get("parameters")
    # 早期发布合同没有保存内部连接点快照；只要没有目标映射，控制节点参数已在
    # 上面固化，其他不涉及动作目标的历史绑定仍可沿用冻结图事实。
    snapshot_handles = snapshot.get("handle_templates")
    if not isinstance(target_mappings, Mapping) or not isinstance(descriptors, list):
        raise CompositeInvocationInvalid("发布合同输入边界映射损坏")

    # 控制节点的输入不会出现在动作目标映射中，但仍必须在真实组合展开路径
    # 固化为调用实参。两条展开路径共同使用中立图重写模块中的规则。
    try:
        materialize_control_arguments(
            expanded_nodes,
            keyword_arguments=normalized_param,
            source_handles=parent_handles,
        )
    except CompositeGraphRewriteError as error:
        raise CompositeInvocationInvalid(f"{error.code} ({error.path})") from None

    # 旧版合同可能只有参数描述，没有内部目标映射；这表示展开时继续沿用
    # 冻结图自身的绑定，不应因为新增的快照校验把历史合同判为损坏。
    if not target_mappings:
        return
    if snapshot_handles is None:
        snapshot_handles = []
    if not isinstance(snapshot_handles, list):
        raise CompositeInvocationInvalid("发布合同缺少内部连接点快照")

    data_key_by_uuid = {
        str(handle.get("uuid")): str(handle["data_key"])
        for handle in snapshot_handles
        if isinstance(handle, Mapping)
        and isinstance(handle.get("uuid"), str)
        and handle.get("io_type") == "target"
        and isinstance(handle.get("data_key"), str)
    }
    expanded_by_source_uuid = {
        str(source.get("uuid")): expanded
        for source, expanded in zip(source_nodes, expanded_nodes, strict=True)
        if isinstance(source, Mapping) and isinstance(source.get("uuid"), str)
    }
    node_template_uuid = contract.get("node_template_uuid")
    if not isinstance(node_template_uuid, str):
        raise CompositeInvocationInvalid("发布合同缺少节点模板身份")

    for descriptor in descriptors:
        if not isinstance(descriptor, Mapping) or not isinstance(
            descriptor.get("name"), str
        ):
            raise CompositeInvocationInvalid("发布合同输入参数损坏")
        name = str(descriptor["name"])
        if name not in normalized_param:
            # 必填参数的缺失已经由 _invocation_param 的调用方合同校验处理；
            # 这里跳过仅为兼容损坏合同中未声明的可选参数。
            continue
        boundary_uuid = _published_boundary_handle_uuid(node_template_uuid, name)
        targets = target_mappings.get(boundary_uuid)
        if not isinstance(targets, list):
            raise CompositeInvocationInvalid(f"输入参数 {name} 的边界映射损坏")
        value = normalized_param[name]
        for target in targets:
            if not isinstance(target, Mapping):
                raise CompositeInvocationInvalid(f"输入参数 {name} 的目标映射损坏")
            source_uuid = str(target.get("workflow_node_uuid") or "")
            target_uuid = str(target.get("target_handle_uuid") or "")
            node = expanded_by_source_uuid.get(source_uuid)
            data_key = data_key_by_uuid.get(target_uuid)
            if node is None or data_key is None:
                raise CompositeInvocationInvalid(f"输入参数 {name} 的目标连接点不存在")
            meta_data = node.setdefault("meta_data", {})
            unilab = meta_data.setdefault("unilab", {})
            bindings = unilab.setdefault("input_bindings", {})
            if not isinstance(bindings, dict):
                raise CompositeInvocationInvalid("子节点输入绑定结构损坏")
            if isinstance(value, Mapping) and value.get("kind") == "workflow_input":
                parameter = value.get("parameter")
                if not isinstance(parameter, str):
                    raise CompositeInvocationInvalid(
                        f"输入参数 {name} 的父参数引用损坏"
                    )
                # 固化的子默认值必须让位于父工作流输入。
                params = node.setdefault("param", {})
                if not isinstance(params, dict):
                    raise CompositeInvocationInvalid("子节点参数结构损坏")
                params.pop(data_key, None)
                params.pop(data_key.split("@@@")[-1], None)
                if node.get("type") == "material_io":
                    params[data_key] = None
                bindings[target_uuid] = {"parameter": parameter}
                continue
            # node_output 与固定值都不能继续携带子工作流自身的参数绑定；
            # 前者由父图边界映射表达，后者直接写入动作参数。
            bindings.pop(target_uuid, None)
            params = node.setdefault("param", {})
            if not isinstance(params, dict):
                raise CompositeInvocationInvalid("子节点参数结构损坏")
            if node.get("type") == "material_io" and isinstance(value, Mapping) and value.get("kind") == "node_output":
                params[data_key] = None
                if data_key == "material":
                    unilab["material_io_material_binding"] = {"result_name": str(value.get("workflow_node_uuid") or "composite_boundary")}
            else:
                params[data_key] = deepcopy(value)


def compile_composite_invocation(
    *,
    parent_graph: Mapping[str, Any],
    contract: Mapping[str, Any],
    invocation_uuid: str,
    pose: Mapping[str, Any],
    param: Mapping[str, Any],
    device_bindings: Mapping[str, str],
) -> CompositeInvocationExpansion:
    """展开冻结发布合同并返回可与父图原子提交的图补丁。

    参数：``parent_graph`` 是当前父图，``contract`` 含私有冻结快照；调用身份、
    画布位置、参数和设备绑定来自公共命令。返回确定性调用根、内部节点、内部边、
    合并后的资源作用域和父工作流元数据；自引用、递归、身份碰撞或损坏引用抛
    ``CompositeInvocationInvalid``。
    """

    parent_workflow = parent_graph["workflow"]
    parent_workflow_uuid = str(parent_workflow["uuid"])
    child_workflow_uuid = str(contract["workflow_uuid"])
    if child_workflow_uuid == parent_workflow_uuid:
        raise CompositeInvocationInvalid("工作流不能调用自身")
    snapshot = contract.get("graph_snapshot")
    if not isinstance(snapshot, Mapping):
        raise CompositeInvocationInvalid("发布合同缺少冻结图")
    snapshot_workflow = snapshot.get("workflow")
    if (
        not isinstance(snapshot_workflow, Mapping)
        or snapshot_workflow.get("workflow_type") != WORKFLOW_TYPE_EXPERIMENT_OPERATION
    ):
        raise CompositeInvocationInvalid("组合调用只能引用实验操作")
    source_nodes = snapshot.get("nodes")
    source_edges = snapshot.get("edges")
    if not isinstance(source_nodes, list) or not isinstance(source_edges, list):
        raise CompositeInvocationInvalid("发布合同冻结图损坏")
    if _references_workflow(source_nodes, parent_workflow_uuid):
        raise CompositeInvocationInvalid("组合调用会形成递归引用")

    node_uuid_map: dict[str, str] = {}
    for node in source_nodes:
        if not isinstance(node, Mapping) or not isinstance(node.get("uuid"), str):
            raise CompositeInvocationInvalid("发布合同包含无身份节点")
        source_uuid = str(node["uuid"])
        if source_uuid in node_uuid_map:
            raise CompositeInvocationInvalid("发布合同包含重复节点身份")
        node_uuid_map[source_uuid] = expanded_node_uuid(invocation_uuid, source_uuid)

    existing_node_uuids = {
        str(item["uuid"])
        for item in parent_graph.get("nodes", [])
        if isinstance(item, Mapping) and isinstance(item.get("uuid"), str)
    }
    insertion_node_uuids = {invocation_uuid, *node_uuid_map.values()}
    if existing_node_uuids & insertion_node_uuids:
        raise CompositeInvocationInvalid("组合调用节点身份与父图冲突")

    boundary_mapping = contract.get("boundary_mapping") or {}
    remapped_boundary = _remap_boundary_value(boundary_mapping, node_uuid_map)
    root = {
        "uuid": invocation_uuid,
        "workflow_node_template_uuid": contract["node_template_uuid"],
        "name": contract["name"],
        "type": "workflow",
        "pose": deepcopy(dict(pose)),
        "param": _invocation_param(contract, param),
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "description": "引用已发布实验操作；兼容的新发布版本由 OS 自动替换。",
        "meta_data": {
            "unilab": {
                "composite": {
                    "version": 1,
                    "contract_uuid": contract["uuid"],
                    "child_workflow_uuid": child_workflow_uuid,
                    "child_workflow_revision": contract["workflow_revision"],
                    # ``source_hash`` 是发布图摘要；组合兼容性 pin 使用子工作流
                    # 已应用源码摘要。服务入口会从同一工作流记录注入
                    # ``applied_source_hash``，旧合同没有该字段时才回退到图摘要，
                    # 保留历史非源码工作流的可读性。
                    "child_applied_source_hash": contract.get(
                        "applied_source_hash",
                        contract["source_hash"],
                    ),
                    "contract_digest": contract["contract_digest"],
                    "contract_compatibility": {
                        "parameters": contract["input_contract"]["parameters"],
                        "outputs": contract["output_contract"]["outputs"],
                    },
                    "executor_requirements": deepcopy(
                        contract["executor_requirements"]
                    ),
                    "device_bindings": dict(device_bindings),
                    **remapped_boundary,
                }
            }
        },
    }

    minimum_x, minimum_y = _top_level_origin(source_nodes)
    expanded_nodes = [root]
    for source in source_nodes:
        copied = deepcopy(dict(source))
        source_uuid = str(source["uuid"])
        copied.pop("create_time", None)
        copied.pop("update_time", None)
        copied.pop("workflow_uuid", None)
        copied.pop("status", None)
        copied["uuid"] = node_uuid_map[source_uuid]
        raw_parent = source.get("parent_uuid")
        if raw_parent is None:
            copied["parent_uuid"] = invocation_uuid
            copied["pose"] = _translated_pose(
                source.get("pose") or {},
                minimum_x=minimum_x,
                minimum_y=minimum_y,
            )
        elif str(raw_parent) in node_uuid_map:
            copied["parent_uuid"] = node_uuid_map[str(raw_parent)]
        else:
            raise CompositeInvocationInvalid("子节点引用了冻结图外的父节点")
        copied["meta_data"] = _remap_nested_composite_metadata(
            source.get("meta_data") or {},
            node_uuid_map,
        )
        copied["param"] = _remap_control_references(
            source.get("param") or {},
            node_uuid_map,
        )
        requirement_key = contract["executor_binding_mapping"].get(source_uuid)
        if requirement_key is not None:
            copied["material_uuid"] = device_bindings[requirement_key]
            copied["meta_data"].setdefault("unilab", {})["executor_binding"] = {
                "mode": "fixed", "device_id": device_bindings[requirement_key],
            }
        expanded_nodes.append(copied)

    _materialize_published_arguments(
        expanded_nodes=expanded_nodes[1:],
        source_nodes=source_nodes,
        contract=contract,
        normalized_param=root["param"],
        parent_handles=parent_graph.get("handle_templates", []),
    )
    from unilabos.workflow.composite_inventory_requirements import (
        CompositeInventoryRequirementError,
        expand_child_inventory_requirements,
    )

    raw_requirements = snapshot.get("inventory_requirements") or []
    if not isinstance(raw_requirements, list):
        raise CompositeInvocationInvalid("发布合同的数量需求损坏")
    try:
        inventory_requirements = expand_child_inventory_requirements(
            raw_requirements,
            invocation_uuid=invocation_uuid,
            child_workflow_uuid=child_workflow_uuid,
            node_uuid_map=node_uuid_map,
            arguments=root["param"] if isinstance(root.get("param"), Mapping) else {},
        )
    except CompositeInventoryRequirementError as error:
        raise CompositeInvocationInvalid(f"{error.code} ({error.path})：{error.message}") from None

    expanded_edges: list[dict[str, Any]] = []
    existing_edge_uuids = {
        str(item["uuid"])
        for item in parent_graph.get("edges", [])
        if isinstance(item, Mapping) and isinstance(item.get("uuid"), str)
    }
    for source in source_edges:
        if not isinstance(source, Mapping):
            raise CompositeInvocationInvalid("发布合同包含非法边")
        source_node_uuid = node_uuid_map.get(str(source.get("source_node_uuid")))
        target_node_uuid = node_uuid_map.get(str(source.get("target_node_uuid")))
        if source_node_uuid is None or target_node_uuid is None:
            raise CompositeInvocationInvalid("发布合同的边引用冻结图外节点")
        edge_uuid = authoring_edge_uuid(
            workflow_uuid=parent_workflow_uuid,
            source_node_uuid=source_node_uuid,
            source_handle_uuid=str(source["source_handle_uuid"]),
            target_node_uuid=target_node_uuid,
            target_handle_uuid=str(source["target_handle_uuid"]),
        )
        if edge_uuid in existing_edge_uuids:
            raise CompositeInvocationInvalid("组合调用边身份与父图冲突")
        copied = deepcopy(dict(source))
        copied.pop("create_time", None)
        copied.pop("update_time", None)
        copied["uuid"] = edge_uuid
        copied["source_node_uuid"] = source_node_uuid
        copied["target_node_uuid"] = target_node_uuid
        expanded_edges.append(copied)
    try:
        child_scopes = project_experiment_operation_resource_scopes(
            snapshot_workflow,
            invocation_uuid=invocation_uuid,
            node_uuid_map=node_uuid_map,
        )
        parent_meta_data = parent_workflow.get("meta_data")
        if parent_meta_data is None:
            parent_meta_data = {}
        if not isinstance(parent_meta_data, Mapping):
            raise CompositeGraphRewriteError(
                "composite_boundary_mapping_invalid",
                "/workflow/meta_data",
                "父工作流元数据必须是对象",
            )
        workflow_meta_data = deepcopy(dict(parent_meta_data))
        raw_unilab = workflow_meta_data.get("unilab")
        if raw_unilab is None:
            raw_unilab = {}
        if not isinstance(raw_unilab, Mapping):
            raise CompositeGraphRewriteError(
                "composite_boundary_mapping_invalid",
                "/workflow/meta_data/unilab",
                "父工作流 unilab 元数据必须是对象",
            )
        unilab = deepcopy(dict(raw_unilab))
        raw_parent_scopes = unilab.get("resource_scopes")
        if raw_parent_scopes is None:
            raw_parent_scopes = []
        if (
            not isinstance(raw_parent_scopes, Sequence)
            or isinstance(raw_parent_scopes, (str, bytes))
            or any(not isinstance(scope, Mapping) for scope in raw_parent_scopes)
        ):
            raise CompositeGraphRewriteError(
                "composite_boundary_mapping_invalid",
                "/workflow/meta_data/unilab/resource_scopes",
                "父工作流资源作用域必须是对象数组",
            )
        resource_scopes = merge_expanded_resource_scopes(
            raw_parent_scopes,
            nested_invocations=(
                (
                    invocation_uuid,
                    (invocation_uuid, *node_uuid_map.values()),
                    child_scopes,
                ),
            ),
        )
        if resource_scopes:
            unilab["resource_scopes"] = list(resource_scopes)
            workflow_meta_data["unilab"] = unilab
        elif "unilab" in workflow_meta_data:
            unilab.pop("resource_scopes", None)
            workflow_meta_data["unilab"] = unilab
    except CompositeGraphRewriteError as error:
        raise CompositeInvocationInvalid(f"{error.code} ({error.path})") from None
    return CompositeInvocationExpansion(
        nodes=tuple(expanded_nodes),
        edges=tuple(expanded_edges),
        resource_scopes=resource_scopes,
        workflow_meta_data=workflow_meta_data,
        inventory_requirements=tuple(dict(item) for item in inventory_requirements),
    )


def expand_composite_invocation(
    *,
    parent_graph: Mapping[str, Any],
    contract: Mapping[str, Any],
    invocation_uuid: str,
    pose: Mapping[str, Any],
    param: Mapping[str, Any],
    device_bindings: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """兼容旧调用方，仅返回冻结合同展开生成的节点和边。"""

    expansion = compile_composite_invocation(
        parent_graph=parent_graph,
        contract=contract,
        invocation_uuid=invocation_uuid,
        pose=pose,
        param=param,
        device_bindings=device_bindings,
    )
    return list(expansion.nodes), list(expansion.edges)


__all__ = [
    "CompositeInvocationExpansion",
    "CompositeInvocationInvalid",
    "compile_composite_invocation",
    "expand_composite_invocation",
]
