"""将本站 published contract 的固定执行器绑定到当前库存，保留原合同摘要。"""
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from .authoring_identity import expanded_node_uuid
from .resource_reference import ResourceReferenceResolver, resolve_resource_reference


def _composite_bindings(
    graph: Mapping[str, Any],
    contract_resolver: Callable[[str], Mapping[str, Any]] | None,
) -> dict[str, str]:
    """按冻结子合同映射恢复最外层调用的设备选择，禁止猜测历史别名。"""
    nodes = {node["uuid"]: node for node in graph["nodes"]}
    composites = {
        uid: node.get("meta_data", {}).get("unilab", {}).get("composite")
        for uid, node in nodes.items()
    }
    result: dict[str, str] = {}
    for uid, composite in composites.items():
        if not composite:
            continue
        parent = nodes[uid].get("parent_uuid")
        ancestors: set[str] = set()
        while parent is not None:
            if parent in ancestors or parent not in nodes:
                raise ValueError("冻结组合调用层级无效")
            ancestors.add(parent)
            parent = nodes[parent].get("parent_uuid")
        if any(composites.get(parent) for parent in ancestors):
            continue
        if not composite.get("executor_requirements"):
            continue
        if contract_resolver is None:
            raise ValueError("冻结组合调用缺少子合同解析端口")
        try:
            child = contract_resolver(composite["contract_uuid"])
        except KeyError as error:
            raise ValueError("冻结组合调用引用的子合同不存在") from error
        if (child["uuid"] != composite["contract_uuid"]
                or child["contract_digest"] != composite.get("contract_digest")):
            raise ValueError("冻结组合调用的子合同摘要不匹配")
        bindings = composite.get("device_bindings", {})
        mapping = child["executor_binding_mapping"]
        if set(bindings) != set(mapping.values()):
            raise ValueError("冻结组合调用设备绑定不完整")
        for source_uuid, key in mapping.items():
            target = expanded_node_uuid(uid, source_uuid)
            if target not in nodes:
                raise ValueError("冻结组合调用设备节点映射无效")
            result[target] = bindings[key]
    return result


def bind_station_contract(
    contract: Mapping[str, Any], resolver: ResourceReferenceResolver | None,
    contract_resolver: Callable[[str], Mapping[str, Any]] | None = None,
    *, transport_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """仅补齐执行图副本的物料身份；失效、缺失或类型漂移的绑定失败关闭。"""
    graph = deepcopy(contract["graph_snapshot"])
    requirements = {r["key"]: r for r in contract.get("executor_requirements", [])}
    mapping = contract.get("executor_binding_mapping", {})
    composite_bindings = _composite_bindings(graph, contract_resolver)
    from .resource_lock_key import device_lock_key
    from .transport_profile import validate_transport_contract

    profile = validate_transport_contract(contract)
    selected: Mapping[str, Any] | None = None
    if transport_binding is not None:
        if profile is None or resolver is None:
            raise ValueError("当前合同没有声明可替代车辆角色")
        if (transport_binding.get("vehicle_id") not in profile["equivalent_devices"]
                or set(transport_binding.get("executor_bindings", {})) != {profile["role"]}):
            raise ValueError("车辆不属于合同声明的等价集合")
        selected = resolve_resource_reference(transport_binding["vehicle_resource_uuid"], resolver)
        alias = resolve_resource_reference(transport_binding["vehicle_id"], resolver)
        if (selected["uuid"] != alias["uuid"] or
                transport_binding["executor_bindings"][profile["role"]] != selected["uuid"]):
            raise ValueError("车辆角色、业务别名与规范资源身份不一致")
        if profile["executor_key"] not in requirements:
            raise ValueError("车辆角色未引用合同执行器要求")
        graph["resource_bindings"] = {
            **graph.get("resource_bindings", {}),
            profile["resource_alias"]: {"instance_uuid": selected["uuid"], "kind": "device",
                                        "canonical_key": device_lock_key(selected["uuid"])},
        }
    if transport_binding is not None and "route_binding" in transport_binding:
        graph["workflow"].setdefault("meta_data", {})["center_transport_route"] = deepcopy(transport_binding["route_binding"])
        graph["workflow"]["meta_data"]["center_transport_binding"] = deepcopy(dict(transport_binding))
    bindings: dict[str, str] = {}
    for node in graph["nodes"]:
        key = mapping.get(node["uuid"])
        if key is None:
            continue
        requirement = requirements.get(key)
        binding = node.get("meta_data", {}).get("unilab", {}).get("executor_binding", {})
        if requirement is None or binding.get("mode") != "fixed" or resolver is None:
            raise ValueError("本站发布合同缺少固定执行器或库存解析端口")
        material = (selected if selected is not None and key == profile["executor_key"] else
                    resolve_resource_reference(composite_bindings.get(node["uuid"], binding.get("device_id")), resolver))
        if material["resource_template_uuid"] != requirement["resource_template_uuid"]:
            raise ValueError("本站发布合同的执行器资源模板发生变化")
        identity = str(material["uuid"])
        if key in bindings and bindings[key] != identity:
            raise ValueError("同一执行器要求解析到不同设备")
        if any(other != key and value == identity for other, value in bindings.items()):
            raise ValueError("不同执行器要求不能隐式合并到同一设备")
        bindings[key] = identity
        node["material_uuid"] = identity
        if node["uuid"] in composite_bindings or (selected is not None and key == profile["executor_key"]):
            binding["device_id"] = identity
    if set(bindings) != set(requirements):
        raise ValueError("本站发布合同执行器要求未完整绑定")
    # 库位地址（台面位号、堆栈料架层号）在这里换成载架上的物理库位；解析端口由库存的资源引用解析器一并提供。
    from .site_address_binding import bind_graph_site_addresses

    bind_graph_site_addresses(graph, resolver)
    return graph
