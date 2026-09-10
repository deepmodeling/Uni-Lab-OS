"""动作（Action）单字段结果注解（result annotation）的公共接口合同测试。"""

from __future__ import annotations

import ast
import importlib
import re
import textwrap
from types import MappingProxyType
from typing import Any

import pytest

from unilabos.workflow.schema import parse_output_contract

_MODULE_NAME = "unilabos.registry.annotation_schema"


def _api() -> Any:
    return importlib.import_module(_MODULE_NAME)


def _annotation(
    expression: str,
    *,
    imports: tuple[str, ...] = (),
) -> tuple[ast.expr, MappingProxyType[str, str]]:
    source = "\n".join(
        [
            *imports,
            "",
            f"def action() -> {expression}:",
            "    pass",
        ]
    )
    tree = ast.parse(textwrap.dedent(source))
    import_map: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            module = statement.module or ""
            for alias in statement.names:
                import_map[alias.asname or alias.name] = f"{module}:{alias.name}"
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                import_map[alias.asname or alias.name] = alias.name
    function = next(
        statement for statement in tree.body if isinstance(statement, ast.FunctionDef)
    )
    assert function.returns is not None
    return function.returns, MappingProxyType(import_map)


def _parse(
    name: str,
    expression: str,
    *,
    imports: tuple[str, ...] = (),
) -> Any:
    annotation, import_map = _annotation(expression, imports=imports)
    return _api().parse_result_annotation(
        name,
        annotation,
        imports=import_map,
    )


def _descriptor(
    name: str,
    schema: dict[str, object],
    *,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "name": name,
        "schema": schema,
        "implicit": False,
    }
    if title is not None:
        descriptor["title"] = title
    if description is not None:
        descriptor["description"] = description
    return descriptor


_ACCEPTED_RESULT_TYPES = [
    pytest.param("str", (), {"type": "string"}, id="string"),
    pytest.param("int", (), {"type": "integer"}, id="integer"),
    pytest.param("float", (), {"type": "number"}, id="number"),
    pytest.param("bool", (), {"type": "boolean"}, id="boolean"),
    pytest.param(
        "dict[str, JSONValue]",
        ("from unilabos.registry.annotations import JSONValue",),
        {"type": "object"},
        id="opaque-json-object",
    ),
    pytest.param(
        "ResourceSlot",
        ("from unilabos.registry.placeholder_type import ResourceSlot",),
        {"$slot": "ResourceSlot"},
        id="resource-slot",
    ),
    pytest.param(
        "list[str]",
        (),
        {"type": "array", "items": {"type": "string"}},
        id="string-list",
    ),
    pytest.param(
        "list[int]",
        (),
        {"type": "array", "items": {"type": "integer"}},
        id="integer-list",
    ),
    pytest.param(
        "list[float]",
        (),
        {"type": "array", "items": {"type": "number"}},
        id="number-list",
    ),
    pytest.param(
        "list[bool]",
        (),
        {"type": "array", "items": {"type": "boolean"}},
        id="boolean-list",
    ),
    pytest.param(
        "list[dict[str, JSONValue]]",
        ("from unilabos.registry.annotations import JSONValue",),
        {"type": "array", "items": {"type": "object"}},
        id="json-object-list",
    ),
    pytest.param(
        "list[ResourceSlot]",
        ("from unilabos.registry.placeholder_type import ResourceSlot",),
        {"type": "array", "items": {"$slot": "ResourceSlot"}},
        id="resource-slot-list",
    ),
    pytest.param(
        "List[str]",
        ("from typing import List",),
        {"type": "array", "items": {"type": "string"}},
        id="typing-list-input",
    ),
    pytest.param(
        "Dict[str, JSONValue]",
        (
            "from typing import Dict",
            "from unilabos.registry.annotations import JSONValue",
        ),
        {"type": "object"},
        id="typing-dict-input",
    ),
    pytest.param(
        "str | None",
        (),
        {"anyOf": [{"type": "string"}, {"type": "null"}]},
        id="nullable-string",
    ),
    pytest.param(
        "Optional[ResourceSlot]",
        (
            "from typing import Optional",
            "from unilabos.registry.placeholder_type import ResourceSlot",
        ),
        {"anyOf": [{"$slot": "ResourceSlot"}, {"type": "null"}]},
        id="nullable-resource-slot",
    ),
    pytest.param(
        "Literal['fast', 'safe']",
        ("from typing import Literal",),
        {"type": "string", "enum": ["fast", "safe"]},
        id="literal-string",
    ),
    pytest.param(
        "list[Literal[1, 2.5]]",
        ("from typing import Literal",),
        {
            "type": "array",
            "items": {"type": "number", "enum": [1, 2.5]},
        },
        id="literal-number-list",
    ),
]


