"""dev 模板的静态作者编译入口：读取函数 AST，不调用作者函数。"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from unilabos.registry.workflow_helpers import INLINED_MARK
from unilabos.registry.workflows import WorkflowBuildContext, WorkflowStep


class WorkflowSourceError(ValueError):
    """作者源码不在可静态编译的语法子集中；``line`` / ``column``（1 起）指向出错位置。"""

    def __init__(self, message: str, *, line: Optional[int] = None, column: Optional[int] = None) -> None:
        super().__init__(message)
        self.line = line
        self.column = None if column is None else column + 1 if column >= 0 else None


#: 节点身份锚点：紧邻语句上一行的注释 ``# unilab:node_uuid <uuid>``（与 V1 源码格式一致）。
_NODE_ANCHOR = re.compile(r"\s*#\s*unilab:node_uuid\s+([0-9a-fA-F-]{36})\s*")


@dataclass
class StaticWorkflowSource:
    """一份作者源码的静态编译结果：步骤上下文 + 装饰器元数据 + 步骤源码范围与身份锚点。"""

    ctx: WorkflowBuildContext
    function_name: str
    display_name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
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
    常量只认模块级 ``NAME = <JSON 字面量>``；输入输出合同来自签名注解（与设备包 ``@workflow(compiler="ast")`` 同一规则）。
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
        except (ValueError, TypeError, SyntaxError):
            raise WorkflowSourceError("@workflow 参数必须是字面量", line=decorator.lineno) from None
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

    def value(node: ast.AST, results: dict[str, WorkflowStep]) -> Any:
        if isinstance(node, ast.Name):
            if node.id in parameters:
                return {"kind": "workflow_input", "parameter": node.id}
            if node.id in results:
                return dict(results[node.id].output())
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
            return dict(results[current.id].output(".".join(segments)))
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
        if name not in {"run", "run_template", "loop_for", "loop_while", "step_output", "device_state", "group", "parallel", "resource_scope"}:
            return fail(node, "仅支持 ctx 的步骤、循环和条件引用声明")
        if any(item.arg is None for item in node.keywords):
            return fail(node, "不支持关键字参数展开")
        positional = [
            results[item.id] if name == "step_output" and index == 0
            and isinstance(item, ast.Name) and item.id in results else value(item, results)
            for index, item in enumerate(node.args)
        ]
        keywords = {item.arg: value(item.value, results) for item in node.keywords}
        if name in {"run", "run_template"}:
            literal_copy(positional, node)
            literal_copy(keywords, node)
        try:
            result = getattr(ctx, name)(*positional, **keywords)
        except WorkflowSourceError:
            raise
        except ValueError as exc:
            return fail(node, str(exc))
        if isinstance(result, dict):
            # 静态编译只处理普通 JSON 值；带构建期保护的条件对象只给 builder 作者函数用。
            result = dict(result)
        if isinstance(result, WorkflowStep):
            result = replace(result, guards=deepcopy(guards))
            ctx.steps[-1] = result
        return result

    def expression(node: ast.AST, results: dict[str, WorkflowStep]) -> dict[str, Any]:
        if isinstance(node, ast.Name) and node.id in parameters:
            return {"input": node.id}
        if isinstance(node, ast.Name) and node.id in results:
            return {"node_key": results[node.id].key}
        if isinstance(node, ast.Attribute):
            return {"field": expression(node.value, results), "name": node.attr}
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and type(node.slice.value) is str:
            return {"field": expression(node.value, results), "name": node.slice.value}
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

    def body(statements: list[ast.stmt], results: dict[str, WorkflowStep], depth: int = 0) -> None:
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
                                body(branch, dict(results), depth + 1)
                            tails.extend(branch_region.frontier)
                        finally:
                            guards.pop()
                    region.frontier = list(dict.fromkeys(tails))
                continue
            if isinstance(statement, ast.With) and len(statement.items) == 1:
                item = statement.items[0]
                context = method(item.context_expr)
                if context not in {"loop_for", "loop_while", "group", "parallel", "resource_scope"} or item.optional_vars is not None:
                    fail(statement, "仅支持无 as 绑定的 ctx 循环、group 或 parallel")
                with call(item.context_expr, results):
                    if context != "resource_scope":
                        ctx.steps[-1] = replace(ctx.steps[-1], guards=deepcopy(guards))
                        remember(ctx.steps[-1].key, statement)
                    if context == "parallel":
                        exports = {}
                        for branch in statement.body:
                            local = dict(results)
                            # 每个直接成员一个独立分支；只允许读取进入并行块前已有的值。
                            with ctx._sequence():
                                body([branch], local, depth + 1)
                            for key in local.keys() - results.keys():
                                if key in exports:
                                    fail(branch, "并行分支不能声明同名结果")
                                exports[key] = local[key]
                        results.update(exports)
                    elif context in {"group", "resource_scope"}:
                        body(statement.body, results, depth + 1)
                    else:
                        body(statement.body, dict(results), depth + 1)
                continue
            result_name = None
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
                result_name = statement.targets[0].id
                node = statement.value
                if result_name in results or result_name == ctx_name or result_name in constants or result_name in parameters:
                    fail(statement, "动作结果不能覆盖已有名称")
            elif isinstance(statement, ast.Expr):
                node = statement.value
            else:
                fail(statement, "不支持该语句；请用步骤声明、条件和循环上下文")
            if method(node) not in {"run", "run_template"}:
                fail(statement, "仅支持 ctx.run / ctx.run_template 动作声明")
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
