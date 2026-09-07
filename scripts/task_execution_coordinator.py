"""Task 工作区动作认领、后台执行与完成上报协调器。"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Callable, Iterable

from scripts.run_workflow_local import (
    WorkflowNode,
    node_method,
    workflow_node_from_mapping,
)
from scripts.task_action_result import find_action_failure
from unilabos.devices.workstation.szlab_poly_studio.error_codes import (
    enrich_mixing_failure,
    enrich_with_plc_alarm,
    read_active_plc_alarms,
)
from unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.sensors import (
    s04_allow_var,
    s04_material_sensor_var,
    s04_ready_var,
    s04_status_var,
)
from unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.sensors import (
    S05_READY,
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
from unilabos.devices.workstation.szlab_poly_studio.sensor import S08Sensors
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.sensors import (
    S09_ALLOW_PROCESS_VAR,
    S09_PROCESS_DONE_VAR,
    S09_STATION_SENSORS,
    s09_remaining_volume_var,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import (
    S05_MATERIAL_SENSOR,
    product_slot_sensor,
)


logger = logging.getLogger(__name__)


class TaskDispatchPreflightCode(StrEnum):
    """Task 派发前固定使用的结构化检查错误码。"""

    EMPTY_SCOPE = "task_dispatch_scope_empty"
    RECOVERY_REQUIRED = "workspace_recovery_required"
    TEMPLATE_MISSING = "task_template_missing"
    TEMPLATE_EMPTY = "task_template_empty"
    NODE_MISSING = "task_node_missing"
    NODE_NOT_EXECUTABLE = "task_node_not_executable"
    NODE_INVALID = "task_node_invalid"
    DEVICE_MISSING = "task_device_missing"
    ACTION_UNSUPPORTED = "task_action_unsupported"


@dataclass(frozen=True)
class TaskDispatchPreflightIssue:
    """单个 Task 派发阻断项；字段可直接进入调度日志契约。"""

    code: TaskDispatchPreflightCode
    message: str
    template_id: str = ""
    template_name: str = ""
    node_id: str = ""
    device_id: str = ""
    action_name: str = ""
    instance_ids: tuple[str, ...] = ()
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": "dispatch_preflight",
            "code": self.code.value,
            "message": self.message,
            "severity": "error",
            "phase": "preflight",
            "template_id": self.template_id,
            "template_name": self.template_name,
            "node_id": self.node_id,
            "device_id": self.device_id,
            "action_name": self.action_name,
            "instance_ids": list(self.instance_ids),
            "detail": dict(self.detail or {}),
        }


@dataclass(frozen=True)
class TaskDispatchPreflightResult:
    """Task 派发预检结果。"""

    errors: tuple[TaskDispatchPreflightIssue, ...] = ()
    warnings: tuple[TaskDispatchPreflightIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [item.as_dict() for item in self.errors],
            "warnings": [item.as_dict() for item in self.warnings],
        }


class TaskApiConflict(RuntimeError):
    """Task API 的正常乐观锁或资源等待冲突。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def deterministic_execution_id(instance_id: str, cursor: int, node_id: str) -> str:
    """根据服务端权威游标生成可跨进程重放的执行 ID。"""
    identity = f"{instance_id}\0{cursor}\0{node_id}".encode()
    return f"task-action-{hashlib.sha256(identity).hexdigest()[:32]}"


def _append_diagnostic(
    stats: dict[str, Any],
    *,
    instance: dict[str, Any] | None,
    code: str,
    message: str,
    node: WorkflowNode | None = None,
    node_id: str = "",
    immediate: bool = False,
    severity: str = "warning",
    category: str = "dispatch_stall",
    detail: dict[str, Any] | None = None,
    phase: str = "等待派发",
    execution_id: str = "",
    template_id: str = "",
    device_id: str = "",
    action_name: str = "",
) -> None:
    diagnostics = stats.setdefault("diagnostics", [])
    if not isinstance(diagnostics, list):
        return
    resolved_action_name = action_name
    if node is not None:
        try:
            resolved_action_name = node_method(node)
        except ValueError:
            resolved_action_name = action_name
    diagnostic = {
        "category": category,
        "code": code,
        "message": message,
        "severity": severity,
        "immediate": immediate,
        "phase": phase,
        "instance_id": str((instance or {}).get("id") or ""),
        "sample_id": str((instance or {}).get("sample_id") or ""),
        "node_id": node_id or (node.uuid if node is not None else ""),
        "device_id": (
            node.device_name if node is not None else str(device_id or "")
        ),
        "action_name": resolved_action_name,
        "detail": detail or {},
    }
    if execution_id:
        diagnostic["execution_id"] = execution_id
    resolved_template_id = template_id or str(
        (instance or {}).get("template_id") or ""
    )
    if resolved_template_id:
        diagnostic["template_id"] = resolved_template_id
    diagnostics.append(diagnostic)


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


