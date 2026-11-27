"""工作流（Workflow）v1 合同与严格值 Schema 的公共接口合同测试。"""

from __future__ import annotations

import copy
import importlib
import math
import re
from collections.abc import Callable
from typing import Any

import pytest

_MODULE_NAME = "unilabos.workflow.schema"
_UUID = "5b82f56a-2e2e-4ae8-af8d-c615ac3dbd4e"
_OTHER_UUID = "33a114c3-fc77-4bf5-8655-d695500c4d7a"


def _api() -> Any:
    """延迟导入，让缺失模块表现为目标行为 RED，而不是测试收集错误。"""

    return importlib.import_module(_MODULE_NAME)


def _parse_schema(raw: object) -> Any:
    return _api().parse_value_schema(raw)


def _parse_input(raw: object) -> Any:
    return _api().parse_input_contract(raw)


def _parse_output(raw: object) -> Any:
    return _api().parse_output_contract(raw)


def _normalize(schema: object, raw_value: object) -> object:
    return _api().normalize_value(schema, raw_value)


def _assert_schema_error(
    operation: Callable[[], object],
    *,
    code: str,
    path: str,
) -> None:
    api = _api()
    with pytest.raises(api.WorkflowSchemaError) as caught:
        operation()

    error = caught.value
    assert error.code == code
    assert error.path == path
    assert isinstance(error.message, str)
    assert re.search(r"[\u4e00-\u9fff]", error.message), "错误消息必须是简体中文"


_BASE_SCHEMAS = [
    pytest.param({"type": "string"}, id="string"),
    pytest.param({"type": "integer"}, id="integer"),
    pytest.param({"type": "number"}, id="number"),
    pytest.param({"type": "boolean"}, id="boolean"),
    pytest.param({"type": "object"}, id="opaque-object"),
    pytest.param({"$slot": "ResourceSlot"}, id="resource-slot"),
    pytest.param(
        {"type": "array", "items": {"type": "string"}},
        id="string-list",
    ),
    pytest.param(
        {"type": "array", "items": {"type": "integer"}},
        id="integer-list",
    ),
    pytest.param(
        {"type": "array", "items": {"type": "number"}},
        id="number-list",
    ),
    pytest.param(
        {"type": "array", "items": {"type": "boolean"}},
        id="boolean-list",
    ),
    pytest.param(
        {"type": "array", "items": {"type": "object"}},
        id="opaque-object-list",
    ),
    pytest.param(
        {"type": "array", "items": {"$slot": "ResourceSlot"}},
        id="resource-slot-list",
    ),
]


@pytest.mark.parametrize("raw", _BASE_SCHEMAS)
def test_parse_value_schema_accepts_every_finite_non_null_type(
    raw: dict[str, object],
) -> None:
    assert _parse_schema(raw).to_dict() == raw


@pytest.mark.parametrize("base", _BASE_SCHEMAS)
def test_parse_value_schema_accepts_nullable_wrapper_for_every_complete_type(
    base: dict[str, object],
) -> None:
    raw = {"anyOf": [base, {"type": "null"}]}

    assert _parse_schema(raw).to_dict() == raw


def test_parse_value_schema_canonicalizes_nullable_member_order() -> None:
    raw = {"anyOf": [{"type": "null"}, {"type": "integer"}]}

    assert _parse_schema(raw).to_dict() == {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }


