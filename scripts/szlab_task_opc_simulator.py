"""由 schema v2 配置驱动的通用 OPC 任务模拟器。"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import logging
import math
import os
import re
import signal
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - 生产目标为 macOS/Linux
    fcntl = None  # type: ignore[assignment]


DEFAULT_URL = "opc.tcp://127.0.0.1:4840"
# 远端 OPC 建连常超过 2s；io_timeout 仍用于单次读写，Client 会话超时单独抬高下限
MIN_OPC_SESSION_TIMEOUT = 15.0
DEFAULT_PROFILE_PATH = (
    Path(__file__).with_name("config") / "szlab_task_opc_simulator.json"
)
MIN_POLL_INTERVAL = 0.05
MAX_POLL_INTERVAL = 60.0
MIN_IO_TIMEOUT = 0.1
MAX_IO_TIMEOUT = 60.0
MAX_IDENTIFIER_LENGTH = 256
MAX_VARIABLE_NAME_LENGTH = 512
_EXPECTED_REVISION = re.compile(r"^[0-9a-f]{64}$")
MIN_GLOBAL_TIMEOUT = 0.1
_MISSING = object()
_DATA_TYPES = {"bool": bool, "int": int, "float": float, "string": str}
_VARIABLE_SOURCES = {
    "action_node",
    "action_sensor",
    "task_input",
    "task_output",
    "manual",
}
_ROOT_FIELDS = {"schema_version", "status", "name", "opc", "variables", "nodes"}
_VARIABLE_FIELDS = {"name", "direction", "data_type", "initial_value", "source"}
_NODE_FIELDS = {
    "workflow_node_id",
    "task_template_ids",
    "device_id",
    "method",
    "params",
    "channel",
    "trigger",
    "on_trigger",
    "on_complete",
    "reset_when",
    "after_reset",
}


class GlobalTimeoutError(TimeoutError):
    """模拟器全局截止时间到达。"""


@dataclass(frozen=True)
class VariableDefinition:
    name: str
    direction: str
    data_type: str
    source: str
    initial_value: Any = _MISSING

    @property
    def has_initial_value(self) -> bool:
        return self.initial_value is not _MISSING


@dataclass(frozen=True)
class Condition:
    variable: str
    operator: str
    value: Any
    edge: str


@dataclass(frozen=True)
class ConditionGroup:
    all: tuple[Condition, ...]


@dataclass(frozen=True)
class WriteOperation:
    name: str
    value: Any
    reason: str = ""

    @property
    def variable(self) -> str:
        return self.name


@dataclass(frozen=True)
class Phase:
    writes: tuple[WriteOperation, ...]
    delay: float = 0.0


@dataclass(frozen=True)
class NodeDefinition:
    workflow_node_id: str
    task_template_ids: tuple[str, ...]
    device_id: str
    method: str
    params: dict[str, Any]
    channel: str
    trigger: ConditionGroup
    on_trigger: Phase
    on_complete: Phase
    reset_when: ConditionGroup | None
    after_reset: Phase | None


@dataclass(frozen=True)
class SimulatorProfile:
    schema_version: int
    status: str
    name: str
    url: str
    poll_interval: float
    io_timeout: float
    variables: tuple[VariableDefinition, ...]
    nodes: tuple[NodeDefinition, ...]
    validation_errors: tuple[str, ...] = ()

    @property
    def variable_map(self) -> dict[str, VariableDefinition]:
        return {item.name: item for item in self.variables}

    @property
    def snapshot_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for node in self.nodes:
            groups = [node.trigger, node.reset_when]
            for group in groups:
                if group is None:
                    continue
                for condition in group.all:
                    if condition.variable not in names:
                        names.append(condition.variable)
        return tuple(names)

    @property
    def initial_writes(self) -> dict[str, Any]:
        return {
            item.name: item.initial_value
            for item in self.variables
            if item.direction == "plc_to_pc" and item.has_initial_value
        }


def _path_error(errors: list[str], path: str) -> None:
    if path not in errors:
        errors.append(path)


def _forbid_extra(
    value: dict[str, Any],
    allowed: set[str],
    path: str,
    errors: list[str],
) -> None:
    for key in value:
        if key not in allowed:
            _path_error(errors, f"{path}.{key}" if path else key)


def _is_typed(value: Any, data_type: Any) -> bool:
    if not isinstance(data_type, str):
        return False
    if data_type == "float":
        return type(value) in (int, float) and math.isfinite(float(value))
    expected = _DATA_TYPES.get(data_type)
    return expected is not None and type(value) is expected


def _is_valid_opc_tcp_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return (
            parsed.scheme.lower() == "opc.tcp"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _parse_group(
    raw: Any,
    path: str,
    variables: dict[str, VariableDefinition],
    errors: list[str],
) -> ConditionGroup:
    if not isinstance(raw, dict):
        _path_error(errors, path)
        return ConditionGroup(())
    _forbid_extra(raw, {"all"}, path, errors)
    items = raw.get("all")
    if not isinstance(items, list) or not items:
        _path_error(errors, f"{path}.all")
        return ConditionGroup(())
    parsed: list[Condition] = []
    for index, item in enumerate(items):
        item_path = f"{path}.all[{index}]"
        if not isinstance(item, dict):
            _path_error(errors, item_path)
            continue
        _forbid_extra(
            item,
            {"variable", "operator", "value", "edge"},
            item_path,
            errors,
        )
        name = item.get("variable")
        operator = item.get("operator")
        edge = item.get("edge")
        if not isinstance(name, str) or name not in variables:
            _path_error(errors, f"{item_path}.variable")
        if operator != "eq":
            _path_error(errors, f"{item_path}.operator")
        if edge not in ("rising", "falling", "level"):
            _path_error(errors, f"{item_path}.edge")
        if "value" not in item:
            _path_error(errors, f"{item_path}.value")
            value = None
        else:
            value = item["value"]
            definition = variables.get(name)
            if definition is not None and not _is_typed(value, definition.data_type):
                _path_error(errors, f"{item_path}.value")
        parsed.append(
            Condition(
                name if isinstance(name, str) else "",
                operator if isinstance(operator, str) else "",
                value,
                edge if isinstance(edge, str) else "",
            )
        )
    return ConditionGroup(tuple(parsed))


def _parse_phase(
    raw: Any,
    path: str,
    variables: dict[str, VariableDefinition],
    errors: list[str],
    *,
    delayed: bool,
) -> Phase:
    if not isinstance(raw, dict):
        _path_error(errors, path)
        return Phase(())
    _forbid_extra(
        raw,
        {"writes", "delay"} if delayed else {"writes"},
        path,
        errors,
    )
    if delayed and "delay" not in raw:
        _path_error(errors, f"{path}.delay")
    delay = raw.get("delay", 0)
    if delayed and (
        type(delay) not in (int, float)
        or not math.isfinite(float(delay))
        or delay < 0
    ):
        _path_error(errors, f"{path}.delay")
        delay = 0
    normalized_delay = (
        float(delay)
        if type(delay) in (int, float) and math.isfinite(float(delay))
        else 0.0
    )
    writes = raw.get("writes")
    if not isinstance(writes, list):
        _path_error(errors, f"{path}.writes")
        writes = []
    parsed: list[WriteOperation] = []
    for index, item in enumerate(writes):
        item_path = f"{path}.writes[{index}]"
        if not isinstance(item, dict):
            _path_error(errors, item_path)
            continue
        _forbid_extra(item, {"variable", "value"}, item_path, errors)
        name = item.get("variable")
        definition = variables.get(name) if isinstance(name, str) else None
        if definition is None or definition.direction != "plc_to_pc":
            _path_error(errors, f"{item_path}.variable")
        value = item.get("value", _MISSING)
        if value is _MISSING or (
            definition is not None
            and not _is_typed(value, definition.data_type)
        ):
            _path_error(errors, f"{item_path}.value")
        parsed.append(
            WriteOperation(
                name if isinstance(name, str) else "",
                None if value is _MISSING else value,
                path,
            )
        )
    return Phase(tuple(parsed), normalized_delay)


def _parse_profile(payload: Any) -> SimulatorProfile:
    errors: list[str] = []
    root = payload if isinstance(payload, dict) else {}
    if not isinstance(payload, dict):
        _path_error(errors, "$")
    else:
        _forbid_extra(root, _ROOT_FIELDS, "", errors)
    schema_version = root.get("schema_version")
    if type(schema_version) is not int or schema_version != 2:
        _path_error(errors, "schema_version")
    status = root.get("status")
    if status not in ("draft", "runnable"):
        _path_error(errors, "status")
    name = root.get("name")
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name.strip()) > MAX_IDENTIFIER_LENGTH
    ):
        _path_error(errors, "name")
        name = ""
    opc = root.get("opc")
    if not isinstance(opc, dict):
        _path_error(errors, "opc")
        url = ""
        poll_interval = 0.2
        io_timeout = 2.0
    else:
        _forbid_extra(
            opc,
            {"url", "poll_interval", "io_timeout"},
            "opc",
            errors,
        )
        url = opc.get("url")
        if not _is_valid_opc_tcp_url(url):
            _path_error(errors, "opc.url")
            url = ""
        poll_interval = opc.get("poll_interval")
        if (
            type(poll_interval) not in (int, float)
            or not math.isfinite(float(poll_interval))
            or not MIN_POLL_INTERVAL <= float(poll_interval) <= MAX_POLL_INTERVAL
        ):
            _path_error(errors, "opc.poll_interval")
            poll_interval = 0.2
        io_timeout = opc.get("io_timeout")
        if (
            type(io_timeout) not in (int, float)
            or not math.isfinite(float(io_timeout))
            or not MIN_IO_TIMEOUT <= float(io_timeout) <= MAX_IO_TIMEOUT
        ):
            _path_error(errors, "opc.io_timeout")
            io_timeout = 2.0

    raw_variables = root.get("variables")
    if not isinstance(raw_variables, list):
        _path_error(errors, "variables")
        raw_variables = []
    elif not raw_variables:
        _path_error(errors, "variables")
    variables: list[VariableDefinition] = []
    names: set[str] = set()
    for index, item in enumerate(raw_variables):
        path = f"variables[{index}]"
        raw = item if isinstance(item, dict) else {}
        if not isinstance(item, dict):
            _path_error(errors, path)
        else:
            _forbid_extra(raw, _VARIABLE_FIELDS, path, errors)
        variable_name = raw.get("name")
        if (
            not isinstance(variable_name, str)
            or not variable_name.strip()
            or len(variable_name.strip()) > MAX_VARIABLE_NAME_LENGTH
            or variable_name in names
        ):
            _path_error(errors, f"{path}.name")
            variable_name = variable_name if isinstance(variable_name, str) else ""
        else:
            names.add(variable_name)
        direction = raw.get("direction")
        if direction not in ("pc_to_plc", "plc_to_pc"):
            _path_error(errors, f"{path}.direction")
        data_type = raw.get("data_type")
        if not isinstance(data_type, str) or data_type not in _DATA_TYPES:
            _path_error(errors, f"{path}.data_type")
        source = raw.get("source")
        if not isinstance(source, str) or source not in _VARIABLE_SOURCES:
            _path_error(errors, f"{path}.source")
            source = ""
        initial = raw.get("initial_value", _MISSING)
        if initial is not _MISSING:
            if direction != "plc_to_pc" or not _is_typed(initial, data_type):
                _path_error(errors, f"{path}.initial_value")
        variables.append(
            VariableDefinition(
                variable_name,
                direction if isinstance(direction, str) else "",
                data_type if isinstance(data_type, str) else "",
                source,
                initial,
            )
        )
    variable_map = {item.name: item for item in variables if item.name}

    raw_nodes = root.get("nodes")
    if not isinstance(raw_nodes, list):
        _path_error(errors, "nodes")
        raw_nodes = []
    elif not raw_nodes:
        _path_error(errors, "nodes")
    nodes: list[NodeDefinition] = []
    workflow_node_ids: set[str] = set()
    for index, item in enumerate(raw_nodes):
        path = f"nodes[{index}]"
        raw = item if isinstance(item, dict) else {}
        if not isinstance(item, dict):
            _path_error(errors, path)
        else:
            _forbid_extra(raw, _NODE_FIELDS, path, errors)

        def text(field_name: str, *, required: bool = True) -> str:
            value = raw.get(field_name)
            if (
                not isinstance(value, str)
                or (required and not value.strip())
                or (
                    isinstance(value, str)
                    and len(value.strip()) > MAX_IDENTIFIER_LENGTH
                )
            ):
                _path_error(errors, f"{path}.{field_name}")
                return ""
            return value

        workflow_node_id = text("workflow_node_id")
        if workflow_node_id and workflow_node_id in workflow_node_ids:
            _path_error(errors, f"{path}.workflow_node_id")
        elif workflow_node_id:
            workflow_node_ids.add(workflow_node_id)
        template_ids = raw.get("task_template_ids")
        if not isinstance(template_ids, list) or not template_ids:
            _path_error(errors, f"{path}.task_template_ids")
            template_ids = []
        else:
            for template_index, template_id in enumerate(template_ids):
                if (
                    not isinstance(template_id, str)
                    or not template_id.strip()
                    or len(template_id.strip()) > MAX_IDENTIFIER_LENGTH
                ):
                    _path_error(
                        errors,
                        f"{path}.task_template_ids[{template_index}]",
                    )
        params = raw.get("params")
        if not isinstance(params, dict):
            _path_error(errors, f"{path}.params")
            params = {}
        trigger = _parse_group(raw.get("trigger"), f"{path}.trigger", variable_map, errors)
        on_trigger = _parse_phase(
            raw.get("on_trigger"),
            f"{path}.on_trigger",
            variable_map,
            errors,
            delayed=False,
        )
        on_complete = _parse_phase(
            raw.get("on_complete"),
            f"{path}.on_complete",
            variable_map,
            errors,
            delayed=True,
        )
        reset_when = (
            None
            if raw.get("reset_when") is None
            else _parse_group(
                raw.get("reset_when"),
                f"{path}.reset_when",
                variable_map,
                errors,
            )
        )
        after_reset = (
            None
            if raw.get("after_reset") is None
            else _parse_phase(
                raw.get("after_reset"),
                f"{path}.after_reset",
                variable_map,
                errors,
                delayed=True,
            )
        )
        if after_reset is not None and reset_when is None:
            _path_error(errors, f"{path}.after_reset")
        nodes.append(
            NodeDefinition(
                workflow_node_id,
                tuple(template_ids),
                text("device_id"),
                text("method"),
                dict(params),
                text("channel"),
                trigger,
                on_trigger,
                on_complete,
                reset_when,
                after_reset,
            )
        )
    normalized_status = status if status in ("draft", "runnable") else "draft"
    profile = SimulatorProfile(
        2 if schema_version == 2 else 0,
        normalized_status,
        name,
        url,
        float(poll_interval),
        float(io_timeout),
        tuple(variables),
        tuple(nodes),
        tuple(errors),
    )
    if status != "draft" and errors:
        raise ValueError("配置校验失败：" + ", ".join(errors))
    return profile


def _parse_expected_revision(raw: str) -> str:
    if not _EXPECTED_REVISION.fullmatch(raw):
        raise argparse.ArgumentTypeError(
            "--expected-revision 必须是 64 位小写 SHA-256"
        )
    return raw


def load_simulator_profile(
    path: str | Path,
    *,
    expected_revision: str | None = None,
) -> SimulatorProfile:
    """读取 schema v2；draft 返回路径错误，runnable 对任何错误拒绝加载。"""
    profile_path = Path(path)
    try:
        content = profile_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"无法读取模拟器配置 {profile_path}: {exc}") from exc
    if expected_revision is not None:
        try:
            _parse_expected_revision(expected_revision)
        except argparse.ArgumentTypeError as exc:
            raise ValueError(str(exc)) from exc
        if hashlib.sha256(content).hexdigest() != expected_revision:
            raise ValueError("配置 revision 不匹配")
    try:
        payload = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取模拟器配置 {profile_path}: {exc}") from exc
    return _parse_profile(payload)


@dataclass(order=True)
class _Scheduled:
    due_at: float
    sequence: int
    node_index: int = field(compare=False)
    phase: Phase = field(compare=False)
    kind: str = field(compare=False)


class TaskOpcStateMachine:
    """仅依赖声明式条件、阶段和 channel 的通用状态机。"""

    def __init__(self, *, profile: SimulatorProfile | None = None) -> None:
        self.profile = profile or load_simulator_profile(DEFAULT_PROFILE_PATH)
        if self.profile.status != "runnable":
            raise ValueError("draft 配置不可运行")
        self._previous: dict[str, Any] | None = None
        self._scheduled: list[_Scheduled] = []
        self._sequence = 0
        self._node_states = ["idle"] * len(self.profile.nodes)
        self._busy_channels: set[str] = set()
        self._trigger_latched = [False] * len(self.profile.nodes)
        self._trigger_suppressed = [False] * len(self.profile.nodes)
        self._edge_active = [
            [False] * len(node.trigger.all) for node in self.profile.nodes
        ]
        self._level_group_previous = [False] * len(self.profile.nodes)

    def initial_writes(self) -> list[WriteOperation]:
        return [
            WriteOperation(item.name, item.initial_value, "启动初态")
            for item in self.profile.variables
            if item.direction == "plc_to_pc" and item.has_initial_value
        ]

    def prime(self, snapshot: dict[str, Any]) -> None:
        self._previous = dict(snapshot)
        for index, node in enumerate(self.profile.nodes):
            level_conditions = [
                condition
                for condition in node.trigger.all
                if condition.edge == "level"
            ]
            if len(level_conditions) == len(node.trigger.all):
                self._level_group_previous[index] = all(
                    self._same(snapshot.get(condition.variable), condition.value)
                    for condition in level_conditions
                )

    @staticmethod
    def _same(left: Any, right: Any) -> bool:
        return type(left) is type(right) and left == right

    def _matches(self, group: ConditionGroup, snapshot: dict[str, Any]) -> bool:
        assert self._previous is not None
        for condition in group.all:
            previous_match = self._same(
                self._previous.get(condition.variable), condition.value
            )
            current_match = self._same(snapshot.get(condition.variable), condition.value)
            if condition.edge == "level":
                matched = current_match
            elif condition.edge == "rising":
                matched = current_match and not previous_match
            else:
                matched = previous_match and not current_match
            if not matched:
                return False
        return bool(group.all)

    def _schedule(self, now: float, node_index: int, phase: Phase, kind: str) -> None:
        self._scheduled.append(
            _Scheduled(now + phase.delay, self._sequence, node_index, phase, kind)
        )
        self._sequence += 1

    @staticmethod
    def _edge_signature(condition: Condition) -> tuple[str, str, Any, str]:
        return (
            condition.variable,
            condition.operator,
            condition.value,
            condition.edge,
        )

    def _suppress_shared_edge_generation(self, source_index: int) -> None:
        source = self.profile.nodes[source_index]
        signatures = {
            self._edge_signature(condition)
            for condition in source.trigger.all
            if condition.edge != "level"
        }
        if not signatures:
            return
        for index, node in enumerate(self.profile.nodes):
            if node.channel != source.channel:
                continue
            shared = any(
                condition.edge != "level"
                and self._edge_signature(condition) in signatures
                for condition in node.trigger.all
            )
            if shared:
                self._trigger_suppressed[index] = True
                self._edge_active[index] = [False] * len(node.trigger.all)

    def _update_trigger_latches(self, snapshot: dict[str, Any]) -> None:
        assert self._previous is not None
        for index, node in enumerate(self.profile.nodes):
            edge_indexes = [
                condition_index
                for condition_index, condition in enumerate(node.trigger.all)
                if condition.edge != "level"
            ]
            if not edge_indexes:
                matches = all(
                    self._same(snapshot.get(condition.variable), condition.value)
                    for condition in node.trigger.all
                )
                if matches and not self._level_group_previous[index]:
                    self._trigger_latched[index] = True
                self._level_group_previous[index] = matches
                continue

            for condition_index in edge_indexes:
                condition = node.trigger.all[condition_index]
                previous_match = self._same(
                    self._previous.get(condition.variable),
                    condition.value,
                )
                current_match = self._same(
                    snapshot.get(condition.variable),
                    condition.value,
                )
                if condition.edge == "rising":
                    if current_match and not previous_match:
                        self._edge_active[index][condition_index] = True
                    elif not current_match:
                        self._edge_active[index][condition_index] = False
                else:
                    if not current_match and previous_match:
                        self._edge_active[index][condition_index] = True
                    elif current_match:
                        self._edge_active[index][condition_index] = False

            if self._trigger_suppressed[index]:
                if not any(
                    self._edge_active[index][condition_index]
                    for condition_index in edge_indexes
                ):
                    self._trigger_suppressed[index] = False
                else:
                    continue
            if self._trigger_latched[index]:
                continue
            edges_ready = all(
                self._edge_active[index][condition_index]
                for condition_index in edge_indexes
            )
            levels_ready = all(
                self._same(snapshot.get(condition.variable), condition.value)
                for condition in node.trigger.all
                if condition.edge == "level"
            )
            if edges_ready and levels_ready:
                self._trigger_latched[index] = True
                self._suppress_shared_edge_generation(index)

    def _start_latched_nodes(self, now: float) -> list[WriteOperation]:
        result: list[WriteOperation] = []
        for index, node in enumerate(self.profile.nodes):
            if (
                self._trigger_latched[index]
                and self._node_states[index] == "idle"
                and node.channel not in self._busy_channels
            ):
                self._trigger_latched[index] = False
                self._node_states[index] = "running"
                self._busy_channels.add(node.channel)
                result.extend(node.on_trigger.writes)
                self._schedule(now, index, node.on_complete, "complete")
        return result

    def _drain(self, now: float) -> list[WriteOperation]:
        due = sorted(item for item in self._scheduled if item.due_at <= now)
        due_ids = {id(item) for item in due}
        self._scheduled = [item for item in self._scheduled if id(item) not in due_ids]
        result: list[WriteOperation] = []
        for event in due:
            result.extend(event.phase.writes)
            if event.kind == "complete":
                node = self.profile.nodes[event.node_index]
                if node.reset_when is None:
                    self._node_states[event.node_index] = "idle"
                    self._busy_channels.discard(node.channel)
                else:
                    self._node_states[event.node_index] = "awaiting_reset"
                    # on_complete 结束后即可释放 channel；after_reset 只延迟写回变量，不占用通道
                    self._busy_channels.discard(node.channel)
            elif event.kind == "after_reset":
                node = self.profile.nodes[event.node_index]
                self._node_states[event.node_index] = "idle"
                self._busy_channels.discard(node.channel)
        return result

    def tick(self, snapshot: dict[str, Any], now: float) -> list[WriteOperation]:
        if self._previous is None:
            self.prime(snapshot)
            return []
        self._update_trigger_latches(snapshot)
        result = self._drain(now)
        for index, node in enumerate(self.profile.nodes):
            if (
                self._node_states[index] == "awaiting_reset"
                and node.reset_when is not None
                and self._matches(node.reset_when, snapshot)
            ):
                if node.after_reset is None:
                    self._node_states[index] = "idle"
                    self._busy_channels.discard(node.channel)
                else:
                    self._node_states[index] = "after_reset"
                    self._schedule(now, index, node.after_reset, "after_reset")
        result.extend(self._drain(now))
        result.extend(self._start_latched_nodes(now))
        self._previous = dict(snapshot)
        result.extend(self._drain(now))
        return result


class SimulatorStateMachine(TaskOpcStateMachine):
    def step(self, snapshot: dict[str, Any], *, now: float) -> list[WriteOperation]:
        return self.tick(snapshot, now)


class OpcAdapter(Protocol):
    def read(self, name: str) -> Any: ...
    def read_many(self, names: Sequence[str]) -> dict[str, Any]: ...
    def write(self, name: str, value: Any) -> None: ...
    def close(self) -> None: ...


class SZLabOpcAdapter:
    def __init__(self, device: Any) -> None:
        self._device = device

    def _reconnect(self) -> None:
        disconnect = getattr(self._device, "disconnect", None)
        connect = getattr(self._device, "_connect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass
        if callable(connect):
            connect()

    def read(self, name: str) -> Any:
        last_exc: BaseException | None = None
        for attempt in range(2):
            try:
                return self._device.read_variable(name, use_cache=False)
            except RuntimeError as exc:
                last_exc = exc
                if attempt == 0:
                    self._reconnect()
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"读取 OPC 变量失败: {name}")

    def read_many(self, names: Sequence[str]) -> dict[str, Any]:
        return {name: self.read(name) for name in names}

    def read_snapshot(
        self,
        names: Sequence[str],
        *,
        stop_requested: Callable[[], bool],
        deadline_exceeded: Callable[[], bool],
    ) -> dict[str, Any] | None:
        values: dict[str, Any] = {}
        for name in names:
            if deadline_exceeded():
                raise GlobalTimeoutError("读取 OPC 快照前已达到全局截止时间")
            if stop_requested():
                return None
            values[name] = self.read(name)
        return values

    def write(self, name: str, value: Any) -> None:
        self._device.write_variable(name, value)

    def close(self) -> None:
        self._device.disconnect()


@dataclass(frozen=True)
class RestoreResult:
    success: bool
    restored: list[str]
    skipped: list[str]
    errors: list[str]


class TrackedWriter:
    """只恢复仍保持本实例最后写值的变量，所有权比较严格区分类型。"""

    def __init__(
        self,
        adapter: OpcAdapter,
        *,
        dry_run: bool = False,
        logger: logging.Logger | None = None,
        stop_requested: Callable[[], bool] = lambda: False,
        deadline_exceeded: Callable[[], bool] = lambda: False,
        profile: SimulatorProfile | None = None,
    ) -> None:
        self._adapter = adapter
        self._dry_run = dry_run
        self._logger = logger or logging.getLogger(__name__)
        self._stop_requested = stop_requested
        self._deadline_exceeded = deadline_exceeded
        self._originals: dict[str, Any] = {}
        self._last_written: dict[str, Any] = {}
        self._written_order: list[str] = []

    @property
    def original_values(self) -> dict[str, Any]:
        return dict(self._originals)

    @property
    def written_variables(self) -> tuple[str, ...]:
        return tuple(self._written_order)

    def _guard_io(self, name: str) -> None:
        if self._stop_requested():
            raise InterruptedError(f"写入 {name} 前收到停止请求")
        if self._deadline_exceeded():
            raise GlobalTimeoutError(f"写入 {name} 前已达到全局截止时间")

    def write(self, name: str | WriteOperation, value: Any = None, reason: str = "") -> None:
        if isinstance(name, WriteOperation):
            name, value, reason = name.name, name.value, name.reason
        self._guard_io(name)
        if name not in self._originals:
            self._originals[name] = self._adapter.read(name)
        self._guard_io(name)
        if self._dry_run:
            self._logger.info("DRY-RUN 跳过写入 %s=%r，原因=%s", name, value, reason)
            return
        self._adapter.write(name, value)
        self._last_written[name] = value
        if name not in self._written_order:
            self._written_order.append(name)

    def restore(self) -> RestoreResult:
        restored: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        if self._dry_run:
            return RestoreResult(True, restored, skipped, errors)
        for name in reversed(self._written_order):
            try:
                current = self._adapter.read(name)
                expected = self._last_written[name]
                if type(current) is not type(expected) or current != expected:
                    message = (
                        f"跳过恢复 {name}：所有权检查失败，远端当前值 {current!r}"
                        f"（最后写值 {expected!r}）"
                    )
                    self._logger.warning(message)
                    skipped.append(name)
                    continue
                self._adapter.write(name, self._originals[name])
                restored.append(name)
            except Exception as exc:
                message = f"恢复 {name} 失败：{exc}"
                self._logger.error(message)
                errors.append(message)
        return RestoreResult(not errors, restored, skipped, errors)


@dataclass(frozen=True)
class SimulatorConfig:
    url: str | None = None
    dry_run: bool = False
    poll_interval: float | None = None
    timeout: float | None = None
    io_timeout: float | None = None
    allow_unsafe_url: bool = False
    log_level: str = "INFO"
    profile: SimulatorProfile = field(
        default_factory=lambda: load_simulator_profile(DEFAULT_PROFILE_PATH)
    )

    def __post_init__(self) -> None:
        if self.url is None:
            object.__setattr__(self, "url", self.profile.url)
        if self.poll_interval is None:
            object.__setattr__(
                self,
                "poll_interval",
                self.profile.poll_interval,
            )
        if self.io_timeout is None:
            object.__setattr__(self, "io_timeout", self.profile.io_timeout)
        if self.timeout == 0:
            object.__setattr__(self, "timeout", None)
        for name, value, minimum, maximum in (
            (
                "poll_interval",
                self.poll_interval,
                MIN_POLL_INTERVAL,
                MAX_POLL_INTERVAL,
            ),
            ("io_timeout", self.io_timeout, MIN_IO_TIMEOUT, MAX_IO_TIMEOUT),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or value < minimum
                or (maximum is not None and value > maximum)
            ):
                raise ValueError(f"{name} 超出允许范围")
        if self.timeout is not None and (
            type(self.timeout) not in (int, float)
            or not math.isfinite(float(self.timeout))
            or self.timeout < MIN_GLOBAL_TIMEOUT
        ):
            raise ValueError("timeout 超出允许范围")


def validate_url(
    url: str,
    *,
    allow_unsafe: bool | None = None,
    allow_unsafe_url: bool | None = None,
) -> str:
    if url != DEFAULT_URL and not (bool(allow_unsafe) or bool(allow_unsafe_url)):
        raise ValueError("拒绝连接非默认 OPC URL；请显式传入 --allow-unsafe-url")
    return url


def create_opc_adapter(config: SimulatorConfig) -> OpcAdapter:
    from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

    repo_root = Path(__file__).resolve().parents[1]
    csv_path = (
        repo_root
        / "unilabos"
        / "devices"
        / "workstation"
        / "szlab_poly_studio"
        / "szlab_plc_0721.csv"
    )
    return SZLabOpcAdapter(
        SZLabPolyPLCDevice(
            url=str(config.url),
            csv_path=str(csv_path),
            auto_connect=True,
            opcua_timeout=max(float(config.io_timeout), MIN_OPC_SESSION_TIMEOUT),
        )
    )


def _normalize_opc_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            path,
            parsed.query,
            parsed.fragment,
        )
    )


class EndpointInstanceLock:
    """同机 OPC endpoint 单实例锁；不提供跨主机互斥。"""

    def __init__(self, url: str, *, lock_dir: str | Path | None = None) -> None:
        self.normalized_url = _normalize_opc_url(url)
        digest = hashlib.sha256(self.normalized_url.encode("utf-8")).hexdigest()[:24]
        directory = (
            Path(lock_dir)
            if lock_dir is not None
            else self._default_lock_directory()
        )
        self._ensure_private_directory(directory)
        self.path = directory / f"endpoint-{digest}.lock"
        self._file: Any = None

    @staticmethod
    def _default_lock_directory() -> Path:
        if sys.platform == "darwin":
            cache_root = Path.home() / "Library" / "Caches"
        else:
            cache_root = Path(
                os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
            )
        preferred = cache_root / "unilab" / "opc-simulator-locks"
        try:
            EndpointInstanceLock._ensure_private_directory(preferred)
            return preferred
        except OSError:
            fallback = (
                Path(tempfile.gettempdir())
                / f"unilab-{os.getuid()}"
                / "opc-simulator-locks"
            )
            EndpointInstanceLock._ensure_private_directory(fallback)
            return fallback

    @staticmethod
    def _ensure_private_directory(directory: Path) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"锁目录不是安全的普通目录：{directory}")
        if info.st_uid != os.getuid():
            raise RuntimeError(f"锁目录不属于当前用户：{directory}")
        os.chmod(directory, 0o700)
        if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
            raise RuntimeError(f"锁目录权限必须为 0700：{directory}")

    @staticmethod
    def _validate_lock_file(fd: int, path: Path) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"锁文件不是 regular file：{path}")
        if info.st_uid != os.getuid():
            raise RuntimeError(f"锁文件不属于当前用户：{path}")
        if stat.S_IMODE(info.st_mode) & ~0o600:
            raise RuntimeError(f"锁文件权限不得宽于 0600：{path}")

    def acquire(self) -> None:
        if self._file is not None:
            return
        if fcntl is None:
            raise RuntimeError("endpoint 单实例锁仅支持 macOS/Linux")
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise RuntimeError(f"拒绝使用符号链接锁文件：{self.path}")
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        locked = False
        try:
            fd = os.open(self.path, flags, 0o600)
            self._validate_lock_file(fd, self.path)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            self._file = os.fdopen(fd, "r+", encoding="utf-8", closefd=True)
            fd = None
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeError(
                    f"同机已有模拟器占用 OPC endpoint：{self.normalized_url}"
                ) from exc
            if exc.errno == errno.ELOOP:
                raise RuntimeError(f"拒绝使用符号链接锁文件：{self.path}") from exc
            raise
        finally:
            if fd is not None:
                if locked:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(fd)

    def release(self) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def create_endpoint_lock(url: str) -> EndpointInstanceLock:
    return EndpointInstanceLock(url)


def run_simulator(
    config: SimulatorConfig,
    *,
    adapter_factory: Callable[[SimulatorConfig], OpcAdapter] = create_opc_adapter,
    lock_factory: Callable[[str], Any] = create_endpoint_lock,
    stop_requested: Callable[[], bool] = lambda: False,
    monotonic: Callable[[], float] = time.monotonic,
    interruptible_wait: Callable[[float], bool] | None = None,
    logger: logging.Logger | None = None,
) -> int:
    validate_url(str(config.url), allow_unsafe_url=config.allow_unsafe_url)
    if config.profile.status != "runnable" or config.profile.validation_errors:
        raise ValueError("draft 或校验未通过的配置不可运行")
    machine = TaskOpcStateMachine(profile=config.profile)
    if stop_requested():
        return 0
    log = logger or logging.getLogger(__name__)
    wait = interruptible_wait or threading.Event().wait
    adapter: OpcAdapter | None = None
    writer: TrackedWriter | None = None
    endpoint_lock: Any = None
    lock_acquired = False
    exit_code = 0
    log.info("OPC 模拟器连接地址: %s", config.url)
    deadline = (
        None
        if config.timeout is None
        else monotonic() + config.timeout
    )

    def expired() -> bool:
        return deadline is not None and monotonic() >= deadline

    try:
        endpoint_lock = lock_factory(str(config.url))
        endpoint_lock.acquire()
        lock_acquired = True
        adapter = adapter_factory(config)
        writer = TrackedWriter(
            adapter,
            dry_run=config.dry_run,
            logger=log,
            stop_requested=stop_requested,
            deadline_exceeded=expired,
            profile=config.profile,
        )
        snapshot: dict[str, Any] = {}
        stopped = False
        for name in config.profile.snapshot_names:
            if stop_requested():
                stopped = True
                break
            if expired():
                raise GlobalTimeoutError(f"模拟器运行超时：{config.timeout}s")
            snapshot[name] = adapter.read(name)
        if not stopped:
            machine.prime(snapshot)
            for operation in machine.initial_writes():
                writer.write(operation)
        while not stopped and not stop_requested():
            if expired():
                raise GlobalTimeoutError(f"模拟器运行超时：{config.timeout}s")
            snapshot = {}
            for name in config.profile.snapshot_names:
                if stop_requested():
                    break
                if expired():
                    raise GlobalTimeoutError(f"模拟器运行超时：{config.timeout}s")
                snapshot[name] = adapter.read(name)
            if stop_requested():
                break
            for operation in machine.tick(snapshot, monotonic()):
                writer.write(operation)
            wait_seconds = float(config.poll_interval)
            if deadline is not None:
                wait_seconds = min(
                    wait_seconds,
                    max(0, deadline - monotonic()),
                )
            if wait(wait_seconds):
                break
    except InterruptedError as exc:
        log.info("收到停止请求，安全退出：%s", exc)
    except GlobalTimeoutError as exc:
        if config.dry_run:
            log.info("dry-run 达到全局超时：%s", exc)
        else:
            log.error("%s", exc)
            exit_code = 1
    finally:
        if writer is not None and not writer.restore().success:
            exit_code = 1
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                log.error("关闭 OPC 连接失败：%s", exc)
                exit_code = 1
        if lock_acquired and endpoint_lock is not None:
            endpoint_lock.release()
    return exit_code


def _bounded_float(name: str, minimum: float, maximum: float | None = None):
    def parse(raw: str) -> float:
        value = float(raw)
        if (
            not math.isfinite(value)
            or value < minimum
            or (maximum is not None and value > maximum)
        ):
            raise argparse.ArgumentTypeError(f"{name} 超出允许范围")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_PROFILE_PATH))
    parser.add_argument(
        "--expected-revision",
        type=_parse_expected_revision,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--url", default=None)
    parser.add_argument(
        "--timeout",
        type=_bounded_float("--timeout", 0),
        default=None,
        help="全局运行秒数；省略或 0 表示持续运行直到收到停止信号",
    )
    parser.add_argument(
        "--io-timeout",
        type=_bounded_float("--io-timeout", MIN_IO_TIMEOUT, MAX_IO_TIMEOUT),
        default=None,
    )
    parser.add_argument(
        "--poll-interval",
        type=_bounded_float(
            "--poll-interval",
            MIN_POLL_INTERVAL,
            MAX_POLL_INTERVAL,
        ),
        default=None,
    )
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-unsafe-url", action="store_true")
    return parser


def _install_stop_signals(stop_event: threading.Event, logger: logging.Logger) -> None:
    def request_stop(signum: int, _frame: Any) -> None:
        logger.warning("收到信号 %s，准备安全退出", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger(__name__)
    try:
        profile = load_simulator_profile(
            args.config,
            expected_revision=args.expected_revision,
        )
        config = SimulatorConfig(
            profile=profile,
            url=args.url,
            timeout=args.timeout,
            io_timeout=args.io_timeout,
            poll_interval=args.poll_interval,
            log_level=args.log_level,
            dry_run=args.dry_run,
            allow_unsafe_url=args.allow_unsafe_url,
        )
    except ValueError as exc:
        log.error("模拟器配置无效：%s", exc)
        return 2
    stop_event = threading.Event()
    _install_stop_signals(stop_event, log)
    try:
        return run_simulator(
            config,
            stop_requested=stop_event.is_set,
            interruptible_wait=stop_event.wait,
            logger=log,
        )
    except Exception as exc:
        if str(exc).strip():
            log.error("模拟器退出：%s", exc)
        else:
            log.error("模拟器退出：%r", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
