"""模块级静态名称解析的失败关闭与安全守护。"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import importlib
import inspect
import typing
from typing import Any

import pytest

_MODULE_NAME = "unilabos.registry.module_scope"
_ERROR_MESSAGE = "模块作用域不符合 Workflow 静态解析合同"


def _api() -> Any:
    """延迟加载，让缺少 production seam 的基线产生逐用例 RED。"""

    return importlib.import_module(_MODULE_NAME)


def _assert_invalid(
    module: object,
    *,
    module_name: object = "lab.workflows.transfer",
    path_prefix: str,
) -> None:
    api = _api()
    signatures: list[tuple[str, str, str]] = []

    for _ in range(2):
        with pytest.raises(api.ModuleScopeError) as caught:
            api.resolve_module_scope(module, module_name=module_name)

        error = caught.value
        assert error.code == "invalid_module_scope"
        assert error.path.startswith("/module")
        assert error.path.startswith(path_prefix)
        assert error.message == _ERROR_MESSAGE
        assert str(error) == _ERROR_MESSAGE
        signatures.append((error.code, error.path, error.message))

    assert signatures[0] == signatures[1]


@pytest.mark.parametrize(
    ("module", "module_name", "path_prefix"),
    [
        pytest.param(None, "lab.workflow", "/module", id="module-none"),
        pytest.param(
            ast.Expr(value=ast.Constant(1)), "lab.workflow", "/module", id="not-module"
        ),
        pytest.param(
            ast.Module(body=None, type_ignores=[]),
            "lab.workflow",
            "/module/body",
            id="body-none",
        ),
        pytest.param(
            ast.Module(body=(), type_ignores=[]),
            "lab.workflow",
            "/module/body",
            id="body-tuple",
        ),
        pytest.param(
            ast.Module(body=[None], type_ignores=[]),
            "lab.workflow",
            "/module/body/0",
            id="body-element-none",
        ),
        pytest.param(
            ast.Module(body=[], type_ignores=[]),
            None,
            "/module/name",
            id="module-name-none",
        ),
        pytest.param(
            ast.Module(body=[], type_ignores=[]),
            "",
            "/module/name",
            id="module-name-empty",
        ),
        pytest.param(
            ast.Module(body=[], type_ignores=[]),
            "lab..workflow",
            "/module/name",
            id="module-name-empty-segment",
        ),
    ],
)
def test_invalid_root_shapes_and_module_names_fail_closed(
    module: object,
    module_name: object,
    path_prefix: str,
) -> None:
    _assert_invalid(module, module_name=module_name, path_prefix=path_prefix)


@pytest.mark.parametrize(
    ("statement", "path_prefix"),
    [
        pytest.param(
            ast.Import(names=None),
            "/module/body/0",
            id="import-names-none",
        ),
        pytest.param(
            ast.Import(names=[None]),
            "/module/body/0/names/0",
            id="import-alias-none",
        ),
        pytest.param(
            ast.Import(names=[ast.alias(name=[], asname=None)]),
            "/module/body/0/names/0",
            id="import-name-unhashable",
        ),
        pytest.param(
            ast.Import(names=[ast.alias(name="package", asname=[])]),
            "/module/body/0/names/0",
            id="import-asname-unhashable",
        ),
        pytest.param(
            ast.ImportFrom(
                module=[],
                names=[ast.alias(name="Result", asname=None)],
                level=0,
            ),
            "/module/body/0",
            id="from-module-unhashable",
        ),
        pytest.param(
            ast.ImportFrom(
                module="contracts",
                names=None,
                level=0,
            ),
            "/module/body/0",
            id="from-names-none",
        ),
        pytest.param(
            ast.ClassDef(
                name=[],
                bases=[],
                keywords=[],
                body=[ast.Pass()],
                decorator_list=[],
            ),
            "/module/body/0",
            id="class-name-unhashable",
        ),
        pytest.param(
            ast.Assign(targets=None, value=ast.Constant(1)),
            "/module/body/0",
            id="assign-targets-none",
        ),
        pytest.param(
            ast.AnnAssign(
                target=None,
                annotation=ast.Name(id="int", ctx=ast.Load()),
                value=None,
                simple=1,
            ),
            "/module/body/0",
            id="annassign-target-none",
        ),
        pytest.param(
            ast.Delete(targets=None),
            "/module/body/0",
            id="delete-targets-none",
        ),
        pytest.param(
            ast.If(test=ast.Constant(True), body=None, orelse=[]),
            "/module/body/0",
            id="if-body-none",
        ),
        pytest.param(
            ast.ClassDef(
                name="Result",
                bases=[],
                keywords=[],
                body=None,
                decorator_list=[],
            ),
            "/module/body/0",
            id="class-body-none",
        ),
        pytest.param(
            ast.ClassDef(
                name="Result",
                bases=[],
                keywords=[],
                body=(),
                decorator_list=[],
            ),
            "/module/body/0",
            id="class-body-tuple",
        ),
        pytest.param(
            ast.FunctionDef(
                name="execute",
                args=ast.arguments(
                    posonlyargs=[],
                    args=[],
                    kwonlyargs=[],
                    kw_defaults=[],
                    defaults=[],
                ),
                body=[None],
                decorator_list=[],
            ),
            "/module/body/0",
            id="function-body-non-statement",
        ),
    ],
)
def test_forged_statement_shapes_never_leak_python_container_errors(
    statement: ast.stmt,
    path_prefix: str,
) -> None:
    _assert_invalid(
        ast.Module(body=[statement], type_ignores=[]),
        path_prefix=path_prefix,
    )


@pytest.mark.parametrize(
    ("source", "path_prefix"),
    [
        pytest.param(
            "from package import *\n",
            "/module/body/0/names/0",
            id="wildcard-import",
        ),
        pytest.param(
            "from .contracts import Result\n",
            "/module/body/0",
            id="relative-one-level",
        ),
        pytest.param(
            "from ..contracts import Result\n",
            "/module/body/0",
            id="relative-two-levels",
        ),
    ],
)
def test_wildcard_and_relative_imports_fail_closed(
    source: str,
    path_prefix: str,
) -> None:
    _assert_invalid(ast.parse(source), path_prefix=path_prefix)


def test_resolved_scope_and_both_public_mappings_are_read_only() -> None:
    tree = ast.parse("from contracts import Imported\nclass Result:\n    pass\n")
    scope = _api().resolve_module_scope(
        tree,
        module_name="lab.workflows.transfer",
    )

    with pytest.raises(TypeError):
        scope.import_identities["Other"] = "contracts:Other"
    with pytest.raises(TypeError):
        del scope.import_identities["Imported"]
    with pytest.raises(TypeError):
        scope.definitions["Other"] = tree.body[1]
    with pytest.raises(TypeError):
        del scope.definitions["Result"]
    with pytest.raises(TypeError):
        scope.annotation_bindings["Other"] = "contracts:Other"
    with pytest.raises(TypeError):
        del scope.annotation_bindings["Imported"]
    with pytest.raises(AttributeError):
        scope.module_name = "changed.module"

    assert dict(scope.import_identities) == {"Imported": "contracts:Imported"}
    assert dict(scope.definitions) == {"Result": tree.body[1]}
    assert scope.module_name == "lab.workflows.transfer"


def test_resolution_does_not_modify_the_input_ast_or_copy_definition_nodes() -> None:
    tree = ast.parse("from contracts import Imported\nclass Result:\n    value: int\n")
    before = ast.dump(tree, include_attributes=True)
    result_node = tree.body[1]

    scope = _api().resolve_module_scope(
        tree,
        module_name="lab.workflows.transfer",
    )

    assert ast.dump(tree, include_attributes=True) == before
    assert scope.definitions["Result"] is result_node


def test_resolver_does_not_import_execute_compile_or_reflect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api()
    tree = ast.parse(
        """
