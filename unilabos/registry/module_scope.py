"""工作流（Workflow）/动作（Action）源模块的纯 AST 顶层名称解析。"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Never, Self

_ERROR_MESSAGE = "模块作用域不符合 Workflow 静态解析合同"
_RESOLVED_SCOPE_TOKEN = object()
_SHADOWED_BINDING = "<shadowed>"

DefinitionNode = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


class ModuleScopeError(ValueError):
    """可稳定投影为注册表（Registry）或编译诊断的模块作用域错误。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True, init=False)
class ResolvedModuleScope:
    """一次纯 AST 解析得到的不可变模块顶层绑定快照。"""

    module_name: str
    import_identities: Mapping[str, str]
    definitions: Mapping[str, DefinitionNode]
    annotation_bindings: Mapping[str, str]

    def __new__(cls, *_args: Any, **_kwargs: Any) -> Never:
        raise TypeError("请通过 resolve_module_scope 创建 ResolvedModuleScope")

    @classmethod
    def _from_bindings(
        cls,
        module_name: str,
        import_identities: dict[str, str],
        definitions: dict[str, DefinitionNode],
        shadowed_names: set[str],
        *,
        token: object,
    ) -> Self:
        if token is not _RESOLVED_SCOPE_TOKEN:
            raise TypeError("ResolvedModuleScope 只能由模块内 resolver 创建")
        scope = object.__new__(cls)
        object.__setattr__(scope, "module_name", module_name)
        object.__setattr__(
            scope,
            "import_identities",
            MappingProxyType(dict(import_identities)),
        )
        object.__setattr__(
            scope,
            "definitions",
            MappingProxyType(dict(definitions)),
        )
        annotation_bindings = dict(import_identities)
        annotation_bindings.update(
            (name, _SHADOWED_BINDING) for name in sorted(shadowed_names)
        )
        object.__setattr__(
            scope,
            "annotation_bindings",
            MappingProxyType(annotation_bindings),
        )
        return scope


def _fail(path: str) -> Never:
    raise ModuleScopeError(
        "invalid_module_scope",
        path,
        _ERROR_MESSAGE,
    )


def _dotted_name(value: object, path: str) -> str:
    if type(value) is not str or not value:
        _fail(path)
    parts = value.split(".")
    if any(not part or not part.isidentifier() for part in parts):
        _fail(path)
    return value


def _local_name(value: object, path: str) -> str:
    if type(value) is not str or not value.isidentifier():
        _fail(path)
    return value


def _statement_list(value: object, path: str) -> list[ast.stmt]:
    if type(value) is not list:
        _fail(path)
    for index, statement in enumerate(value):
        if not isinstance(statement, ast.stmt):
            _fail(f"{path}/{index}")
    return value


def _node_list(value: object, node_type: type[ast.AST], path: str) -> list[Any]:
    if type(value) is not list:
        _fail(path)
    for index, node in enumerate(value):
        if not isinstance(node, node_type):
            _fail(f"{path}/{index}")
    return value


def _target_names(target: object, path: str) -> set[str]:
    if not isinstance(target, ast.expr):
        _fail(path)
    if isinstance(target, ast.Name):
        return {_local_name(getattr(target, "id", None), path)}
    if isinstance(target, (ast.Tuple, ast.List)):
        elements = _node_list(getattr(target, "elts", None), ast.expr, path)
        names: set[str] = set()
        for index, element in enumerate(elements):
            names.update(_target_names(element, f"{path}/elts/{index}"))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(getattr(target, "value", None), f"{path}/value")
    if isinstance(target, ast.Attribute):
        return _expression_bindings(getattr(target, "value", None), f"{path}/value")
    if isinstance(target, ast.Subscript):
        names = _expression_bindings(getattr(target, "value", None), f"{path}/value")
        names.update(
            _expression_bindings(getattr(target, "slice", None), f"{path}/slice")
        )
        return names
    _fail(path)


