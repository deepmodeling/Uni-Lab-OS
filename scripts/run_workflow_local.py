"""本地执行 szlab workflow，用于绕过网页、FastAPI 和 unilab 后台调试设备动作。"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from scripts.task_action_result import ActionReturnedFailure, find_action_failure


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
    method: str = ""
    legacy_route_compatible: bool = True


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


def iter_action_logs(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    logs = result.get("logs")
    if logs is None and isinstance(result.get("data"), dict):
        logs = result["data"].get("logs")
    if not isinstance(logs, list):
        return []

    entries: list[dict[str, Any]] = []
    for item in logs:
        if isinstance(item, str):
            entries.append({"message": item, "detail": None})
        elif isinstance(item, dict):
            message = item.get("message")
            if message:
                entries.append({"message": str(message), "detail": item.get("detail")})
    return entries


def iter_opc_wait_logs(*sources: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_source_ids: set[int] = set()
    for source in sources:
        for candidate in _iter_opc_wait_sources(source):
            if candidate is None or id(candidate) in seen_source_ids:
                continue
            seen_source_ids.add(id(candidate))
            drain = getattr(candidate, "drain_opc_wait_events", None)
            if not callable(drain):
                continue
            for item in drain() or []:
                if not isinstance(item, dict):
                    continue
                entry = _normalize_opc_wait_log(item)
                if entry is not None:
                    entries.append(entry)
    return entries


def _normalize_opc_wait_log(event: dict[str, Any]) -> dict[str, Any] | None:
    message = event.get("message")
    if not message:
        return None
    detail = dict(event.get("detail") or {})
    if event.get("phase") and not detail.get("phase"):
        detail["phase"] = event["phase"]
    level = str(event.get("level") or "info").strip().lower()
    if (
        detail.get("type") == "opc_wait"
        and detail.get("phase") == "finish"
        and detail.get("success") is False
    ):
        level = "error"
    return {"message": str(message), "level": level, "detail": detail}


def bind_opc_wait_logger(logger: WorkflowLogger, *sources: Any) -> Callable[[], None]:
    bound_sources: list[Any] = []

    def write_wait_event(event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        entry = _normalize_opc_wait_log(event)
        if entry is not None:
            logger.log(
                entry["message"],
                level=entry["level"],
                detail=entry["detail"],
            )

    seen_source_ids: set[int] = set()
    for source in sources:
        for candidate in _iter_opc_wait_sources(source):
            if candidate is None or id(candidate) in seen_source_ids:
                continue
            seen_source_ids.add(id(candidate))
            setter = getattr(candidate, "set_opc_wait_event_writer", None)
            if not callable(setter):
                continue
            setter(write_wait_event)
            bound_sources.append(candidate)

    def unbind() -> None:
        for candidate in bound_sources:
            setter = getattr(candidate, "set_opc_wait_event_writer", None)
            if callable(setter):
                setter(None)

    return unbind


def _iter_opc_wait_sources(source: Any) -> list[Any]:
    if source is None:
        return []
    candidates = [source]
    for attr in ("_client", "_plc_gateway"):
        nested = getattr(source, attr, None)
        if nested is not None:
            candidates.append(nested)
    return candidates


def method_name_from_template(template_name: str) -> str:
    """网页 workflow 中的 auto-* 节点名映射到 Python 方法名。"""
    return template_name.removeprefix("auto-")


def node_method(node: WorkflowNode) -> str:
    """返回节点的真实动作方法；仅存量节点允许从模板名推导。"""
    if node.method:
        return node.method
    if node.legacy_route_compatible:
        return method_name_from_template(node.name)
    raise ValueError(f"workflow node 缺少显式 method: {node.uuid}")


def route_node_device(node: WorkflowNode, runtime_config: RuntimeConfig | None = None) -> str:
    """本地调试时可通过配置将旧设备名路由到目标设备。"""
    if not node.legacy_route_compatible:
        return node.device_name
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

    if method_name == "measure_density":
        try:
            density_count = int(params.get("density_measurement_count", 1))
        except (TypeError, ValueError):
            density_count = 1
        density_count = max(1, min(density_count, 10))
        variables.extend(
            f"{base_name}[{index}]"
            for base_name in ("S09抽液天平读数", "S09放液天平读数")
            for index in range(density_count)
        )

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
        snapshot = plc.get_variables(variable_names, use_cache=False)
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        return {
            name: {
                "success": False,
                "error": message,
                "exception_type": exc.__class__.__name__,
            }
            for name in variable_names
        }
    if isinstance(snapshot, dict):
        return snapshot
    return {
        name: {
            "success": False,
            "error": f"变量读取接口返回无效类型: {type(snapshot).__name__}",
        }
        for name in variable_names
    }


def opc_snapshot_failures(
    snapshot: dict[str, Any],
    variable_names: list[str] | None = None,
    *,
    plc: Any = None,
) -> list[dict[str, Any]]:
    """提取 OPC 快照中的明确读取失败，并保留变量定位信息。"""
    failures: list[dict[str, Any]] = []
    names = variable_names if variable_names is not None else list(snapshot)
    for name in names:
        item = snapshot.get(name)
        if name not in snapshot:
            error = "读取结果未返回该变量"
            exception_type = ""
        elif not isinstance(item, dict):
            continue
        elif item.get("success") is not False and "value" in item:
            continue
        else:
            error = str(
                item.get("error") or item.get("message") or "变量读取失败"
            )
            exception_type = str(item.get("exception_type") or "")
        try:
            display_name, node_id = get_opc_variable_metadata(plc, name)
        except Exception:
            display_name, node_id = name, None
        failure = {
            "name": name,
            "display_name": display_name,
            "node_id": node_id,
            "error": error,
        }
        if exception_type:
            failure["exception_type"] = exception_type
        failures.append(failure)
    return failures


def log_opc_snapshot_failures(
    logger: WorkflowLogger,
    snapshot: dict[str, Any],
    variable_names: list[str],
    *,
    phase: str,
    plc: Any = None,
) -> list[dict[str, Any]]:
    failures = opc_snapshot_failures(snapshot, variable_names, plc=plc)
    if not failures:
        return []
    phase_label = {
        "sampling_before": "动作前",
        "sampling_live": "动作中",
        "sampling_after": "动作后",
    }.get(phase, phase)
    logger.log(
        f"OPC 状态读取失败（{phase_label}）："
        f"{len(failures)}/{len(variable_names)} 个变量失败",
        level="error",
        detail={
            "type": "opc_snapshot_read_failed",
            "phase": phase,
            "failed_count": len(failures),
            "variable_count": len(variable_names),
            "failures": failures,
        },
    )
    return failures


def log_opc_snapshot_recovery(
    logger: WorkflowLogger,
    failures: list[dict[str, Any]],
    *,
    phase: str,
) -> None:
    """在一段连续 OPC 读取失败结束时补一条恢复事件。"""
    if not failures:
        return
    phase_label = {
        "sampling_live": "动作中",
        "sampling_after": "动作后",
    }.get(phase, phase)
    logger.log(
        f"OPC 状态读取已恢复（{phase_label}）："
        f"{len(failures)} 个变量恢复读取",
        detail={
            "type": "opc_snapshot_recovered",
            "phase": phase,
            "recovered_count": len(failures),
            "recovered_failures": failures,
        },
    )


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
                "after": after_value,
            }
        )
    return {
        "before": format_snapshot_detail(before, plc),
        "after": format_snapshot_detail(after, plc),
        "changes": changes,
    }


def workflow_node_from_mapping(item: Any) -> WorkflowNode:
    """严格解析显式节点格式，并兼容存量网页 workflow 节点。"""
    if not isinstance(item, dict):
        raise ValueError("workflow node 必须是对象")
    explicit_fields = {"workflow_node_id", "device_id", "method", "params"}
    legacy_fields = {"uuid", "name", "device_name", "param"}
    common_fields = {"disabled"}
    item_fields = set(item)
    explicit_present = explicit_fields.intersection(item_fields)
    legacy_present = legacy_fields.intersection(item_fields)
    if explicit_present and legacy_present:
        raise ValueError(
            "workflow node 字段格式冲突: explicit 不允许 legacy 字段 "
            f"{', '.join(sorted(legacy_present))}"
        )
    if explicit_present:
        unknown_fields = item_fields - explicit_fields - common_fields - {
            "opc_variables"
        }
        if unknown_fields:
            raise ValueError(
                f"workflow node 不支持字段: {', '.join(sorted(unknown_fields))}"
            )
        missing = explicit_fields - item_fields
        if missing:
            raise ValueError(
                f"workflow node explicit 缺少字段: {', '.join(sorted(missing))}"
            )
        node_id = _required_node_string(item, "workflow_node_id")
        device_name = _required_node_string(item, "device_id")
        method = _required_node_string(item, "method")
        if not isinstance(item["params"], dict):
            raise ValueError(f"workflow node params 必须是对象: {node_id}")
        opc_variables = item.get("opc_variables", [])
        if (
            not isinstance(opc_variables, list)
            or any(
                not isinstance(variable, str) or not variable.strip()
                for variable in opc_variables
            )
        ):
            raise ValueError(
                f"workflow node opc_variables 必须是非空字符串数组: {node_id}"
            )
        disabled = item.get("disabled", False)
        if not isinstance(disabled, bool):
            raise ValueError(f"workflow node disabled 必须为 bool: {node_id}")
        return WorkflowNode(
            uuid=node_id,
            name=method,
            device_name=device_name,
            param=dict(item["params"]),
            disabled=disabled,
            method=method,
            legacy_route_compatible=False,
        )

    unknown_fields = item_fields - legacy_fields - common_fields
    if unknown_fields:
        raise ValueError(
            f"workflow node 不支持字段: {', '.join(sorted(unknown_fields))}"
        )
    missing = legacy_fields - item_fields
    if missing:
        raise ValueError(
            f"workflow node legacy 缺少字段: {', '.join(sorted(missing))}"
        )
    node_id = _required_node_string(item, "uuid")
    name = _required_node_string(item, "name")
    device_name = _required_node_string(item, "device_name")
    param = item["param"]
    if not isinstance(param, dict):
        raise ValueError(f"workflow node param 必须是对象: {node_id}")
    disabled = item.get("disabled", False)
    if not isinstance(disabled, bool):
        raise ValueError(f"workflow node disabled 必须为 bool: {node_id}")
    return WorkflowNode(
        uuid=node_id,
        name=name,
        device_name=device_name,
        param=dict(param),
        disabled=disabled,
        legacy_route_compatible=True,
    )


def _required_node_string(item: dict[str, Any], field_name: str) -> str:
    value = item.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"workflow node {field_name} 必须是非空字符串")
    return value.strip()


def load_workflow_nodes(workflow_file: Path) -> tuple[list[WorkflowNode], list[dict[str, Any]]]:
    data = json.loads(workflow_file.read_text(encoding="utf-8"))
    workflow_data = data.get("data", data)
    nodes = [workflow_node_from_mapping(item) for item in workflow_data.get("nodes", [])]
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


def normalize_opcua_url(url: str | None) -> str | None:
    if url is None:
        return None
    normalized = str(url).strip()
    if normalized and "://" not in normalized:
        normalized = f"opc.tcp://{normalized}"
    return normalized


def ignore_opcua_token_time_drift() -> None:
    """现场调试兼容 PLC/OPC UA Server 时间严重漂移的情况。"""
    from opcua import ua
    from opcua.common.connection import SecureConnection

    def _check_sym_header_ignore_prev_token_timeout(self: Any, security_header: Any) -> None:
        assert isinstance(
            security_header,
            ua.SymmetricAlgorithmHeader,
        ), "Expected SymAlgHeader, got: {0}".format(security_header)
        if security_header.TokenId != self.security_token.TokenId:
            if security_header.TokenId != self.next_security_token.TokenId:
                if self._allow_prev_token and security_header.TokenId == self.prev_security_token.TokenId:
                    return
                raise ua.UaError(
                    "Invalid security token id {}, expected {} or {}".format(
                        security_header.TokenId,
                        self.security_token.TokenId,
                        self.next_security_token.TokenId,
                    )
                )
            self.revolve_tokens()
            self.security_policy.make_remote_symmetric_key(self.local_nonce, self.remote_nonce)
            self.prev_security_token = ua.ChannelSecurityToken()
        if self.prev_security_token.TokenId != 0:
            self.security_policy.make_remote_symmetric_key(self.local_nonce, self.remote_nonce)
            self.prev_security_token = ua.ChannelSecurityToken()

    SecureConnection._check_sym_header = _check_sym_header_ignore_prev_token_timeout


def pc_to_plc_clear_values() -> dict[str, int]:
    from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import (
        ROBOT_TASK_NUMBER_VARIABLE,
        ROBOT_WRITE_DONE_VARIABLE,
        ROBOT_ACTION_SPECS,
    )

    values = {ROBOT_WRITE_DONE_VARIABLE: False, ROBOT_TASK_NUMBER_VARIABLE: 0}
    for spec in ROBOT_ACTION_SPECS.values():
        for variable in spec.variables:
            values.setdefault(variable, 0)
    return values


def clear_pc_to_plc_variables(
    devices: dict[str, Any],
    runtime_config: RuntimeConfig,
    logger: WorkflowLogger | None = None,
) -> dict[str, Any]:
    logger = logger or WorkflowLogger()
    plc = devices.get(runtime_config.device_factory.plc_device_id)
    if plc is None or not hasattr(plc, "write_variable"):
        raise KeyError(f"未创建可写 PLC 设备: {runtime_config.device_factory.plc_device_id}")

    writes: dict[str, Any] = {}
    already_clear: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, value in pc_to_plc_clear_values().items():
        try:
            plc.write_variable(name, value)
            writes[name] = value
        except Exception as exc:
            try:
                current_value = plc.read_variable(name, use_cache=False)
            except Exception:
                current_value = None
            else:
                if current_value == value:
                    already_clear[name] = value
                    continue
            errors[name] = str(exc)

    detail = {"written_variables": writes, "already_clear_variables": already_clear, "errors": errors}
    if errors:
        logger.log(f"清空 PC->PLC 变量失败: {len(errors)} 个变量", level="error", detail=detail)
        raise RuntimeError(f"清空 PC->PLC 变量失败: {errors}")
    logger.log(f"已清空 PC->PLC 变量: 写入 {len(writes)} 个，已清零 {len(already_clear)} 个", detail=detail)
    return detail


def create_local_devices(
    graph_file: Path,
    opcua_url: str | None = None,
    csv_path: Path | None = None,
    use_subscription: bool | None = None,
    runtime_config: RuntimeConfig | None = None,
) -> dict[str, Any]:
    runtime_config = runtime_config or load_runtime_config()
    opcua_url = normalize_opcua_url(opcua_url)
    device_factory = runtime_config.device_factory
    graph_config = load_ai4c_graph_config(graph_file)
    if device_factory.devices:
        devices: dict[str, Any] = {}
        device_items = list(device_factory.devices.items())
        device_items.sort(key=lambda item: item[0] != device_factory.plc_device_id)
        has_plc_gateway = bool(device_factory.plc_device_id and device_factory.plc_device_id in device_factory.devices)
        for device_id, class_path in device_items:
            device_config = dict(graph_config.get(device_id, {}))
            if opcua_url:
                device_config["url"] = opcua_url
            if csv_path:
                device_config["csv_path"] = str(csv_path.resolve())
            if has_plc_gateway and device_id != device_factory.plc_device_id:
                device_config.setdefault("use_plc_gateway", True)
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

        method_name = node_method(node)
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
        before_failures = log_opc_snapshot_failures(
            logger,
            before,
            snapshot_variables,
            phase="sampling_before",
            plc=snapshot_client,
        )
        unbind_wait_logger = bind_opc_wait_logger(logger, default_plc, device, snapshot_client)
        try:
            result = getattr(device, method_name)(**node.param)
        finally:
            unbind_wait_logger()
        after = snapshot_opc_state(snapshot_client, snapshot_variables) if snapshot_client is not None else {}
        if after:
            diff_detail = build_snapshot_diff_detail(before, after, plc=snapshot_client)
            logger.log(
                f"OPC状态变化: {len(diff_detail['changes'])}/{len(before)} 个变量变化",
                detail=diff_detail,
            )
        after_failures = log_opc_snapshot_failures(
            logger,
            after,
            snapshot_variables,
            phase="sampling_after",
            plc=snapshot_client,
        )
        if before_failures and not after_failures:
            log_opc_snapshot_recovery(
                logger,
                before_failures,
                phase="sampling_after",
            )
        for action_log in iter_action_logs(result):
            logger.log(
                action_log["message"],
                detail={"node_uuid": node.uuid, "action_log": action_log.get("detail")},
            )
        for wait_log in iter_opc_wait_logs(default_plc, device, snapshot_client):
            logger.log(
                wait_log["message"],
                level=wait_log.get("level", "info"),
                detail=wait_log.get("detail"),
            )
        if isinstance(result, dict) and result.get("display_message"):
            logger.log(str(result["display_message"]))
        failure = find_action_failure(result)
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
        if failure is not None:
            raise ActionReturnedFailure(
                device_id=device_name,
                action_name=method_name,
                failure=failure,
            )

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
    parser.add_argument(
        "--ignore-opcua-token-time-drift",
        action="store_true",
        help="忽略 OPC UA 旧 token 的时间漂移过期校验，仅用于现场调试",
    )
    parser.add_argument(
        "--clear-pc-to-plc-before-run",
        action="store_true",
        help="执行 workflow 前将机器人 PC->PLC 写入变量统一清零",
    )
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
    if args.ignore_opcua_token_time_drift:
        ignore_opcua_token_time_drift()
    devices = create_local_devices(
        graph_file=args.graph,
        opcua_url=args.url,
        csv_path=args.csv,
        use_subscription=False if args.no_subscription else None,
        runtime_config=runtime_config,
    )
    log_handle = args.log_file.open("w", encoding="utf-8") if args.log_file else None
    logger = WorkflowLogger(file=log_handle)
    try:
        if args.clear_pc_to_plc_before_run:
            clear_pc_to_plc_variables(devices, runtime_config, logger=logger)
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
