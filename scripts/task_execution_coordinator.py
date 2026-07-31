"""Task 工作区动作认领、后台执行与完成上报协调器。"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from scripts.run_workflow_local import (
    WorkflowNode,
    node_method,
    workflow_node_from_mapping,
)
from unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.sensors import (
    s04_allow_var,
    s04_material_sensor_var,
    s04_ready_var,
    s04_status_var,
)
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.sensors import (
    ADDITION_BEAKER_SENSOR,
    S06_ALLOW_PROCESS_VAR,
    S06_DONE_VAR,
    S06_READY_VAR,
)
from unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition.sensors import (
    NODE_ALLOW_PROCESS as S07_ALLOW_PROCESS_VAR,
    NODE_HOME as S07_HOME_VAR,
)
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.sensors import (
    S09_ALLOW_PROCESS_VAR,
    S09_HOME_SIGNALS,
    S09_PROCESS_DONE_VAR,
    S09_STATION_SENSORS,
    S09_TIP_BOX_SENSORS,
    s09_remaining_volume_var,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import (
    product_slot_sensor,
)


class TaskApiConflict(RuntimeError):
    """Task API 的正常乐观锁或资源等待冲突。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def deterministic_execution_id(instance_id: str, cursor: int, node_id: str) -> str:
    """根据服务端权威游标生成可跨进程重放的执行 ID。"""
    identity = f"{instance_id}\0{cursor}\0{node_id}".encode()
    return f"task-action-{hashlib.sha256(identity).hexdigest()[:32]}"


def workflow_nodes_from_payload(payload: dict[str, Any]) -> list[WorkflowNode]:
    """按 workflow JSON 契约解析节点，不创建临时文件。"""
    if not isinstance(payload, dict):
        raise ValueError("workflow 内容必须是对象")
    workflow = payload.get("data", payload)
    if not isinstance(workflow, dict):
        raise ValueError("workflow 内容无效")
    nodes = workflow.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("workflow nodes 必须是数组")
    return [workflow_node_from_mapping(item) for item in nodes]


def _continuation_device_owners(
    workspace: dict[str, Any],
    *,
    templates: dict[str, dict[str, Any]],
    nodes_by_id: dict[str, WorkflowNode],
) -> dict[str, str]:
    """让已开始的原子 Task 保持其下一动作设备，直到该 Task 完成。"""
    candidates: list[tuple[int, int, str, str, str]] = []
    for instance in workspace.get("task_instances", []):
        if not isinstance(instance, dict) or instance.get("status") != "running":
            continue
        state = instance.get("execution_state")
        if not isinstance(state, dict):
            continue
        template = templates.get(str(instance.get("template_id") or ""))
        node_ids = template.get("node_ids", []) if template else []
        cursor = int(state.get("cursor", 0))
        if cursor <= 0 or cursor >= len(node_ids):
            continue
        node = nodes_by_id.get(str(node_ids[cursor]))
        if node is None:
            continue
        started_at = instance.get("started_at")
        candidates.append(
            (
                int(started_at) if started_at is not None else 2**63 - 1,
                int(instance.get("order") or 0),
                str(instance.get("sample_id") or ""),
                str(instance.get("id") or ""),
                node.device_name,
            )
        )

    owners: dict[str, str] = {}
    for _, _, _, instance_id, device_name in sorted(candidates):
        owners.setdefault(device_name, instance_id)
    return owners


_S072_INBOUND_START_NODE_IDS = frozenset(
    {"w01_pick_beaker_s03"}
)
_S072_PLACE_NODE_IDS = frozenset(
    {"w01_place_beaker_s072"}
)
_S072_OCCUPIED_REQUIRED_NODE_IDS = frozenset(
    {
        "w01_dose_powder_s07",
        "w02_pick_beaker_s072",
    }
)
_S072_PICK_NODE_IDS = frozenset(
    {"w02_pick_beaker_s072"}
)
_S09_INBOUND_START_NODE_IDS = frozenset({"w03_pick_beaker_s06"})
_S09_PLACE_NODE_IDS = frozenset({"w03_place_beaker_s09"})
_S09_OCCUPIED_REQUIRED_NODE_IDS = frozenset(
    {"w03_add_liquid_s09", "w04_pick_beaker_s09"}
)
_S09_PICK_NODE_IDS = frozenset({"w04_pick_beaker_s09"})


