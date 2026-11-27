import ast
import asyncio
import json

import pytest
from pylabrobot.resources import Coordinate, Resource

from unilabos.registry.action_policy import (
    SUCCESS_TYPE_NORMAL,
    SUCCESS_TYPE_OPERATOR_INTERVENTION,
    SUCCESS_TYPE_SKIP,
    ActionDecisionOutcome,
    normalize_error_policy,
    resolve_error_options,
)
from unilabos.registry.ast_registry_scanner import (
    _collect_imports,
    _extract_class_body,
)
from unilabos.registry.decorators import action, get_action_meta
from unilabos.ros.nodes.base_device_node import (
    BaseROS2DeviceNode,
    _native_driver_result_error,
)
from unilabos.utils.exception import DeviceActionError, TimeoutException
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


def test_documented_flat_policy_and_timeout_metadata_are_normalized():
    @action(
        timeout=600,
        execution_timeout=540,
        default_on_user_timeout="skip",
        error_policy={
            "allow_retry": True,
            "allow_skip": True,
            "max_retries": 3,
            "decision_timeout_seconds": 120,
            "options": [
                {
                    "action": "cool_down",
                    "label": "先降温再重试",
                    "fallback_action": "emergency_cool",
                    "then": "retry",
                }
            ],
        },
    )
    def heat_to(self):
        return None

    meta = get_action_meta(heat_to)
    assert meta["timeout"] == 600
    assert meta["execution_timeout"] == 540
    assert meta["exception_handling"] is True
    assert meta["default_on_user_timeout"] == "skip"
    assert [item["action"] for item in meta["error_policy"]["options"]["*"]] == [
        "retry",
        "cool_down",
        "skip",
    ]
    assert meta["error_policy"]["options"]["*"][1]["then"] == "retry"
    assert meta["error_policy"]["default_on_decision_timeout"] == "skip"


def test_timeout_exception_keeps_documented_category_and_kind():
    exc = TimeoutException("设备未在期限内完成", kind="execution", seconds=5)

    assert exc.category == "timeout"
    assert exc.severity == "error"
    assert exc.kind == "execution"
    assert exc.seconds == 5


def test_ast_scanner_preserves_documented_error_handling_arguments():
    source = """
from unilabos.registry.decorators import action

class Driver:
    @action(timeout=60, execution_timeout=50, exception_handling=False,
            default_on_user_timeout="skip")
    def run(self):
        pass
"""
    tree = ast.parse(source)
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    extracted = _extract_class_body(class_node, _collect_imports(tree))
    args = extracted["actions"]["run"]["action_args"]

    assert args["timeout"] == 60
    assert args["execution_timeout"] == 50
    assert args["exception_handling"] is False
    assert args["default_on_user_timeout"] == "skip"


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
        self.reported_defaults = []

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
        self.reported_defaults.append(default_on_timeout)
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
    assert "ValueError: bad input" in outcome.audit_error


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


def test_custom_recovery_then_retry_runs_on_edge_before_retry():
    node = FakeDecisionNode([{"action": "cool_down"}])
    policy = normalize_error_policy(
        {
            "options": [
                {
                    "action": "cool_down",
                    "label": "先降温再重试",
                    "fallback_action": "emergency_cool",
                    "then": "retry",
                }
            ]
        }
    )
    calls = []

    async def retry_action():
        calls.append("retry")
        return {"retried": True}

    async def fallback(option):
        calls.append(option["fallback_action"]["action_name"])

    outcome = asyncio.run(
        node._resolve_action_exception(
            TimeoutException("超时"),
            retry_action,
            "heat_to",
            {"task_id": "task-5", "job_id": "job-5"},
            policy,
            fallback,
        )
    )

    assert calls == ["emergency_cool", "retry"]
    assert outcome.suc_type == SUCCESS_TYPE_NORMAL
    assert outcome.value == {"retried": True}


def test_retry_waits_for_timed_out_action_to_settle():
    node = FakeDecisionNode([{"action": "retry"}])
    calls = []

    async def wait_for_timed_out_action():
        calls.append("settled")

    async def retry_action():
        calls.append("retry")
        return {"retried": True}

    outcome = asyncio.run(
        node._resolve_action_exception(
            TimeoutException("超时"),
            retry_action,
            "run",
            {"task_id": "task-6", "job_id": "job-6"},
            normalize_error_policy(
                {"options": {"*": [{"action": "retry", "label": "重试"}]}}
            ),
            wait_for_timed_out_action=wait_for_timed_out_action,
        )
    )

    assert calls == ["settled", "retry"]
    assert outcome.value == {"retried": True}


