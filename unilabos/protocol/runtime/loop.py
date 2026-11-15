"""工作流循环节点（``type="loop"``）的参数契约、条件求值与迭代变量。

循环是**容器节点**：循环体 = ``parent_uuid`` 指向它的子节点（可嵌套）。运行时由本机
调度器逐轮执行循环体——每轮给循环体节点追加一个新的 attempt（``trigger="loop_iteration"``），
节点运行/attempt 的持久化事实与重试完全同构，前端按节点看到的是当前轮次的结果与全部
轮次的 attempt 历史。

- ``for``：固定 ``count`` 轮（整数，或建任务时换成输入值的工作流输入引用）；``start`` 是循环变量的
  起点，本轮的循环变量 = ``start + index``（源码 ``for i in range(start, stop)``）；
- ``while``：每轮开始前求值 ``condition``，为真才执行循环体；``max_iterations`` 是安全上限，
  达到即按失败收敛；``interval_seconds`` 是两轮之间的等待（空循环体 + 设备状态条件 =
  "等到某状态"，此时必须给出轮询间隔）。

条件的数据源：

- ``device_state``：设备状态字段（设备 ``@topic_config`` / status_fields 上报到微后端
  telemetry 的最新值），``device_id`` + ``field``；
- ``node_output``：某个节点（通常在循环体里）最近一次成功的返回值，``node_uuid`` +
  ``data_key``（返回值里的路径，空串取整个返回值）。该节点在本任务里还没有产出时按
  "继续"处理，即循环体至少跑一轮——这正是"重复测量直到达标"的写法。

while 也可以用静态条件表达式 ``expression``（与条件节点同一套 JSON：``and`` / ``or`` / ``not``、
``eq`` / ``ne`` / ``lt`` / ``le`` / ``gt`` / ``ge``，读工作流输入、循环前的结果和携带的变量），每轮开始前
求值（源码原生 ``while <条件>:``）。

跨轮携带值（carry，源码里是循环前赋值、循环体里重新赋值的变量）：``carry`` 是第一轮开始前的初值
（字面量、工作流输入、循环前的结果、外层循环携带的变量），``carry_next`` 是每轮循环体成功结束后
下一轮的取值（通常引用本轮循环体里的结果；没写的键保持不变）。carry 是循环节点自己的结果
``return_value.carry``：循环体和循环条件读到的是本轮的值，循环结束后下游读到最终值；引用写成指向
循环节点的 ``node_output``（``data_key`` 为 ``carry.<键>``；for 的循环变量是 ``value``）或条件表达式里的
``{"field": {"field": {"node_uuid": 循环}, "name": "carry"}, "name": 键}``。每轮开始时 carry 随轮次写进
循环节点运行的 ``control_data.loop.carry``，重启后从这里续用。

循环体节点参数里的字符串可以引用迭代变量：``{{loop.index}}``（0 起）、``{{loop.iteration}}``
（1 起）、``{{loop.count}}``（for 的总轮数；while 为 ``null``）。整个字符串恰好是一个占位符
时替换为原始类型（int），否则做文本替换。嵌套循环取最内层。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LOOP_NODE_TYPE = "loop"
#: 循环体节点 attempt 的 trigger：与 initial / retry_decision / recovery 并列。
LOOP_ITERATION_TRIGGER = "loop_iteration"

ComparisonOp = Literal["==", "!=", ">", ">=", "<", "<=", "contains", "exists"]

#: 单次任务里 while 循环的默认安全上限。
DEFAULT_MAX_ITERATIONS = 1000
MAX_FOR_COUNT = 100_000
#: 一个循环最多携带的值个数
MAX_CARRY_KEYS = 64
#: 模板里条件表达式引用尚未建好的步骤 key 时，校验用这个占位 uuid 顶替
_TEMPLATE_NODE_PLACEHOLDER = "00000000-0000-4000-8000-000000000001"

_PLACEHOLDER = re.compile(r"\{\{\s*loop\.(index|iteration|count)\s*\}\}")
_WHOLE_PLACEHOLDER = re.compile(r"^\s*\{\{\s*loop\.(index|iteration|count)\s*\}\}\s*$")


class LoopCondition(BaseModel):
    """while 循环的继续条件：``<source 取到的值> <op> <value>``。"""

    model_config = ConfigDict(extra="forbid")

    source: Literal["device_state", "node_output"]
    device_id: Optional[str] = None
    field: Optional[str] = None
    node_uuid: Optional[str] = None
    data_key: str = ""
    op: ComparisonOp = "=="
    value: Any = None

    @model_validator(mode="after")
    def _check_source_fields(self) -> "LoopCondition":
        if self.source == "device_state":
            if not (self.device_id or "").strip() or not (self.field or "").strip():
                raise ValueError("device_state 条件必须给出 device_id 与 field")
            self.device_id = self.device_id.strip()  # type: ignore[union-attr]
            self.field = self.field.strip()  # type: ignore[union-attr]
        else:
            if not (self.node_uuid or "").strip():
                raise ValueError("node_output 条件必须给出 node_uuid")
            self.node_uuid = self.node_uuid.strip()  # type: ignore[union-attr]
        if self.op != "exists" and self.value is None:
            raise ValueError(f"比较运算 {self.op!r} 需要 value")
        return self

    def describe(self) -> str:
        """人可读的一句话，用于日志 / 节点摘要。"""

        if self.source == "device_state":
            subject = f"{self.device_id}.{self.field}"
        else:
            subject = f"节点 {self.node_uuid}" + (f".{self.data_key}" if self.data_key else "")
        if self.op == "exists":
            return f"{subject} 存在"
        return f"{subject} {self.op} {self.value!r}"


class LoopSpec(BaseModel):
    """循环节点的 ``param``。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["for", "while"]
    #: for 的轮数：整数，或 ``{"kind": "workflow_input", "parameter": 名}``（建任务时换成输入值）
    count: Optional[Any] = None
    #: for 循环变量的起点（``range(start, stop)``）；本轮循环变量 = start + index
    start: int = Field(default=0, ge=-MAX_FOR_COUNT, le=MAX_FOR_COUNT)
    condition: Optional[LoopCondition] = None
    #: while 的静态条件表达式（与 ``condition`` 二选一），每轮开始前求值
    expression: Optional[Dict[str, Any]] = None
    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1, le=MAX_FOR_COUNT)
    interval_seconds: float = Field(default=0.0, ge=0.0, le=86_400.0)
    #: 跨轮携带值的初值：键 → JSON 值或结构化引用
    carry: Optional[Dict[str, Any]] = None
    #: 每轮成功后下一轮的 carry：键（须在 ``carry`` 里）→ JSON 值或结构化引用
    carry_next: Optional[Dict[str, Any]] = None

    @field_validator("count")
    @classmethod
    def _check_count(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, Mapping):
            if (set(value) != {"kind", "parameter"} or value.get("kind") != "workflow_input"
                    or not isinstance(value.get("parameter"), str) or not value["parameter"].isidentifier()):
                raise ValueError("count 只能是整数或工作流输入引用")
            return dict(value)
        if type(value) is not int or not 1 <= value <= MAX_FOR_COUNT:
            raise ValueError(f"count 必须是 1 至 {MAX_FOR_COUNT} 的整数")
        return value

    @model_validator(mode="after")
    def _check_mode(self) -> "LoopSpec":
        if self.mode == "for":
            if self.count is None:
                raise ValueError("for 循环必须给出 count")
            if self.condition is not None or self.expression is not None:
                raise ValueError("for 循环不接受 condition")
        else:
            if (self.condition is None) == (self.expression is None):
                raise ValueError("while 循环必须给出 condition 或 expression 之一")
            if self.count is not None:
                raise ValueError("while 循环不接受 count，用 max_iterations 设上限")
            if self.start:
                raise ValueError("while 循环不接受 start")
        if self.expression is not None:
            from unilabos.protocol.runtime.condition import remap_expression, validate_expression

            # 模板阶段按步骤 key 引用，校验时顶上占位 uuid
            validate_expression(remap_expression(
                self.expression, lambda _key: _TEMPLATE_NODE_PLACEHOLDER, reference_key="node_key"))
        if self.carry is not None:
            if not self.carry or len(self.carry) > MAX_CARRY_KEYS:
                raise ValueError(f"carry 必须声明 1 至 {MAX_CARRY_KEYS} 个值")
            if any(not key.isidentifier() for key in self.carry):
                raise ValueError("carry 的键必须是标识符")
        if self.carry_next is not None:
            if self.carry is None:
                raise ValueError("carry_next 需要先声明 carry 初值")
            unknown = sorted(set(self.carry_next) - set(self.carry))
            if unknown:
                raise ValueError(f"carry_next 引用了未声明的 carry：{unknown}")
        return self

    def describe(self) -> str:
        if self.mode == "for":
            count = self.count["parameter"] if isinstance(self.count, dict) else self.count
            return f"for ×{count}"
        if self.condition is None:
            return "while 条件表达式"
        return f"while {self.condition.describe()}"


