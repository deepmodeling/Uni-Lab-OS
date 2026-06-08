"""本地执行 AI4C workflow，用于绕过网页、FastAPI 和 unilab 后台调试设备动作。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO


AI4C_DIR = Path(__file__).resolve().parent
ROBOT_ARM_DEVICE_ID = "AI4C_robot_arm"
ROBOT_ARM_ROUTE_ALIASES = {"AI4C_station", "AI4C_robot_arm"}
COMMON_SNAPSHOT_VARIABLES = [
    "Robotic_Arm_Idle",
    "Robotic_Arm_Action_Complete",
    "Robotic_Arm_Target_Position_Code",
    "Robotic_Arm_Target_Pick_Place_Code",
    "Robotic_Arm_Action_Code",
    "Robotic_Arm_Action_Trigger",
]
ACTION_SNAPSHOT_VARIABLES = {
    "place_well_plate_to_pipetting_station": ["Pipetting_Station_Occupied"],
    "pick_well_plate_from_pipetting_station": ["Pipetting_Station_Occupied"],
    "place_well_plate_to_magnetic_stirrer": ["Magnetic_Stirrer_Occupied"],
    "pick_well_plate_from_magnetic_stirrer": ["Magnetic_Stirrer_Occupied"],
    "place_well_plate_to_hplc_station": ["HPLC_Pool_Occupied"],
    "pick_well_plate_from_hplc_station": ["HPLC_Pool_Occupied"],
    "place_well_plate_to_solid_weighing": ["Solid_Weighing_Occupied"],
    "pick_well_plate_from_solid_weighing": ["Solid_Weighing_Occupied"],
}


@dataclass(frozen=True)
class WorkflowNode:
    uuid: str
    name: str
    device_name: str
    param: dict[str, Any]
    disabled: bool = False


class WorkflowLogger:
    def __init__(self, writer: Callable[[str], Any] | None = None, file: TextIO | None = None):
        self._writer = writer or print
        self._file = file

    def log(self, message: str = "") -> None:
        self._writer(message)
        if self._file is not None:
            self._file.write(f"{message}\n")
            self._file.flush()


def method_name_from_template(template_name: str) -> str:
    """网页 workflow 中的 auto-* 节点名映射到 Python 方法名。"""
    return template_name.removeprefix("auto-")


def route_node_device(node: WorkflowNode) -> str:
    """本地调试时将 AI4C 机械臂相关节点统一路由到新的机械臂设备。"""
    if node.device_name in ROBOT_ARM_ROUTE_ALIASES:
        return ROBOT_ARM_DEVICE_ID
    return node.device_name


def collect_snapshot_variables(method_name: str, params: dict[str, Any]) -> list[str]:
    variables = list(COMMON_SNAPSHOT_VARIABLES)
    variables.extend(ACTION_SNAPSHOT_VARIABLES.get(method_name, []))

    position = int(params.get("position", 1) or 1)
    if method_name == "pick_well_plate_from_loading_rack":
        variables.append(f"Well_Plate_Loading_Rack_InPut[{position - 1}]")
    elif method_name == "place_well_plate_to_unloading_rack":
        variables.append(f"Well_Plate_Unloading_Rack_InPut[{position - 1}]")

    return list(dict.fromkeys(variables))


def snapshot_opc_state(plc: Any, variable_names: list[str]) -> dict[str, Any]:
    try:
        return plc.get_variables(variable_names, use_cache=False)
    except Exception as exc:
        return {name: {"success": False, "error": str(exc)} for name in variable_names}


def format_opc_variable_label(plc: Any, variable_name: str) -> str:
    """显示为 Browser 友好的中文名 + 代码英文名 + NodeId。"""
    name_mapping = getattr(plc, "_name_mapping", {}) or {}
    variables_to_find = getattr(plc, "_variables_to_find", {}) or {}
    chinese_name = name_mapping.get(variable_name, variable_name)
    node_id = variables_to_find.get(chinese_name, {}).get("node_id")
    if chinese_name != variable_name and node_id:
        return f"{chinese_name} [{variable_name}] ({node_id})"
    if chinese_name != variable_name:
        return f"{chinese_name} [{variable_name}]"
    if node_id:
        return f"{variable_name} ({node_id})"
    return variable_name


def log_snapshot(logger: WorkflowLogger, label: str, snapshot: dict[str, Any], plc: Any = None) -> None:
    logger.log(f"  OPC状态-{label}:")
    for name, value in snapshot.items():
        logger.log(f"    {format_opc_variable_label(plc, name)}: {value}")


def log_snapshot_diff(logger: WorkflowLogger, before: dict[str, Any], after: dict[str, Any], plc: Any = None) -> None:
    logger.log("  OPC状态变化:")
    for name in before:
        before_value = before.get(name)
        after_value = after.get(name)
        marker = " *" if before_value != after_value else ""
        logger.log(f"    {format_opc_variable_label(plc, name)}: {before_value} -> {after_value}{marker}")


def load_workflow_nodes(workflow_file: Path) -> tuple[list[WorkflowNode], list[dict[str, Any]]]:
    data = json.loads(workflow_file.read_text(encoding="utf-8"))
    workflow_data = data.get("data", data)
    nodes = [
        WorkflowNode(
            uuid=item["uuid"],
            name=item["name"],
            device_name=item.get("device_name") or item.get("resource_name", ""),
            param=item.get("param") or {},
            disabled=bool(item.get("disabled", False)),
        )
        for item in workflow_data.get("nodes", [])
    ]
    return nodes, workflow_data.get("edges", [])


def build_execution_order(nodes: list[WorkflowNode], edges: list[dict[str, Any]]) -> list[WorkflowNode]:
    """按 workflow edges 做拓扑排序；同层节点保持 JSON 中原始顺序。"""
    nodes_by_uuid = {node.uuid: node for node in nodes if not node.disabled}
    original_index = {node.uuid: index for index, node in enumerate(nodes) if not node.disabled}
    incoming_count = {uuid: 0 for uuid in nodes_by_uuid}
    outgoing: dict[str, list[str]] = {uuid: [] for uuid in nodes_by_uuid}

    for edge in edges:
        source = edge.get("source_node_uuid")
        target = edge.get("target_node_uuid")
        if source not in nodes_by_uuid or target not in nodes_by_uuid:
            continue
        outgoing[source].append(target)
        incoming_count[target] += 1

    ready = sorted(
        [uuid for uuid, count in incoming_count.items() if count == 0],
        key=lambda uuid: original_index[uuid],
    )
    ordered: list[WorkflowNode] = []

    while ready:
        current = ready.pop(0)
        ordered.append(nodes_by_uuid[current])
        for target in sorted(outgoing[current], key=lambda uuid: original_index[uuid]):
            incoming_count[target] -= 1
            if incoming_count[target] == 0:
                ready.append(target)
        ready.sort(key=lambda uuid: original_index[uuid])

    if len(ordered) != len(nodes_by_uuid):
        unresolved = sorted(set(nodes_by_uuid) - {node.uuid for node in ordered})
        raise ValueError(f"workflow 存在环或无法解析的依赖: {unresolved}")

    return ordered


def load_ai4c_graph_config(graph_file: Path) -> dict[str, dict[str, Any]]:
    graph = json.loads(graph_file.read_text(encoding="utf-8"))
    return {node["id"]: node.get("config", {}) for node in graph.get("nodes", [])}


def _resolve_path(path: str | None, base_dir: Path = AI4C_DIR) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    return candidate if candidate.is_absolute() else base_dir / candidate


def create_local_devices(
    graph_file: Path,
    opcua_url: str | None = None,
    csv_path: Path | None = None,
    use_subscription: bool | None = None,
    plc_action_timeout: float = 300.0,
) -> dict[str, Any]:
    from unilabos.devices.workstation.AI4C.AI4C_plc import AI4CPLCDevice
    from unilabos.devices.workstation.AI4C.AI4C_robot_arm import AI4CRobotArmDevice

    graph_config = load_ai4c_graph_config(graph_file)
    plc_config = dict(graph_config.get("AI4C_plc", {}))
    robot_config = dict(graph_config.get("AI4C_robot_arm", {}))

    url = opcua_url or plc_config.get("url")
    if not url:
        raise ValueError("缺少 OPC UA url，请在 AI4C.json 或 --url 中指定")

    csv = csv_path.resolve() if csv_path else _resolve_path(plc_config.get("csv_path"))
    if csv is None:
        raise ValueError("缺少 CSV 节点文件，请在 AI4C.json 或 --csv 中指定")

    if use_subscription is None:
        use_subscription = bool(plc_config.get("use_subscription", False))

    plc = AI4CPLCDevice(
        url=url,
        csv_path=str(csv),
        username=plc_config.get("username"),
        password=plc_config.get("password"),
        use_subscription=use_subscription,
    )
    robot_arm = AI4CRobotArmDevice(
        plc_device_id=robot_config.get("plc_device_id", "AI4C_plc"),
        plc_action_timeout=plc_action_timeout,
    )

    def call_plc_directly(function_name: str, function_args: dict[str, Any]) -> Any:
        function = getattr(plc, function_name)
        return function(**function_args)

    # 本地调试绕过 ROS ActionClient，仍复用 AI4CRobotArmDevice 的动作逻辑。
    robot_arm._call_plc_command = call_plc_directly  # type: ignore[method-assign]
    return {
        "AI4C_plc": plc,
        ROBOT_ARM_DEVICE_ID: robot_arm,
    }


def run_nodes(
    ordered_nodes: list[WorkflowNode],
    devices: dict[str, Any],
    logger: WorkflowLogger | None = None,
) -> list[dict[str, Any]]:
    logger = logger or WorkflowLogger()
    results: list[dict[str, Any]] = []
    plc = devices.get("AI4C_plc")

    for index, node in enumerate(ordered_nodes, start=1):
        device_name = route_node_device(node)
        device = devices.get(device_name)
        if device is None:
            raise KeyError(f"未创建本地设备实例: {device_name}")

        method_name = method_name_from_template(node.name)
        if not hasattr(device, method_name):
            raise AttributeError(f"{device_name} 不存在动作方法: {method_name}")

        snapshot_variables = collect_snapshot_variables(method_name, node.param)
        before = snapshot_opc_state(plc, snapshot_variables) if plc is not None else {}

        logger.log(f"[{index}/{len(ordered_nodes)}] {device_name}.{method_name}({node.param})")
        if before:
            log_snapshot(logger, "before", before, plc=plc)
        result = getattr(device, method_name)(**node.param)
        after = snapshot_opc_state(plc, snapshot_variables) if plc is not None else {}
        if after:
            log_snapshot(logger, "after", after, plc=plc)
            log_snapshot_diff(logger, before, after, plc=plc)
        logger.log(f"  -> {result}")
        results.append(
            {
                "uuid": node.uuid,
                "device_name": device_name,
                "method": method_name,
                "param": node.param,
                "opc_before": before,
                "opc_after": after,
                "result": result,
            }
        )
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(f"动作失败: {device_name}.{method_name}: {result}")

    return results


def run_workflow(
    workflow_file: Path,
    devices: dict[str, Any],
    logger: WorkflowLogger | None = None,
) -> list[dict[str, Any]]:
    nodes, edges = load_workflow_nodes(workflow_file)
    ordered_nodes = build_execution_order(nodes, edges)
    return run_nodes(ordered_nodes, devices, logger=logger)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run AI4C workflow locally without web/unilab services.")
    parser.add_argument("--workflow", type=Path, default=AI4C_DIR / "robot.json", help="网页导出的 workflow JSON")
    parser.add_argument("--graph", type=Path, default=AI4C_DIR / "AI4C.json", help="AI4C 设备图 JSON")
    parser.add_argument("--url", default=None, help="覆盖 AI4C.json 中的 OPC UA 服务地址")
    parser.add_argument("--csv", type=Path, default=None, help="覆盖 AI4C.json 中的 OPC UA 节点 CSV")
    parser.add_argument("--no-subscription", action="store_true", help="禁用 OPC UA 订阅，全部强制读取节点")
    parser.add_argument("--timeout", type=float, default=300.0, help="机械臂动作等待超时时间")
    parser.add_argument("--log-file", type=Path, default=None, help="将本地执行日志同步写入指定文件")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    devices = create_local_devices(
        graph_file=args.graph,
        opcua_url=args.url,
        csv_path=args.csv,
        use_subscription=False if args.no_subscription else None,
        plc_action_timeout=args.timeout,
    )
    log_handle = args.log_file.open("w", encoding="utf-8") if args.log_file else None
    logger = WorkflowLogger(file=log_handle)
    try:
        results = run_workflow(args.workflow, devices, logger=logger)
        logger.log(f"本地 workflow 执行完成，共 {len(results)} 个节点")
        return 0
    finally:
        if log_handle is not None:
            log_handle.close()
        plc = devices.get("AI4C_plc")
        if hasattr(plc, "disconnect"):
            plc.disconnect()


if __name__ == "__main__":
    sys.exit(main())

    # 使用命令：
    # python -m run_ai4c_workflow_local --graph AI4C.json --workflow robot.json --url opc.tcp://jdht1471820.bohrium.tech:50003 --csv ai4c_sim_updated.csv --no-subscription --timeout 60 --log-file tmp.log