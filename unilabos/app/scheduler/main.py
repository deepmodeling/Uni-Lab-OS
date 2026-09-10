"""Edge scheduler 独立进程入口。

    python -m unilabos.app.scheduler.main
    # 或
    uvicorn "unilabos.app.scheduler.main:app" --port 8092

环境变量：

    ULAB_SCHEDULER_HOST      默认 127.0.0.1
    ULAB_SCHEDULER_PORT      默认 8092
    ULAB_LAB_ID              本地实验室身份，默认 edge-lab
    ULAB_INVENTORY_DB        Edge 仓储 SQLite 路径（如 ~/.unilabos/inventory.db）；
                             设置后启用仓储路由并接入调度器物料预留
    ULAB_DEVICE_STATE_DB     设备状态 SQLite 路径（默认 ~/.unilabos/device_state.db，
                             与仓储/工作流库分开；设为 "off" 关闭落盘）
    ULAB_WORKFLOW_HISTORY_DB 工作流执行历史 SQLite 路径（默认
                             ~/.unilabos/workflow_history.db；"off" 关闭）
    ULAB_ESTIMATE_MODE       时长预估模式：declared / historical / auto（默认 auto）
    ULAB_ESTIMATE_DEFAULT_S  预估兜底默认时长（秒），默认 60
"""

from __future__ import annotations

import logging
import os

from unilabos.app.scheduler.api import create_app
from unilabos.app.scheduler.estimation import DurationEstimator
from unilabos.app.scheduler.monitor import monitor_bus
from unilabos.app.scheduler.ordering import StableLocalOrderer
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.utils.tracing import initialize_tracing


def build_estimator() -> DurationEstimator:
    return DurationEstimator(
        mode=os.environ.get("ULAB_ESTIMATE_MODE", "auto").strip() or "auto",
        default_s=float(os.environ.get("ULAB_ESTIMATE_DEFAULT_S", "60")),
    )


def _build_device_state():
    db_path = os.environ.get("ULAB_DEVICE_STATE_DB", "~/.unilabos/device_state.db").strip()
    if not db_path or db_path.lower() == "off":
        return None
    from unilabos.app.scheduler.device_state import DeviceStateStore

    db_path = os.path.abspath(os.path.expanduser(db_path))
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    return DeviceStateStore(db_path)


def _build_history():
    db_path = os.environ.get("ULAB_WORKFLOW_HISTORY_DB", "~/.unilabos/workflow_history.db").strip()
    if not db_path or db_path.lower() == "off":
        return None
    from unilabos.app.scheduler.history import WorkflowHistoryStore

    db_path = os.path.abspath(os.path.expanduser(db_path))
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
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

    db_path = os.path.abspath(os.path.expanduser(db_path))
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    return InventoryService(
        InventoryStore(db_path),
        edge_id=os.environ.get("ULAB_EDGE_ID", "edge-default"),
        lab_id=os.environ.get("ULAB_LAB_ID", "edge-lab"),
        monitor=monitor_bus,
    )


def build_scheduler(
    inventory=None, history=None, material_replenishment_factory=None
) -> EdgeScheduler:
    """装配固定使用本地稳定排序的 OS 调度器（Scheduler）。

    参数：``inventory`` 是可选库存权威（Inventory Authority），``history`` 是
    可选工作流（Workflow）历史存储。返回：不访问外部排序服务的调度器实例。
    异常：预估器或调度器初始化错误原样传播。
    """

    # ``estimator`` 由排序展示与调度器共享，历史样本只积累一份。
    estimator = build_estimator()
    return EdgeScheduler(
        orderer=StableLocalOrderer(),
        inventory=inventory,
        estimator=estimator,
        monitor=monitor_bus,
        history=history,
        material_replenishment_factory=material_replenishment_factory,
    )


initialize_tracing()
_inventory = _build_inventory()
_history = _build_history()
app = create_app(
    build_scheduler(inventory=_inventory, history=_history),
    device_state=_build_device_state(),
    history=_history,
    include_execution_shaped_workflow_routes=False,
)

_workflow_history_path = os.environ.get(
    "ULAB_WORKFLOW_HISTORY_DB", "~/.unilabos/workflow_history.db"
).strip()
if _workflow_history_path and _workflow_history_path.lower() != "off":
    from unilabos.app.workflow_api import install_workflow_api
    from unilabos.workflow.service import WorkflowService
    from unilabos.workflow.store import WorkflowStore

    install_workflow_api(
        app,
        WorkflowService(
            WorkflowStore(os.path.abspath(os.path.expanduser(_workflow_history_path)))
        ),
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
    from unilabos.app.scheduler.inventory.layout import (
        create_lab_router as _create_lab_router,
    )

    install_backend_resource_api(app, BackendResourceService(_inventory.store))
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
