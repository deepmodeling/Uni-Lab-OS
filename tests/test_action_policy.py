import asyncio
import ast
import json

import pytest

from unilabos.app.ws_client import MessageProcessor, QueueItem
from unilabos.registry.action_policy import (
    ERROR_DECISION_TARGET_BACKEND,
    ERROR_DECISION_TARGET_MICRO_BACKEND,
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
from unilabos.ros.nodes.presets.host_node import HostNode
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


def test_failed_result_carries_structured_error_info():
    error_info = {
        "exception_type": "CommunicationError",
        "exception_mro": ["CommunicationError", "Exception"],
    }

    encoded = json.loads(
        get_result_info_str("offline", False, None, error_info=error_info)
    )
    serialized = serialize_result_info(
        "offline",
        False,
        None,
        error_info=error_info,
    )

    assert encoded == serialized
    assert encoded["error_info"] == error_info
    assert "suc_type" not in encoded


class _Logger:
    def info(self, message):
        pass

    def warning(self, message):
        pass


class _DecisionBridge:
    def __init__(self):
        self.reports = []

    def publish_job_error_decision_required(self, report):
        self.reports.append(report)
        return True


class FakeHostDecisionNode:
    _begin_action_error_decision = HostNode._begin_action_error_decision
    _emit_local_action_event = staticmethod(HostNode._emit_local_action_event)
    _handle_action_error_decision_timeout = (
        HostNode._handle_action_error_decision_timeout
    )
    handle_action_error_decision = HostNode.handle_action_error_decision
    get_pending_action_error_decisions = (
        HostNode.get_pending_action_error_decisions
    )

    def __init__(self):
        import threading

        self.bridge = _DecisionBridge()
        self.bridges = [self.bridge]
        self._goals = {"job-1": object()}
        self._pending_action_error_decisions = {}
        self._pending_action_error_decisions_lock = threading.RLock()
        self._error_execution_contexts = {
            "job-1": {
                "item": _queue_item(),
                "action_type": "UniLabJsonCommand",
                "action_kwargs": {"channel": 1},
                "sample_material": {},
                "server_info": None,
                "retry_count": 0,
            }
        }
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
        self.sent_goals = []
        self.finished = []

    def lab_logger(self):
        return _Logger()

    def send_goal(self, *args, **kwargs):
        self.sent_goals.append((args, kwargs))

    def _finish_error_handled_job(
        self,
        item,
        status,
        return_info,
        result_data,
    ):
        self.finished.append((item, status, return_info, result_data))
        self._error_execution_contexts.pop(item.job_id, None)


def _queue_item(
    error_decision_target=ERROR_DECISION_TARGET_BACKEND,
):
    return QueueItem(
        task_type="job_call_back_status",
        device_id="device-1",
        action_name="run",
        task_id="task-1",
        job_id="job-1",
        notebook_id="notebook-1",
        device_action_key="/devices/device-1/run",
        error_decision_target=error_decision_target,
    )


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
    host._error_execution_contexts["job-1"]["item"] = pending_item
    assert host._begin_action_error_decision(
        pending_item,
        _error_return_info(),
        {"return_info": "failed"},
    )
    decision_id = next(iter(host._pending_action_error_decisions))
    return decision_id


def test_host_owns_decision_and_publishes_registry_options():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    report = host.bridge.reports[0]
    assert report["decision_id"] == decision_id
    assert report["device_id"] == "device-1"
    assert report["exception_type"] == "CommunicationError"
    assert [option["action"] for option in report["options"]] == [
        "retry",
        "skip",
        "abort",
    ]
    assert report["expires_at"] > report["created_at"]
    assert report["max_retries"] == 2
    assert report["default_on_decision_timeout"] == "abort"
    assert "job-1" not in host._goals

    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        {"action": "abort"},
    )


