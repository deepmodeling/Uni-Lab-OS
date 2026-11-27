"""工作流（Workflow）公共 JSON 资源预算与完整值深度合同测试。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from unilabos.workflow import json_codec
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    normalize_value,
    parse_input_contract,
    parse_value_schema,
)

_EXTERNAL_INTEGER_DIGITS = 4096


@pytest.fixture(autouse=True)
def _preserve_interpreter_int_string_limit() -> Iterator[None]:
    """公共解码器不能修改进程级整数转换限制。"""

    original = sys.get_int_max_str_digits()
    yield
    assert sys.get_int_max_str_digits() == original


def _integer_token(digits: int, *, negative: bool = False) -> bytes:
    prefix = b"-1" if negative else b"1"
    return prefix + b"0" * (digits - 1)


@pytest.mark.parametrize("negative", [False, True], ids=["positive", "negative"])
def test_external_integer_budget_accepts_4096_digits(negative: bool) -> None:
    token = _integer_token(_EXTERNAL_INTEGER_DIGITS, negative=negative)
    expected = 10 ** (_EXTERNAL_INTEGER_DIGITS - 1)
    if negative:
        expected = -expected

    decoded = decode_json_bytes(
        token,
        max_integer_digits=_EXTERNAL_INTEGER_DIGITS,
    )

    assert type(decoded) is int
    assert decoded == expected


@pytest.mark.parametrize("negative", [False, True], ids=["positive", "negative"])
def test_external_integer_budget_rejects_4097_digits_before_bigint_construction(
    monkeypatch: pytest.MonkeyPatch,
    negative: bool,
) -> None:
    decoder_calls: list[str] = []

    def record_bigint_construction(raw: str) -> int:
        decoder_calls.append(raw)
        return 0

    monkeypatch.setattr(
        json_codec,
        "_decode_json_integer",
        record_bigint_construction,
    )

    with pytest.raises(ValueError):
        decode_json_bytes(
            _integer_token(_EXTERNAL_INTEGER_DIGITS + 1, negative=negative),
            max_integer_digits=_EXTERNAL_INTEGER_DIGITS,
        )

    assert decoder_calls == []


def test_trusted_decoder_default_still_round_trips_5001_digit_integer() -> None:
    value = -(10**5000)

    encoded = encode_json(value)
    decoded = decode_json_bytes(encoded)

    assert encoded == _integer_token(5001, negative=True)
    assert type(decoded) is int
    assert decoded == value


def _make_deep_object(depth: int) -> dict[str, Any]:
    """迭代构造指定对象容器深度的单链 JSON。"""

    assert depth > 0
    root: dict[str, Any] = {}
    cursor = root
    for _ in range(depth - 1):
        child: dict[str, Any] = {}
        cursor["next"] = child
        cursor = child
    cursor["value"] = "leaf"
    return root


def _assert_deep_object(value: object, depth: int) -> None:
    """迭代验证深链，避免测试自身触发递归比较。"""

    cursor = value
    for _ in range(depth - 1):
        assert type(cursor) is dict
        assert set(cursor) == {"next"}
        cursor = cursor["next"]
    assert cursor == {"value": "leaf"}


def _assert_schema_error(
    operation: Callable[[], object],
    *,
    code: str,
    path: str,
) -> None:
    with pytest.raises(WorkflowSchemaError) as caught:
        operation()

    assert caught.value.code == code
    assert caught.value.path == path


def test_standalone_opaque_object_accepts_complete_depth_10000() -> None:
    raw = _make_deep_object(10000)

    normalized = normalize_value(
        parse_value_schema({"type": "object"}),
        raw,
    )

    assert normalized is not raw
    _assert_deep_object(normalized, 10000)


def test_standalone_opaque_object_rejects_complete_depth_10001() -> None:
    raw = _make_deep_object(10001)

    _assert_schema_error(
        lambda: normalize_value(
            parse_value_schema({"type": "object"}),
            raw,
        ),
        code="invalid_value",
        path="/next" * 10000,
    )


def test_list_of_opaque_object_accepts_item_depth_9999() -> None:
    raw_item = _make_deep_object(9999)

    normalized = normalize_value(
        parse_value_schema(
            {
                "type": "array",
                "items": {"type": "object"},
            }
        ),
        [raw_item],
    )

    assert type(normalized) is list
    assert normalized[0] is not raw_item
    _assert_deep_object(normalized[0], 9999)


def test_list_of_opaque_object_rejects_item_depth_10000_with_full_pointer() -> None:
    raw_item = _make_deep_object(10000)

    _assert_schema_error(
        lambda: normalize_value(
            parse_value_schema(
                {
                    "type": "array",
                    "items": {"type": "object"},
                }
            ),
            [raw_item],
        ),
        code="invalid_value",
        path="/0" + "/next" * 9999,
    )


def _input_contract_with_default(default: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "parameters": [
            {
                "name": "settings",
                "schema": {"type": "object"},
                "required": False,
                "default": default,
            }
        ],
    }


def test_input_default_depth_9997_can_parse_and_dump() -> None:
    raw_default = _make_deep_object(9997)

    contract = parse_input_contract(_input_contract_with_default(raw_default))
    dumped_default = contract.to_dict()["parameters"][0]["default"]

    assert dumped_default is not raw_default
    _assert_deep_object(dumped_default, 9997)


def test_input_default_depth_9998_is_invalid_contract_with_full_pointer() -> None:
    raw_default = _make_deep_object(9998)

    _assert_schema_error(
        lambda: parse_input_contract(_input_contract_with_default(raw_default)),
        code="invalid_contract",
        path="/parameters/0/default" + "/next" * 9997,
    )
