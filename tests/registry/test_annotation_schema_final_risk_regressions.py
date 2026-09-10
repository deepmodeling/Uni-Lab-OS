"""参数注解（parameter annotation）最终风险阻塞项的公共接口回归测试。"""

from __future__ import annotations

import ast
import re
import sys
import time
from collections.abc import Callable
from types import MappingProxyType
from typing import Any

import pytest

from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    parse_parameter_annotation,
    render_parameter_annotation,
)
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    parse_input_contract,
)

_EMPTY_IMPORTS = MappingProxyType({})
_LITERAL_IMPORTS = MappingProxyType({"Literal": "typing:Literal"})
_ANNOTATED_IMPORTS = MappingProxyType(
    {
        "Annotated": "typing:Annotated",
        "Field": "pydantic:Field",
    }
)
_JSON_IMPORTS = MappingProxyType(
    {
        "JSONValue": "unilabos.registry.annotations:JSONValue",
    }
)
_HASH_COLLISION_SMALL = 2048
_HASH_COLLISION_WIDE = 8192
_AUTHORING_INTEGER_DIGITS = 4096


def _literal_annotation(values: list[object]) -> ast.Subscript:
    slice_node: ast.expr
    constants = [ast.Constant(value=value) for value in values]
    if len(constants) == 1:
        slice_node = constants[0]
    else:
        slice_node = ast.Tuple(elts=constants, ctx=ast.Load())
    return ast.Subscript(
        value=ast.Name(id="Literal", ctx=ast.Load()),
        slice=slice_node,
        ctx=ast.Load(),
    )


def _parse_parameter_literal(values: list[object]) -> Any:
    return parse_parameter_annotation(
        "value",
        _literal_annotation(values),
        default=NO_DEFAULT,
        imports=_LITERAL_IMPORTS,
    )


def _parse_contract_enum(values: list[object], kind: str = "integer") -> Any:
    return parse_input_contract(
        {
            "version": 1,
            "parameters": [
                {
                    "name": "value",
                    "schema": {"type": kind, "enum": values},
                    "required": True,
                }
            ],
        }
    )


def _collision_values(count: int) -> list[int]:
    """生成相异但具有相同可预测 integer hash 的值。"""

    return [index * sys.hash_info.modulus for index in range(count)]


def _minimum_duration(
    operation: Callable[[list[object]], object],
    values: list[object],
    *,
    samples: int = 2,
) -> tuple[float, object]:
    durations: list[float] = []
    result: object | None = None
    for _ in range(samples):
        started = time.perf_counter()
        result = operation(values)
        durations.append(time.perf_counter() - started)
    assert result is not None
    return min(durations), result


def _enum_dump(layer: str, parsed: object) -> list[object]:
    data = parsed.to_dict()
    if layer == "parameter":
        return data["schema"]["enum"]
    return data["parameters"][0]["schema"]["enum"]


@pytest.mark.parametrize(
    ("layer", "operation"),
    [
        pytest.param(
            "parameter",
            _parse_parameter_literal,
            id="parameter-parser",
        ),
        pytest.param(
            "contract",
            _parse_contract_enum,
            id="input-contract-parser",
        ),
    ],
)
def test_predictable_integer_hash_collisions_scale_below_quadratic(
    layer: str,
    operation: Callable[[list[object]], object],
) -> None:
    """四倍碰撞输入应允许 O(n log n)，但不能退回 set 的近二次扫描。"""

    operation(_collision_values(64))
    small_values = _collision_values(_HASH_COLLISION_SMALL)
    wide_values = _collision_values(_HASH_COLLISION_WIDE)

    small_seconds, _ = _minimum_duration(operation, small_values)
    wide_seconds, parsed = _minimum_duration(operation, wide_values)

    assert _enum_dump(layer, parsed) == wide_values
    assert wide_seconds <= small_seconds * 8 + 0.02, (
        "可预测 integer hash collision 使公开 parser 增长过快："
        f"small={small_seconds:.6f}s, wide={wide_seconds:.6f}s"
    )


