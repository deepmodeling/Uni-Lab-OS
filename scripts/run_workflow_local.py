"""本地执行 szlab workflow，用于绕过网页、FastAPI 和 unilab 后台调试设备动作。"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
SZLAB_DIR = REPO_ROOT / "tests" / "szlab_poly_studio"
DEFAULT_RUNTIME_CONFIG = SZLAB_DIR / "runtime_configs" / "ai4c_runtime.json"
ROBOT_ARM_DEVICE_ID = "AI4C_robot_arm"


@dataclass(frozen=True)
class RuntimeDeviceFactoryConfig:
    plc_device_id: str = ""
    target_device_id: str = ""
    route_aliases: set[str] = field(default_factory=set)
    plc_class: str = ""
    target_class: str = ""
    target_config: dict[str, Any] = field(default_factory=dict)
    direct_plc_command_method: str | None = None
    timeout_config_key: str | None = None
    devices: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeOpcSnapshotConfig:
    common_variables: list[str] = field(default_factory=list)
    action_variables: dict[str, list[str]] = field(default_factory=dict)
    param_variables: dict[str, list[dict[str, str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeConfig:
    path: Path
    device_factory: RuntimeDeviceFactoryConfig
    opc_snapshot: RuntimeOpcSnapshotConfig


@dataclass(frozen=True)
class WorkflowNode:
    uuid: str
    name: str
    device_name: str
    param: dict[str, Any]
    disabled: bool = False


def load_runtime_config(config_path: Path | str | None = None) -> RuntimeConfig:
    path = Path(config_path or DEFAULT_RUNTIME_CONFIG)
    data = json.loads(path.read_text(encoding="utf-8"))
    device_data = data.get("device_factory") or {}
    snapshot_data = data.get("opc_snapshot") or {}

    device_factory = RuntimeDeviceFactoryConfig(
        plc_device_id=device_data.get("plc_device_id", ""),
        target_device_id=device_data.get("target_device_id", ""),
        route_aliases=set(device_data.get("route_aliases") or []),
        plc_class=device_data.get("plc_class", ""),
        target_class=device_data.get("target_class", ""),
        target_config=dict(device_data.get("target_config") or {}),
        direct_plc_command_method=device_data.get("direct_plc_command_method"),
        timeout_config_key=device_data.get("timeout_config_key"),
        devices=dict(device_data.get("devices") or {}),
    )
    opc_snapshot = RuntimeOpcSnapshotConfig(
        common_variables=list(snapshot_data.get("common_variables") or []),
        action_variables={
            str(method): list(variables)
            for method, variables in (snapshot_data.get("action_variables") or {}).items()
        },
        param_variables={
            str(method): [dict(item) for item in variables]
            for method, variables in (snapshot_data.get("param_variables") or {}).items()
        },
    )
    return RuntimeConfig(path=path, device_factory=device_factory, opc_snapshot=opc_snapshot)


class WorkflowLogger:
    def __init__(self, writer: Callable[[str], Any] | None = None, file: TextIO | None = None):
        self._writer = writer or print
        self._file = file

    def log(self, message: str = "", *, level: str = "info", detail: dict[str, Any] | None = None) -> None:
        try:
            self._writer(message, level=level, detail=detail)
        except TypeError:
            self._writer(message)
        if self._file is not None:
            self._file.write(f"{message}\n")
            self._file.flush()


def method_name_from_template(template_name: str) -> str:
    """网页 workflow 中的 auto-* 节点名映射到 Python 方法名。"""
    return template_name.removeprefix("auto-")


def route_node_device(node: WorkflowNode, runtime_config: RuntimeConfig | None = None) -> str:
    """本地调试时可通过配置将旧设备名路由到目标设备。"""
    runtime_config = runtime_config or load_runtime_config()
    device_factory = runtime_config.device_factory
    if node.device_name in device_factory.route_aliases:
        return device_factory.target_device_id
    return node.device_name


def collect_snapshot_variables(
    method_name: str,
    params: dict[str, Any],
    runtime_config: RuntimeConfig | None = None,
) -> list[str]:
    runtime_config = runtime_config or load_runtime_config()
    snapshot_config = runtime_config.opc_snapshot
    variables = list(snapshot_config.common_variables)
    variables.extend(snapshot_config.action_variables.get(method_name, []))

    template_context = _build_template_context(params)
    for item in snapshot_config.param_variables.get(method_name, []):
        template = item.get("template")
        if not template:
            continue
        variables.append(template.format(**template_context))

    return list(dict.fromkeys(variables))


def _build_template_context(params: dict[str, Any]) -> dict[str, Any]:
    context = dict(params)
    for key, value in params.items():
        try:
            numeric_value = int(value)
        except (TypeError, ValueError):
            continue
        context[f"{key}_minus_1"] = numeric_value - 1
    return context


def snapshot_opc_state(plc: Any, variable_names: list[str]) -> dict[str, Any]:
    if not variable_names:
        return {}
    try:
        return plc.get_variables(variable_names, use_cache=False)
    except Exception as exc:
        return {name: {"success": False, "error": str(exc)} for name in variable_names}


def format_opc_variable_label(plc: Any, variable_name: str) -> str:
    """显示为 Browser 友好的中文名 + 代码英文名 + NodeId。"""
    chinese_name, node_id = get_opc_variable_metadata(plc, variable_name)
    if chinese_name != variable_name and node_id:
        return f"{chinese_name} [{variable_name}] ({node_id})"
    if chinese_name != variable_name:
        return f"{chinese_name} [{variable_name}]"
    if node_id:
        return f"{variable_name} ({node_id})"
    return variable_name


def get_opc_variable_metadata(plc: Any, variable_name: str) -> tuple[str, str | None]:
    if hasattr(plc, "get_opc_variable_metadata"):
        return plc.get_opc_variable_metadata(variable_name)
    name_mapping = getattr(plc, "_name_mapping", {}) or {}
    variables_to_find = getattr(plc, "_variables_to_find", {}) or {}
    chinese_name = name_mapping.get(variable_name, variable_name)
    node_id = variables_to_find.get(chinese_name, {}).get("node_id")
    return chinese_name, node_id


def format_snapshot_detail(snapshot: dict[str, Any], plc: Any = None) -> dict[str, Any]:
    return {
        name: {
            "name": name,
            "label": format_opc_variable_label(plc, name),
            "display_name": get_opc_variable_metadata(plc, name)[0],
            "node_id": get_opc_variable_metadata(plc, name)[1],
            "value": value,
        }
        for name, value in snapshot.items()
    }


def build_snapshot_diff_detail(before: dict[str, Any], after: dict[str, Any], plc: Any = None) -> dict[str, Any]:
    changes = []
    for name in before:
        before_value = before.get(name)
        after_value = after.get(name)
        if before_value == after_value:
            continue
        display_name, node_id = get_opc_variable_metadata(plc, name)
        changes.append(
            {
                "name": name,
                "label": format_opc_variable_label(plc, name),
                "display_name": display_name,
                "node_id": node_id,
                "before": before_value,
                "value_goal": after_value,
                "after": after_value,
            }
        )
    return {
        "before": format_snapshot_detail(before, plc),
        "after": format_snapshot_detail(after, plc),
        "changes": changes,
    }


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


def _resolve_path(path: str | None, base_dir: Path = SZLAB_DIR) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    return candidate if candidate.is_absolute() else base_dir / candidate


def _load_class(class_path: str) -> type:
    module_name, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def create_local_devices(
    graph_file: Path,
    opcua_url: str | None = None,
    csv_path: Path | None = None,
    use_subscription: bool | None = None,
    plc_action_timeout: float = 300.0,
    runtime_config: RuntimeConfig | None = None,
) -> dict[str, Any]:
    runtime_config = runtime_config or load_runtime_config()
    device_factory = runtime_config.device_factory
    graph_config = load_ai4c_graph_config(graph_file)
    if device_factory.devices:
        devices: dict[str, Any] = {}
        for device_id, class_path in device_factory.devices.items():
            device_config = dict(graph_config.get(device_id, {}))
            if opcua_url and "url" in device_config:
                device_config["url"] = opcua_url
            if plc_action_timeout and "timeout" in device_config:
                device_config["timeout"] = plc_action_timeout
            device_class = _load_class(class_path)
            devices[device_id] = device_class(**device_config)
        for device in devices.values():
            plc_device_id = getattr(device, "plc_device_id", "")
            if plc_device_id and hasattr(device, "set_plc_gateway"):
                plc = devices.get(plc_device_id)
                if plc is not None:
                    device.set_plc_gateway(plc)
        return devices

    plc_config = dict(graph_config.get(device_factory.plc_device_id, {}))
    target_graph_config = dict(graph_config.get(device_factory.target_device_id, {}))

    url = opcua_url or plc_config.get("url")
    if not url:
        raise ValueError("缺少 OPC UA url，请在设备图或 --url 中指定")

    csv = csv_path.resolve() if csv_path else _resolve_path(plc_config.get("csv_path"), graph_file.parent)

    if use_subscription is None:
        use_subscription = bool(plc_config.get("use_subscription", False))

    plc_class = _load_class(device_factory.plc_class)
    target_class = _load_class(device_factory.target_class)
    plc_kwargs = {
        "url": url,
        "csv_path": str(csv) if csv is not None else None,
        "username": plc_config.get("username"),
        "password": plc_config.get("password"),
        "use_subscription": use_subscription,
    }
    plc = plc_class(
        **plc_kwargs,
    )
    target_config = dict(device_factory.target_config)
    target_config.update(target_graph_config)
    if device_factory.timeout_config_key:
        target_config[device_factory.timeout_config_key] = plc_action_timeout
    target_device = target_class(**target_config)

    def call_plc_directly(function_name: str, function_args: dict[str, Any]) -> Any:
        function = getattr(plc, function_name)
        return function(**function_args)

    if device_factory.direct_plc_command_method:
        # 本地调试绕过 ROS ActionClient，仍复用目标设备的动作逻辑。
        setattr(target_device, device_factory.direct_plc_command_method, call_plc_directly)

    return {
        device_factory.plc_device_id: plc,
        device_factory.target_device_id: target_device,
    }


def run_nodes(
    ordered_nodes: list[WorkflowNode],
    devices: dict[str, Any],
    logger: WorkflowLogger | None = None,
    runtime_config: RuntimeConfig | None = None,
) -> list[dict[str, Any]]:
    logger = logger or WorkflowLogger()
    runtime_config = runtime_config or load_runtime_config()
    results: list[dict[str, Any]] = []
    default_plc = devices.get(runtime_config.device_factory.plc_device_id)

    for index, node in enumerate(ordered_nodes, start=1):
        device_name = route_node_device(node, runtime_config)
        device = devices.get(device_name)
        if device is None:
            raise KeyError(f"未创建本地设备实例: {device_name}")

        method_name = method_name_from_template(node.name)
        if not hasattr(device, method_name):
            raise AttributeError(f"{device_name} 不存在动作方法: {method_name}")

        snapshot_variables = collect_snapshot_variables(method_name, node.param, runtime_config)
        snapshot_client = default_plc or (device if hasattr(device, "get_variables") else None)
        before = snapshot_opc_state(snapshot_client, snapshot_variables) if snapshot_client is not None else {}

        logger.log(
            f"[{index}/{len(ordered_nodes)}] {device_name}.{method_name}({node.param})",
            detail={"device_name": device_name, "method": method_name, "param": node.param},
        )
        if before:
            logger.log(
                f"OPC状态采样: {len(before)} 个变量",
                detail={"before": format_snapshot_detail(before, snapshot_client)},
            )
        result = getattr(device, method_name)(**node.param)
        after = snapshot_opc_state(snapshot_client, snapshot_variables) if snapshot_client is not None else {}
        if after:
            diff_detail = build_snapshot_diff_detail(before, after, plc=snapshot_client)
            logger.log(
                f"OPC状态变化: {len(diff_detail['changes'])}/{len(before)} 个变量变化",
                detail=diff_detail,
            )
        logger.log(f"动作结果: {result}", detail={"result": result})
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
    runtime_config: RuntimeConfig | None = None,
) -> list[dict[str, Any]]:
    nodes, edges = load_workflow_nodes(workflow_file)
    ordered_nodes = build_execution_order(nodes, edges)
    return run_nodes(ordered_nodes, devices, logger=logger, runtime_config=runtime_config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run szlab workflow locally without web/unilab services.")
    parser.add_argument("--ui", action="store_true", help="启动 szlab 本地 workflow 调试界面")
    parser.add_argument("--host", default="127.0.0.1", help="测试界面监听地址")
    parser.add_argument("--port", type=int, default=8014, help="测试界面监听端口")
    parser.add_argument("--no-browser", action="store_true", help="启动测试界面时不自动打开浏览器")
    parser.add_argument("--preset", default="ai4c", help="UI 使用的 preset 名称")
    parser.add_argument("--runtime-config", type=Path, default=None, help="本地运行配置 JSON")
    parser.add_argument("--workflow", type=Path, default=SZLAB_DIR / "robot.json", help="workflow JSON")
    parser.add_argument("--graph", type=Path, default=SZLAB_DIR / "AI4C.json", help="设备图 JSON")
    parser.add_argument("--url", default=None, help="覆盖设备图中的 OPC UA 服务地址")
    parser.add_argument("--csv", type=Path, default=None, help="覆盖设备图中的 OPC UA 节点 CSV")
    parser.add_argument("--no-subscription", action="store_true", help="禁用 OPC UA 订阅，全部强制读取节点")
    parser.add_argument("--timeout", type=float, default=300.0, help="机械臂动作等待超时时间")
    parser.add_argument("--log-file", type=Path, default=None, help="将本地执行日志同步写入指定文件")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.ui:
        from scripts.workflow_ui import start_ui

        start_ui(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
            preset_name=args.preset,
            runtime_config=load_runtime_config(args.runtime_config) if args.runtime_config else None,
        )
        return 0

    runtime_config = load_runtime_config(args.runtime_config)
    devices = create_local_devices(
        graph_file=args.graph,
        opcua_url=args.url,
        csv_path=args.csv,
        use_subscription=False if args.no_subscription else None,
        plc_action_timeout=args.timeout,
        runtime_config=runtime_config,
    )
    log_handle = args.log_file.open("w", encoding="utf-8") if args.log_file else None
    logger = WorkflowLogger(file=log_handle)
    try:
        results = run_workflow(args.workflow, devices, logger=logger, runtime_config=runtime_config)
        logger.log(f"本地 workflow 执行完成，共 {len(results)} 个节点")
        return 0
    finally:
        if log_handle is not None:
            log_handle.close()
        for device in devices.values():
            if hasattr(device, "disconnect"):
                device.disconnect()


if __name__ == "__main__":
    sys.exit(main())
