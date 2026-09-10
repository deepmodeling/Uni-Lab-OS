"""共享参数注解（parameter annotation）v1 公共接口合同测试。"""

from __future__ import annotations

import ast
import builtins
import importlib
import re
import textwrap
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import pytest

_MODULE_NAME = "unilabos.registry.annotation_schema"
_ABSENT = object()
_STANDARD_RENDER_IMPORTS = {
    "AllowedResourceTemplates": (
        "unilabos.registry.annotations:AllowedResourceTemplates"
    ),
    "Annotated": "typing:Annotated",
    "Field": "pydantic:Field",
    "JSONValue": "unilabos.registry.annotations:JSONValue",
    "Literal": "typing:Literal",
    "ResourceSlot": "unilabos.registry.placeholder_type:ResourceSlot",
}


@dataclass(frozen=True)
class _ExtractedParameter:
    name: str
    annotation: ast.expr
    default: ast.expr | object
    imports: MappingProxyType[str, str]


def _api() -> Any:
    """延迟导入，让缺失模块成为可收集、可计数的目标行为 RED。"""

    return importlib.import_module(_MODULE_NAME)


def _source(
    annotation: str,
    *,
    default: str | object = _ABSENT,
    imports: tuple[str, ...] = (),
) -> str:
    default_source = "" if default is _ABSENT else f" = {default}"
    import_source = "\n".join(imports)
    return (
        f"{import_source}\n\n"
        f"def workflow(value: {annotation}{default_source}):\n"
        "    pass\n"
    )


def _extract_parameter(source: str, name: str = "value") -> _ExtractedParameter:
    """只用 AST 提取 arg/default/import map，与 scanner 的静态输入形状一致。"""

    tree = ast.parse(textwrap.dedent(source))
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                local_name = alias.asname or alias.name
                imports[local_name] = f"{module}:{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name
                imports[local_name] = alias.name

    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    positional = [*function.args.posonlyargs, *function.args.args]
    positional_defaults: dict[str, ast.expr] = {}
    first_default = len(positional) - len(function.args.defaults)
    for index, default in enumerate(function.args.defaults, start=first_default):
        positional_defaults[positional[index].arg] = default
    keyword_defaults = {
        argument.arg: default
        for argument, default in zip(
            function.args.kwonlyargs,
            function.args.kw_defaults,
            strict=True,
        )
        if default is not None
    }
    arguments = [*positional, *function.args.kwonlyargs]
    argument = next(argument for argument in arguments if argument.arg == name)
    assert argument.annotation is not None
    default = positional_defaults.get(
        name,
        keyword_defaults.get(name, _ABSENT),
    )
    return _ExtractedParameter(
        name=name,
        annotation=argument.annotation,
        default=default,
        imports=MappingProxyType(imports),
    )


def _parse(
    source: str,
    *,
    doc_title: str | None = None,
    doc_description: str | None = None,
) -> Any:
    api = _api()
    extracted = _extract_parameter(source)
    default = api.NO_DEFAULT if extracted.default is _ABSENT else extracted.default
    return api.parse_parameter_annotation(
        extracted.name,
        extracted.annotation,
        default=default,
        imports=extracted.imports,
        doc_title=doc_title,
        doc_description=doc_description,
    )


def _descriptor(
    schema: dict[str, object],
    *,
    required: bool = True,
    default: object = _ABSENT,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "name": "value",
        "schema": schema,
        "required": required,
    }
    if default is not _ABSENT:
        result["default"] = default
    if title is not None:
        result["title"] = title
    if description is not None:
        result["description"] = description
    return result


def _render(parameter: object) -> str:
    return ast.unparse(_api().render_parameter_annotation(parameter))


def _assert_round_trip(
    source: str,
    parameter: object,
    *,
    doc_title: str | None = None,
    doc_description: str | None = None,
) -> None:
    api = _api()
    extracted = _extract_parameter(source)
    default = api.NO_DEFAULT if extracted.default is _ABSENT else extracted.default
    imports = dict(_STANDARD_RENDER_IMPORTS)
    imports.update(extracted.imports)
    rendered = api.render_parameter_annotation(parameter)
    reparsed = api.parse_parameter_annotation(
        extracted.name,
        rendered,
        default=default,
        imports=MappingProxyType(imports),
    )
    assert reparsed.to_dict() == parameter.to_dict()
    assert reparsed.resource_templates == parameter.resource_templates
    assert doc_title is None or parameter.to_dict().get("title") == doc_title.strip()
    assert (
        doc_description is None
        or parameter.to_dict().get("description") == doc_description.strip()
    )


