"""``--test_mode`` 的共同规则：哪些驱动真实执行、被模拟的动作返回什么。

Host 派发作业（两种 backend 的 host 执行适配器）与驱动发起的跨设备调用
（HostLink 的动作路由）用同一套规则：目标驱动声明 ``run_in_test_mode = True``
（模拟器、离线模式的驱动）才真实执行，其余一律返回按注册表 handles 构造的模拟结果，
不触达真实硬件。
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


def driver_runs_in_test_mode(driver: Any) -> bool:
    """驱动是否声明在测试模式下真实执行（必须显式为 ``True``）。"""

    return getattr(driver, "run_in_test_mode", False) is True


def build_test_mode_return(
    action_name: str, action_mapping: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """按注册表 handles 的 output 定义构建 test_mode 模拟返回值。

    data_key 中每层 ``@flatten`` 对应一层嵌套数组，叶子为空字典。
    例如 "vessel" → {}，"plate.@flatten" → [{}]，"a.@flatten.@flatten" → [[{}]]。
    """

    mock_return: Dict[str, Any] = {"test_mode": True, "action_name": action_name}
    handles = action_mapping.get("handles", {}) if isinstance(action_mapping, Mapping) else {}
    if isinstance(handles, Mapping):
        for output_handle in handles.get("output", []) or []:
            data_key = str(output_handle.get("data_key") or "")
            handler_key = str(output_handle.get("handler_key") or "")
            if not handler_key:
                continue
            value: Any = {}
            for _ in range(data_key.count("@flatten")):
                value = [value]
            mock_return[handler_key] = value
    return mock_return


__all__ = ["build_test_mode_return", "driver_runs_in_test_mode"]
