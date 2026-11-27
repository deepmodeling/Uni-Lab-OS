"""单调用显式核实只恢复已有 Task 的事实，不创建或调度第二次执行。"""

from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.center_client.test_atomic_workflow_events import _events, _submit, _workflow
from tests.app.center_client.test_center_client import INSTANCE, INVOCATION, STATION, config
from unilabos.app.center_client.client import CenterClient, CenterClientError
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.reconciliation import ReconciliationRecovery
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.reconciliation import ReconciliationError, validate_request, validate_snapshot
from unilabos.workflow.service import WorkflowConflict
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


def snapshot(package, task_uuid):
    value = {
        "station_invocation_uuid": INVOCATION, "global_task_uuid": package.global_task_uuid,
        "station_uuid": STATION, "workflow_uuid": package.raw["workflow_uuid"], "payload_digest": package.payload_digest,
        "state": {"dispatch_status": "accepted", "execution_status": "not_started", "attention_state": "required",
                  "owner_instance_uuid": INSTANCE, "local_task_uuid": task_uuid, "result_digest": None, "result_revision": 0},
        "state_revision": 4, "result_revision": 0, "recovery_blocked": True,
        "result_events": [], "event_receipts": [], "conflicts": [], "history": [],
    }
    value["evidence_digest"] = digest(value)
    return value


class Center:
    def __init__(self, package, task_uuid):
        self.package = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
        self.snapshot = snapshot(package, task_uuid)
        self.requests = []
        self.receipts = {}
        self.after_commit = None
        self.bad_receipt = None

    def request(self, method, path, *, json_body=None, session_headers=None, **kwargs):
        if path.endswith("/session"):
            return {"session_uuid": str(uuid4()), "session_epoch": 2,
                    "station_uuid": STATION, "instance_uuid": INSTANCE}
        if path.endswith("/reconciliation-state"):
            return deepcopy(self.snapshot)
        if path.endswith("/task-package"):
            return deepcopy(self.package)
        assert path.endswith("/reconciliations"), "核实流程不得调用 claim 或其他执行接口"
        validate_request(json_body)
        key = session_headers["Idempotency-Key"]
        self.requests.append((key, deepcopy(json_body)))
        if key not in self.receipts:
            state = deepcopy(self.snapshot)
            state["state_revision"] += 1
            state["state"]["attention_state"] = "clear"
            identity = str(uuid4())
            state["history"].append({
                "reconciliation_uuid": identity, "request_digest": digest(json_body),
                "resolution": deepcopy(json_body["resolution"]), "reason": json_body["reason"],
                "evidence_refs": deepcopy(json_body["evidence_refs"]), "actor_kind": "machine",
                "actor_uuid": INSTANCE, "created_at_ms": 0,
            })
            state["evidence_digest"] = digest({key: value for key, value in state.items() if key != "evidence_digest"})
            self.receipts[key] = {"reconciliation_uuid": identity, "resolution": deepcopy(json_body["resolution"]),
                                  "snapshot": state, "execution_authorized": False}
        if self.after_commit is not None:
            callback, self.after_commit = self.after_commit, None
            callback()
            raise CenterTransportError("核实已提交但响应丢失", retryable=True)
        receipt = deepcopy(self.receipts[key])
        if self.bad_receipt:
            self.bad_receipt(receipt)
        return receipt


def recovery(tmp_path, service, durable, package, task_uuid):
    remote = Center(package, task_uuid)
    return ReconciliationRecovery(CenterClient(config(tmp_path), remote), durable, service), remote


def remove_inbox(store):
    with store.transaction() as connection:
        connection.execute("DELETE FROM center_invocation_inbox WHERE invocation_uuid=?", (INVOCATION,))


def counts(store):
    return tuple(store.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                 for name in ("workflow_task", "workflow_node_job"))


def test_resume_repairs_only_existing_task_and_preserves_accepted_identity(tmp_path):
    with _workflow(tmp_path, schedule=True) as (store, service, durable, package, scheduler):
        task = _submit(service, package)
        original_events, original_counts = _events(store), counts(store)
        remove_inbox(store)
        flow, remote = recovery(tmp_path, service, durable, package, task["uuid"])
        key = flow.prepare_resume(INVOCATION, reason="本地任务与设备日志已核实", evidence_refs=["log://original-task"])
        assert remote.requests == []
        assert durable.reconciliation(key)["attempted"] == 0
        receipt = flow.send(key)
        assert receipt["execution_authorized"] is False
        row = store.db.execute("SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?", (INVOCATION,)).fetchone()
        assert row["claim_confirmed"] == 0
        assert row["reconciliation_uuid"] == receipt["reconciliation_uuid"]
        assert row["local_task_uuid"] == task["uuid"]
        assert _events(store) == original_events
        assert counts(store) == original_counts
        assert scheduler.submissions == [task["uuid"]]
        assert durable.reconciled_existing_task(INVOCATION, task["uuid"])
        assert flow.send(key) == receipt
        assert len(remote.requests) == 1
        assert counts(store) == original_counts


