"""车辆准备与正常执行共用序列；丢回执、历史恢复不产生新的执行身份。"""

from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import INVOCATION, event_ack
from tests.app.center_client.test_event_envelope import draft, store_at
from tests.app.center_client.test_event_history import recovery, response, session
from tests.app.center_client.test_handoff_events import intent
from unilabos.app.center_client.client import CenterClientError
from unilabos.center_protocol.digest import canonical_json, digest


def request():
    return {"request_uuid": str(uuid4()), "generation": 1, "condition_digest": digest({"ready": True})}


def test_preparation_interleaves_with_handoff_and_task_and_replays_after_restart(tmp_path):
    path = tmp_path / "center.db"
    store = store_at(path)
    args = {"event_uuid": str(uuid4()), "request": request(), "fact": "waiting"}
    first = store.preparation_event(**args)
    second = store.remember_handoff_intent(intent(store))
    third = store.record_event(INVOCATION, terminal=False, body=draft())
    assert [first["event_sequence"], second["event_sequence"], third["event_sequence"]] == [1, 2, 3]
    assert store.preparation_event(**args) == first
    store.close()
    store = store_at(path)
    assert canonical_json(store.preparation_events()[0]) == canonical_json(first)
    assert store.preparation_event(**args) == first
    wrong = event_ack(first)
    wrong["event_digest"] = digest({"foreign": True})
    with pytest.raises(ValueError):
        store.acknowledge_preparation(first, wrong)
    assert store.preparation_events() == [first]
    store.acknowledge_preparation(first, event_ack(first))
    assert store.preparation_events() == []
    assert store.record_event(str(uuid4()), terminal=False, body=draft())["event_sequence"] == 4
    store.close()


def test_preparation_uuid_cannot_be_reused_by_other_stream(tmp_path):
    store = store_at(tmp_path / "center.db")
    event = store.preparation_event(event_uuid=str(uuid4()), request=request(), fact="waiting")
    body = draft()
    body["event_uuid"] = event["event_uuid"]
    with pytest.raises(CenterClientError):
        store.record_event(INVOCATION, terminal=False, body=body)
    handoff = intent(store)
    handoff["event_uuid"] = event["event_uuid"]
    with pytest.raises(CenterClientError):
        store.remember_handoff_intent(handoff)
    with pytest.raises(CenterClientError):
        store.preparation_event(event_uuid=event["event_uuid"], request=request(), fact="waiting")
    assert store._db.execute("SELECT last_event_sequence FROM center_runtime_state").fetchone()[0] == 1
    store.close()


def test_missing_preparation_restores_from_exact_center_history_without_task(tmp_path):
    path = tmp_path / "center.db"
    source = store_at(path)
    session(source)
    first = source.preparation_event(event_uuid=str(uuid4()), request=request(), fact="waiting")
    source.record_event(INVOCATION, terminal=False, body=draft())
    with source._db.transaction():
        source._db.execute("DELETE FROM center_preparation_outbox")
    source.close()
    store, transport, recover = recovery(tmp_path, [response(first, prefix=2)], path)
    assert store.gaps() == [1]
    assert recover.recover(1)["state"] == "recovered"
    assert store.gaps() == []
    assert store._db.execute("SELECT COUNT(*) FROM center_invocation_inbox").fetchone()[0] == 0
    store.close()
    restored = store_at(path)
    assert restored.preparation_events() == []
    assert restored.record_event(str(uuid4()), terminal=False, body=draft())["event_sequence"] == 3
    restored.close()


def test_never_created_transport_is_durably_fenced_without_fake_task_or_event(tmp_path):
    from tests.app.center_client.test_atomic_workflow_events import _submit, _workflow
    from tests.app.center_client.test_reconciliation import snapshot
    from unilabos.workflow.service import WorkflowConflict

    with _workflow(tmp_path) as (store, service, durable, package, _):
        proof = snapshot(package, None)
        proof["state"].update(dispatch_status="canceled", attention_state="clear")
        proof["evidence_digest"] = digest({k: v for k, v in proof.items() if k != "evidence_digest"})
        request = {"station_invocation_uuid": INVOCATION, "global_task_uuid": package.global_task_uuid}
        allocation = str(uuid4())
        durable.fence_transport_without_task(request, allocation, proof)
        durable.fence_transport_without_task(request, allocation, proof)
        assert durable.uncreated_terminal(INVOCATION)
        with pytest.raises(CenterClientError):
            durable.remember_package(package)
        with pytest.raises(WorkflowConflict):
            _submit(service, package)
        assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM center_boundary_outbox").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM center_transport_revocation").fetchone()[0] == 1


def test_transport_cannot_fence_a_real_task_or_unsettled_center_call(tmp_path):
    from tests.app.center_client.test_atomic_workflow_events import _submit, _workflow
    from tests.app.center_client.test_reconciliation import snapshot

    with _workflow(tmp_path) as (store, service, durable, package, _):
        request = {"station_invocation_uuid": INVOCATION, "global_task_uuid": package.global_task_uuid}
        proof = snapshot(package, None)
        with pytest.raises(CenterClientError):
            durable.fence_transport_without_task(request, str(uuid4()), proof)
        _submit(service, package)
        proof["state"].update(dispatch_status="canceled", attention_state="clear")
        proof["evidence_digest"] = digest({k: v for k, v in proof.items() if k != "evidence_digest"})
        with pytest.raises(CenterClientError):
            durable.fence_transport_without_task(request, str(uuid4()), proof)
        assert store.db.execute("SELECT COUNT(*) FROM center_transport_revocation").fetchone()[0] == 0


def test_waiting_reason_changes_are_distinct_facts_in_the_same_stream(tmp_path):
    store = store_at(tmp_path / "center.db")
    args = dict(request=request(), fact="waiting", event_uuid=str(uuid4()), waiting_reason="fleet_busy")
    first = store.preparation_event(**args)
    assert store.preparation_event(**args) == first
    second = store.preparation_event(**{**args, "event_uuid": str(uuid4()), "waiting_reason": "no_compatible_vehicle"})
    assert second["event_sequence"] == first["event_sequence"] + 1
    assert store.latest_preparation_event(args["request"]["request_uuid"]) == second
    with pytest.raises(ValueError):
        store.preparation_event(**{**args, "event_uuid": str(uuid4()), "waiting_reason": "unknown"})
    store.close()
