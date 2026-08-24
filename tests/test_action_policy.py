import ast
import json
import time

import pytest

from unilabos.server.scheduler.execution_queue import (
    DeviceActionManager,
    JobInfo,
    JobStatus,
    QueueItem,
)
from unilabos.server.scheduler.backend import JobExecutionBackend
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
from unilabos.ros.nodes.base_device_node import (
    _coerce_device_error_info,
    _native_driver_result_failed,
)
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos.utils.type_check import serialize_result_info


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


def test_action_without_policy_exposes_empty_registry_object():
    @action()
    def run(self):
        return None

    assert run._action_error_policy == {}
    assert get_action_meta(run)["error_policy"] == {}


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


def test_ast_scanner_defaults_unconfigured_policy_to_empty_object():
    tree = ast.parse(
        """
from unilabos.registry.decorators import action

class Driver:
    @action()
    def run(self):
        pass
"""
    )
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef)
    )
    extracted = _extract_class_body(class_node, _collect_imports(tree))

    assert extracted["actions"]["run"]["action_args"]["error_policy"] == {}


@pytest.mark.parametrize(
    ("suc_type", "return_value"),
    [
        (SUCCESS_TYPE_NORMAL, {"value": 1}),
        (SUCCESS_TYPE_SKIP, None),
        (SUCCESS_TYPE_OPERATOR_INTERVENTION, {"recovered": True}),
    ],
)
def test_result_info_distinguishes_three_success_types(suc_type, return_value):
    serialized = serialize_result_info(
        "",
        True,
        return_value,
        suc_type=suc_type,
    )

    encoded_at_ros_boundary = json.loads(
        json.dumps(serialized, ensure_ascii=False)
    )

    assert encoded_at_ros_boundary == serialized
    assert serialized["suc"] is True
    assert serialized["suc_type"] == suc_type
    assert serialized["return_value"] == return_value


def test_failed_result_does_not_claim_success_type():
    result = serialize_result_info("failed", False, None)

    assert result == {"error": "failed", "suc": False, "return_value": None}


def test_policy_rejects_empty_class_options():
    with pytest.raises(ValueError, match="非空列表"):
        normalize_error_policy({"options": {"ValueError": []}})


def test_policy_rejects_duplicate_actions_for_one_exception():
    with pytest.raises(ValueError, match="重复 action"):
        normalize_error_policy(
            {
                "options": {
                    "ValueError": [
                        {"action": "retry", "label": "重试一次"},
                        {"action": "retry", "label": "再次重试"},
                    ]
                }
            }
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_retries", True),
        ("decision_timeout_seconds", False),
    ],
)
def test_policy_rejects_boolean_numeric_settings(field, value):
    policy = {
        "options": {"ValueError": [{"action": "abort", "label": "终止"}]},
        field: value,
    }
    with pytest.raises(ValueError):
        normalize_error_policy(policy)


def test_policy_requires_timeout_action_for_every_exception_class():
    with pytest.raises(ValueError, match="每个异常 options"):
        normalize_error_policy(
            {
                "options": {
                    "CommunicationError": [
                        {"action": "retry", "label": "重试"},
                    ],
                    "ValueError": [
                        {"action": "abort", "label": "终止"},
                    ],
                },
                "default_on_decision_timeout": "retry",
            }
        )


def test_policy_accepts_timeout_action_present_for_every_exception_class():
    policy = normalize_error_policy(
        {
            "options": {
                "CommunicationError": [
                    {"action": "skip", "label": "跳过"},
                ],
                "ValueError": [
                    {"action": "skip", "label": "跳过"},
                ],
            },
            "default_on_decision_timeout": "skip",
        }
    )

    assert policy["default_on_decision_timeout"] == "skip"


def test_failed_result_carries_structured_error_info():
    error_info = {
        "exception_type": "CommunicationError",
        "exception_mro": ["CommunicationError", "Exception"],
    }

    serialized = serialize_result_info(
        "offline",
        False,
        None,
        error_info=error_info,
    )

    assert serialized["error_info"] == error_info
    assert "suc_type" not in serialized