def _delete_target_effects(
    target: object,
    path: str,
) -> tuple[set[str], set[str]]:
    """区分真正删除的名字与 target 求值时产生的绑定。"""

    if not isinstance(target, ast.expr):
        _fail(path)
    if isinstance(target, ast.Name):
        return {_local_name(getattr(target, "id", None), path)}, set()
    if isinstance(target, (ast.Tuple, ast.List)):
        elements = _node_list(getattr(target, "elts", None), ast.expr, path)
        deleted: set[str] = set()
        evaluated: set[str] = set()
        for index, element in enumerate(elements):
            child_deleted, child_evaluated = _delete_target_effects(
                element,
                f"{path}/elts/{index}",
            )
            deleted.update(child_deleted)
            evaluated.update(child_evaluated)
        return deleted, evaluated
    if isinstance(target, ast.Starred):
        return _delete_target_effects(
            getattr(target, "value", None),
            f"{path}/value",
        )
    if isinstance(target, ast.Attribute):
        return set(), _expression_bindings(
            getattr(target, "value", None),
            f"{path}/value",
        )
    if isinstance(target, ast.Subscript):
        evaluated = _expression_bindings(
            getattr(target, "value", None),
            f"{path}/value",
        )
        evaluated.update(
            _expression_bindings(getattr(target, "slice", None), f"{path}/slice")
        )
        return set(), evaluated
    _fail(path)


def _delete_effects(
    statement: ast.Delete,
    path: str,
) -> tuple[set[str], set[str]]:
    targets = _node_list(
        getattr(statement, "targets", None),
        ast.expr,
        f"{path}/targets",
    )
    if not targets:
        _fail(path)
    deleted: set[str] = set()
    evaluated: set[str] = set()
    for index, target in enumerate(targets):
        target_deleted, target_evaluated = _delete_target_effects(
            target,
            f"{path}/targets/{index}",
        )
        deleted.update(target_deleted)
        evaluated.update(target_evaluated)
    return deleted, evaluated


