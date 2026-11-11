"""调度器本地条件：封闭的 JSON 表达式，不执行 Python 字符串。"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

_COMPARE = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt,
            "le": operator.le, "gt": operator.gt, "ge": operator.ge}


def remap_expression(value: Any, resolve: Callable[[str], str], *, reference_key: str = "node_uuid") -> Any:
    """模板实例化和组合展开只替换结构化节点引用，不改写字段名和字面量。"""
    if isinstance(value, dict):
        if set(value) in ({"lit"}, {"value"}):
            return value
        if set(value) == {reference_key}:
            return {"node_uuid": resolve(value[reference_key])}
        return {key: remap_expression(item, resolve, reference_key=reference_key) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_expression(item, resolve, reference_key=reference_key) for item in value]
    return value


def _scalar(value: Any) -> bool:
    return value is None or type(value) in {bool, str, int} or (
        type(value) is float and math.isfinite(value)
    )


def validate_expression(expr: Any, *, depth: int = 0) -> set[str]:
    """校验表达式并返回引用的节点 UUID；限制嵌套和操作集合。"""
    if depth > 32 or not isinstance(expr, dict):
        raise ValueError("条件表达式必须是对象，嵌套不能超过 32 层")
    keys = set(expr)
    if keys == {"lit"} and _scalar(expr["lit"]):
        return set()
    if keys == {"input"} and isinstance(expr["input"], str) and expr["input"].isidentifier():
        return set()
    if keys == {"value"}:
        from unilabos.protocol.utils.json_codec import encode_json

        encode_json(expr["value"])
        return set()
    if keys == {"node_uuid"}:
        UUID(expr["node_uuid"])
        return {expr["node_uuid"]}
    if keys == {"field", "name"} and isinstance(expr["name"], str) and expr["name"]:
        return validate_expression(expr["field"], depth=depth + 1)
    if keys == {"not"}:
        return validate_expression(expr["not"], depth=depth + 1)
    if keys == {"op", "args"}:
        op, args = expr["op"], expr["args"]
        if not isinstance(args, list) or not 2 <= len(args) <= 64:
            raise ValueError("条件操作数必须是含 2 至 64 项的列表")
        if op not in {*_COMPARE, "and", "or"} or (op in _COMPARE and len(args) != 2):
            raise ValueError("条件运算符或操作数数量无效")
        refs: set[str] = set()
        for arg in args:
            refs.update(validate_expression(arg, depth=depth + 1))
        return refs
    raise ValueError("不支持的条件表达式")


def condition_references(param: Mapping[str, Any]) -> set[str]:
    if set(param) != {"version", "expression"} or type(param["version"]) is not int or param["version"] != 1:
        raise ValueError("条件节点必须声明 version=1 和 expression")
    return validate_expression(param["expression"])


def condition_guards(policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    guards = policy.get("condition_guards", [])
    if not isinstance(guards, list) or len(guards) > 16:
        raise ValueError("condition_guards 必须是最多 16 项的列表")
    seen: set[str] = set()
    for guard in guards:
        if not isinstance(guard, dict) or set(guard) != {"node_uuid", "equals"} or type(guard["equals"]) is not bool:
            raise ValueError("条件分支必须声明 node_uuid 和严格布尔 equals")
        UUID(guard["node_uuid"])
        if guard["node_uuid"] in seen:
            raise ValueError("条件分支重复引用同一条件节点")
        seen.add(guard["node_uuid"])
    return guards


def strict_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("条件结果必须是布尔值，不能用数字、字符串或对象代替")
    return value


def evaluate_condition(param: Mapping[str, Any], read_result: Callable[[str], Any], *,
                       read_input: Callable[[str], Any] | None = None) -> bool:
    """从当前 attempt 的成功结果读取字段；缺失字段和类型错误都使节点失败。"""
    condition_references(param)

    def evaluate(expr: dict[str, Any]) -> Any:
        if "lit" in expr:
            return expr["lit"]
        if "value" in expr:
            return expr["value"]
        if "input" in expr:
            if read_input is None:
                raise ValueError("条件缺少工作流输入读取器")
            return read_input(expr["input"])
        if "node_uuid" in expr:
            return read_result(expr["node_uuid"])
        if "field" in expr:
            value = evaluate(expr["field"])
            if not isinstance(value, Mapping) or expr["name"] not in value:
                raise ValueError(f"条件引用的返回字段不存在：{expr['name']}")
            return value[expr["name"]]
        if "not" in expr:
            return not strict_bool(evaluate(expr["not"]))
        op, args = expr["op"], expr["args"]
        if op == "and":
            return all(strict_bool(evaluate(arg)) for arg in args)
        if op == "or":
            return any(strict_bool(evaluate(arg)) for arg in args)
        left, right = (evaluate(arg) for arg in args)
        numbers = type(left) in {int, float} and type(right) in {int, float}
        if not _scalar(left) or not _scalar(right) or (type(left) is not type(right) and not numbers):
            raise ValueError("条件比较要求同类型标量或两个数字")
        if op not in {"eq", "ne"} and not (numbers or type(left) is type(right) is str):
            raise ValueError("大小比较只支持数字或字符串")
        return _COMPARE[op](left, right)

    return strict_bool(evaluate(param["expression"]))


def validate_condition_graph(nodes: Mapping[str, Any], kinds: Mapping[str, str],
                             parents: Mapping[str, str | None],
                             params: Mapping[str, dict[str, Any]]) -> dict[str, set[str]]:
    """校验分支作用域，返回必须加入执行计划的条件依赖。"""
    def get(node: Any, key: str, default: Any = None) -> Any:
        return node.get(key, default) if isinstance(node, Mapping) else getattr(node, key, default)

    guards = {key: condition_guards(get(node, "execution_policy", {}) or {}) for key, node in nodes.items()}
    dependencies: dict[str, set[str]] = {}

    def visible(source: str, target: str, active: list[dict[str, Any]]) -> None:
        if source not in nodes or source == target:
            raise ValueError("条件引用了不存在、禁用或自身节点")
        if any(guard not in active for guard in guards[source]):
            raise ValueError("条件引用了其他分支的结果，不能保证该结果可用")
        ancestor = parents.get(target)
        allowed = {None, ancestor}
        while ancestor is not None:
            ancestor = parents.get(ancestor)
            allowed.add(ancestor)
        if parents.get(source) not in allowed:
            raise ValueError("条件不能读取其他循环体或尚未完成的循环体结果")

    for key, node in nodes.items():
        active: list[dict[str, Any]] = []
        refs: set[str] = set()
        for guard in guards[key]:
            source = guard["node_uuid"]
            visible(source, key, active)
            if kinds.get(source) != "condition":
                raise ValueError("条件分支必须引用 condition 节点")
            refs.add(source)
            active.append(guard)
        parent = parents.get(key)
        if parent is not None and any(guard not in active for guard in guards[parent]):
            raise ValueError("循环体必须继承循环节点的分支条件")
        if kinds.get(key) == "condition":
            for source in condition_references(params[key]):
                visible(source, key, active)
                refs.add(source)
        dependencies[key] = refs
    return dependencies


def condition_input_references(param: Mapping[str, Any]) -> set[str]:
    condition_references(param)
    names: set[str] = set()
    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            if set(value) in ({"lit"}, {"value"}):
                return
            if set(value) == {"input"}:
                names.add(value["input"])
            else:
                for item in value.values():
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(param["expression"])
    return names


def bind_condition_inputs(param: Mapping[str, Any], resolve: Callable[[str], Any]) -> dict[str, Any]:
    """展开子流程时改绑条件输入；静态实参保持为 JSON 值，不再次解释。"""
    def rewrite(value: Any) -> Any:
        if isinstance(value, Mapping):
            if set(value) in ({"lit"}, {"value"}):
                return dict(value)
            if set(value) == {"input"}:
                argument = resolve(value["input"])
                if isinstance(argument, Mapping) and argument.get("kind") == "workflow_input":
                    return {"input": argument["parameter"]}
                if isinstance(argument, Mapping) and argument.get("kind") == "workflow_literal":
                    argument = argument["value"]
                return {"value": argument}
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value
    return rewrite(param)