def loop_expression_references(param: Mapping[str, Any]) -> set[str]:
    """while 条件表达式引用的节点 uuid（可能含循环自己和外层循环：读它们的 carry）。"""

    from unilabos.protocol.runtime.condition import validate_expression

    expression = param.get("expression")
    return validate_expression(expression) if isinstance(expression, Mapping) else set()


def loop_input_references(param: Mapping[str, Any]) -> set[str]:
    """while 条件表达式读取的工作流输入名。"""

    from unilabos.protocol.runtime.condition import condition_input_references

    expression = param.get("expression")
    if not isinstance(expression, Mapping):
        return set()
    return condition_input_references({"version": 1, "expression": dict(expression)})


def remap_loop_param(param: Mapping[str, Any], resolve: Any) -> Dict[str, Any]:
    """组合展开：循环条件里的节点身份换成展开后的身份（carry 里的结果引用另由结果引用重映射）。"""

    from unilabos.protocol.runtime.condition import remap_expression

    result = dict(param)
    if isinstance(result.get("expression"), Mapping):
        result["expression"] = remap_expression(result["expression"], resolve)
    condition = result.get("condition")
    if isinstance(condition, Mapping) and condition.get("source") == "node_output" and condition.get("node_uuid"):
        result["condition"] = {**condition, "node_uuid": resolve(str(condition["node_uuid"]))}
    return result