def test_parse_value_schema_normalizes_all_supported_constraints() -> None:
    schema = _parse_schema(
        {
            "type": "array",
            "items": {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [
                    _UUID.upper(),
                    _OTHER_UUID,
                ],
            },
            "minItems": 0,
            "maxItems": 3,
        }
    )

    assert schema.to_dict() == {
        "type": "array",
        "items": {
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": [_UUID, _OTHER_UUID],
        },
        "minItems": 0,
        "maxItems": 3,
    }
    assert _parse_schema(
        {
            "type": "integer",
            "enum": [1.0, 3],
            "minimum": 1,
            "maximum": 3.0,
        }
    ).to_dict() == {
        "type": "integer",
        "enum": [1, 3],
        "minimum": 1,
        "maximum": 3.0,
    }
    assert _parse_schema(
        {
            "type": "string",
            "enum": ["ab", "abcd"],
            "minLength": 2,
            "maxLength": 4,
        }
    ).to_dict() == {
        "type": "string",
        "enum": ["ab", "abcd"],
        "minLength": 2,
        "maxLength": 4,
    }


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        pytest.param([], "", id="schema-not-object"),
        pytest.param({}, "", id="empty-schema"),
        pytest.param({"type": "null"}, "/type", id="bare-null"),
        pytest.param({"type": "bytes"}, "/type", id="unsupported-type"),
        pytest.param({"type": "string", "format": "uuid"}, "/format", id="unknown-key"),
        pytest.param(
            {"$slot": "ResourceSlot", "type": "object"},
            "/type",
            id="mixed-slot-and-type",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
            "/items/type",
            id="nested-declared-list",
        ),
        pytest.param(
            {
                "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "/items/anyOf",
            id="nullable-list-item",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "string", "extra": True}},
            "/items/extra",
            id="closed-item-schema",
        ),
        pytest.param(
            {"anyOf": [{"type": "string"}]},
            "/anyOf",
            id="nullable-needs-two-members",
        ),
        pytest.param(
            {
                "anyOf": [
                    {"type": "string"},
                    {"type": "integer"},
                    {"type": "null"},
                ]
            },
            "/anyOf",
            id="nullable-needs-one-base",
        ),
        pytest.param(
            {"anyOf": [{"type": "null"}, {"type": "null"}]},
            "/anyOf",
            id="nullable-needs-one-non-null",
        ),
        pytest.param(
            {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null", "description": "no"},
                ]
            },
            "/anyOf/1/description",
            id="closed-null-member",
        ),
        pytest.param(
            {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "no",
            },
            "/description",
            id="closed-nullable-schema",
        ),
        pytest.param(
            {"type": "object", "properties": {}},
            "/properties",
            id="opaque-object-has-no-properties",
        ),
    ],
)
def test_parse_value_schema_rejects_unsupported_or_open_shapes(
    raw: object,
    path: str,
) -> None:
    _assert_schema_error(
        lambda: _parse_schema(raw),
        code="invalid_schema",
        path=path,
    )


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        pytest.param({"type": "string", "enum": []}, "/enum", id="empty-enum"),
        pytest.param(
            {"type": "string", "enum": ["x", "x"]},
            "/enum/1",
            id="duplicate-enum",
        ),
        pytest.param(
            {"type": "integer", "enum": [1, 1.0]},
            "/enum/1",
            id="duplicate-after-normalization",
        ),
        pytest.param(
            {"type": "integer", "enum": [True]},
            "/enum/0",
            id="bool-not-integer-enum",
        ),
        pytest.param(
            {"type": "number", "enum": [math.inf]},
            "/enum/0",
            id="enum-must-be-finite",
        ),
        pytest.param(
            {"type": "string", "enum": ["x"], "minLength": 2},
            "/enum/0",
            id="enum-obeys-other-constraints",
        ),
        pytest.param(
            {"type": "object", "enum": [{}]},
            "/enum",
            id="object-has-no-enum",
        ),
        pytest.param(
            {"$slot": "ResourceSlot", "enum": [{"uuid": _UUID}]},
            "/enum",
            id="slot-has-no-enum",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "string"}, "enum": [[]]},
            "/enum",
            id="array-has-no-enum",
        ),
        pytest.param(
            {"type": "number", "minimum": math.nan},
            "/minimum",
            id="minimum-must-be-finite",
        ),
        pytest.param(
            {"type": "integer", "maximum": True},
            "/maximum",
            id="numeric-bound-excludes-bool",
        ),
        pytest.param(
            {"type": "number", "minimum": 3, "maximum": 2},
            "/maximum",
            id="numeric-bounds-consistent",
        ),
        pytest.param(
            {"type": "string", "minLength": -1},
            "/minLength",
            id="string-bound-non-negative",
        ),
        pytest.param(
            {"type": "string", "maxLength": 1.0},
            "/maxLength",
            id="string-bound-is-integer",
        ),
        pytest.param(
            {"type": "string", "minLength": True},
            "/minLength",
            id="string-bound-excludes-bool",
        ),
        pytest.param(
            {"type": "string", "minLength": 4, "maxLength": 3},
            "/maxLength",
            id="string-bounds-consistent",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "string"}, "minItems": -1},
            "/minItems",
            id="array-bound-non-negative",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "string"}, "maxItems": False},
            "/maxItems",
            id="array-bound-excludes-bool",
        ),
        pytest.param(
            {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 1,
            },
            "/maxItems",
            id="array-bounds-consistent",
        ),
        pytest.param(
            {"type": "boolean", "minimum": 0},
            "/minimum",
            id="constraint-only-on-matching-type",
        ),
    ],
)
def test_parse_value_schema_rejects_invalid_constraints(
    raw: dict[str, object],
    path: str,
) -> None:
    _assert_schema_error(
        lambda: _parse_schema(raw),
        code="invalid_schema",
        path=path,
    )


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        pytest.param(
            {"$slot": "ResourceSlot", "allowed_resource_template_uuids": []},
            "/allowed_resource_template_uuids",
            id="empty-allowlist",
        ),
        pytest.param(
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [_UUID, _UUID.upper()],
            },
            "/allowed_resource_template_uuids/1",
            id="duplicate-normalized-template-uuid",
        ),
        pytest.param(
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": ["not-a-uuid"],
            },
            "/allowed_resource_template_uuids/0",
            id="invalid-template-uuid",
        ),
        pytest.param(
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": _UUID,
            },
            "/allowed_resource_template_uuids",
            id="allowlist-must-be-array",
        ),
        pytest.param(
            {"type": "string", "allowed_resource_template_uuids": [_UUID]},
            "/allowed_resource_template_uuids",
            id="allowlist-only-on-slot",
        ),
    ],
)
def test_parse_value_schema_rejects_invalid_resource_slot_allowlist(
    raw: dict[str, object],
    path: str,
) -> None:
    _assert_schema_error(
        lambda: _parse_schema(raw),
        code="invalid_schema",
        path=path,
    )


