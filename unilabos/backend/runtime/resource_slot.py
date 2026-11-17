"""驱动动作参数里 ResourceSlot 的识别，HostLink 与 ROS 2 共用。"""

from __future__ import annotations

import inspect
import types
from typing import Any, Optional, Union, get_args, get_origin


def resource_slot_shape(annotation: Any) -> Optional[str]:
    """ResourceSlot 兼容注解返回 ``single`` / ``list``，其它返回 ``None``。

    驱动模块常用 ``from __future__ import annotations``，此时注解是字符串（如
    ``"ResourceSlot"``、``"list[ResourceSlot]"``），必须与真实类型同样识别；
    ``Optional[ResourceSlot]`` 视为单个。
    """

    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        normalized = annotation.replace(" ", "").replace("typing.", "")
        if "ResourceSlot" not in normalized:
            return None
        if normalized.startswith(("list[", "List[")):
            return "list"
        return "single"

    origin = get_origin(annotation)
    if origin is list:
        arguments = get_args(annotation)
        if arguments and resource_slot_shape(arguments[0]) == "single":
            return "list"
        return None
    if origin in (Union, types.UnionType):
        shapes = {
            shape
            for item in get_args(annotation)
            if item is not type(None) and (shape := resource_slot_shape(item))
        }
        return shapes.pop() if len(shapes) == 1 else None

    name = str(getattr(annotation, "__qualname__", "") or "")
    module = str(getattr(annotation, "__module__", "") or "")
    return "single" if name == "ResourceSlot" and module else None


def resource_slot_shapes(function: Any) -> dict[str, str]:
    """函数各参数中 ResourceSlot 的形态：``{参数名: "single" | "list"}``。"""

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return {}
    return {
        name: shape
        for name, parameter in parameters.items()
        if (shape := resource_slot_shape(parameter.annotation))
    }