def _error_signature(source: str) -> tuple[str, str, str]:
    api = _api()
    with pytest.raises(api.AnnotationSchemaError) as caught:
        _parse(source)
    error = caught.value
    assert isinstance(error.code, str) and error.code
    assert isinstance(error.path, str) and error.path.startswith("/")
    assert isinstance(error.message, str) and error.message
    assert re.search(r"[\u4e00-\u9fff]", error.message), "错误消息必须是简体中文"
    return error.code, error.path, error.message


def _assert_stable_error(source: str) -> None:
    assert _error_signature(source) == _error_signature(source)


_ACCEPTED_TYPES = [
    pytest.param("str", (), {"type": "string"}, "str", id="string"),
    pytest.param("int", (), {"type": "integer"}, "int", id="integer"),
    pytest.param("float", (), {"type": "number"}, "float", id="number"),
    pytest.param("bool", (), {"type": "boolean"}, "bool", id="boolean"),
    pytest.param(
        "dict[str, JSONValue]",
        ("from unilabos.registry.annotations import JSONValue",),
        {"type": "object"},
        "dict[str, JSONValue]",
        id="opaque-object",
    ),
    pytest.param(
        "ResourceSlot",
        ("from unilabos.registry.placeholder_type import ResourceSlot",),
        {"$slot": "ResourceSlot"},
        "ResourceSlot",
        id="resource-slot",
    ),
    pytest.param(
        "list[str]",
        (),
        {"type": "array", "items": {"type": "string"}},
        "list[str]",
        id="string-list",
    ),
    pytest.param(
        "list[int]",
        (),
        {"type": "array", "items": {"type": "integer"}},
        "list[int]",
        id="integer-list",
    ),
    pytest.param(
        "list[float]",
        (),
        {"type": "array", "items": {"type": "number"}},
        "list[float]",
        id="number-list",
    ),
    pytest.param(
        "list[bool]",
        (),
        {"type": "array", "items": {"type": "boolean"}},
        "list[bool]",
        id="boolean-list",
    ),
    pytest.param(
        "list[dict[str, JSONValue]]",
        ("from unilabos.registry.annotations import JSONValue",),
        {"type": "array", "items": {"type": "object"}},
        "list[dict[str, JSONValue]]",
        id="opaque-object-list",
    ),
    pytest.param(
        "list[ResourceSlot]",
        ("from unilabos.registry.placeholder_type import ResourceSlot",),
        {"type": "array", "items": {"$slot": "ResourceSlot"}},
        "list[ResourceSlot]",
        id="resource-slot-list",
    ),
    pytest.param(
        "List[str]",
        ("from typing import List",),
        {"type": "array", "items": {"type": "string"}},
        "list[str]",
        id="typing-list-input",
    ),
    pytest.param(
        "Dict[str, JSONValue]",
        (
            "from typing import Dict",
            "from unilabos.registry.annotations import JSONValue",
        ),
        {"type": "object"},
        "dict[str, JSONValue]",
        id="typing-dict-input",
    ),
]


def test_public_no_default_sentinel_is_distinct_from_none() -> None:
    assert _api().NO_DEFAULT is not None


def test_source_annotation_helpers_are_publicly_importable() -> None:
    annotations = importlib.import_module("unilabos.registry.annotations")

    assert annotations.JSONValue is not None
    assert annotations.AllowedResourceTemplates is not None


@pytest.mark.parametrize(
    ("annotation", "imports", "schema", "rendered"),
    _ACCEPTED_TYPES,
)
def test_accepts_complete_v1_type_matrix_and_renders_deterministically(
    annotation: str,
    imports: tuple[str, ...],
    schema: dict[str, object],
    rendered: str,
) -> None:
    source = _source(annotation, imports=imports)
    parameter = _parse(source)

    assert parameter.to_dict() == _descriptor(schema)
    assert parameter.resource_templates == ()
    assert _render(parameter) == rendered
    _assert_round_trip(source, parameter)


