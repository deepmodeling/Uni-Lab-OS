"""dev 模板的静态作者编译入口：读取函数 AST，不调用作者函数。"""

from __future__ import annotations

import ast
import inspect
import math
import re
import textwrap
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Union

from unilabos.protocol.runtime.loop import DEFAULT_MAX_ITERATIONS
from unilabos.registry.workflow_helpers import INLINED_MARK
from unilabos.registry.workflows import LOOP_SELF_KEY, WorkflowBuildContext, WorkflowStep


class WorkflowSourceError(ValueError):
    """作者源码不在可静态编译的语法子集中；``line`` / ``column``（1 起）指向出错位置。"""

    def __init__(self, message: str, *, line: Optional[int] = None, column: Optional[int] = None) -> None:
        super().__init__(message)
        self.line = line
        self.column = None if column is None else column + 1 if column >= 0 else None


#: 节点身份锚点：紧邻语句上一行的注释 ``# unilab:node_uuid <uuid>``（与 V1 源码格式一致）。
_NODE_ANCHOR = re.compile(r"\s*#\s*unilab:node_uuid\s+([0-9a-fA-F-]{36})\s*")
#: ``with`` 块里的资源作用域：``ctx.lock`` 是 ``ctx.resource_scope`` 的简写，编译结果相同
_SCOPE_CONTEXTS = frozenset({"resource_scope", "lock"})
#: 产生一个执行步骤（结果可被后面引用）的 ctx 调用：设备动作与人工装入 / 取走 / 补量
_STEP_CALLS = frozenset({"run", "run_template", "material_io"})


@dataclass(frozen=True)
class _Value:
    """源码里的普通变量：字面量、工作流输入引用或结果引用（含循环携带的变量和 for 循环变量）。

    ``unavailable`` 非空时这个名字不能再读（for 循环结束后的循环变量），读取时用它报错。
    """

    value: Any
    unavailable: str = ""


#: 作用域表：名字 → 动作步骤（``x = ctx.run(...)``）或普通变量
_Scope = dict[str, Union[WorkflowStep, _Value]]


def _split_path(path: str) -> list[str]:
    """结果路径 ``a.b\\.c`` → 字段名列表（``\\`` 转义点号与反斜杠）。"""

    segments: list[str] = []
    current: list[str] = []
    escaped = False
    for char in path:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ".":
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
    if path:
        segments.append("".join(current))
    return segments


def _has_reference(item: Any) -> bool:
    if isinstance(item, dict):
        return item.get("kind") in {"node_output", "workflow_input"} or any(_has_reference(child) for child in item.values())
    if isinstance(item, list):
        return any(_has_reference(child) for child in item)
    return False


@dataclass
class StaticWorkflowSource:
    """一份作者源码的静态编译结果：步骤上下文 + 装饰器元数据 + 步骤源码范围与身份锚点。"""

    ctx: WorkflowBuildContext
    function_name: str
    display_name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    #: ``@workflow(allowed_templates=...)``：输入名 → 允许的注册表资源 id（发布时解析成资源模板 uuid）
    allowed_templates: dict[str, list[str]] = field(default_factory=dict)
    #: 步骤 key → (起始行, 起始列, 结束行, 结束列)，行列从 1 开始，列按 UTF-8 字节计
    step_ranges: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    #: 步骤 key → 锚点声明的节点 UUID
    anchors: dict[str, str] = field(default_factory=dict)


def collect_static_workflow(fn: Callable[..., Any]) -> WorkflowBuildContext:
    """只允许声明调用、循环上下文和条件块；绝不 exec/eval 作者源码。"""
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError) as exc:
        raise WorkflowSourceError("静态工作流需要可读取的 .py 函数源码") from exc
    tree = ast.parse(source)
    functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    if len(functions) != 1:
        raise WorkflowSourceError("静态工作流需要一个普通函数")
    function = functions[0]
    from unilabos.registry.workflow_contract import parse_workflow_contract

    try:
        input_contract, output_contract = parse_workflow_contract(fn, function)
    except ValueError as exc:
        raise WorkflowSourceError(f"{fn.__qualname__}：{exc}") from exc
    from unilabos.registry.workflow_helpers import live_scope

    closure = inspect.getclosurevars(fn)
    constants = {**closure.globals, **closure.nonlocals}
    function = _expand_helpers(function, live_scope(fn), constants, qualname=fn.__qualname__)
    return _compile_function(
        function, qualname=fn.__qualname__, input_contract=input_contract,
        output_contract=output_contract, constants=constants, source_lines=source.splitlines(),
    ).ctx


