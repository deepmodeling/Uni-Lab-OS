"""dev 模板的静态作者编译入口：读取函数 AST，不调用作者函数。"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import Any

from unilabos.registry.workflows import WorkflowBuildContext, WorkflowStep


class WorkflowSourceError(ValueError):
    """作者源码不在可静态编译的语法子集中。"""


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
    args = function.args
    from unilabos.registry.workflow_contract import parse_workflow_contract

    try:
        input_contract, output_contract = parse_workflow_contract(fn, function)
    except ValueError as exc:
        raise WorkflowSourceError(f"{fn.__qualname__}：{exc}") from exc
    parameters = {item["name"] for item in input_contract["parameters"]}
    ctx_name = args.args[0].arg
    closure = inspect.getclosurevars(fn)
    constants = {**closure.globals, **closure.nonlocals}
    ctx = WorkflowBuildContext()
    ctx.input_contract = input_contract
    ctx.output_contract = output_contract
    ctx.output_bindings = {item["name"]: {"kind": "workflow_input", "parameter": item["name"]}
                           for item in output_contract["outputs"] if item.get("implicit")}
    guards: list[dict[str, Any]] = []
    # 条件名对应源码行，实例化后的稳定身份仍由 dev 模板生成。
    comparator = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}

    def fail(node: ast.AST, message: str) -> Any:
        raise WorkflowSourceError(f"{fn.__qualname__} 第 {getattr(node, 'lineno', '?')} 行：{message}")

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
                return results[node.id].output()
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
            return results[current.id].output(".".join(segments))
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
        if name not in {"run", "run_template", "loop_for", "loop_while", "step_output", "device_state", "group", "parallel"}:
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
        result = getattr(ctx, name)(*positional, **keywords)
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
                if context not in {"loop_for", "loop_while", "group", "parallel"} or item.optional_vars is not None:
                    fail(statement, "仅支持无 as 绑定的 ctx 循环、group 或 parallel")
                with call(item.context_expr, results):
                    ctx.steps[-1] = replace(ctx.steps[-1], guards=deepcopy(guards))
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
                    elif context == "group":
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
            if result_name is not None:
                results[result_name] = step

    body(function.body, {})
    if not ctx.steps:
        raise WorkflowSourceError("静态工作流未声明任何步骤")
    if set(ctx.output_bindings) != {item["name"] for item in output_contract["outputs"]}:
        raise WorkflowSourceError("工作流声明了结果但没有在函数末尾返回")
    return ctx


def collect_static_steps(fn: Callable[..., Any]) -> list[WorkflowStep]:
    return collect_static_workflow(fn).steps
