"""动作参数中的结构化结果引用；只取当前成功结果，不求值作者代码。"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID

from unilabos.protocol.runtime.condition import condition_guards


def _walk(value: Any, visit: Callable[[dict[str, Any]], Any], *, template: bool = False,
          depth: int = 0, unwrap_literals: bool = False) -> Any:
    if depth > 64:
        raise ValueError("结果参数嵌套不能超过 64 层")
    if isinstance(value, Mapping):
        if value.get("kind") == "workflow_literal":
            if set(value) != {"kind", "value"}:
                raise ValueError("字面输入封装格式无效")
            return deepcopy(value["value"] if unwrap_literals else value)
        if value.get("kind") == "node_output":
            identity = "node_key" if template else "workflow_node_uuid"
            if (set(value) != {"kind", identity, "data_key"}
                    or not isinstance(value.get(identity), str) or not value[identity]
                    or not isinstance(value.get("data_key"), str)):
                raise ValueError("结果引用必须声明节点身份和 data_key 路径")
            if not template:
                UUID(value[identity])
            return visit(dict(value))
        return {key: _walk(item, visit, template=template, depth=depth + 1, unwrap_literals=unwrap_literals) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk(item, visit, template=template, depth=depth + 1, unwrap_literals=unwrap_literals) for item in value]
    return value


def result_references(value: Any) -> set[str]:
    refs: set[str] = set()
    def collect(binding: dict[str, Any]) -> dict[str, Any]:
        refs.add(binding["workflow_node_uuid"])
        return binding
    _walk(value, collect)
    return refs


def remap_result_bindings(value: Any, resolve: Callable[[str], str], *, template: bool = False) -> Any:
    def remap(binding: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "node_output", "workflow_node_uuid": resolve(binding[
            "node_key" if template else "workflow_node_uuid"]), "data_key": binding["data_key"]}
    return _walk(value, remap, template=template)


def resolve_result_bindings(value: Any, read_result: Callable[[str], Any], *,
                            read_path: Callable[[Any, str], tuple[bool, Any]]) -> Any:
    def resolve(binding: dict[str, Any]) -> Any:
        result = read_result(binding["workflow_node_uuid"])
        path = binding["data_key"]
        if path:
            exists, result = read_path(result, path)
            if not exists:
                raise ValueError(f"动作引用的返回字段不存在：{path}")
        # 返回的数据是值，不再次解释其中的引用标记或循环占位符。
        return deepcopy(result)
    return _walk(value, resolve, unwrap_literals=True)


def validate_result_binding_graph(nodes: Mapping[str, Any], kinds: Mapping[str, str],
                                  parents: Mapping[str, str | None],
                                  params: Mapping[str, dict[str, Any]]) -> dict[str, set[str]]:
    def policy(node: Any) -> Mapping[str, Any]:
        return (node.get("execution_policy") if isinstance(node, Mapping)
                else getattr(node, "execution_policy", None)) or {}
    guards = {key: condition_guards(policy(node)) for key, node in nodes.items()}
    dependencies: dict[str, set[str]] = {}
    for target in nodes:
        # 旧组合调用的 handle 引用由组合边界规范器单独校验、展开。
        if kinds.get(target) in {"workflow", "condition"}:
            continue
        node = nodes[target]
        meta = (node.get("meta_data") if isinstance(node, Mapping) else getattr(node, "meta_data", None)) or {}
        refs = result_references([params.get(target, {}), meta.get("inventory_requirements", [])])
        allowed = {None}
        ancestor = parents.get(target)
        while ancestor is not None:
            allowed.add(ancestor)
            ancestor = parents.get(ancestor)
        for source in refs:
            if source not in nodes or source == target or kinds.get(source) not in {"device_action", "condition", "wait"}:
                raise ValueError("结果引用了不存在、禁用、自身或不产出动作结果的节点")
            if any(guard not in guards[target] for guard in guards[source]):
                raise ValueError("结果引用了其他分支，不能保证该分支已执行")
            if parents.get(source) not in allowed:
                raise ValueError("结果不能从其他循环体或循环体内部泄漏到外部")
        dependencies[target] = refs
    return dependencies


def result_binding_locations(value: Any, *, template: bool = False) -> dict[tuple[Any, ...], dict[str, Any]]:
    """模板配置允许改默认值，但不能删除、增加或替换过程中的动态连线。"""
    _walk(value, lambda binding: binding, template=template)
    found: dict[tuple[Any, ...], dict[str, Any]] = {}
    def collect(item: Any, path: tuple[Any, ...]) -> None:
        if isinstance(item, Mapping):
            if item.get("kind") == "node_output":
                found[path] = dict(item)
            else:
                for key, child in item.items():
                    collect(child, (*path, key))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                collect(child, (*path, index))
    collect(value, ())
    return found
