"""把设备 Registry 动作定义投影为生产控制面的逻辑能力。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_INTERNAL_ACTIONS = {
    "_execute_driver_command",
    "_execute_driver_command_async",
}


def project_device_action_capabilities(
    mappings: Any,
) -> list[dict[str, str]]:
    """返回可上报的业务动作，排除仅承载复用调用的内部端点。

    除名称与动作类型外，同时上报注册表里的显示名与业务说明（来自设备包
    ``@action(description=...)``），供单点动作页展示动作用途；两者缺省时省略，
    消费方按名称回退。
    """

    if not isinstance(mappings, Mapping):
        return []
    result = []
    for name, raw in mappings.items():
        action_name = str(name).strip()
        if not action_name or action_name in _INTERNAL_ACTIONS:
            continue
        action = raw if isinstance(raw, Mapping) else {}
        action_type = action.get("type")
        action_type_name = str(
            getattr(action_type, "__name__", action_type) or ""
        ).strip()
        if not action_type_name:
            continue
        capability = {"name": action_name, "type": action_type_name}
        displayname = str(action.get("displayname") or "").strip()
        if displayname and displayname != action_name:
            capability["display_name"] = displayname
        description = str(action.get("description") or "").strip()
        if description:
            capability["description"] = description
        result.append(capability)
    return sorted(result, key=lambda item: (item["name"], item["type"]))


__all__ = ["project_device_action_capabilities"]