_PREFLIGHT_ACTIVE_INSTANCE_STATUSES = frozenset(
    {"waiting", "pending", "running"}
)


def validate_task_dispatch_preflight(
    workspace: dict[str, Any],
    workflow_nodes: Iterable[WorkflowNode],
    devices: dict[str, Any],
) -> TaskDispatchPreflightResult:
    """检查本次 Task 派发范围与实际 workflow、设备动作是否一致。"""
    if not isinstance(workspace, dict):
        raise TypeError("workspace 必须是对象")
    if not isinstance(devices, dict):
        raise TypeError("devices 必须是对象")

    nodes_by_id: dict[str, WorkflowNode] = {}
    duplicate_node_ids: set[str] = set()
    for node in workflow_nodes:
        if not isinstance(node, WorkflowNode):
            raise TypeError("workflow_nodes 必须包含 WorkflowNode")
        if node.uuid in nodes_by_id:
            duplicate_node_ids.add(node.uuid)
        nodes_by_id[node.uuid] = node

    templates = {
        str(template.get("id") or ""): template
        for template in workspace.get("templates", [])
        if isinstance(template, dict) and str(template.get("id") or "")
    }
    active_instances = [
        instance
        for instance in workspace.get("task_instances", [])
        if isinstance(instance, dict)
        and instance.get("status") in _PREFLIGHT_ACTIVE_INSTANCE_STATUSES
    ]
    instance_ids_by_template: dict[str, list[str]] = {}
    for instance in active_instances:
        template_id = str(instance.get("template_id") or "")
        instance_id = str(instance.get("id") or "")
        if template_id:
            instance_ids_by_template.setdefault(template_id, [])
            if instance_id:
                instance_ids_by_template[template_id].append(instance_id)

    target_template_ids: list[str] = []

    def add_target(template_id: Any) -> None:
        resolved = str(template_id or "")
        if resolved and resolved not in target_template_ids:
            target_template_ids.append(resolved)

    for template_id in workspace.get("scheduled_template_ids", []):
        add_target(template_id)
    for instance in active_instances:
        add_target(instance.get("template_id"))

    errors: list[TaskDispatchPreflightIssue] = []
    if workspace.get("pause_reason") is not None:
        errors.append(TaskDispatchPreflightIssue(
            code=TaskDispatchPreflightCode.RECOVERY_REQUIRED,
            message="Task 工作区存在失败暂停，需先执行显式恢复",
            detail={"pause_reason": workspace.get("pause_reason")},
        ))
    if not target_template_ids:
        errors.append(TaskDispatchPreflightIssue(
            code=TaskDispatchPreflightCode.EMPTY_SCOPE,
            message="当前没有已排程模板或可继续派发的 Task 实例",
        ))

    for template_id in target_template_ids:
        template = templates.get(template_id)
        instance_ids = tuple(instance_ids_by_template.get(template_id, []))
        if template is None:
            errors.append(TaskDispatchPreflightIssue(
                code=TaskDispatchPreflightCode.TEMPLATE_MISSING,
                message=f"派发范围引用了不存在的 Task 模板: {template_id}",
                template_id=template_id,
                instance_ids=instance_ids,
            ))
            continue
        template_name = str(template.get("name") or template_id)
        raw_node_ids = template.get("node_ids")
        node_ids = raw_node_ids if isinstance(raw_node_ids, list) else []
        if not node_ids:
            errors.append(TaskDispatchPreflightIssue(
                code=TaskDispatchPreflightCode.TEMPLATE_EMPTY,
                message=f"Task「{template_name}」没有可派发的动作节点",
                template_id=template_id,
                template_name=template_name,
                instance_ids=instance_ids,
            ))
            continue

        for raw_node_id in node_ids:
            node_id = str(raw_node_id or "")
            node = nodes_by_id.get(node_id)
            common = {
                "template_id": template_id,
                "template_name": template_name,
                "node_id": node_id,
                "instance_ids": instance_ids,
            }
            if node is None:
                errors.append(TaskDispatchPreflightIssue(
                    code=TaskDispatchPreflightCode.NODE_MISSING,
                    message=(
                        f"Task「{template_name}」引用的动作节点不在当前 "
                        f"workflow 中: {node_id or '<empty>'}"
                    ),
                    **common,
                ))
                continue
            if node_id in duplicate_node_ids:
                errors.append(TaskDispatchPreflightIssue(
                    code=TaskDispatchPreflightCode.NODE_INVALID,
                    message=(
                        f"Task「{template_name}」引用了 ID 重复的动作节点: "
                        f"{node_id}"
                    ),
                    detail={"reason": "duplicate_workflow_node_id"},
                    **common,
                ))
                continue
            if node.disabled:
                errors.append(TaskDispatchPreflightIssue(
                    code=TaskDispatchPreflightCode.NODE_NOT_EXECUTABLE,
                    message=(
                        f"Task「{template_name}」的动作节点当前不可执行: "
                        f"{node_id}"
                    ),
                    device_id=node.device_name,
                    detail={"reason": "disabled"},
                    **common,
                ))
                continue
            try:
                action_name = node_method(node)
            except ValueError as exc:
                errors.append(TaskDispatchPreflightIssue(
                    code=TaskDispatchPreflightCode.NODE_INVALID,
                    message=(
                        f"Task「{template_name}」的动作节点配置无效: "
                        f"{node_id}"
                    ),
                    device_id=node.device_name,
                    detail={"reason": str(exc)},
                    **common,
                ))
                continue
            device = devices.get(node.device_name)
            if device is None:
                errors.append(TaskDispatchPreflightIssue(
                    code=TaskDispatchPreflightCode.DEVICE_MISSING,
                    message=(
                        f"Task「{template_name}」的动作设备不可用: "
                        f"{node.device_name}"
                    ),
                    device_id=node.device_name,
                    action_name=action_name,
                    **common,
                ))
                continue
            if not callable(getattr(device, action_name, None)):
                errors.append(TaskDispatchPreflightIssue(
                    code=TaskDispatchPreflightCode.ACTION_UNSUPPORTED,
                    message=(
                        f"Task「{template_name}」的设备不支持动作: "
                        f"{node.device_name}.{action_name}"
                    ),
                    device_id=node.device_name,
                    action_name=action_name,
                    **common,
                ))

    return TaskDispatchPreflightResult(errors=tuple(errors))


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
_S09_INBOUND_START_NODE_IDS = frozenset({
    "w03_pick_beaker_s06",
    "w06_pick_beaker_s05_for_density",
})
_S09_PLACE_NODE_IDS = frozenset({
    "w03_place_beaker_s09",
    "w05_place_beaker_s09_for_density",
})
_S09_OCCUPIED_REQUIRED_NODE_IDS = frozenset(
    {
        "w03_add_liquid_s09",
        "w04_pick_beaker_s09",
        "w05_measure_density_s09",
        "w06_pick_beaker_s09_after_density",
    }
)
_S09_PICK_NODE_IDS = frozenset({
    "w04_pick_beaker_s09",
    "w06_pick_beaker_s09_after_density",
})
_S08_INBOUND_START_NODE_IDS = frozenset({"w05_pick_sample_vial_s03"})
_S08_PLACE_NODE_IDS = frozenset({"w05_place_sample_vial_s08"})
_S08_OCCUPIED_REQUIRED_NODE_IDS = frozenset(
    {
        "w05_open_sample_vial_s08",
        "w06_pick_beaker_s09_after_density",
        "w07_pour_beaker_s08",
        "w07_close_sample_vial_s08",
        "w07_pick_sample_vial_s08",
    }
)
_S08_PICK_NODE_IDS = frozenset({"w07_pick_sample_vial_s08"})
_S04_POSITION_SOURCE_NODE_ID = "w04_place_beaker_s04"
_S04_POSITION_DEPENDENT_NODE_IDS = frozenset({
    "w04_run_stirring_s04",
    "w06_pick_beaker_s04",
})


