"""开始许可穿过持久事务、HTTP、HostLink；重复命令不能再次调用驱动。"""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

from unilabos.protocol.runtime.execution_start import execution_identity
from unilabos.server.backend.scheduler.payloads import build_job_start_payload
from unilabos.server.backend.execution_start import ExecutionStartGuard, ExecutionStartUnknown
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.service import WorkflowService
from unilabos.server.services.runtime.workflow.errors import StoreConflict
from unilabos.server.api.runtime.workflow import create_workflow_app
from tests.server.test_workflow_execution_claims import make_task, request
from tests.server.test_workflow_parallel_execution import wait_for


def issued(service):
    task, job_id = make_task(service)
    claims = ExecutionClaims(service)
    epoch = claims.activate_authority()
    claims.reserve(request(task, job_id), authority_epoch=epoch)
    service.mark_workflow_node_job_running(job_id)
    job = service.get_workflow_node_job(job_id)
    payload = build_job_start_payload(job_id, task["uuid"], task["workflow_uuid"], job["workflow_node_uuid"],
        "reactor", "operate", "", {}, node_run_uuid=job["workflow_node_run_uuid"])
    payload["execution_permit"] = claims.seal_dispatch(job_id, payload, epoch)
    body = {"permit": payload["execution_permit"], "invocation_uuid": str(uuid4()),
            "executor_epoch": str(uuid4()), "identity": execution_identity(payload)}
    return claims, job_id, payload, body


@pytest.fixture
def service(tmp_path):
    value = WorkflowService(tmp_path / "runtime.db")
    yield value
    value.close()


def test_concurrent_connections_can_consume_a_start_only_once(service):
    claims, job, payload, body = issued(service)
    other = WorkflowService(service.path)
    try:
        second = ExecutionClaims(other)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(owner.begin_execution, job, deepcopy(body)) for owner in (claims, second)]
            assert sorted(f.result()["decision"] for f in futures) == ["START", "UNKNOWN"]
        assert claims.begin_execution(job, body)["decision"] == "UNKNOWN"
    finally:
        other.close()


@pytest.mark.parametrize("field", ["authority_epoch", "attempt_no", "fences", "action_args", "device_id", "resource_contract"])
def test_tampered_permit_or_parameters_cannot_start(service, field):
    claims, job, payload, body = issued(service)
    changed = deepcopy(body)
    if field == "fences":
        key = next(iter(changed["permit"]["fences"]))
        changed["permit"]["fences"][key] += 1
    elif field in {"authority_epoch", "attempt_no"}:
        changed["permit"][field] = "old-epoch" if field == "authority_epoch" else 2
    else:
        changed["identity"][field] = {"volume": 999} if field == "action_args" else ({"device_tenancy": {}} if field == "resource_contract" else "different-device")
    with pytest.raises(StoreConflict):
        claims.begin_execution(job, changed)
    assert claims.begin_execution(job, body)["decision"] == "START"


def test_new_authority_rejects_old_begin_and_old_scheduler_reservation(service):
    claims, job, payload, body = issued(service)
    old_epoch = body["permit"]["authority_epoch"]
    claims.activate_authority()
    with pytest.raises(StoreConflict, match="代次"):
        claims.begin_execution(job, body)
    task2, job2 = make_task(service)
    with pytest.raises(StoreConflict, match="代次"):
        claims.reserve(request(task2, job2), authority_epoch=old_epoch)
    assert claims.get(job)["state"] == "uncertain"


def test_lost_begin_reply_never_retries_physical_call(service):
    claims, job, payload, body = issued(service)
    class LostReply:
        def begin_execution(self, job, body):
            assert claims.begin_execution(job, body)["decision"] == "START"
            raise ConnectionError("已提交开始，但应答丢失")
    consumed = []
    guard = ExecutionStartGuard(LostReply(), payload, "host-1", lambda: False, lambda: consumed.append(True))
    with pytest.raises(ExecutionStartUnknown):
        guard()
    retry = ExecutionStartGuard(claims, payload, "host-2", lambda: False, lambda: consumed.append(True))
    with pytest.raises(ExecutionStartUnknown):
        retry()
    assert not consumed


def test_started_receipt_is_immutable_and_late_receipt_survives_authority_restart(service):
    claims, job, payload, body = issued(service)
    assert claims.begin_execution(job, body)["decision"] == "START"
    receipt = {key: body[key] for key in ("permit", "invocation_uuid", "executor_epoch")}
    receipt.update(receipt_uuid=str(uuid4()), started_at_ms=100)
    claims.activate_authority()
    assert claims.record_start_receipt(job, receipt)["created"]
    assert not claims.record_start_receipt(job, receipt)["created"]
    with pytest.raises(StoreConflict, match="不可改变"):
        claims.record_start_receipt(job, {**receipt, "started_at_ms": 101})


def test_http_begin_uses_same_one_shot_authority_and_closed_protocol(service):
    claims, job, payload, body = issued(service)
    with TestClient(create_workflow_app(service)) as client:
        url = f"/api/v1/workflow-node-jobs/{job}/begin"
        invalid = deepcopy(body)
        invalid["permit"]["attempt_no"] = True
        assert client.post(url, json=invalid).json()["code"] != 0
        assert client.post(url, json={**body, "ignored": True}).json()["code"] != 0
        assert client.post(url, json=body).json()["data"]["decision"] == "START"
        assert client.post(url, json=body).json()["data"]["decision"] == "UNKNOWN"


