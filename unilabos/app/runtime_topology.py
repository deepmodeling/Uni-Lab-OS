"""Resolve the OS process composition independently from domain authority."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class ControlPlaneMode(str, Enum):
    """工作流、物料与任务事实的权威位置。"""

    LOCAL = "local"
    BACKEND = "backend"


class RuntimeProcessRole(str, Enum):
    """一个 OS 进程在 Workbench 运行拓扑中的职责。"""

    WORKSPACE_BACKEND = "workspace_backend"
    EDGE_RUNTIME = "edge_runtime"


@dataclass(frozen=True)
class RuntimeProcessPlan:
    """启动组合根消费的完整、已验证进程计划。"""

    role: RuntimeProcessRole
    control_plane: ControlPlaneMode
    starts_web_server: bool
    initializes_host_devices: bool


def resolve_runtime_process_plan(arguments: dict[str, Any]) -> RuntimeProcessPlan:
    """解析本地进程角色，并拒绝已经移除的 Backend 控制面。

    参数：``arguments`` 是命令行参数投影；返回：只使用本地事实源的进程计划。
    异常：传入 ``backend`` 或未知控制面时抛出 ``ValueError``，不会启动任何进程。
    状态不变量：Scheduler 与 Edge 必须独立运行，控制面固定为 ``local``；
    保留 ``ControlPlaneMode.BACKEND`` 枚举只用于读取旧代码，不能再由
    运行时计划激活。
    """

    requested_control_plane = str(
        arguments.get("control_plane") or ControlPlaneMode.LOCAL.value
    )
    if requested_control_plane != ControlPlaneMode.LOCAL.value:
        raise ValueError("当前 OS 仅支持 local 控制面，不再支持 backend 模式")
    control_plane = ControlPlaneMode.LOCAL
    try:
        role = RuntimeProcessRole(
            str(arguments.get("process_role") or RuntimeProcessRole.WORKSPACE_BACKEND.value)
        )
    except ValueError as error:
        raise ValueError(
            "process_role 必须是 workspace_backend 或 edge_runtime；"
            "完整工站请使用 unilab workspace start 启动"
        ) from error

    from unilabos.simulation.configuration import validate_simulation_environment

    validate_simulation_environment()
    is_slave = bool(arguments.get("is_slave", False))
    bridges = {str(value) for value in arguments.get("app_bridges") or ()}
    if "websocket" in bridges:
        raise ValueError("当前 OS 已移除云端 websocket bridge，请使用 fastapi 或 edge_control")

    if role is RuntimeProcessRole.WORKSPACE_BACKEND:
        if is_slave:
            raise ValueError("workspace_backend 进程不能使用 --is_slave")
    elif role is RuntimeProcessRole.EDGE_RUNTIME:
        if is_slave:
            raise ValueError(
                "local Authority 的 edge_runtime 直接拥有设备，不能使用 --is_slave"
            )

    if "edge_control" in bridges and role is not RuntimeProcessRole.EDGE_RUNTIME:
        raise ValueError("local Authority 仅允许 edge_runtime 使用 edge_control bridge")
    if role is RuntimeProcessRole.EDGE_RUNTIME and "edge_control" not in bridges:
        raise ValueError("local Authority 的 edge_runtime 必须启用 edge_control bridge")

    return RuntimeProcessPlan(
        role=role,
        control_plane=control_plane,
        starts_web_server=role is not RuntimeProcessRole.EDGE_RUNTIME,
        initializes_host_devices=role is RuntimeProcessRole.EDGE_RUNTIME,
    )


def publish_edge_runtime_ready_signal(path: str | None = None) -> None:
    """Atomically publish that the Edge device initialization phase completed."""

    target_value = path or os.environ.get("UNILABOS_EDGE_READY_FILE")
    if not target_value:
        return
    target = Path(target_value)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"schemaVersion": 1, "pid": os.getpid()}) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


__all__ = [
    "ControlPlaneMode",
    "RuntimeProcessPlan",
    "RuntimeProcessRole",
    "publish_edge_runtime_ready_signal",
    "resolve_runtime_process_plan",
]