def test_native_action_failure_is_not_confused_with_json_command_boolean_data():
    class HeatChill:
        pass

    class UniLabJsonCommand:
        pass

    assert _native_driver_result_failed("heat_chill", HeatChill, False)
    assert _native_driver_result_failed(
        "set_position", HeatChill, {"success": False}
    )
    assert not _native_driver_result_failed(
        "heat_chill", HeatChill, {"success": True}
    )
    assert not _native_driver_result_failed(
        "auto-is_empty", UniLabJsonCommand, False
    )
    assert not _native_driver_result_failed(
        "_execute_driver_command", HeatChill, False
    )


def test_native_false_result_gets_structured_action_result_error():
    error_info = _coerce_device_error_info(
        "heat_chill",
        False,
        "driver returned an unsuccessful native action result: False",
    )

    assert error_info["action_name"] == "heat_chill"
    assert error_info["exception_type"] == "ActionResultError"
    assert error_info["exception_mro"][:2] == ["ActionResultError", "RuntimeError"]
    assert "unsuccessful native action result" in error_info["error_message"]


def test_native_structured_failure_preserves_driver_error_classification():
    error_info = _coerce_device_error_info(
        "set_position",
        {
            "success": False,
            "error": "top-level fallback",
            "error_info": {
                "exception_type": "CommunicationError",
                "exception_mro": ["CommunicationError", "Exception"],
                "error_message": "serial port closed",
                "category": "communication",
                "severity": "recoverable",
            },
        },
        "native result failed",
    )

    assert error_info["exception_type"] == "CommunicationError"
    assert error_info["exception_mro"] == ["CommunicationError", "Exception"]
    assert error_info["error_message"] == "serial port closed"
    assert error_info["category"] == "communication"
    assert error_info["severity"] == "recoverable"


class _Logger:
    def info(self, message):
        pass

    def warning(self, message):
        pass


class _DecisionBridge:
    def __init__(self):
        self.required = []

    def publish_job_error_decision_required(self, report):
        self.required.append(report)
        return True


class _DecisionStatusBridge(_DecisionBridge):
    def __init__(self):
        super().__init__()
        self.finished = []

    def publish_job_status(self, result_data, item, status, return_info=None):
        if status in {"success", "failed", "canceled"}:
            self.finished.append((item, status, return_info, result_data))


class _RecordingMonitor:
    def __init__(self, events):
        self.events = events

    def emit(self, channel, event_type, data):
        if channel == "action":
            self.events.append((event_type, data))


class _FakeExecutionAdapter:
    def __init__(self):
        self.sent_goals = []
        self.cancelled = []
        self._action_value_mappings = {
            "device-1": {
                "run": {
                    "type": "UniLabJsonCommand",
                    "error_policy": normalize_error_policy(
                        {
                            "options": {
                                "CommunicationError": [
                                    {"action": "retry", "label": "重试"},
                                    {"action": "skip", "label": "跳过"},
                                    {"action": "abort", "label": "终止"},
                                    {
                                        "action": "operator_intervention",
                                        "label": "人工替代结果",
                                    },
                                ]
                            },
                            "max_retries": 2,
                            "decision_timeout_seconds": 30,
                        }
                    ),
                },
                "auto-reset": {"type": "UniLabJsonCommand"},
            }
        }

    def send_goal(self, *args, **kwargs):
        self.sent_goals.append((args, kwargs))

    def cancel_goal(self, job_id):
        self.cancelled.append(job_id)
        return True


class FakeMicrobackend(JobExecutionBackend):
    def __init__(self):
        self.adapter = _FakeExecutionAdapter()
        self.decision_bridge = _DecisionStatusBridge()
        self.local_events = []
        super().__init__(
            host_node_getter=lambda: self.adapter,
            monitor=_RecordingMonitor(self.local_events),
            result_bridges=[self.decision_bridge],
        )
        self._action_value_mappings = self.adapter._action_value_mappings
        self.sent_goals = self.adapter.sent_goals
        self.finished = self.decision_bridge.finished
        self.device_manager.enqueue_job(
            JobInfo(
                job_id="job-1",
                task_id="task-1",
                device_id="device-1",
                notebook_id="notebook-1",
                action_name="run",
                device_action_key="/devices/device-1/run",
                status=JobStatus.QUEUE,
                start_time=time.time(),
                node_id="node-1",
            )
        )


# 兼容下方历史测试变量名；对象本身已经是微后端，不再模拟 HostNode。
FakeHostDecisionNode = FakeMicrobackend


