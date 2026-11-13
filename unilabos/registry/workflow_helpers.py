"""静态工作流的辅助函数展开：编译前把辅助函数调用替换成它的源码，绝不执行作者代码。

模板作者常把重复的步骤或参数抽成辅助函数，例如::

    def _reset(ctx):                     # 步骤辅助函数：第一个实参是 ctx
        ctx.run("reactor/reset", {}, name="复位")

    def _water(quantity):                # 值辅助函数：函数体只有一句 return
        return {"key": "water", "kind": "lot", "lot_uuid": WATER_LOT_UUID, "quantity": quantity}

    @workflow(display_name="加水")
    def add_water(ctx):
        _reset(ctx)
        ctx.run("dispenser/dispense", {}, inventory=[_water(500)])

展开在 AST 上完成：步骤辅助函数的调用语句替换成函数体语句（``x = helper(ctx)`` 取函数末尾
``return`` 的步骤），值辅助函数的调用替换成代入实参后的返回表达式。辅助函数的形参换成实参
表达式，局部名改成不会冲突的新名字，引用的模块常量并入常量表（同名不同值时改名）。辅助函数
可以嵌套调用其它辅助函数，也可以从其它模块导入。展开后的语句位置记为调用处，报错与步骤源码
范围都指向模板里的那一行。
"""

from __future__ import annotations

import ast
import inspect
import itertools
import textwrap
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

#: 辅助函数最多嵌套这么多层（也用于发现递归）。
MAX_HELPER_DEPTH = 8

#: 展开得到的语句带这个标记：节点 UUID 锚点只认模板里亲手写的语句。
INLINED_MARK = "_unilab_inlined"


class HelperError(ValueError):
    """辅助函数无法静态展开；``node`` 指向模板里的调用处。"""

    def __init__(self, message: str, node: ast.AST) -> None:
        super().__init__(message)
        self.node = node


@dataclass(frozen=True)
class Helper:
    name: str
    function: ast.FunctionDef
    scope: "HelperScope"


@dataclass(frozen=True)
class HelperScope:
    """一个模块里可见的名字：常量与辅助函数（按名字解析出函数源码）。"""

    constants: dict[str, Any]
    resolve: Callable[[str], Optional[Helper]]


def live_scope(fn: Callable[..., Any]) -> HelperScope:
    """已导入函数的作用域：只看函数实际引用的全局 / 闭包名，辅助函数读源码、不调用。"""

    closure = inspect.getclosurevars(fn)
    names = {**closure.globals, **closure.nonlocals}

    def resolve(name: str) -> Optional[Helper]:
        target = names.get(name)
        if not inspect.isfunction(target) or getattr(target, "_workflow_registry_meta", None) is not None:
            return None
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(target)))
        except (OSError, TypeError, SyntaxError):
            return None
        function = tree.body[0] if tree.body else None
        if not isinstance(function, ast.FunctionDef):
            return None  # lambda 等不能当作辅助函数
        return Helper(name, function, live_scope(target))

    return HelperScope(names, resolve)


def source_scope(tree: ast.Module, constants: dict[str, Any]) -> HelperScope:
    """源码文本的作用域：同一文件里的顶层函数（不含 @workflow 函数）可作辅助函数。"""

    from unilabos.registry.workflow_ast import _is_workflow_decorator

    functions = {
        item.name: item for item in tree.body
        if isinstance(item, ast.FunctionDef) and not any(_is_workflow_decorator(dec) for dec in item.decorator_list)
    }

    def resolve(name: str) -> Optional[Helper]:
        return Helper(name, functions[name], scope) if name in functions else None

    scope = HelperScope(constants, resolve)
    return scope


def inline_helpers(function: ast.FunctionDef, scope: HelperScope, constants: dict[str, Any]) -> ast.FunctionDef:
    """返回展开全部辅助函数后的函数 AST（新对象）；辅助函数用到的常量原地并入 ``constants``。"""

    expanded = deepcopy(function)
    inliner = _Inliner(ctx_name=function.args.args[0].arg, scope=scope, constants=constants,
                       counter=itertools.count(1), stack=())
    expanded.body = inliner.statements(expanded.body)
    return expanded


def _body_without_docstring(function: ast.FunctionDef) -> list[ast.stmt]:
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def _walk_names(nodes: list[ast.AST]) -> Iterator[ast.Name]:
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                yield child


class _Substitute(ast.NodeTransformer):
    """形参 → 实参表达式；局部名 / 冲突常量 → 新名字。"""

    def __init__(self, expressions: dict[str, ast.expr], renames: dict[str, str]) -> None:
        self.expressions = expressions
        self.renames = renames

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.expressions:
            if not isinstance(node.ctx, ast.Load):
                raise HelperError(f"辅助函数不能给形参 {node.id} 重新赋值", node)
            return ast.copy_location(deepcopy(self.expressions[node.id]), node)
        if node.id in self.renames:
            return ast.copy_location(ast.Name(id=self.renames[node.id], ctx=node.ctx), node)
        return node