def test_ws_decision_routes_to_host_not_device(monkeypatch):
    received = []

    class _Host:
        def handle_action_error_decision(
            self,
            decision_id,
            job_id,
            decision,
            *,
            decision_target=None,
        ):
            received.append((decision_id, job_id, decision, decision_target))
            return True

    monkeypatch.setattr(
        HostNode,
        "get_instance",
        classmethod(lambda cls, index=0: _Host()),
    )
    payload = {
        "decision_id": "decision-ws",
        "job_id": "job-ws",
        "device_id": "remote-device",
        "action": "retry",
    }

    asyncio.run(MessageProcessor._handle_job_error_decision(object(), payload))

    assert received == [
        (
            "decision-ws",
            "job-ws",
            payload,
            ERROR_DECISION_TARGET_BACKEND,
        )
    ]


def test_host_micro_backend_decision_stays_local_and_rejects_cloud_reply():
    from unilabos.app.web.event_bus import monitor_bus

    sub_id, event_queue, _ = monitor_bus.subscribe(channels={"action"})
    host = FakeHostDecisionNode()
    try:
        decision_id = _begin_pending(
            host,
            item=_queue_item(ERROR_DECISION_TARGET_MICRO_BACKEND),
        )

        required_event = event_queue.get(timeout=1)
        assert required_event["channel"] == "action"
        assert required_event["type"] == "job_error_decision_required"
        assert required_event["data"]["decision_id"] == decision_id
        assert required_event["data"]["expires_at"] > required_event["data"]["created_at"]
        assert host.bridge.reports == []
        reports = host.get_pending_action_error_decisions(
            ERROR_DECISION_TARGET_MICRO_BACKEND,
        )
        assert [report["decision_id"] for report in reports] == [decision_id]
        assert not host.handle_action_error_decision(
            decision_id,
            "job-1",
            {"action": "abort"},
            decision_target=ERROR_DECISION_TARGET_BACKEND,
        )
        assert host.handle_action_error_decision(
            decision_id,
            "job-1",
            {"action": "retry"},
            decision_target=ERROR_DECISION_TARGET_MICRO_BACKEND,
        )
        resolved_event = event_queue.get(timeout=1)
        assert resolved_event["type"] == "job_error_decision_resolved"
        assert resolved_event["data"]["selected_action"] == "retry"
        assert (
            host.sent_goals[0][0][0].error_decision_target
            == ERROR_DECISION_TARGET_MICRO_BACKEND
        )
    finally:
        monitor_bus.unsubscribe(sub_id)


def test_local_api_job_targets_host_micro_backend(monkeypatch):
    from unilabos.app.model import JobAddReq
    from unilabos.app.web import controller

    sent = []

    class _Host:
        def send_goal(self, item, *args, **kwargs):
            sent.append(item)

    monkeypatch.setattr(
        HostNode,
        "get_instance",
        classmethod(lambda cls, index=0: _Host()),
    )
    monkeypatch.setattr(
        controller,
        "_get_action_type",
        lambda device_id, action_name: "UniLabJsonCommand",
    )
    monkeypatch.setattr(
        controller,
        "check_device_action_busy",
        lambda device_id, action_name: (False, None),
    )

    result = controller.job_add(
        JobAddReq(
            device_id="device-1",
            action="run",
            sample_material={},
        )
    )

    assert result.status == 1
    assert sent[0].error_decision_target == ERROR_DECISION_TARGET_MICRO_BACKEND


