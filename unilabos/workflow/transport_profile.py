"""发布合同显式声明的等价车辆角色；不允许任务输入任意替换设备。"""

from collections.abc import Mapping
from typing import Any


def validate_profile(profile: Any) -> dict[str, Any] | None:
    """公开合同仅包含角色声明；完整动作和区间在发布/绑定时另行验证。"""
    if profile is None:
        return None
    fields = {"schema_version", "role", "executor_key", "resource_alias", "equivalent_devices"}
    if (
        not isinstance(profile, Mapping)
        or set(profile) != fields
        or profile["schema_version"] != 1
        or type(profile["schema_version"]) is not int
        or profile["role"] != "transport_vehicle"
        or not all(isinstance(profile[k], str) and profile[k].strip() for k in ("executor_key", "resource_alias"))
        or not isinstance(profile["equivalent_devices"], list)
        or not profile["equivalent_devices"]
        or not all(isinstance(v, str) and v.strip() == v and v for v in profile["equivalent_devices"])
        or len(set(profile["equivalent_devices"])) != len(profile["equivalent_devices"])
    ):
        raise ValueError("搬运合同的等价车辆角色声明无效")
    if profile["resource_alias"] not in profile["equivalent_devices"]:
        raise ValueError("车辆资源别名必须属于显式等价集合")
    return dict(profile)


def transport_profile(graph: Mapping[str, Any]) -> dict[str, Any] | None:
    """从完整冻结图校验角色覆盖和连续车辆资源区间。"""
    profile = validate_profile(graph.get("workflow", {}).get("meta_data", {}).get("center_transport_allocation"))
    if profile is None:
        return None
    nodes = [
        node for node in graph.get("nodes", []) if node.get("meta_data", {}).get("unilab", {}).get("executor_binding")
    ]
    if not nodes or any(
        node["meta_data"]["unilab"]["executor_binding"] != {"mode": "fixed", "device_id": profile["resource_alias"]}
        for node in nodes
    ):
        raise ValueError("首版搬运角色必须覆盖全部设备动作，不能混用另一执行器或车辆别名")
    identities = {node["uuid"] for node in nodes}
    scopes = graph.get("workflow", {}).get("meta_data", {}).get("unilab", {}).get("resource_scopes", [])
    continuous = [
        scope
        for scope in scopes
        if scope.get("hard_boundary") is True
        and profile["resource_alias"] in scope.get("resources", [])
        and identities <= set(scope.get("node_uuids", []))
    ]
    if len(continuous) != 1 or any(
        any(
            alias in profile["equivalent_devices"] and alias != profile["resource_alias"]
            for alias in scope.get("resources", [])
        )
        for scope in scopes
    ):
        raise ValueError("全部搬运动作必须位于同一个连续车辆资源区间")
    return dict(profile)


def validate_transport_contract(contract: Mapping[str, Any]) -> dict[str, Any] | None:
    """发布和绑定共用完整覆盖检查，不能只替换声明中的一个节点。"""
    profile = transport_profile(contract["graph_snapshot"])
    if profile is None:
        return None
    nodes = [
        node
        for node in contract["graph_snapshot"]["nodes"]
        if node.get("meta_data", {}).get("unilab", {}).get("executor_binding")
    ]
    mapping = contract.get("executor_binding_mapping", {})
    requirements = contract.get("executor_requirements", [])
    if {r["key"] for r in requirements} != {profile["executor_key"]} or any(
        mapping.get(node["uuid"]) != profile["executor_key"] for node in nodes
    ):
        raise ValueError("车辆角色没有覆盖全部动作执行器要求")
    return profile
