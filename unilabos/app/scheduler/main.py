"""Edge scheduler 独立进程入口。

    python -m unilabos.app.scheduler.main
    # 或
    uvicorn "unilabos.app.scheduler.main:app" --port 8092

环境变量：

    ULAB_SCHEDULER_HOST      默认 127.0.0.1
    ULAB_SCHEDULER_PORT      默认 8092
    PUBCHEM_ADDR              PubChem PUG REST 地址，默认
                              https://pubchem.ncbi.nlm.nih.gov；设为 off 关闭
    PUBCHEM_TIMEOUT           PubChem 请求超时（秒或带 s 后缀），默认 30s
    ULAB_LAB_ID              本地实验室身份，默认 edge-lab
    ULAB_INVENTORY_DB        Edge 仓储 SQLite 路径（如 ~/.unilabos/inventory.db）；
                             设置后启用仓储路由并接入调度器物料预留
    ULAB_DEVICE_STATE_DB     设备状态 SQLite 路径（默认 ~/.unilabos/device_state.db，
                             与仓储/工作流库分开；设为 "off" 关闭落盘）
    ULAB_WORKFLOW_HISTORY_DB 工作流执行历史 SQLite 路径（默认
                             ~/.unilabos/workflow_history.db；"off" 关闭）
    ULAB_ESTIMATE_MODE       时长预估模式：declared / historical / auto（默认 auto）
    ULAB_ESTIMATE_DEFAULT_S  预估兜底默认时长（秒），默认 60
    ULAB_SCHEDULER_MAX_INFLIGHT_JOBS
                             全局在途作业容量，默认 100
    ULAB_SCHEDULER_MAX_ACTIVE_TASKS
                             全局运行任务容量，默认 500
    ULAB_SCHEDULER_MAX_TASKS_PER_WORKFLOW
                             同一工作流定义的运行任务容量，默认 100
    ULAB_SCHEDULER_AGING_INTERVAL_SECONDS
                             待派发候选每增加一级有效优先级的等待秒数，默认 30
"""

from __future__ import annotations

import logging
import os
from typing import Any

from unilabos.app.scheduler.api import create_app
from unilabos.app.scheduler.estimation import DurationEstimator
from unilabos.app.scheduler.monitor import monitor_bus
from unilabos.app.scheduler.ordering import StableLocalOrderer
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.utils.tracing import initialize_tracing


def build_estimator() -> DurationEstimator:
    from unilabos.app.scheduler.timing_config import load_timing_rules
    estimator = DurationEstimator(
        mode=os.environ.get("ULAB_ESTIMATE_MODE", "auto").strip() or "auto",
        default_s=float(os.environ.get("ULAB_ESTIMATE_DEFAULT_S", "60")),
    )
    load_timing_rules(estimator)
    return estimator


def _positive_env_int(name: str, default: int) -> int:
    """读取正整数环境变量；非法配置在进程启动时立即失败。"""

    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} 必须是正整数") from error
    if value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


def _build_device_state():
    db_path = os.environ.get("ULAB_DEVICE_STATE_DB", "~/.unilabos/device_state.db").strip()
    if not db_path or db_path.lower() == "off":
        return None
    from unilabos.app.scheduler.device_state import DeviceStateStore
    from unilabos.persistence.database import prepare_database_path

    db_path = prepare_database_path(db_path, create_parent=True)
    return DeviceStateStore(db_path)


def _build_history():
    db_path = os.environ.get("ULAB_WORKFLOW_HISTORY_DB", "~/.unilabos/workflow_history.db").strip()
    if not db_path or db_path.lower() == "off":
        return None
    from unilabos.app.scheduler.history import WorkflowHistoryStore
    from unilabos.persistence.database import prepare_database_path

    db_path = prepare_database_path(db_path, create_parent=True)
    store = WorkflowHistoryStore(db_path)
    interrupted = store.mark_interrupted()
    if interrupted:
        logging.getLogger(__name__).info(
            "[EdgeScheduler] marked %d stale workflow runs as interrupted", interrupted
        )
    return store


def _build_inventory():
    db_path = os.environ.get("ULAB_INVENTORY_DB", "").strip()
    if not db_path:
        return None
    from unilabos.app.scheduler.inventory.service import InventoryService
    from unilabos.app.scheduler.inventory.store import InventoryStore
    from unilabos.persistence.database import prepare_database_path

    db_path = prepare_database_path(db_path, create_parent=True)
    return InventoryService(
        InventoryStore(db_path),
        edge_id=os.environ.get("ULAB_EDGE_ID", "edge-default"),
        lab_id=os.environ.get("ULAB_LAB_ID", "edge-lab"),
        monitor=monitor_bus,
    )


