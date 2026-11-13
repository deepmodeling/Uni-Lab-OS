"""严格合同必须在真实 Host 调用前和上报成功前生效。"""
from copy import deepcopy

import pytest

from unilabos.registry.typed_contract import TypedActionValueError, validate_action_values
from unilabos.server.backend.execution_queue import JOB_ORIGIN_LOCAL_SCHEDULER
from tests.server.test_action_timeout_execution import _Bridge, _make_backend, _payload, _wait_until

SCHEMA = {
    "type": "object", "x-unilabos-action-contract": {"version": 2},
    "properties": {
        "goal": {"type": "object", "properties": {"volume": {"type": "number", "minimum": 0}}, "required": ["volume"], "additionalProperties": False},
        "result": {"type": "object", "properties": {"actual_volume": {"type": "number", "minimum": 0}}, "required": ["actual_volume"], "additionalProperties": False},
    },
}


def test_invalid_goal_never_reaches_adapter():
    bridge = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}), resumable=True)
    backend, adapter = _make_backend({"schema": SCHEMA}, bridge)
    try:
        backend.dispatch(_payload("invalid-goal", JOB_ORIGIN_LOCAL_SCHEDULER, {"volume": -1}))
        _wait_until(lambda: bridge.decisions)
        assert adapter.sent == []
        assert bridge.decisions[0]["exception_type"] == "TypedActionValueError"
    finally:
        backend.stop()


def test_invalid_success_result_enters_error_decision():
    bridge = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}), resumable=True)
    backend, adapter = _make_backend({"schema": SCHEMA}, bridge)
    try:
        backend.dispatch(_payload("invalid-result", JOB_ORIGIN_LOCAL_SCHEDULER, {"volume": 1}))
        _wait_until(lambda: adapter.sent)
        backend.publish_job_status({}, adapter.sent[0], "success", {"suc": True, "return_value": {"actual_volume": "unknown"}})
        _wait_until(lambda: bridge.decisions)
        assert bridge.decisions[0]["exception_type"] == "TypedActionValueError"
        assert not any(status == "success" for _, status, _ in bridge.statuses)
        assert backend.resolve_error_decision(bridge.decisions[0]["decision_id"], {"action": "abort", "scheduler_updated": True})
        _wait_until(lambda: bridge.statuses)
        assert bridge.statuses[-1][1] == "failed"
        assert bridge.statuses[-1][2]["suc"] is False
    finally:
        backend.stop()


def test_physical_contract_is_not_silently_treated_as_implemented():
    schema = deepcopy(SCHEMA)
    schema["x-unilabos-action-contract"]["resource_contract"] = {"version": 1, "aliquot": {"source_material_param": "source", "target_material_params": ["target"]}}
    with pytest.raises(TypedActionValueError, match="尚未启用.*aliquot"):
        validate_action_values(schema, {"volume": 1})


def test_operator_replacement_obeys_result_contract_and_invalid_value_keeps_decision():
    bridge = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}), resumable=True)
    backend, adapter = _make_backend({"schema": SCHEMA}, bridge)
    try:
        backend.dispatch(_payload("replace-result", JOB_ORIGIN_LOCAL_SCHEDULER, {"volume": 1}))
        _wait_until(lambda: adapter.sent)
        backend.publish_job_status({}, adapter.sent[0], "success", {"suc": True, "return_value": {"actual_volume": "unknown"}})
        _wait_until(lambda: bridge.decisions)
        decision = bridge.decisions[0]["decision_id"]
        assert not backend.resolve_error_decision(decision, {"action": "operator_intervention", "result": {"actual_volume": -1}, "scheduler_updated": True})
        assert backend.list_error_decisions()[0]["decision_id"] == decision
        assert backend.resolve_error_decision(decision, {"action": "operator_intervention", "result": {"actual_volume": 1}, "scheduler_updated": True})
        _wait_until(lambda: bridge.statuses)
        assert bridge.statuses[-1][1] == "success"
    finally:
        backend.stop()


def test_authority_rejects_invalid_replacement_before_sending_host_command(monkeypatch):
    from tests.server.test_edge_control_service import _service, _dispatch_payload, _edge_change

    report = {"decision_id": "decision-1", "job_id": "job-1", "device_id": "device-1", "action_name": "pick",
              "options": [{"action": "operator_intervention"}]}
    authority = _service({"snapshot-1": {"report": report, "item": {}, "return_info": {}}})
    monkeypatch.setattr(authority, "resolve_action_schema", lambda device, action: SCHEMA)
    authority.attach_connection()
    authority.dispatch(_dispatch_payload())
    authority.outgoing.get_nowait()
    authority.handle_message("edge_change", _edge_change(1, event_type="execution.error_pending", detail_payload_uuid="snapshot-1"))
    assert not authority.resolve_error_decision("decision-1", {"action": "operator_intervention", "result": {"actual_volume": -1}})
    assert authority.list_error_decisions()[0]["decision_id"] == "decision-1"
    assert authority.outgoing.empty()
    assert authority.resolve_error_decision("decision-1", {"action": "operator_intervention", "result": {"actual_volume": 1}})
    assert authority.outgoing.get_nowait()["data"]["command_type"] == "replace_result"


@pytest.mark.parametrize("failure", ["missing", "unreachable"])
def test_resource_validation_requires_reachable_authority(failure):
    from types import SimpleNamespace
    from uuid import uuid4
    from unilabos.server.services.runtime.workflow.run_preflight import ResourceUnavailableError, validate_resource_values

    schema = {"properties": {"goal": {"properties": {"source": {"x-unilabos-material-lock": True}}}}}

    def unavailable(_identity):
        raise RuntimeError("库存服务不可达")

    authority = None if failure == "missing" else SimpleNamespace(get_material=unavailable)
    with pytest.raises(ResourceUnavailableError):
        validate_resource_values(schema, {"source": {"uuid": str(uuid4())}}, authority)