@dataclass(frozen=True)
class _TemporaryStationState:
    """实体传感器接入前，由成功动作记录推导的临时有料状态。"""

    has_material: bool
    inbound_instance_id: str | None = None


def _temporary_station_state(
    workspace: dict[str, Any],
    *,
    inbound_start_node_ids: frozenset[str],
    place_node_ids: frozenset[str],
    pick_node_ids: frozenset[str],
) -> _TemporaryStationState:
    """用放/取成功记录恢复临时状态，避免进程重启后丢失。"""
    transitions: list[tuple[int, int, bool]] = []
    templates = {
        str(template.get("id")): template
        for template in workspace.get("templates", [])
        if isinstance(template, dict)
    }
    inbound_instance_id: str | None = None
    sequence = 0

    for instance in workspace.get("task_instances", []):
        if not isinstance(instance, dict):
            continue
        state = instance.get("execution_state")
        if not isinstance(state, dict):
            continue
        succeeded_node_ids: set[str] = set()
        for record in state.get("records", []):
            if not isinstance(record, dict) or record.get("status") != "succeeded":
                continue
            node_id = str(record.get("node_id") or "")
            succeeded_node_ids.add(node_id)
            if node_id in place_node_ids:
                transitions.append(
                    (int(record.get("finished_at") or 0), sequence, True)
                )
                sequence += 1
            elif node_id in pick_node_ids:
                transitions.append(
                    (int(record.get("finished_at") or 0), sequence, False)
                )
                sequence += 1

        template = templates.get(str(instance.get("template_id") or ""))
        node_ids = template.get("node_ids", []) if template else []
        has_inbound_start = any(
            str(node_id) in inbound_start_node_ids for node_id in node_ids
        )
        has_inbound_place = any(
            str(node_id) in place_node_ids for node_id in node_ids
        )
        inbound_started = any(
            node_id in succeeded_node_ids
            for node_id in inbound_start_node_ids
        )
        inbound_finished = any(
            node_id in succeeded_node_ids for node_id in place_node_ids
        )
        if (
            has_inbound_start
            and has_inbound_place
            and inbound_started
            and not inbound_finished
        ):
            inbound_instance_id = str(instance.get("id") or "") or None

    transitions.sort()
    has_material = transitions[-1][2] if transitions else False
    return _TemporaryStationState(
        has_material=has_material,
        inbound_instance_id=inbound_instance_id,
    )


def _temporary_station_trigger_satisfied(
    workspace: dict[str, Any],
    *,
    instance_id: str,
    node_id: str,
    inbound_start_node_ids: frozenset[str],
    place_node_ids: frozenset[str],
    occupied_required_node_ids: frozenset[str],
    pick_node_ids: frozenset[str],
) -> bool:
    state = _temporary_station_state(
        workspace,
        inbound_start_node_ids=inbound_start_node_ids,
        place_node_ids=place_node_ids,
        pick_node_ids=pick_node_ids,
    )
    if node_id in inbound_start_node_ids:
        return not state.has_material and state.inbound_instance_id is None
    if node_id in place_node_ids:
        return not state.has_material and state.inbound_instance_id in {
            None,
            instance_id,
        }
    if node_id in occupied_required_node_ids:
        return state.has_material
    return True


def _temporary_s072_state(workspace: dict[str, Any]) -> _TemporaryStationState:
    """恢复 S072 临时有料状态。"""
    return _temporary_station_state(
        workspace,
        inbound_start_node_ids=_S072_INBOUND_START_NODE_IDS,
        place_node_ids=_S072_PLACE_NODE_IDS,
        pick_node_ids=_S072_PICK_NODE_IDS,
    )


