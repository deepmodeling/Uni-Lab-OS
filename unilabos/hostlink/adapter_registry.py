"""当前设备执行 adapter 注册表，归 HostLink transport 边界管理。"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Optional


_adapter_condition = threading.Condition()
_active_adapter: Optional[Any] = None


def set_execution_adapter(adapter: Any) -> None:
    """注册当前 runtime 创建的设备执行适配器。"""

    global _active_adapter
    with _adapter_condition:
        _active_adapter = adapter
        _adapter_condition.notify_all()


def clear_execution_adapter(adapter: Optional[Any] = None) -> None:
    """清除当前适配器；传入实例时仅允许其清除自己的注册。"""

    global _active_adapter
    with _adapter_condition:
        if adapter is None or _active_adapter is adapter:
            _active_adapter = None
            _adapter_condition.notify_all()


def get_execution_adapter(timeout: Optional[float] = 0) -> Optional[Any]:
    """返回当前 transport adapter，并兼容尚未显式注册的 ROS2 HostNode。"""

    from unilabos.config.config import BasicConfig

    wait_timeout = 0.0 if timeout is None else max(float(timeout), 0.0)
    with _adapter_condition:
        if _active_adapter is not None:
            return _active_adapter
        if BasicConfig.backend == "hostlink":
            if wait_timeout:
                _adapter_condition.wait_for(
                    lambda: _active_adapter is not None,
                    timeout=wait_timeout,
                )
            return _active_adapter

    try:
        from unilabos.ros.nodes.presets.host_node import HostNode
    except ImportError:
        return None
    return HostNode.get_instance(timeout)


def execution_result_bridges(bridges: Iterable[Any]) -> list[Any]:
    """存在微后端生命周期 owner 时，只把原始执行结果交给该 owner。"""

    values = list(bridges)
    owners = [
        bridge
        for bridge in values
        if bool(getattr(bridge, "owns_job_lifecycle", False))
    ]
    return owners or values


__all__ = [
    "clear_execution_adapter",
    "execution_result_bridges",
    "get_execution_adapter",
    "set_execution_adapter",
]