@pytest.mark.parametrize(
    ("raw_schema", "raw_value", "expected"),
    [
        pytest.param({"type": "string"}, "sample", "sample", id="string"),
        pytest.param({"type": "boolean"}, False, False, id="boolean"),
        pytest.param({"type": "integer"}, 3, 3, id="integer"),
        pytest.param({"type": "integer"}, 3.0, 3, id="integral-float"),
        pytest.param({"type": "number"}, 3, 3, id="integer-widens-to-number"),
        pytest.param({"type": "number"}, 3.25, 3.25, id="fractional-number"),
        pytest.param(
            {"type": "object"},
            {
                "null": None,
                "bool": True,
                "number": 1.5,
                "list": ["x", {"nested": 2}],
            },
            {
                "null": None,
                "bool": True,
                "number": 1.5,
                "list": ["x", {"nested": 2}],
            },
            id="recursive-opaque-json",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "integer"}},
            [1, 2.0],
            [1, 2],
            id="homogeneous-array",
        ),
        pytest.param(
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [_OTHER_UUID],
            },
            {"uuid": _UUID.upper()},
            {"uuid": _UUID},
            id="closed-slot-reference-without-material-lookup",
        ),
        pytest.param(
            {"type": "array", "items": {"$slot": "ResourceSlot"}},
            [{"uuid": _UUID}, {"uuid": _UUID}, {"uuid": _OTHER_UUID.upper()}],
            [{"uuid": _UUID}, {"uuid": _UUID}, {"uuid": _OTHER_UUID}],
            id="slot-list-preserves-order-and-duplicates",
        ),
        pytest.param(
            {"anyOf": [{"type": "string", "minLength": 2}, {"type": "null"}]},
            None,
            None,
            id="nullable-bypasses-constraints",
        ),
        pytest.param(
            {"type": "integer", "minimum": 2, "maximum": 4},
            2,
            2,
            id="inclusive-minimum",
        ),
        pytest.param(
            {"type": "integer", "minimum": 2, "maximum": 4},
            4,
            4,
            id="inclusive-maximum",
        ),
        pytest.param(
            {"type": "string", "enum": ["red", "green"]},
            "green",
            "green",
            id="enum-member",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "boolean"}, "minItems": 0},
            [],
            [],
            id="empty-list-is-value",
        ),
    ],
)
def test_normalize_value_accepts_and_canonicalizes_strict_values(
    raw_schema: dict[str, object],
    raw_value: object,
    expected: object,
) -> None:
    schema = _parse_schema(raw_schema)

    assert _normalize(schema, raw_value) == expected


