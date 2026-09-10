import asyncio
import ast
import json

import pytest
from pylabrobot.resources import Coordinate, Resource

from unilabos.registry.action_policy import (
    SUCCESS_TYPE_NORMAL,
    SUCCESS_TYPE_OPERATOR_INTERVENTION,
    SUCCESS_TYPE_SKIP,
    normalize_error_policy,
    resolve_error_options,
)
from unilabos.registry.ast_registry_scanner import (
    _collect_imports,
    _extract_class_body,
)
from unilabos.registry.decorators import action, get_action_meta
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.utils.type_check import (
    get_result_info_str,
    serialize_result_info,
)


class CommunicationError(Exception):
    pass


class ModbusCommunicationError(CommunicationError):
    pass


def _policy():
    return {
        "options": {
            "CommunicationError": [
                {"action": "retry", "label": "重试"},
                {
                    "action": "reset_connection",
                    "label": "审批后重置连接",
                    "fallback_action": {
                        "action_name": "reset",
                        "params": {"channel": 2},
                    },
                },
            ],
            "*": [{"action": "abort", "label": "终止"}],
        },
        "max_retries": 2,
        "decision_timeout_seconds": 30,
    }


def test_policy_matches_exception_mro_and_preserves_server_action():
    policy = normalize_error_policy(_policy())

    options = resolve_error_options(
        policy,
        ModbusCommunicationError("offline"),
    )

    assert [option["action"] for option in options] == [
        "retry",
        "reset_connection",
    ]
    assert options[1]["fallback_action"] == {
        "action_name": "reset",
        "params": {"channel": 2},
    }


def test_policy_uses_wildcard_for_unmatched_exception():
    policy = normalize_error_policy(_policy())

    assert resolve_error_options(policy, ValueError("bad")) == [
        {"action": "abort", "label": "终止"}
    ]


def test_policy_accepts_legacy_fallback_action_string():
    policy = normalize_error_policy(
        {
            "options": {
                "ValueError": [
                    {
                        "action": "reset",
                        "label": "重置",
                        "fallback_action": "reset_device",
                    }
                ]
            }
        }
    )

    assert policy["options"]["ValueError"][0]["fallback_action"] == {
        "action_name": "reset_device",
        "params": {},
    }


def test_action_exposes_normalized_policy_in_runtime_and_registry_meta():
    @action(error_policy=_policy())
    def run(self):
        return None

    assert run._action_error_policy == get_action_meta(run)["error_policy"]
    assert run._action_error_policy["options"]["CommunicationError"][1][
        "fallback_action"
    ]["params"] == {"channel": 2}


def test_ast_scanner_preserves_exception_class_option_mapping():
    source = """
from unilabos.registry.decorators import action

class Driver:
    @action(error_policy={
        "options": {
            "ValueError": [
                {
                    "action": "inspect",
                    "label": "人工检查",
                    "fallback_action": {
                        "action_name": "inspect_device",
                        "params": {"station": "A"},
                    },
                }
            ]
        }
    })
    def run(self):
        pass
"""
    tree = ast.parse(source)
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef)
    )
    extracted = _extract_class_body(class_node, _collect_imports(tree))

    value_error_options = extracted["actions"]["run"]["action_args"][
        "error_policy"
    ]["options"]["ValueError"]
    assert value_error_options[0]["fallback_action"]["params"] == {
        "station": "A"
    }


@pytest.mark.parametrize(
    ("suc_type", "return_value"),
    [
        (SUCCESS_TYPE_NORMAL, {"value": 1}),
        (SUCCESS_TYPE_SKIP, None),
        (SUCCESS_TYPE_OPERATOR_INTERVENTION, {"recovered": True}),
    ],
)
def test_result_info_distinguishes_three_success_types(suc_type, return_value):
    encoded = json.loads(
        get_result_info_str("", True, return_value, suc_type=suc_type)
    )
    serialized = serialize_result_info(
        "",
        True,
        return_value,
        suc_type=suc_type,
    )

    assert encoded == serialized
    assert encoded["suc"] is True
    assert encoded["suc_type"] == suc_type
    assert encoded["return_value"] == return_value


