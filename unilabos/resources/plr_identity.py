"""把 PLR 内嵌枪头提升为权威物料节点，恢复时重新组装原生 state。"""

from __future__ import annotations

import copy
import inspect
from typing import Any

TIP_CHILDREN = "unilabos_tip_children"
HEAD_CHILDREN = "unilabos_head_children"
TIP_COUNTER = "unilabos_tip_counter"
HEAD_SLOT = "unilabos_head_slot"


def has_legacy_tip_channels(resource_or_class: Any) -> bool:
    """仅识别 PLR 旧式 LiquidHandler，不占用其他物料的同名业务字段。"""
    cls = resource_or_class if isinstance(resource_or_class, type) else type(resource_or_class)
    return any(
        base.__name__ == "LiquidHandler"
        and base.__module__ in {
            "pylabrobot.legacy.liquid_handling.liquid_handler",
        }
        for base in cls.__mro__
    )


def material_children(resource: Any) -> list[Any]:
    """运行时结构孩子以及旧液体处理器通道中持有的 Resource 枪头。"""
    from pylabrobot.resources import Resource

    children = list(getattr(resource, "children", None) or [])
    if not has_legacy_tip_channels(resource):
        return children
    seen = {id(child) for child in children}
    for field in ("head", "head96"):
        channels = getattr(resource, field, None)
        if not isinstance(channels, dict):
            continue
        for tracker in channels.values():
            tip = getattr(tracker, "_pending_tip", None)
            if isinstance(tip, Resource) and id(tip) not in seen:
                children.append(tip)
                seen.add(id(tip))
    return children


def material_serialization(resource: Any) -> tuple[dict, dict]:
    """生成规范结构和状态；UUID 由调用方从对应实例读取。"""
    from pylabrobot.resources import Resource, Tip, TipSpot

    states = {}
    seen_names = set()
    counters = {}
    visited = set()

    def remember(node):
        if id(node) in visited:
            raise ValueError("物料树中重复引用了同一个运行时对象")
        visited.add(id(node))
        if hasattr(node, "_tip_counter"):
            counters[id(node)] = (node, node._tip_counter)
        for child in material_children(node):
            remember(child)

    remember(resource)

    def visit(node):
        if node.name in seen_names:
            raise ValueError(f"物料序列化出现重复名称或重复对象: {node.name}")
        seen_names.add(node.name)
        counter = counters.get(id(node), (None, None))[1]
        try:
            data = copy.deepcopy(node.serialize())
        finally:
            if counter is not None:
                node._tip_counter = counter
        state = copy.deepcopy(node.serialize_state())
        # PetriDish 等原生类型只输出 diameter/height；统一 pose 仍需三轴尺寸。
        for axis in ("x", "y", "z"):
            data.setdefault(f"size_{axis}", getattr(node, f"get_size_{axis}")())
        if isinstance(node, Tip):
            data["size_x"] = data["size_y"] = data["diameter"]
        children = {child.name: child for child in material_children(node)}
        serialized_children = data.get("children", [])
        if isinstance(node, TipSpot):
            # prototype 是工厂配置，不是某支实际枪头；避免每次 snapshot 改名。
            data["prototype_tip"]["name"] = f"{node.name}#prototype"
            state[TIP_COUNTER] = counter
            # 未提交的取放含历史对象，不允许伪装成已提交的物料拓扑。
            committed = state.get("tip")
            pending = state.get("pending_tip")
            if (committed or {}).get("name") != (pending or {}).get("name"):
                raise ValueError(
                    f"枪头位 {node.name} 有未提交的取放，请 commit/rollback 后再生成权威快照"
                )
            serialized_children = [child.serialize() for child in node.children]
            for key in ("tip", "pending_tip", "tip_state"):
                state.pop(key, None)
            state[TIP_CHILDREN] = True
        head_refs = {}
        head_slots = {}
        for field in ("head_state", "head96_state") if has_legacy_tip_channels(node) else ():
            channels = state.get(field)
            if not isinstance(channels, dict):
                continue
            refs = {}
            for channel, tracker_state in channels.items():
                committed = tracker_state.get("tip")
                pending = tracker_state.get("pending_tip")
                if (committed or {}).get("name") != (pending or {}).get("name"):
                    raise ValueError(
                        f"通道 {node.name}/{field}/{channel} 有未提交的取放"
                    )
                name = (pending or {}).get("name")
                if name is not None and name not in children:
                    raise ValueError(f"通道 {node.name}/{field}/{channel} 缺少对应枪头实例")
                refs[channel] = None
                if name is not None:
                    head_slots[name] = {"field": field, "channel": int(channel)}
                if name is not None and all(
                    item["name"] != name for item in serialized_children
                ):
                    serialized_children.append(children[name].serialize())
                channels[channel] = {}
            if refs:
                head_refs[field] = refs
        if head_refs:
            state[HEAD_CHILDREN] = head_refs
        states[node.name] = state
        data["children"] = [
            visit(children[child["name"]]) for child in serialized_children
        ]
        for name, slot in head_slots.items():
            # 通道是位置，不是身份；身份仍来自独立物料节点的 UUID。
            states[name][HEAD_SLOT] = slot
        return data

    try:
        return visit(resource), states
    finally:
        for node, counter in counters.values():
            node._tip_counter = counter