def _error_signature(
    operation: Callable[[], object],
    error_type: type[AnnotationSchemaError | WorkflowSchemaError],
) -> tuple[str, str, str]:
    with pytest.raises(error_type) as caught:
        operation()

    error = caught.value
    assert isinstance(error.code, str) and error.code
    assert isinstance(error.path, str) and error.path.startswith("/")
    assert isinstance(error.message, str) and error.message
    assert re.search(r"[\u4e00-\u9fff]", error.message), "错误消息必须是简体中文"
    return error.code, error.path, error.message


@pytest.mark.parametrize(
    ("layer", "values", "kind"),
    [
        pytest.param(
            "parameter",
            [0, sys.hash_info.modulus, sys.hash_info.modulus],
            "integer",
            id="parameter-collision-duplicate",
        ),
        pytest.param(
            "contract",
            [0, sys.hash_info.modulus, sys.hash_info.modulus],
            "integer",
            id="contract-collision-duplicate",
        ),
        pytest.param(
            "parameter",
            [True, 1],
            "integer",
            id="parameter-true-vs-one",
        ),
        pytest.param(
            "contract",
            [True, 1],
            "integer",
            id="contract-true-vs-one",
        ),
        pytest.param(
            "parameter",
            [1, 1.0],
            "number",
            id="parameter-int-vs-float",
        ),
        pytest.param(
            "contract",
            [1, 1.0],
            "number",
            id="contract-int-vs-float",
        ),
        pytest.param(
            "parameter",
            [-0.0, 0],
            "number",
            id="parameter-negative-zero-vs-zero",
        ),
        pytest.param(
            "contract",
            [-0.0, 0],
            "number",
            id="contract-negative-zero-vs-zero",
        ),
    ],
)
def test_collision_hardening_preserves_strict_duplicate_semantics(
    layer: str,
    values: list[object],
    kind: str,
) -> None:
    if layer == "parameter":
        error_type = AnnotationSchemaError
    else:
        error_type = WorkflowSchemaError

    def operation() -> object:
        if layer == "parameter":
            return _parse_parameter_literal(values)
        return _parse_contract_enum(values, kind)

    first = _error_signature(operation, error_type)
    second = _error_signature(operation, error_type)

    assert first == second


def _nested_json_default(value: int) -> ast.Dict:
    return ast.Dict(
        keys=[ast.Constant(value="outer")],
        values=[
            ast.List(
                elts=[
                    ast.Dict(
                        keys=[ast.Constant(value="value")],
                        values=[ast.Constant(value=value)],
                    )
                ],
                ctx=ast.Load(),
            )
        ],
    )


def _authoring_case(
    location: str,
    value: int,
) -> tuple[ast.expr, ast.expr | object, MappingProxyType[str, str], str]:
    if location == "literal":
        return (
            _literal_annotation([value]),
            NO_DEFAULT,
            _LITERAL_IMPORTS,
            "/annotation",
        )
    if location == "default":
        return (
            ast.Name(id="int", ctx=ast.Load()),
            ast.Constant(value=value),
            _EMPTY_IMPORTS,
            "/default",
        )
    if location == "field-bound":
        annotation = ast.Subscript(
            value=ast.Name(id="Annotated", ctx=ast.Load()),
            slice=ast.Tuple(
                elts=[
                    ast.Name(id="int", ctx=ast.Load()),
                    ast.Call(
                        func=ast.Name(id="Field", ctx=ast.Load()),
                        args=[],
                        keywords=[
                            ast.keyword(
                                arg="ge",
                                value=ast.Constant(value=value),
                            )
                        ],
                    ),
                ],
                ctx=ast.Load(),
            ),
            ctx=ast.Load(),
        )
        return (
            annotation,
            NO_DEFAULT,
            _ANNOTATED_IMPORTS,
            "/annotation/metadata/0/ge",
        )
    assert location == "nested-json-default"
    annotation = ast.Subscript(
        value=ast.Name(id="dict", ctx=ast.Load()),
        slice=ast.Tuple(
            elts=[
                ast.Name(id="str", ctx=ast.Load()),
                ast.Name(id="JSONValue", ctx=ast.Load()),
            ],
            ctx=ast.Load(),
        ),
        ctx=ast.Load(),
    )
    return (
        annotation,
        _nested_json_default(value),
        _JSON_IMPORTS,
        "/default",
    )