@pytest.mark.parametrize(
    ("raw_schema", "raw_value", "path"),
    [
        pytest.param({"type": "string"}, 1, "", id="string-never-parses-number"),
        pytest.param({"type": "boolean"}, 1, "", id="one-is-not-bool"),
        pytest.param({"type": "boolean"}, "false", "", id="string-is-not-bool"),
        pytest.param({"type": "integer"}, True, "", id="bool-is-not-integer"),
        pytest.param({"type": "integer"}, 3.5, "", id="fraction-is-not-integer"),
        pytest.param({"type": "number"}, False, "", id="bool-is-not-number"),
        pytest.param({"type": "number"}, math.nan, "", id="nan-is-not-json-number"),
        pytest.param(
            {"type": "number"}, math.inf, "", id="infinity-is-not-json-number"
        ),
        pytest.param({"type": "object"}, [], "", id="object-requires-dict"),
        pytest.param({"type": "object"}, {1: "x"}, "", id="object-keys-are-strings"),
        pytest.param(
            {"type": "object"},
            {"a/b": {"~key": [0, math.nan]}},
            "/a~1b/~0key/1",
            id="recursive-json-error-pointer-is-escaped",
        ),
        pytest.param(
            {"type": "object"},
            {"nested": (1, 2)},
            "/nested",
            id="tuple-is-not-json-array",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "integer"}},
            (1, 2),
            "",
            id="declared-list-requires-list",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "integer"}},
            [1, True],
            "/1",
            id="array-item-is-strict",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "string"}},
            ["x", None],
            "/1",
            id="null-item-is-not-omission",
        ),
        pytest.param({"$slot": "ResourceSlot"}, _UUID, "", id="slot-is-not-bare-uuid"),
        pytest.param(
            {"$slot": "ResourceSlot"},
            {"uuid": _UUID, "children": []},
            "/children",
            id="slot-reference-is-closed",
        ),
        pytest.param(
            {"$slot": "ResourceSlot"},
            {"uuid": "invalid"},
            "/uuid",
            id="slot-uuid-is-valid",
        ),
        pytest.param(
            {"$slot": "ResourceSlot"},
            {"uuid": _UUID, "resource_template_uuid": _OTHER_UUID},
            "/resource_template_uuid",
            id="caller-cannot-inject-template",
        ),
        pytest.param(
            {"type": "integer", "minimum": 2},
            1,
            "",
            id="minimum-enforced",
        ),
        pytest.param(
            {"type": "string", "maxLength": 2},
            "abc",
            "",
            id="max-length-enforced",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "string"}, "minItems": 2},
            ["one"],
            "",
            id="min-items-enforced",
        ),
        pytest.param(
            {"type": "integer", "enum": [1]},
            True,
            "",
            id="enum-does-not-equate-true-and-one",
        ),
        pytest.param(
            {"type": "integer", "enum": [1]},
            2,
            "",
            id="enum-membership-enforced",
        ),
        pytest.param(
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            1,
            "",
            id="nullable-still-strict-when-non-null",
        ),
    ],
)
def test_normalize_value_rejects_coercion_non_json_and_constraint_violations(
    raw_schema: dict[str, object],
    raw_value: object,
    path: str,
) -> None:
    schema = _parse_schema(raw_schema)

    _assert_schema_error(
        lambda: _normalize(schema, raw_value),
        code="invalid_value",
        path=path,
    )


def test_schema_and_normalized_values_do_not_mutate_or_share_caller_containers() -> (
    None
):
    raw_schema = {
        "type": "string",
        "enum": ["one", "two"],
    }
    original_schema = copy.deepcopy(raw_schema)
    schema = _parse_schema(raw_schema)
    first_dump = schema.to_dict()
    first_dump["enum"].append("mutated")

    assert raw_schema == original_schema
    assert schema.to_dict() == original_schema

    raw_value = {"nested": [{"value": 1}]}
    original_value = copy.deepcopy(raw_value)
    normalized = _normalize(_parse_schema({"type": "object"}), raw_value)
    normalized["nested"][0]["value"] = 2

    assert raw_value == original_value
    raw_value["nested"][0]["value"] = 3
    assert normalized == {"nested": [{"value": 2}]}