def _temporary_s072_trigger_satisfied(
    workspace: dict[str, Any],
    *,
    instance_id: str,
    node_id: str,
) -> bool:
    """附加 S072 临时触发条件；后续由实体传感器条件替换。"""
    return _temporary_station_trigger_satisfied(
        workspace,
        instance_id=instance_id,
        node_id=node_id,
        inbound_start_node_ids=_S072_INBOUND_START_NODE_IDS,
        place_node_ids=_S072_PLACE_NODE_IDS,
        occupied_required_node_ids=_S072_OCCUPIED_REQUIRED_NODE_IDS,
        pick_node_ids=_S072_PICK_NODE_IDS,
    )


def _temporary_s09_state(workspace: dict[str, Any]) -> _TemporaryStationState:
    """恢复 S09 烧杯工位的临时有料状态。"""
    return _temporary_station_state(
        workspace,
        inbound_start_node_ids=_S09_INBOUND_START_NODE_IDS,
        place_node_ids=_S09_PLACE_NODE_IDS,
        pick_node_ids=_S09_PICK_NODE_IDS,
    )


def _temporary_s09_trigger_satisfied(
    workspace: dict[str, Any],
    *,
    instance_id: str,
    node_id: str,
) -> bool:
    """附加 S09 烧杯工位临时触发条件；后续由实体传感器条件替换。"""
    return _temporary_station_trigger_satisfied(
        workspace,
        instance_id=instance_id,
        node_id=node_id,
        inbound_start_node_ids=_S09_INBOUND_START_NODE_IDS,
        place_node_ids=_S09_PLACE_NODE_IDS,
        occupied_required_node_ids=_S09_OCCUPIED_REQUIRED_NODE_IDS,
        pick_node_ids=_S09_PICK_NODE_IDS,
    )


_ATOMIC_START_ACTIVE_CONFLICTS: dict[str, frozenset[str]] = {
    "w01_pick_beaker_s03": frozenset({"w01_dose_powder_s07"}),
    "w02_pick_beaker_s072": frozenset({"w02_add_solvent_s06"}),
    "w03_pick_beaker_s06": frozenset(
        {"w02_add_solvent_s06", "w03_add_liquid_s09"}
    ),
    "w04_pick_beaker_s09": frozenset(
        {"w03_add_liquid_s09", "w04_run_stirring_s04"}
    ),
}


def _active_workflow_node_ids(workspace: dict[str, Any]) -> set[str]:
    """返回服务端当前仍在执行的 workflow 节点。"""
    active: set[str] = set()
    for instance in workspace.get("task_instances", []):
        if not isinstance(instance, dict):
            continue
        state = instance.get("execution_state")
        if not isinstance(state, dict):
            continue
        active_node_id = str(state.get("active_node_id") or "")
        if active_node_id:
            active.add(active_node_id)
    return active


def _read_trigger_variable(reader: Any, variable_name: str) -> Any:
    read_variable = getattr(reader, "read_variable", None)
    if not callable(read_variable):
        raise RuntimeError("缺少 PLC 变量读取接口")
    return read_variable(variable_name, use_cache=True)


def _s09_volume_to_raw(volume: Any, volume_unit: Any) -> int:
    unit = str(volume_unit or "raw").strip().lower()
    value = float(volume)
    if unit in {"raw", "int", "int16", "plc", "0.1ul", "0.1µl"}:
        return int(round(value))
    if unit in {"ul", "µl", "μl", "microliter", "microliters"}:
        return int(round(value * 10))
    if unit in {"ml", "milliliter", "milliliters"}:
        return int(round(value * 10000))
    raise ValueError("S09 体积单位必须是 raw、uL 或 mL")


