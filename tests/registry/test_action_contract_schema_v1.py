"""动作（Action）参数与具名结果（named result）组合门面的公共行为合同。"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import importlib
import inspect
import textwrap
import typing
from collections.abc import Callable
from typing import Any

import pytest

_MODULE_NAME = "unilabos.registry.action_contract_schema"
_MODULE_DOTTED_NAME = "lab.devices.transfer"


def _api() -> Any:
    """延迟加载，让缺少 production seam 的基线产生逐用例 RED。"""

    return importlib.import_module(_MODULE_NAME)


def _module_and_action(
    source: str,
    *,
    action_name: str = "action",
) -> tuple[ast.Module, ast.FunctionDef | ast.AsyncFunctionDef]:
    module = ast.parse(textwrap.dedent(source))
    actions = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == action_name
    ]
    assert len(actions) == 1
    return module, actions[0]


def _parse(
    source: str,
    *,
    action_name: str = "action",
    module_name: str = _MODULE_DOTTED_NAME,
) -> Any:
    module, action = _module_and_action(source, action_name=action_name)
    return _api().parse_action_contract(
        module,
        action,
        module_name=module_name,
    )


def _qualified_template_groups(
    groups: object,
) -> list[tuple[str, list[tuple[str, str]]]]:
    return [
        (
            name,
            [(symbol.local_name, symbol.qualified_name) for symbol in symbols],
        )
        for name, symbols in groups  # type: ignore[union-attr]
    ]


def _assert_action_error(
    callback: Callable[[], object],
    *,
    code: str,
    path: str | None = None,
    path_prefix: str | None = None,
) -> None:
    api = _api()
    with pytest.raises(api.ActionContractError) as caught:
        callback()

    error = caught.value
    assert error.code == code
    if path is not None:
        assert error.path == path
    if path_prefix is not None:
        assert error.path.startswith(path_prefix)
    assert type(error.message) is str
    assert error.message
    assert str(error) == error.message


_CANONICAL_ACTION = """
from typing import Annotated, Literal, TypedDict
from pydantic import Field
from lab.resources import plate_96 as plate
from unilabos.registry.annotations import AllowedResourceTemplates
from unilabos.registry.placeholder_type import ResourceSlot

class TransferResult(TypedDict):
    sample: Annotated[
        ResourceSlot,
        AllowedResourceTemplates(plate),
        Field(title="处理后样品"),
    ]
    report: str

class Pump:
    def transfer(
        self,
        source: Annotated[
            ResourceSlot,
            AllowedResourceTemplates(plate),
        ],
        volume: Annotated[float, Field(title="设定体积", ge=0)] = 1.25,
        /,
        mode: Literal["safe", "fast"] = "safe",
        sample_uuids=None,
        *,
        note: str | None = None,
    ) -> TransferResult:
        \"\"\"Args:
            source[来源样品]: 待处理的样品。
            volume[文档体积]: 移液体积。
            mode[运行模式]: 安全或快速模式。
        \"\"\"
        raise NotImplementedError
