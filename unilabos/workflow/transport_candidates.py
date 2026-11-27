"""按已发布合同及同一实时派发检查发现候选，不在预检阶段占车。"""

from unilabos.app.scheduler.device_target import DeviceTargetUnavailable
from unilabos.app.scheduler.inventory.transport_allocation import VehicleCandidate
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.resource_reference import resolve_resource_reference
from unilabos.workflow.station_contract import bind_station_contract
from unilabos.workflow.transport_profile import validate_transport_contract


def candidates(contract, request, resolver, preflight):
    """只允许显式等价集合；每个动作和附属资源都必须准入。"""
    if contract["contract_digest"] != request["contract_digest"]:
        raise ValueError("搬运合同摘要与准备请求不符")
    profile = validate_transport_contract(contract)
    if profile is None:
        raise ValueError("搬运合同未声明等价车辆角色")
    result = []
    for alias in profile["equivalent_devices"]:
        if request["vehicle_selection"]["kind"] == "fixed" and alias != request["vehicle_selection"]["vehicle_id"]:
            continue
        # 未安装的等价车型不阻挡其它车辆；解析器异常或畸形回执仍失败关闭。
        if resolver is not None and resolver(alias) is None:
            continue
        resolved = resolve_resource_reference(alias, resolver)
        binding = {
            "vehicle_id": alias,
            "vehicle_resource_uuid": resolved["uuid"],
            "executor_bindings": {profile["role"]: resolved["uuid"]},
        }
        graph = bind_station_contract(contract, resolver, transport_binding=binding)
        plan, _ = ExecutionPlanBuilder().build(graph, run_mode="normal", target_node_uuid=None)
        try:
            for node in plan["nodes"]:
                if node.get("action_name") and node.get("device_id"):
                    selected = preflight(node)
                    if node.get("material_uuid") and selected["material_uuid"] != node["material_uuid"]:
                        raise ValueError("预检执行设备与冻结角色不符")
        except DeviceTargetUnavailable:
            continue
        keys = tuple(
            r["canonical_key"] for r in plan.get("resource_plan", {}).get("resources", []) if r.get("canonical_key")
        )
        result.append(VehicleCandidate(resolved["uuid"], alias, keys))
    return result
