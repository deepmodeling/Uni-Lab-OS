"""工作流类型（Workflow Type）的公共领域约束。"""

from __future__ import annotations

from typing import Any, Literal, cast

WORKFLOW_TYPE_NORMAL = "normal"
WORKFLOW_TYPE_EXPERIMENT_OPERATION = "experiment_operation"
WORKFLOW_TYPES = frozenset(
    {
        WORKFLOW_TYPE_NORMAL,
        WORKFLOW_TYPE_EXPERIMENT_OPERATION,
    }
)

WorkflowType = Literal["normal", "experiment_operation"]


def normalize_workflow_type(
    value: Any,
    *,
    default: WorkflowType = WORKFLOW_TYPE_NORMAL,
) -> WorkflowType:
    """规范工作流类型并拒绝未知值。

    参数：``value`` 是 HTTP、领域源码或目录投影提供的原始值；``default`` 用于
    兼容尚未声明类型的旧工作流。返回：``normal`` 或
    ``experiment_operation``。异常：非字符串或未知字符串抛出
    ``ValueError``，禁止把拼写错误静默归为普通流程。
    """

    candidate = default if value is None else value
    if not isinstance(candidate, str) or candidate not in WORKFLOW_TYPES:
        raise ValueError("workflow_type must be normal or experiment_operation")
    return cast(WorkflowType, candidate)
