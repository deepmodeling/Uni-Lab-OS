"""工站调度权威的嵌入式运行模块。"""

from __future__ import annotations

import os
from pathlib import Path

from unilabos.app.control_plane import (
    ControlPlaneRuntimeContext,
    ControlPlaneRuntimeHandle,
)
from unilabos.utils.banner_print import print_status


def start_embedded_scheduler_runtime(
    context: ControlPlaneRuntimeContext,
) -> ControlPlaneRuntimeHandle:
    """启动工站库存、DAG 调度器、历史存储与跨进程 Edge 执行权威。"""

    from unilabos.app.runtime_storage import prepare_runtime_storage_session
    from unilabos.app.scheduler.integration import (
        setup_edge_inventory,
        setup_edge_scheduler,
        shutdown_edge_services,
    )
    from unilabos.config.config import BasicConfig, EdgeControlConfig
    from unilabos.registry.template_snapshot import RegistryTemplateSnapshot

    if BasicConfig.process_role != "workspace_backend":
        raise ValueError("只有 workspace_backend 进程可以装配工站调度服务")

    arguments = context.arguments
    paths = prepare_runtime_storage_session(
        arguments,
        working_dir=context.working_dir,
    )

    inventory_db = os.path.abspath(os.path.expanduser(paths.inventory_db))
    setup_edge_inventory(
        inventory_db,
        ws_client=None,
        resource_tree_set=context.resource_tree_set,
        registry_snapshot=RegistryTemplateSnapshot.from_registry(context.registry),
        resource_graph_source_id=context.graph_source_id,
        material_shapes=context.material_shapes,
        material_model_catalog=context.material_model_catalog,
        # 完整工作区还有领域工作流源码固定点；库存资源图先落事实，设备目录由
        # Workflow 组合根的 before_publish 屏障统一激活，避免半代对外可见。
        defer_runtime_device_catalog_activation=(
            BasicConfig.workflow_source_discovery_plan is not None
        ),
    )
    print_status(
        f"工站物料服务已启用 (数据库类型: {BasicConfig.database_type})",
        "info",
    )

    from unilabos.app.edge_control.local_authority import (
        LocalEdgeAuthorityStore,
        LocalEdgeControlAuthority,
    )

    execution_backend = LocalEdgeControlAuthority(
        LocalEdgeAuthorityStore(
            Path(paths.workflow_history_db).with_name("edge_authority.db")
        ),
        api_key=str(EdgeControlConfig.api_key or "").strip(),
    )

    _scheduler, execution_backend = setup_edge_scheduler(
        ws_client=None,
        inventory_db_path=inventory_db,
        device_state_db_path=paths.device_state_db,
        workflow_history_db_path=paths.workflow_history_db,
        execution_backend=execution_backend,
    )
    print_status(
        "工站调度器已启用 (DAG 调度 + 设备状态 + 工作流历史)",
        "info",
    )

    return ControlPlaneRuntimeHandle(
        bridges=(),
        communication_clients=(),
        shutdown_services=shutdown_edge_services,
    )


__all__ = ["start_embedded_scheduler_runtime"]
