"""组装工站调度器与跨进程 Edge 执行权威。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unilabos.app.edge_control.local_authority import LocalEdgeControlAuthority
    from unilabos.app.scheduler.service import EdgeScheduler


def create_edge_stack(
    *,
    execution_backend: LocalEdgeControlAuthority,
    orderer: Any = None,
    inventory: Any = None,
    estimator: Any = None,
    monitor: Any = None,
    history: Any = None,
    observations: Any = None,
    observation_sources: Any = None,
    serial_queue: bool = False,
) -> tuple[EdgeScheduler, LocalEdgeControlAuthority]:
    """绑定调度、持久结果回报和执行进程生命周期。

    参数：``execution_backend`` 必须由工站后端显式提供；其余参数为共享的
    调度依赖。返回已启动的调度器与原执行权威。缺少执行权威时立即失败，
    不加载 HostNode 或设备驱动，也不提供进程内派发回退。
    """
    from unilabos.app.scheduler.service import EdgeScheduler

    if execution_backend is None:
        raise ValueError("工站调度器必须提供跨进程 Edge 执行权威")
    backend = execution_backend
    dispatcher = backend
    if observations is not None:
        from unilabos.simulation.dispatch import SourceDispatcher

        dispatcher = SourceDispatcher(backend, observations, observation_sources)
    device_target_resolver = None
    station_resources = (
        getattr(inventory, "station_resources", None)
        if inventory is not None
        else None
    )
    registration_reader = getattr(
        getattr(backend, "store", None),
        "latest_registration",
        None,
    )
    if station_resources is not None and callable(registration_reader):
        from unilabos.app.scheduler.device_target import (
            make_registered_device_target_resolver,
        )

        device_target_resolver = make_registered_device_target_resolver(
            station_resources,
            registration_reader,
        )
    scheduler = EdgeScheduler(
        orderer=orderer,
        dispatcher=dispatcher,
        busy_key_provider=backend.busy_device_action_keys,
        inventory=inventory,
        station_resources=station_resources,
        device_target_resolver=device_target_resolver,
        estimator=estimator,
        monitor=monitor,
        history=history,
        serial_queue=serial_queue,
    )
    backend.add_job_outcome_listener(scheduler.on_job_outcome)
    backend.add_job_feedback_listener(scheduler.on_job_feedback)
    backend.add_execution_process_restarted_listener(scheduler.on_execution_process_restarted)
    backend.add_execution_connection_lost_listener(scheduler.on_execution_connection_lost)
    backend.start()
    return scheduler, backend


__all__ = ["create_edge_stack"]