@pytest.mark.parametrize(
    ("annotation", "imports", "schema", "rendered"),
    [
        pytest.param(
            "Optional[str]",
            ("from typing import Optional",),
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "str | None",
            id="optional-string",
        ),
        pytest.param(
            "str | None",
            (),
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "str | None",
            id="pep604-string",
        ),
        pytest.param(
            "Optional[ResourceSlot]",
            (
                "from typing import Optional",
                "from unilabos.registry.placeholder_type import ResourceSlot",
            ),
            {"anyOf": [{"$slot": "ResourceSlot"}, {"type": "null"}]},
            "ResourceSlot | None",
            id="optional-resource-slot",
        ),
        pytest.param(
            "list[int] | None",
            (),
            {
                "anyOf": [
                    {"type": "array", "items": {"type": "integer"}},
                    {"type": "null"},
                ]
            },
            "list[int] | None",
            id="pep604-integer-list",
        ),
    ],
)
def test_optional_and_pep604_nullable_are_equivalent(
    annotation: str,
    imports: tuple[str, ...],
    schema: dict[str, object],
    rendered: str,
) -> None:
    source = _source(annotation, default="None", imports=imports)
    parameter = _parse(source)

    assert parameter.to_dict() == _descriptor(
        schema,
        required=False,
        default=None,
    )
    assert _render(parameter) == rendered
    _assert_round_trip(source, parameter)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            _source("Optional[str]", imports=("from typing import Optional",)),
            id="required-nullable",
        ),
        pytest.param(
            _source(
                "Optional[str]",
                default="'not-null'",
                imports=("from typing import Optional",),
            ),
            id="nullable-non-null-default",
        ),
        pytest.param(_source("str", default="None"), id="non-null-none-default"),
        pytest.param(
            _source("list[str | None]", default="[]"),
            id="nullable-list-item",
        ),
        pytest.param(
            _source(
                "Optional[Optional[str]]",
                default="None",
                imports=("from typing import Optional",),
            ),
            id="nested-optional",
        ),
        pytest.param(
            _source(
                "Union[str, int]",
                imports=("from typing import Union",),
            ),
            id="multi-branch-union",
        ),
        pytest.param(
            _source("str | int | None", default="None"),
            id="multi-branch-pep604",
        ),
        pytest.param(
            _source("None | str | None", default="None"),
            id="duplicate-none",
        ),
        pytest.param(
            _source(
                "Optional[str] | None",
                default="None",
                imports=("from typing import Optional",),
            ),
            id="mixed-nested-nullable",
        ),
    ],
)
def test_rejects_illegal_nullable_and_union_shapes(source: str) -> None:
    _assert_stable_error(source)


_LITERAL_CASES = [
    pytest.param(
        "Literal['fast', 'safe']",
        {"type": "string", "enum": ["fast", "safe"]},
        "Literal['fast', 'safe']",
        id="string-family",
    ),
    pytest.param(
        "Literal[True, False]",
        {"type": "boolean", "enum": [True, False]},
        "Literal[True, False]",
        id="boolean-family",
    ),
    pytest.param(
        "Literal[1, 3, -2]",
        {"type": "integer", "enum": [1, 3, -2]},
        "Literal[1, 3, -2]",
        id="integer-family",
    ),
    pytest.param(
        "Literal[1, 2.5, -0.25]",
        {"type": "number", "enum": [1, 2.5, -0.25]},
        "Literal[1, 2.5, -0.25]",
        id="number-widening",
    ),
]


@pytest.mark.parametrize(("annotation", "schema", "rendered"), _LITERAL_CASES)
def test_literal_accepts_four_strict_scalar_families(
    annotation: str,
    schema: dict[str, object],
    rendered: str,
) -> None:
    source = _source(annotation, imports=("from typing import Literal",))
    parameter = _parse(source)

    assert parameter.to_dict() == _descriptor(schema)
    assert _render(parameter) == rendered
    _assert_round_trip(source, parameter)