def compile_workflow_source(python_source: str, *, function_name: Optional[str] = None) -> StaticWorkflowSource:
    """作者源码文本 → 静态工作流（工作流作者 / Python 导入用）；只解析 AST，不 import、不执行。

    工作流函数：带 ``@workflow(...)`` 装饰的顶层函数；没有装饰器时取唯一一个首参为 ``ctx`` 的顶层函数。
    常量只认模块级 ``NAME = <JSON 字面量>``；输入输出合同来自签名注解（与设备包 ``@workflow`` 模板同一规则）。
    """

    from unilabos.registry.workflow_contract import module_symbols, parse_workflow_contract_symbols

    if not isinstance(python_source, str):
        raise WorkflowSourceError("源码必须是文本")
    try:
        tree = ast.parse(python_source)
    except SyntaxError as exc:
        raise WorkflowSourceError(
            f"第 {exc.lineno or '?'} 行：Python 语法错误：{exc.msg}", line=exc.lineno,
            column=(exc.offset - 1) if exc.offset else None,
        ) from None
    functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    decorated = [item for item in functions if any(_is_workflow_decorator(dec) for dec in item.decorator_list)]
    if function_name is not None:
        candidates = [item for item in functions if item.name == function_name]
    elif decorated:
        candidates = decorated
    else:
        candidates = [item for item in functions if item.args.args and item.args.args[0].arg == "ctx"]
    if len(candidates) != 1:
        raise WorkflowSourceError("源码必须恰好声明一个 @workflow 工作流函数（首参为 ctx）")
    function = candidates[0]
    imports, records = module_symbols(tree)
    try:
        input_contract, output_contract = parse_workflow_contract_symbols(function, imports=imports, records=records)
    except ValueError as exc:
        raise WorkflowSourceError(f"{function.name}：{exc}", line=function.lineno) from exc
    constants: dict[str, Any] = {}
    for item in tree.body:
        if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
            try:
                constants[item.targets[0].id] = ast.literal_eval(item.value)
            except (ValueError, TypeError, SyntaxError):
                continue
    from unilabos.registry.workflow_helpers import source_scope

    function = _expand_helpers(function, source_scope(tree, constants), constants, qualname=function.name)
    lines = python_source.splitlines()
    result = _compile_function(
        function, qualname=function.name, input_contract=input_contract,
        output_contract=output_contract, constants=constants, source_lines=lines,
    )
    decorator = next((dec for dec in function.decorator_list if _is_workflow_decorator(dec)), None)
    allowed_literal: Any = None
    if isinstance(decorator, ast.Call):
        try:
            if decorator.args:
                result.display_name = str(ast.literal_eval(decorator.args[0]))
            for keyword in decorator.keywords:
                if keyword.arg == "display_name":
                    result.display_name = str(ast.literal_eval(keyword.value))
                elif keyword.arg == "description":
                    result.description = str(ast.literal_eval(keyword.value))
                elif keyword.arg == "tags":
                    result.tags = [str(tag) for tag in ast.literal_eval(keyword.value)]
                elif keyword.arg == "allowed_templates":
                    allowed_node = keyword.value
                    allowed_literal = ast.literal_eval(keyword.value)
        except (ValueError, TypeError, SyntaxError):
            raise WorkflowSourceError("@workflow 参数必须是字面量", line=decorator.lineno) from None
        if allowed_literal is not None:
            from unilabos.registry.workflow_contract import apply_allowed_templates, normalize_allowed_templates

            try:
                result.allowed_templates = normalize_allowed_templates(allowed_literal)
                apply_allowed_templates(input_contract, result.allowed_templates)
            except ValueError as exc:
                raise WorkflowSourceError(
                    f"{function.name}：{exc}", line=allowed_node.lineno, column=allowed_node.col_offset,
                ) from None
    return result


def _expand_helpers(function: ast.FunctionDef, scope: Any, constants: dict[str, Any], *, qualname: str) -> ast.FunctionDef:
    """编译前展开辅助函数调用（只读源码，不执行）；出错位置报在模板里的调用行。"""

    from unilabos.registry.workflow_helpers import HelperError, inline_helpers

    try:
        return inline_helpers(function, scope, constants)
    except HelperError as exc:
        raise WorkflowSourceError(
            f"{qualname} 第 {getattr(exc.node, 'lineno', '?')} 行：{exc}",
            line=getattr(exc.node, "lineno", None), column=getattr(exc.node, "col_offset", None),
        ) from None


