"""工作流（Workflow）v1 Schema 规范值加固回归测试。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

import pytest

_MODULE_NAME = "unilabos.workflow.schema"
_LEGAL_DEEP_OBJECT_DEPTH = 1_200


def _api() -> Any:
    return importlib.import_module(_MODULE_NAME)


def _assert_schema_error(
    operation: Callable[[], object],
    *,
    code: str,
    path: str,
) -> None:
    api = _api()
    with pytest.raises(api.WorkflowSchemaError) as caught:
        operation()

    assert caught.value.code == code
    assert caught.value.path == path


def _make_deep_object(depth: int) -> dict[str, Any]:
    """迭代构造指定容器深度的单链 object。"""

    assert depth > 0
    root: dict[str, Any] = {}
    cursor = root
    for _ in range(depth - 1):
        child: dict[str, Any] = {}
        cursor["next"] = child
        cursor = child
    cursor["value"] = "original"
    return root


def _deep_leaf(value: object, depth: int) -> dict[str, Any]:
    """迭代走到深层 object 叶子，避免测试自身依赖递归比较。"""

    cursor = value
    for _ in range(depth - 1):
        assert type(cursor) is dict
        assert set(cursor) == {"next"}
        cursor = cursor["next"]
    assert type(cursor) is dict
    assert set(cursor) == {"value"}
    return cursor


@pytest.mark.parametrize(
    ("class_name", "illegal_payload"),
    [
        pytest.param(
            "WorkflowValueSchema",
            {"type": "bytes"},
            id="value-schema",
        ),
        pytest.param(
            "WorkflowInputContract",
            {"version": 1, "parameters": "not-a-list"},
            id="input-contract",
        ),
        pytest.param(
            "WorkflowOutputContract",
            {"version": 1, "outputs": "not-a-list"},
            id="output-contract",
        ),
    ],
)
def test_typed_value_objects_reject_direct_public_construction(
    class_name: str,
    illegal_payload: dict[str, object],
) -> None:
    value_object_type = getattr(_api(), class_name)

    with pytest.raises((TypeError, ValueError)):
        value_object_type(illegal_payload)


@pytest.mark.parametrize(
    "value_object_factory",
    [
        pytest.param(
            lambda: _api().parse_value_schema(
                {"type": "string", "enum": ["red", "green"]}
            ),
            id="value-schema",
        ),
        pytest.param(
            lambda: _api().parse_input_contract(
                {
                    "version": 1,
                    "parameters": [
                        {
                            "name": "settings",
                            "schema": {"type": "object"},
                            "required": False,
                            "default": {"nested": [1]},
                        }
                    ],
                }
            ),
            id="input-contract",
        ),
        pytest.param(
            lambda: _api().parse_output_contract(
                {
                    "version": 1,
                    "outputs": [
                        {
                            "name": "status",
                            "schema": {
                                "type": "string",
                                "enum": ["ready", "done"],
                            },
                        }
                    ],
                }
            ),
            id="output-contract",
        ),
    ],
)
def test_typed_value_objects_do_not_expose_mutable_canonical_containers(
    value_object_factory: Callable[[], object],
) -> None:
    value_object = value_object_factory()
    exposed_mutable_attributes: list[str] = []

    for name in dir(value_object):
        if name.startswith("__"):
            continue
        attribute = getattr(value_object, name)
        if isinstance(attribute, (dict, list, set)):
            exposed_mutable_attributes.append(name)

    assert exposed_mutable_attributes == []


@pytest.mark.parametrize(
    ("value_object_factory", "mutate_first_dump"),
    [
        pytest.param(
            lambda: _api().parse_value_schema(
                {"type": "string", "enum": ["red", "green"]}
            ),
            lambda dump: dump["enum"].append("changed"),
            id="value-schema",
        ),
        pytest.param(
            lambda: _api().parse_input_contract(
                {
                    "version": 1,
                    "parameters": [
                        {
                            "name": "settings",
                            "schema": {"type": "object"},
                            "required": False,
                            "default": {"nested": [1]},
                        }
                    ],
                }
            ),
            lambda dump: dump["parameters"][0]["default"]["nested"].append(2),
            id="input-contract",
        ),
        pytest.param(
            lambda: _api().parse_output_contract(
                {
                    "version": 1,
                    "outputs": [
                        {
                            "name": "status",
                            "schema": {
                                "type": "string",
                                "enum": ["ready", "done"],
                            },
                        }
                    ],
                }
            ),
            lambda dump: dump["outputs"][0]["schema"]["enum"].append("changed"),
            id="output-contract",
        ),
    ],
)
def test_typed_value_objects_return_independent_dumps(
    value_object_factory: Callable[[], object],
    mutate_first_dump: Callable[[dict[str, Any]], None],
) -> None:
    value_object = value_object_factory()
    first_dump = value_object.to_dict()
    second_dump = value_object.to_dict()

    mutate_first_dump(first_dump)

    assert value_object.to_dict() == second_dump
    assert first_dump != second_dump


def _double_nullable_schema() -> dict[str, object]:
    return {
        "anyOf": [
            {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"},
                ]
            },
            {"type": "null"},
        ]
    }


@pytest.mark.parametrize(
    ("operation", "path"),
    [
        pytest.param(
            lambda: _api().parse_value_schema(_double_nullable_schema()),
            "/anyOf/0/anyOf",
            id="standalone",
        ),
        pytest.param(
            lambda: _api().parse_input_contract(
                {
                    "version": 1,
                    "parameters": [
                        {
                            "name": "value",
                            "schema": _double_nullable_schema(),
                            "required": False,
                            "default": None,
                        }
                    ],
                }
            ),
            "/parameters/0/schema/anyOf/0/anyOf",
            id="input-contract",
        ),
        pytest.param(
            lambda: _api().parse_output_contract(
                {
                    "version": 1,
                    "outputs": [
                        {
                            "name": "value",
                            "schema": _double_nullable_schema(),
                        }
                    ],
                }
            ),
            "/outputs/0/schema/anyOf/0/anyOf",
            id="output-contract",
        ),
    ],
)
def test_double_nullable_is_rejected_with_stable_prefixed_path(
    operation: Callable[[], object],
    path: str,
) -> None:
    _assert_schema_error(
        operation,
        code="invalid_schema",
        path=path,
    )


def test_deep_opaque_default_can_parse_and_return_independent_dumps() -> None:
    raw_default = _make_deep_object(_LEGAL_DEEP_OBJECT_DEPTH)
    contract = _api().parse_input_contract(
        {
            "version": 1,
            "parameters": [
                {
                    "name": "settings",
                    "schema": {"type": "object"},
                    "required": False,
                    "default": raw_default,
                }
            ],
        }
    )

    first_dump = contract.to_dict()
    second_dump = contract.to_dict()
    first_default = first_dump["parameters"][0]["default"]
    second_default = second_dump["parameters"][0]["default"]

    assert first_default is not raw_default
    assert second_default is not first_default
    assert _deep_leaf(first_default, _LEGAL_DEEP_OBJECT_DEPTH)["value"] == "original"
    assert _deep_leaf(second_default, _LEGAL_DEEP_OBJECT_DEPTH)["value"] == "original"

    _deep_leaf(first_default, _LEGAL_DEEP_OBJECT_DEPTH)["value"] = "first-dump"
    _deep_leaf(raw_default, _LEGAL_DEEP_OBJECT_DEPTH)["value"] = "caller"

    third_default = contract.to_dict()["parameters"][0]["default"]
    assert _deep_leaf(second_default, _LEGAL_DEEP_OBJECT_DEPTH)["value"] == "original"
    assert _deep_leaf(third_default, _LEGAL_DEEP_OBJECT_DEPTH)["value"] == "original"


def test_over_backend_depth_default_reports_stable_contract_error_and_full_path() -> (
    None
):
    api = _api()
    depth = api.MAX_BACKEND_JSON_DEPTH + 1
    raw_default = _make_deep_object(depth)
    expected_path = "/parameters/0/default" + "/next" * (depth - 1)

    _assert_schema_error(
        lambda: api.parse_input_contract(
            {
                "version": 1,
                "parameters": [
                    {
                        "name": "settings",
                        "schema": {"type": "object"},
                        "required": False,
                        "default": raw_default,
                    }
                ],
            }
        ),
        code="invalid_contract",
        path=expected_path,
    )