def test_literal_can_be_list_item_or_top_level_nullable() -> None:
    list_source = _source(
        "list[Literal['left', 'right']]",
        default="['right']",
        imports=("from typing import Literal",),
    )
    list_parameter = _parse(list_source)
    assert list_parameter.to_dict() == _descriptor(
        {
            "type": "array",
            "items": {"type": "string", "enum": ["left", "right"]},
        },
        required=False,
        default=["right"],
    )
    assert _render(list_parameter) == "list[Literal['left', 'right']]"

    nullable_source = _source(
        "Literal[1, 2] | None",
        default="None",
        imports=("from typing import Literal",),
    )
    nullable_parameter = _parse(nullable_source)
    assert nullable_parameter.to_dict() == _descriptor(
        {
            "anyOf": [
                {"type": "integer", "enum": [1, 2]},
                {"type": "null"},
            ]
        },
        required=False,
        default=None,
    )
    assert _render(nullable_parameter) == "Literal[1, 2] | None"


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param("Literal", id="bare-literal"),
        pytest.param("Literal[()]", id="empty-literal"),
        pytest.param("Literal['x', 'x']", id="duplicate-string"),
        pytest.param("Literal[1, 1.0]", id="duplicate-after-number-widening"),
        pytest.param("Literal[True, 1]", id="boolean-is-not-integer"),
        pytest.param("Literal['x', 1]", id="mixed-string-integer"),
        pytest.param("Literal[None]", id="none-member"),
        pytest.param("Literal[1e309]", id="non-finite-number"),
        pytest.param("Literal[float('nan')]", id="call-member"),
        pytest.param("Literal[ResourceSlot]", id="slot-member"),
    ],
)
def test_literal_rejects_empty_duplicate_mixed_or_non_scalar_members(
    annotation: str,
) -> None:
    source = _source(
        annotation,
        imports=(
            "from typing import Literal",
            "from unilabos.registry.placeholder_type import ResourceSlot",
        ),
    )
    _assert_stable_error(source)


_FIELD_IMPORTS = (
    "from typing import Annotated",
    "from pydantic import Field",
)