def test_hostlink_completed_payload_replay_does_not_call_driver_again():
    from tests.server.test_workflow_loop_execution import _Stack
    from unilabos.registry.workflows import upsert_workflow
    from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
    stack = _Stack()
    try:
        record = upsert_workflow(stack.service, {"workflow_uuid": str(uuid4()), "name": "一次调用", "description": "", "tags": [],
            "nodes": [{"uuid": str(uuid4()), "name": "记录", "type": "device_action", "material_uuid": str(uuid4()),
                       "action_name": "record", "meta_data": {"target_device_id": "loop-device"}, "param": {"value": 7}}], "edges": []})
        task = stack.service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
            input_value={}, description=None, meta_data={})
        wait_for(lambda: stack.service.get_workflow_task(task["uuid"])["status"] == "succeeded")
        job = stack.service.list_workflow_node_runs(task["uuid"])[0]["current_job_uuid"]
        row = stack.service.connection.execute("SELECT * FROM workflow_execution_dispatch WHERE job_uuid=?", (job,)).fetchone()
        assert row["authorized_at"] and row["receipt"]
        claim = ExecutionClaims(stack.service).get(job)
        payload = json.loads(row["identity"])
        payload["execution_permit"] = {"claim_uuid": claim["claim_uuid"], "fences": claim["fences"], "attempt_no": claim["attempt_no"],
                                       "authority_epoch": row["authority_epoch"], "payload_hash": row["payload_hash"]}
        seen = []
        stack.microbackend.add_job_finished_listener(lambda _job, _success, _result, kind, _info: seen.append(kind))
        stack.microbackend.dispatch(payload)
        wait_for(lambda: SUCCESS_TYPE_EXECUTION_UNKNOWN in seen)
        assert stack.driver.calls == [7]
    finally:
        stack.close()


def test_pause_does_not_consume_begin_then_resume_allows_it(service):
    claims, job, payload, body = issued(service)
    task = service.get_workflow_node_job(job)["workflow_task_uuid"]
    service.command_workflow_task(task, command_type="pause", expected_revision=0, idempotency_key="pause")
    assert claims.begin_execution(job, body)["decision"] == "WAIT"
    revision = service.get_workflow_task(task)["control_revision"]
    service.command_workflow_task(task, command_type="resume", expected_revision=revision, idempotency_key="resume")
    assert claims.begin_execution(job, body)["decision"] == "START"


def test_epoch_change_between_begin_and_driver_boundary_prevents_call(service):
    claims, job, payload, body = issued(service)
    class RestartAfterBegin:
        def begin_execution(self, job, body):
            result = claims.begin_execution(job, body)
            claims.activate_authority()
            return result
        record_start_receipt = claims.record_start_receipt
    guard = ExecutionStartGuard(RestartAfterBegin(), payload, "host-1", lambda: False, lambda: None)
    with pytest.raises(ExecutionStartUnknown, match="已经失效"):
        guard()


def test_unknown_hostlink_transport_keeps_task_and_resource_for_reconciliation():
    from tests.server.test_workflow_loop_execution import _Stack
    from unilabos.registry.workflows import upsert_workflow
    from unilabos.backend.hostlink.protocol import LinkError
    stack = _Stack()
    original = stack.runtime.call_action_async
    async def lose_result(*args, **kwargs):
        await original(*args, **kwargs)
        raise LinkError("设备执行后丢失传输结果")
    stack.runtime.call_action_async = lose_result
    try:
        record = upsert_workflow(stack.service, {"workflow_uuid": str(uuid4()), "name": "丢物理回执", "description": "", "tags": [],
            "nodes": [{"uuid": str(uuid4()), "name": "记录", "type": "device_action", "material_uuid": str(uuid4()),
                       "action_name": "record", "meta_data": {"target_device_id": "loop-device"}, "param": {"value": 7}}], "edges": []})
        task = stack.service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
            input_value={}, description=None, meta_data={})
        wait_for(lambda: bool(stack.scheduler.list_reconciliation_decisions()))
        decision = stack.scheduler.list_reconciliation_decisions()[0]
        assert stack.driver.calls == [7]
        assert stack.service.get_workflow_node_job(decision["job_id"])["status"] == "execution_unknown"
        assert ExecutionClaims(stack.service).get(decision["job_id"])["state"] == "uncertain"
        assert stack.scheduler.resources.held_by(decision["job_id"])
    finally:
        stack.close()


def test_stopped_authority_cannot_start_and_old_stop_does_not_revoke_new_owner(service):
    claims, job, payload, body = issued(service)
    old = body["permit"]["authority_epoch"]
    claims.deactivate_authority(old)
    with pytest.raises(StoreConflict, match="代次"):
        claims.begin_execution(job, body)
    current = claims.activate_authority()
    claims.deactivate_authority(old)
    task2, job2 = make_task(service)
    # 旧进程迟到的 stop 不能让新调度器失去派发资格。
    claims.reserve(request(task2, job2).model_copy(update={"always_free": True}), authority_epoch=current)