def _is_workflow_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (isinstance(target, ast.Name) and target.id == "workflow") or (
        isinstance(target, ast.Attribute) and target.attr == "workflow"
    )


def _compile_function(
    function: ast.FunctionDef,
    *,
    qualname: str,
    input_contract: dict[str, Any],
    output_contract: dict[str, Any],
    constants: dict[str, Any],
    source_lines: list[str],
) -> StaticWorkflowSource:
    args = function.args
    parameters = {item["name"] for item in input_contract["parameters"]}
    ctx_name = args.args[0].arg
    ctx = WorkflowBuildContext()
    ctx.input_contract = input_contract
    ctx.output_contract = output_contract
    ctx.output_bindings = {item["name"]: {"kind": "workflow_input", "parameter": item["name"]}
                           for item in output_contract["outputs"] if item.get("implicit")}
    compiled = StaticWorkflowSource(ctx=ctx, function_name=function.name)

    def remember(key: str, statement: ast.stmt) -> None:
        compiled.step_ranges[key] = (
            statement.lineno, statement.col_offset + 1,
            statement.end_lineno or statement.lineno, (statement.end_col_offset or statement.col_offset) + 1,
        )
        previous = statement.lineno - 2
        if getattr(statement, INLINED_MARK, False):
            return  # 辅助函数展开出的步骤：源码范围记在调用处，节点锚点只认模板里亲手写的语句
        if 0 <= previous < len(source_lines):
            match = _NODE_ANCHOR.fullmatch(source_lines[previous])
            if match is not None:
                identity = match.group(1).lower()
                if identity in compiled.anchors.values():
                    fail(statement, f"节点 UUID 锚点重复：{identity}")
                compiled.anchors[key] = identity
    guards: list[dict[str, Any]] = []
    # 条件名对应源码行，实例化后的稳定身份仍由 dev 模板生成。
    comparator = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}

    def fail(node: ast.AST, message: str) -> Any:
        raise WorkflowSourceError(
            f"{qualname} 第 {getattr(node, 'lineno', '?')} 行：{message}",
            line=getattr(node, "lineno", None), column=getattr(node, "col_offset", None),
        )

    def literal_copy(value: Any, node: ast.AST) -> Any:
        if value is None or type(value) in {str, int, float, bool}:
            return value
        if type(value) in {list, tuple}:
            return [literal_copy(item, node) for item in value]
        if type(value) is dict and all(type(key) is str for key in value):
            return {key: literal_copy(item, node) for key, item in value.items()}
        return fail(node, "常量必须是 JSON 字面量，不能读取对象属性或调用函数")

    def bound(name: str, node: ast.AST, results: _Scope) -> Any:
        """名字当前绑定的值：动作步骤取整个结果引用，普通变量取它的值。"""

        item = results[name]
        if isinstance(item, WorkflowStep):
            return dict(item.output())
        if item.unavailable:
            return fail(node, item.unavailable)
        return deepcopy(item.value)

    def value(node: ast.AST, results: _Scope) -> Any:
        if isinstance(node, ast.Name):
            if node.id in parameters:
                return {"kind": "workflow_input", "parameter": node.id}
            if node.id in results:
                return bound(node.id, node, results)
            if node.id in constants:
                return literal_copy(constants[node.id], node)
            return fail(node, f"未知常量或当前作用域外的动作结果：{node.id}")
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            segments = []
            current = node
            while isinstance(current, (ast.Attribute, ast.Subscript)):
                if isinstance(current, ast.Attribute):
                    segment = current.attr
                    current = current.value
                else:
                    if (not isinstance(current.slice, ast.Constant)
                            or type(current.slice.value) not in {str, int}
                            or isinstance(current.slice.value, int) and current.slice.value < 0):
                        return fail(node, "结果下标只能是字符串或非负整数常量")
                    segment = str(current.slice.value)
                    current = current.value
                if not segment:
                    return fail(node, "结果字段名不能为空")
                segments.insert(0, segment.replace("\\", "\\\\").replace(".", "\\."))
            if not isinstance(current, ast.Name) or current.id not in results:
                return fail(node, "只能读取当前作用域内动作结果的字段")
            base = results[current.id]
            if isinstance(base, WorkflowStep):
                return dict(base.output(".".join(segments)))
            reference = bound(current.id, node, results)
            if not isinstance(reference, dict) or reference.get("kind") != "node_output":
                return fail(node, f"变量 {current.id} 不是动作结果，不能读取字段")
            return {**reference, "data_key": ".".join(item for item in (reference["data_key"], *segments) if item)}
        if isinstance(node, ast.Dict):
            result = {}
            for key, item in zip(node.keys, node.values):
                if key is None:
                    return fail(node, "不支持字典展开")
                resolved = value(key, results)
                if type(resolved) is not str or resolved in result:
                    return fail(node, "字典键必须是唯一字符串")
                result[resolved] = value(item, results)
            return result
        if isinstance(node, (ast.List, ast.Tuple)):
            return [value(item, results) for item in node.elts]
        if isinstance(node, ast.Call) and method(node) in {"step_output", "device_state"}:
            return call(node, results)
        try:
            return literal_copy(ast.literal_eval(node), node)
        except (ValueError, TypeError):
            return fail(node, "参数只支持 JSON 常量和当前作用域的动作结果引用")

    def method(node: ast.AST) -> str:
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == ctx_name):
            return node.func.attr
        return ""

    def call(node: ast.Call, results: dict[str, WorkflowStep]) -> Any:
        name = method(node)
        if name not in {*_STEP_CALLS, "loop_for", "loop_while", "step_output", "device_state", "group", "parallel", *_SCOPE_CONTEXTS}:
            return fail(node, "仅支持 ctx 的步骤、循环和条件引用声明")
        if any(item.arg is None for item in node.keywords):
            return fail(node, "不支持关键字参数展开")
        if name == "material_io" and node.args:
            return fail(node, "ctx.material_io 只接受命名参数（mode=、mount=、site= …）")
        positional = [
            results[item.id] if name == "step_output" and index == 0
            and isinstance(item, ast.Name) and isinstance(results.get(item.id), WorkflowStep) else value(item, results)
            for index, item in enumerate(node.args)
        ]
        keywords = {item.arg: value(item.value, results) for item in node.keywords}
        if name in _STEP_CALLS:
            literal_copy(positional, node)
            literal_copy(keywords, node)
        try:
            result = getattr(ctx, name)(*positional, **keywords)
        except WorkflowSourceError:
            raise
        except ValueError as exc:
            return fail(node, str(exc))
        except TypeError as exc:
            return fail(node, f"ctx.{name} 的参数不对：{exc}")
        if isinstance(result, dict):
            result = dict(result)  # 条件 / 结果引用：拷贝一份，不与 ctx 内部记录共享
        if isinstance(result, WorkflowStep):
            result = replace(result, guards=deepcopy(guards))
            ctx.steps[-1] = result
        return result

    def value_expression(item: Any, node: ast.AST) -> dict[str, Any]:
        """普通变量的值 → 条件表达式（结果引用变成字段链，字面量变成 lit / value）。"""

        if isinstance(item, dict) and item.get("kind") == "workflow_input":
            return {"input": item["parameter"]}
        if isinstance(item, dict) and item.get("kind") == "node_output":
            result: dict[str, Any] = {"node_key": item["node_key"]}
            for segment in _split_path(item["data_key"]):
                result = {"field": result, "name": segment}
            return result
        if item is None or type(item) in {bool, str, int} or (type(item) is float and math.isfinite(item)):
            return {"lit": item}
        if _has_reference(item):
            return fail(node, "条件里不能读取含结果引用的容器变量，请直接读取其中的字段")
        return {"value": item}

    def expression(node: ast.AST, results: _Scope) -> dict[str, Any]:
        if isinstance(node, ast.Name) and node.id in parameters:
            return {"input": node.id}
        if isinstance(node, ast.Name) and isinstance(results.get(node.id), WorkflowStep):
            return {"node_key": results[node.id].key}
        if isinstance(node, ast.Name) and node.id in results:
            return value_expression(bound(node.id, node, results), node)
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            if isinstance(node, ast.Subscript) and not (isinstance(node.slice, ast.Constant)
                                                        and type(node.slice.value) is str):
                return {"lit": value(node, results)}
            inner = expression(node.value, results)
            if set(inner) == {"lit"}:
                return fail(node, "条件里不能读取字面量的字段")
            name = node.attr if isinstance(node, ast.Attribute) else node.slice.value
            return {"field": inner, "name": name}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return {"not": expression(node.operand, results)}
        if isinstance(node, ast.BoolOp):
            return {"op": "and" if isinstance(node.op, ast.And) else "or",
                    "args": [expression(item, results) for item in node.values]}
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            parts = []
            for index, op in enumerate(node.ops):
                if type(op) not in comparator:
                    return fail(node, "条件比较支持 == != < <= > >=")
                parts.append({"op": comparator[type(op)], "args": [expression(operands[index], results), expression(operands[index + 1], results)]})
            return parts[0] if len(parts) == 1 else {"op": "and", "args": parts}
        return {"lit": value(node, results)}

    # 正在编译循环体的 for 循环变量：循环体里不能改写
    for_variables: set[str] = set()

    def assigned_names(statements: list[ast.stmt]) -> list[str]:
        """循环体（含嵌套分支、分组和内层循环）里被赋值的名字，按首次出现排序；for 循环变量不算。"""

        names: list[str] = []
        for statement in statements:
            if isinstance(statement, ast.Assign):
                names.extend(target.id for target in statement.targets if isinstance(target, ast.Name))
            elif isinstance(statement, (ast.If, ast.While, ast.For)):
                names.extend(assigned_names([*statement.body, *statement.orelse]))
            elif isinstance(statement, ast.With):
                names.extend(assigned_names(statement.body))
        return list(dict.fromkeys(names))

    def assignment(node: ast.AST, results: _Scope) -> Any:
        """``x = <值>``：动作结果字段、字面量、工作流输入或其他变量；不做运算。"""

        if isinstance(node, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.JoinedStr)):
            try:
                return literal_copy(ast.literal_eval(node), node)  # -1 这类字面量
            except (ValueError, TypeError, SyntaxError):
                return fail(node, f"不支持运算：{ast.unparse(node)}。变量只能赋值为动作结果的字段、字面量、"
                                  "工作流输入或其他变量；需要计算的值请让设备动作返回")
        if isinstance(node, ast.Call):
            return fail(node, f"变量只能赋值为动作结果的字段、字面量、工作流输入或其他变量，不能调用 {ast.unparse(node.func)}")
        return value(node, results)

    def range_count(node: ast.AST, results: _Scope) -> Any:
        """``range`` 的实参：整数字面量（常量、保存字面量的变量）或工作流输入。"""

        if isinstance(node, ast.Name) and node.id in parameters:
            return {"kind": "workflow_input", "parameter": node.id}
        try:
            item = value(node, results)
        except WorkflowSourceError:
            item = None
        if type(item) is int:
            return item
        if isinstance(item, dict) and item.get("kind") == "workflow_input":
            return item
        return fail(node, "range 的参数只能是整数字面量或工作流输入")

    def native_loop(statement: Union[ast.While, ast.For], results: _Scope, depth: int, locked: frozenset[str]) -> None:
        """原生 ``while <条件>:`` / ``for i in range(...)``：编译成调度器循环，循环体里改写的外部变量逐轮携带。"""

        if statement.orelse:
            fail(statement, "循环不支持 else 子句")
        spec: dict[str, Any]
        target: Optional[str] = None
        if isinstance(statement, ast.For):
            iterator = statement.iter
            if (not isinstance(iterator, ast.Call) or not isinstance(iterator.func, ast.Name)
                    or iterator.func.id != "range" or iterator.keywords or not 1 <= len(iterator.args) <= 2):
                fail(statement, "for 只支持 range(n) 或 range(start, stop)")
            if not isinstance(statement.target, ast.Name):
                fail(statement, "for 循环变量必须是一个名字")
            target = statement.target.id
            # 上一个循环结束后留下的同名循环变量可以重用（for i ... 之后再 for i ...）
            reusable = isinstance(results.get(target), _Value) and bool(results[target].unavailable)
            if (target in results and not reusable) or target in parameters or target in constants or target == ctx_name:
                fail(statement, f"for 循环变量 {target} 与已有名称冲突")
            if len(iterator.args) == 1:
                count = range_count(iterator.args[0], results)
                spec = {"mode": "for", "count": count}
                label = count["parameter"] if isinstance(count, dict) else count
            else:
                first, stop = (range_count(item, results) for item in iterator.args)
                if not isinstance(first, int) or not isinstance(stop, int):
                    fail(statement, "range(start, stop) 只支持整数字面量")
                spec = {"mode": "for", "count": stop - first, "start": first}
                label = stop - first
            if isinstance(spec["count"], int) and spec["count"] < 1:
                fail(statement, "range 至少要有一轮（一轮都不跑的循环请删掉）")
            spec["interval_seconds"] = 0.0
            name = f"循环 ×{label}"
        else:
            spec = {"mode": "while", "max_iterations": DEFAULT_MAX_ITERATIONS, "interval_seconds": 0.0}
            name = f"循环（第 {statement.lineno} 行）"
        assigned = assigned_names(statement.body)
        if target is not None and target in assigned:
            fail(statement, f"不能在循环体里改写 for 循环变量 {target}")
        # 循环前已有、循环体里重新赋值的名字逐轮携带：循环前的值是初值，本轮末尾的值是下一轮的值
        carried = [key for key in assigned if key in results
                   and not (isinstance(results[key], _Value) and results[key].unavailable)]
        for key in carried:
            if key in locked:
                fail(statement, f"变量 {key} 不能在条件分支、并行分支或 with ctx.loop_* 循环体里改写"
                                "（分支外的取值不确定）；在分支里用新名字，或把赋值移到每轮都会执行的位置")
        if carried:
            spec["carry"] = {key: bound(key, statement, results) for key in carried}

        def carried_scope(loop_key: str) -> _Scope:
            scope = dict(results)
            for key in carried:
                scope[key] = _Value({"kind": "node_output", "node_key": loop_key, "data_key": f"carry.{key}"})
            return scope

        if isinstance(statement, ast.While):
            if any(isinstance(item, ast.Call) and method(item) in {"device_state", "step_output"}
                   for item in ast.walk(statement.test)):
                fail(statement, "while 条件不能读设备状态或用 ctx.step_output；等设备状态请用 "
                                "with ctx.loop_while(ctx.device_state(...), interval_seconds=...)")
            # 条件每轮开始前求值：读到的是本轮携带的值（循环步骤建好后占位 key 换成它自己的 key）
            spec["expression"] = expression(statement.test, carried_scope(LOOP_SELF_KEY))
        index = len(ctx.steps)
        try:
            with ctx._loop(spec, name, ""):
                ctx.steps[-1] = replace(ctx.steps[-1], guards=deepcopy(guards))
                remember(ctx.steps[-1].key, statement)
                loop_key = ctx.steps[index].key
                scope = carried_scope(loop_key)
                if target is not None:
                    scope[target] = _Value({"kind": "node_output", "node_key": loop_key, "data_key": "value"})
                    for_variables.add(target)
                try:
                    body(statement.body, scope, depth + 1, locked)
                finally:
                    if target is not None:
                        for_variables.discard(target)
                updates = {}
                for key in carried:
                    current = bound(key, statement, scope)
                    if current != {"kind": "node_output", "node_key": loop_key, "data_key": f"carry.{key}"}:
                        updates[key] = current
                if updates:
                    ctx.steps[index].params["carry_next"] = updates
        except WorkflowSourceError:
            raise
        except ValueError as exc:
            fail(statement, str(exc))
        # 循环之后：携带的变量是最终值；for 循环变量不再可读
        for key in carried:
            results[key] = _Value({"kind": "node_output", "node_key": loop_key, "data_key": f"carry.{key}"})
        if target is not None:
            results[target] = _Value(None, unavailable=f"for 循环变量 {target} 只能在循环体里读取")

    def body(statements: list[ast.stmt], results: _Scope, depth: int = 0,
             locked: frozenset[str] = frozenset()) -> None:
        """``locked``：在这里不能改写的名字（条件 / 并行分支外、with 循环体外定义的名字）。"""

        if depth > 16:
            fail(function, "控制流嵌套不能超过 16 层")
        for position, statement in enumerate(statements):
            if isinstance(statement, ast.Return):
                if depth != 0 or position != len(statements) - 1:
                    fail(statement, "工作流 return 必须是函数末尾的顶层返回，不能提前结束分支或循环")
                returned = {} if statement.value is None else value(statement.value, results)
                expected = {item["name"] for item in output_contract["outputs"] if not item.get("implicit")}
                if not isinstance(returned, dict) or set(returned) != expected:
                    fail(statement, "返回字典必须与声明的具名结果字段一致")
                for name, binding in returned.items():
                    if not isinstance(binding, dict) or binding.get("kind") not in {"node_output", "workflow_input"}:
                        fail(statement, "返回字段必须引用动作结果或工作流输入")
                    ctx.output_bindings[name] = binding
                continue
            if isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                continue
            if isinstance(statement, (ast.Break, ast.Continue)):
                fail(statement, "不支持 break / continue：用 while 条件结束循环，用 if 跳过本轮的步骤")
            if isinstance(statement, (ast.While, ast.For)):
                native_loop(statement, results, depth, locked)
                continue
            if isinstance(statement, ast.If):
                # 整个条件作为一个分支单元；两条分支都从条件判定处出发。
                with ctx._sequence() as region:
                    step = ctx._record(WorkflowStep(kind="condition", target="", action="",
                        params={"version": 1, "expression": expression(statement.test, results)},
                        name=f"条件（第 {statement.lineno} 行）", guards=deepcopy(guards)))
                    remember(step.key, statement)
                    tails = []
                    for selected, branch in ((True, statement.body), (False, statement.orelse)):
                        guards.append({"node_key": step.key, "equals": selected})
                        try:
                            with ctx._sequence(entry=[step.key]) as branch_region:
                                body(branch, dict(results), depth + 1, locked | set(results))
                            tails.extend(branch_region.frontier)
                        finally:
                            guards.pop()
                    region.frontier = list(dict.fromkeys(tails))
                continue
            if isinstance(statement, ast.With) and len(statement.items) == 1:
                item = statement.items[0]
                context = method(item.context_expr)
                if context not in {"loop_for", "loop_while", "group", "parallel", *_SCOPE_CONTEXTS} or item.optional_vars is not None:
                    fail(statement, "仅支持无 as 绑定的 ctx 循环、group、parallel 或 lock / resource_scope")
                with call(item.context_expr, results):
                    if context not in _SCOPE_CONTEXTS:
                        ctx.steps[-1] = replace(ctx.steps[-1], guards=deepcopy(guards))
                        remember(ctx.steps[-1].key, statement)
                    if context == "parallel":
                        exports = {}
                        for branch in statement.body:
                            local = dict(results)
                            # 每个直接成员一个独立分支；只允许读取进入并行块前已有的值。
                            with ctx._sequence():
                                body([branch], local, depth + 1, locked | set(results))
                            for key in local.keys() - results.keys():
                                if key in exports:
                                    fail(branch, "并行分支不能声明同名结果")
                                exports[key] = local[key]
                        results.update(exports)
                    elif context == "group" or context in _SCOPE_CONTEXTS:
                        body(statement.body, results, depth + 1, locked)
                    else:
                        # with ctx.loop_* 不携带变量：循环体里不能改写循环外的名字
                        body(statement.body, dict(results), depth + 1, locked | set(results))
                continue
            result_name = None
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
                result_name = statement.targets[0].id
                node = statement.value
                if result_name == ctx_name or result_name in constants or result_name in parameters:
                    fail(statement, f"不能改写 {result_name}（ctx、工作流输入或模块常量）；换一个变量名")
                if result_name in for_variables:
                    fail(statement, f"不能在循环体里改写 for 循环变量 {result_name}")
                if result_name in locked:
                    fail(statement, f"变量 {result_name} 不能在条件分支、并行分支或 with ctx.loop_* 循环体里改写"
                                    "（分支外 / 下一轮的取值不确定）；在分支里用新名字，或把赋值移到每轮都会执行的位置")
                if method(node) not in _STEP_CALLS:
                    results[result_name] = _Value(assignment(node, results))
                    continue
            elif isinstance(statement, ast.Expr):
                node = statement.value
            else:
                fail(statement, "不支持该语句；请用步骤声明、变量赋值、条件和循环")
            if method(node) not in _STEP_CALLS:
                fail(statement, "仅支持 ctx.run / ctx.run_template / ctx.material_io 步骤声明")
            step = call(node, results)
            remember(step.key, statement)
            if result_name is not None:
                results[result_name] = step

    body(function.body, {})
    if not ctx.steps:
        raise WorkflowSourceError("静态工作流未声明任何步骤", line=function.lineno)
    if set(ctx.output_bindings) != {item["name"] for item in output_contract["outputs"]}:
        raise WorkflowSourceError("工作流声明了结果但没有在函数末尾返回", line=function.lineno)
    return compiled


def collect_static_steps(fn: Callable[..., Any]) -> list[WorkflowStep]:
    return collect_static_workflow(fn).steps