class _NamedExpressionBindings(ast.NodeVisitor):
    """收集当前会执行表达式中的 ``:=`` 模块绑定。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.names: set[str] = set()

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.names.update(
            _target_names(getattr(node, "target", None), f"{self.path}/target")
        )
        value = getattr(node, "value", None)
        if not isinstance(value, ast.expr):
            _fail(f"{self.path}/value")
        self.visit(value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # 默认值在创建 Lambda 时执行；body 不执行。
        arguments = getattr(node, "args", None)
        if not isinstance(arguments, ast.arguments):
            _fail(f"{self.path}/args")
        defaults = _node_list(
            getattr(arguments, "defaults", None),
            ast.expr,
            f"{self.path}/args/defaults",
        )
        for default in defaults:
            self.visit(default)
        keyword_defaults = getattr(arguments, "kw_defaults", None)
        if type(keyword_defaults) is not list:
            _fail(f"{self.path}/args/kw_defaults")
        for default in keyword_defaults:
            if default is not None:
                if not isinstance(default, ast.expr):
                    _fail(f"{self.path}/args/kw_defaults")
                self.visit(default)


def _expression_bindings(value: object, path: str) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, ast.expr):
        _fail(path)
    collector = _NamedExpressionBindings(path)
    collector.visit(value)
    return collector.names


def _alias(alias: object, path: str) -> tuple[str, str | None]:
    if not isinstance(alias, ast.alias):
        _fail(path)
    name = _dotted_name(getattr(alias, "name", None), path)
    asname_value = getattr(alias, "asname", None)
    if asname_value is None:
        return name, None
    return name, _local_name(asname_value, path)


def _import_bindings(statement: ast.Import, path: str) -> dict[str, str]:
    aliases = _node_list(getattr(statement, "names", None), ast.alias, f"{path}/names")
    if not aliases:
        _fail(path)
    bindings: dict[str, str] = {}
    for index, alias_node in enumerate(aliases):
        alias_path = f"{path}/names/{index}"
        name, asname = _alias(alias_node, alias_path)
        if asname is None:
            local_name = name.split(".", 1)[0]
            identity = local_name
        else:
            local_name = asname
            identity = name
        bindings[local_name] = identity
    return bindings


def _from_import_bindings(
    statement: ast.ImportFrom,
    path: str,
) -> dict[str, str]:
    level = getattr(statement, "level", None)
    if type(level) is not int or level != 0:
        _fail(path)
    module_name = _dotted_name(getattr(statement, "module", None), path)
    aliases = _node_list(getattr(statement, "names", None), ast.alias, f"{path}/names")
    if not aliases:
        _fail(path)

    bindings: dict[str, str] = {}
    for index, alias_node in enumerate(aliases):
        alias_path = f"{path}/names/{index}"
        name_value = getattr(alias_node, "name", None)
        if name_value == "*":
            _fail(alias_path)
        name, asname = _alias(alias_node, alias_path)
        if "." in name:
            _fail(alias_path)
        bindings[asname or name] = f"{module_name}:{name}"
    if module_name == "__future__":
        return {}
    return bindings


def _pattern_names(pattern: object, path: str) -> set[str]:
    if not isinstance(pattern, ast.pattern):
        _fail(path)
    if isinstance(pattern, (ast.MatchValue, ast.MatchSingleton)):
        return set()
    if isinstance(pattern, ast.MatchSequence):
        patterns = _node_list(getattr(pattern, "patterns", None), ast.pattern, path)
        names: set[str] = set()
        for index, child in enumerate(patterns):
            names.update(_pattern_names(child, f"{path}/patterns/{index}"))
        return names
    if isinstance(pattern, ast.MatchMapping):
        patterns = _node_list(getattr(pattern, "patterns", None), ast.pattern, path)
        names = set()
        for index, child in enumerate(patterns):
            names.update(_pattern_names(child, f"{path}/patterns/{index}"))
        rest = getattr(pattern, "rest", None)
        if rest is not None:
            names.add(_local_name(rest, f"{path}/rest"))
        return names
    if isinstance(pattern, ast.MatchClass):
        positional = _node_list(getattr(pattern, "patterns", None), ast.pattern, path)
        keyword = _node_list(getattr(pattern, "kwd_patterns", None), ast.pattern, path)
        names = set()
        for index, child in enumerate([*positional, *keyword]):
            names.update(_pattern_names(child, f"{path}/patterns/{index}"))
        return names
    if isinstance(pattern, ast.MatchStar):
        name = getattr(pattern, "name", None)
        return set() if name is None else {_local_name(name, f"{path}/name")}
    if isinstance(pattern, ast.MatchAs):
        names = set()
        child = getattr(pattern, "pattern", None)
        if child is not None:
            names.update(_pattern_names(child, f"{path}/pattern"))
        name = getattr(pattern, "name", None)
        if name is not None:
            names.add(_local_name(name, f"{path}/name"))
        return names
    if isinstance(pattern, ast.MatchOr):
        patterns = _node_list(getattr(pattern, "patterns", None), ast.pattern, path)
        names = set()
        for index, child in enumerate(patterns):
            names.update(_pattern_names(child, f"{path}/patterns/{index}"))
        return names
    _fail(path)


def _definition_header_bindings(statement: DefinitionNode, path: str) -> set[str]:
    _statement_list(getattr(statement, "body", None), f"{path}/body")
    names: set[str] = set()
    decorators = _node_list(
        getattr(statement, "decorator_list", None),
        ast.expr,
        f"{path}/decorators",
    )
    for index, decorator in enumerate(decorators):
        names.update(_expression_bindings(decorator, f"{path}/decorators/{index}"))

    if isinstance(statement, ast.ClassDef):
        bases = _node_list(getattr(statement, "bases", None), ast.expr, f"{path}/bases")
        for index, base in enumerate(bases):
            names.update(_expression_bindings(base, f"{path}/bases/{index}"))
        keywords = _node_list(
            getattr(statement, "keywords", None), ast.keyword, f"{path}/keywords"
        )
        for index, keyword in enumerate(keywords):
            names.update(
                _expression_bindings(
                    getattr(keyword, "value", None),
                    f"{path}/keywords/{index}/value",
                )
            )
        return names

    arguments = getattr(statement, "args", None)
    if not isinstance(arguments, ast.arguments):
        _fail(f"{path}/args")
    defaults = _node_list(
        getattr(arguments, "defaults", None), ast.expr, f"{path}/args/defaults"
    )
    for index, default in enumerate(defaults):
        names.update(_expression_bindings(default, f"{path}/args/defaults/{index}"))
    keyword_defaults = getattr(arguments, "kw_defaults", None)
    if type(keyword_defaults) is not list:
        _fail(f"{path}/args/kw_defaults")
    for index, default in enumerate(keyword_defaults):
        names.update(_expression_bindings(default, f"{path}/args/kw_defaults/{index}"))
    argument_groups = (
        getattr(arguments, "posonlyargs", None),
        getattr(arguments, "args", None),
        getattr(arguments, "kwonlyargs", None),
    )
    for group_index, group in enumerate(argument_groups):
        parameters = _node_list(group, ast.arg, f"{path}/args/groups/{group_index}")
        for parameter_index, parameter in enumerate(parameters):
            names.update(
                _expression_bindings(
                    getattr(parameter, "annotation", None),
                    f"{path}/args/groups/{group_index}/{parameter_index}/annotation",
                )
            )
    for attribute in ("vararg", "kwarg"):
        parameter = getattr(arguments, attribute, None)
        if parameter is not None:
            if not isinstance(parameter, ast.arg):
                _fail(f"{path}/args/{attribute}")
            names.update(
                _expression_bindings(
                    getattr(parameter, "annotation", None),
                    f"{path}/args/{attribute}/annotation",
                )
            )
    names.update(
        _expression_bindings(getattr(statement, "returns", None), f"{path}/returns")
    )
    return names


class _ClassGlobalDeclarations(ast.NodeVisitor):
    """收集一个 class code block 直接所属的 ``global`` 声明。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.names: set[str] = set()
        self.nested_bindings: set[str] = set()

    def visit_Global(self, node: ast.Global) -> None:
        names = getattr(node, "names", None)
        if type(names) is not list or not names:
            _fail(self.path)
        for name in names:
            self.names.add(_local_name(name, self.path))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.nested_bindings.update(_class_global_bindings(node, self.path))

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None