def _atomic_start_signal_conditions(node: WorkflowNode) -> dict[str, Any]:
    """生成主工艺前八个原子 Task 的首动作 PLC 触发条件。"""
    params = node.param
    if node.uuid == "w01_pick_beaker_s03":
        return {
            product_slot_sensor(
                int(params.get("product_type", 1)),
                params.get("position", "1-1"),
                used=False,
            ): True,
        }
    if node.uuid == "w01_dose_powder_s07":
        return {
            S07_HOME_VAR: True,
            S07_ALLOW_PROCESS_VAR: True,
        }
    if node.uuid == "w02_pick_beaker_s072":
        return {ADDITION_BEAKER_SENSOR: False}
    if node.uuid == "w02_add_solvent_s06":
        return {
            ADDITION_BEAKER_SENSOR: True,
            S06_READY_VAR: True,
            S06_ALLOW_PROCESS_VAR: True,
            S06_DONE_VAR: False,
        }
    if node.uuid == "w03_pick_beaker_s06":
        return {
            ADDITION_BEAKER_SENSOR: True,
            S09_HOME_SIGNALS[4]: True,
        }
    if node.uuid == "w03_add_liquid_s09":
        take_tip_box = int(params.get("take_tip_box_index", 1))
        release_tip_box = int(params.get("release_tip_box_index", 2))
        liquid_bottle = int(params.get("liquid_bottle_index", 1))
        station = int(params.get("station", 1))
        return {
            S09_TIP_BOX_SENSORS[take_tip_box]: True,
            S09_TIP_BOX_SENSORS[release_tip_box]: True,
            S09_STATION_SENSORS[liquid_bottle]: True,
            S09_STATION_SENSORS[station]: True,
            S09_HOME_SIGNALS[1]: True,
            S09_ALLOW_PROCESS_VAR: True,
            S09_PROCESS_DONE_VAR: False,
        }
    if node.uuid == "w04_pick_beaker_s09":
        position = int(params.get("position", 1))
        return {
            S09_HOME_SIGNALS[4]: True,
            s04_material_sensor_var(position): False,
            s04_ready_var(position): True,
        }
    if node.uuid == "w04_run_stirring_s04":
        position = int(params.get("position", 1))
        return {
            s04_material_sensor_var(position): True,
            s04_status_var(position): 1,
            s04_allow_var(position): True,
        }
    return {}


def _s09_remaining_volume_satisfied(node: WorkflowNode, plc: Any) -> bool:
    if node.uuid != "w03_add_liquid_s09":
        return True
    params = node.param
    if bool(params.get("skip_level_check", False)):
        return True
    bottle = int(params.get("liquid_bottle_index", 1))
    configured = params.get(f"S09液体瓶{bottle}剩余液量")
    remaining_ml = (
        float(configured)
        if configured is not None
        else float(
            _read_trigger_variable(plc, s09_remaining_volume_var(bottle))
        )
    )
    raw_volume = _s09_volume_to_raw(
        params.get("aspirate_volume", 1),
        params.get("volume_unit", "raw"),
    )
    return raw_volume > 0 and remaining_ml + 1e-9 >= raw_volume / 10000.0


def _atomic_task_start_trigger_satisfied(
    workspace: dict[str, Any],
    *,
    node: WorkflowNode,
    devices: dict[str, Any],
) -> bool:
    """首动作认领前统一检查前八个原子 Task；读取异常时安全等待。"""
    conditions = _atomic_start_signal_conditions(node)
    conflicts = _ATOMIC_START_ACTIVE_CONFLICTS.get(node.uuid, frozenset())
    if conflicts & _active_workflow_node_ids(workspace):
        return False
    if not conditions:
        return True

    plc = devices.get("szlab_poly_plc")
    if plc is None:
        return False
    try:
        if any(
            _read_trigger_variable(plc, name) != expected
            for name, expected in conditions.items()
        ):
            return False
        return _s09_remaining_volume_satisfied(node, plc)
    except Exception:
        return False


@dataclass
class _InFlightAction:
    future: Future[Any]
    workflow_path: str
    instance_id: str
    node_id: str
    execution_id: str
    device_name: str
    result_summary: Any = None
    outcome_prepared: bool = False
    error: dict[str, str] | None = None


@dataclass
class _PendingTerminalReport:
    workflow_path: str
    instance_id: str
    node_id: str
    execution_id: str
    error: dict[str, str]
    retryable: bool = True
    report_error: str | None = None


