"""已发布实验操作更新后的引用方组合调用替换。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from unilabos.workflow.authoring_identity import authoring_edge_uuid
from unilabos.workflow.composite_invocation import (
    CompositeInvocationInvalid,
    compile_composite_invocation,
)
from unilabos.workflow.json_codec import strict_json_equal


class CompositeContractRefreshPending(ValueError):
    """引用方暂不能安全采用新的实验操作发布合同。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定诊断码和可直接展示的中文说明。

        参数：``code`` 供前端稳定判断原因，``message`` 供用户阅读。返回：无。
        异常：构造函数不抛异常；实例由刷新边界抛出并由服务层收敛为诊断。
        """

        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompositeContractRefreshResult:
    """一次父图刷新产生的新图与已替换调用身份。"""

    graph: dict[str, Any]
    invocation_uuids: tuple[str, ...]


def composite_invocation_metadata(
    node: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """读取组合调用节点的服务端元数据。

    参数：``node`` 是父图中的一个节点投影。返回：组合调用元数据；普通节点或
    元数据形状不完整时返回 ``None``。异常：无，不修改调用方容器。
    """

    meta_data = node.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    composite = unilab.get("composite") if isinstance(unilab, Mapping) else None
    return composite if isinstance(composite, Mapping) else None


def graph_references_composite_child(
    graph: Mapping[str, Any],
    *,
    child_workflow_uuid: str,
    except_contract_uuid: str | None = None,
) -> bool:
    """判断工作流图是否引用指定实验操作的旧组合调用。

    参数：``graph`` 是完整引用方图，``child_workflow_uuid`` 是实验操作稳定身份；
    ``except_contract_uuid`` 可指定当前最新发布合同，匹配它的调用不算待刷新。
    返回：至少一个组合调用满足条件时为真。异常：无；普通节点和损坏的非对象
    元数据会被忽略，不能因此把无关工作流加入刷新集合。
    """

    for node in graph.get("nodes", []):
        if not isinstance(node, Mapping):
            continue
        composite = composite_invocation_metadata(node)
        if (
            composite is not None
            and composite.get("child_workflow_uuid") == child_workflow_uuid
            and (
                except_contract_uuid is None
                or composite.get("contract_uuid") != except_contract_uuid
            )
        ):
            return True
    return False


def _descriptors(
    contract: Mapping[str, Any],
    *,
    field: str,
    collection: str,
) -> list[Mapping[str, Any]]:
    """读取合同中的有序输入或输出描述符并关闭式校验形状。

    参数：``contract`` 是发布合同，``field`` 是输入或输出信封字段，
    ``collection`` 是信封内数组名。返回：保持合同顺序的描述符列表。异常：
    字段缺失、元素形状错误或参数名重复时抛出待处理诊断。
    """

    envelope = contract.get(field)
    values = envelope.get(collection) if isinstance(envelope, Mapping) else None
    if not isinstance(values, list) or any(
        not isinstance(item, Mapping)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("schema"), Mapping)
        or (field == "input_contract" and not isinstance(item.get("required"), bool))
        for item in values
    ):
        raise CompositeContractRefreshPending(
            "composite_contract_invalid",
            "实验操作发布合同的输入输出定义不完整",
        )
    names = [str(item["name"]) for item in values]
    if len(set(names)) != len(names):
        raise CompositeContractRefreshPending(
            "composite_contract_invalid",
            "实验操作发布合同包含重复参数名",
        )
    return values


def _assert_boundary_compatible(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> None:
    """确认新合同能继续使用父工作流原有的参数连接。

    参数：``previous`` 是引用方当前使用的旧合同，``current`` 是实验操作最新合同。
    返回：无。异常：参数被删除、改名、类型变化或新增必填参数时抛出待处理诊断，
    防止自动替换后把父工作流已有连线传给错误的参数。
    """

    if previous.get("workflow_uuid") != current.get("workflow_uuid"):
        raise CompositeContractRefreshPending(
            "composite_contract_invalid",
            "引用方使用的前后发布合同不属于同一个实验操作",
        )

    previous_inputs = _descriptors(
        previous,
        field="input_contract",
        collection="parameters",
    )
    current_inputs = _descriptors(
        current,
        field="input_contract",
        collection="parameters",
    )
    previous_outputs = _descriptors(
        previous,
        field="output_contract",
        collection="outputs",
    )
    current_outputs = _descriptors(
        current,
        field="output_contract",
        collection="outputs",
    )
    current_input_by_name = {str(item["name"]): item for item in current_inputs}
    current_output_by_name = {str(item["name"]): item for item in current_outputs}
    for descriptor in previous_inputs:
        name = str(descriptor["name"])
        replacement = current_input_by_name.get(name)
        if replacement is None or not strict_json_equal(
            descriptor.get("schema"), replacement.get("schema")
        ):
            raise CompositeContractRefreshPending(
                "composite_input_incompatible",
                f"实验操作输入参数 {name} 已删除、改名或类型不兼容",
            )
        if descriptor.get("required") is False and replacement.get("required") is True:
            raise CompositeContractRefreshPending(
                "composite_input_incompatible",
                f"实验操作输入参数 {name} 已改为必填",
            )
    previous_input_names = {str(item["name"]) for item in previous_inputs}
    for descriptor in current_inputs:
        if (
            str(descriptor["name"]) not in previous_input_names
            and descriptor.get("required") is True
        ):
            raise CompositeContractRefreshPending(
                "composite_input_required",
                f"实验操作新增必填参数 {descriptor['name']}，需要补充引用方传参",
            )
    for descriptor in previous_outputs:
        name = str(descriptor["name"])
        replacement = current_output_by_name.get(name)
        if replacement is None or not strict_json_equal(
            descriptor.get("schema"), replacement.get("schema")
        ):
            raise CompositeContractRefreshPending(
                "composite_output_incompatible",
                f"实验操作输出参数 {name} 已删除、改名或类型不兼容",
            )


def _assert_executor_compatible(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    bindings: Mapping[str, str],
    validate_bindings: Callable[[Sequence[Mapping[str, Any]], Mapping[str, str]], bool],
) -> dict[str, str]:
    """确认原设备绑定仍满足新合同，并返回去除废弃键后的绑定。

    参数：前两个合同提供旧、新设备要求；``bindings`` 是父调用现有绑定；
    ``validate_bindings`` 复核绑定物料当前仍有效。返回：只含新合同要求键的绑定。
    异常：要求新增、模板变化、物料失效或合同损坏时抛出待处理诊断。
    """

    previous_requirements = previous.get("executor_requirements")
    current_requirements = current.get("executor_requirements")
    if not isinstance(previous_requirements, list) or not isinstance(
        current_requirements, list
    ):
        raise CompositeContractRefreshPending(
            "composite_contract_invalid",
            "实验操作发布合同的设备要求不完整",
        )
    previous_by_key = {
        str(item.get("key")): item
        for item in previous_requirements
        if isinstance(item, Mapping) and isinstance(item.get("key"), str)
    }
    current_by_key = {
        str(item.get("key")): item
        for item in current_requirements
        if isinstance(item, Mapping) and isinstance(item.get("key"), str)
    }
    if len(previous_by_key) != len(previous_requirements) or len(current_by_key) != len(
        current_requirements
    ):
        raise CompositeContractRefreshPending(
            "composite_contract_invalid",
            "实验操作发布合同包含重复或无效的设备要求",
        )
    normalized: dict[str, str] = {}
    for key, requirement in current_by_key.items():
        previous_requirement = previous_by_key.get(key)
        material_uuid = bindings.get(key)
        if (
            previous_requirement is None
            or previous_requirement.get("resource_template_uuid")
            != requirement.get("resource_template_uuid")
            or not isinstance(material_uuid, str)
        ):
            raise CompositeContractRefreshPending(
                "composite_executor_incompatible",
                "实验操作的执行设备要求已变化，需要重新选择设备",
            )
        normalized[key] = material_uuid
    if not validate_bindings(current_requirements, normalized):
        raise CompositeContractRefreshPending(
            "composite_executor_unavailable",
            "引用方原来选择的设备已不满足实验操作要求",
        )
    return normalized


def _descendant_uuids(
    nodes: Sequence[Mapping[str, Any]], invocation_uuid: str
) -> set[str]:
    """返回一个调用根下的全部私有后代节点 UUID。

    参数：``nodes`` 是父图节点全集，``invocation_uuid`` 是组合调用根身份。返回：
    递归收集的后代 UUID 集合，不含调用根。异常：无；损坏的非字符串父引用不会
    被猜测为层级关系。
    """

    descendants: set[str] = set()
    frontier = {invocation_uuid}
    while frontier:
        children = {
            str(node["uuid"])
            for node in nodes
            if isinstance(node.get("uuid"), str)
            and isinstance(node.get("parent_uuid"), str)
            and str(node["parent_uuid"]) in frontier
            and str(node["uuid"]) not in descendants
        }
        descendants.update(children)
        frontier = children
    return descendants


def _handle_uuid(contract: Mapping[str, Any], io_type: str, name: str) -> str:
    """按发布合同模板身份重算一个边界连接点 UUID。

    参数：``contract`` 提供发布模板 UUID，``io_type`` 是输入或输出方向，
    ``name`` 是参数名。返回：与发布投影规则一致的确定性 UUID。异常：模板身份
    缺失或非法时抛出待处理诊断。
    """

    try:
        template_uuid = UUID(str(contract["node_template_uuid"]))
    except (KeyError, TypeError, ValueError):
        raise CompositeContractRefreshPending(
            "composite_contract_invalid",
            "实验操作发布合同缺少有效模板身份",
        ) from None
    return str(uuid5(template_uuid, f"published-handle:{io_type}:{name}"))


def _boundary_handle_remap(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, str]:
    """按参数名建立旧发布模板连接点到新模板连接点的映射。

    参数：``previous``/``current`` 是同一实验操作前后两版发布合同。返回：旧
    连接点（Handle）UUID 到新连接点 UUID 的完整映射，包含业务参数和
    ``ready``。异常：合同描述符或模板身份无效时抛出待处理诊断。
    """

    mapping = {
        _handle_uuid(previous, io_type, "ready"): _handle_uuid(
            current, io_type, "ready"
        )
        for io_type in ("target", "source")
    }
    for field, collection, io_type in (
        ("input_contract", "parameters", "target"),
        ("output_contract", "outputs", "source"),
    ):
        for descriptor in _descriptors(previous, field=field, collection=collection):
            name = str(descriptor["name"])
            mapping[_handle_uuid(previous, io_type, name)] = _handle_uuid(
                current, io_type, name
            )
    return mapping


def _replace_invocation(
    *,
    parent_graph: Mapping[str, Any],
    invocation: Mapping[str, Any],
    previous_contract: Mapping[str, Any],
    current_contract: Mapping[str, Any],
    validate_bindings: Callable[[Sequence[Mapping[str, Any]], Mapping[str, str]], bool],
) -> dict[str, Any]:
    """在独立父图副本中原子替换一个组合调用及其私有子树。

    参数：父图、调用根、前后合同与设备绑定验证器共同固定本次替换。返回：保留
    父图外部节点和连线、采用新子树的独立完整图。异常：合同或绑定不兼容、私有
    子节点被外部直连、重新展开失败时抛出待处理诊断；原父图保持不变。
    """

    # ``invocation_uuid`` 是父图中组合工作流调用（CompositeWorkflowInvocation）
    # 的稳定身份；替换私有子树时必须保持不变，外部连线才能继续引用同一节点。
    invocation_uuid = str(invocation["uuid"])
    composite = composite_invocation_metadata(invocation)
    assert composite is not None
    _assert_boundary_compatible(previous_contract, current_contract)
    raw_bindings = composite.get("device_bindings")
    if not isinstance(raw_bindings, Mapping):
        raise CompositeContractRefreshPending(
            "composite_contract_invalid",
            "引用方缺少实验操作设备绑定",
        )
    bindings = _assert_executor_compatible(
        previous_contract,
        current_contract,
        {str(key): str(value) for key, value in raw_bindings.items()},
        validate_bindings,
    )
    nodes = [deepcopy(dict(item)) for item in parent_graph.get("nodes", [])]
    edges = [deepcopy(dict(item)) for item in parent_graph.get("edges", [])]
    descendants = _descendant_uuids(nodes, invocation_uuid)
    subtree = {invocation_uuid, *descendants}
    retained_nodes = [node for node in nodes if str(node.get("uuid")) not in subtree]
    handle_remap = _boundary_handle_remap(previous_contract, current_contract)
    retained_edges: list[dict[str, Any]] = []
    for edge in edges:
        # 两个 UUID 是父图连线端点身份；仅调用根端点可以按参数名更换
        # 连接点（Handle），私有后代端点若被外部引用则关闭式拒绝。
        source_uuid = str(edge.get("source_node_uuid"))
        target_uuid = str(edge.get("target_node_uuid"))
        if source_uuid in subtree and target_uuid in subtree:
            continue
        if source_uuid in descendants or target_uuid in descendants:
            raise CompositeContractRefreshPending(
                "composite_private_edge_invalid",
                "引用方存在直接连接实验操作内部节点的连线，不能自动替换",
            )
        if source_uuid == invocation_uuid:
            old_handle = str(edge.get("source_handle_uuid"))
            if old_handle not in handle_remap:
                raise CompositeContractRefreshPending(
                    "composite_boundary_invalid",
                    "引用方存在无法按参数名迁移的实验操作输出连线",
                )
            edge["source_handle_uuid"] = handle_remap[old_handle]
        if target_uuid == invocation_uuid:
            old_handle = str(edge.get("target_handle_uuid"))
            if old_handle not in handle_remap:
                raise CompositeContractRefreshPending(
                    "composite_boundary_invalid",
                    "引用方存在无法按参数名迁移的实验操作输入连线",
                )
            edge["target_handle_uuid"] = handle_remap[old_handle]
        if source_uuid == invocation_uuid or target_uuid == invocation_uuid:
            edge["uuid"] = authoring_edge_uuid(
                workflow_uuid=str(parent_graph["workflow"]["uuid"]),
                source_node_uuid=source_uuid,
                source_handle_uuid=str(edge["source_handle_uuid"]),
                target_node_uuid=target_uuid,
                target_handle_uuid=str(edge["target_handle_uuid"]),
            )
        retained_edges.append(edge)
    base_graph = {
        **deepcopy(dict(parent_graph)),
        "workflow": _without_invocation_resource_projection(
            parent_graph["workflow"],
            invocation_uuid=invocation_uuid,
            descendant_uuids=descendants,
        ),
        "nodes": retained_nodes,
        "edges": retained_edges,
    }
    try:
        expansion = compile_composite_invocation(
            parent_graph=base_graph,
            contract=current_contract,
            invocation_uuid=invocation_uuid,
            pose=invocation.get("pose") or {},
            param=invocation.get("param") or {},
            device_bindings=bindings,
        )
    except CompositeInvocationInvalid as error:
        raise CompositeContractRefreshPending(
            "composite_expansion_invalid",
            str(error),
        ) from None
    expanded_nodes = list(expansion.nodes)
    expanded_edges = list(expansion.edges)
    root = expanded_nodes[0]
    root["parent_uuid"] = invocation.get("parent_uuid")
    for field in ("execution_policy", "disabled", "minimized"):
        if field in invocation:
            root[field] = deepcopy(invocation[field])
    old_meta = invocation.get("meta_data")
    if isinstance(old_meta, Mapping):
        preserved = deepcopy(dict(old_meta))
        unilab = preserved.get("unilab")
        new_unilab = root["meta_data"]["unilab"]
        if isinstance(unilab, Mapping):
            preserved_unilab = dict(unilab)
            old_input_bindings = preserved_unilab.get("input_bindings")
            if isinstance(old_input_bindings, Mapping):
                if any(key not in handle_remap for key in old_input_bindings):
                    raise CompositeContractRefreshPending(
                        "composite_boundary_invalid",
                        "引用方存在无法按参数名迁移的工作流输入绑定",
                    )
                preserved_unilab["input_bindings"] = {
                    handle_remap[key]: deepcopy(value)
                    for key, value in old_input_bindings.items()
                }
            preserved_unilab["composite"] = new_unilab["composite"]
            preserved["unilab"] = preserved_unilab
        else:
            preserved["unilab"] = new_unilab
        root["meta_data"] = preserved
    # 被替换子树内消费的旧需求（含本调用带入的需求）整体换成新展开结果。
    retained_requirements = [
        deepcopy(dict(item))
        for item in parent_graph.get("inventory_requirements") or []
        if str(item.get("consume_node_uuid") or "") not in subtree
    ]
    return {
        **base_graph,
        "workflow": {
            **base_graph["workflow"],
            "meta_data": expansion.workflow_meta_data,
        },
        "nodes": [*retained_nodes, *expanded_nodes],
        "edges": [*retained_edges, *expanded_edges],
        "inventory_requirements": [
            *retained_requirements,
            *[dict(item) for item in expansion.inventory_requirements],
        ],
    }


def _without_invocation_resource_projection(
    workflow: Mapping[str, Any],
    *,
    invocation_uuid: str,
    descendant_uuids: set[str],
) -> dict[str, Any]:
    """移除一次旧组合展开派生的作用域，并收缩父作用域成员。"""

    result = deepcopy(dict(workflow))
    raw_meta_data = result.get("meta_data")
    if not isinstance(raw_meta_data, Mapping):
        return result
    meta_data = deepcopy(dict(raw_meta_data))
    result["meta_data"] = meta_data
    raw_unilab = meta_data.get("unilab")
    if not isinstance(raw_unilab, Mapping):
        return result
    unilab = deepcopy(dict(raw_unilab))
    meta_data["unilab"] = unilab
    raw_scopes = unilab.get("resource_scopes")
    if not isinstance(raw_scopes, list):
        return result
    retained: list[Any] = []
    for raw_scope in raw_scopes:
        if not isinstance(raw_scope, Mapping):
            retained.append(deepcopy(raw_scope))
            continue
        scope_owner = raw_scope.get("composite_invocation_uuid")
        if isinstance(scope_owner, str) and (
            scope_owner == invocation_uuid or scope_owner in descendant_uuids
        ):
            continue
        scope = deepcopy(dict(raw_scope))
        members = scope.get("node_uuids")
        if isinstance(members, list):
            scope["node_uuids"] = [
                member for member in members if str(member) not in descendant_uuids
            ]
        retained.append(scope)
    if retained:
        unilab["resource_scopes"] = retained
    else:
        unilab.pop("resource_scopes", None)
    return result


def refresh_published_composite_invocations(
    *,
    parent_graph: Mapping[str, Any],
    current_contract: Mapping[str, Any],
    load_contract: Callable[[str], Mapping[str, Any]],
    validate_bindings: Callable[[Sequence[Mapping[str, Any]], Mapping[str, str]], bool],
) -> CompositeContractRefreshResult:
    """把引用方图中指向同一实验操作的旧调用替换为最新发布合同。

    参数：``parent_graph`` 是父工作流当前完整图，``current_contract`` 是刚发布的
    实验操作合同；``load_contract`` 读取旧合同，``validate_bindings`` 验证现有设备
    是否仍可使用。返回独立新图和替换的调用 UUID。任一调用不兼容时整个父图保持
    不变并抛 ``CompositeContractRefreshPending``，调用方可把原因返回给前端。
    """

    # ``child_workflow_uuid`` 是新合同所属实验操作的稳定身份；刷新按它查找所有
    # 旧合同调用，而不是按某一个易变化的发布合同 UUID 查找。
    child_workflow_uuid = current_contract.get("workflow_uuid")
    if not isinstance(child_workflow_uuid, str):
        raise CompositeContractRefreshPending(
            "composite_contract_invalid",
            "实验操作发布合同缺少工作流身份",
        )
    graph = deepcopy(dict(parent_graph))
    # ``invocation_uuids`` 收集本父图实际替换的稳定调用身份，供服务层形成诊断
    # 与验证；``candidates`` 固定替换顺序，避免遍历中修改图集合。
    invocation_uuids = []
    candidates = []
    for node in graph.get("nodes", []):
        if not isinstance(node, Mapping):
            continue
        composite = composite_invocation_metadata(node)
        if (
            composite is not None
            and composite.get("child_workflow_uuid") == child_workflow_uuid
            and composite.get("contract_uuid") != current_contract.get("uuid")
        ):
            candidates.append(str(node["uuid"]))
    for invocation_uuid in candidates:
        invocation = next(
            item for item in graph["nodes"] if str(item.get("uuid")) == invocation_uuid
        )
        composite = composite_invocation_metadata(invocation)
        assert composite is not None
        # ``contract_uuid`` 是该调用当前固定的旧发布合同身份；只用它读取兼容
        # 基线，替换成功后调用根会改为当前新合同身份。
        contract_uuid = composite.get("contract_uuid")
        if not isinstance(contract_uuid, str):
            raise CompositeContractRefreshPending(
                "composite_contract_invalid",
                "引用方缺少原有实验操作发布合同身份",
            )
        try:
            previous_contract = load_contract(contract_uuid)
        except KeyError:
            raise CompositeContractRefreshPending(
                "composite_contract_missing",
                "引用方使用的旧实验操作发布合同不存在",
            ) from None
        graph = _replace_invocation(
            parent_graph=graph,
            invocation=invocation,
            previous_contract=previous_contract,
            current_contract=current_contract,
            validate_bindings=validate_bindings,
        )
        invocation_uuids.append(invocation_uuid)
    return CompositeContractRefreshResult(
        graph=graph,
        invocation_uuids=tuple(invocation_uuids),
    )


__all__ = [
    "CompositeContractRefreshPending",
    "CompositeContractRefreshResult",
    "composite_invocation_metadata",
    "graph_references_composite_child",
    "refresh_published_composite_invocations",
]
