"""微后端三个业务 SQLite 的表归属清单。

本模块只声明所有权，不负责建表。迁移实现仍由各领域 store 独立拥有；组合根
可用这里的清单做启动诊断、文档生成和跨库写入审计。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DatabaseTableContract:
    """单个数据库的稳定文件名、权威职责和物理对象。"""

    key: str
    filename: str
    authority: str
    tables: tuple[str, ...]
    compatibility_views: tuple[str, ...] = ()


INVENTORY_DATABASE = DatabaseTableContract(
    key="inventory",
    filename="inventory.db",
    authority="资源模板、物料、Site、库存账本、库位与同步游标",
    tables=(
        "resource_template",
        "resource_handle_template",
        "resource_template_inventory",
        "material",
        "relative_position",
        "material_inventory",
        "material_content_version",
        "site",
        "material_state_history",
        "inventory_lot",
        "inventory_reservation",
        "inventory_ledger",
        "sync_outbox",
        "processed_command",
        "sync_cursor",
        "lab_meta",
        "lab_zone",
        "lab_placement",
    ),
    compatibility_views=(
        "inventory_resource_template",
        "material_instance",
        "resource_relation",
        "substance_content",
    ),
)

DEVICE_STATE_DATABASE = DatabaseTableContract(
    key="device_state",
    filename="device_state.db",
    authority="设备属性最新值与有界遥测历史",
    tables=(
        "device_property_latest",
        "device_property_history",
    ),
)

WORKFLOW_DATABASE = DatabaseTableContract(
    key="workflow",
    filename="workflow_history.db",
    authority="工作流定义、执行任务、节点作业与人工干预",
    tables=(
        "workflow",
        "workflow_node_template",
        "workflow_handle_template",
        "workflow_node",
        "workflow_edge",
        "workflow_task",
        "workflow_node_job",
        "workflow_task_command",
        "execution_lock_lease",
        "workflow_node_job_result",
        "workflow_node_job_feedback_history",
        "workflow_intervention",
        "workflow_manual_confirmation",
        "workflow_source_registration",
        "workflow_authoring",
        "frontend_event",
    ),
    compatibility_views=("workflow_runs", "job_runs"),
)

MICROBACKEND_DATABASES: tuple[DatabaseTableContract, ...] = (
    INVENTORY_DATABASE,
    DEVICE_STATE_DATABASE,
    WORKFLOW_DATABASE,
)

# 后端受控模式的可靠投递库不属于三个领域业务库；单独列出，防止被误合并。
EDGE_CONTROL_TABLES: tuple[str, ...] = (
    "edge_control_meta",
    "edge_command",
    "edge_event_outbox",
    "edge_job_runtime",
    "edge_job_outcome_pending",
)


def table_owner(table_name: str) -> str | None:
    """返回领域表/兼容视图的唯一数据库 key；未知对象返回 ``None``。"""

    matches = [
        contract.key
        for contract in MICROBACKEND_DATABASES
        if table_name in contract.tables or table_name in contract.compatibility_views
    ]
    if len(matches) > 1:
        raise RuntimeError(f"数据库表 {table_name!r} 存在多个所有者: {matches}")
    return matches[0] if matches else None


__all__ = [
    "DatabaseTableContract",
    "DEVICE_STATE_DATABASE",
    "EDGE_CONTROL_TABLES",
    "INVENTORY_DATABASE",
    "MICROBACKEND_DATABASES",
    "WORKFLOW_DATABASE",
    "table_owner",
]