def _class_global_bindings(statement: ast.ClassDef, path: str) -> set[str]:
    body = _statement_list(getattr(statement, "body", None), f"{path}/body")
    declarations = _ClassGlobalDeclarations(f"{path}/body")
    for child in body:
        declarations.visit(child)
    if not declarations.names:
        return declarations.nested_bindings

    possible: set[str] = set()
    for index, child in enumerate(body):
        possible.update(_possible_bindings(child, f"{path}/body/{index}"))
    return (declarations.names & possible) | declarations.nested_bindings


def _possible_bindings(statement: ast.stmt, path: str) -> set[str]:
    if isinstance(statement, ast.Import):
        return set(_import_bindings(statement, path))
    if isinstance(statement, ast.ImportFrom):
        return set(_from_import_bindings(statement, path))
    if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        name = _local_name(getattr(statement, "name", None), path)
        return {name, *_definition_header_bindings(statement, path)}
    if isinstance(statement, ast.Assign):
        targets = _node_list(
            getattr(statement, "targets", None), ast.expr, f"{path}/targets"
        )
        if not targets:
            _fail(path)
        names = _expression_bindings(getattr(statement, "value", None), f"{path}/value")
        for index, target in enumerate(targets):
            names.update(_target_names(target, f"{path}/targets/{index}"))
        return names
    if isinstance(statement, ast.AnnAssign):
        names = _target_names(getattr(statement, "target", None), path)
        names.update(
            _expression_bindings(
                getattr(statement, "annotation", None), f"{path}/annotation"
            )
        )
        names.update(
            _expression_bindings(getattr(statement, "value", None), f"{path}/value")
        )
        return names
    if isinstance(statement, ast.AugAssign):
        names = _target_names(getattr(statement, "target", None), path)
        names.update(
            _expression_bindings(getattr(statement, "value", None), f"{path}/value")
        )
        return names
    if isinstance(statement, ast.Delete):
        targets = _node_list(
            getattr(statement, "targets", None), ast.expr, f"{path}/targets"
        )
        if not targets:
            _fail(path)
        names: set[str] = set()
        for index, target in enumerate(targets):
            names.update(_target_names(target, f"{path}/targets/{index}"))
        return names
    if isinstance(statement, ast.Expr):
        return _expression_bindings(getattr(statement, "value", None), f"{path}/value")
    if isinstance(statement, (ast.If, ast.While)):
        names = _expression_bindings(getattr(statement, "test", None), f"{path}/test")
        for attribute in ("body", "orelse"):
            children = _statement_list(
                getattr(statement, attribute, None), f"{path}/{attribute}"
            )
            for index, child in enumerate(children):
                names.update(_possible_bindings(child, f"{path}/{attribute}/{index}"))
        return names
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        names = _target_names(getattr(statement, "target", None), f"{path}/target")
        names.update(
            _expression_bindings(getattr(statement, "iter", None), f"{path}/iter")
        )
        for attribute in ("body", "orelse"):
            children = _statement_list(
                getattr(statement, attribute, None), f"{path}/{attribute}"
            )
            for index, child in enumerate(children):
                names.update(_possible_bindings(child, f"{path}/{attribute}/{index}"))
        return names
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        items = _node_list(
            getattr(statement, "items", None), ast.withitem, f"{path}/items"
        )
        names: set[str] = set()
        for index, item in enumerate(items):
            names.update(
                _expression_bindings(
                    getattr(item, "context_expr", None),
                    f"{path}/items/{index}/context_expr",
                )
            )
            target = getattr(item, "optional_vars", None)
            if target is not None:
                names.update(
                    _target_names(target, f"{path}/items/{index}/optional_vars")
                )
        body = _statement_list(getattr(statement, "body", None), f"{path}/body")
        for index, child in enumerate(body):
            names.update(_possible_bindings(child, f"{path}/body/{index}"))
        return names
    if isinstance(statement, (ast.Try, ast.TryStar)):
        names: set[str] = set()
        for attribute in ("body", "orelse", "finalbody"):
            children = _statement_list(
                getattr(statement, attribute, None), f"{path}/{attribute}"
            )
            for index, child in enumerate(children):
                names.update(_possible_bindings(child, f"{path}/{attribute}/{index}"))
        handlers = _node_list(
            getattr(statement, "handlers", None), ast.ExceptHandler, f"{path}/handlers"
        )
        for index, handler in enumerate(handlers):
            handler_path = f"{path}/handlers/{index}"
            names.update(
                _expression_bindings(
                    getattr(handler, "type", None), f"{handler_path}/type"
                )
            )
            handler_name = getattr(handler, "name", None)
            if handler_name is not None:
                names.add(_local_name(handler_name, f"{handler_path}/name"))
            children = _statement_list(
                getattr(handler, "body", None), f"{handler_path}/body"
            )
            for body_index, child in enumerate(children):
                names.update(
                    _possible_bindings(child, f"{handler_path}/body/{body_index}")
                )
        return names
    if isinstance(statement, ast.Match):
        names = _expression_bindings(
            getattr(statement, "subject", None), f"{path}/subject"
        )
        cases = _node_list(
            getattr(statement, "cases", None), ast.match_case, f"{path}/cases"
        )
        for index, case in enumerate(cases):
            case_path = f"{path}/cases/{index}"
            names.update(
                _pattern_names(getattr(case, "pattern", None), f"{case_path}/pattern")
            )
            names.update(
                _expression_bindings(getattr(case, "guard", None), f"{case_path}/guard")
            )
            children = _statement_list(getattr(case, "body", None), f"{case_path}/body")
            for body_index, child in enumerate(children):
                names.update(
                    _possible_bindings(child, f"{case_path}/body/{body_index}")
                )
        return names
    if isinstance(statement, (ast.Assert, ast.Raise, ast.Return)):
        names: set[str] = set()
        for attribute in ("test", "msg", "exc", "cause", "value"):
            names.update(
                _expression_bindings(
                    getattr(statement, attribute, None), f"{path}/{attribute}"
                )
            )
        return names
    if isinstance(
        statement, (ast.Pass, ast.Break, ast.Continue, ast.Global, ast.Nonlocal)
    ):
        return set()
    _fail(path)


