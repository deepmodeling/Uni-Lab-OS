"""工作流（Workflow）Schema 规范值与 JSON 大整数 codec 加固测试。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.schema import (
    normalize_value,
    parse_input_contract,
    parse_output_contract,
    parse_value_schema,
)

_HUGE_POSITIVE = 10**5000
_HUGE_NEGATIVE = -(10**5000)
_HUGE_POSITIVE_TOKEN = b"1" + b"0" * 5000
_HUGE_NEGATIVE_TOKEN = b"-1" + b"0" * 5000


@pytest.fixture(autouse=True)
def _preserve_interpreter_int_string_limit() -> Iterator[None]:
    """所有公共操作都不得修改进程级整数转换限制。"""

    original = sys.get_int_max_str_digits()
    yield
    assert sys.get_int_max_str_digits() == original


@pytest.mark.parametrize(
    "value_object_factory",
    [
        pytest.param(
            lambda: parse_value_schema({"type": "integer"}),
            id="value-schema",
        ),
        pytest.param(
            lambda: parse_input_contract({"version": 1, "parameters": []}),
            id="input-contract",
        ),
        pytest.param(
            lambda: parse_output_contract({"version": 1, "outputs": []}),
            id="output-contract",
        ),
    ],
)
def test_typed_value_object_rejects_payload_deletion_and_remains_intact(
    value_object_factory: Callable[[], Any],
) -> None:
    value_object = value_object_factory()
    canonical = value_object.to_dict()

    with pytest.raises(AttributeError):
        del value_object._payload

    assert value_object.to_dict() == canonical


@pytest.mark.parametrize(
    ("value", "expected_token"),
    [
        pytest.param(
            _HUGE_POSITIVE,
            _HUGE_POSITIVE_TOKEN,
            id="positive-5001-digits",
        ),
        pytest.param(
            _HUGE_NEGATIVE,
            _HUGE_NEGATIVE_TOKEN,
            id="negative-5001-digits",
        ),
    ],
)
def test_json_codec_round_trips_huge_integer_as_standard_number_token(
    value: int,
    expected_token: bytes,
) -> None:
    encoded = encode_json(value)

    assert encoded == expected_token
    assert not encoded.startswith(b'"')
    assert not encoded.endswith(b'"')
    decoded = decode_json_bytes(encoded)
    assert type(decoded) is int
    assert decoded == value


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        pytest.param(
            _HUGE_POSITIVE_TOKEN,
            _HUGE_POSITIVE,
            id="positive-5001-digits",
        ),
        pytest.param(
            _HUGE_NEGATIVE_TOKEN,
            _HUGE_NEGATIVE,
            id="negative-5001-digits",
        ),
    ],
)
def test_json_codec_decodes_external_huge_integer_token(
    token: bytes,
    expected: int,
) -> None:
    decoded = decode_json_bytes(token)

    assert type(decoded) is int
    assert decoded == expected


def test_schema_round_trips_huge_integer_bounds() -> None:
    raw = {
        "type": "integer",
        "minimum": _HUGE_NEGATIVE,
        "maximum": _HUGE_POSITIVE,
    }

    dumped = parse_value_schema(raw).to_dict()

    assert dumped.keys() == raw.keys()
    assert type(dumped["minimum"]) is int
    assert type(dumped["maximum"]) is int
    assert dumped["minimum"] == _HUGE_NEGATIVE
    assert dumped["maximum"] == _HUGE_POSITIVE


def test_input_contract_round_trips_huge_integer_default() -> None:
    contract = parse_input_contract(
        {
            "version": 1,
            "parameters": [
                {
                    "name": "huge_count",
                    "schema": {"type": "integer"},
                    "required": False,
                    "default": _HUGE_POSITIVE,
                }
            ],
        }
    )

    default = contract.to_dict()["parameters"][0]["default"]
    assert type(default) is int
    assert default == _HUGE_POSITIVE


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("integer", id="integer"),
        pytest.param("number", id="number"),
    ],
)
def test_normalize_value_preserves_huge_integer(kind: str) -> None:
    normalized = normalize_value(
        parse_value_schema({"type": kind}),
        _HUGE_NEGATIVE,
    )

    assert type(normalized) is int
    assert normalized == _HUGE_NEGATIVE