@pytest.mark.parametrize(
    ("annotation", "schema", "title", "description", "rendered"),
    [
        pytest.param(
            "Annotated[int, Field(ge=1, le=4)]",
            {"type": "integer", "minimum": 1, "maximum": 4},
            None,
            None,
            "Annotated[int, Field(ge=1, le=4)]",
            id="integer-bounds",
        ),
        pytest.param(
            "Annotated[float, Field(ge=-0.5, le=2)]",
            {"type": "number", "minimum": -0.5, "maximum": 2},
            None,
            None,
            "Annotated[float, Field(ge=-0.5, le=2)]",
            id="number-bounds",
        ),
        pytest.param(
            "Annotated[str, Field(min_length=1, max_length=8)]",
            {"type": "string", "minLength": 1, "maxLength": 8},
            None,
            None,
            "Annotated[str, Field(min_length=1, max_length=8)]",
            id="string-length",
        ),
        pytest.param(
            "Annotated[list[str], Field(min_length=0, max_length=3)]",
            {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 0,
                "maxItems": 3,
            },
            None,
            None,
            "Annotated[list[str], Field(min_length=0, max_length=3)]",
            id="list-length",
        ),
        pytest.param(
            (
                "Annotated[int, Field(description='  数量  ', title='  次数  ', "
                "le=9, ge=1)]"
            ),
            {"type": "integer", "minimum": 1, "maximum": 9},
            "次数",
            "数量",
            ("Annotated[int, Field(title='次数', description='数量', ge=1, le=9)]"),
            id="presentation-and-keyword-order",
        ),
    ],
)
def test_field_accepts_closed_constraint_set_and_renders_canonical_order(
    annotation: str,
    schema: dict[str, object],
    title: str | None,
    description: str | None,
    rendered: str,
) -> None:
    source = _source(annotation, imports=_FIELD_IMPORTS)
    parameter = _parse(source)

    assert parameter.to_dict() == _descriptor(
        schema,
        title=title,
        description=description,
    )
    assert _render(parameter) == rendered
    _assert_round_trip(source, parameter)


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param("Annotated[int, Field(1)]", id="positional-field"),
        pytest.param("Annotated[int, Field(*BOUNDS)]", id="star-args"),
        pytest.param("Annotated[int, Field(**OPTIONS)]", id="star-kwargs"),
        pytest.param("Annotated[int, Field(ge=LOWER)]", id="dynamic-bound"),
        pytest.param("Annotated[str, Field(title='  ')]", id="blank-title"),
        pytest.param(
            "Annotated[str, Field(description='  ')]",
            id="blank-description",
        ),
        pytest.param("Annotated[int, Field(ge=True)]", id="boolean-bound"),
        pytest.param("Annotated[int, Field(ge=1.5)]", id="fractional-int-bound"),
        pytest.param("Annotated[float, Field(ge=1e309)]", id="non-finite-bound"),
        pytest.param("Annotated[str, Field(min_length=-1)]", id="negative-length"),
        pytest.param(
            "Annotated[str, Field(min_length=4, max_length=2)]",
            id="reversed-length",
        ),
        pytest.param(
            "Annotated[int, Field(ge=4, le=2)]",
            id="reversed-number-bound",
        ),
        pytest.param("Annotated[str, Field(ge=1)]", id="bound-type-mismatch"),
        pytest.param(
            "Annotated[int, Field(min_length=1)]",
            id="length-type-mismatch",
        ),
        pytest.param("Annotated[int, Field(default=1)]", id="field-default"),
        pytest.param(
            "Annotated[int, Field(default_factory=int)]",
            id="field-default-factory",
        ),
        pytest.param("Annotated[str, Field(alias='x')]", id="field-alias"),
        pytest.param("Annotated[str, Field(pattern='x')]", id="field-pattern"),
        pytest.param(
            "Annotated[int, Field(multiple_of=2)]",
            id="field-multiple-of",
        ),
        pytest.param("Annotated[int, Field(strict=True)]", id="field-strict"),
        pytest.param(
            "Annotated[str, Field(json_schema_extra={})]",
            id="field-json-schema-extra",
        ),
        pytest.param("Annotated[str, Field(unknown=1)]", id="unknown-keyword"),
        pytest.param(
            "Annotated[str, Field(title='a'), Field(description='b')]",
            id="duplicate-field",
        ),
        pytest.param(
            "Annotated[Annotated[str, Field(title='a')], Field(title='a')]",
            id="nested-annotated",
        ),
        pytest.param("Annotated[str, LocalField(title='a')]", id="unproven-field"),
    ],
)
def test_field_rejects_dynamic_mismatched_or_non_v1_metadata(
    annotation: str,
) -> None:
    source = _source(
        annotation,
        imports=(
            *_FIELD_IMPORTS,
            "from local_helpers import LocalField",
            "from local_helpers import BOUNDS, OPTIONS, LOWER",
        ),
    )
    _assert_stable_error(source)


@pytest.mark.parametrize(
    (
        "field_title",
        "field_description",
        "doc_title",
        "doc_description",
        "title",
        "description",
    ),
    [
        pytest.param(
            None,
            None,
            "  文档标题  ",
            "  文档说明  ",
            "文档标题",
            "文档说明",
            id="doc-only",
        ),
        pytest.param(
            "字段标题", "字段说明", None, None, "字段标题", "字段说明", id="field-only"
        ),
        pytest.param(
            "相同标题",
            "相同说明",
            "相同标题",
            "相同说明",
            "相同标题",
            "相同说明",
            id="same",
        ),
        pytest.param(
            "字段胜出",
            "字段说明",
            "文档标题",
            "文档说明",
            "字段胜出",
            "字段说明",
            id="field-wins",
        ),
        pytest.param(None, None, "  ", "", None, None, id="blank-doc-is-absent"),
    ],
)
def test_presentation_precedence_is_field_over_trimmed_doc(
    field_title: str | None,
    field_description: str | None,
    doc_title: str | None,
    doc_description: str | None,
    title: str | None,
    description: str | None,
) -> None:
    keywords = []
    if field_title is not None:
        keywords.append(f"title={field_title!r}")
    if field_description is not None:
        keywords.append(f"description={field_description!r}")
    if keywords:
        annotation = f"Annotated[str, Field({', '.join(keywords)})]"
        imports = _FIELD_IMPORTS
    else:
        annotation = "str"
        imports = ()
    source = _source(annotation, imports=imports)
    parameter = _parse(
        source,
        doc_title=doc_title,
        doc_description=doc_description,
    )

    assert parameter.to_dict() == _descriptor(
        {"type": "string"},
        title=title,
        description=description,
    )
    if title is None and description is None:
        assert _render(parameter) == "str"
    else:
        expected_keywords = []
        if title is not None:
            expected_keywords.append(f"title={title!r}")
        if description is not None:
            expected_keywords.append(f"description={description!r}")
        assert _render(parameter) == (
            f"Annotated[str, Field({', '.join(expected_keywords)})]"
        )
    _assert_round_trip(source, parameter)