def test_micro_backend_rest_contract_roundtrip(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.web.api import api
    from unilabos.app.web.controller import job_result_store, store_job_result

    host = FakeHostDecisionNode()
    decision_id = _begin_pending(
        host,
        item=_queue_item(ERROR_DECISION_TARGET_MICRO_BACKEND),
    )
    monkeypatch.setattr(
        HostNode,
        "get_instance",
        classmethod(lambda cls, index=0: host),
    )
    app = FastAPI()
    app.include_router(api, prefix="/api/v1")
    client = TestClient(app)

    paths = app.openapi()["paths"]
    assert "/api/v1/error-decisions" in paths
    assert "/api/v1/error-decisions/{decision_id}" in paths
    assert "/api/v1/monitor/events" in paths
    assert "/api/v1/monitor/snapshot" in paths

    response = client.get("/api/v1/error-decisions")
    assert response.status_code == 200
    assert response.json()["decisions"][0]["decision_id"] == decision_id
    snapshot = client.get("/api/v1/monitor/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["host_ready"] is True
    assert snapshot.json()["pending_error_decisions"][0]["decision_id"] == decision_id

    response = client.post(
        f"/api/v1/error-decisions/{decision_id}",
        json={"action": "skip", "reason": "operator confirmed"},
    )
    assert response.status_code == 200
    assert response.json() == {"decision_id": decision_id, "status": "delivered"}

    response = client.post(
        f"/api/v1/error-decisions/{decision_id}",
        json={"action": "skip"},
    )
    assert response.status_code == 404

    store_job_result(
        "job-poll",
        "success",
        {"suc": True, "suc_type": "skip", "return_value": None},
    )
    try:
        first = client.get("/api/v1/job/job-poll/status")
        second = client.get("/api/v1/job/job-poll/status")
        assert first.status_code == second.status_code == 200
        assert first.json()["data"] == second.json()["data"]
        assert first.json()["data"]["status"] == 4
    finally:
        job_result_store.get_and_remove("job-poll")


def test_monitor_bus_sse_contract_and_bounded_replay():
    from unilabos.app.web.event_bus import MonitorBus, format_sse_event

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


def test_host_retry_uses_existing_action_client_path_and_new_transport_id():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        {"action": "retry"},
    )

    args, kwargs = host.sent_goals[0]
    assert args[0].job_id == "job-1"
    assert args[1] == "UniLabJsonCommand"
    assert args[2] == {"channel": 1}
    assert kwargs["cache_error_context"] is False
    assert kwargs["transport_goal_id"] != "job-1"
    assert host._error_execution_contexts["job-1"]["retry_count"] == 1
    assert HostNode.get_goal_status(host, "job-1") == 2
    assert not host.finished


def test_host_decision_validates_identity_and_first_result_wins():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    assert not host.handle_action_error_decision(
        decision_id,
        "other-job",
        {"action": "retry"},
    )
    assert not host.handle_action_error_decision(
        decision_id,
        "job-1",
        {"decision_id": "other-decision", "action": "retry"},
    )
    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        {"action": "skip", "result": {"ignored": True}},
    )
    assert not host.handle_action_error_decision(
        decision_id,
        "job-1",
        {"action": "abort"},
    )
    assert host.finished[0][1] == "success"
    assert host.finished[0][2]["suc_type"] == SUCCESS_TYPE_SKIP


def test_host_retry_limit_fails_closed():
    host = FakeHostDecisionNode()
    host._error_execution_contexts["job-1"]["retry_count"] = 1
    decision_id = _begin_pending(
        host,
        {
            "options": {
                "CommunicationError": [
                    {"action": "retry", "label": "重试"}
                ]
            },
            "max_retries": 1,
        },
    )

    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        {"action": "retry"},
    )

    assert not host.sent_goals
    assert host.finished[0][1] == "failed"
    assert "exceeded 1 retries" in host.finished[0][2]["error"]


def test_host_dispatches_registered_fallback_action():
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
        {"action": "reset_connection"},
    )

    args, kwargs = host.sent_goals[0]
    assert args[0].action_name == "auto-reset"
    assert args[2] == {"channel": 2}
    assert kwargs["result_item"].action_name == "run"
    assert kwargs["recovery_suc_type"] == SUCCESS_TYPE_OPERATOR_INTERVENTION
    assert kwargs["cache_error_context"] is False


def test_host_rejects_unconfigured_backend_option_without_consuming_pending():
    host = FakeHostDecisionNode()
    decision_id = _begin_pending(host)

    assert not host.handle_action_error_decision(
        decision_id,
        "job-1",
        {"action": "force_success"},
    )
    assert decision_id in host._pending_action_error_decisions

    assert host.handle_action_error_decision(
        decision_id,
        "job-1",
        {"action": "abort"},
    )