def test_late_success_of_timed_out_action_wins_over_selected_retry():
    """不可取消动作在人工决策前成功时，不能重复执行物理 retry。"""

    node = FakeDecisionNode([{"action": "retry"}])
    calls = []

    async def wait_for_timed_out_action():
        calls.append("settled")
        return ActionDecisionOutcome({"original": True}, SUCCESS_TYPE_NORMAL)

    async def retry_action():
        calls.append("retry")
        raise AssertionError("late successful action must not be retried")

    outcome = asyncio.run(
        node._resolve_action_exception(
            TimeoutException("initial timed out"),
            retry_action,
            "run",
            {"task_id": "task-late-success", "job_id": "job-late-success"},
            normalize_error_policy(
                {"options": {"*": [{"action": "retry", "label": "重试"}]}}
            ),
            wait_for_timed_out_action=wait_for_timed_out_action,
        )
    )

    assert calls == ["settled"]
    assert outcome.value == {"original": True}
    assert outcome.suc_type == SUCCESS_TYPE_NORMAL


@pytest.mark.parametrize("result", [False, {"success": False}])
def test_native_driver_failures_share_one_error_conversion(result):
    error = _native_driver_result_error("device-1", "run", None, result)

    assert isinstance(error, DeviceActionError)
    assert error.return_value == result


def test_retry_timeout_waits_before_the_next_retry():
    """retry 自己超时后，下一次 retry 也必须先等待同一动作收束。"""

    node = FakeDecisionNode([{"action": "retry"}, {"action": "retry"}])
    calls = []

    async def wait_for_inflight_action():
        calls.append("settled")

    async def retry_action():
        calls.append("retry")
        if calls.count("retry") == 1:
            raise TimeoutException("retry timed out")
        return {"retried": True}

    outcome = asyncio.run(
        node._resolve_action_exception(
            TimeoutException("initial timed out"),
            retry_action,
            "run",
            {"task_id": "task-7", "job_id": "job-7"},
            normalize_error_policy(
                {
                    "max_retries": 2,
                    "options": {"*": [{"action": "retry", "label": "重试"}]},
                }
            ),
            wait_for_timed_out_action=wait_for_inflight_action,
        )
    )

    assert calls == ["settled", "retry", "settled", "retry"]
    assert outcome.value == {"retried": True}


def test_retry_limit_removes_retry_from_next_decision_options():
    node = FakeDecisionNode([{"action": "retry"}, {"action": "skip"}])

    async def retry_action():
        raise ValueError("still failing")

    outcome = asyncio.run(
        node._resolve_action_exception(
            ValueError("initial failure"),
            retry_action,
            "run",
            {"task_id": "task-limit", "job_id": "job-limit"},
            normalize_error_policy(
                {
                    "max_retries": 1,
                    "options": {"*": [
                        {"action": "retry", "label": "重试"},
                        {"action": "skip", "label": "跳过"},
                    ]},
                }
            ),
        )
    )

    assert outcome.suc_type == SUCCESS_TYPE_SKIP
    assert [item["action"] for item in node.reported_options[1]] == ["skip"]


def test_retry_limit_removes_recovery_options_that_eventually_retry():
    node = FakeDecisionNode([{"action": "skip"}])
    calls = []

    async def retry_action():
        calls.append("retry")
        raise AssertionError("retry must not be available when max_retries is zero")

    async def fallback(_option):
        calls.append("fallback")

    outcome = asyncio.run(
        node._resolve_action_exception(
            ValueError("initial failure"),
            retry_action,
            "run",
            {"task_id": "task-limit-fallback", "job_id": "job-limit-fallback"},
            normalize_error_policy(
                {
                    "max_retries": 0,
                    "options": {"*": [
                        {
                            "action": "cool_down",
                            "label": "降温后重试",
                            "fallback_action": "cool_down",
                            "then": "retry",
                        },
                        {"action": "skip", "label": "跳过"},
                    ]},
                }
            ),
            fallback,
        )
    )

    assert [item["action"] for item in node.reported_options[0]] == ["skip"]
    assert calls == []
    assert outcome.suc_type == SUCCESS_TYPE_SKIP