def test_missing_task_cannot_prepare_recovery(tmp_path):
    with _workflow(tmp_path) as (store, service, durable, package, _):
        flow, remote = recovery(tmp_path, service, durable, package, str(uuid4()))
        with pytest.raises(WorkflowConflict, match="现存 Task"):
            flow.prepare_resume(INVOCATION, reason="待核实", evidence_refs=["log://missing"])
        assert counts(store) == (0, 0)
        assert remote.requests == []
        assert store.db.execute("SELECT COUNT(*) FROM center_reconciliation_request").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["input", "request_fingerprint", "revision_fingerprint", "local_task"])
def test_resume_rejects_changed_permanent_task_facts(tmp_path, field):
    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        if field != "local_task":
            value = json.dumps({"value": 8}) if field == "input" else "sha256:" + "a" * 64
            with store.transaction() as connection:
                connection.execute(f"UPDATE workflow_task SET {field}=? WHERE uuid=?", (value, task["uuid"]))
        flow, remote = recovery(tmp_path, service, durable, package, str(uuid4()) if field == "local_task" else task["uuid"])
        with pytest.raises(WorkflowConflict):
            flow.prepare_resume(INVOCATION, reason="身份核实", evidence_refs=["log://identity"])
        assert remote.requests == []
        assert counts(store)[0] == 1


def test_first_send_rejects_task_that_became_terminal(tmp_path):
    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        flow, remote = recovery(tmp_path, service, durable, package, task["uuid"])
        key = flow.prepare_resume(INVOCATION, reason="恢复核实", evidence_refs=["log://resume"])
        TaskRuntimeProjection(store).project_canceled(task["uuid"])
        with pytest.raises(WorkflowConflict, match="终态"):
            flow.send(key)
        assert remote.requests == []
        assert durable.reconciliation(key)["attempted"] == 0


@pytest.mark.parametrize("soft_delete", [False, True])
def test_lost_ack_restart_keeps_body_and_reports_real_terminal_without_new_task(tmp_path, soft_delete):
    with _workflow(tmp_path, schedule=True) as (store, service, durable, package, scheduler):
        task = _submit(service, package)
        remove_inbox(store)
        flow, remote = recovery(tmp_path, service, durable, package, task["uuid"])
        key = flow.prepare_resume(INVOCATION, reason="已核实原任务", evidence_refs=["log://device"])
        def finish_original_task():
            TaskRuntimeProjection(store).project_canceled(task["uuid"])
            if soft_delete:
                TaskRuntimeProjection(store).project_cleanup_settled(task["uuid"])
                store.delete_task(task["uuid"])

        remote.after_commit = finish_original_task
        with pytest.raises(CenterTransportError):
            flow.send(key)
        assert durable.reconciliation(key)["receipt"] is None
        restarted = DurableCenterStore(tmp_path / "workflow_history.db", station_uuid=STATION, instance_uuid=INSTANCE)
        try:
            again = ReconciliationRecovery(CenterClient(config(tmp_path), remote), restarted, service)
            again.send(key)
            assert remote.requests[0] == remote.requests[1]
            assert _events(store)[-1]["payload"]["status"] == "canceled"
            original = store.db.execute("SELECT status,deleted_at FROM workflow_task WHERE uuid=?", (task["uuid"],)).fetchone()
            assert original["status"] == "canceled"
            assert (original["deleted_at"] is not None) == soft_delete
            assert counts(store)[0] == 1
            assert scheduler.submissions == [task["uuid"]]
        finally:
            restarted.close()


@pytest.mark.parametrize("field", ["execution_authorized", "local_task", "owner", "digest",
                                     "request_digest", "reason", "missing_history", "duplicate_history", "actor"])
def test_bad_ack_cannot_repair_inbox(tmp_path, field):
    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        remove_inbox(store)
        flow, remote = recovery(tmp_path, service, durable, package, task["uuid"])
        key = flow.prepare_resume(INVOCATION, reason="核实", evidence_refs=["log://proof"])

        def tamper(receipt):
            if field == "execution_authorized":
                receipt[field] = True
            elif field == "digest":
                receipt["snapshot"]["evidence_digest"] = "sha256:" + "a" * 64
            elif field in {"request_digest", "reason", "missing_history", "duplicate_history", "actor"}:
                history = receipt["snapshot"]["history"]
                if field == "missing_history":
                    history.clear()
                elif field == "duplicate_history":
                    history.append(deepcopy(history[0]))
                elif field == "actor":
                    history[0]["actor_uuid"] = str(uuid4())
                elif field == "request_digest":
                    other_request = deepcopy(remote.requests[-1][1])
                    other_request["expected_state_revision"] += 1
                    history[0]["request_digest"] = digest(other_request)
                else:
                    history[0][field] = "另一份同 resolution 请求的自洽回执"
                    other_request = deepcopy(remote.requests[-1][1])
                    other_request["reason"] = history[0]["reason"]
                    history[0]["request_digest"] = digest(other_request)
                receipt["snapshot"]["evidence_digest"] = digest({k: v for k, v in receipt["snapshot"].items() if k != "evidence_digest"})
            else:
                state = receipt["snapshot"]["state"]
                state["local_task_uuid" if field == "local_task" else "owner_instance_uuid"] = str(uuid4())
                receipt["snapshot"]["evidence_digest"] = digest({k: v for k, v in receipt["snapshot"].items() if k != "evidence_digest"})

        remote.bad_receipt = tamper
        with pytest.raises((CenterClientError, ReconciliationError)):
            flow.send(key)
        assert store.db.execute("SELECT COUNT(*) FROM center_invocation_inbox").fetchone()[0] == 0
        assert durable.reconciliation(key)["receipt"] is None


