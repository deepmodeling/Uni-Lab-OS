"""动作具名结果记录（Action named result record）三种声明形式的公共合同测试。"""

from __future__ import annotations

import ast
import importlib
import re
import textwrap
from types import MappingProxyType
from typing import Any

import pytest

from unilabos.workflow.schema import parse_output_contract

_MODULE_NAME = "unilabos.registry.action_result_schema"
_ERROR_MESSAGE = "Action 结果声明不符合 Workflow 版本 1 合同"

_COMMON_IMPORTS = """
from dataclasses import dataclass
from typing import Annotated, TypedDict
from pydantic import Field
from unilabos.registry.annotations import AllowedResourceTemplates
from unilabos.registry.placeholder_type import ResourceSlot
from lab.resources import plate_96 as plate
"""

_FIELDS = """
    sample: Annotated[
        ResourceSlot,
        AllowedResourceTemplates(plate),
        Field(title="转移后样品"),
    ]
    transferred_volume: Annotated[float, Field(ge=0)]
"""


def _api() -> Any:
    return importlib.import_module(_MODULE_NAME)


def _imports(tree: ast.Module) -> MappingProxyType[str, str]:
    import_map: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            module = statement.module or ""
            for alias in statement.names:
                import_map[alias.asname or alias.name] = f"{module}:{alias.name}"
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                import_map[alias.asname or alias.name] = alias.name
    return MappingProxyType(import_map)


def _declaration(
    source: str,
) -> tuple[ast.expr | ast.ClassDef | None, MappingProxyType[str, str]]:
    tree = ast.parse(textwrap.dedent(source))
    classes = [
        statement for statement in tree.body if isinstance(statement, ast.ClassDef)
    ]
    if classes:
        return classes[-1], _imports(tree)
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return statement.returns, _imports(tree)
    raise AssertionError("测试源码必须包含 result class 或 Action function")


def _parse(source: str) -> Any:
    declaration, imports = _declaration(source)
    return _api().parse_action_result_declaration(
        declaration,
        imports=imports,
    )


def _expected_contract() -> dict[str, object]:
    return {
        "version": 1,
        "outputs": [
            {
                "name": "sample",
                "schema": {"$slot": "ResourceSlot"},
                "title": "转移后样品",
                "implicit": False,
            },
            {
                "name": "transferred_volume",
                "schema": {"type": "number", "minimum": 0},
                "implicit": False,
            },
        ],
    }


def _normalized_symbols(parsed: object) -> list[tuple[str, list[tuple[str, str]]]]:
    return [
        (
            name,
            [(symbol.local_name, symbol.qualified_name) for symbol in symbols],
        )
        for name, symbols in parsed.resource_templates
    ]


_TYPED_DICT_SOURCE = (
    _COMMON_IMPORTS
    + """
class TransferResult(TypedDict):
    \"\"\"转移结果。\"\"\"
"""
    + _FIELDS
)

_DATACLASS_SOURCE = (
    _COMMON_IMPORTS
    + """
@dataclass(frozen=True, slots=True, kw_only=True)
class TransferResult:
"""
    + _FIELDS
)

_COMPAT_DICT_SOURCE = (
    _COMMON_IMPORTS
    + """
def transfer() -> {
    "sample": Annotated[
        ResourceSlot,
        AllowedResourceTemplates(plate),
        Field(title="转移后样品"),
    ],
    "transferred_volume": Annotated[float, Field(ge=0)],
}:
    pass
"""
)


def test_three_declaration_forms_have_identical_canonical_order_and_symbols() -> None:
    parsed = [
        _parse(_TYPED_DICT_SOURCE),
        _parse(_DATACLASS_SOURCE),
        _parse(_COMPAT_DICT_SOURCE),
    ]

    assert [result.to_dict() for result in parsed] == [
        _expected_contract(),
        _expected_contract(),
        _expected_contract(),
    ]
    assert [_normalized_symbols(result) for result in parsed] == [
        [
            ("sample", [("plate", "lab.resources:plate_96")]),
            ("transferred_volume", []),
        ],
        [
            ("sample", [("plate", "lab.resources:plate_96")]),
            ("transferred_volume", []),
        ],
        [
            ("sample", [("plate", "lab.resources:plate_96")]),
            ("transferred_volume", []),
        ],
    ]