from package_that_must_not_load import Result
class LocalResult:
    pass
"""
    )
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        calls.append("called")
        raise AssertionError("module scope resolver 必须保持纯 AST")

    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    monkeypatch.setattr(builtins, "compile", forbidden)
    monkeypatch.setattr(importlib, "import_module", forbidden)
    monkeypatch.setattr(inspect, "signature", forbidden)
    monkeypatch.setattr(inspect, "get_annotations", forbidden)
    monkeypatch.setattr(inspect, "getmembers", forbidden)
    monkeypatch.setattr(typing, "get_type_hints", forbidden)
    monkeypatch.setattr(dataclasses, "fields", forbidden)
    monkeypatch.setattr(dataclasses, "is_dataclass", forbidden)
    monkeypatch.setattr(builtins, "__import__", forbidden)

    scope = api.resolve_module_scope(
        tree,
        module_name="lab.workflows.transfer",
    )

    assert dict(scope.import_identities) == {
        "Result": "package_that_must_not_load:Result"
    }
    assert set(scope.definitions) == {"LocalResult"}
    assert calls == []


def test_repeated_resolution_is_deterministic_and_has_no_cross_call_state() -> None:
    tree = ast.parse("from contracts import Imported\nclass Result:\n    pass\n")
    api = _api()

    first = api.resolve_module_scope(tree, module_name="lab.workflows.transfer")
    second = api.resolve_module_scope(tree, module_name="lab.workflows.transfer")

    assert dict(first.import_identities) == dict(second.import_identities)
    assert list(first.definitions) == list(second.definitions)
    assert dict(first.annotation_bindings) == dict(second.annotation_bindings)
    assert first.definitions["Result"] is tree.body[1]
    assert second.definitions["Result"] is tree.body[1]
    assert first.import_identities is not second.import_identities
    assert first.definitions is not second.definitions
    assert first.annotation_bindings is not second.annotation_bindings