def restore_tip_counters(resource: Any, states: dict) -> None:
    """状态恢复可能重建枪头，最后恢复发号计数，防止再次生成已存在名称。"""
    state = states.get(resource.name, {})
    if TIP_COUNTER in state:
        counter = state[TIP_COUNTER]
        if type(counter) is not int or counter < 0:
            raise ValueError(f"枪头位 {resource.name} 的发号计数无效")
        resource._tip_counter = counter
    for child in material_children(resource):
        restore_tip_counters(child, states)


def prepare_plr_restore(data: dict, states: dict) -> None:
    """将规范 Tip 子节点转回 PLR 要求的 TipSpot state；不改变权威存储。"""
    from pylabrobot.resources import Resource, Tip, TipSpot
    from pylabrobot.utils.object_parsing import find_subclass

    for child in data.get("children", []):
        prepare_plr_restore(child, states)
    cls = find_subclass(data["type"], Resource)
    if cls is not None:
        parameters = inspect.signature(cls).parameters
        # 规范尺寸只供 pose 使用，恢复时仍遵循原生构造函数的几何参数。
        if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            for axis in ("x", "y", "z"):
                if f"size_{axis}" not in parameters:
                    data.pop(f"size_{axis}", None)
        if "model" not in parameters and data.get("model") is None:
            data.pop("model", None)
    if cls is not None and issubclass(cls, Tip):
        if "size_x" in data:
            data["diameter"] = data.pop("size_x")
        data.pop("size_y", None)
    state = states.get(data["name"], {})
    if state.pop(TIP_CHILDREN, False):
        if cls is None or not issubclass(cls, TipSpot):
            raise ValueError("枪头位内嵌状态只能用于 TipSpot")
        children = data.pop("children", [])
        if len(children) > 1:
            raise ValueError(f"枪头位 {data['name']} 不能恢复多个枪头")
        tip = children[0] if children else None
        tip_cls = find_subclass(tip["type"], Resource) if tip else None
        if tip and (tip_cls is None or not issubclass(tip_cls, Tip)):
            raise ValueError(f"枪头位 {data['name']} 的孩子必须是 Tip")
        state["tip"] = copy.deepcopy(tip)
        state["pending_tip"] = copy.deepcopy(tip)
        state["tip_state"] = copy.deepcopy(states.get(tip["name"], {})) if tip else None
    head_refs = state.pop(HEAD_CHILDREN, {})
    if head_refs and (cls is None or not has_legacy_tip_channels(cls)):
        raise ValueError("通道内嵌枪头状态只能用于 PLR LiquidHandler")
    for field, channels in head_refs.items():
        children = {child["name"]: child for child in data.get("children", [])}
        removed = set()
        for channel in channels:
            candidates = [
                child
                for child in children.values()
                if states.get(child["name"], {}).get(HEAD_SLOT)
                == {"field": field, "channel": int(channel)}
            ]
            if len(candidates) > 1:
                raise ValueError(f"通道 {data['name']}/{channel} 不能持有多个枪头")
            tip = candidates[0] if candidates else None
            name = tip["name"] if tip else None
            if name is not None:
                removed.add(name)
            state[field].pop(channel, None)
            # JSON 对象键只能是字符串，PLR 的通道 API 则以整数寻址。
            channel = int(channel)
            state[field][channel] = {
                "tip": copy.deepcopy(tip),
                "pending_tip": copy.deepcopy(tip),
                "tip_state": copy.deepcopy(states.get(name, {})) if tip else None,
            }
        data["children"] = [
            child for child in data.get("children", []) if child["name"] not in removed
        ]
