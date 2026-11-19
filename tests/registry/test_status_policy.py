from __future__ import annotations

import json
import math
from copy import deepcopy

import pytest

from unilabos.registry.status_policy import (
    StatusEvaluation,
    evaluate_status,
    normalize_status_policy,
)


def test_normalize_generates_json_safe_canonical_registry_policy() -> None:
    source = {
        "healthy_values": ("Idle", "Running", "Idle"),
        "incidents": {
            "Warning": "需要检查设备",
            "Error": {
                "code": "stirrer.error",
                "severity": "CRITICAL",
                "message": "设备故障",
            },
        },
    }
    original = deepcopy(source)

    normalized = normalize_status_policy(source)

    assert source == original
    assert normalized == {
        "normal_values": ["Idle", "Running"],
        "incidents": [
            {
                "value": "Warning",
                "code": "status.Warning",
                "severity": "error",
                "message": "需要检查设备",
                "hold": True,
            },
            {
                "value": "Error",
                "code": "stirrer.error",
                "severity": "critical",
                "message": "设备故障",
                "hold": True,
            },
        ],
    }
    assert json.loads(json.dumps(normalized)) == normalized
    assert normalize_status_policy(normalized) == normalized


def test_compact_error_values_use_defaults_and_explicit_rule_wins() -> None:
    normalized = normalize_status_policy(
        {
            "error_values": ["Error", "Alarm", "Error"],
            "incidents": {
                "Alarm": {"code": "alarm.explicit", "hold": False},
            },
            "severity": "warning",
            "message": "设备异常",
            "hold": True,
        }
    )

    assert normalized is not None
    assert normalized["incidents"] == [
        {
            "value": "Alarm",
            "code": "alarm.explicit",
            "severity": "warning",
            "message": "设备异常",
            "hold": False,
        },
        {
            "value": "Error",
            "code": "status.Error",
            "severity": "warning",
            "message": "设备异常",
            "hold": True,
        },
    ]


def test_empty_optional_text_keeps_default_generation_compatibility() -> None:
    normalized = normalize_status_policy(
        {"incidents": {"Error": {"code": "", "message": None}}}
    )

    assert normalized is not None
    assert normalized["incidents"][0]["code"] == "status.Error"
    assert normalized["incidents"][0]["message"] == (
        "device status changed to 'Error'"
    )


def test_scalar_matching_is_type_exact_instead_of_python_equality() -> None:
    policy = {
        "normal_values": [True, 1.0],
        "incidents": [
            {"value": 1, "code": "integer.one"},
            {"value": "1", "code": "string.one"},
        ],
    }

    assert evaluate_status(policy, True) == StatusEvaluation(healthy=True)
    assert evaluate_status(policy, 1.0) == StatusEvaluation(healthy=True)
    assert evaluate_status(policy, 1).incident == {
        "value": 1,
        "code": "integer.one",
        "severity": "error",
        "message": "device status changed to 1",
        "hold": True,
    }
    assert evaluate_status(policy, "1").incident["code"] == "string.one"


def test_same_typed_value_cannot_be_normal_and_incident() -> None:
    with pytest.raises(ValueError, match="both normal and incident"):
        normalize_status_policy(
            {"normal_values": [1], "incidents": [{"value": 1}]}
        )


def test_duplicate_incident_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate status incident value"):
        normalize_status_policy(
            {
                "incidents": [
                    {"value": "Error", "code": "first"},
                    {"value": "Error", "code": "second"},
                ]
            }
        )


def test_incident_only_policy_treats_other_scalar_values_as_recovery() -> None:
    policy = {"incidents": {"Error": {"code": "device.error"}}}

    assert evaluate_status(policy, "Error").healthy is False
    assert evaluate_status(policy, "Idle") == StatusEvaluation(healthy=True)


def test_normal_allow_list_leaves_unknown_value_neutral() -> None:
    policy = {
        "normal_values": ["Idle"],
        "incidents": {"Error": {"code": "device.error"}},
    }

    assert evaluate_status(policy, "Idle") == StatusEvaluation(healthy=True)
    assert evaluate_status(policy, "Starting") == StatusEvaluation(healthy=None)


def test_unknown_incident_matches_unlisted_scalar_and_preserves_observed_value() -> None:
    policy = {
        "normal_values": ["Idle"],
        "unknown_incident": {
            "code": "device.unknown",
            "severity": "warning",
            "hold": False,
        },
    }

    evaluation = evaluate_status(policy, "Starting")

    assert evaluation.healthy is False
    assert evaluation.incident == {
        "value": "Starting",
        "code": "device.unknown",
        "severity": "warning",
        "message": "device status changed to '*'",
        "hold": False,
    }


@pytest.mark.parametrize("policy", [None, {}])
def test_empty_policy_is_unconfigured(policy) -> None:
    assert normalize_status_policy(policy) is None
    assert evaluate_status(policy, "anything") == StatusEvaluation(healthy=None)


@pytest.mark.parametrize("policy", [False, 0, "", []])
def test_falsy_non_mapping_policy_is_not_silently_treated_as_empty(policy) -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        normalize_status_policy(policy)


@pytest.mark.parametrize(
    ("policy", "error_type", "message"),
    [
        ({"normal_values": None}, TypeError, "normal_values"),
        ({"incidents": "Error"}, TypeError, "incidents"),
        ({"incidents": [None]}, TypeError, r"incidents\[0\]"),
        ({"incidents": [{}]}, ValueError, "requires value"),
        ({"error_values": None}, TypeError, "error_values"),
        (
            {"incidents": {"Error": {"severity": "fatal"}}},
            ValueError,
            "severity",
        ),
        (
            {"incidents": {"Error": {"severity": 3}}},
            TypeError,
            "severity",
        ),
        (
            {"incidents": {"Error": {"hold": "yes"}}},
            TypeError,
            "hold",
        ),
        (
            {"incidents": {"Error": {"code": 3}}},
            TypeError,
            "code",
        ),
        (
            {"incidents": {"Error": {"message": []}}},
            TypeError,
            "message",
        ),
    ],
)
def test_malformed_policy_is_rejected(policy, error_type, message) -> None:
    with pytest.raises(error_type, match=message):
        normalize_status_policy(policy)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_policy_values_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        normalize_status_policy({"normal_values": [value]})


@pytest.mark.parametrize("value", [None, {}, [], math.nan, math.inf])
def test_non_scalar_or_non_finite_observation_is_neutral(value) -> None:
    policy = {"unknown_incident": {"code": "device.unknown"}}

    assert evaluate_status(policy, value) == StatusEvaluation(healthy=None)


def test_policy_requires_at_least_one_effective_rule() -> None:
    with pytest.raises(ValueError, match="must define"):
        normalize_status_policy({"normal_values": [], "incidents": None})


def test_evaluation_returns_a_copy_of_incident_config() -> None:
    policy = {"incidents": {"Error": {"code": "device.error"}}}

    first = evaluate_status(policy, "Error")
    assert first.incident is not None
    first.incident["message"] = "mutated"

    second = evaluate_status(policy, "Error")
    assert second.incident is not None
    assert second.incident["message"] == "device status changed to 'Error'"