def compare(op: str, lhs: Any, rhs: Any) -> bool:
    """按运算符比较两个 JSON 值；类型收敛与前端条件分支一致。

    - ``exists``：左值非 None；
    - 数值比较：两侧都能转成数值时按数值比，否则按字符串；
    - 布尔左值：右值接受 true/1/yes/是 等写法；
    - ``contains``：字符串包含，或列表/字典成员。
    """

    if op == "exists":
        return lhs is not None
    if op == "contains":
        if isinstance(lhs, (list, tuple, dict)):
            return rhs in lhs
        return str(rhs) in str(lhs)
    if isinstance(lhs, bool):
        rhs_bool = (
            rhs
            if isinstance(rhs, bool)
            else str(rhs).strip().lower() in {"true", "1", "yes", "是"}
        )
        if op == "==":
            return lhs == rhs_bool
        if op == "!=":
            return lhs != rhs_bool
        lhs, rhs = int(lhs), int(rhs_bool)
    left = _as_number(lhs)
    right = _as_number(rhs)
    if left is None or right is None:
        left, right = str(lhs), str(rhs)
        if op in {">", ">=", "<", "<="}:
            raise ValueError(f"无法对非数值 {lhs!r} 与 {rhs!r} 做 {op} 比较")
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">":
        return left > right  # type: ignore[operator]
    if op == ">=":
        return left >= right  # type: ignore[operator]
    if op == "<":
        return left < right  # type: ignore[operator]
    if op == "<=":
        return left <= right  # type: ignore[operator]
    raise ValueError(f"不支持的比较运算 {op!r}")


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def loop_context(index: int, count: Optional[int]) -> Dict[str, Any]:
    """第 ``index``（0 起）轮的迭代变量。"""

    return {"index": int(index), "iteration": int(index) + 1, "count": count}


def substitute_loop_placeholders(value: Any, context: Mapping[str, Any]) -> Any:
    """递归替换参数里的 ``{{loop.*}}``；整串恰为占位符时保留原始类型。"""

    if isinstance(value, str):
        whole = _WHOLE_PLACEHOLDER.match(value)
        if whole:
            return context.get(whole.group(1))
        if "{{" not in value:
            return value
        return _PLACEHOLDER.sub(
            lambda match: _render(context.get(match.group(1))), value
        )
    if isinstance(value, dict):
        if value.get("kind") == "workflow_literal":
            return value
        return {key: substitute_loop_placeholders(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_loop_placeholders(item, context) for item in value]
    return value


def references_loop_placeholder(value: Any) -> bool:
    """参数里是否用到了迭代变量（校验循环体外的节点不该引用）。"""

    if isinstance(value, str):
        return bool(_PLACEHOLDER.search(value))
    if isinstance(value, dict):
        return any(references_loop_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(references_loop_placeholder(item) for item in value)
    return False


def _render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_loop_spec(param: Optional[Mapping[str, Any]]) -> LoopSpec:
    """从节点 ``param`` 解析循环参数；错误信息带"循环节点"前缀便于前端直接展示。"""

    try:
        return LoopSpec.model_validate(dict(param or {}))
    except ValueError as exc:
        raise ValueError(f"循环节点参数无效：{_first_error(exc)}") from exc


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items: List[Dict[str, Any]] = errors()
        except Exception:  # noqa: BLE001 - 退回默认文案
            items = []
        if items:
            first = items[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            message = str(first.get("msg", ""))
            message = message.removeprefix("Value error, ")
            return f"{location}: {message}" if location else message
    return str(exc)


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "LOOP_ITERATION_TRIGGER",
    "LOOP_NODE_TYPE",
    "LoopCondition",
    "LoopSpec",
    "MAX_CARRY_KEYS",
    "compare",
    "loop_context",
    "loop_expression_references",
    "loop_input_references",
    "parse_loop_spec",
    "remap_loop_param",
    "references_loop_placeholder",
    "substitute_loop_placeholders",
]