class TaskExecutionCoordinator:
    """以 Task 工作区为权威状态，异步执行已认领 workflow 节点。"""

    _WAIT_CONFLICTS = {
        "version_conflict",
        "action_already_active",
        "workspace_paused",
    }
    _NON_RETRYABLE_TERMINAL_CONFLICTS = {
        "action_execution_not_found",
        "action_not_active",
        "action_replay_conflict",
        "instance_not_found",
        "instance_not_running",
    }

    def __init__(
        self,
        *,
        task_client: Any,
        node_runner: Callable[
            [WorkflowNode, dict[str, Any], Callable[..., Any]],
            Any,
        ]
        | Callable[
            [WorkflowNode, dict[str, Any], Callable[..., Any], dict[str, Any]],
            Any,
        ],
        device_provider: Callable[[], dict[str, Any]],
        max_workers: int = 4,
    ) -> None:
        self._task_client = task_client
        self._node_runner = node_runner
        self._device_provider = device_provider
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="TaskAction",
        )
        self._lock = threading.RLock()
        self._in_flight: dict[str, _InFlightAction] = {}
        self._pending_terminal_reports: dict[
            str, _PendingTerminalReport
        ] = {}
        self._reported_execution_ids: set[str] = set()
        self._closing = threading.Event()

    def _invoke_node_runner(
        self,
        node: WorkflowNode,
        devices: dict[str, Any],
        action_callable: Callable[..., Any],
        context: dict[str, Any],
    ) -> Any:
        try:
            return self._node_runner(node, devices, action_callable, context)
        except TypeError:
            return self._node_runner(node, devices, action_callable)

    def shutdown(self) -> dict[str, Any]:
        """停止认领，等待实体动作结束，再尽力上报其终态。"""
        self._closing.set()
        with self._lock:
            pass
        self._executor.shutdown(wait=True, cancel_futures=False)
        stats: dict[str, Any] = {
            "success": True,
            "in_flight": 0,
            "completed": 0,
            "failed": 0,
        }
        with self._lock:
            self._harvest_completed(stats)
            self._retry_pending_terminal_report(stats)
            self._update_activity_stats(stats)
            if self._in_flight or self._pending_terminal_reports:
                stats["success"] = False
                stats["message"] = (
                    "动作已结束，但终态上报暂未完成；"
                    "服务端 active execution 将由下次进程安全恢复"
                )
        return stats

    def cycle(
        self,
        *,
        workflow_path: str,
        workflow_nodes: Iterable[WorkflowNode],
        harvest_only: bool = False,
    ) -> dict[str, int | bool]:
        """收割已完成动作并认领新动作；不等待设备动作完成。"""
        if type(harvest_only) is not bool:
            raise TypeError("harvest_only 必须为 bool")
        nodes_by_id = {
            node.uuid: node
            for node in workflow_nodes
            if not node.disabled
        }
        stats: dict[str, int | bool] = {
            "success": True,
            "active": 0,
            "in_flight": 0,
            "claimed": 0,
            "completed": 0,
            "failed": 0,
        }
        with self._lock:
            self._harvest_completed(stats)
            if self._retry_pending_terminal_report(
                stats, workflow_path=workflow_path
            ):
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats
            if harvest_only:
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats
            if self._closing.is_set():
                stats["success"] = False
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats
            response = self._task_client.get_workspace(
                workflow_path=workflow_path
            )
            workspace = _workspace_from_response(response)
            if workspace.get("scheduler_paused") or workspace.get("pause_reason"):
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats

            templates = {
                str(template.get("id")): template
                for template in workspace.get("templates", [])
                if isinstance(template, dict)
            }
            current_response = response
            if self._recover_orphaned_execution(
                current_response,
                workflow_path=workflow_path,
                workspace=workspace,
                stats=stats,
            ):
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats

            devices = self._device_provider()
            busy_device_names = self._busy_device_names()
            continuation_device_owners = _continuation_device_owners(
                workspace,
                templates=templates,
                nodes_by_id=nodes_by_id,
            )

            for instance in workspace.get("task_instances", []):
                if not isinstance(instance, dict) or instance.get("status") != "running":
                    continue
                template = templates.get(str(instance.get("template_id")))
                state = instance.get("execution_state") or {}
                node_ids = template.get("node_ids", []) if template else []
                cursor = int(state.get("cursor", 0))
                if cursor >= len(node_ids):
                    continue
                stats["active"] = int(stats["active"]) + 1
                if state.get("active_execution_id") or self._instance_is_in_flight(
                    str(instance.get("id"))
                ):
                    continue

                node_id = str(node_ids[cursor])
                node = nodes_by_id.get(node_id)
                instance_id = str(instance.get("id"))
                if node is not None:
                    continuation_owner = continuation_device_owners.get(
                        node.device_name
                    )
                    if (
                        continuation_owner is not None
                        and continuation_owner != instance_id
                    ):
                        continue
                payload = instance.get("payload")
                node_parameters = (
                    payload.get("node_parameters")
                    if isinstance(payload, dict)
                    else None
                )
                override = (
                    node_parameters.get(node_id)
                    if isinstance(node_parameters, dict)
                    else None
                )
                if node is not None and isinstance(override, dict):
                    node = replace(node, param={**node.param, **override})
                execution_id = deterministic_execution_id(
                    str(instance.get("id")), cursor, node_id
                )
                if execution_id in self._reported_execution_ids:
                    continue
                if self._closing.is_set():
                    break
                trigger_workspace = _workspace_from_response(current_response)
                if not _temporary_s072_trigger_satisfied(
                    trigger_workspace,
                    instance_id=instance_id,
                    node_id=node_id,
                ):
                    continue
                if not _temporary_s09_trigger_satisfied(
                    trigger_workspace,
                    instance_id=instance_id,
                    node_id=node_id,
                ):
                    continue
                if (
                    cursor == 0
                    and node is not None
                    and not _atomic_task_start_trigger_satisfied(
                        trigger_workspace,
                        node=node,
                        devices=devices,
                    )
                ):
                    continue
                method_name = ""
                action_callable: Callable[..., Any] | None = None
                if node is not None:
                    try:
                        method_name = node_method(node)
                        device = devices.get(node.device_name)
                        if device is not None:
                            action_callable = getattr(
                                device, method_name, None
                            )
                    except (AttributeError, ValueError):
                        action_callable = None
                device = devices.get(node.device_name) if node is not None else None
                if (
                    node is None
                    or device is None
                    or not callable(action_callable)
                ):
                    claimed_response = self._claim(
                        current_response,
                        workflow_path=workflow_path,
                        instance_id=instance_id,
                        node_id=node_id,
                        execution_id=execution_id,
                        resources=[],
                    )
                    if claimed_response is None:
                        continue
                    stats["claimed"] = int(stats["claimed"]) + 1
                    current_response = claimed_response
                    self._submit_terminal_report(
                        _PendingTerminalReport(
                            workflow_path=workflow_path,
                            instance_id=str(instance.get("id")),
                            node_id=node_id,
                            execution_id=execution_id,
                            error={
                                "code": "unsupported_action",
                                "message": (
                                    f"不支持的 Task 动作节点: {node_id}"
                                ),
                            },
                        ),
                        expected_version=int(current_response["version"]),
                        stats=stats,
                    )
                    break

                if node.device_name in busy_device_names:
                    continue

                claimed_response = self._claim(
                    current_response,
                    workflow_path=workflow_path,
                    instance_id=instance_id,
                    node_id=node_id,
                    execution_id=execution_id,
                    resources=[],
                )
                if claimed_response is None:
                    continue
                current_response = claimed_response
                stats["claimed"] = int(stats["claimed"]) + 1
                busy_device_names.add(node.device_name)
                try:
                    future = self._executor.submit(
                        self._invoke_node_runner,
                        node,
                        devices,
                        action_callable,
                        {
                            "workflow_path": workflow_path,
                            "instance_id": instance_id,
                            "node_id": node_id,
                            "execution_id": execution_id,
                            "sample_id": str(instance.get("sample_id") or ""),
                        },
                    )
                except Exception as exc:
                    pending = _PendingTerminalReport(
                        workflow_path=workflow_path,
                        instance_id=str(instance.get("id")),
                        node_id=node_id,
                        execution_id=execution_id,
                        error={
                            "code": "action_dispatch_failed",
                            "message": str(exc),
                        },
                    )
                    self._submit_terminal_report(
                        pending,
                        expected_version=int(current_response["version"]),
                        stats=stats,
                    )
                    break
                self._in_flight[execution_id] = _InFlightAction(
                    future=future,
                    workflow_path=workflow_path,
                    instance_id=instance_id,
                    node_id=node_id,
                    execution_id=execution_id,
                    device_name=node.device_name,
                )

            self._update_activity_stats(stats, workflow_path=workflow_path)
            return stats

    def _submit_terminal_report(
        self,
        pending: _PendingTerminalReport,
        *,
        expected_version: int,
        stats: dict[str, Any],
    ) -> bool:
        try:
            self._task_client.fail_action(
                workflow_path=pending.workflow_path,
                expected_version=expected_version,
                instance_id=pending.instance_id,
                node_id=pending.node_id,
                execution_id=pending.execution_id,
                error=pending.error,
            )
        except Exception as exc:
            pending.retryable = not (
                isinstance(exc, TaskApiConflict)
                and exc.code in self._NON_RETRYABLE_TERMINAL_CONFLICTS
            )
            pending.report_error = str(exc)
            self._pending_terminal_reports[pending.execution_id] = pending
            if not pending.retryable:
                stats["success"] = False
            return False
        self._pending_terminal_reports.pop(pending.execution_id, None)
        self._reported_execution_ids.add(pending.execution_id)
        stats["failed"] = int(stats["failed"]) + 1
        return True

    def _retry_pending_terminal_report(
        self,
        stats: dict[str, int | bool],
        *,
        workflow_path: str | None = None,
    ) -> bool:
        pending = next(
            (
                item
                for item in self._pending_terminal_reports.values()
                if workflow_path is None or item.workflow_path == workflow_path
            ),
            None,
        )
        if pending is None:
            return False
        if not pending.retryable:
            stats["success"] = False
            return True
        try:
            response = self._task_client.get_workspace(
                workflow_path=pending.workflow_path
            )
        except Exception as exc:
            pending.report_error = str(exc)
            return True
        self._submit_terminal_report(
            pending,
            expected_version=int(response["version"]),
            stats=stats,
        )
        return True

    def _update_activity_stats(
        self,
        stats: dict[str, Any],
        *,
        workflow_path: str | None = None,
    ) -> None:
        pending_count = sum(
            1
            for item in self._pending_terminal_reports.values()
            if workflow_path is None or item.workflow_path == workflow_path
        )
        stats["in_flight"] = len(self._in_flight) + pending_count
        if "active" in stats and pending_count:
            stats["active"] = max(int(stats["active"]), pending_count)

    def _recover_orphaned_execution(
        self,
        response: dict[str, Any],
        *,
        workflow_path: str,
        workspace: dict[str, Any],
        stats: dict[str, int | bool],
    ) -> bool:
        """失败关闭服务端有记录但本进程无法证明正在执行的动作。"""
        for instance in workspace.get("task_instances", []):
            if not isinstance(instance, dict) or instance.get("status") != "running":
                continue
            state = instance.get("execution_state") or {}
            execution_id = str(state.get("active_execution_id") or "")
            if not execution_id or execution_id in self._in_flight:
                continue
            node_id = str(state.get("active_node_id") or "")
            stats["active"] = int(stats["active"]) + 1
            self._submit_terminal_report(
                _PendingTerminalReport(
                    workflow_path=workflow_path,
                    instance_id=str(instance.get("id")),
                    node_id=node_id,
                    execution_id=execution_id,
                    error={
                        "code": "orphaned_execution",
                        "message": (
                            "服务端存在活动执行但本地无对应 future，无法安全恢复"
                        ),
                    },
                ),
                expected_version=int(response["version"]),
                stats=stats,
            )
            return True
        return False

    def _claim(
        self,
        response: dict[str, Any],
        *,
        workflow_path: str,
        instance_id: str,
        node_id: str,
        execution_id: str,
        resources: list[str],
    ) -> dict[str, Any] | None:
        try:
            return self._task_client.claim_action(
                workflow_path=workflow_path,
                expected_version=int(response["version"]),
                instance_id=instance_id,
                node_id=node_id,
                execution_id=execution_id,
                resources=resources,
            )
        except TaskApiConflict as exc:
            if exc.code in self._WAIT_CONFLICTS:
                return None
            raise

    def _harvest_completed(self, stats: dict[str, int | bool]) -> None:
        for execution_id, action in list(self._in_flight.items()):
            if not action.future.done():
                continue
            if not action.outcome_prepared:
                self._prepare_outcome(action)
            if action.error is None:
                try:
                    response = self._task_client.get_workspace(
                        workflow_path=action.workflow_path
                    )
                    self._task_client.succeed_action(
                        workflow_path=action.workflow_path,
                        expected_version=int(response["version"]),
                        instance_id=action.instance_id,
                        node_id=action.node_id,
                        execution_id=execution_id,
                        result=action.result_summary,
                        release_resources=[],
                    )
                except Exception:
                    continue
                stats["completed"] = int(stats["completed"]) + 1
            else:
                try:
                    response = self._task_client.get_workspace(
                        workflow_path=action.workflow_path
                    )
                    self._task_client.fail_action(
                        workflow_path=action.workflow_path,
                        expected_version=int(response["version"]),
                        instance_id=action.instance_id,
                        node_id=action.node_id,
                        execution_id=execution_id,
                        error=action.error,
                    )
                except Exception:
                    continue
                stats["failed"] = int(stats["failed"]) + 1
            self._reported_execution_ids.add(execution_id)
            del self._in_flight[execution_id]

    def _prepare_outcome(self, action: _InFlightAction) -> None:
        """只判定一次实体动作结果，后续 tick 仅重试对应终态上报。"""
        action.outcome_prepared = True
        try:
            result = action.future.result()
            summary = _json_safe(result)
            failure = _false_result(summary)
            if failure is not None:
                raise RuntimeError(f"动作返回 success=false: {failure}")
            action.result_summary = summary
        except Exception as exc:
            action.error = {
                "code": "action_failed",
                "message": str(exc),
            }

    def _instance_is_in_flight(self, instance_id: str) -> bool:
        return any(
            action.instance_id == instance_id
            for action in self._in_flight.values()
        )

    def _busy_device_names(self) -> set[str]:
        return {
            action.device_name
            for action in self._in_flight.values()
            if action.device_name
        }


def _workspace_from_response(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(response.get("version"), int):
        raise RuntimeError("Task API 未返回有效工作区版本")
    workspace = response.get("workspace")
    if not isinstance(workspace, dict):
        raise RuntimeError("Task API 未返回有效工作区")
    return workspace


def _false_result(
    value: Any,
    *,
    _allow_bare_false: bool = True,
) -> Any | None:
    if _allow_bare_false and type(value) is bool:
        return value if value is False else None
    if isinstance(value, dict):
        if value.get("success") is False:
            return value
        if "result" in value:
            failure = _false_result(
                value["result"],
                _allow_bare_false=True,
            )
            if failure is not None:
                return failure
        for key, nested in value.items():
            if key == "result":
                continue
            failure = _false_result(
                nested,
                _allow_bare_false=False,
            )
            if failure is not None:
                return failure
    if isinstance(value, (list, tuple)):
        if (
            _allow_bare_false
            and value
            and type(value[0]) is bool
            and value[0] is False
        ):
            return value
        for item in value:
            failure = _false_result(
                item,
                _allow_bare_false=False,
            )
            if failure is not None:
                return failure
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)
