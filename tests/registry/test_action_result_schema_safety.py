"""动作结果声明（Action result declaration）的纯 AST、安全与资源增长守护。"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import importlib
import inspect
import time
import typing
from types import MappingProxyType
from typing import Any

import pytest

_MODULE_NAME = "unilabos.registry.action_result_schema"
_ERROR_MESSAGE = "Action 结果声明不符合 Workflow 版本 1 合同"


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
) -> tuple[ast.expr | ast.ClassDef, MappingProxyType[str, str]]:
    tree = ast.parse(source)
    classes = [
        statement for statement in tree.body if isinstance(statement, ast.ClassDef)
    ]
    if classes:
        return classes[-1], _imports(tree)
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef):
            assert statement.returns is not None
            return statement.returns, _imports(tree)
    raise AssertionError("缺少测试声明")


_SAFE_SOURCES = [
    pytest.param(
        ("from typing import TypedDict\nclass Result(TypedDict):\n    value: int\n"),
        id="typed-dict",
    ),
    pytest.param(
        (
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True)\n"
            "class Result:\n"
            "    value: int\n"
        ),
        id="frozen-dataclass",
    ),
    pytest.param(
        (
            "from typing import Annotated\n"
            "from unilabos.registry.annotations "
            "import AllowedResourceTemplates\n"
            "from unilabos.registry.placeholder_type import ResourceSlot\n"
            "from package_that_must_not_load import plate\n"
            "def action() -> {\n"
            "    'sample': Annotated[\n"
            "        ResourceSlot,\n"
            "        AllowedResourceTemplates(plate),\n"
            "    ],\n"
            "}:\n"
            "    pass\n"
        ),
        id="compat-dict-static-symbol",
    ),
]


@pytest.mark.parametrize("source", _SAFE_SOURCES)
def test_parser_does_not_import_execute_compile_or_reflect(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api()
    declaration, imports = _declaration(source)
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        calls.append("called")
        raise AssertionError("Action result parser 必须保持纯 AST")

    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    monkeypatch.setattr(builtins, "compile", forbidden)
    monkeypatch.setattr(importlib, "import_module", forbidden)
    monkeypatch.setattr(inspect, "signature", forbidden)
    monkeypatch.setattr(inspect, "get_annotations", forbidden)
    monkeypatch.setattr(inspect, "getmembers", forbidden)
    monkeypatch.setattr(typing, "get_type_hints", forbidden)
    monkeypatch.setattr(dataclasses, "dataclass", forbidden)
    monkeypatch.setattr(dataclasses, "fields", forbidden)
    monkeypatch.setattr(dataclasses, "is_dataclass", forbidden)
    monkeypatch.setattr(dataclasses, "make_dataclass", forbidden)
    monkeypatch.setattr(builtins, "__import__", forbidden)

    parsed = api.parse_action_result_declaration(
        declaration,
        imports=imports,
    )

    assert parsed.to_dict()["outputs"]
    assert calls == []


def _deep_field_declaration(
    levels: int = 2500,
) -> tuple[ast.Dict, MappingProxyType[str, str]]:
    value: ast.expr = ast.Constant(value=1)
    for _ in range(levels):
        value = ast.List(elts=[value], ctx=ast.Load())
    annotation = ast.Subscript(
        value=ast.Name(id="Annotated", ctx=ast.Load()),
        slice=ast.Tuple(
            elts=[
                ast.Name(id="float", ctx=ast.Load()),
                ast.Call(
                    func=ast.Name(id="Field", ctx=ast.Load()),
                    args=[],
                    keywords=[ast.keyword(arg="ge", value=value)],
                ),
            ],
            ctx=ast.Load(),
        ),
        ctx=ast.Load(),
    )
    return (
        ast.Dict(
            keys=[ast.Constant(value="value")],
            values=[annotation],
        ),
        MappingProxyType(
            {
                "Annotated": "typing:Annotated",
                "Field": "pydantic:Field",
            }
        ),
    )


def test_deep_annotation_failure_is_stably_relocated() -> None:
    api = _api()
    declaration, imports = _deep_field_declaration()
    signatures: list[tuple[str, str, str]] = []

    for _ in range(2):
        with pytest.raises(api.ActionResultSchemaError) as caught:
            api.parse_action_result_declaration(
                declaration,
                imports=imports,
            )
        error = caught.value
        assert error.code == "invalid_action_result"
        assert error.path.startswith("/return/fields/0/annotation")
        assert error.message == _ERROR_MESSAGE
        signatures.append((error.code, error.path, error.message))

    assert signatures[0] == signatures[1]


def _wide_declaration(count: int) -> ast.Dict:
    return ast.Dict(
        keys=[ast.Constant(value=f"field_{index}") for index in range(count)],
        values=[ast.Name(id="int", ctx=ast.Load()) for _ in range(count)],
    )


def _minimum_parse_seconds(
    api: Any,
    declaration: ast.Dict,
    *,
    samples: int = 2,
) -> tuple[float, object]:
    durations: list[float] = []
    parsed: object | None = None
    for _ in range(samples):
        started = time.perf_counter()
        parsed = api.parse_action_result_declaration(
            declaration,
            imports=MappingProxyType({}),
        )
        durations.append(time.perf_counter() - started)
    assert parsed is not None
    return min(durations), parsed


def test_wide_field_table_has_bounded_growth_and_preserves_order() -> None:
    api = _api()
    api.parse_action_result_declaration(
        _wide_declaration(32),
        imports=MappingProxyType({}),
    )
    small_seconds, _ = _minimum_parse_seconds(api, _wide_declaration(256))
    wide_seconds, parsed = _minimum_parse_seconds(
        api,
        _wide_declaration(1024),
    )

    assert [output["name"] for output in parsed.to_dict()["outputs"]] == [
        f"field_{index}" for index in range(1024)
    ]
    assert wide_seconds <= small_seconds * 8 + 0.05, (
        "Action result 字段扩大四倍后解析增长过快："
        f"small={small_seconds:.6f}s, wide={wide_seconds:.6f}s"
    )