def test_parsed_schema_is_an_immutable_typed_value_object() -> None:
    schema = _parse_schema({"type": "string"})

    with pytest.raises((AttributeError, TypeError)):
        setattr(schema, "_mutation_probe", True)  # noqa: B010


def test_parse_input_contract_accepts_exactly_the_three_declaration_shapes() -> None:
    raw = {
        "version": 1,
        "parameters": [
            {
                "name": "required_name",
                "schema": {"type": "string"},
                "required": True,
                "title": "  Required name  ",
                "description": "  Must be supplied  ",
            },
            {
                "name": "attempts",
                "schema": {"type": "integer"},
                "required": False,
                "default": 3.0,
            },
            {
                "name": "optional_sample",
                "schema": {
                    "anyOf": [
                        {"$slot": "ResourceSlot"},
                        {"type": "null"},
                    ]
                },
                "required": False,
                "default": None,
            },
        ],
    }

    assert _parse_input(raw).to_dict() == {
        "version": 1,
        "parameters": [
            {
                "name": "required_name",
                "schema": {"type": "string"},
                "required": True,
                "title": "Required name",
                "description": "Must be supplied",
            },
            {
                "name": "attempts",
                "schema": {"type": "integer"},
                "required": False,
                "default": 3,
            },
            {
                "name": "optional_sample",
                "schema": {
                    "anyOf": [
                        {"$slot": "ResourceSlot"},
                        {"type": "null"},
                    ]
                },
                "required": False,
                "default": None,
            },
        ],
    }


def test_parse_input_contract_allows_only_empty_default_for_non_null_slot_list() -> (
    None
):
    raw = {
        "version": 1,
        "parameters": [
            {
                "name": "samples",
                "schema": {
                    "type": "array",
                    "items": {"$slot": "ResourceSlot"},
                },
                "required": False,
                "default": [],
            }
        ],
    }

    assert _parse_input(raw).to_dict() == raw


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        pytest.param([], "", id="envelope-is-object"),
        pytest.param({"version": 1}, "/parameters", id="parameters-required"),
        pytest.param(
            {"version": True, "parameters": []},
            "/version",
            id="version-excludes-bool",
        ),
        pytest.param(
            {"version": 2, "parameters": []},
            "/version",
            id="only-version-one",
        ),
        pytest.param(
            {"version": 1, "parameters": [], "extra": True},
            "/extra",
            id="envelope-closed",
        ),
        pytest.param(
            {"version": 1, "parameters": {}},
            "/parameters",
            id="parameters-is-list",
        ),
        pytest.param(
            {
                "version": 1,
                "parameters": [
                    {
                        "name": "value",
                        "schema": {"type": "string"},
                        "required": True,
                        "x": 1,
                    }
                ],
            },
            "/parameters/0/x",
            id="descriptor-closed",
        ),
        pytest.param(
            {
                "version": 1,
                "parameters": [
                    {
                        "name": "not-valid",
                        "schema": {"type": "string"},
                        "required": True,
                    }
                ],
            },
            "/parameters/0/name",
            id="name-is-identifier",
        ),
        pytest.param(
            {
                "version": 1,
                "parameters": [
                    {
                        "name": "class",
                        "schema": {"type": "string"},
                        "required": True,
                    }
                ],
            },
            "/parameters/0/name",
            id="name-is-not-keyword",
        ),
        pytest.param(
            {
                "version": 1,
                "parameters": [
                    {"name": "x", "schema": {"type": "string"}, "required": True},
                    {"name": "x", "schema": {"type": "integer"}, "required": True},
                ],
            },
            "/parameters/1/name",
            id="names-unique",
        ),
        pytest.param(
            {
                "version": 1,
                "parameters": [
                    {
                        "name": "value",
                        "schema": {"type": "string"},
                        "required": "yes",
                    }
                ],
            },
            "/parameters/0/required",
            id="required-is-bool",
        ),
        pytest.param(
            {
                "version": 1,
                "parameters": [{"name": "value", "schema": {"type": "string"}}],
            },
            "/parameters/0/required",
            id="required-field-is-present",
        ),
        pytest.param(
            {
                "version": 1,
                "parameters": [
                    {
                        "name": "value",
                        "schema": {"type": "string"},
                        "required": True,
                        "title": "  ",
                    }
                ],
            },
            "/parameters/0/title",
            id="trimmed-title-non-empty",
        ),
        pytest.param(
            {
                "version": 1,
                "parameters": [
                    {
                        "name": "value",
                        "schema": {"type": "string"},
                        "required": True,
                        "description": 3,
                    }
                ],
            },
            "/parameters/0/description",
            id="description-is-string",
        ),
    ],
)
def test_parse_input_contract_rejects_open_or_malformed_structure(
    raw: object,
    path: str,
) -> None:
    _assert_schema_error(
        lambda: _parse_input(raw),
        code="invalid_contract",
        path=path,
    )


