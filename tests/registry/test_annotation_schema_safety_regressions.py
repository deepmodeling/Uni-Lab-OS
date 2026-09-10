"""参数注解（parameter annotation）模块安全阻塞项的公共接口回归测试。"""

from __future__ import annotations

import ast
import inspect
import re
import time
from collections.abc import Callable
from types import MappingProxyType
from typing import Any

import pytest

from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    ParsedParameter,
    ResourceTemplateSymbol,
    parse_parameter_annotation,
)
from unilabos.registry.annotations import AllowedResourceTemplates
from unilabos.workflow.schema import parse_input_contract

_EMPTY_IMPORTS = MappingProxyType({})
_LITERAL_IMPORTS = MappingProxyType({"Literal": "typing:Literal"})
_RESOURCE_SLOT_SCHEMA = {"$slot": "ResourceSlot"}
_DEEP_SINGLETON_LEVELS = 2500


def _descriptor(
    name: str,
    schema: dict[str, object],
) -> dict[str, object]:
    return {
        "name": name,
        "schema": schema,
        "required": True,
    }


def _contract(*parameters: dict[str, object]) -> object:
    return parse_input_contract(
        {
            "version": 1,
            "parameters": list(parameters),
        }
    )


_STRING_PARAMETER = _descriptor("value", {"type": "string"})
_SLOT_PARAMETER = _descriptor("sample", _RESOURCE_SLOT_SCHEMA)
_VALID_SYMBOL = ResourceTemplateSymbol("plate", "lab.resources:plate")


@pytest.mark.parametrize(
    ("contract", "symbols"),
    [
        pytest.param(_contract(), (), id="empty-contract"),
        pytest.param(
            _contract(
                _descriptor("first", {"type": "string"}),
                _descriptor("second", {"type": "integer"}),
            ),
            (),
            id="multi-parameter-contract",
        ),
        pytest.param(
            _contract(_STRING_PARAMETER),
            (_VALID_SYMBOL,),
            id="template-on-non-slot",
        ),
        pytest.param(
            _contract(_SLOT_PARAMETER),
            (object(),),
            id="wrong-symbol-object",
        ),
        pytest.param(
            _contract(_SLOT_PARAMETER),
            (ResourceTemplateSymbol("", "lab.resources:plate"),),
            id="empty-symbol-name",
        ),
        pytest.param(
            _contract(_SLOT_PARAMETER),
            (_VALID_SYMBOL,),
            id="valid-looking-state-still-parser-only",
        ),
    ],
)
def test_parsed_parameter_normal_constructor_cannot_forge_parser_state(
    contract: object,
    symbols: tuple[object, ...],
) -> None:
    """即使值看似合法，普通 caller 也不能绕过唯一 parser Authority。"""

    for _ in range(2):
        with pytest.raises(TypeError):
            ParsedParameter(contract, symbols)


def _deep_singleton_literal() -> ast.expr:
    """构造源码约 5 KB 的单子节点 literal，不让 CPython parser 抢先失败。"""

    node: ast.expr = ast.Constant(value=1)
    for _ in range(_DEEP_SINGLETON_LEVELS):
        node = ast.List(elts=[node], ctx=ast.Load())
    return node


def _annotation_error_signature(
    operation: Callable[[], object],
) -> tuple[str, str, str]:
    with pytest.raises(AnnotationSchemaError) as caught:
        operation()

    error = caught.value
    assert isinstance(error.code, str) and error.code
    assert isinstance(error.path, str) and error.path.startswith("/")
    assert isinstance(error.message, str) and error.message
    assert re.search(r"[\u4e00-\u9fff]", error.message), "错误消息必须是简体中文"
    return error.code, error.path, error.message


@pytest.mark.parametrize(
    "position",
    [
        pytest.param("default", id="deep-default"),
        pytest.param("literal-member", id="deep-literal-member"),
    ],
)
def test_deep_singleton_literal_is_a_stable_annotation_error(
    position: str,
) -> None:
    deep_literal = _deep_singleton_literal()
    if position == "default":
        annotation: ast.expr = ast.Name(id="str", ctx=ast.Load())
        default: ast.expr | object = deep_literal
        imports = _EMPTY_IMPORTS
        expected_path = "/default"
    else:
        annotation = ast.Subscript(
            value=ast.Name(id="Literal", ctx=ast.Load()),
            slice=deep_literal,
            ctx=ast.Load(),
        )
        default = NO_DEFAULT
        imports = _LITERAL_IMPORTS
        expected_path = "/annotation"

    def operation() -> object:
        return parse_parameter_annotation(
            "value",
            annotation,
            default=default,
            imports=imports,
        )

    first = _annotation_error_signature(operation)
    second = _annotation_error_signature(operation)

    assert first == second
    assert first[1] == expected_path


def _literal_annotation(values: list[int]) -> ast.Subscript:
    return ast.Subscript(
        value=ast.Name(id="Literal", ctx=ast.Load()),
        slice=ast.Tuple(
            elts=[ast.Constant(value=value) for value in values],
            ctx=ast.Load(),
        ),
        ctx=ast.Load(),
    )


def _parse_literal_values(values: list[int]) -> Any:
    return parse_parameter_annotation(
        "value",
        _literal_annotation(values),
        default=NO_DEFAULT,
        imports=_LITERAL_IMPORTS,
    )


def _minimum_parse_seconds(
    values: list[int],
    *,
    samples: int = 2,
) -> tuple[float, Any]:
    annotation = _literal_annotation(values)
    durations: list[float] = []
    result: Any = None
    for _ in range(samples):
        started = time.perf_counter()
        result = parse_parameter_annotation(
            "value",
            annotation,
            default=NO_DEFAULT,
            imports=_LITERAL_IMPORTS,
        )
        durations.append(time.perf_counter() - started)
    return min(durations), result


def test_wide_unique_literal_scales_near_linearly_and_preserves_order() -> None:
    """四倍成员不应接近十六倍工作量，也不能以较小成员上限规避。"""

    _parse_literal_values(list(range(64, 0, -1)))
    small_values = list(range(1000, 0, -1))
    wide_values = list(range(4000, 0, -1))

    small_seconds, _ = _minimum_parse_seconds(small_values)
    wide_seconds, parameter = _minimum_parse_seconds(wide_values)

    assert parameter.to_dict()["schema"]["enum"] == wide_values
    assert wide_seconds <= small_seconds * 8 + 0.02, (
        "Literal 成员扩大四倍后耗时增长过快："
        f"small={small_seconds:.6f}s, wide={wide_seconds:.6f}s"
    )


def test_wide_literal_duplicate_is_still_rejected_deterministically() -> None:
    values = list(range(1024))
    values.append(values[511])

    def operation() -> object:
        return _parse_literal_values(values)

    first = _annotation_error_signature(operation)
    second = _annotation_error_signature(operation)

    assert first == second
    assert first[1] == "/annotation"


@pytest.mark.parametrize(
    "constructor",
    [
        pytest.param(AnnotationSchemaError.__init__, id="annotation-error"),
        pytest.param(
            AllowedResourceTemplates.__init__,
            id="allowed-resource-templates",
        ),
    ],
)
def test_new_public_constructors_declare_none_return(
    constructor: Callable[..., object],
) -> None:
    return_annotation = inspect.signature(constructor).return_annotation

    assert return_annotation in {None, "None", type(None)}