def build_scheduler(inventory=None, history=None) -> EdgeScheduler:
    """装配固定使用本地稳定排序的 OS 调度器（Scheduler）。

    参数：``inventory`` 是可选库存权威（Inventory Authority），``history`` 是
    可选工作流（Workflow）历史存储。返回：不访问外部排序服务的调度器实例。
    异常：预估器或调度器初始化错误原样传播。
    """

    # ``estimator`` 由排序展示与调度器共享，历史样本只积累一份。
    estimator = build_estimator()
    return EdgeScheduler(
        orderer=StableLocalOrderer(
            aging_interval_seconds=float(
                os.environ.get("ULAB_SCHEDULER_AGING_INTERVAL_SECONDS", "30")
            )
        ),
        inventory=inventory,
        station_resources=(
            inventory.station_resources if inventory is not None else None
        ),
        estimator=estimator,
        serial_queue=True,
        monitor=monitor_bus,
        history=history,
        max_in_flight_jobs=_positive_env_int(
            "ULAB_SCHEDULER_MAX_INFLIGHT_JOBS",
            100,
        ),
        max_active_tasks=_positive_env_int(
            "ULAB_SCHEDULER_MAX_ACTIVE_TASKS",
            500,
        ),
        max_tasks_per_workflow=_positive_env_int(
            "ULAB_SCHEDULER_MAX_TASKS_PER_WORKFLOW",
            100,
        ),
    )


def build_workflow_service(
    scheduler: EdgeScheduler,
    database_path: str,
) -> tuple[Any, Any]:
    """在指定现有数据库上装配工作流服务与任务调度桥。

    参数：``scheduler`` 是本进程唯一调度器（Scheduler）；``database_path`` 是
    已配置的工作流 SQLite 文件。返回：共享同一 ``WorkflowStore`` 的服务与桥，
    供进程生命周期持有。异常：Schema 初始化或安全恢复失败原样传播；恢复只投递
    已提交事实，不会盲目重放结果未知的设备作业。
    """

    from unilabos.workflow.service import WorkflowService
    from unilabos.workflow.store import WorkflowStore
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    workflow_store = WorkflowStore(
        database_path,
        persist_workflow_definitions=False,
    )
    workflow_definitions = WorkflowStore(":memory:")
    bridge = TaskSchedulerBridge(workflow_store, scheduler=scheduler,
                                 time_scheduling_enabled=scheduler.time_scheduling_enabled)
    try:
        service = WorkflowService(
            workflow_store,
            definition_store=workflow_definitions,
            device_preflight=scheduler.preflight_device_target,
            task_scheduler_bridge=bridge,
        )
        bridge.recover_active_tasks(
            execution_process_restarted=False
        )
    except BaseException:
        bridge.close()
        workflow_store.close()
        workflow_definitions.close()
        raise
    return service, bridge


from unilabos.persistence.config import resolve_database_settings, verify_database_connection

_database_settings = resolve_database_settings({
    "database_type": os.environ.get("UNILABOS_BASICCONFIG_DATABASE_TYPE"),
    "database_url": os.environ.get("UNILABOS_BASICCONFIG_DATABASE_URL"),
})
verify_database_connection(_database_settings)
initialize_tracing()
_inventory = _build_inventory()
_history = _build_history()
_scheduler = build_scheduler(inventory=_inventory, history=_history)
app = create_app(
    _scheduler,
    device_state=_build_device_state(),
    history=_history,
    include_execution_shaped_workflow_routes=False,
)

_workflow_history_path = os.environ.get(
    "ULAB_WORKFLOW_HISTORY_DB", "~/.unilabos/workflow_history.db"
).strip()
if _workflow_history_path and _workflow_history_path.lower() != "off":
    from unilabos.app.workflow_api import install_workflow_api
    from unilabos.persistence.database import prepare_database_path

    _workflow_service, _workflow_bridge = build_workflow_service(
        _scheduler,
        prepare_database_path(_workflow_history_path, create_parent=True),
    )
    install_workflow_api(
        app,
        _workflow_service,
    )
if _inventory is not None:
    from unilabos.app.scheduler.inventory.api import (
        create_legacy_material_router as _create_legacy_material_router,
    )
    from unilabos.app.scheduler.inventory.api import (
        create_router as _create_inventory_router,
    )
    from unilabos.app.scheduler.inventory.backend_api import (
        install_backend_resource_api,
    )
    from unilabos.app.scheduler.inventory.backend_contract import (
        BackendResourceService,
    )
    from unilabos.app.scheduler.inventory.compound_source import configured_compound_source
    from unilabos.app.scheduler.inventory.layout import (
        create_lab_router as _create_lab_router,
    )

    install_backend_resource_api(
        app,
        BackendResourceService(
            _inventory.store,
            edge_id=_inventory.edge_id,
            lab_id=_inventory.lab_id,
            monitor=monitor_bus,
        ),
        compound_source=configured_compound_source(),
    )
    app.include_router(_create_inventory_router(_inventory))
    app.include_router(_create_legacy_material_router(_inventory))
    app.include_router(_create_lab_router(_inventory))


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        app,
        host=os.environ.get("ULAB_SCHEDULER_HOST", "127.0.0.1"),
        port=int(os.environ.get("ULAB_SCHEDULER_PORT", "8092")),
    )


if __name__ == "__main__":
    main()