@pytest.mark.parametrize(
    ("descriptor", "path"),
    [
        pytest.param(
            {
                "name": "value",
                "schema": {"type": "string"},
                "required": True,
                "default": "x",
            },
            "/parameters/0/default",
            id="required-has-no-default",
        ),
        pytest.param(
            {
                "name": "value",
                "schema": {"type": "string"},
                "required": False,
            },
            "/parameters/0/default",
            id="optional-non-null-needs-default",
        ),
        pytest.param(
            {
                "name": "value",
                "schema": {"type": "string"},
                "required": False,
                "default": None,
            },
            "/parameters/0/default",
            id="non-null-default-is-non-null",
        ),
        pytest.param(
            {
                "name": "value",
                "schema": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "required": True,
            },
            "/parameters/0/schema/anyOf",
            id="required-cannot-be-nullable",
        ),
        pytest.param(
            {
                "name": "value",
                "schema": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "required": False,
                "default": "x",
            },
            "/parameters/0/default",
            id="nullable-default-is-null",
        ),
        pytest.param(
            {
                "name": "count",
                "schema": {"type": "integer", "minimum": 2},
                "required": False,
                "default": 1,
            },
            "/parameters/0/default",
            id="default-obeys-schema",
        ),
        pytest.param(
            {
                "name": "sample",
                "schema": {"$slot": "ResourceSlot"},
                "required": False,
                "default": {"uuid": _UUID},
            },
            "/parameters/0/default",
            id="slot-cannot-select-material-default",
        ),
        pytest.param(
            {
                "name": "samples",
                "schema": {
                    "type": "array",
                    "items": {"$slot": "ResourceSlot"},
                },
                "required": False,
                "default": [{"uuid": _UUID}],
            },
            "/parameters/0/default",
            id="slot-list-default-must-be-empty",
        ),
    ],
)
def test_parse_input_contract_rejects_every_illegal_required_default_null_combination(
    descriptor: dict[str, object],
    path: str,
) -> None:
    raw = {"version": 1, "parameters": [descriptor]}

    _assert_schema_error(
        lambda: _parse_input(raw),
        code="invalid_contract",
        path=path,
    )


def test_input_contract_owns_defaults_and_returns_fresh_dumps() -> None:
    raw = {
        "version": 1,
        "parameters": [
            {
                "name": "settings",
                "schema": {"type": "object"},
                "required": False,
                "default": {"nested": [1, {"enabled": True}]},
            }
        ],
    }
    original = copy.deepcopy(raw)
    contract = _parse_input(raw)
    first_dump = contract.to_dict()
    first_dump["parameters"][0]["default"]["nested"][1]["enabled"] = False

    assert raw == original
    raw["parameters"][0]["default"]["nested"][0] = 2
    assert contract.to_dict() == original
    with pytest.raises((AttributeError, TypeError)):
        setattr(contract, "_mutation_probe", True)  # noqa: B010


