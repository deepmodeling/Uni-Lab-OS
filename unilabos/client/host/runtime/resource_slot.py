"""驱动动作参数里 ResourceSlot 的识别，HostLink 与 ROS 2 共用。"""

from __future__ import annotations

import inspect
import types
from collections.abc import Mapping
from typing import Annotated, Any, Optional, Union, get_args, get_origin


def resource_slot_shape(annotation: Any) -> Optional[str]:
    """ResourceSlot 兼容注解返回 ``single`` / ``list``，其它返回 ``None``。

    驱动模块常用 ``from __future__ import annotations``，此时注解是字符串（如
    ``"ResourceSlot"``、``"list[ResourceSlot]"``），必须与真实类型同样识别；
    ``Optional[ResourceSlot]`` 视为单个；``Annotated[...]`` 按它包着的类型识别。
    """

    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        normalized = annotation.replace(" ", "").replace("typing.", "")
        if normalized.startswith("Annotated["):
            # Annotated[类型, 元数据...]：只看第一个成员（元数据里不会出现 ResourceSlot）
            normalized = normalized[len("Annotated["):]
        if "ResourceSlot" not in normalized:
            return None
        if normalized.startswith(("list[", "List[")):
            return "list"
        return "single"

    origin = get_origin(annotation)
    if origin is Annotated:
        arguments = get_args(annotation)
        return resource_slot_shape(arguments[0]) if arguments else None
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


def is_unset_resource_slot(value: Any) -> bool:
    """单个 ResourceSlot 的值是「未指定」：None、空白字符串、空对象或空列表。

    与调度器判定「没有物料、不加锁」的口径一致（``scheduler.materials`` 的空值规则）：
    可选容器参数留空、或按 goal 默认值填了 ``{}`` 时不解析、不加锁，驱动拿到 None。
    """

    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (Mapping, list, tuple)):
        return len(value) == 0
    return False


def device_slot_reference(tree_set: Any) -> Optional[dict[str, str]]:
    """ResourceSlot 解析到的是设备本身时，返回交给驱动的设备引用；否则返回 ``None``。

    设备不是 PLR 物料：设备子树里只放 PLR 物料，设备节点本身不反序列化（``to_plr_resources``
    跳过设备树），没有 PLR 实例可给。搬运动作把设备当所属物料时（例如机械臂的夹爪库位挂在设备上），
    驱动拿到 ``{"uuid", "id", "name", "type": "device"}``，与 ``{"uuid": ...}`` 引用同形。
    ``tree_set`` 是权威取回或上游传来的单棵 ``ResourceTreeSet``。
    """

    trees = getattr(tree_set, "trees", None) or []
    if len(trees) != 1:
        return None
    root = trees[0].root_node.res_content
    if str(getattr(root, "type", "") or "") != "device":
        return None
    return {
        "uuid": str(root.uuid or ""),
        "id": str(root.id or ""),
        "name": str(root.name or root.id or ""),
        "type": "device",
    }


def first_plr_resource(tree_set: Any, identity: Any) -> Any:
    """单个 ResourceSlot 的资源树转成 PLR 后的根实例；一个 PLR 物料都转不出时明确报错（而不是越界）。

    调用方先用 :func:`device_slot_reference` 处理设备本身；``to_plr_resources`` 只会在树里全是设备时
    返回空列表，这里兜底说清是哪个引用。
    """

    resources = tree_set.to_plr_resources()
    if not resources:
        roots = [
            f"{tree.root_node.res_content.id}（type={tree.root_node.res_content.type}）"
            for tree in getattr(tree_set, "trees", None) or []
        ]
        raise ValueError(f"ResourceSlot {identity!r} 的资源树里没有 PLR 物料：{', '.join(roots) or '空树'}")
    return resources[0]


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