def test_failed_result_does_not_claim_success_type():
    result = serialize_result_info("failed", False, None)

    assert result == {"error": "failed", "suc": False, "return_value": None}


def test_result_info_serializes_attached_resource_as_stable_reference():
    """PLR resources contain parent/child cycles and must stay wire-safe."""

    carrier = Resource(
        name="carrier",
        size_x=10,
        size_y=10,
        size_z=10,
        category="carrier",
    )
    material = Resource(
        name="material",
        size_x=1,
        size_y=1,
        size_z=1,
        category="material",
    )
    material.unilabos_uuid = "10000000-0000-4000-8000-000000000001"
    carrier.assign_child_resource(material, location=Coordinate())

    encoded = json.loads(
        get_result_info_str("", True, {"resource": material})
    )

    assert encoded["return_value"] == {
        "resource": {"uuid": material.unilabos_uuid}
    }


def test_policy_rejects_empty_class_options():
    with pytest.raises(ValueError, match="非空列表"):
        normalize_error_policy({"options": {"ValueError": []}})


class FakeDecisionNode:
    _resolve_action_exception = BaseROS2DeviceNode._resolve_action_exception
    _approved_result_value = staticmethod(
        BaseROS2DeviceNode._approved_result_value
    )

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.reported_options = []

    async def _request_action_error_decision(
        self,
        exc,
        action_name,
        context,
        options,
        timeout_seconds,
        default_on_timeout,
    ):
        self.reported_options.append(options)
        return self.decisions.pop(0)


def test_operator_intervention_returns_server_result_directly():
    node = FakeDecisionNode(
        [
            {
                "action": "reset_connection",
                "result": {
                    "suc": True,
                    "return_value": {"connection": "restored"},
                },
            }
        ]
    )

    async def retry_action():
        raise AssertionError("operator intervention must not retry locally")

    outcome = asyncio.run(
        node._resolve_action_exception(
            CommunicationError("offline"),
            retry_action,
            "connect",
            {"task_id": "task-1", "job_id": "job-1"},
            normalize_error_policy(_policy()),
        )
    )

    assert outcome.suc_type == SUCCESS_TYPE_OPERATOR_INTERVENTION
    assert outcome.value == {"connection": "restored"}
    fallback_params = node.reported_options[0][1]["fallback_action"]["params"]
    assert fallback_params == {"channel": 2}


def test_skip_is_success_with_skip_type():
    node = FakeDecisionNode([{"action": "skip", "result": {"ignored": True}}])
    policy = normalize_error_policy(
        {"options": {"ValueError": [{"action": "skip", "label": "跳过"}]}}
    )

    async def retry_action():
        raise AssertionError("skip must not retry")

    outcome = asyncio.run(
        node._resolve_action_exception(
            ValueError("bad input"),
            retry_action,
            "run",
            {"task_id": "task-2", "job_id": "job-2"},
            policy,
        )
    )

    assert outcome.suc_type == SUCCESS_TYPE_SKIP
    assert outcome.value == {"ignored": True}


def test_retry_success_is_normal_success():
    node = FakeDecisionNode([{"action": "retry"}])
    policy = normalize_error_policy(
        {"options": {"ValueError": [{"action": "retry", "label": "重试"}]}}
    )

    async def retry_action():
        return {"retried": True}

    outcome = asyncio.run(
        node._resolve_action_exception(
            ValueError("transient"),
            retry_action,
            "run",
            {"task_id": "task-3", "job_id": "job-3"},
            policy,
        )
    )

    assert outcome.suc_type == SUCCESS_TYPE_NORMAL
    assert outcome.value == {"retried": True}


def test_operator_intervention_requires_explicit_result():
    node = FakeDecisionNode([{"action": "reset_connection"}])

    async def retry_action():
        return None

    with pytest.raises(RuntimeError, match="missing result"):
        asyncio.run(
            node._resolve_action_exception(
                CommunicationError("offline"),
                retry_action,
                "connect",
                {"task_id": "task-4", "job_id": "job-4"},
                normalize_error_policy(_policy()),
            )
        )
