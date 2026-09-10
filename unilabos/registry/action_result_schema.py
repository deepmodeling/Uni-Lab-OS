"""动作具名结果记录（Action named result record）的纯 AST 解析与规范合并。"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Never, Self

from unilabos.registry.annotation_schema import (
    AnnotationSchemaError,
    ResourceTemplateSymbol,
    parse_result_annotation,
)
from unilabos.workflow.schema import (
    WorkflowOutputContract,
    WorkflowSchemaError,
    parse_output_contract,
)

_DATACLASS = "dataclasses:dataclass"
_TYPED_DICT = "typing:TypedDict"
_ERROR_MESSAGE = "Action 结果声明不符合 Workflow 版本 1 合同"
_PARSED_RESULTS_TOKEN = object()


class ActionResultSchemaError(ValueError):
    """可稳定投影为注册表（Registry）或编译诊断的动作（Action）结果声明错误。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True, init=False)
class ParsedActionResults:
    """不保存来源语法形式的规范动作（Action）显式结果合同。"""

    _contract: WorkflowOutputContract
    resource_templates: tuple[
        tuple[str, tuple[ResourceTemplateSymbol, ...]],
        ...,
    ]

    def __new__(cls, *_args: Any, **_kwargs: Any) -> Never:
        raise TypeError(
            "请通过 parse_action_result_declaration 创建 ParsedActionResults"
        )

    @classmethod
    def _from_canonical(
        cls,
        contract: WorkflowOutputContract,
        resource_templates: tuple[
            tuple[str, tuple[ResourceTemplateSymbol, ...]],
            ...,
        ],
        *,
        token: object,
    ) -> Self:
        if token is not _PARSED_RESULTS_TOKEN:
            raise TypeError("ParsedActionResults 只能由模块内 parser 创建")
        results = object.__new__(cls)
        object.__setattr__(results, "_contract", contract)
        object.__setattr__(
            results,
            "resource_templates",
            resource_templates,
        )
        return results

    def to_dict(self) -> dict[str, Any]:
        """返回与 canonical contract 不共享容器的完整 descriptor。"""

        return self._contract.to_dict()


def _fail(path: str) -> Never:
    raise ActionResultSchemaError(
        "invalid_action_result",
        path,
        _ERROR_MESSAGE,
    )


def _is_import(
    node: ast.AST,
    qualified_name: str,
    imports: Mapping[str, str],
) -> bool:
    return (
        isinstance(node, ast.Name)
        and type(getattr(node, "id", None)) is str
        and imports.get(node.id) == qualified_name
    )


def _annotation_error_path(
    field_path: str,
    error: AnnotationSchemaError,
) -> str:
    if error.path.startswith("/outputs/0/name") or error.path in {"", "/"}:
        return f"{field_path}/name"
    if error.path.startswith("/annotation"):
        return f"{field_path}{error.path}"
    return f"{field_path}/annotation{error.path}"


def _parse_field(
    name: str,
    annotation: ast.expr,
    *,
    field_index: int,
    imports: Mapping[str, str],
) -> tuple[
    dict[str, Any],
    tuple[str, tuple[ResourceTemplateSymbol, ...]],
]:
    field_path = f"/return/fields/{field_index}"
    try:
        parsed = parse_result_annotation(
            name,
            annotation,
            imports=imports,
        )
    except AnnotationSchemaError as error:
        _fail(_annotation_error_path(field_path, error))
    except (AttributeError, IndexError, TypeError):
        _fail(f"{field_path}/annotation")
    return (
        parsed.to_dict(),
        (name, parsed.resource_templates),
    )


def _class_fields(
    declaration: ast.ClassDef,
    *,
    imports: Mapping[str, str],
) -> tuple[
    list[dict[str, Any]],
    list[tuple[str, tuple[ResourceTemplateSymbol, ...]]],
]:
    descriptors: list[dict[str, Any]] = []
    templates: list[tuple[str, tuple[ResourceTemplateSymbol, ...]]] = []
    names: set[str] = set()

    if type(getattr(declaration, "body", None)) is not list:
        _fail("/return")
    for body_index, statement in enumerate(declaration.body):
        if (
            body_index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(getattr(statement, "value", None), ast.Constant)
            and type(getattr(statement.value, "value", None)) is str
        ):
            continue
        if len(declaration.body) == 1 and isinstance(statement, ast.Pass):
            continue
        if not isinstance(statement, ast.AnnAssign):
            _fail(f"/return/body/{body_index}")
        target = getattr(statement, "target", None)
        annotation = getattr(statement, "annotation", None)
        if (
            not isinstance(target, ast.Name)
            or type(getattr(target, "id", None)) is not str
            or not isinstance(annotation, ast.expr)
            or getattr(statement, "simple", None) != 1
            or not hasattr(statement, "value")
            or statement.value is not None
        ):
            _fail(f"/return/body/{body_index}")
        field_index = len(descriptors)
        name = target.id
        if name in names:
            _fail(f"/return/fields/{field_index}/name")
        names.add(name)
        descriptor, symbols = _parse_field(
            name,
            annotation,
            field_index=field_index,
            imports=imports,
        )
        descriptors.append(descriptor)
        templates.append(symbols)

    if not descriptors:
        _fail("/return")
    return descriptors, templates


