"""领域包工作流源码（Workflow Source）的目录布局合同。"""

from __future__ import annotations

from unilabos.workflow.workflow_type import (
    WORKFLOW_TYPE_EXPERIMENT_OPERATION,
    WORKFLOW_TYPE_NORMAL,
    WorkflowType,
    normalize_workflow_type,
)

NORMAL_WORKFLOW_SOURCE_DIRECTORY = "workflows"
EXPERIMENT_OPERATION_SOURCE_DIRECTORY = "experiment_operations"
WORKFLOW_SOURCE_DIRECTORIES = frozenset(
    {
        NORMAL_WORKFLOW_SOURCE_DIRECTORY,
        EXPERIMENT_OPERATION_SOURCE_DIRECTORY,
    }
)


def workflow_source_directory(workflow_type: str | None) -> str:
    """返回新工作流源码应写入的领域包一级目录。

    参数：``workflow_type`` 是 HTTP 或 AST 已规范化前的工作流类型。返回：普通
    工作流使用既有 ``workflows``，实验操作使用 ``experiment_operations``。
    异常：未知类型沿用 ``normalize_workflow_type`` 的 ``ValueError``，禁止落入
    错误目录。
    """

    normalized_type: WorkflowType = normalize_workflow_type(workflow_type)
    if normalized_type == WORKFLOW_TYPE_EXPERIMENT_OPERATION:
        return EXPERIMENT_OPERATION_SOURCE_DIRECTORY
    assert normalized_type == WORKFLOW_TYPE_NORMAL
    return NORMAL_WORKFLOW_SOURCE_DIRECTORY


def is_workflow_source_directory(value: object) -> bool:
    """判断路径首段是否属于受支持的工作流源码目录。

    参数：``value`` 是 manifest 或持久注册给出的路径首段。返回：仅既有普通
    工作流目录和实验操作目录返回 ``True``；本函数不访问文件系统且不抛出异常。
    """

    return isinstance(value, str) and value in WORKFLOW_SOURCE_DIRECTORIES


__all__ = [
    "EXPERIMENT_OPERATION_SOURCE_DIRECTORY",
    "NORMAL_WORKFLOW_SOURCE_DIRECTORY",
    "WORKFLOW_SOURCE_DIRECTORIES",
    "is_workflow_source_directory",
    "workflow_source_directory",
]