_ALLOWED_IMPORTS = (
    "from typing import Annotated",
    "from pydantic import Field",
    "from unilabos.registry.annotations import AllowedResourceTemplates",
    "from unilabos.registry.placeholder_type import ResourceSlot",
    "from lab.resources import plate_96 as plate, tube_rack",
)


@pytest.mark.parametrize(
    ("annotation", "schema", "symbols", "rendered"),
    [
        pytest.param(
            ("Annotated[ResourceSlot, AllowedResourceTemplates(plate, tube_rack)]"),
            {"$slot": "ResourceSlot"},
            [
                ("plate", "lab.resources:plate_96"),
                ("tube_rack", "lab.resources:tube_rack"),
            ],
            ("Annotated[ResourceSlot, AllowedResourceTemplates(plate, tube_rack)]"),
            id="slot-two-symbols",
        ),
        pytest.param(
            ("Annotated[list[ResourceSlot], AllowedResourceTemplates(tube_rack)]"),
            {"type": "array", "items": {"$slot": "ResourceSlot"}},
            [("tube_rack", "lab.resources:tube_rack")],
            ("Annotated[list[ResourceSlot], AllowedResourceTemplates(tube_rack)]"),
            id="slot-list",
        ),
        pytest.param(
            ("Annotated[ResourceSlot | None, AllowedResourceTemplates(plate)]"),
            {"anyOf": [{"$slot": "ResourceSlot"}, {"type": "null"}]},
            [("plate", "lab.resources:plate_96")],
            ("Annotated[ResourceSlot | None, AllowedResourceTemplates(plate)]"),
            id="nullable-slot",
        ),
        pytest.param(
            (
                "Annotated[ResourceSlot, Field(description='  容器  '), "
                "AllowedResourceTemplates(tube_rack, plate)]"
            ),
            {"$slot": "ResourceSlot"},
            [
                ("tube_rack", "lab.resources:tube_rack"),
                ("plate", "lab.resources:plate_96"),
            ],
            (
                "Annotated[ResourceSlot, "
                "AllowedResourceTemplates(tube_rack, plate), "
                "Field(description='容器')]"
            ),
            id="allowed-before-field",
        ),
    ],
)
def test_allowed_resource_templates_are_static_ordered_import_identities(
    annotation: str,
    schema: dict[str, object],
    symbols: list[tuple[str, str]],
    rendered: str,
) -> None:
    default = "None" if "| None" in annotation else _ABSENT
    source = _source(annotation, default=default, imports=_ALLOWED_IMPORTS)
    parameter = _parse(source)

    expected = _descriptor(schema)
    if default is not _ABSENT:
        expected["required"] = False
        expected["default"] = None
    if "description=" in annotation:
        expected["description"] = "容器"
    assert parameter.to_dict() == expected
    assert [
        (symbol.local_name, symbol.qualified_name)
        for symbol in parameter.resource_templates
    ] == symbols
    assert _render(parameter) == rendered
    _assert_round_trip(source, parameter)


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(
            "Annotated[ResourceSlot, AllowedResourceTemplates()]",
            id="empty",
        ),
        pytest.param(
            "Annotated[ResourceSlot, AllowedResourceTemplates(plate, plate)]",
            id="duplicate",
        ),
        pytest.param(
            "Annotated[ResourceSlot, AllowedResourceTemplates('uuid')]",
            id="string-uuid",
        ),
        pytest.param(
            "Annotated[ResourceSlot, AllowedResourceTemplates(resources.plate)]",
            id="attribute",
        ),
        pytest.param(
            "Annotated[ResourceSlot, AllowedResourceTemplates(make_plate())]",
            id="call",
        ),
        pytest.param(
            "Annotated[ResourceSlot, AllowedResourceTemplates(*PLATES)]",
            id="star-args",
        ),
        pytest.param(
            "Annotated[ResourceSlot, AllowedResourceTemplates(**OPTIONS)]",
            id="star-kwargs",
        ),
        pytest.param(
            "Annotated[ResourceSlot, AllowedResourceTemplates(local_plate)]",
            id="unimported-local",
        ),
        pytest.param(
            "Annotated[str, AllowedResourceTemplates(plate)]",
            id="non-slot",
        ),
        pytest.param(
            (
                "Annotated[ResourceSlot, AllowedResourceTemplates(plate), "
                "AllowedResourceTemplates(tube_rack)]"
            ),
            id="duplicate-metadata",
        ),
    ],
)
def test_allowed_resource_templates_reject_invalid_static_shapes(
    annotation: str,
) -> None:
    source = _source(
        annotation,
        imports=(
            *_ALLOWED_IMPORTS,
            "import lab.resources as resources",
            "from local_helpers import make_plate, PLATES, OPTIONS",
        ),
    )
    _assert_stable_error(source)