@pytest.mark.parametrize(
    "decorator",
    [
        pytest.param("@dataclass(frozen=True)", id="frozen-only"),
        pytest.param(
            "@dataclass(frozen=True, slots=True)",
            id="frozen-slots",
        ),
        pytest.param(
            "@dataclass(frozen=True, kw_only=True)",
            id="frozen-keyword-only",
        ),
        pytest.param(
            "@dataclass(kw_only=True, frozen=True, slots=True)",
            id="option-order-does-not-matter",
        ),
    ],
)
def test_frozen_dataclass_accepts_only_non_contract_layout_options(
    decorator: str,
) -> None:
    source = (
        "from dataclasses import dataclass\n"
        f"{decorator}\n"
        "class Result:\n"
        "    value: int\n"
    )

    assert _parse(source).to_dict() == {
        "version": 1,
        "outputs": [
            {
                "name": "value",
                "schema": {"type": "integer"},
                "implicit": False,
            }
        ],
    }


def test_none_declares_no_explicit_outputs() -> None:
    parsed = _parse(
        """
        def action() -> None:
            pass
        """
    )

    assert parsed.to_dict() == {"version": 1, "outputs": []}
    assert parsed.resource_templates == ()


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            """
            from typing import TypedDict
            class Result(TypedDict):
                value: str | None
            """,
            id="typed-dict",
        ),
        pytest.param(
            """
            from dataclasses import dataclass
            @dataclass(frozen=True)
            class Result:
                value: str | None
            """,
            id="frozen-dataclass",
        ),
        pytest.param(
            """
            def action() -> {"value": str | None}:
                pass
            """,
            id="compat-dict",
        ),
    ],
)
def test_nullable_result_is_present_but_has_no_default_or_required(
    source: str,
) -> None:
    descriptor = _parse(source).to_dict()["outputs"][0]

    assert descriptor == {
        "name": "value",
        "schema": {
            "anyOf": [
                {"type": "string"},
                {"type": "null"},
            ]
        },
        "implicit": False,
    }
    assert "default" not in descriptor
    assert "required" not in descriptor


def _assert_action_error(
    operation: Any,
    *,
    path_prefix: str,
) -> None:
    api = _api()
    signatures: list[tuple[str, str, str]] = []
    for _ in range(2):
        with pytest.raises(api.ActionResultSchemaError) as caught:
            operation()
        error = caught.value
        assert error.code == "invalid_action_result"
        assert error.path.startswith(path_prefix)
        assert error.message == _ERROR_MESSAGE
        assert str(error) == error.message
        assert re.search(r"[\u4e00-\u9fff]", error.message)
        signatures.append((error.code, error.path, error.message))
    assert signatures[0] == signatures[1]