def _clear_binding(
    name: str,
    import_identities: dict[str, str],
    definitions: dict[str, DefinitionNode],
    shadowed_names: set[str],
) -> None:
    import_identities.pop(name, None)
    definitions.pop(name, None)
    shadowed_names.discard(name)


def _shadow_binding(
    name: str,
    import_identities: dict[str, str],
    definitions: dict[str, DefinitionNode],
    shadowed_names: set[str],
) -> None:
    _clear_binding(name, import_identities, definitions, shadowed_names)
    shadowed_names.add(name)


def resolve_module_scope(
    module: ast.Module,
    *,
    module_name: str,
) -> ResolvedModuleScope:
    """解析一个真实模块 AST 的最终、保守且 shadow-aware 的顶层绑定。"""

    if not isinstance(module, ast.Module):
        _fail("/module")
    resolved_module_name = _dotted_name(module_name, "/module/name")
    body = _statement_list(getattr(module, "body", None), "/module/body")

    import_identities: dict[str, str] = {}
    definitions: dict[str, DefinitionNode] = {}
    shadowed_names: set[str] = set()
    for index, statement in enumerate(body):
        path = f"/module/body/{index}"
        if isinstance(statement, ast.Import):
            bindings = _import_bindings(statement, path)
            for local_name, identity in bindings.items():
                _clear_binding(
                    local_name,
                    import_identities,
                    definitions,
                    shadowed_names,
                )
                import_identities[local_name] = identity
            continue
        if isinstance(statement, ast.ImportFrom):
            bindings = _from_import_bindings(statement, path)
            for local_name, identity in bindings.items():
                _clear_binding(
                    local_name,
                    import_identities,
                    definitions,
                    shadowed_names,
                )
                import_identities[local_name] = identity
            continue
        if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            header_bindings = _definition_header_bindings(statement, path)
            for local_name in header_bindings:
                _shadow_binding(
                    local_name,
                    import_identities,
                    definitions,
                    shadowed_names,
                )
            if isinstance(statement, ast.ClassDef):
                for local_name in _class_global_bindings(statement, path):
                    _shadow_binding(
                        local_name,
                        import_identities,
                        definitions,
                        shadowed_names,
                    )
            local_name = _local_name(getattr(statement, "name", None), path)
            _clear_binding(
                local_name,
                import_identities,
                definitions,
                shadowed_names,
            )
            definitions[local_name] = statement
            shadowed_names.add(local_name)
            continue
        if isinstance(statement, ast.Delete):
            deleted_names, evaluated_names = _delete_effects(statement, path)
            for local_name in deleted_names:
                _clear_binding(
                    local_name,
                    import_identities,
                    definitions,
                    shadowed_names,
                )
            for local_name in evaluated_names:
                _shadow_binding(
                    local_name,
                    import_identities,
                    definitions,
                    shadowed_names,
                )
            continue
        possible_bindings = _possible_bindings(statement, path)
        for local_name in possible_bindings:
            _shadow_binding(
                local_name,
                import_identities,
                definitions,
                shadowed_names,
            )

    return ResolvedModuleScope._from_bindings(
        resolved_module_name,
        import_identities,
        definitions,
        shadowed_names,
        token=_RESOLVED_SCOPE_TOKEN,
    )