@dataclass(frozen=True)
class _TemporaryStationState:
    """实体传感器接入前，由成功动作记录推导的临时有料状态。"""

    has_material: bool
    inbound_instance_id: str | None = None
    occupied_sample_id: str | None = None


def _recorded_action_position(result: Any) -> int | None:
    """从直接或批量动作结果中提取实际执行位置。"""
    if isinstance(result, dict):
        if result.get("position") is not None:
            return int(result["position"])
        nested_result = _recorded_action_position(result.get("result"))
        if nested_result is not None:
            return nested_result
        params = result.get("param")
        if isinstance(params, dict) and params.get("position") is not None:
            return int(params["position"])
        return None
    if isinstance(result, list):
        for item in reversed(result):
            position = _recorded_action_position(item)
            if position is not None:
                return position
    return None


def _resolve_sample_s04_position(
    workspace: dict[str, Any],
    *,
    instance: dict[str, Any],
    templates: dict[str, dict[str, Any]],
    nodes_by_id: dict[str, WorkflowNode],
) -> int | None:
    """读取同一样品最近一次 S04 放料位置，供后续磁搅与取料继承。"""
    source_node = nodes_by_id.get(_S04_POSITION_SOURCE_NODE_ID)
    if source_node is None:
        return None
    sample_id = str(instance.get("sample_id") or "")
    current_order = int(instance.get("order") or 0)
    candidates: list[tuple[int, str, int]] = []
    for peer in workspace.get("task_instances", []):
        if not isinstance(peer, dict) or str(peer.get("sample_id") or "") != sample_id:
            continue
        peer_order = int(peer.get("order") or 0)
        if peer_order > current_order:
            continue
        template = templates.get(str(peer.get("template_id") or ""))
        if not template or _S04_POSITION_SOURCE_NODE_ID not in template.get("node_ids", []):
            continue
        recorded_position: int | None = None
        state = peer.get("execution_state")
        if isinstance(state, dict):
            for record in reversed(state.get("records", [])):
                if (
                    isinstance(record, dict)
                    and record.get("node_id") == _S04_POSITION_SOURCE_NODE_ID
                    and record.get("status") == "succeeded"
                ):
                    recorded_position = _recorded_action_position(record.get("result"))
                    if recorded_position is not None:
                        break

        payload = peer.get("payload")
        node_parameters = payload.get("node_parameters") if isinstance(payload, dict) else None
        override = (
            node_parameters.get(_S04_POSITION_SOURCE_NODE_ID)
            if isinstance(node_parameters, dict)
            else None
        )
        params = {**source_node.param, **(override if isinstance(override, dict) else {})}
        position = (
            recorded_position
            if recorded_position is not None
            else int(params.get("position", 1))
        )
        if position not in range(1, 5):
            raise ValueError("磁搅位置必须在 1-4 范围内")
        candidates.append((peer_order, str(peer.get("id") or ""), position))
    return max(candidates)[2] if candidates else None