_TYPED_DICT_REJECTIONS = [
    pytest.param(
        """
        class Result:
            value: str
        """,
        "/return",
        id="missing-base",
    ),
    pytest.param(
        """
        from typing import TypedDict
        class Result(TypedDict, object):
            value: str
        """,
        "/return/bases/1",
        id="multiple-bases",
    ),
    pytest.param(
        """
        from typing_extensions import TypedDict
        class Result(TypedDict):
            value: str
        """,
        "/return/bases/0",
        id="typing-extensions-base",
    ),
    pytest.param(
        """
        class TypedDict:
            pass
        class Result(TypedDict):
            value: str
        """,
        "/return/bases/0",
        id="unproven-local-base",
    ),
    pytest.param(
        """
        from typing import TypedDict
        class Result(TypedDict, total=False):
            value: str
        """,
        "/return",
        id="total-false",
    ),
    pytest.param(
        """
        from typing import TypedDict
        @decorator
        class Result(TypedDict):
            value: str
        """,
        "/return/decorators/0",
        id="decorated",
    ),
    pytest.param(
        """
        from typing import TypedDict
        class Result(TypedDict):
            pass
        """,
        "/return",
        id="empty",
    ),
    pytest.param(
        """
        from typing import TypedDict
        class Result(TypedDict):
            value: str = "default"
        """,
        "/return/body/0",
        id="field-default",
    ),
    pytest.param(
        """
        from typing import TypedDict
        class Result(TypedDict):
            value = "assignment"
        """,
        "/return/body/0",
        id="assignment",
    ),
    pytest.param(
        """
        from typing import TypedDict
        class Result(TypedDict):
            def method(self):
                pass
        """,
        "/return/body/0",
        id="method",
    ),
    pytest.param(
        """
        from typing import TypedDict
        class Result(TypedDict):
            class Nested:
                pass
        """,
        "/return/body/0",
        id="nested-class",
    ),
    pytest.param(
        """
        from typing import Required, TypedDict
        class Result(TypedDict):
            value: Required[str]
        """,
        "/return/fields/0/annotation",
        id="required-wrapper",
    ),
    pytest.param(
        """
        from typing import NotRequired, TypedDict
        class Result(TypedDict):
            value: NotRequired[str]
        """,
        "/return/fields/0/annotation",
        id="not-required-wrapper",
    ),
    pytest.param(
        """
        from typing import ClassVar, TypedDict
        class Result(TypedDict):
            value: ClassVar[str]
        """,
        "/return/fields/0/annotation",
        id="class-var-wrapper",
    ),
    pytest.param(
        """
        from typing import TypedDict
        class Result(TypedDict):
            value: str
            "late docstring"
        """,
        "/return/body/1",
        id="late-docstring",
    ),
]


@pytest.mark.parametrize(("source", "path"), _TYPED_DICT_REJECTIONS)
def test_typed_dict_rejects_every_open_class_shape(
    source: str,
    path: str,
) -> None:
    _assert_action_error(lambda: _parse(source), path_prefix=path)


_DATACLASS_REJECTIONS = [
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="bare-decorator",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass()
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="missing-frozen",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=False)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="frozen-false",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=FLAG)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="dynamic-frozen",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(True)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="positional",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True, order=True)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="unknown-option",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True, frozen=True)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="duplicate-option",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True, **OPTIONS)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="star-keywords",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True, slots=False)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="slots-false",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True, kw_only=False)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="keyword-only-false",
    ),
    pytest.param(
        """
        from local_helpers import dataclass
        @dataclass(frozen=True)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="wrong-dataclass-import",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @other
        @dataclass(frozen=True)
        class Result:
            value: str
        """,
        "/return/decorators/0",
        id="multiple-decorators",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Result(Base):
            value: str
        """,
        "/return/bases/0",
        id="base-class",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Result(metaclass=Meta):
            value: str
        """,
        "/return",
        id="class-keyword",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Result:
            value: str = "default"
        """,
        "/return/body/0",
        id="field-default",
    ),
    pytest.param(
        """
        from dataclasses import dataclass, field
        @dataclass(frozen=True)
        class Result:
            value: str = field()
        """,
        "/return/body/0",
        id="dataclass-field-call",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Result:
            def __post_init__(self):
                pass
        """,
        "/return/body/0",
        id="post-init",
    ),
    pytest.param(
        """
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Result:
            pass
        """,
        "/return",
        id="empty",
    ),
]


@pytest.mark.parametrize(("source", "path"), _DATACLASS_REJECTIONS)
def test_dataclass_rejects_mutable_dynamic_or_runtime_shapes(
    source: str,
    path: str,
) -> None:
    _assert_action_error(lambda: _parse(source), path_prefix=path)