def _parse_authoring_case(
    annotation: ast.expr,
    default: ast.expr | object,
    imports: MappingProxyType[str, str],
) -> Any:
    return parse_parameter_annotation(
        "value",
        annotation,
        default=default,
        imports=imports,
    )


@pytest.mark.parametrize(
    ("location", "negative"),
    [
        pytest.param("literal", False, id="literal"),
        pytest.param("default", True, id="default-negative"),
        pytest.param("field-bound", False, id="field-bound"),
        pytest.param(
            "nested-json-default",
            True,
            id="nested-json-default-negative",
        ),
    ],
)
def test_authoring_accepts_4096_digit_integer_and_annotation_round_trips(
    location: str,
    negative: bool,
) -> None:
    initial_limit = sys.get_int_max_str_digits()
    value = 10 ** (_AUTHORING_INTEGER_DIGITS - 1)
    if negative:
        value = -value
    annotation, default, imports, _ = _authoring_case(location, value)

    parameter = _parse_authoring_case(annotation, default, imports)
    rendered_source = ast.unparse(render_parameter_annotation(parameter))
    rendered = ast.parse(rendered_source, mode="eval").body
    reparsed = _parse_authoring_case(rendered, default, imports)

    assert reparsed.to_dict() == parameter.to_dict()
    assert sys.get_int_max_str_digits() == initial_limit


@pytest.mark.parametrize(
    ("location", "negative"),
    [
        pytest.param("literal", False, id="literal"),
        pytest.param("default", True, id="default-negative"),
        pytest.param("field-bound", False, id="field-bound"),
        pytest.param(
            "nested-json-default",
            True,
            id="nested-json-default-negative",
        ),
    ],
)
def test_authoring_rejects_4097_digit_integer_with_stable_error(
    location: str,
    negative: bool,
) -> None:
    initial_limit = sys.get_int_max_str_digits()
    value = 10**_AUTHORING_INTEGER_DIGITS
    if negative:
        value = -value
    annotation, default, imports, expected_path = _authoring_case(location, value)

    def operation() -> object:
        return _parse_authoring_case(annotation, default, imports)

    first = _error_signature(operation, AnnotationSchemaError)
    second = _error_signature(operation, AnnotationSchemaError)

    assert first == second
    assert first[1].startswith(expected_path)
    assert sys.get_int_max_str_digits() == initial_limit


def test_authoring_rejects_large_hex_literal_by_canonical_decimal_budget() -> None:
    initial_limit = sys.get_int_max_str_digits()
    source = "Literal[0x" + "f" * 4000 + "]"
    annotation = ast.parse(source, mode="eval").body

    def operation() -> object:
        return parse_parameter_annotation(
            "value",
            annotation,
            default=NO_DEFAULT,
            imports=_LITERAL_IMPORTS,
        )

    first = _error_signature(operation, AnnotationSchemaError)
    second = _error_signature(operation, AnnotationSchemaError)

    assert first == second
    assert first[1].startswith("/annotation")
    assert sys.get_int_max_str_digits() == initial_limit


def test_trusted_input_contract_keeps_integer_values_above_authoring_budget() -> None:
    initial_limit = sys.get_int_max_str_digits()
    trusted_value = 10**4999
    raw = {
        "version": 1,
        "parameters": [
            {
                "name": "value",
                "schema": {
                    "type": "integer",
                    "enum": [trusted_value],
                },
                "required": False,
                "default": trusted_value,
            }
        ],
    }

    contract = parse_input_contract(raw)
    dumped = contract.to_dict()

    assert dumped == raw
    assert sys.get_int_max_str_digits() == initial_limit
