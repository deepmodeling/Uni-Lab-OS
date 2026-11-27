"""结构化工作流控制表达式的封闭求值器。"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from typing import Any


class ConditionEvaluationError(ValueError):
    """条件表达式无法安全求值。"""

    code = "condition_evaluation_failed"


_BINARY: dict[str, Callable[[Any, Any], Any]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "//": operator.floordiv,
    "%": operator.mod,
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}
_CALLS: dict[str, Callable[..., Any]] = {
    "len": len,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "contains": lambda container, item: item in container,
    "get": lambda container, key: (
        container.get(key) if isinstance(container, Mapping) else None
    ),
}


def evaluate_condition_expression(
    expression: Any,
    *,
    variables: Mapping[str, Any],
    max_depth: int = 32,
) -> bool:
    """求值一个结构化表达式，并要求最终结果是严格 ``bool``。"""

    try:
        result = _evaluate(
            expression, variables=variables, depth=0, max_depth=max_depth
        )
    except ConditionEvaluationError:
        raise
    except (ArithmeticError, LookupError, TypeError, ValueError) as error:
        raise ConditionEvaluationError(str(error) or type(error).__name__) from error
    if type(result) is not bool:
        raise ConditionEvaluationError("条件结果必须是严格布尔值")
    return result


def _evaluate(
    expression: Any,
    *,
    variables: Mapping[str, Any],
    depth: int,
    max_depth: int,
) -> Any:
    """递归求值内部节点，保持可调用操作为显式闭集。"""

    if depth > max_depth:
        raise ConditionEvaluationError("条件表达式超过最大嵌套深度")
    if not isinstance(expression, Mapping):
        raise ConditionEvaluationError("条件表达式节点必须是对象")
    if set(expression) == {"lit"}:
        return expression["lit"]
    if set(expression) == {"var"}:
        name = expression["var"]
        if not isinstance(name, str) or name not in variables:
            raise ConditionEvaluationError(f"条件变量不存在：{name}")
        return variables[name]
    if set(expression) == {"field", "name"}:
        container = _evaluate(
            expression["field"],
            variables=variables,
            depth=depth + 1,
            max_depth=max_depth,
        )
        name = expression["name"]
        if not isinstance(container, Mapping) or not isinstance(name, str):
            raise ConditionEvaluationError("条件字段访问要求对象和字符串字段名")
        return container[name]
    if set(expression) == {"index", "key"}:
        container = _evaluate(
            expression["index"],
            variables=variables,
            depth=depth + 1,
            max_depth=max_depth,
        )
        key = _evaluate(
            expression["key"],
            variables=variables,
            depth=depth + 1,
            max_depth=max_depth,
        )
        return container[key]
    if set(expression) == {"unop", "operand"}:
        value = _evaluate(
            expression["operand"],
            variables=variables,
            depth=depth + 1,
            max_depth=max_depth,
        )
        operation = expression["unop"]
        if operation == "not":
            if type(value) is not bool:
                raise ConditionEvaluationError("not 操作数必须是严格布尔值")
            return not value
        if operation == "neg":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConditionEvaluationError("负号操作数必须是数值")
            return -value
        raise ConditionEvaluationError(f"未知条件一元运算符：{operation}")
    if set(expression) == {"binop", "left", "right"}:
        operation = expression["binop"]
        left = _evaluate(
            expression["left"],
            variables=variables,
            depth=depth + 1,
            max_depth=max_depth,
        )
        if operation in {"and", "or"}:
            if type(left) is not bool:
                raise ConditionEvaluationError("布尔运算数必须是严格布尔值")
            if operation == "and" and not left:
                return False
            if operation == "or" and left:
                return True
            right = _evaluate(
                expression["right"],
                variables=variables,
                depth=depth + 1,
                max_depth=max_depth,
            )
            if type(right) is not bool:
                raise ConditionEvaluationError("布尔运算数必须是严格布尔值")
            return right
        function = _BINARY.get(str(operation))
        if function is None:
            raise ConditionEvaluationError(f"未知条件二元运算符：{operation}")
        right = _evaluate(
            expression["right"],
            variables=variables,
            depth=depth + 1,
            max_depth=max_depth,
        )
        return function(left, right)
    if set(expression) == {"call", "args"}:
        function = _CALLS.get(str(expression["call"]))
        arguments = expression["args"]
        if function is None or not isinstance(arguments, list):
            raise ConditionEvaluationError("条件函数不在白名单中或参数无效")
        return function(
            *[
                _evaluate(
                    argument,
                    variables=variables,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                for argument in arguments
            ]
        )
    raise ConditionEvaluationError("无法识别条件表达式")


__all__ = ["ConditionEvaluationError", "evaluate_condition_expression"]
