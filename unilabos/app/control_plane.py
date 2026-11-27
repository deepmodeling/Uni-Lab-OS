"""选择 Uni-Lab OS 本地控制面的启动 seam。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from unilabos.app.runtime_topology import (
    ControlPlaneMode,
    RuntimeProcessRole,
    resolve_runtime_process_plan,
)
from unilabos.config.config import BasicConfig


@dataclass(frozen=True)
class ControlPlaneRuntimeContext:
    """两个控制面 adapter 共用的冻结启动输入。"""

    arguments: dict[str, Any]
    working_dir: str
    resource_tree_set: Any
    registry: Any
    graph_source_id: str
    material_shapes: Any
    material_model_catalog: Any


@dataclass(frozen=True)
class ControlPlaneRuntimeHandle:
    """控制面 adapter 向 HostNode 和进程生命周期公开的最小接口。"""

    bridges: tuple[Any, ...]
    communication_clients: tuple[Any, ...]
    shutdown_services: Callable[[], None]


def validate_control_plane_arguments(
    arguments: dict[str, Any],
) -> ControlPlaneMode:
    """验证本地控制面模式及 bridge 组合。

    参数：``arguments`` 是命令行参数投影。返回：固定为
    ``ControlPlaneMode.LOCAL``。异常：传入已移除的 ``backend`` 或非法 bridge
    组合时抛出 ``ValueError``；不会启动进程。
    """

    return resolve_runtime_process_plan(arguments).control_plane


def should_mount_embedded_scheduler_routes() -> bool:
    """判断当前 Web 进程是否拥有工站调度接口。"""

    return BasicConfig.process_role == RuntimeProcessRole.WORKSPACE_BACKEND.value


def should_mount_workspace_authoring_routes() -> bool:
    """判断当前进程是否挂载本地工作流创作接口。

    参数：无。返回：``workspace_backend`` 进程为 ``True``，Edge
    Runtime 为 ``False``。异常：无；该判断不再区分远端控制面。
    """

    return BasicConfig.process_role == RuntimeProcessRole.WORKSPACE_BACKEND.value


def start_control_plane_runtime(
    context: ControlPlaneRuntimeContext,
) -> ControlPlaneRuntimeHandle:
    """按进程职责启动本地工站调度运行时。

    参数：``context`` 是冻结的进程启动输入。返回：本地 Scheduler/Edge 运行时句
    柄。异常：控制面不是 ``local`` 时抛出 ``ValueError``，不会创建远端协议客户
    端；组件启动失败沿用对应运行时的异常。状态不变量：所有物料、工作流和任务
    事实都由当前 OS 持有。
    """

    plan = resolve_runtime_process_plan(context.arguments)
    if plan.role is RuntimeProcessRole.WORKSPACE_BACKEND:
        from unilabos.app.scheduler.runtime import start_embedded_scheduler_runtime

        return start_embedded_scheduler_runtime(context)
    raise ValueError("只有 workspace_backend 进程可以启动工站调度服务")


__all__ = [
    "ControlPlaneMode",
    "RuntimeProcessRole",
    "ControlPlaneRuntimeContext",
    "ControlPlaneRuntimeHandle",
    "should_mount_embedded_scheduler_routes",
    "should_mount_workspace_authoring_routes",
    "start_control_plane_runtime",
    "validate_control_plane_arguments",
]