def _queue_item():
    return QueueItem(
        task_type="job_call_back_status",
        device_id="device-1",
        action_name="run",
        task_id="task-1",
        job_id="job-1",
        notebook_id="notebook-1",
        device_action_key="/devices/device-1/run",
        node_id="node-1",
    )


def _local_event_data(host, event_type):
    return [data for current_type, data in host.local_events if current_type == event_type]


def _error_return_info():
    return serialize_result_info(
        "offline",
        False,
        None,
        error_info={
            "action_name": "run",
            "exception_type": "CommunicationError",
            "exception_mro": [
                "CommunicationError",
                "Exception",
                "BaseException",
                "object",
            ],
            "error_message": "offline",
            "traceback": "trace",
        },
    )


def _begin_pending(host, policy=None, item=None):
    if policy is not None:
        host._action_value_mappings["device-1"]["run"]["error_policy"] = (
            normalize_error_policy(policy)
        )
    pending_item = item or _queue_item()
    assert host._begin_action_error_decision(
        pending_item,
        _error_return_info(),
        {"return_info": "failed"},
    )
    decision_id = next(iter(host._pending_action_error_decisions))
    return decision_id


def _decision(
    decision_id,
    action,
    *,
    job_id="job-1",
    device_id="device-1",
    **extra,
):
    return {
        "decision_id": decision_id,
        "job_id": job_id,
        "device_id": device_id,
        "action": action,
        "scheduler_updated": True,
        **extra,
    }


def test_host_holds_failure_and_publishes_registry_options_to_backend():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    report = _local_event_data(host, "job_error_decision_required")[0]
    assert report["decision_id"] == decision_id
    assert report["device_id"] == "device-1"
    assert report["exception_type"] == "CommunicationError"
    assert [option["action"] for option in report["options"]] == [
        "retry",
        "skip",
        "abort",
        "operator_intervention",
    ]
    assert report["expires_at"] > report["created_at"]
    assert report["max_retries"] == 2
    assert report["default_on_decision_timeout"] == "abort"
    assert host.device_manager.get_job_info("job-1") is not None
    assert host.decision_bridge.required == [report]
    assert not host.finished

    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "abort"),
    )


def test_empty_registry_policy_still_uses_backend_owned_default_error_flow():
    host = FakeHostDecisionNode()
    host._action_value_mappings["device-1"]["run"]["error_policy"] = {}

    decision_id = _begin_pending(host)

    report = next(
        item
        for item in host.get_pending_action_error_decisions()
        if item["decision_id"] == decision_id
    )
    assert [option["action"] for option in report["options"]] == [
        "retry",
        "abort",
        "operator_intervention",
    ]


def test_backend_release_keeps_retry_as_failed_without_host_redispatch():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    required = _local_event_data(host, "job_error_decision_required")[0]
    assert required["decision_id"] == decision_id
    assert required["expires_at"] > required["created_at"]
    reports = host.get_pending_action_error_decisions()
    assert [report["decision_id"] for report in reports] == [decision_id]
    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "retry"),
    )
    resolved = _local_event_data(host, "job_error_decision_resolved")[0]
    assert resolved["selected_action"] == "retry"
    assert not host.sent_goals
    assert host.finished[0][1] == "failed"
    assert host.finished[0][2]["error_resolution"] == {
        "decision_id": decision_id,
        "selected_action": "retry",
        "reason": "",
        "scheduler_updated": True,
    }


def test_operator_intervention_replaces_effective_result_but_keeps_raw_failure():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(
            decision_id,
            "operator_intervention",
            result={"confirmed": True},
            reason="operator supplied result",
        ),
    )

    _, status, return_info, result_data = host.finished[0]
    assert status == "success"
    assert return_info["suc"] is True
    assert return_info["return_value"] == {"confirmed": True}
    assert return_info["suc_type"] == SUCCESS_TYPE_OPERATOR_INTERVENTION
    assert return_info["error_resolution"] == {
        "decision_id": decision_id,
        "selected_action": "operator_intervention",
        "reason": "operator supplied result",
        "scheduler_updated": True,
    }
    assert result_data["raw_return_info"]["suc"] is False
    assert json.loads(result_data["return_info"]) == return_info