def test_parse_output_contract_defaults_implicit_to_false_and_preserves_order() -> None:
    raw = {
        "version": 1,
        "outputs": [
            {
                "name": "result",
                "schema": {"type": "integer"},
                "title": "  Result  ",
                "description": "  Final value  ",
            },
            {
                "name": "sample",
                "schema": {
                    "anyOf": [
                        {"$slot": "ResourceSlot"},
                        {"type": "null"},
                    ]
                },
                "implicit": True,
            },
        ],
    }

    assert _parse_output(raw).to_dict() == {
        "version": 1,
        "outputs": [
            {
                "name": "result",
                "schema": {"type": "integer"},
                "title": "Result",
                "description": "Final value",
                "implicit": False,
            },
            {
                "name": "sample",
                "schema": {
                    "anyOf": [
                        {"$slot": "ResourceSlot"},
                        {"type": "null"},
                    ]
                },
                "implicit": True,
            },
        ],
    }


def test_parse_output_contract_accepts_empty_contract() -> None:
    raw = {"version": 1, "outputs": []}

    assert _parse_output(raw).to_dict() == raw


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        pytest.param([], "", id="envelope-is-object"),
        pytest.param({"version": 1}, "/outputs", id="outputs-required"),
        pytest.param(
            {"version": 2, "outputs": []},
            "/version",
            id="only-version-one",
        ),
        pytest.param(
            {"version": 1, "outputs": [], "extra": True},
            "/extra",
            id="envelope-closed",
        ),
        pytest.param(
            {"version": 1, "outputs": {}},
            "/outputs",
            id="outputs-is-list",
        ),
        pytest.param(
            {
                "version": 1,
                "outputs": [
                    {
                        "name": "result",
                        "schema": {"type": "integer"},
                        "unknown": True,
                    }
                ],
            },
            "/outputs/0/unknown",
            id="descriptor-closed",
        ),
        pytest.param(
            {
                "version": 1,
                "outputs": [{"name": "", "schema": {"type": "integer"}}],
            },
            "/outputs/0/name",
            id="name-non-empty",
        ),
        pytest.param(
            {
                "version": 1,
                "outputs": [
                    {"name": "x", "schema": {"type": "integer"}},
                    {"name": "x", "schema": {"type": "string"}},
                ],
            },
            "/outputs/1/name",
            id="names-unique",
        ),
        pytest.param(
            {
                "version": 1,
                "outputs": [
                    {
                        "name": "result",
                        "schema": {"type": "integer"},
                        "implicit": 1,
                    }
                ],
            },
            "/outputs/0/implicit",
            id="implicit-is-strict-bool",
        ),
        pytest.param(
            {
                "version": 1,
                "outputs": [
                    {
                        "name": "result",
                        "schema": {"type": "integer"},
                        "required": True,
                    }
                ],
            },
            "/outputs/0/required",
            id="output-has-no-required",
        ),
        pytest.param(
            {
                "version": 1,
                "outputs": [
                    {
                        "name": "result",
                        "schema": {"type": "integer"},
                        "default": 0,
                    }
                ],
            },
            "/outputs/0/default",
            id="output-has-no-default",
        ),
        pytest.param(
            {
                "version": 1,
                "outputs": [
                    {
                        "name": "result",
                        "schema": {"type": "integer"},
                        "title": " ",
                    }
                ],
            },
            "/outputs/0/title",
            id="trimmed-title-non-empty",
        ),
    ],
)
def test_parse_output_contract_rejects_open_malformed_or_defaulted_outputs(
    raw: object,
    path: str,
) -> None:
    _assert_schema_error(
        lambda: _parse_output(raw),
        code="invalid_contract",
        path=path,
    )


def test_nested_schema_errors_include_contract_json_pointer_prefix() -> None:
    _assert_schema_error(
        lambda: _parse_input(
            {
                "version": 1,
                "parameters": [
                    {
                        "name": "value",
                        "schema": {"type": "string", "format": "uuid"},
                        "required": True,
                    }
                ],
            }
        ),
        code="invalid_schema",
        path="/parameters/0/schema/format",
    )
    _assert_schema_error(
        lambda: _parse_output(
            {
                "version": 1,
                "outputs": [
                    {
                        "name": "value",
                        "schema": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "string"}},
                        },
                    }
                ],
            }
        ),
        code="invalid_schema",
        path="/outputs/0/schema/items/type",
    )
