"""与工作流运行方式独立的 Runtime 能力。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from threading import RLock
from typing import Any, Mapping


@dataclass(frozen=True)
class RuntimeCapabilities:
    """本地 Runtime 对外暴露的能力。

    默认策略保留本地 Runtime 的既有行为：可以创作和查看所有工作流，也可以
    进入单步调试。需要收紧权限的部署可以单独配置字段，不再引入全局运行方式。
    """

    can_edit_definitions: bool = True
    can_view_drafts: bool = True
    can_run_unpublished: bool = True
    can_debug: bool = True
    can_execute_device_actions: bool = True

    def as_dict(self) -> dict[str, bool]:
        """返回稳定的公开能力投影。"""

        return {key: bool(value) for key, value in asdict(self).items()}


_capabilities = RuntimeCapabilities()
_capabilities_lock = RLock()


def get_runtime_capabilities() -> RuntimeCapabilities:
    """读取当前进程的能力策略。"""

    with _capabilities_lock:
        return _capabilities


def set_runtime_capabilities(
    capabilities: RuntimeCapabilities | Mapping[str, Any],
) -> RuntimeCapabilities:
    """用经过校验的布尔字段替换当前进程能力策略。"""

    if isinstance(capabilities, RuntimeCapabilities):
        normalized = capabilities
    elif isinstance(capabilities, Mapping):
        current = get_runtime_capabilities()
        values: dict[str, bool] = {}
        for field_name in (field.name for field in fields(current)):
            if field_name in capabilities:
                value = capabilities[field_name]
                if not isinstance(value, bool):
                    raise ValueError(f"Runtime 能力 {field_name} 必须是布尔值")
                values[field_name] = value
        normalized = replace(current, **values)
    else:
        raise TypeError("Runtime 能力必须是 RuntimeCapabilities 或映射")
    global _capabilities
    with _capabilities_lock:
        _capabilities = normalized
        return _capabilities


def reset_runtime_capabilities() -> RuntimeCapabilities:
    """恢复测试和 CLI 使用的本地 Runtime 默认开放策略。"""

    return set_runtime_capabilities(RuntimeCapabilities())


def is_workflow_visible(workflow: Mapping[str, Any]) -> bool:
    """判断当前策略是否向读取模型暴露工作流。"""

    capabilities = get_runtime_capabilities()
    if capabilities.can_view_drafts:
        return True
    return (
        workflow.get("workflow_type") == "normal"
        and workflow.get("status") == "published"
    )


def visible_workflow_filter(
    workflow_type: str | None,
    status: str | None,
    operation_category_uuid: str | None,
) -> tuple[str | None, str | None, str | None, bool]:
    """根据可见能力规范化工作流列表筛选条件。"""

    if get_runtime_capabilities().can_view_drafts:
        return workflow_type, status, operation_category_uuid, False
    if (
        workflow_type not in (None, "normal")
        or status not in (None, "published")
        or operation_category_uuid is not None
    ):
        return "normal", "published", None, True
    return "normal", "published", None, False


__all__ = [
    "RuntimeCapabilities",
    "get_runtime_capabilities",
    "is_workflow_visible",
    "reset_runtime_capabilities",
    "set_runtime_capabilities",
    "visible_workflow_filter",
]