def _relocate(nodes: list[ast.AST], site: ast.AST) -> None:
    for node in nodes:
        for child in ast.walk(node):
            if "lineno" in child._attributes:
                ast.copy_location(child, site)
                child.end_lineno = getattr(site, "end_lineno", None)
                child.end_col_offset = getattr(site, "end_col_offset", None)
            if isinstance(child, ast.stmt):
                setattr(child, INLINED_MARK, True)


class _Inliner:
    def __init__(self, *, ctx_name: Optional[str], scope: HelperScope, constants: dict[str, Any],
                 counter: Iterator[int], stack: tuple[str, ...]) -> None:
        self.ctx_name = ctx_name
        self.scope = scope
        self.constants = constants
        self.counter = counter
        self.stack = stack

    # ── 语句 ──────────────────────────────────────────────────

    def statements(self, statements: list[ast.stmt]) -> list[ast.stmt]:
        result: list[ast.stmt] = []
        for statement in statements:
            result.extend(self.statement(statement))
        return result

    def statement(self, statement: ast.stmt) -> list[ast.stmt]:
        if isinstance(statement, ast.If):
            statement.test = self.expression(statement.test)
            statement.body = self.statements(statement.body)
            statement.orelse = self.statements(statement.orelse)
            return [statement]
        if isinstance(statement, ast.With):
            for item in statement.items:
                item.context_expr = self.expression(item.context_expr)
            statement.body = self.statements(statement.body)
            return [statement]
        if isinstance(statement, ast.Expr) and (helper := self._step_helper(statement.value)) is not None:
            return self._expand_steps(helper, statement.value, target=None, site=statement)
        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and (helper := self._step_helper(statement.value)) is not None):
            return self._expand_steps(helper, statement.value, target=statement.targets[0].id, site=statement)
        if isinstance(statement, (ast.Expr, ast.Assign, ast.Return)) and statement.value is not None:
            statement.value = self.expression(statement.value)
        return [statement]

    def expression(self, node: ast.expr) -> ast.expr:
        inliner = self

        class Values(ast.NodeTransformer):
            def visit_Call(self, call: ast.Call) -> ast.AST:
                self.generic_visit(call)
                helper = inliner._value_helper(call)
                return call if helper is None else inliner._expand_value(helper, call)

        return Values().visit(node)

    # ── 识别 ──────────────────────────────────────────────────

    def _helper(self, call: ast.AST) -> Optional[Helper]:
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            return None
        return self.scope.resolve(call.func.id)

    def _passes_ctx(self, call: ast.Call) -> bool:
        values = [*call.args, *(keyword.value for keyword in call.keywords)]
        return any(isinstance(item, ast.Name) and item.id == self.ctx_name for item in values)

    def _step_helper(self, call: ast.AST) -> Optional[Helper]:
        helper = self._helper(call)
        return helper if helper is not None and self._passes_ctx(call) else None

    def _value_helper(self, call: ast.Call) -> Optional[Helper]:
        helper = self._helper(call)
        if helper is None or self._passes_ctx(call):
            return None
        body = _body_without_docstring(helper.function)
        if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
            raise HelperError(f"辅助函数 {helper.name} 不接收 ctx，只能是一句 return 表达式", call)
        return helper

    # ── 展开 ──────────────────────────────────────────────────

    def _enter(self, helper: Helper, call: ast.Call) -> None:
        if helper.name in self.stack:
            raise HelperError(f"辅助函数 {helper.name} 递归调用，无法静态展开", call)
        if len(self.stack) >= MAX_HELPER_DEPTH:
            raise HelperError(f"辅助函数嵌套超过 {MAX_HELPER_DEPTH} 层", call)

    def _bind(self, helper: Helper, call: ast.Call) -> dict[str, ast.expr]:
        args = helper.function.args
        if args.vararg or args.kwarg or args.posonlyargs:
            raise HelperError(f"辅助函数 {helper.name} 不能使用 *args / **kwargs / 仅位置参数", call)
        if any(item.arg is None for item in call.keywords) or any(isinstance(item, ast.Starred) for item in call.args):
            raise HelperError(f"调用辅助函数 {helper.name} 不支持参数展开", call)
        positional = [item.arg for item in args.args]
        keyword_only = [item.arg for item in args.kwonlyargs]
        if len(call.args) > len(positional):
            raise HelperError(f"辅助函数 {helper.name} 的实参多于形参", call)
        bound: dict[str, ast.expr] = dict(zip(positional, call.args))
        for keyword in call.keywords:
            if keyword.arg not in positional and keyword.arg not in keyword_only:
                raise HelperError(f"辅助函数 {helper.name} 没有形参 {keyword.arg}", call)
            if keyword.arg in bound:
                raise HelperError(f"辅助函数 {helper.name} 的形参 {keyword.arg} 重复赋值", call)
            bound[keyword.arg] = keyword.value
        defaults = dict(zip(positional[len(positional) - len(args.defaults):], args.defaults))
        defaults.update({item.arg: value for item, value in zip(args.kwonlyargs, args.kw_defaults) if value is not None})
        for name in (*positional, *keyword_only):
            if name not in bound:
                if name not in defaults:
                    raise HelperError(f"调用辅助函数 {helper.name} 缺少实参 {name}", call)
                bound[name] = ("default", defaults[name])  # type: ignore[assignment]
        return bound

    def _import_free_names(self, helper: Helper, source: dict[str, Any], nodes: list[ast.AST],
                           bound: set[str], locals_: set[str]) -> dict[str, str]:
        """辅助函数（及其展开的嵌套辅助函数）引用的常量并入调用方常量表。

        调用方已有同一对象时沿用原名；否则换成带辅助函数名的新名字——原名可能与调用方的
        常量或之后声明的动作结果同名。
        """

        renames: dict[str, str] = {}
        for node in _walk_names(nodes):
            name = node.id
            if name in bound or name in locals_ or name in renames or name not in source:
                continue
            value = source[name]
            if name in self.constants and self.constants[name] is value:
                continue
            alias = f"_{helper.name}_{name.lstrip('_')}_{next(self.counter)}"
            self.constants[alias] = value
            renames[name] = alias
        return renames

    def _child(self, helper: Helper, ctx_name: Optional[str]) -> "_Inliner":
        return _Inliner(ctx_name=ctx_name, scope=helper.scope, constants=dict(helper.scope.constants),
                        counter=self.counter, stack=(*self.stack, helper.name))

    def _arguments(self, helper: Helper, call: ast.Call, bound: dict[str, Any]) -> dict[str, ast.expr]:
        """实参在调用方展开；缺省值在辅助函数自己的作用域里展开并引入常量。"""

        values: dict[str, ast.expr] = {}
        for name, item in bound.items():
            if isinstance(item, tuple) and item[0] == "default":
                child = self._child(helper, None)
                default = child.expression(deepcopy(item[1]))
                default = _Substitute({}, self._import_free_names(helper, child.constants, [default], set(), set())).visit(default)
                values[name] = default
            else:
                values[name] = self.expression(item)
        return values

    def _expand_value(self, helper: Helper, call: ast.Call) -> ast.expr:
        self._enter(helper, call)
        bound = self._bind(helper, call)
        values = self._arguments(helper, call, bound)
        returned = deepcopy(_body_without_docstring(helper.function)[0].value)
        child = self._child(helper, None)
        returned = child.expression(returned)
        renames = self._import_free_names(helper, child.constants, [returned], set(values), set())
        result = _Substitute(values, renames).visit(returned)
        _relocate([result], call)
        return result

    def _expand_steps(self, helper: Helper, call: ast.Call, *, target: Optional[str], site: ast.stmt) -> list[ast.stmt]:
        self._enter(helper, call)
        bound = self._bind(helper, call)
        ctx_params = [name for name, item in bound.items()
                      if isinstance(item, ast.Name) and item.id == self.ctx_name]
        if len(ctx_params) != 1:
            raise HelperError(f"调用辅助函数 {helper.name} 时 ctx 只能传给一个形参", call)
        ctx_param = ctx_params[0]
        values = self._arguments(helper, call, {name: item for name, item in bound.items() if name != ctx_param})
        child = self._child(helper, ctx_param)
        body = child.statements(deepcopy(_body_without_docstring(helper.function)))

        returned: Optional[ast.expr] = None
        if body and isinstance(body[-1], ast.Return):
            returned = body.pop().value
        if any(isinstance(node, ast.Return) for statement in body for node in ast.walk(statement)):
            raise HelperError(f"辅助函数 {helper.name} 的 return 只能写在函数末尾", call)

        locals_ = {
            node.id for statement in body for node in ast.walk(statement)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        renames = {name: f"_{helper.name}_{name}_{next(self.counter)}" for name in locals_}
        tail: list[ast.stmt] = []
        if target is not None:
            if isinstance(returned, ast.Name) and returned.id in locals_:
                renames[returned.id] = target  # 返回的步骤直接用调用方的名字
            elif isinstance(returned, ast.Call):
                tail.append(ast.Assign(targets=[ast.Name(id=target, ctx=ast.Store())], value=returned))
            else:
                raise HelperError(f"辅助函数 {helper.name} 没有在末尾 return 一个步骤", call)
        elif isinstance(returned, ast.Call):
            tail.append(ast.Expr(value=returned))
        renames[ctx_param] = self.ctx_name or ctx_param
        renames.update(self._import_free_names(helper, child.constants, [*body, *tail], {ctx_param, *values}, locals_))
        substitute = _Substitute(values, renames)
        expanded = [substitute.visit(statement) for statement in (*body, *tail)]
        _relocate(expanded, site)
        return expanded