@pytest.mark.parametrize(
    ("annotation", "default", "schema", "required", "value"),
    [
        pytest.param("str", _ABSENT, {"type": "string"}, True, _ABSENT, id="required"),
        pytest.param("int", "3", {"type": "integer"}, False, 3, id="optional-literal"),
        pytest.param(
            "str | None",
            "None",
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            False,
            None,
            id="optional-nullable",
        ),
        pytest.param(
            "list[ResourceSlot]",
            "[]",
            {"type": "array", "items": {"$slot": "ResourceSlot"}},
            False,
            [],
            id="empty-slot-list",
        ),
        pytest.param(
            "dict[str, JSONValue]",
            "{'nested': [1, True, None]}",
            {"type": "object"},
            False,
            {"nested": [1, True, None]},
            id="json-object",
        ),
    ],
)
def test_default_accepts_only_the_three_contract_shapes(
    annotation: str,
    default: str | object,
    schema: dict[str, object],
    required: bool,
    value: object,
) -> None:
    source = _source(
        annotation,
        default=default,
        imports=(
            "from unilabos.registry.annotations import JSONValue",
            "from unilabos.registry.placeholder_type import ResourceSlot",
        ),
    )
    parameter = _parse(source)

    assert parameter.to_dict() == _descriptor(
        schema,
        required=required,
        default=value,
    )
    _assert_round_trip(source, parameter)


@pytest.mark.parametrize(
    ("annotation", "default"),
    [
        pytest.param(
            "ResourceSlot",
            "{'uuid': '00000000-0000-0000-0000-000000000001'}",
            id="slot",
        ),
        pytest.param(
            "list[ResourceSlot]",
            "[{'uuid': '00000000-0000-0000-0000-000000000001'}]",
            id="nonempty-slot-list",
        ),
        pytest.param("list[int]", "(1, 2)", id="tuple"),
        pytest.param("list[int]", "{1, 2}", id="set"),
        pytest.param("list[str]", "['a'] + ['b']", id="calculation"),
        pytest.param("str", "DEFAULT", id="name"),
        pytest.param("str", "factory()", id="call"),
        pytest.param("list[int]", "[item for item in values]", id="comprehension"),
        pytest.param("float", "1e309", id="non-finite"),
        pytest.param("int", "True", id="bool-is-not-int"),
    ],
)
def test_default_rejects_slot_values_and_non_json_or_executable_ast(
    annotation: str,
    default: str,
) -> None:
    source = _source(
        annotation,
        default=default,
        imports=("from unilabos.registry.placeholder_type import ResourceSlot",),
    )
    _assert_stable_error(source)