def _find_free_s04_position(devices: dict[str, Any]) -> int | None:
    """按位置顺序读取实机信号，返回首个空闲且就绪的 S04 工位。"""
    plc = devices.get("szlab_poly_plc")
    if plc is None:
        logger.warning("S04 自动选位失败：缺少 PLC 设备 szlab_poly_plc")
        return None

    read_errors: list[str] = []
    for position in range(1, 5):
        try:
            if bool(_read_trigger_variable(plc, s04_material_sensor_var(position))):
                continue
            if not bool(_read_trigger_variable(plc, s04_ready_var(position))):
                continue
            return position
        except Exception as exc:
            read_errors.append(f"位置 {position}: {exc}")
            continue

    if read_errors:
        logger.warning(
            "S04 自动选位未找到可用工位，部分位置读取失败：%s",
            "; ".join(read_errors),
        )
    return None


def _temporary_station_state(
    workspace: dict[str, Any],
    *,
    inbound_start_node_ids: frozenset[str],
    place_node_ids: frozenset[str],
    pick_node_ids: frozenset[str],
) -> _TemporaryStationState:
    """用放/取成功记录恢复临时状态，避免进程重启后丢失。"""
    transitions: list[tuple[int, int, bool, str | None]] = []
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
                    (
                        int(record.get("finished_at") or 0),
                        sequence,
                        True,
                        str(instance.get("sample_id") or "") or None,
                    )
                )
                sequence += 1
            elif node_id in pick_node_ids:
                transitions.append(
                    (int(record.get("finished_at") or 0), sequence, False, None)
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
        inbound_started_count = len(
            succeeded_node_ids.intersection(inbound_start_node_ids)
        )
        inbound_finished_count = len(
            succeeded_node_ids.intersection(place_node_ids)
        )
        if (
            has_inbound_start
            and has_inbound_place
            and inbound_started_count > inbound_finished_count
        ):
            inbound_instance_id = str(instance.get("id") or "") or None

    transitions.sort()
    has_material = transitions[-1][2] if transitions else False
    return _TemporaryStationState(
        has_material=has_material,
        inbound_instance_id=inbound_instance_id,
        occupied_sample_id=(
            transitions[-1][3] if transitions and has_material else None
        ),
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


def _temporary_s08_state(workspace: dict[str, Any]) -> _TemporaryStationState:
    """从动作记录恢复 S08 样品瓶工位的预占、占用及所属样品。"""
    return _temporary_station_state(
        workspace,
        inbound_start_node_ids=_S08_INBOUND_START_NODE_IDS,
        place_node_ids=_S08_PLACE_NODE_IDS,
        pick_node_ids=_S08_PICK_NODE_IDS,
    )


def _temporary_s08_trigger_satisfied(
    workspace: dict[str, Any],
    *,
    instance_id: str,
    sample_id: str,
    node_id: str,
) -> bool:
    """保证 S08 从进瓶到取瓶期间只服务同一个样品。"""
    state = _temporary_s08_state(workspace)
    if node_id in _S08_INBOUND_START_NODE_IDS:
        return not state.has_material and state.inbound_instance_id is None
    if node_id in _S08_PLACE_NODE_IDS:
        return not state.has_material and state.inbound_instance_id in {
            None,
            instance_id,
        }
    if node_id in _S08_OCCUPIED_REQUIRED_NODE_IDS:
        return (
            state.has_material
            and state.occupied_sample_id is not None
            and state.occupied_sample_id == sample_id
        )
    return True


_ATOMIC_START_ACTIVE_CONFLICTS: dict[str, frozenset[str]] = {
    "w01_pick_beaker_s03": frozenset({"w01_dose_powder_s07"}),
    "w02_pick_beaker_s072": frozenset({"w02_add_solvent_s06"}),
    "w03_pick_beaker_s06": frozenset(
        {"w02_add_solvent_s06", "w03_add_liquid_s09"}
    ),
    # S04 的 1-4 号位独立提供有料与准备信号。其他位置正在磁搅时，
    # 仍应允许向空闲且就绪的位置搬运，因此这里只与 S09 自身加工冲突。
    "w04_pick_beaker_s09": frozenset({"w03_add_liquid_s09"}),
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


def _atomic_start_signal_conditions(
    node: WorkflowNode,
    *,
    next_node: WorkflowNode | None = None,
) -> dict[str, Any]:
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
        return {ADDITION_BEAKER_SENSOR: True}
    if node.uuid == "w03_add_liquid_s09":
        liquid_station = int(params.get("liquid_station_index", 1))
        return {
            S09_STATION_SENSORS[liquid_station]: True,
            S09_ALLOW_PROCESS_VAR: True,
            S09_PROCESS_DONE_VAR: False,
        }
    if node.uuid == "w04_pick_beaker_s09":
        target_params = (
            next_node.param
            if next_node is not None
            and next_node.uuid == "w04_place_beaker_s04"
            else params
        )
        position = int(target_params.get("position", 1))
        return {
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
    if node.uuid == "w06_pick_beaker_s04":
        position = int(params.get("position", 1))
        return {
            s04_material_sensor_var(position): True,
            S05_MATERIAL_SENSOR: False,
            S05_READY: True,
        }
    if node.uuid == "w05_pick_sample_vial_s03":
        position = params.get("position", "1-1")
        product_type = int(params.get("product_type", 2))
        return {
            product_slot_sensor(product_type, position, used=False): True,
            product_slot_sensor(1, position, used=False): False,
            S08Sensors.CAP_STATION[1]: False,
        }
    return {}


def _s09_remaining_volume_satisfied(node: WorkflowNode, plc: Any) -> bool:
    if node.uuid != "w03_add_liquid_s09":
        return True
    params = node.param
    if bool(params.get("skip_level_check", False)):
        return True
    bottle = int(params.get("liquid_station_index", 1))
    configured = params.get(f"S09液体瓶{bottle}剩余液量")
    remaining_ml = (
        float(configured)
        if configured is not None
        else float(
            _read_trigger_variable(plc, s09_remaining_volume_var(bottle))
        )
    )
    raw_volume = _s09_volume_to_raw(
        params.get("volume", 1),
        params.get("volume_unit", "raw"),
    )
    return raw_volume > 0 and remaining_ml + 1e-9 >= raw_volume / 10000.0


def _atomic_task_start_trigger_satisfied(
    workspace: dict[str, Any],
    *,
    node: WorkflowNode,
    next_node: WorkflowNode | None = None,
    devices: dict[str, Any],
) -> bool:
    """首动作认领前统一检查前八个原子 Task；读取异常时安全等待。"""
    conditions = _atomic_start_signal_conditions(node, next_node=next_node)
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
        # S06 加液完成后，不先把烧杯搬入 S09 占住单工位。只有确认
        # S04 至少存在一个无料且就绪的磁搅位，才启动 S06 -> S09 搬运。
        if (
            node.uuid == "w03_pick_beaker_s06"
            and _find_free_s04_position(devices) is None
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
    concurrency_key: str
    sample_id: str = ""
    template_id: str = ""
    action_name: str = ""
    result_summary: Any = None
    outcome_prepared: bool = False
    error: dict[str, Any] | None = None
    plc_alarm_reader: Any = None


@dataclass
class _PendingTerminalReport:
    workflow_path: str
    instance_id: str
    node_id: str
    execution_id: str
    error: dict[str, str]
    sample_id: str = ""
    template_id: str = ""
    device_id: str = ""
    action_name: str = ""
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
    ) -> dict[str, Any]:
        """收割已完成动作并认领新动作；不等待设备动作完成。"""
        if type(harvest_only) is not bool:
            raise TypeError("harvest_only 必须为 bool")
        nodes_by_id = {
            node.uuid: node
            for node in workflow_nodes
            if not node.disabled
        }
        stats: dict[str, Any] = {
            "success": True,
            "active": 0,
            "in_flight": 0,
            "claimed": 0,
            "completed": 0,
            "failed": 0,
            "diagnostics": [],
        }
        with self._lock:
            had_local_execution = any(
                action.workflow_path == workflow_path
                for action in self._in_flight.values()
            )
            self._harvest_completed(stats)
            if self._retry_pending_terminal_report(
                stats, workflow_path=workflow_path
            ):
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats
            if harvest_only and had_local_execution:
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
            # 进程重启后，本地 future 会丢失，但 Task 服务仍可能保留
            # active_execution_id。排空模式也必须检查并失败关闭这种孤立动作，
            # 否则前端会永久显示 running 且不会产生任何失败日志。
            if harvest_only:
                if self._recover_orphaned_execution(
                    response,
                    workflow_path=workflow_path,
                    workspace=workspace,
                    stats=stats,
                ):
                    self._update_activity_stats(
                        stats, workflow_path=workflow_path
                    )
                    return stats
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats
            if workspace.get("scheduler_paused") or workspace.get("pause_reason"):
                pause_reason = workspace.get("pause_reason")
                reason = pause_reason if isinstance(pause_reason, dict) else {}
                reason_instance_id = str(reason.get("instance_id") or "")
                affected_instance = next(
                    (
                        item
                        for item in workspace.get("task_instances", [])
                        if isinstance(item, dict)
                        and str(item.get("id") or "") == reason_instance_id
                    ),
                    None,
                )
                _append_diagnostic(
                    stats,
                    instance=affected_instance,
                    code=str(reason.get("code") or "scheduler_paused"),
                    message=str(reason.get("message") or "Task 排程已暂停"),
                    node_id=str(reason.get("node_id") or ""),
                    immediate=True,
                    severity="warning",
                    category="scheduler_alarm",
                    detail=reason.get("detail") if isinstance(reason.get("detail"), dict) else {},
                )
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
            busy_concurrency_keys = self._busy_concurrency_keys()
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
                        _append_diagnostic(
                            stats,
                            instance=instance,
                            code="device_continuation_owned",
                            message="设备由另一正在继续执行的样品占用",
                            node=node,
                            node_id=node_id,
                            detail={"owner_instance_id": continuation_owner},
                        )
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
                if node is not None and node_id in _S04_POSITION_DEPENDENT_NODE_IDS:
                    inherited_position = _resolve_sample_s04_position(
                        workspace,
                        instance=instance,
                        templates=templates,
                        nodes_by_id=nodes_by_id,
                    )
                    if inherited_position is not None:
                        node = replace(
                            node,
                            param={**node.param, "position": inherited_position},
                        )
                if node is not None and node_id == _S04_POSITION_SOURCE_NODE_ID:
                    free_position = _find_free_s04_position(devices)
                    if free_position is None:
                        continue
                    node = replace(
                        node,
                        param={**node.param, "position": free_position},
                    )
                next_node: WorkflowNode | None = None
                if cursor + 1 < len(node_ids):
                    next_node_id = str(node_ids[cursor + 1])
                    next_node = nodes_by_id.get(next_node_id)
                    next_override = (
                        node_parameters.get(next_node_id)
                        if isinstance(node_parameters, dict)
                        else None
                    )
                    if next_node is not None and isinstance(next_override, dict):
                        next_node = replace(
                            next_node,
                            param={**next_node.param, **next_override},
                        )
                    if (
                        next_node is not None
                        and next_node_id == _S04_POSITION_SOURCE_NODE_ID
                    ):
                        free_position = _find_free_s04_position(devices)
                        if free_position is None:
                            continue
                        next_node = replace(
                            next_node,
                            param={**next_node.param, "position": free_position},
                        )
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
                    _append_diagnostic(
                        stats,
                        instance=instance,
                        code="s072_material_state_wait",
                        message="等待 S072 临时有料状态满足",
                        node=node,
                        node_id=node_id,
                    )
                    continue
                if not _temporary_s09_trigger_satisfied(
                    trigger_workspace,
                    instance_id=instance_id,
                    node_id=node_id,
                ):
                    _append_diagnostic(
                        stats,
                        instance=instance,
                        code="s09_material_state_wait",
                        message="等待 S09 临时有料状态满足",
                        node=node,
                        node_id=node_id,
                    )
                    continue
                if not _temporary_s08_trigger_satisfied(
                    trigger_workspace,
                    instance_id=instance_id,
                    sample_id=str(instance.get("sample_id") or ""),
                    node_id=node_id,
                ):
                    _append_diagnostic(
                        stats,
                        instance=instance,
                        code="s08_sample_vial_state_wait",
                        message="等待 S08 样品瓶工位空闲或由当前样品占用",
                        node=node,
                        node_id=node_id,
                    )
                    continue
                if (
                    cursor == 0
                    and node is not None
                    and not _atomic_task_start_trigger_satisfied(
                        trigger_workspace,
                        node=node,
                        next_node=next_node,
                        devices=devices,
                    )
                ):
                    _append_diagnostic(
                        stats,
                        instance=instance,
                        code="input_trigger_wait",
                        message="等待首动作输入条件、工站状态或液量条件满足",
                        node=node,
                        node_id=node_id,
                    )
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
                    _append_diagnostic(
                        stats,
                        instance=instance,
                        code="unsupported_action",
                        message=f"不支持的 Task 动作节点: {node_id}",
                        node=node,
                        node_id=node_id,
                        immediate=True,
                        severity="error",
                        category="dispatch_error",
                        phase="dispatching",
                        execution_id=execution_id,
                        template_id=str(instance.get("template_id") or ""),
                    )
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
                            sample_id=str(instance.get("sample_id") or ""),
                            template_id=str(instance.get("template_id") or ""),
                            device_id=node.device_name if node is not None else "",
                            action_name=method_name,
                        ),
                        expected_version=int(current_response["version"]),
                        stats=stats,
                    )
                    break

                concurrency_key = _action_concurrency_key(node)
                if concurrency_key in busy_concurrency_keys:
                    _append_diagnostic(
                        stats,
                        instance=instance,
                        code="device_busy",
                        message=f"设备 {node.device_name} 正在执行其他动作",
                        node=node,
                        node_id=node_id,
                    )
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
                    _append_diagnostic(
                        stats,
                        instance=instance,
                        code="claim_wait",
                        message="动作认领遇到版本、活动动作或暂停冲突，等待重试",
                        node=node,
                        node_id=node_id,
                    )
                    continue
                current_response = claimed_response
                stats["claimed"] = int(stats["claimed"]) + 1
                busy_concurrency_keys.add(concurrency_key)
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
                            "template_id": str(instance.get("template_id") or ""),
                            "device_id": node.device_name,
                            "action_name": method_name,
                        },
                    )
                except Exception as exc:
                    _append_diagnostic(
                        stats,
                        instance=instance,
                        code="action_dispatch_failed",
                        message=str(exc),
                        node=node,
                        node_id=node_id,
                        immediate=True,
                        severity="error",
                        category="dispatch_error",
                        phase="dispatching",
                        execution_id=execution_id,
                        template_id=str(instance.get("template_id") or ""),
                    )
                    pending = _PendingTerminalReport(
                        workflow_path=workflow_path,
                        instance_id=str(instance.get("id")),
                        node_id=node_id,
                        execution_id=execution_id,
                        error={
                            "code": "action_dispatch_failed",
                            "message": str(exc),
                        },
                        sample_id=str(instance.get("sample_id") or ""),
                        template_id=str(instance.get("template_id") or ""),
                        device_id=node.device_name,
                        action_name=method_name,
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
                    concurrency_key=concurrency_key,
                    sample_id=str(instance.get("sample_id") or ""),
                    template_id=str(instance.get("template_id") or ""),
                    action_name=method_name,
                    plc_alarm_reader=devices.get("szlab_poly_plc"),
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
            _append_diagnostic(
                stats,
                instance={
                    "id": pending.instance_id,
                    "sample_id": pending.sample_id,
                    "template_id": pending.template_id,
                },
                code="terminal_report_failed",
                message=f"动作终态上报失败：{type(exc).__name__}：{exc}",
                node_id=pending.node_id,
                immediate=True,
                severity="error",
                category="dispatch_error",
                phase="reporting",
                execution_id=pending.execution_id,
                template_id=pending.template_id,
                device_id=pending.device_id,
                action_name=pending.action_name,
                detail={
                    "retryable": pending.retryable,
                    "terminal_error": pending.error,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            if not pending.retryable:
                stats["success"] = False
            return False
        self._pending_terminal_reports.pop(pending.execution_id, None)
        self._reported_execution_ids.add(pending.execution_id)
        stats["failed"] = int(stats["failed"]) + 1
        return True

    def _retry_pending_terminal_report(
        self,
        stats: dict[str, Any],
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
            _append_diagnostic(
                stats,
                instance={
                    "id": pending.instance_id,
                    "sample_id": pending.sample_id,
                    "template_id": pending.template_id,
                },
                code="terminal_report_retry_failed",
                message=(
                    "读取 Task 工作区失败，无法重试动作终态上报："
                    f"{type(exc).__name__}：{exc}"
                ),
                node_id=pending.node_id,
                immediate=True,
                severity="error",
                category="dispatch_error",
                phase="reporting",
                execution_id=pending.execution_id,
                template_id=pending.template_id,
                device_id=pending.device_id,
                action_name=pending.action_name,
                detail={
                    "retryable": True,
                    "terminal_error": pending.error,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
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
        stats: dict[str, Any],
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
            message = "服务端存在活动执行但本地无对应 future，无法安全恢复"
            _append_diagnostic(
                stats,
                instance=instance,
                code="orphaned_execution",
                message=message,
                node_id=node_id,
                immediate=True,
                severity="error",
                category="dispatch_error",
                phase="recovering",
                execution_id=execution_id,
                template_id=str(instance.get("template_id") or ""),
            )
            self._submit_terminal_report(
                _PendingTerminalReport(
                    workflow_path=workflow_path,
                    instance_id=str(instance.get("id")),
                    node_id=node_id,
                    execution_id=execution_id,
                    error={
                        "code": "orphaned_execution",
                        "message": message,
                    },
                    sample_id=str(instance.get("sample_id") or ""),
                    template_id=str(instance.get("template_id") or ""),
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

    def _harvest_completed(self, stats: dict[str, Any]) -> None:
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
                except Exception as exc:
                    _append_diagnostic(
                        stats,
                        instance={
                            "id": action.instance_id,
                            "sample_id": action.sample_id,
                            "template_id": action.template_id,
                        },
                        code="terminal_report_failed",
                        message=(
                            "动作成功终态上报失败："
                            f"{type(exc).__name__}：{exc}"
                        ),
                        node_id=action.node_id,
                        immediate=True,
                        severity="error",
                        category="dispatch_error",
                        phase="reporting",
                        execution_id=execution_id,
                        template_id=action.template_id,
                        device_id=action.device_name,
                        action_name=action.action_name,
                        detail={
                            "terminal_status": "succeeded",
                            "exception_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
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
                except Exception as exc:
                    _append_diagnostic(
                        stats,
                        instance={
                            "id": action.instance_id,
                            "sample_id": action.sample_id,
                            "template_id": action.template_id,
                        },
                        code="terminal_report_failed",
                        message=(
                            "动作失败终态上报失败："
                            f"{type(exc).__name__}：{exc}"
                        ),
                        node_id=action.node_id,
                        immediate=True,
                        severity="error",
                        category="dispatch_error",
                        phase="reporting",
                        execution_id=execution_id,
                        template_id=action.template_id,
                        device_id=action.device_name,
                        action_name=action.action_name,
                        detail={
                            "terminal_status": "failed",
                            "action_error": action.error,
                            "exception_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
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
                enriched = enrich_mixing_failure(
                    failure,
                    device_id=action.device_name,
                )
                active_alarms = read_active_plc_alarms(
                    action.plc_alarm_reader,
                    stations={enriched["station"]} if enriched and enriched.get("station") else None,
                )
                if active_alarms:
                    action.error = enrich_with_plc_alarm(
                        enriched or failure,
                        alarm=active_alarms[0],
                    )
                    return
                if enriched is not None:
                    action.error = enriched
                    return
                raise RuntimeError(f"动作返回 success=false: {failure}")
            action.result_summary = summary
        except Exception as exc:
            base_error = {
                "success": False,
                "code": "action_failed",
                "message": str(exc),
            }
            action.error = enrich_mixing_failure(
                base_error,
                device_id=action.device_name,
            ) or base_error
            active_alarms = read_active_plc_alarms(
                action.plc_alarm_reader,
                stations={action.error["station"]} if action.error.get("station") else None,
            )
            if active_alarms:
                action.error = enrich_with_plc_alarm(
                    action.error,
                    alarm=active_alarms[0],
                )

    def _instance_is_in_flight(self, instance_id: str) -> bool:
        return any(
            action.instance_id == instance_id
            for action in self._in_flight.values()
        )

    def _busy_concurrency_keys(self) -> set[str]:
        return {
            action.concurrency_key
            for action in self._in_flight.values()
            if action.concurrency_key
        }


def _action_concurrency_key(node: WorkflowNode) -> str:
    """S04 各磁搅位独立互斥，其他动作仍按整台设备互斥。"""
    try:
        method_name = node_method(node)
    except ValueError:
        method_name = ""
    if method_name == "run_stirring" and node.param.get("position") is not None:
        return f"{node.device_name}:position:{int(node.param['position'])}"
    return node.device_name


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
    return find_action_failure(value, _allow_bare_false=_allow_bare_false)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)
