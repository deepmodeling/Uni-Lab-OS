"""V1 工作流干预、结构化人工交互与 /error-decisions 合同。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from tests.server.v1_lifecycle_support import Stack, wait_for
from unilabos.server.services.runtime.workflow.human_interactions import HumanInteractions


@pytest.fixture
def stack():
    value = Stack()
    yield value
    value.close()


def _pending_decision(stack: Stack, task, decision_id="decision-1", options=None):
    job_uuid = stack.executor.dispatched[0]["job_id"]
    stack.service.mark_workflow_node_job_decision_pending(job_uuid, {
        "decision_id": decision_id, "job_id": job_uuid, "task_id": task["uuid"], "device_id": "device-a",
        "action_name": "record", "error_message": "夹爪卡住",
        "options": options or [{"action": "retry"}, {"action": "skip"}, {"action": "abort"}],
    })
    return job_uuid


def test_decision_pending_opens_an_intervention_and_selection_is_delivered(stack: Stack) -> None:
    delivered = []
    stack.service._intervention_delivery = lambda decision_id, payload: delivered.append((decision_id, payload)) or True
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    job_uuid = _pending_decision(stack, task)

    listing = ok_data(stack.client.get("/api/v1/workflow-interventions"))
    assert len(listing) == 1
    intervention = listing[0]
    assert intervention["workflow_node_job_uuid"] == job_uuid and intervention["revision"] == 1
    assert intervention["status"] == "open" and intervention["edge_command_uuid"] == "decision-1"
    assert [option["id"] for option in intervention["options"]] == ["retry", "skip", "abort"]
    assert intervention["meta_data"]["error_message"] == "夹爪卡住"
    assert ok_data(stack.client.get(f"/api/v1/workflow-interventions/{intervention['uuid']}"))["uuid"] == intervention["uuid"]

    url = f"/api/v1/workflow-interventions/{intervention['uuid']}/decisions"
    error_of(stack.client.post(url, json={"revision": 2, "option_id": "skip"}, headers={"Idempotency-Key": "k"}), 3003)
    error_of(stack.client.post(url, json={"revision": 1, "option_id": "nope"}, headers={"Idempotency-Key": "k"}), 3003)
    error_of(stack.client.post(url, json={"revision": 1, "option_id": "skip"}), 3003)
    created = stack.client.post(url, json={"revision": 1, "option_id": "skip"}, headers={"Idempotency-Key": "k"})
    body = ok_data(created, status=201)
    assert body["created"] is True and body["command_uuid"] == "decision-1"
    assert body["intervention"]["status"] == "selected" and body["intervention"]["delivery_status"] == "accepted"
    assert delivered == [("decision-1", {"option": {"action": "skip", "id": "skip"}, "action": "skip",
                                         "reason": f"workflow intervention {intervention['uuid']}",
                                         "operator_id": "local-workspace"})]
    replay = stack.client.post(url, json={"revision": 1, "option_id": "skip"}, headers={"Idempotency-Key": "k"})
    assert ok_data(replay, status=200)["created"] is False and len(delivered) == 1
    error_of(stack.client.post(url, json={"revision": 1, "option_id": "abort"}, headers={"Idempotency-Key": "k2"}), 3003)
    assert ok_data(stack.client.get("/api/v1/workflow-interventions", params={"status": "selected"}))[0]["uuid"] == \
        intervention["uuid"]
    error_of(stack.client.get(f"/api/v1/workflow-interventions/{uuid4()}"), 3002)


def test_undelivered_selection_is_a_conflict_and_can_be_retried(stack: Stack) -> None:
    accept = {"value": False}
    stack.service._intervention_delivery = lambda decision_id, payload: accept["value"]
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    _pending_decision(stack, task)
    intervention = ok_data(stack.client.get("/api/v1/workflow-interventions"))[0]
    url = f"/api/v1/workflow-interventions/{intervention['uuid']}/decisions"
    error_of(stack.client.post(url, json={"revision": 1, "option_id": "abort"}, headers={"Idempotency-Key": "k"}), 3003)
    assert ok_data(stack.client.get(f"/api/v1/workflow-interventions/{intervention['uuid']}"))["delivery_status"] == "unknown"
    accept["value"] = True
    assert ok_data(stack.client.post(url, json={"revision": 1, "option_id": "abort"},
                                     headers={"Idempotency-Key": "k"}), status=200)["intervention"]["delivery_status"] == "accepted"


def test_new_decision_supersedes_and_terminal_attempt_closes_interventions(stack: Stack) -> None:
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    job_uuid = _pending_decision(stack, task)
    stack.service.mark_workflow_node_job_decision_resumed(job_uuid, "decision-1")
    assert ok_data(stack.client.get("/api/v1/workflow-interventions")) == []
    assert ok_data(stack.client.get("/api/v1/workflow-interventions", params={"status": "superseded"}))[0]["revision"] == 1
    _pending_decision(stack, task, decision_id="decision-2")
    assert ok_data(stack.client.get("/api/v1/workflow-interventions"))[0]["revision"] == 2
    stack.executor.finish(0)
    wait_for(lambda: stack.task(task)["status"] == "succeeded")
    assert ok_data(stack.client.get("/api/v1/workflow-interventions")) == []


def test_human_interaction_reply_is_validated_and_idempotent(stack: Stack) -> None:
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    job_uuid = stack.executor.dispatched[0]["job_id"]
    humans = HumanInteractions(stack.service)
    request = humans.open(job_uuid, kind="choose", arguments={"options": ["a", "b"], "labels": ["甲", "乙"]})
    listing = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/human-interactions"))
    assert [item["uuid"] for item in listing] == [request["uuid"]] and listing[0]["status"] == "pending"
    url = f"/api/v1/workflow-human-interactions/{request['uuid']}/reply"
    error_of(stack.client.post(url, json={"choice": "c"}), 1000)
    error_of(stack.client.post(url, json={"text": "a"}), 1000)
    error_of(stack.client.post(url, json={"choice": "a", "bogus": 1}), 1000)
    answered = ok_data(stack.client.post(url, json={"choice": "a"}))
    assert answered["status"] == "answered" and answered["result"] == {"choice": "a"}
    assert ok_data(stack.client.post(url, json={"choice": "a"}))["status"] == "answered"
    error_of(stack.client.post(url, json={"choice": "b"}), 3003)
    error_of(stack.client.post(f"/api/v1/workflow-human-interactions/{uuid4()}/reply", json={"choice": "a"}), 3002)
    with pytest.raises(Exception):
        humans.open(job_uuid, kind="choose", arguments={"options": ["x"], "labels": ["X"]})


def test_human_interaction_sketch_and_terminal_task_cancels_pending(stack: Stack) -> None:
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    job_uuid = stack.executor.dispatched[0]["job_id"]
    request = HumanInteractions(stack.service).open(job_uuid, kind="sketch", arguments={"timeout_seconds": 60})
    url = f"/api/v1/workflow-human-interactions/{request['uuid']}/reply"
    error_of(stack.client.post(url, json={"x": [1, 2], "y": [1, 2], "corner_x": [], "corner_y": []}), 1000)
    stack.executor.finish(0)
    wait_for(lambda: stack.task(task)["status"] == "succeeded")
    listing = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/human-interactions"))
    assert listing[0]["status"] == "canceled"
    error_of(stack.client.post(url, json={"x": [1.0, 2.0, 3.0], "y": [1.0, 2.0, 3.0], "corner_x": [], "corner_y": []}), 3003)


class _Backend:
    def __init__(self):
        self.pending = {"d-1": {"decision_id": "d-1", "job_id": "job-1", "options": [{"action": "retry"}]}}
        self.resolved = []

    def list_error_decisions(self):
        return list(self.pending.values())

    def resolve_error_decision(self, decision_id, decision):
        if decision_id not in self.pending:
            return False
        self.pending.pop(decision_id)
        self.resolved.append((decision_id, decision))
        return True


def test_error_decisions_items_resolved_and_idempotency_key() -> None:
    from unilabos.server.api.runtime.diagnostics import create_backend_app

    backend = _Backend()
    with TestClient(create_backend_app(lambda: None, lambda: backend)) as client:
        assert ok_data(client.get("/api/v1/error-decisions")) == {"items": [backend.pending["d-1"]]}
        error_of(client.post("/api/v1/error-decisions/d-1", json={"reason": "缺少处理方式"}), 1000)
        headers = {"Idempotency-Key": "decide-1", "X-UniLab-Operator": "alice"}
        result = ok_data(client.post("/api/v1/error-decisions/d-1", json={"action": "retry"}, headers=headers))
        assert result == {"decision_id": "d-1", "status": "resolved"}
        assert backend.resolved[0][1]["operator_id"] == "alice" and backend.resolved[0][1]["action"] == "retry"
        # 同键同内容重放：返回首次结果，不再投递
        assert ok_data(client.post("/api/v1/error-decisions/d-1", json={"action": "retry"}, headers=headers)) == result
        assert len(backend.resolved) == 1
        error_of(client.post("/api/v1/error-decisions/d-1", json={"action": "abort"}, headers=headers), 3003)
        # 不再等待：状态冲突
        error_of(client.post("/api/v1/error-decisions/d-1", json={"action": "retry"}), 3003)
    with TestClient(create_backend_app(lambda: None, lambda: None)) as client:
        error_of(client.get("/api/v1/error-decisions"), 5001)
