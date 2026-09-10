"""AST literal complex 溢出必须稳定收敛为 Annotation Schema 错误。"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from types import MappingProxyType

import pytest

from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    parse_parameter_annotation,
)

_AUTHORING_INTEGER_DIGITS = 4096
_OVERFLOW_INTEGER_DIGITS = 1024
_HUGE_DECIMAL = "9" * _OVERFLOW_INTEGER_DIGITS
_EMPTY_IMPORTS = MappingProxyType({})
_LITERAL_IMPORTS = MappingProxyType({"Literal": "typing:Literal"})
_FIELD_IMPORTS = MappingProxyType(
    {
        "Annotated": "typing:Annotated",
        "Field": "pydantic:Field",
    }
)
_JSON_IMPORTS = MappingProxyType(
    {"JSONValue": "unilabos.registry.annotations:JSONValue"}
)


@dataclass(frozen=True)
class _LiteralSeam:
    annotation: ast.expr
    default: ast.expr | object
    imports: MappingProxyType[str, str]
    overflow_expression: ast.expr
    expected_path: str


def _expression(operator: str) -> tuple[str, ast.expr]:
    source = f"{_HUGE_DECIMAL} {operator} 1j"
    return source, ast.parse(source, mode="eval").body


def _literal_seam(seam: str, operator: str) -> _LiteralSeam:
    expression_source, expression = _expression(operator)
    if seam == "literal-member":
        annotation = ast.parse(
            f"Literal[{expression_source}]",
            mode="eval",
        ).body
        return _LiteralSeam(
            annotation=annotation,
            default=NO_DEFAULT,
            imports=_LITERAL_IMPORTS,
            overflow_expression=expression,
            expected_path="/annotation",
        )
    if seam == "parameter-default":
        return _LiteralSeam(
            annotation=ast.parse("float", mode="eval").body,
            default=expression,
            imports=_EMPTY_IMPORTS,
            overflow_expression=expression,
            expected_path="/default",
        )
    if seam == "field-bound":
        annotation = ast.parse(
            f"Annotated[float, Field(ge={expression_source})]",
            mode="eval",
        ).body
        return _LiteralSeam(
            annotation=annotation,
            default=NO_DEFAULT,
            imports=_FIELD_IMPORTS,
            overflow_expression=expression,
            expected_path="/annotation/metadata/0/ge",
        )
    assert seam == "nested-json-default"
    default = ast.parse(
        f"{{'outer': [{expression_source}]}}",
        mode="eval",
    ).body
    annotation = ast.parse(
        "dict[str, JSONValue]",
        mode="eval",
    ).body
    return _LiteralSeam(
        annotation=annotation,
        default=default,
        imports=_JSON_IMPORTS,
        overflow_expression=expression,
        expected_path="/default",
    )


def _error_signature(seam: _LiteralSeam) -> tuple[str, str, str]:
    with pytest.raises(AnnotationSchemaError) as caught:
        parse_parameter_annotation(
            "value",
            seam.annotation,
            default=seam.default,
            imports=seam.imports,
        )

    error = caught.value
    assert error.code == "invalid_annotation"
    assert error.path == seam.expected_path
    assert isinstance(error.message, str) and error.message
    assert str(error) == error.message
    assert re.search(r"[\u4e00-\u9fff]", error.message), "错误消息必须是简体中文"
    return error.code, error.path, error.message


@pytest.mark.parametrize(
    ("seam_name", "operator"),
    [
        pytest.param("literal-member", "+", id="literal-positive-complex"),
        pytest.param("literal-member", "-", id="literal-negative-complex"),
        pytest.param("parameter-default", "+", id="default-positive-complex"),
        pytest.param("parameter-default", "-", id="default-negative-complex"),
        pytest.param("field-bound", "+", id="field-positive-complex"),
        pytest.param("field-bound", "-", id="field-negative-complex"),
        pytest.param(
            "nested-json-default",
            "+",
            id="nested-json-positive-complex",
        ),
        pytest.param(
            "nested-json-default",
            "-",
            id="nested-json-negative-complex",
        ),
    ],
)
def test_huge_integer_complex_overflow_is_a_stable_annotation_error(
    seam_name: str,
    operator: str,
) -> None:
    assert len(_HUGE_DECIMAL) == _OVERFLOW_INTEGER_DIGITS
    assert _OVERFLOW_INTEGER_DIGITS < _AUTHORING_INTEGER_DIGITS
    seam = _literal_seam(seam_name, operator)

    with pytest.raises(OverflowError):
        ast.literal_eval(seam.overflow_expression)

    first = _error_signature(seam)
    second = _error_signature(seam)

    assert first == second