def test_retry_limit_waits_for_timed_out_action_even_without_options():
    node = FakeDecisionNode([])
    calls = []

    async def wait_for_timed_out_action():
        calls.append("settled")
        return ActionDecisionOutcome({"late": True}, SUCCESS_TYPE_NORMAL)

    async def retry_action():
        raise AssertionError("no retry is available")

    outcome = asyncio.run(
        node._resolve_action_exception(
            TimeoutException("retry timed out"),
            retry_action,
            "run",
            {"task_id": "task-limit-timeout", "job_id": "job-limit-timeout"},
            normalize_error_policy(
                {
                    "max_retries": 0,
                    "options": {"*": [{"action": "retry", "label": "重试"}]},
                }
            ),
            wait_for_timed_out_action=wait_for_timed_out_action,
        )
    )

    assert calls == ["settled"]
    assert outcome.value == {"late": True}


def test_fallback_native_failure_reenters_error_policy():
    node = FakeDecisionNode([{"action": "cool_down"}, {"action": "skip"}])
    calls = []

    async def retry_action():
        raise AssertionError("failed fallback must not retry the original action")

    async def fallback(_option):
        calls.append("fallback")
        raise DeviceActionError("device-1", "cool_down", "驱动返回失败结果")

    outcome = asyncio.run(
        node._resolve_action_exception(
            TimeoutException("initial timeout"),
            retry_action,
            "run",
            {"task_id": "task-fallback", "job_id": "job-fallback"},
            normalize_error_policy(
                {
                    "options": {"*": [
                        {
                            "action": "cool_down",
                            "label": "降温后重试",
                            "fallback_action": "cool_down",
                            "then": "retry",
                        },
                        {"action": "skip", "label": "跳过"},
                    ]}
                }
            ),
            fallback,
        )
    )

    assert calls == ["fallback"]
    assert outcome.suc_type == SUCCESS_TYPE_SKIP


def test_timeout_default_not_offered_by_policy_falls_back_to_abort():
    node = FakeDecisionNode([{"action": "abort", "reason": "decision_timeout"}])

    async def retry_action():
        raise AssertionError("default abort must not retry")

    with pytest.raises(TimeoutException):
        asyncio.run(
            node._resolve_action_exception(
                TimeoutException("initial timed out"),
                retry_action,
                "run",
                {"task_id": "task-default", "job_id": "job-default"},
                normalize_error_policy(
                    {
                        "allow_retry": True,
                        "allow_skip": False,
                        "default_on_decision_timeout": "skip",
                    }
                ),
            )
        )

    assert node.reported_defaults == ["abort"]


def test_timeout_default_retry_falls_back_to_abort_after_retry_limit():
    node = FakeDecisionNode([{"action": "abort", "reason": "decision_timeout"}])

    async def retry_action():
        raise AssertionError("retry limit must prevent default retry")

    with pytest.raises(TimeoutException):
        asyncio.run(
            node._resolve_action_exception(
                TimeoutException("initial timed out"),
                retry_action,
                "run",
                {"task_id": "task-default-retry", "job_id": "job-default-retry"},
                normalize_error_policy(
                    {
                        "allow_retry": True,
                        "allow_skip": True,
                        "max_retries": 0,
                        "default_on_decision_timeout": "retry",
                    }
                ),
            )
        )

    assert node.reported_options == [[{"action": "skip", "label": "跳过"}]]
    assert node.reported_defaults == ["abort"]


def test_transport_unavailable_default_still_waits_before_abort():
    """决策请求无法发布时，默认 abort 仍须经过超时动作收束屏障。"""

    node = FakeDecisionNode(
        [{"action": "abort", "reason": "decision_transport_unavailable"}]
    )
    calls = []

    async def wait_for_inflight_action():
        calls.append("settled")

    async def retry_action():
        raise AssertionError("abort must not retry")

    with pytest.raises(TimeoutException):
        asyncio.run(
            node._resolve_action_exception(
                TimeoutException("initial timed out"),
                retry_action,
                "run",
                {"task_id": "task-8", "job_id": "job-8"},
                normalize_error_policy(
                    {"options": {"*": [{"action": "retry", "label": "重试"}]}}
                ),
                wait_for_timed_out_action=wait_for_inflight_action,
            )
        )

    assert calls == ["settled"]
