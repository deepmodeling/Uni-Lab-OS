"""Edge scheduler 独立进程入口。

    python -m unilabos.app.scheduler.main
    # 或
    uvicorn "unilabos.app.scheduler.main:app" --port 8092

环境变量：

    ULAB_SCHEDULER_HOST      默认 127.0.0.1
    ULAB_SCHEDULER_PORT      默认 8092
    ULAB_ORDERING_URL        uni-lab-scheduler 地址（如 http://127.0.0.1:8091）；
                             不设则用本地稳定排序 stub
    ULAB_ORDERING_ALGORITHM  远端排序算法名，默认 WeightedCriticalPath
    ULAB_LAB_ID              提交远端排序时的 lab_id，默认 edge-lab
    ULAB_INVENTORY_DB        Edge 仓储 SQLite 路径（默认 ~/.unilabos/inventory.db）；
                             设为 off 才关闭仓储与 Resource Provider
    ULAB_DEVICE_STATE_DB     设备状态 SQLite 路径（默认 ~/.unilabos/device_state.db，
                             与仓储/工作流库分开；设为 "off" 关闭落盘）
    ULAB_WORKFLOW_HISTORY_DB Workflow Authority SQLite 路径（默认
                             ~/.unilabos/workflow_history.db）
    ULAB_ESTIMATE_MODE       时长预估模式：declared / historical / auto（默认 auto）
    ULAB_ESTIMATE_DEFAULT_S  预估兜底默认时长（秒），默认 60
"""

from __future__ import annotations

import logging
import os

from unilabos.app.scheduler.api import create_app
from unilabos.app.scheduler.estimation import DurationEstimator
from unilabos.app.scheduler.history import WorkflowHistoryStore
from unilabos.app.scheduler.monitor import monitor_bus
from unilabos.app.scheduler.ordering import HttpSchedulerOrderer, StableLocalOrderer
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import install_workflow_api
from unilabos.storage.paths import RuntimeStoragePaths
from unilabos.storage.profiles import select_scheduler_authority_profile
from unilabos.utils.tracing import initialize_tracing
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


def build_estimator() -> DurationEstimator:
    return DurationEstimator(
        mode=os.environ.get("ULAB_ESTIMATE_MODE", "auto").strip() or "auto",
        default_s=float(os.environ.get("ULAB_ESTIMATE_DEFAULT_S", "60")),
    )


def _build_device_state(storage_paths: RuntimeStoragePaths):
    """按统一路径创建设备状态投影；该存储关闭时返回 ``None``。"""

    db_path = storage_paths.device_state_db
    if db_path is None:
        return None
    from unilabos.app.scheduler.device_state import DeviceStateStore

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return DeviceStateStore(str(db_path))


def _build_inventory(storage_paths: RuntimeStoragePaths):
    """按统一路径创建本地库存权威服务；该存储关闭时返回 ``None``。"""

    db_path = storage_paths.inventory_db
    if db_path is None:
        return None
    from unilabos.app.scheduler.inventory.service import InventoryService
    from unilabos.app.scheduler.inventory.store import InventoryStore

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return InventoryService(
        InventoryStore(str(db_path)),
        edge_id=os.environ.get("ULAB_EDGE_ID", "edge-default"),
        lab_id=os.environ.get("ULAB_LAB_ID", "edge-lab"),
        monitor=monitor_bus,
    )


def build_scheduler(inventory=None, history=None) -> EdgeScheduler:
    # estimator 与 orderer 共享：历史样本一处积累，排序与泳道图口径一致
    estimator = build_estimator()
    ordering_url = os.environ.get("ULAB_ORDERING_URL", "").strip()
    if ordering_url:
        orderer = HttpSchedulerOrderer(
            base_url=ordering_url,
            lab_id=os.environ.get("ULAB_LAB_ID", "edge-lab"),
            algorithm=os.environ.get("ULAB_ORDERING_ALGORITHM", "WeightedCriticalPath"),
            estimator=estimator,
        )
    else:
        orderer = StableLocalOrderer()
    return EdgeScheduler(
        orderer=orderer,
        inventory=inventory,
        estimator=estimator,
        monitor=monitor_bus,
        history=history,
    )


initialize_tracing()
_storage_paths = RuntimeStoragePaths.resolve(
    {
        "working_dir": os.environ.get("ULAB_WORKING_DIR", "~/.unilabos"),
        "edge_inventory_db": os.environ.get(
            "ULAB_INVENTORY_DB", "~/.unilabos/inventory.db"
        ),
        "edge_device_state_db": os.environ.get(
            "ULAB_DEVICE_STATE_DB", "~/.unilabos/device_state.db"
        ),
        "edge_workflow_history_db": os.environ.get(
            "ULAB_WORKFLOW_HISTORY_DB", "~/.unilabos/workflow_history.db"
        ),
    }
)
_authority_profile = select_scheduler_authority_profile(
    os.environ.get("ULAB_SCHEDULER_AUTHORITY_PROFILE", "local_scheduler"),
    edge_control_enabled=False,
)
_inventory = _build_inventory(_storage_paths)
_workflow_service = WorkflowService(
    WorkflowStore(_storage_paths.workflow_db),
    authority_profile=_authority_profile,
)
_history = WorkflowHistoryStore(
    str(_storage_paths.workflow_db),
    read_only=True,
)
app = create_app(
    build_scheduler(inventory=_inventory, history=None),
    device_state=_build_device_state(_storage_paths),
    history=_history,
    include_execution_shaped_workflow_routes=False,
)
install_workflow_api(app, _workflow_service)
if _inventory is not None:
    from unilabos.app.scheduler.inventory.backend_api import (
        install_backend_resource_api,
    )
    from unilabos.app.scheduler.inventory.backend_contract import (
        BackendResourceService,
    )
    from unilabos.app.scheduler.inventory.api import (
        create_legacy_material_router as _create_legacy_material_router,
        create_router as _create_inventory_router,
    )
    from unilabos.app.scheduler.inventory.layout import create_lab_router as _create_lab_router

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