def _parse_typed_dict(
    declaration: ast.ClassDef,
    *,
    imports: Mapping[str, str],
) -> tuple[
    list[dict[str, Any]],
    list[tuple[str, tuple[ResourceTemplateSymbol, ...]]],
]:
    if not declaration.bases:
        _fail("/return")
    for index, base in enumerate(declaration.bases):
        if index > 0 or not _is_import(base, _TYPED_DICT, imports):
            _fail(f"/return/bases/{index}")
    if declaration.keywords:
        _fail("/return")
    if declaration.decorator_list:
        _fail("/return/decorators/0")
    return _class_fields(declaration, imports=imports)


def _parse_dataclass_decorator(
    decorator: ast.expr,
    *,
    imports: Mapping[str, str],
) -> None:
    if (
        not isinstance(decorator, ast.Call)
        or not _is_import(
            getattr(decorator, "func", ast.AST()),
            _DATACLASS,
            imports,
        )
        or type(getattr(decorator, "args", None)) is not list
        or decorator.args
        or type(getattr(decorator, "keywords", None)) is not list
    ):
        _fail("/return/decorators/0")

    seen: set[str] = set()
    for keyword in decorator.keywords:
        if not isinstance(keyword, ast.keyword):
            _fail("/return/decorators/0")
        name = getattr(keyword, "arg", None)
        value = getattr(keyword, "value", None)
        if (
            type(name) is not str
            or name not in {"frozen", "kw_only", "slots"}
            or name in seen
            or not isinstance(value, ast.Constant)
            or getattr(value, "value", None) is not True
        ):
            _fail("/return/decorators/0")
        seen.add(name)
    if "frozen" not in seen:
        _fail("/return/decorators/0")


def _parse_dataclass(
    declaration: ast.ClassDef,
    *,
    imports: Mapping[str, str],
) -> tuple[
    list[dict[str, Any]],
    list[tuple[str, tuple[ResourceTemplateSymbol, ...]]],
]:
    if declaration.bases:
        _fail("/return/bases/0")
    if declaration.keywords:
        _fail("/return")
    if len(declaration.decorator_list) != 1:
        if declaration.decorator_list:
            _fail("/return/decorators/0")
        _fail("/return")
    _parse_dataclass_decorator(
        declaration.decorator_list[0],
        imports=imports,
    )
    return _class_fields(declaration, imports=imports)


def _parse_class(
    declaration: ast.ClassDef,
    *,
    imports: Mapping[str, str],
) -> tuple[
    list[dict[str, Any]],
    list[tuple[str, tuple[ResourceTemplateSymbol, ...]]],
]:
    for attribute in ("bases", "keywords", "body", "decorator_list"):
        if type(getattr(declaration, attribute, None)) is not list:
            _fail("/return")
    if declaration.bases:
        return _parse_typed_dict(declaration, imports=imports)
    return _parse_dataclass(declaration, imports=imports)


def _parse_compat_dict(
    declaration: ast.Dict,
    *,
    imports: Mapping[str, str],
) -> tuple[
    list[dict[str, Any]],
    list[tuple[str, tuple[ResourceTemplateSymbol, ...]]],
]:
    keys = getattr(declaration, "keys", None)
    values = getattr(declaration, "values", None)
    if type(keys) is not list or type(values) is not list:
        _fail("/return")
    if not keys and not values:
        _fail("/return")
    field_count = max(len(keys), len(values))
    descriptors: list[dict[str, Any]] = []
    templates: list[tuple[str, tuple[ResourceTemplateSymbol, ...]]] = []
    names: set[str] = set()
    for index in range(field_count):
        key = keys[index] if index < len(keys) else None
        if (
            not isinstance(key, ast.Constant)
            or type(getattr(key, "value", None)) is not str
        ):
            _fail(f"/return/fields/{index}/name")
        name = key.value
        if name in names:
            _fail(f"/return/fields/{index}/name")
        names.add(name)
        if index >= len(values):
            _fail(f"/return/fields/{index}/annotation")
        value = values[index]
        if not isinstance(value, ast.expr):
            _fail(f"/return/fields/{index}/annotation")
        descriptor, symbols = _parse_field(
            name,
            value,
            field_index=index,
            imports=imports,
        )
        descriptors.append(descriptor)
        templates.append(symbols)
    return descriptors, templates


def _canonical_results(
    descriptors: list[dict[str, Any]],
    templates: list[tuple[str, tuple[ResourceTemplateSymbol, ...]]],
) -> ParsedActionResults:
    try:
        contract = parse_output_contract({"version": 1, "outputs": descriptors})
    except WorkflowSchemaError:
        _fail("/return")
    return ParsedActionResults._from_canonical(
        contract,
        tuple(templates),
        token=_PARSED_RESULTS_TOKEN,
    )


def parse_action_result_declaration(
    declaration: ast.expr | ast.ClassDef | None,
    *,
    imports: Mapping[str, str],
) -> ParsedActionResults:
    """静态解析一个已定位定义节点的动作结果声明（Action result declaration）。"""

    if not isinstance(imports, Mapping):
        _fail("/return")
    if (
        isinstance(declaration, ast.Constant)
        and getattr(declaration, "value", object()) is None
    ):
        return _canonical_results([], [])
    if isinstance(declaration, ast.Dict):
        descriptors, templates = _parse_compat_dict(
            declaration,
            imports=imports,
        )
        return _canonical_results(descriptors, templates)
    if isinstance(declaration, ast.ClassDef):
        descriptors, templates = _parse_class(
            declaration,
            imports=imports,
        )
        return _canonical_results(descriptors, templates)
    _fail("/return")
