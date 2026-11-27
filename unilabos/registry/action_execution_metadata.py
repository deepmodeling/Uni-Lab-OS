"""动作执行责任元数据的唯一规范化模块。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.registry.decorators import ExecutorKind, NodeType, normalize_enum_value


def normalize_action_execution_metadata(
    action_arguments: Mapping[str, Any],
) -> dict[str, str]:
    """规范化动作节点类型与受控执行器种类。

    参数：``action_arguments`` 是装饰器运行时或 AST 编译得到的动作参数。
    返回：只含已声明 ``node_type``/``executor_kind`` 的稳定字符串字典。
    异常：执行器种类不属于 ``ExecutorKind`` 时抛 ``ValueError``，防止不同注册表
    投影对同一动作产生不同执行责任。
    """

    normalized: dict[str, str] = {}
    node_type = normalize_enum_value(action_arguments.get("node_type"), NodeType)
    if node_type:
        normalized["node_type"] = node_type
    executor_kind = normalize_enum_value(
        action_arguments.get("executor_kind"),
        ExecutorKind,
    )
    if executor_kind:
        allowed_executor_kinds = {kind.value for kind in ExecutorKind}
        if executor_kind not in allowed_executor_kinds:
            raise ValueError(f"不支持的 executor_kind: {executor_kind}")
        normalized["executor_kind"] = executor_kind
    return normalized


__all__ = ["normalize_action_execution_metadata"]