@pytest.mark.parametrize(
    ("expression", "imports", "schema"),
    _ACCEPTED_RESULT_TYPES,
)
def test_result_annotation_reuses_complete_finite_type_matrix(
    expression: str,
    imports: tuple[str, ...],
    schema: dict[str, object],
) -> None:
    parsed = _parse("value", expression, imports=imports)

    assert parsed.to_dict() == _descriptor("value", schema)
    assert parsed.resource_templates == ()
    assert "required" not in parsed.to_dict()
    assert "default" not in parsed.to_dict()


def test_result_annotation_reuses_field_presentation_and_constraints() -> None:
    parsed = _parse(
        "amount",
        (
            "Annotated[float, Field(description='  转移体积  ', "
            "title='  体积  ', le=10, ge=0)]"
        ),
        imports=(
            "from typing import Annotated",
            "from pydantic import Field",
        ),
    )

    assert parsed.to_dict() == _descriptor(
        "amount",
        {
            "type": "number",
            "minimum": 0,
            "maximum": 10,
        },
        title="体积",
        description="转移体积",
    )


def test_result_annotation_preserves_static_template_symbol_order() -> None:
    parsed = _parse(
        "sample",
        ("Annotated[ResourceSlot | None, AllowedResourceTemplates(plate, tube)]"),
        imports=(
            "from typing import Annotated",
            ("from unilabos.registry.annotations import AllowedResourceTemplates"),
            "from unilabos.registry.placeholder_type import ResourceSlot",
            "from lab.resources import plate_96 as plate, tube",
        ),
    )

    assert parsed.to_dict() == _descriptor(
        "sample",
        {
            "anyOf": [
                {"$slot": "ResourceSlot"},
                {"type": "null"},
            ]
        },
    )
    assert [
        (symbol.local_name, symbol.qualified_name)
        for symbol in parsed.resource_templates
    ] == [
        ("plate", "lab.resources:plate_96"),
        ("tube", "lab.resources:tube"),
    ]


def _assert_annotation_error(
    operation: Any,
    *,
    path_prefix: str,
) -> None:
    api = _api()
    signatures: list[tuple[str, str, str]] = []
    for _ in range(2):
        with pytest.raises(api.AnnotationSchemaError) as caught:
            operation()
        error = caught.value
        assert isinstance(error.code, str) and error.code
        assert error.path.startswith(path_prefix)
        assert re.search(r"[\u4e00-\u9fff]", error.message)
        signatures.append((error.code, error.path, error.message))
    assert signatures[0] == signatures[1]


@pytest.mark.parametrize(
    ("name", "expression", "imports", "path"),
    [
        pytest.param(
            "value", "Any", ("from typing import Any",), "/annotation", id="any"
        ),
        pytest.param("value", "bytes", (), "/annotation", id="bytes"),
        pytest.param("value", "dict", (), "/annotation", id="bare-dict"),
        pytest.param("value", "list[list[str]]", (), "/annotation", id="nested-list"),
        pytest.param("value", "str | int", (), "/annotation", id="multi-union"),
        pytest.param(
            "value",
            "list[str | None]",
            (),
            "/annotation",
            id="nullable-list-item",
        ),
        pytest.param(
            "value",
            "Annotated[int, Field(default=1)]",
            ("from typing import Annotated", "from pydantic import Field"),
            "/annotation",
            id="field-default",
        ),
        pytest.param(
            "value",
            "Required[str]",
            ("from typing import Required",),
            "/annotation",
            id="required-wrapper",
        ),
        pytest.param(
            "value",
            "ResourceSlot",
            (),
            "/annotation",
            id="unproven-resource-slot",
        ),
        pytest.param("", "str", (), "/", id="empty-name"),
        pytest.param("not-valid", "str", (), "/", id="invalid-name"),
        pytest.param("class", "str", (), "/", id="keyword-name"),
    ],
)
def test_result_annotation_rejects_unsupported_or_invalid_output(
    name: str,
    expression: str,
    imports: tuple[str, ...],
    path: str,
) -> None:
    _assert_annotation_error(
        lambda: _parse(name, expression, imports=imports),
        path_prefix=path,
    )


def test_parsed_result_is_parser_only_and_deeply_immutable() -> None:
    api = _api()
    contract = parse_output_contract(
        {
            "version": 1,
            "outputs": [
                {
                    "name": "sample",
                    "schema": {"$slot": "ResourceSlot"},
                }
            ],
        }
    )
    with pytest.raises(TypeError):
        api.ParsedResult(contract, ())

    parsed = _parse(
        "sample",
        "ResourceSlot",
        imports=("from unilabos.registry.placeholder_type import ResourceSlot",),
    )
    first = parsed.to_dict()
    first["schema"]["$slot"] = "mutated"
    second = parsed.to_dict()

    assert second == _descriptor("sample", {"$slot": "ResourceSlot"})
    assert first is not second
    assert first["schema"] is not second["schema"]
    assert isinstance(parsed.resource_templates, tuple)
    with pytest.raises(AttributeError):
        parsed.resource_templates = ()
