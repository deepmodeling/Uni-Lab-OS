"""搬运工作流创建前的许可与整车占用校验。"""

from __future__ import annotations

from typing import Any, Mapping

from unilabos.center_protocol.transport import validate_transport_context
from unilabos.workflow.resource_lock_key import device_lock_key
from unilabos.workflow.resource_lock_plan import deserialize_resource_plan
from unilabos.workflow.resource_reference import ResourceReferenceResolver, resolve_resource_reference
from unilabos.workflow.store import StoreConflict


def submission_context(
    connection: Any, invocation_uuid: str | None, input_value: Mapping[str, Any]
) -> dict[str, Any] | None:
    """仅使用已经领取、固定在本地账本中的中心许可，不接受输入自带授权。"""
    from unilabos.workflow.center_boundary import claimed_transport_context

    supplied = input_value.get("center_transport")
    if invocation_uuid is None:
        if "center_transport" in input_value:
            raise StoreConflict("搬运输入必须绑定中心领取账本，禁止本地输入自行授权")
        return None
    context = claimed_transport_context(connection, invocation_uuid)
    if context is None:
        if "center_transport" in input_value:
            raise StoreConflict("冻结包未附带中心搬运许可")
        return None
    if context != validate_transport_context(supplied):
        raise StoreConflict("搬运输入与中心冻结许可不一致")
    return validate_transport_context(context)


def validate_vehicle_interval(
    plan: Mapping[str, Any], context: Mapping[str, Any],
    resolver: ResourceReferenceResolver | None = None,
) -> None:
    """以库存权威解析许可中的车辆，验证覆盖整个搬运计划的显式连续区间。"""
    try:
        resources = deserialize_resource_plan(plan["resource_plan"])
        identity = context["vehicle_id"]
        if resolver is not None:
            # 许可保留业务车辆 ID；实际计划已把设备锁绑定到库存 UUID。
            # 不匹配时拒绝，不能退回名称匹配或改写冻结许可。
            identity = resolve_resource_reference(identity, resolver)["uuid"]
        vehicle = device_lock_key(identity)
        matching = {
            r.resource_id
            for r in resources.resources
            if r.canonical_key == vehicle and r.kind in {"device", "robot", "motion"}
        }
        physical = {
            str(node["uuid"])
            for node in plan.get("nodes", [])
            if node.get("device_id") or node.get("executor_binding", {}).get("device_id")
        }
        # 边界 Job 是纯数据节点；所有原计划节点必须被同一车辆区间覆盖。
        nodes = {
            str(node["uuid"])
            for node in plan.get("nodes", [])
            if node.get("kind") not in {"workflow_input", "workflow_output"}
        }
        if not physical or not any(
            interval.resource_id in matching and interval.explicit_boundary and nodes <= set(interval.node_uuids)
            for interval in resources.intervals
        ):
            raise ValueError("缺少覆盖所有搬运动作的显式整车占用区间")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise StoreConflict(f"搬运工作流的车辆锁不完整：{error}") from error
