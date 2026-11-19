"""动作声明里引用入参的四则运算表达式：软超时、预计时长共用。

表达式只允许动作入参名、数字字面量、``+ - * /``、括号与一元正负号，用 AST 白名单校验，
绝不 ``eval``。入参名可以直接写（``duration * 1.5 + 30``），也可以按 V1 的写法放在花括号里
（``{cycles} * 2``），归一化后统一成不带花括号的规范字符串。
"""

from __future__ import annotations

import ast
import math
import re
from typing import Any, Dict, List, Mapping, Type

_ALLOWED_BINARY = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_UNARY = (ast.UAdd, ast.USub)
_BRACED_NAME = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")


class ParamExpressionError(ValueError):
    """表达式语法、引用或求值错误。"""


def parse_param_expression(
    expression: str, *, field: str, action_name: str = "action",
    error: Type[ValueError] = ParamExpressionError,
) -> ast.AST:
    """解析并按白名单校验，返回表达式 AST（``{name}`` 先换成 ``name``）。"""

    text = _BRACED_NAME.sub(r"\1", expression)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise error(f"{action_name} 的 {field} 表达式语法错误: {expression!r} ({exc.msg})") from None
    _validate_node(tree.body, expression, field=field, action_name=action_name, error=error)
    return tree.body


def expression_names(node: ast.AST) -> List[str]:
    """表达式引用的入参名（按出现顺序去重）。"""

    names: List[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id not in names:
            names.append(child.id)
    return names


def evaluate_param_expression(
    node: ast.AST, values: Mapping[str, Any], *, field: str, action_name: str = "action",
    error: Type[ValueError] = ParamExpressionError,
) -> float:
    """用实参求值；缺参数、非数值、除零都抛 ``error``。"""

    return _evaluate(node, dict(values), field=field, action_name=action_name, error=error)


def _validate_node(node: ast.AST, expression: str, *, field: str, action_name: str, error: Type[ValueError]) -> None:
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINARY):
            raise error(f"{action_name} 的 {field} 只支持 + - * /: {expression!r}")
        _validate_node(node.left, expression, field=field, action_name=action_name, error=error)
        _validate_node(node.right, expression, field=field, action_name=action_name, error=error)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARY):
            raise error(f"{action_name} 的 {field} 只支持一元正负号: {expression!r}")
        _validate_node(node.operand, expression, field=field, action_name=action_name, error=error)
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise error(f"{action_name} 的 {field} 只允许数字字面量: {expression!r}")
        return
    if isinstance(node, ast.Name):
        return
    raise error(f"{action_name} 的 {field} 只允许入参名、数字与四则运算: {expression!r}")


def _evaluate(node: ast.AST, values: Dict[str, Any], *, field: str, action_name: str, error: Type[ValueError]) -> float:
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise error(f"{action_name} 的 {field} 缺少参数 {node.id!r} 的值")
        return _coerce_number(node.id, values[node.id], field=field, action_name=action_name, error=error)
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate(node.operand, values, field=field, action_name=action_name, error=error)
        return -operand if isinstance(node.op, ast.USub) else operand
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, values, field=field, action_name=action_name, error=error)
        right = _evaluate(node.right, values, field=field, action_name=action_name, error=error)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise error(f"{action_name} 的 {field} 表达式除以零")
        return left / right
    raise error(f"{action_name} 的 {field} 表达式包含不支持的节点")


def _coerce_number(name: str, value: Any, *, field: str, action_name: str, error: Type[ValueError]) -> float:
    if isinstance(value, bool):
        raise error(f"{action_name} 的 {field} 参数 {name!r} 是布尔值，不能参与运算")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise error(f"{action_name} 的 {field} 参数 {name!r} 不是数字: {value!r}") from None
    else:
        raise error(f"{action_name} 的 {field} 参数 {name!r} 类型为 {type(value).__name__}，不能参与运算")
    if not math.isfinite(number):
        raise error(f"{action_name} 的 {field} 参数 {name!r} 不是有限数: {value!r}")
    return number


__all__ = [
    "ParamExpressionError",
    "evaluate_param_expression",
    "expression_names",
    "parse_param_expression",
]