_COMPAT_DICT_REJECTIONS = [
    pytest.param(
        "def action() -> {}:\n    pass\n",
        "/return",
        id="empty",
    ),
    pytest.param(
        "def action() -> {1: str}:\n    pass\n",
        "/return/fields/0/name",
        id="integer-key",
    ),
    pytest.param(
        'def action() -> {"": str}:\n    pass\n',
        "/return/fields/0/name",
        id="empty-key",
    ),
    pytest.param(
        'def action() -> {"not-valid": str}:\n    pass\n',
        "/return/fields/0/name",
        id="invalid-name",
    ),
    pytest.param(
        'def action() -> {"class": str}:\n    pass\n',
        "/return/fields/0/name",
        id="keyword-name",
    ),
    pytest.param(
        "def action() -> {NAME: str}:\n    pass\n",
        "/return/fields/0/name",
        id="computed-name",
    ),
    pytest.param(
        'def action() -> {PREFIX + "x": str}:\n    pass\n',
        "/return/fields/0/name",
        id="computed-expression-name",
    ),
    pytest.param(
        "def action() -> {**EXTRA}:\n    pass\n",
        "/return/fields/0/name",
        id="mapping-unpack",
    ),
    pytest.param(
        'def action() -> {"value": str, "value": int}:\n    pass\n',
        "/return/fields/1/name",
        id="duplicate-key",
    ),
    pytest.param(
        'def action() -> {"value": bytes}:\n    pass\n',
        "/return/fields/0/annotation",
        id="unsupported-annotation",
    ),
    pytest.param(
        'def action() -> {"value": factory()}:\n    pass\n',
        "/return/fields/0/annotation",
        id="dynamic-annotation",
    ),
]


@pytest.mark.parametrize(("source", "path"), _COMPAT_DICT_REJECTIONS)
def test_compat_dict_rejects_open_keys_and_values(
    source: str,
    path: str,
) -> None:
    _assert_action_error(lambda: _parse(source), path_prefix=path)


def test_compat_dict_rejects_missing_ast_value() -> None:
    declaration = ast.Dict(
        keys=[ast.Constant(value="value")],
        values=[],
    )

    _assert_action_error(
        lambda: _api().parse_action_result_declaration(
            declaration,
            imports=MappingProxyType({}),
        ),
        path_prefix="/return/fields/0/annotation",
    )


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param(None, id="missing-return-annotation"),
        pytest.param(ast.Name(id="Result", ctx=ast.Load()), id="unresolved-name"),
        pytest.param(ast.Name(id="dict", ctx=ast.Load()), id="bare-dict"),
        pytest.param(ast.Constant(value="result"), id="constant"),
        pytest.param(ast.List(elts=[], ctx=ast.Load()), id="list"),
        pytest.param(
            ast.Call(
                func=ast.Name(id="factory", ctx=ast.Load()),
                args=[],
                keywords=[],
            ),
            id="call",
        ),
    ],
)
def test_root_declaration_fails_closed(
    declaration: ast.expr | ast.ClassDef | None,
) -> None:
    _assert_action_error(
        lambda: _api().parse_action_result_declaration(
            declaration,
            imports=MappingProxyType({}),
        ),
        path_prefix="/return",
    )


def test_parsed_action_results_are_parser_only_and_dump_isolated() -> None:
    api = _api()
    contract = parse_output_contract(_expected_contract())
    with pytest.raises(TypeError):
        api.ParsedActionResults(contract, ())

    parsed = _parse(_COMPAT_DICT_SOURCE)
    first = parsed.to_dict()
    first["outputs"][0]["schema"]["$slot"] = "mutated"
    first["outputs"].append({"name": "extra"})
    second = parsed.to_dict()

    assert second == _expected_contract()
    assert first is not second
    assert first["outputs"] is not second["outputs"]
    assert isinstance(parsed.resource_templates, tuple)
    with pytest.raises(AttributeError):
        parsed.resource_templates = ()
