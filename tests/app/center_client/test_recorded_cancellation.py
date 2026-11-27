"""旧人工取消误报只能补证并结算本站责任，不改写中心或本地执行历史。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import INSTANCE, STATION
from tests.app.center_client.test_material_cancel_settlement import SettlementCenter
from tests.app.center_client.test_material_exports import inventory  # noqa: F401
from tests.app.center_client.test_material_settlement_recovery import admit_later_task, settlement_case
from unilabos.app.center_client import recorded_cancellation as module
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.events import validate_event
from unilabos.center_protocol.material_input import validate_input_receipt
from unilabos.center_protocol.reconciliation import validate_receipt, validate_request, validate_snapshot
from unilabos.center_protocol.task_package import validate_task_package
from unilabos.persistence.center_events import append_event_in_transaction

REASON = "已核对旧人工收尾与原任务真实成功结果"
EVIDENCE = ["scan://original-carrier", "log://original-task"]


class HistoricalCenter(SettlementCenter):
    """使用真实核实协议形状；补证始终保留中心原取消终态。"""

    def __init__(self):
        super().__init__()
        self.review_requests = []
        self.review_receipts = {}
        self.lose_on_commit = None
        self.bad_receipt = None

    def attach(self, case, event):
        package = case.runtime.store.package(case.invocation)
        payload = event["payload"]
        occurred = int(datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")).timestamp() * 1000)
        result_digest = digest(["canceled", payload["output"]])
        projected_event_digest = digest([
            event["event_uuid"], case.invocation, INSTANCE, case.task["uuid"],
            "canceled", payload["output"], occurred,
        ])
        assert projected_event_digest != event["event_digest"]
        self.snapshot = {
            "station_invocation_uuid": case.invocation, "global_task_uuid": package.global_task_uuid,
            "station_uuid": STATION, "workflow_uuid": package.raw["workflow_uuid"],
            "payload_digest": package.payload_digest,
            "state": {
                "dispatch_status": "accepted", "execution_status": "canceled", "attention_state": "clear",
                "owner_instance_uuid": INSTANCE, "local_task_uuid": case.task["uuid"],
                "result_digest": result_digest, "result_revision": 1,
            },
            "state_revision": 4, "result_revision": 1, "recovery_blocked": False,
            "result_events": [{
                "event_uuid": event["event_uuid"], "event_digest": projected_event_digest,
                "event_type": "canceled", "owner_instance_uuid": INSTANCE,
                "local_task_uuid": case.task["uuid"], "application": "applied", "result_digest": result_digest,
            }],
            "event_receipts": [{
                "event_uuid": event["event_uuid"], "event_digest": event["event_digest"],
                "event_epoch_uuid": event["event_epoch_uuid"], "event_sequence": event["event_sequence"],
                "application": "applied",
            }],
            "conflicts": [], "history": [],
        }
        self.refresh()

    def refresh(self):
        self.snapshot["evidence_digest"] = digest({
            key: value for key, value in self.snapshot.items() if key != "evidence_digest"
        })

    def reconciliation_state(self, invocation, epoch):
        return validate_snapshot(deepcopy(self.snapshot), invocation)

    def reconcile(self, invocation, body, *, idempotency_key, event_epoch_uuid):
        validate_request(body)
        assert body["resolution"] == {"kind": "record_evidence"}
        self.review_requests.append((idempotency_key, deepcopy(body), event_epoch_uuid))
        if idempotency_key not in self.review_receipts:
            assert body["expected_state_revision"] == self.snapshot["state_revision"]
            assert body["expected_result_revision"] == self.snapshot["result_revision"]
            assert body["expected_evidence_digest"] == self.snapshot["evidence_digest"]
            identity = str(uuid4())
            self.snapshot["state_revision"] += 1
            self.snapshot["history"].append({
                "reconciliation_uuid": identity, "request_digest": digest(body),
                "resolution": deepcopy(body["resolution"]), "reason": body["reason"],
                "evidence_refs": deepcopy(body["evidence_refs"]), "actor_kind": "machine",
                "actor_uuid": INSTANCE, "created_at_ms": len(self.review_receipts) + 1,
            })
            self.refresh()
            self.review_receipts[idempotency_key] = {
                "reconciliation_uuid": identity, "resolution": deepcopy(body["resolution"]),
                "snapshot": deepcopy(self.snapshot), "execution_authorized": False,
            }
            if self.lose_on_commit == len(self.review_receipts):
                self.lose_on_commit = None
                raise CenterTransportError("历史补证已保存但响应丢失", retryable=True)
        receipt = deepcopy(self.review_receipts[idempotency_key])
        if self.bad_receipt:
            self.bad_receipt(receipt)
        return validate_receipt(receipt, invocation, body, actor_instance_uuid=INSTANCE)


@contextmanager
def historical_case(tmp_path, inventory):
    client = HistoricalCenter()
    with settlement_case(tmp_path, inventory, client=client) as case:
        package = case.runtime.store.package(case.invocation)
        current = case.workflow.get_task(case.task["uuid"])
        with case.workflow.transaction() as connection:
            event = append_event_in_transaction(connection, case.invocation, terminal=True, body={
                "event_uuid": str(uuid4()), "local_task_uuid": case.task["uuid"], "event_type": "outcome",
                "payload": {"task_payload_digest": package.payload_digest, "status": "canceled",
                            "output": {"reason": "历史人工收尾"}},
                "occurred_at": datetime.fromisoformat(current["finished_at"].replace("Z", "+00:00"))
                .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            })
            connection.execute(
                "UPDATE center_outcome_evidence SET pending_reason='closed_center_experiment_canceled' "
                "WHERE invocation_uuid=?", (case.invocation,),
            )
        case.runtime.store.acknowledge_event(event["event_uuid"], "applied")
        case.event = event
        client.attach(case, event)
        yield case


def inspect(case):
    return module.inspect_recorded_cancellation(case.runtime, case.invocation)


def arguments(case):
    detail = inspect(case)
    return {
        "reason": REASON, "evidence_refs": list(EVIDENCE),
        "expected_local_facts_digest": detail["local_facts_digest"],
        "expected_center_evidence_digest": detail["center_evidence_digest"],
    }


def settle(case, args=None, **changes):
    return module.reconcile_recorded_cancellation(
        case.runtime, case.invocation, **{**(args or arguments(case)), **changes},
    )


def state(case):
    with case.workflow.read() as connection:
        row = connection.execute(
            "SELECT recorded_cancellation_json FROM center_outcome_evidence WHERE invocation_uuid=?",
            (case.invocation,),
        ).fetchone()
    return json.loads(row[0]) if row[0] else None


def release_events(case):
    return case.inventory.query_all(
        "SELECT * FROM inventory_ledger WHERE op_type='resources_released' AND aggregate_id=?",
        (case.invocation,),
    )


def history(case):
    with case.workflow.read() as connection:
        outbox = [dict(row) for row in connection.execute(
            "SELECT * FROM center_boundary_outbox ORDER BY event_sequence"
        ).fetchall()]
        recorded = connection.execute(
            "SELECT terminal_recorded FROM center_invocation_inbox WHERE invocation_uuid=?", (case.invocation,),
        ).fetchone()[0]
    return (case.workflow.get_task(case.task["uuid"]), case.workflow.list_jobs(case.task["uuid"]),
            case.workflow.get_station_workflow_result(case.task["uuid"]), outbox, recorded)


def test_recorded_cancel_settles_only_local_responsibility_and_keeps_history(tmp_path, inventory):
    with historical_case(tmp_path, inventory) as case:
        before, args = history(case), arguments(case)
        assert case.client.review_requests == [] and release_events(case) == []
        result = settle(case, args)
        assert result["completed"] is True and result["resources_released"] is True
        assert result["center_terminal_corrected"] is False
        assert result["kind"] == "recorded_cancellation_reconciliation"
        assert "release_receipt" not in result
        assert result["inventory_release_proof"]
        for field in ("evidence_request", "release_evidence_request"):
            assert result[field]["receipt"]["resolution"] == {"kind": "record_evidence"}
            assert result[field]["receipt"]["execution_authorized"] is False
        assert history(case) == before
        assert case.client.snapshot["state"]["execution_status"] == "canceled"
        assert len(case.client.snapshot["history"]) == 2
        assert case.client.settlements == []
        assert len(release_events(case)) == 1
        assert settle(case, args) == result
        assert len(case.client.review_requests) == 2 and len(release_events(case)) == 1
        assert history(case) == before


@pytest.mark.parametrize("change", [
    "owner", "task", "package", "event_uuid", "event_digest", "result_digest", "epoch",
    "not_applied", "recovery", "conflict", "result_revision",
])
def test_center_identity_and_history_conflicts_never_release(tmp_path, inventory, change):
    with historical_case(tmp_path, inventory) as case:
        snap = case.client.snapshot
        if change == "owner":
            snap["state"]["owner_instance_uuid"] = str(uuid4())
        elif change == "task":
            snap["state"]["local_task_uuid"] = str(uuid4())
        elif change == "package":
            snap["payload_digest"] = "sha256:" + "a" * 64
        elif change == "event_uuid":
            snap["result_events"][0]["event_uuid"] = str(uuid4())
        elif change == "event_digest":
            snap["event_receipts"][0]["event_digest"] = "sha256:" + "b" * 64
        elif change == "result_digest":
            snap["result_events"][0]["result_digest"] = "sha256:" + "c" * 64
        elif change == "epoch":
            snap["event_receipts"][0]["event_epoch_uuid"] = str(uuid4())
        elif change == "not_applied":
            snap["event_receipts"][0]["application"] = "conflict"
        elif change == "recovery":
            snap["recovery_blocked"] = True
        elif change == "conflict":
            snap["conflicts"].append({"event_uuid": str(uuid4())})
        else:
            snap["result_revision"] = snap["state"]["result_revision"] = 2
        case.client.refresh()
        with pytest.raises(ValueError):
            settle(case)
        assert release_events(case) == [] and case.client.review_requests == []


@pytest.mark.parametrize("scope", ["task", "invocation"])
def test_local_recovery_protection_blocks_history_settlement(tmp_path, inventory, scope):
    with historical_case(tmp_path, inventory) as case:
        with case.workflow.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS center_recovery_holds "
                "(subject_kind TEXT,subject_uuid TEXT,revision BIGINT,PRIMARY KEY(subject_kind,subject_uuid))"
            )
            connection.execute("INSERT INTO center_recovery_holds VALUES (?,?,1)",
                               (scope, case.task["uuid"] if scope == "task" else case.invocation))
        with pytest.raises((ValueError, RuntimeError)):
            settle(case)
        assert release_events(case) == [] and case.client.review_requests == []


@pytest.mark.parametrize("field", ["expected_local_facts_digest", "expected_center_evidence_digest"])
def test_stale_inspection_never_submits_evidence_or_releases(tmp_path, inventory, field):
    with historical_case(tmp_path, inventory) as case:
        args = arguments(case)
        args[field] = "sha256:" + "f" * 64
        with pytest.raises(ValueError):
            settle(case, args)
        assert release_events(case) == [] and case.client.review_requests == []


def test_original_output_checked_even_without_remaining_reservations(tmp_path, inventory):
    with historical_case(tmp_path, inventory) as case:
        with case.inventory.transaction() as connection:
            connection.execute("UPDATE inventory_reservation SET status='released' WHERE workflow_id IN (?,?)",
                               (case.invocation, case.task["uuid"]))
            connection.execute("UPDATE inventory_material_source_binding SET status='released' WHERE workflow_id=?",
                               (case.task["uuid"],))
            connection.execute("UPDATE material SET inventory_status='in_use' WHERE uuid=?", (case.local,))
        with pytest.raises(ValueError):
            settle(case)
        assert release_events(case) == []


@pytest.mark.parametrize("which_ack", [1, 2])
def test_lost_history_ack_reuses_request_and_cannot_release_later_work(tmp_path, inventory, which_ack):
    with historical_case(tmp_path, inventory) as case:
        args, before = arguments(case), history(case)
        case.client.lose_on_commit = which_ack
        with pytest.raises(CenterTransportError):
            settle(case, args)
        later = admit_later_task(case) if which_ack == 2 else None
        events = release_events(case)
        result = settle(case, args)
        assert result["completed"] is True
        repeated = [item for item in case.client.review_requests
                    if item[0] == case.client.review_requests[which_ack - 1][0]]
        assert len(repeated) == 2 and repeated[0] == repeated[1]
        assert len(case.client.review_receipts) == 2
        if later:
            assert release_events(case) == events
            assert case.inventory.query_all(
                "SELECT * FROM inventory_material_source_binding WHERE workflow_id=?", (later[0]["workflow_id"],)
            ) == later
        assert history(case) == before


def test_inventory_commit_before_history_save_replays_original_proof(tmp_path, inventory, monkeypatch):
    with historical_case(tmp_path, inventory) as case:
        args, before = arguments(case), history(case)
        save = module._save

        def fail_after_inventory(workflow, invocation, value):
            if value["resources_released"]:
                raise RuntimeError("inventory committed before historical state")
            return save(workflow, invocation, value)

        with monkeypatch.context() as patch:
            patch.setattr(module, "_save", fail_after_inventory)
            with pytest.raises(RuntimeError, match="inventory committed"):
                settle(case, args)
        events = release_events(case)
        assert len(events) == 1 and state(case)["resources_released"] is False
        later = admit_later_task(case)
        assert settle(case, args)["completed"] is True
        assert release_events(case) == events
        assert case.inventory.query_all(
            "SELECT * FROM inventory_material_source_binding WHERE workflow_id=?", (later[0]["workflow_id"],)
        ) == later
        assert history(case) == before


@pytest.mark.parametrize("change", [
    "no_cancel", "not_successful", "cleanup", "not_acknowledged", "ack_conflict", "unrecorded",
])
def test_incomplete_original_local_facts_never_submit_or_release(tmp_path, inventory, change):
    with historical_case(tmp_path, inventory) as case:
        with case.workflow.transaction() as connection:
            if change == "no_cancel":
                connection.execute("DELETE FROM center_command_inbox WHERE "
                                   "json_extract(envelope_json,'$.command.type')='task.cancel'")
            elif change == "not_successful":
                connection.execute("UPDATE workflow_task SET status='canceled' WHERE uuid=?", (case.task["uuid"],))
            elif change == "cleanup":
                connection.execute("UPDATE workflow_task SET cleanup_status='required' WHERE uuid=?",
                                   (case.task["uuid"],))
            elif change == "not_acknowledged":
                connection.execute("UPDATE center_boundary_outbox SET acknowledged=0 WHERE event_uuid=?",
                                   (case.event["event_uuid"],))
            elif change == "ack_conflict":
                connection.execute("UPDATE center_boundary_outbox SET application='conflict' WHERE event_uuid=?",
                                   (case.event["event_uuid"],))
            else:
                connection.execute("UPDATE center_invocation_inbox SET terminal_recorded=0 WHERE invocation_uuid=?",
                                   (case.invocation,))
        with pytest.raises(ValueError):
            settle(case)
        assert release_events(case) == [] and case.client.review_requests == []


@pytest.mark.parametrize("field", ["material_refs", "container_refs", "handoff_uuids", "local_input_hold"])
def test_formal_input_responsibility_cannot_use_historical_release(tmp_path, inventory, field):
    with historical_case(tmp_path, inventory) as case:
        with case.workflow.transaction() as connection:
            row = connection.execute("SELECT package_json FROM center_invocation_inbox WHERE invocation_uuid=?",
                                     (case.invocation,)).fetchone()
            body = json.loads(row[0])
            refs = {
                "material_refs": [{"station_uuid": STATION, "local_material_uuid": case.local,
                                   "local_revision": 1, "material_lineage_uuid": str(uuid4()),
                                   "handoff_item_uuid": str(uuid4())}],
                "container_refs": [{"container_uuid": str(uuid4()), "state_version": 1}],
                "handoff_uuids": [str(uuid4())],
            }
            if field in refs:
                body["payload"][field] = refs[field]
                body["payload_digest"] = digest(body["payload"])
            validate_task_package(body)
            connection.execute("UPDATE center_invocation_inbox SET package_json=? WHERE invocation_uuid=?",
                               (json.dumps(body), case.invocation))
            # Corrupt-history fixture still uses coherent package/event digests and a valid fixed-task receipt.
            for row in connection.execute("SELECT event_uuid,event_json FROM center_boundary_outbox "
                                          "WHERE invocation_uuid=?", (case.invocation,)).fetchall():
                event = json.loads(row["event_json"])
                event["payload"]["task_payload_digest"] = body["payload_digest"]
                event["event_digest"] = digest({k: v for k, v in event.items() if k != "event_digest"})
                validate_event(event)
                connection.execute("UPDATE center_boundary_outbox SET event_json=? WHERE event_uuid=?",
                                   (json.dumps(event), row["event_uuid"]))
            receipt = {"station_invocation_uuid": case.invocation, "task_payload_digest": body["payload_digest"],
                       "local_task_uuid": case.task["uuid"], "resources_digest": digest([case.local])}
            validate_input_receipt(receipt, body)
            connection.execute("INSERT INTO center_material_input VALUES (?,?,?,'released')",
                               (case.invocation, case.task["uuid"], json.dumps(receipt)))
        with pytest.raises(ValueError, match="跨站物料|输入占用责任"):
            settle(case)
        assert release_events(case) == [] and case.client.review_requests == []


@pytest.mark.parametrize("values", [
    {"reason": ""}, {"reason": " \t"}, {"evidence_refs": []}, {"evidence_refs": [" "]},
])
def test_reason_and_explicit_evidence_are_required(tmp_path, inventory, values):
    with historical_case(tmp_path, inventory) as case:
        with pytest.raises(ValueError):
            settle(case, **values)
        assert state(case) is None and release_events(case) == [] and case.client.review_requests == []


def test_actual_output_change_after_inspection_requires_new_review(tmp_path, inventory):
    with historical_case(tmp_path, inventory) as case:
        args = arguments(case)
        before = history(case)
        changed = {**before[2], "operator_note": "new evidence"}
        with case.workflow.transaction() as connection:
            connection.execute("UPDATE workflow_result SET result=? WHERE workflow_task_uuid=?",
                               (json.dumps(changed), case.task["uuid"]))
        with pytest.raises(ValueError, match="摘要"):
            settle(case, args)
        assert release_events(case) == [] and case.client.review_requests == []


def test_changed_original_facts_after_evidence_ack_prevent_release(tmp_path, inventory, monkeypatch):
    with historical_case(tmp_path, inventory) as case:
        args = arguments(case)
        original = case.client.reconcile

        def acknowledge_then_change(*positional, **keywords):
            receipt = original(*positional, **keywords)
            with case.workflow.transaction() as connection:
                connection.execute("UPDATE workflow_task SET cleanup_status='required' WHERE uuid=?",
                                   (case.task["uuid"],))
            return receipt

        monkeypatch.setattr(case.client, "reconcile", acknowledge_then_change)
        with pytest.raises(ValueError):
            settle(case, args)
        assert release_events(case) == [] and len(case.client.review_requests) == 1
        assert state(case)["resources_released"] is False


@pytest.mark.parametrize("field", ["reason", "evidence_refs"])
def test_recovery_cannot_replace_frozen_operator_evidence(tmp_path, inventory, field):
    with historical_case(tmp_path, inventory) as case:
        args = arguments(case)
        case.client.lose_on_commit = 1
        with pytest.raises(CenterTransportError):
            settle(case, args)
        args[field] = "另一原因" if field == "reason" else ["scan://different-carrier"]
        with pytest.raises(ValueError, match="原因和证据"):
            settle(case, args)
        assert release_events(case) == [] and len(case.client.review_requests) == 1


def test_completed_record_requires_distinct_precise_release_evidence(tmp_path, inventory):
    with historical_case(tmp_path, inventory) as case:
        result = settle(case)
        assert module.reconciliation_completed(result)
        changed = deepcopy(result)
        changed["release_evidence_request"] = deepcopy(changed["evidence_request"])
        assert not module.reconciliation_completed(changed)


def test_first_center_ack_cannot_be_saved_then_restarted_with_a_new_request(tmp_path, inventory, monkeypatch):
    with historical_case(tmp_path, inventory) as case:
        args, before = arguments(case), history(case)
        save = module._save

        def crash_after_ack(workflow, invocation, value):
            if value["evidence_request"] and value["evidence_request"]["receipt"]:
                raise RuntimeError("first receipt received before save")
            return save(workflow, invocation, value)

        with monkeypatch.context() as patch:
            patch.setattr(module, "_save", crash_after_ack)
            with pytest.raises(RuntimeError, match="first receipt"):
                settle(case, args)
        assert state(case)["evidence_request"]["receipt"] is None and release_events(case) == []
        result = settle(case, args)
        assert result["completed"] and len(case.client.review_receipts) == 2
        assert case.client.review_requests[0] == case.client.review_requests[1]
        assert history(case) == before


@pytest.mark.parametrize("phase", [1, 2])
def test_explicit_stale_rejection_archives_old_request_and_uses_fresh_snapshot(
    tmp_path, inventory, monkeypatch, phase,
):
    with historical_case(tmp_path, inventory) as case:
        args, before = arguments(case), history(case)
        original = case.client.reconcile
        rejected = []

        def stale_once(invocation, body, *, idempotency_key, event_epoch_uuid):
            if len(case.client.review_receipts) + 1 == phase and not rejected:
                attempt = (idempotency_key, deepcopy(body), event_epoch_uuid)
                rejected.append(attempt)
                case.client.review_requests.append(attempt)
                case.client.snapshot["state_revision"] += 1
                case.client.refresh()
                raise CenterTransportError("concurrent evidence changed", status_code=409,
                                           code="RECONCILIATION_SNAPSHOT_STALE")
            return original(invocation, body, idempotency_key=idempotency_key, event_epoch_uuid=event_epoch_uuid)

        monkeypatch.setattr(case.client, "reconcile", stale_once)
        result = settle(case, args)
        work = result["evidence_request" if phase == 1 else "release_evidence_request"]
        archived = work["superseded_requests"]
        assert len(archived) == 1 and archived[0]["receipt"] is None
        assert archived[0]["request_uuid"] == rejected[0][0] and archived[0]["body"] == rejected[0][1]
        assert archived[0]["error"]["status_code"] == 409
        assert work["request_uuid"] != rejected[0][0]
        assert work["body"]["expected_evidence_digest"] != rejected[0][1]["expected_evidence_digest"]
        assert result["completed"] and module.reconciliation_completed(result)
        assert len(case.client.review_requests) == 3 and len(case.client.review_receipts) == 2
        assert len(release_events(case)) == 1 and history(case) == before
        assert settle(case, args) == result and len(case.client.review_requests) == 3


@pytest.mark.parametrize("status,code", [
    (500, "RECONCILIATION_SNAPSHOT_STALE"), (409, "UNRELATED_CONFLICT"), (None, None),
])
def test_uncertain_failure_never_replaces_the_original_evidence_request(
    tmp_path, inventory, monkeypatch, status, code,
):
    with historical_case(tmp_path, inventory) as case:
        args = arguments(case)
        original = case.client.reconcile
        attempted = []

        def uncertain_once(invocation, body, *, idempotency_key, event_epoch_uuid):
            if not attempted:
                attempted.append((idempotency_key, deepcopy(body), event_epoch_uuid))
                case.client.review_requests.append(attempted[0])
                raise CenterTransportError("ambiguous response", status_code=status, code=code, retryable=True)
            return original(invocation, body, idempotency_key=idempotency_key, event_epoch_uuid=event_epoch_uuid)

        monkeypatch.setattr(case.client, "reconcile", uncertain_once)
        with pytest.raises(CenterTransportError):
            settle(case, args)
        assert release_events(case) == []
        assert not state(case)["evidence_request"].get("superseded_requests")
        assert settle(case, args)["completed"]
        assert case.client.review_requests[0] == case.client.review_requests[1]


@pytest.mark.parametrize("phase", [1, 2])
def test_stale_retry_cannot_continue_after_center_conflict(tmp_path, inventory, monkeypatch, phase):
    with historical_case(tmp_path, inventory) as case:
        args = arguments(case)
        original = case.client.reconcile

        def conflict_before_retry(invocation, body, *, idempotency_key, event_epoch_uuid):
            if len(case.client.review_receipts) + 1 == phase:
                case.client.review_requests.append((idempotency_key, deepcopy(body), event_epoch_uuid))
                case.client.snapshot["conflicts"] = [{"event_uuid": str(uuid4())}]
                case.client.snapshot["state_revision"] += 1
                case.client.refresh()
                raise CenterTransportError("conflict arrived", status_code=409,
                                           code="RECONCILIATION_SNAPSHOT_STALE")
            return original(invocation, body, idempotency_key=idempotency_key, event_epoch_uuid=event_epoch_uuid)

        monkeypatch.setattr(case.client, "reconcile", conflict_before_retry)
        with pytest.raises(ValueError, match="冲突"):
            settle(case, args)
        assert len(case.client.review_requests) == phase
        assert len(release_events(case)) == phase - 1
        assert state(case)["completed"] is False
        work = state(case)["evidence_request" if phase == 1 else "release_evidence_request"]
        assert not work.get("superseded_requests") and work["receipt"] is None


def test_repeated_stale_rejection_is_bounded_and_next_attempt_uses_persisted_request(
    tmp_path, inventory, monkeypatch,
):
    with historical_case(tmp_path, inventory) as case:
        args = arguments(case)
        original = case.client.reconcile
        rejected = []

        def three_stale_responses(invocation, body, *, idempotency_key, event_epoch_uuid):
            if len(rejected) < 3:
                rejected.append((idempotency_key, deepcopy(body), event_epoch_uuid))
                case.client.review_requests.append(rejected[-1])
                case.client.snapshot["state_revision"] += 1
                case.client.refresh()
                raise CenterTransportError("concurrent review", status_code=409,
                                           code="RECONCILIATION_SNAPSHOT_STALE")
            return original(invocation, body, idempotency_key=idempotency_key, event_epoch_uuid=event_epoch_uuid)

        monkeypatch.setattr(case.client, "reconcile", three_stale_responses)
        with pytest.raises(CenterTransportError):
            settle(case, args)
        assert len(case.client.review_requests) == 3 and release_events(case) == []
        pending = state(case)["evidence_request"]
        assert len(pending["superseded_requests"]) == 3 and pending["receipt"] is None
        assert pending["request_uuid"] not in {item[0] for item in rejected}
        assert settle(case, args)["completed"]
        assert case.client.review_requests[3][:2] == (pending["request_uuid"], pending["body"])


@pytest.mark.parametrize("fault", ["execution_authorized", "wrong_actor"])
def test_invalid_center_evidence_receipt_never_authorizes_inventory_release(
    tmp_path, inventory, monkeypatch, fault,
):
    with historical_case(tmp_path, inventory) as case:
        args = arguments(case)
        original = case.client.reconcile

        def invalid_receipt(*positional, **keywords):
            receipt = original(*positional, **keywords)
            if fault == "execution_authorized":
                receipt["execution_authorized"] = True
            else:
                receipt["snapshot"]["history"][-1]["actor_uuid"] = str(uuid4())
                receipt["snapshot"]["evidence_digest"] = digest({
                    k: v for k, v in receipt["snapshot"].items() if k != "evidence_digest"
                })
            return receipt

        monkeypatch.setattr(case.client, "reconcile", invalid_receipt)
        with pytest.raises(ValueError):
            settle(case, args)
        assert release_events(case) == [] and len(case.client.review_requests) == 1
        assert state(case)["evidence_request"]["receipt"] is None