def test_monitor_bus_sse_contract_and_bounded_replay():
    from unilabos.server.scheduler.monitor import MonitorBus, format_sse_event

    bus = MonitorBus(history=2)
    bus.emit("action", "job_status", {"job_id": "job-1", "status": "running"})
    bus.emit("action", "job_status", {"job_id": "job-1", "status": "success"})
    bus.emit("action", "job_status", {"job_id": "job-2", "status": "failed"})

    sub_id, _, replay = bus.subscribe(channels={"action"}, backlog=10)
    try:
        assert [event["seq"] for event in replay] == [2, 3]
        encoded = format_sse_event(replay[-1])
        assert encoded.startswith("id: 3\nevent: action\ndata: ")
        assert '"job_id": "job-2"' in encoded
        assert encoded.endswith("\n\n")
    finally:
        bus.unsubscribe(sub_id)


def test_retry_release_never_redispatches_on_host():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "retry"),
    )

    assert not host.sent_goals
    assert host.finished[0][1] == "failed"
    assert host.finished[0][2]["error_resolution"]["selected_action"] == "retry"


def test_scheduler_retry_attempt_is_promoted_after_original_failed_releases_lock():
    manager = DeviceActionManager()
    key = "/devices/device-1/run"
    original = JobInfo(
        job_id="job-1",
        task_id="task-1",
        device_id="device-1",
        notebook_id="notebook-1",
        action_name="run",
        device_action_key=key,
        status=JobStatus.QUEUE,
        start_time=time.time(),
    )
    recovery = JobInfo(
        job_id="job-recovery",
        task_id="task-1",
        device_id="device-1",
        notebook_id="notebook-1",
        action_name="run",
        device_action_key=key,
        status=JobStatus.QUEUE,
        start_time=time.time(),
    )

    assert manager.enqueue_job(original) == (True, True)
    assert manager.enqueue_job(recovery) == (False, False)
    assert manager.active_jobs[key] is original
    assert manager.device_queues[key] == [recovery]

    next_job, lock_became_free = manager.end_job("job-1")
    assert next_job is recovery
    assert lock_became_free is False
    assert manager.active_jobs[key] is recovery


def test_host_decision_validates_identity_and_first_result_wins():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    assert not host.handle_action_error_decision(
        decision_id,
        "other-job",
        _decision(decision_id, "retry", job_id="other-job"),
    )
    assert not host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision("other-decision", "retry"),
    )
    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "skip", result={"ignored": True}),
    )
    assert not host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "abort"),
    )
    assert host.finished[0][1] == "failed"
    assert host.finished[0][2]["error_resolution"]["selected_action"] == "skip"
    assert _local_event_data(host, "job_error_decision_resolved")[0][
        "selected_action"
    ] == "skip"


@pytest.mark.parametrize("missing", ["decision_id", "job_id", "device_id"])
def test_host_decision_requires_complete_identity(missing):
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)
    decision = _decision(decision_id, "abort")
    decision.pop(missing)

    assert not host.handle_action_error_decision(
        decision_id,
        "job-1",
        decision,
    )
    assert decision_id in host._pending_action_error_decisions


def test_host_rejects_release_until_scheduler_is_updated():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)
    decision = _decision(decision_id, "retry")
    decision["scheduler_updated"] = False

    assert not host.handle_action_error_decision(
        decision_id,
        "job-1",
        decision,
    )

    assert decision_id in host._pending_action_error_decisions
    assert not host.sent_goals
    assert not host.finished


def test_pending_decision_exposes_backend_timeout_without_local_timer():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)
    pending = host._pending_action_error_decisions[decision_id]
    assert pending["error_info"]["expires_at"] == pending["report"]["expires_at"]
    assert pending["timer"] is None


def test_resolved_decision_lookup_does_not_execute_twice():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)
    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "skip"),
    )

    replayed = host.get_resolved_action_error_decision(
        decision_id,
        "job-1",
        "device-1",
    )

    resolved_events = _local_event_data(host, "job_error_decision_resolved")
    assert replayed == resolved_events[0]
    assert resolved_events == [replayed]
    assert len(host.finished) == 1


def test_host_reports_retry_count_but_does_not_enforce_scheduler_limit():
    host = FakeHostDecisionNode()
    host._action_value_mappings["device-1"]["run"]["error_policy"] = (
        normalize_error_policy(
            {
                "options": {
                    "CommunicationError": [
                        {"action": "retry", "label": "重试"}
                    ]
                },
                "max_retries": 1,
            }
        )
    )

    item = _queue_item()
    item.retry_count = 1
    assert host._begin_action_error_decision(
        item,
        _error_return_info(),
        {"return_info": "failed"},
    )
    assert not host.sent_goals
    report = _local_event_data(host, "job_error_decision_required")[0]
    assert report["retry_count"] == 1
    assert report["max_retries"] == 1
    assert [option["action"] for option in report["options"]] == ["retry"]