def test_apply_fault_rolls_back_binding_and_outbox_but_keeps_ack_for_retry(tmp_path, monkeypatch):
    from unilabos.persistence import center_events

    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        remove_inbox(store)
        flow, remote = recovery(tmp_path, service, durable, package, task["uuid"])
        key = flow.prepare_resume(INVOCATION, reason="核实", evidence_refs=["log://proof"])
        original = center_events.append_event_in_transaction

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("事实提交故障")

        with monkeypatch.context() as scoped:
            scoped.setattr(center_events, "append_event_in_transaction", fail)
            with pytest.raises(RuntimeError, match="提交故障"):
                flow.send(key)
        assert store.db.execute("SELECT COUNT(*) FROM center_invocation_inbox").fetchone()[0] == 0
        assert durable.reconciliation(key)["receipt"] is not None
        flow.send(key)
        assert len(remote.requests) == 1
        assert durable.reconciliation(key)["applied"] == 1


@pytest.mark.parametrize("field", ["event_epoch_uuid", "instance_uuid"])
def test_old_identity_resume_receipt_cannot_authorize_new_ledger(tmp_path, field):
    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        remove_inbox(store)
        flow, _ = recovery(tmp_path, service, durable, package, task["uuid"])
        key = flow.prepare_resume(INVOCATION, reason="核实", evidence_refs=["log://proof"])
        flow.send(key)
        with store.transaction() as connection:
            connection.execute(f"UPDATE center_runtime_state SET {field}=?", (str(uuid4()),))
        with pytest.raises(WorkflowConflict, match="纪元|实例"):
            service.recover_center_workflow_invocation(INVOCATION)


def test_record_evidence_needs_no_local_task_and_does_not_change_claim(tmp_path):
    with _workflow(tmp_path) as (store, service, durable, package, _):
        flow, _ = recovery(tmp_path, service, durable, package, str(uuid4()))
        key = flow.prepare_evidence(INVOCATION, reason="补充设备日志", evidence_refs=["log://evidence"])
        receipt = flow.send(key)
        assert receipt["resolution"] == {"kind": "record_evidence"}
        assert counts(store) == (0, 0)
        assert durable.reconciliation(key)["applied"] == 1


def test_snapshot_digest_covers_new_conflicting_evidence(tmp_path):
    with _workflow(tmp_path) as (_store, _service, _durable, package, _):
        state = snapshot(package, str(uuid4()))
        state["conflicts"].append({"event_uuid": str(uuid4()), "event_digest": "sha256:" + "c" * 64})
        with pytest.raises(ReconciliationError, match="摘要"):
            validate_snapshot(state, INVOCATION)


def test_confirm_recorded_result_keeps_existing_task_and_result_unchanged(tmp_path):
    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        TaskRuntimeProjection(store).project_canceled(task["uuid"])
        before = _events(store)
        flow, remote = recovery(tmp_path, service, durable, package, task["uuid"])
        result_digest = "sha256:" + "b" * 64
        remote.snapshot["state"].update(execution_status="canceled", result_digest=result_digest, result_revision=1)
        remote.snapshot["result_revision"] = 1
        remote.snapshot["evidence_digest"] = digest({k: v for k, v in remote.snapshot.items() if k != "evidence_digest"})
        key = flow.prepare_result_confirmation(
            INVOCATION, local_task_uuid=task["uuid"], result_event_uuid=before[-1]["event_uuid"],
            result_digest=result_digest, reason="已核对原取消结果", evidence_refs=["log://original-result"],
        )
        receipt = flow.send(key)
        assert receipt["resolution"]["kind"] == "confirm_recorded_result"
        assert _events(store) == before
        assert counts(store)[0] == 1
        assert durable.reconciliation(key)["applied"] == 1


def test_client_rejects_invalid_path_and_oversized_key_before_network(tmp_path):
    with _workflow(tmp_path) as (_store, service, durable, package, _):
        flow, remote = recovery(tmp_path, service, durable, package, str(uuid4()))
        with pytest.raises(CenterClientError, match="UUID"):
            flow.inspect("../another-invocation")
        key = flow.prepare_evidence(INVOCATION, reason="证据", evidence_refs=["log://proof"])
        with pytest.raises(CenterClientError, match="幂等键"):
            flow.client.reconcile(INVOCATION, durable.reconciliation(key)["body"],
                                  idempotency_key="核" * 67, event_epoch_uuid=durable.event_epoch_uuid)
        assert remote.requests == []