"""


def test_real_action_produces_one_ordered_canonical_input_and_output_contract() -> None:
    contract = _parse(_CANONICAL_ACTION, action_name="transfer")

    assert contract.to_dict() == {
        "input_contract": {
            "version": 1,
            "parameters": [
                {
                    "name": "source",
                    "schema": {"$slot": "ResourceSlot"},
                    "required": True,
                    "title": "来源样品",
                    "description": "待处理的样品。",
                },
                {
                    "name": "volume",
                    "schema": {"type": "number", "minimum": 0},
                    "required": False,
                    "default": 1.25,
                    "title": "设定体积",
                    "description": "移液体积。",
                },
                {
                    "name": "mode",
                    "schema": {
                        "type": "string",
                        "enum": ["safe", "fast"],
                    },
                    "required": False,
                    "default": "safe",
                    "title": "运行模式",
                    "description": "安全或快速模式。",
                },
                {
                    "name": "note",
                    "schema": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    },
                    "required": False,
                    "default": None,
                },
            ],
        },
        "output_contract": {
            "version": 1,
            "outputs": [
                {
                    "name": "sample",
                    "schema": {"$slot": "ResourceSlot"},
                    "title": "处理后样品",
                    "implicit": False,
                },
                {
                    "name": "report",
                    "schema": {"type": "string"},
                    "implicit": False,
                },
            ],
        },
    }
    assert _qualified_template_groups(contract.input_resource_templates) == [
        ("source", [("plate", "lab.resources:plate_96")]),
        ("volume", []),
        ("mode", []),
        ("note", []),
    ]
    assert _qualified_template_groups(contract.output_resource_templates) == [
        ("sample", [("plate", "lab.resources:plate_96")]),
        ("report", []),
    ]


def test_positional_and_keyword_defaults_align_without_framework_parameters() -> None:
    contract = _parse(
        """
        class Device:
            def action(
                cls,
                first: int,
                second: str = "two",
                /,
                third: float = 3.5,
                sample_uuids=None,
                *,
                required_flag: bool,
                optional_note: str = "note",
            ) -> None:
                pass
        """
    )

    assert contract.to_dict() == {
        "input_contract": {
            "version": 1,
            "parameters": [
                {
                    "name": "first",
                    "schema": {"type": "integer"},
                    "required": True,
                },
                {
                    "name": "second",
                    "schema": {"type": "string"},
                    "required": False,
                    "default": "two",
                },
                {
                    "name": "third",
                    "schema": {"type": "number"},
                    "required": False,
                    "default": 3.5,
                },
                {
                    "name": "required_flag",
                    "schema": {"type": "boolean"},
                    "required": True,
                },
                {
                    "name": "optional_note",
                    "schema": {"type": "string"},
                    "required": False,
                    "default": "note",
                },
            ],
        },
        "output_contract": {"version": 1, "outputs": []},
    }


def test_top_level_function_does_not_treat_self_as_a_method_receiver() -> None:
    contract = _parse(
        """
        def action(self: int, *, enabled: bool = True) -> None:
            pass
        """
    )

    parameters = contract.to_dict()["input_contract"]["parameters"]
    assert [parameter["name"] for parameter in parameters] == ["self", "enabled"]


def test_async_class_action_excludes_cls_and_framework_owned_parameter() -> None:
    contract = _parse(
        """
        class Device:
            async def action(
                cls,
                sample_uuids,
                *,
                duration: float,
            ) -> None:
                pass
        """
    )

    assert [
        parameter["name"]
        for parameter in contract.to_dict()["input_contract"]["parameters"]
    ] == ["duration"]


@pytest.mark.parametrize(
    "result_declaration",
    [
        pytest.param(
            """
            from typing import TypedDict
            class Result(TypedDict):
                value: int
            """,
            id="typed-dict-name",
        ),
        pytest.param(
            """
            from dataclasses import dataclass
            @dataclass(frozen=True)
            class Result:
                value: int
            """,
            id="frozen-dataclass-name",
        ),
    ],
)
def test_result_name_resolves_only_to_a_static_module_class(
    result_declaration: str,
) -> None:
    contract = _parse(
        textwrap.dedent(result_declaration)
        + "\ndef action(value: int) -> Result:\n    pass\n"
    )

    assert contract.to_dict()["output_contract"] == {
        "version": 1,
        "outputs": [
            {
                "name": "value",
                "schema": {"type": "integer"},
                "implicit": False,
            }
        ],
    }


def test_inline_result_and_none_are_delegated_to_the_frozen_result_parser() -> None:
    inline = _parse(
        """
        def action(value: int) -> {"answer": int}:
            pass
        """
    )
    no_result = _parse(
        """
        def action(value: int) -> None:
            pass
        """
    )

    assert inline.to_dict()["output_contract"] == {
        "version": 1,
        "outputs": [
            {
                "name": "answer",
                "schema": {"type": "integer"},
                "implicit": False,
            }
        ],
    }
    assert no_result.to_dict()["output_contract"] == {
        "version": 1,
        "outputs": [],
    }


def test_nested_imports_cannot_replace_trusted_module_annotation_or_result_names() -> (
    None
):
    contract = _parse(
        """
        from typing import TypedDict
        from unilabos.registry.placeholder_type import ResourceSlot

        class Result(TypedDict):
            sample: ResourceSlot

        class Device:
            def action(self, sample: ResourceSlot) -> Result:
                from malicious.types import ResourceSlot, Result, TypedDict
                return {"sample": sample}
        """
    )

    assert contract.to_dict() == {
        "input_contract": {
            "version": 1,
            "parameters": [
                {
                    "name": "sample",
                    "schema": {"$slot": "ResourceSlot"},
                    "required": True,
                }
            ],
        },
        "output_contract": {
            "version": 1,
            "outputs": [
                {
                    "name": "sample",
                    "schema": {"$slot": "ResourceSlot"},
                    "implicit": False,
                }
            ],
        },
    }


@pytest.mark.parametrize(
    "binding",
    [
        pytest.param("from contracts import Result", id="imported-result"),
        pytest.param("Result = runtime_result", id="assigned-result"),
        pytest.param(
            "if condition:\n    Result = runtime_result",
            id="conditional-result",
        ),
        pytest.param(
            "def Result():\n    pass",
            id="function-result",
        ),
    ],
)
def test_return_name_rejects_every_non_class_or_uncertain_module_binding(
    binding: str,
) -> None:
    module, action = _module_and_action(
        textwrap.dedent(binding) + "\n\ndef action(value: int) -> Result:\n    pass\n"
    )

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        ),
        code="invalid_action_result",
        path="/return",
    )


@pytest.mark.parametrize(
    ("source", "expected_path"),
    [
        pytest.param(
            """
            from unilabos.registry.placeholder_type import ResourceSlot
            ResourceSlot = replacement
            def action(sample: ResourceSlot) -> None:
                pass
            """,
            "/parameters/0/annotation",
            id="imported-annotation-shadowed",
        ),
        pytest.param(
            """
            int = replacement
            def action(value: int) -> None:
                pass
            """,
            "/parameters/0/annotation",
            id="builtin-annotation-shadowed",
        ),
    ],
)
def test_annotation_names_respect_the_same_module_shadow_barrier(
    source: str,
    expected_path: str,
) -> None:
    module, action = _module_and_action(source)

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        ),
        code="invalid_annotation",
        path=expected_path,
    )


@pytest.mark.parametrize(
    "signature",
    [
        pytest.param("def action(self, *values: int) -> None", id="varargs"),
        pytest.param("def action(self, **values: int) -> None", id="kwargs"),
        pytest.param("def action(self, value) -> None", id="missing-annotation"),
        pytest.param(
            "def action(self, *, value) -> None",
            id="missing-keyword-annotation",
        ),
    ],
)
def test_open_or_untyped_action_signatures_fail_closed(signature: str) -> None:
    module, action = _module_and_action(f"{signature}:\n    pass\n")

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        ),
        code="invalid_action_contract",
        path_prefix="/parameters",
    )


@pytest.mark.parametrize(
    ("return_annotation", "expected_code"),
    [
        pytest.param(None, "invalid_action_contract", id="missing-return"),
        pytest.param("package.Result", "invalid_action_result", id="attribute"),
        pytest.param('"Result"', "invalid_action_result", id="forward-reference"),
        pytest.param("result_factory()", "invalid_action_result", id="call"),
    ],
)
def test_missing_or_dynamic_return_declarations_fail_closed(
    return_annotation: str | None,
    expected_code: str,
) -> None:
    suffix = "" if return_annotation is None else f" -> {return_annotation}"
    module, action = _module_and_action(f"def action(value: int){suffix}:\n    pass\n")

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        ),
        code=expected_code,
        path="/return",
    )


@pytest.mark.parametrize(
    "return_annotation",
    [
        pytest.param("dict[int, str]", id="non-string-key"),
        pytest.param("dict[str, int]", id="non-json-value"),
        pytest.param("dict[Any, Any]", id="open-key"),
    ],
)
def test_only_explicit_opaque_json_mapping_results_are_closed_empty(
    return_annotation: str,
) -> None:
    module, action = _module_and_action(
        "from typing import Any\n"
        f"def action(value: int) -> {return_annotation}:\n"
        "    pass\n"
    )

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        ),
        code="invalid_action_result",
        path="/return",
    )


def test_json_value_mapping_result_is_a_closed_empty_opaque_result() -> None:
    contract = _parse(
        """
        from unilabos.registry.annotations import JSONValue
        def action(value: int) -> dict[str, JSONValue]:
            pass
        """
    )

    assert contract.to_dict()["output_contract"] == {
        "version": 1,
        "outputs": [],
    }


def test_forged_module_container_is_reported_as_a_stable_scope_error() -> None:
    module, action = _module_and_action("def action(value: int) -> None:\n    pass\n")
    module.body = tuple(module.body)  # type: ignore[assignment]

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        ),
        code="invalid_module_scope",
        path="/module/body",
    )


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(
            lambda action: setattr(action.args, "args", [ast.Constant(1)]),
            id="non-argument-positional-entry",
        ),
        pytest.param(
            lambda action: setattr(action.args, "defaults", None),
            id="non-list-defaults",
        ),
        pytest.param(
            lambda action: setattr(action.args, "kw_defaults", []),
            id="misaligned-keyword-defaults",
        ),
        pytest.param(
            lambda action: setattr(action, "body", None),
            id="non-list-body",
        ),
    ],
)
def test_forged_action_ast_never_leaks_container_or_attribute_errors(
    forge: Callable[[ast.FunctionDef | ast.AsyncFunctionDef], None],
) -> None:
    module, action = _module_and_action(
        """
        def action(value: int, *, enabled: bool = True) -> None:
            pass
        """
    )
    forge(action)

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        ),
        code="invalid_action_contract",
        path_prefix="/",
    )


def test_invalid_module_name_preserves_the_module_scope_error_contract() -> None:
    module, action = _module_and_action("def action(value: int) -> None:\n    pass\n")

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name="invalid..module",
        ),
        code="invalid_module_scope",
        path="/module/name",
    )


def test_contract_dumps_templates_and_source_ast_are_isolated_from_mutation() -> None:
    module, action = _module_and_action(_CANONICAL_ACTION, action_name="transfer")
    before = ast.dump(module, include_attributes=True)
    contract = _api().parse_action_contract(
        module,
        action,
        module_name=_MODULE_DOTTED_NAME,
    )
    first_dump = contract.to_dict()
    first_dump["input_contract"]["parameters"][0]["schema"]["$slot"] = "forged"
    first_dump["output_contract"]["outputs"].clear()

    assert contract.to_dict()["input_contract"]["parameters"][0]["schema"] == {
        "$slot": "ResourceSlot"
    }
    assert len(contract.to_dict()["output_contract"]["outputs"]) == 2
    assert type(contract.input_resource_templates) is tuple
    assert type(contract.output_resource_templates) is tuple
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        contract.input_resource_templates[0][1][0].qualified_name = "forged"
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        contract.input_resource_templates = ()
    assert ast.dump(module, include_attributes=True) == before


def test_facade_never_imports_executes_compiles_or_reflects_author_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api()
    module, action = _module_and_action(
        """
        from typing import TypedDict

        class Result(TypedDict):
            value: int

        def action(value: int) -> Result:
            return {"value": value}
        """
    )

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("静态 Action Contract facade 不得使用运行时执行或反射")

    with monkeypatch.context() as guarded:
        guarded.setattr(builtins, "eval", forbidden)
        guarded.setattr(builtins, "exec", forbidden)
        guarded.setattr(builtins, "compile", forbidden)
        guarded.setattr(importlib, "import_module", forbidden)
        guarded.setattr(inspect, "signature", forbidden)
        guarded.setattr(typing, "get_type_hints", forbidden)
        guarded.setattr(dataclasses, "fields", forbidden)
        guarded.setattr(dataclasses, "is_dataclass", forbidden)
        guarded.setattr(builtins, "__import__", forbidden)

        contract = api.parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        )

        assert contract.to_dict()["output_contract"]["outputs"][0]["name"] == "value"


@pytest.mark.parametrize(
    ("source", "expected_code", "expected_path"),
    [
        pytest.param(
            """
            def action(value) -> None:
                pass
            """,
            "invalid_action_contract",
            "/parameters/0/annotation",
            id="legacy-empty-type-fallback-is-gone",
        ),
        pytest.param(
            """
            def action(value: "int") -> None:
                pass
            """,
            "invalid_annotation",
            "/parameters/0/annotation",
            id="legacy-string-annotation-guess-is-gone",
        ),
        pytest.param(
            """
            @action(goal={"value": {"type": "str"}})
            def action(value) -> None:
                pass
            """,
            "invalid_action_contract",
            "/parameters/0/annotation",
            id="runtime-goal-example-does-not-create-inputs",
        ),
    ],
)
def test_legacy_scanner_fallbacks_cannot_produce_apparently_valid_contracts(
    source: str,
    expected_code: str,
    expected_path: str,
) -> None:
    module, action = _module_and_action(source)

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        ),
        code=expected_code,
        path=expected_path,
    )


def test_dynamic_default_is_rejected_without_running_it() -> None:
    module, action = _module_and_action(
        """
        def action(value: int = explode()) -> None:
            pass
        """
    )

    _assert_action_error(
        lambda: _api().parse_action_contract(
            module,
            action,
            module_name=_MODULE_DOTTED_NAME,
        ),
        code="invalid_annotation",
        path="/parameters/0/default",
    )
