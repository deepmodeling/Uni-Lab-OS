"""``@action(estimate_duration_fixed=..., estimate_duration_express=...)``：动作预计时长。

字段名与 V1 一致，V1 写的驱动可以原样迁移：

- ``estimate_duration_fixed``：兜底秒数（有限且 ≥ 0）。没有声明时按
  :data:`DEFAULT_ESTIMATE_DURATION_S`（60 s，与 V1 相同）兜底；显式写 ``None`` 表示不提供兜底。
- ``estimate_duration_express``：引用动作入参的四则运算表达式，与 ``execution_timeout``
  同一套语法（``cycles * 2``，或 V1 的 ``{cycles} * 2``）。声明期校验语法与入参名，注册表保存
  归一化后的字符串。

调度器在派发一个 attempt 时，用最终 ``action_args``（叠加注册表 ``goal_default``）求出预计时长，
写在 attempt 上（``estimated_s`` / ``estimate_source``）：

- 表达式求值成功 → ``expression``；
- 表达式缺参数、非数值、除零或结果为负 → 退回兜底，并记下原因；
- 声明了兜底 → ``fixed``；没有声明 → ``default``（60 s）；显式 ``None`` → 不给出预计时长。

预计时长只用于展示与观测（时间线、监控面板、任务 ETA），不是准入条件，也不影响硬 / 软超时。
"""

from __future__ import annotations

import ast
import math
from typing import Any, Dict, Iterable, Mapping, Optional

from unilabos.registry.action_expression import (
    evaluate_param_expression,
    expression_names,
    parse_param_expression,
)

#: 未声明 ``estimate_duration_fixed`` 时的兜底秒数（与 V1 相同）
DEFAULT_ESTIMATE_DURATION_S = 60.0
FIXED_FIELD = "estimate_duration_fixed"
EXPRESS_FIELD = "estimate_duration_express"


class DurationExpressionError(ValueError):
    """预计时长声明或求值错误。"""


def normalize_estimate_duration_fixed(value: Any, *, action_name: str = "action") -> Optional[float]:
    """校验兜底秒数：``None`` 表示不提供兜底，否则必须是有限且 ≥ 0 的数。"""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{action_name} 的 {FIXED_FIELD} 必须是秒数或 None，不能是 {type(value).__name__}")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{action_name} 的 {FIXED_FIELD} 必须是有限且不小于 0 的秒数，收到 {value!r}")
    return seconds


def normalize_estimate_duration_express(
    value: Any, *, action_parameter_names: Optional[Iterable[str]] = None, action_name: str = "action",
) -> str:
    """校验表达式：只引用动作入参的四则运算；空串 / ``None`` 表示没有表达式。返回规范字符串。"""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{action_name} 的 {EXPRESS_FIELD} 必须是字符串，不能是 {type(value).__name__}")
    expression = value.strip()
    if not expression:
        return ""
    tree = parse_param_expression(expression, field=EXPRESS_FIELD, action_name=action_name, error=DurationExpressionError)
    if action_parameter_names is not None:
        available = {str(name) for name in action_parameter_names if str(name) not in {"self", "cls"}}
        unknown = [name for name in expression_names(tree) if name not in available]
        if unknown:
            raise ValueError(f"{action_name} 的 {EXPRESS_FIELD} 引用了非动作入参: {', '.join(unknown)}")
    return ast.unparse(tree)


def resolve_action_estimate(
    mapping: Optional[Mapping[str, Any]], action_args: Optional[Mapping[str, Any]] = None,
    *, action_name: str = "action",
) -> Dict[str, Any]:
    """按注册表动作条目求预计时长。

    返回 ``{"estimated_s": 秒|None, "estimate_source": "expression"|"fixed"|"default"|None,
    "error": 表达式失败原因|None}``；任何失败都只降级，不抛异常。
    """

    entry = mapping or {}
    resolved: Dict[str, Any] = {"estimated_s": None, "estimate_source": None, "error": None}
    expression = entry.get(EXPRESS_FIELD) or ""
    if isinstance(expression, str) and expression.strip():
        defaults = entry.get("goal_default")
        values: Dict[str, Any] = dict(defaults) if isinstance(defaults, Mapping) else {}
        values.update(dict(action_args or {}))
        try:
            tree = parse_param_expression(expression, field=EXPRESS_FIELD, action_name=action_name, error=DurationExpressionError)
            seconds = evaluate_param_expression(
                tree, values, field=EXPRESS_FIELD, action_name=action_name, error=DurationExpressionError,
            )
            if not math.isfinite(seconds) or seconds < 0:
                raise DurationExpressionError(f"{action_name} 的 {EXPRESS_FIELD} 求值结果必须是不小于 0 的有限数，得到 {seconds!r}")
            resolved.update(estimated_s=seconds, estimate_source="expression")
            return resolved
        except DurationExpressionError as exc:
            resolved["error"] = str(exc)
    if FIXED_FIELD in entry:
        try:
            fixed = normalize_estimate_duration_fixed(entry.get(FIXED_FIELD), action_name=action_name)
        except (TypeError, ValueError) as exc:
            resolved["error"] = resolved["error"] or str(exc)
            fixed = None
        if fixed is not None:
            resolved.update(estimated_s=fixed, estimate_source="fixed")
        return resolved
    resolved.update(estimated_s=DEFAULT_ESTIMATE_DURATION_S, estimate_source="default")
    return resolved


__all__ = [
    "DEFAULT_ESTIMATE_DURATION_S",
    "DurationExpressionError",
    "EXPRESS_FIELD",
    "FIXED_FIELD",
    "normalize_estimate_duration_express",
    "normalize_estimate_duration_fixed",
    "resolve_action_estimate",
]
