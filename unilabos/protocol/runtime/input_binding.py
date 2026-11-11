"""工作流输入引用与字面输入边界；容器里的输入不会被误当作过程代码。"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any


def map_input_bindings(value: Any, resolve: Callable[[str], Any], *, depth: int = 0) -> Any:
    if depth > 64:
        raise ValueError("输入引用嵌套不能超过 64 层")
    if isinstance(value, Mapping):
        if value.get("kind") == "workflow_literal":
            if set(value) != {"kind", "value"}:
                raise ValueError("字面输入封装格式无效")
            return deepcopy(value)
        if value.get("kind") == "workflow_input":
            if set(value) != {"kind", "parameter"} or not isinstance(value["parameter"], str) or not value["parameter"].isidentifier():
                raise ValueError("工作流输入引用必须声明 parameter 名称")
            return resolve(value["parameter"])
        return {key: map_input_bindings(item, resolve, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [map_input_bindings(item, resolve, depth=depth + 1) for item in value]
    return value


def input_references(value: Any) -> set[str]:
    names: set[str] = set()
    def collect(name: str) -> Any:
        names.add(name)
        return {"kind": "workflow_input", "parameter": name}
    map_input_bindings(value, collect)
    return names


def literal_input(value: Any) -> Any:
    def reserved(item: Any) -> bool:
        if isinstance(item, Mapping):
            return item.get("kind") in ("workflow_input", "node_output", "workflow_literal") or any(reserved(child) for child in item.values())
        if isinstance(item, list):
            return any(reserved(child) for child in item)
        return isinstance(item, str) and "{{loop." in item
    copied = deepcopy(value)
    return {"kind": "workflow_literal", "value": copied} if reserved(value) else copied


def input_binding_locations(value: Any) -> dict[tuple[Any, ...], str]:
    input_references(value)
    result: dict[tuple[Any, ...], str] = {}
    def visit(item: Any, path: tuple[Any, ...]) -> None:
        if isinstance(item, Mapping):
            if item.get("kind") == "workflow_literal":
                return
            if item.get("kind") == "workflow_input":
                result[path] = item["parameter"]
                return
            for key, child in item.items():
                visit(child, (*path, key))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, (*path, index))
    visit(value, ())
    return result