def test_retry_exhaustion_and_timeout_policy_are_left_to_backend():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(
        host,
        {
            "options": {
                "CommunicationError": [
                    {"action": "retry", "label": "重试"},
                    {"action": "abort", "label": "终止"},
                ]
            },
            "max_retries": 1,
            "default_on_decision_timeout": "retry",
        },
    )

    report = _local_event_data(host, "job_error_decision_required")[0]
    assert [option["action"] for option in report["options"]] == ["retry", "abort"]
    assert report["default_on_decision_timeout"] == "retry"
    assert decision_id in host._pending_action_error_decisions
    assert not host.finished


def test_host_forwards_all_scheduler_options_without_local_recovery_context():
    host = FakeHostDecisionNode()

    assert host._begin_action_error_decision(
        _queue_item(),
        _error_return_info(),
        {"return_info": "failed"},
    )

    report = _local_event_data(host, "job_error_decision_required")[0]
    assert [option["action"] for option in report["options"]] == [
        "retry",
        "skip",
        "abort",
        "operator_intervention",
    ]


def test_cancel_pending_error_decision_rejects_late_backend_release():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)
    assert host.cancel_job("job-1")

    assert not host._pending_action_error_decisions
    assert host.device_manager.get_job_info("job-1") is None
    assert host.finished[0][1] == "canceled"
    resolved_events = _local_event_data(host, "job_error_decision_resolved")
    assert resolved_events[0]["selected_action"] == "abort"
    assert resolved_events[0]["reason"] == "job_canceled"
    assert not host.cancel_job("job-1")
    assert len(resolved_events) == 1
    assert not host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "retry"),
    )


def test_goal_accepted_after_inflight_cancel_is_canceled_immediately():
    import threading

    class _CancelFuture:
        def add_done_callback(self, callback):
            self.callback = callback

    class _ResultFuture:
        def add_done_callback(self, callback):
            self.callback = callback

        def result(self):
            raise AssertionError("canceled goal response must not block on result")

    class _GoalHandle:
        accepted = True

        def __init__(self):
            self.cancel_calls = 0
            self.result_future = _ResultFuture()

        def get_result_async(self):
            return self.result_future

        def cancel_goal_async(self):
            self.cancel_calls += 1
            return _CancelFuture()

    class _GoalResponseFuture:
        def __init__(self, goal_handle):
            self.goal_handle = goal_handle

        def result(self):
            return self.goal_handle

    class _RosExecutionAdapter:
        _request_goal_cancel = HostNode._request_goal_cancel

        def __init__(self):
            self._goals = {}
            self._inflight_goal_jobs = set()
            self._canceled_jobs = {"job-1"}
            self._goal_state_lock = threading.RLock()

        def lab_logger(self):
            return _Logger()

    host = _RosExecutionAdapter()
    host._canceled_jobs.add("job-1")
    goal_handle = _GoalHandle()

    HostNode.goal_response_callback(
        host,
        _queue_item(),
        "/devices/device-1/run",
        _GoalResponseFuture(goal_handle),
    )

    assert host._goals["job-1"] is goal_handle
    assert goal_handle.cancel_calls == 1


def test_fallback_release_is_failed_and_never_dispatched_by_host():
    host = FakeHostDecisionNode()
    options = [
        {
            "action": "reset_connection",
            "label": "重置连接",
            "fallback_action": {
                "action_name": "reset",
                "params": {"channel": 2},
            },
        }
    ]
    decision_id = _begin_pending(
        host,
        {"options": {"CommunicationError": options}},
    )

    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "reset_connection"),
    )

    assert not host.sent_goals
    assert host.finished[0][1] == "failed"
    assert host.finished[0][2]["error_resolution"]["selected_action"] == "reset_connection"


def test_host_rejects_unconfigured_backend_option_without_consuming_pending():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    assert not host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "force_success"),
    )
    assert decision_id in host._pending_action_error_decisions

    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        _decision(decision_id, "abort"),
    )