def test_parsed_parameter_and_dumps_are_deeply_immutable() -> None:
    source = _source(
        ("Annotated[ResourceSlot, AllowedResourceTemplates(plate, tube_rack)]"),
        imports=_ALLOWED_IMPORTS,
    )
    parameter = _parse(source)
    first = parameter.to_dict()
    first["schema"]["$slot"] = "mutated"
    first["schema"]["extra"] = []
    second = parameter.to_dict()

    assert second == _descriptor({"$slot": "ResourceSlot"})
    assert first is not second
    assert first["schema"] is not second["schema"]
    assert isinstance(parameter.resource_templates, tuple)
    with pytest.raises(AttributeError):
        parameter.resource_templates = ()
    with pytest.raises(AttributeError):
        parameter.resource_templates[0].qualified_name = "mutated:plate"


def test_render_uses_canonical_value_instead_of_the_original_ast() -> None:
    api = _api()
    extracted = _extract_parameter(_source("str"))
    parameter = api.parse_parameter_annotation(
        extracted.name,
        extracted.annotation,
        default=api.NO_DEFAULT,
        imports=extracted.imports,
    )
    assert isinstance(extracted.annotation, ast.Name)
    extracted.annotation.id = "bytes"

    assert _render(parameter) == "str"


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param("Any", id="any"),
        pytest.param("object", id="object"),
        pytest.param("dict", id="bare-dict"),
        pytest.param("dict[str, Any]", id="any-dict-value"),
        pytest.param("list", id="bare-list"),
        pytest.param("tuple[str]", id="tuple"),
        pytest.param("set[str]", id="set"),
        pytest.param("list[list[str]]", id="nested-list"),
        pytest.param("CustomModel", id="custom-model"),
        pytest.param("bytes", id="bytes"),
        pytest.param("datetime", id="datetime"),
        pytest.param("Decimal", id="decimal"),
        pytest.param("list[CustomModel]", id="custom-list-item"),
        pytest.param("module.CustomModel", id="attribute"),
        pytest.param("(lambda: str)()", id="call"),
    ],
)
def test_unsupported_ast_always_returns_stable_annotation_schema_error(
    annotation: str,
) -> None:
    source = _source(
        annotation,
        imports=(
            "from typing import Any",
            "from models import CustomModel",
            "from datetime import datetime",
            "from decimal import Decimal",
            "import models as module",
        ),
    )
    _assert_stable_error(source)


def test_same_named_local_json_and_slot_symbols_are_not_trusted() -> None:
    for annotation in ("dict[str, JSONValue]", "ResourceSlot"):
        _assert_stable_error(_source(annotation))


def test_same_named_local_metadata_symbols_are_not_trusted() -> None:
    source = _source(
        "Annotated[str, Field(title='x')]",
        imports=("from typing import Annotated",),
    )
    _assert_stable_error(source)


def test_parser_neither_imports_modules_nor_executes_author_expressions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api()
    valid = _extract_parameter(
        _source(
            ("Annotated[ResourceSlot, AllowedResourceTemplates(dangerous_plate)]"),
            imports=(
                "from typing import Annotated",
                ("from unilabos.registry.annotations import AllowedResourceTemplates"),
                ("from unilabos.registry.placeholder_type import ResourceSlot"),
                "from package_that_must_not_load import dangerous_plate",
            ),
        )
    )
    executable = _extract_parameter(_source("str", default="detonate()"))
    calls: list[str] = []

    def _forbidden(*args: object, **kwargs: object) -> None:
        calls.append("called")
        raise AssertionError("parser 不得 import、eval、exec 或执行作者表达式")

    monkeypatch.setattr(builtins, "eval", _forbidden)
    monkeypatch.setattr(builtins, "exec", _forbidden)
    monkeypatch.setattr(importlib, "import_module", _forbidden)
    monkeypatch.setattr(builtins, "__import__", _forbidden)

    parameter = api.parse_parameter_annotation(
        valid.name,
        valid.annotation,
        default=api.NO_DEFAULT,
        imports=valid.imports,
    )
    with pytest.raises(api.AnnotationSchemaError):
        api.parse_parameter_annotation(
            executable.name,
            executable.annotation,
            default=executable.default,
            imports=executable.imports,
        )

    assert parameter.to_dict() == _descriptor({"$slot": "ResourceSlot"})
    assert parameter.resource_templates[0].qualified_name == (
        "package_that_must_not_load:dangerous_plate"
    )
    assert calls == []
