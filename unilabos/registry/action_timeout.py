"""``@action(timeout=..., execution_timeout=...)`` 的校验、归一化与求值。

两种超时是两条不同的闸门：

- ``timeout``：硬超时，秒。到期后执行面对动作发起协作式取消，并以
  ``TimeoutException`` 进入错误决策链（与其他失败同形）。
- ``execution_timeout``：业务软超时，秒或**表达式**。到期后动作继续运行，执行面以
  ``ExecutionTimeoutException`` 打开一条决策（多一个 ``wait`` 选项）；操作员可以继续等待、
  终止或替换结果。表达式引用动作入参，支持 ``+ - * /``、括号、一元正负号与数字字面量，
  例如 ``"duration * 1.5 + 30"``。声明期只校验语法与参数名，注册表保存归一化后的字符串；
  执行面在下发时用真实 ``action_args``（叠加注册表 ``goal_default``）求值。入参名也可以按 V1
  的写法放在花括号里（``{duration} * 1.5``）。语法与求值规则见 ``action_expression``。
"""

from __future__ import annotations

import ast
import math
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Union

from unilabos.registry.action_expression import (
    evaluate_param_expression,
    expression_names,
    parse_param_expression,
)

TimeoutSpec = Union[float, str]


class TimeoutExpressionError(ValueError):
    """表达式语法、引用或求值错误。"""


def normalize_action_timeout(value: Any, *, action_name: str = "action") -> Optional[float]:
    """校验硬超时：``None`` 表示不限时，否则必须是有限正数（秒）。"""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{action_name} 的 timeout 必须是秒数（正数），不能是 {type(value).__name__}")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{action_name} 的 timeout 必须是有限正数，收到 {value!r}")
    return seconds


def normalize_execution_timeout(
    value: Any,
    *,
    action_parameter_names: Optional[Iterable[str]] = None,
    action_name: str = "action",
) -> Optional[TimeoutSpec]:
    """校验软超时：正数秒，或只引用动作入参的四则运算表达式。

    返回归一化结果：数字 → ``float``；表达式 → ``ast.unparse`` 后的规范字符串
    （空白与括号统一，便于注册表 YAML 稳定）。
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{action_name} 的 execution_timeout 不能是布尔值")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f"{action_name} 的 execution_timeout 必须是有限正数，收到 {value!r}")
        return seconds
    if not isinstance(value, str):
        raise TypeError(
            f"{action_name} 的 execution_timeout 必须是秒数或参数表达式字符串，"
            f"不能是 {type(value).__name__}"
        )
    expression = value.strip()
    if not expression:
        raise ValueError(f"{action_name} 的 execution_timeout 表达式不能为空")
    tree = _parse_expression(expression, action_name=action_name)
    names = _referenced_names(tree)
    if action_parameter_names is not None:
        available = {
            str(name)
            for name in action_parameter_names
            if str(name) not in {"self", "cls"}
        }
        unknown = [name for name in names if name not in available]
        if unknown:
            raise ValueError(
                f"{action_name} 的 execution_timeout 引用了非动作入参: {', '.join(unknown)}"
            )
    if not names:
        # 纯常量表达式：直接折叠成秒数，注册表里不必保留表达式
        seconds = _evaluate(tree, {}, action_name=action_name)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(
                f"{action_name} 的 execution_timeout 表达式必须求得有限正数，收到 {expression!r}"
            )
        return seconds
    return ast.unparse(tree)


def execution_timeout_parameter_names(spec: Optional[TimeoutSpec]) -> List[str]:
    """表达式引用的动作入参名（按出现顺序去重）；数字或 ``None`` 返回空列表。"""

    if spec is None or isinstance(spec, (int, float)):
        return []
    return _referenced_names(_parse_expression(str(spec)))


def evaluate_execution_timeout(
    spec: Optional[TimeoutSpec],
    action_args: Optional[Mapping[str, Any]] = None,
    *,
    action_name: str = "action",
) -> Optional[float]:
    """用真实参数求软超时秒数；``None`` 表示未声明。

    参数缺失、非数值、除零或结果非正都抛 :class:`TimeoutExpressionError`，
    由调用方决定是拒绝下发还是记录告警后放弃看门狗。
    """

    if spec is None:
        return None
    if isinstance(spec, bool):
        raise TimeoutExpressionError(f"{action_name} 的 execution_timeout 不能是布尔值")
    if isinstance(spec, (int, float)):
        seconds = float(spec)
    else:
        tree = _parse_expression(str(spec), action_name=action_name)
        seconds = _evaluate(tree, dict(action_args or {}), action_name=action_name)
    if not math.isfinite(seconds) or seconds <= 0:
        raise TimeoutExpressionError(
            f"{action_name} 的 execution_timeout 求值结果必须是有限正数，得到 {seconds!r}"
        )
    return seconds


def _parse_expression(expression: str, *, action_name: str = "action") -> ast.AST:
    return parse_param_expression(
        expression, field="execution_timeout", action_name=action_name, error=TimeoutExpressionError,
    )


def _referenced_names(node: ast.AST) -> List[str]:
    return expression_names(node)


def _evaluate(node: ast.AST, values: Dict[str, Any], *, action_name: str) -> float:
    return evaluate_param_expression(
        node, values, field="execution_timeout", action_name=action_name, error=TimeoutExpressionError,
    )


__all__ = [
    "TimeoutExpressionError",
    "TimeoutSpec",
    "evaluate_execution_timeout",
    "execution_timeout_parameter_names",
    "normalize_action_timeout",
    "normalize_execution_timeout",
]
