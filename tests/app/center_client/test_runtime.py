"""持久收件箱及公共提交恢复：断网、重复、重启与真实结果。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import INSTANCE, STATION, config, event_ack, package_response
from unilabos.app.center_client.client import CenterClient, CenterClientError
from unilabos.app.center_client.digest import digest
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.runtime import CenterRuntime, validate_command_page
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.workflow.store import StoreNotFound


class Center:
    """真实客户端使用的协议端口；能够在提交成功后丢弃 ACK。"""

    def __init__(self) -> None:
        self.package = package_response()
        self.commands: list[dict[str, Any]] = []
        self.reports: list[dict[str, Any]] = []
        self.claims = 0
        self.ack_lost = False
        self.report_lost = False
        self.reject_control = False
        self.before_ack: Any = None
        self.session_uuid = str(uuid4())
        self.catalog_receipt = None
        self.catalog_chunks: dict[tuple[str, int], dict[str, Any]] = {}

    def add(self, *, canceled: bool = False) -> dict[str, Any]:
        payload = self.package["payload"]
        command = {
            "protocol_version": "1.0", "type": "task.available", "command_uuid": str(uuid4()),
            "command_sequence": len(self.commands) + 1, "station_uuid": STATION,
            "payload": {
                "station_invocation_uuid": payload["station_invocation_uuid"],
                "global_task_uuid": payload["global_task_uuid"],
                "task_package_path": f'/api/v1/station-invocations/{payload["station_invocation_uuid"]}/task-package',
                "payload_digest": self.package["payload_digest"],
            },
        }
        envelope = {"command": command, "command_digest": digest(command),
                    "disposition": "canceled" if canceled else "active", "disposition_revision": int(canceled)}
        self.commands.append(envelope)
        return envelope

    def add_cancel(self, *, reason: str = "用户取消") -> dict[str, Any]:
        payload = self.package["payload"]
        command = {
            "protocol_version": "1.0", "type": "task.cancel", "command_uuid": str(uuid4()),
            "command_sequence": len(self.commands) + 1, "station_uuid": STATION,
            "payload": {
                "station_invocation_uuid": payload["station_invocation_uuid"],
                "global_task_uuid": payload["global_task_uuid"],
                "reason": reason,
            },
        }
        envelope = {"command": command, "command_digest": digest(command),
                    "disposition": "active", "disposition_revision": 0}
        self.commands.append(envelope)
        return envelope

    def request(self, method: str, path: str, *, json_body=None, session_headers=None, expected_status=None, allow_null_response=False) -> dict[str, Any] | None:
        if path.endswith("/events"):
            assert expected_status == 202
            self.reports.append(deepcopy(json_body))
            if self.report_lost:
                self.report_lost = False
                raise CenterTransportError("断网", retryable=True)
            return event_ack(json_body)
        if self.reject_control:
            raise CenterTransportError("已失去控制权", code="STALE_SESSION", retryable=False)
        if path.endswith("/session"):
            return {"session_uuid": self.session_uuid, "session_epoch": 1,
                    "station_uuid": STATION, "instance_uuid": INSTANCE}
        if path.endswith("/workflow-catalog/current"):
            assert allow_null_response is True
            return deepcopy(self.catalog_receipt)
        if "/workflow-catalog/" in path and "/chunks/" in path:
            assert method == "PUT"
            self.catalog_chunks[(json_body["catalog_version"], json_body["chunk_index"])] = deepcopy(json_body)
            return {key: json_body[key] for key in ("station_uuid", "catalog_version", "chunk_index", "chunk_digest")}
        if "/workflow-catalog/" in path and path.endswith("/commit"):
            assert method == "POST"
            for index, expected in enumerate(json_body["chunk_digests"]):
                assert self.catalog_chunks[(json_body["catalog_version"], index)]["chunk_digest"] == expected
            self.catalog_receipt = {key: value for key, value in json_body.items()
                                    if key not in {"protocol_version", "chunk_digests"}}
            return deepcopy(self.catalog_receipt)
        if "/commands?" in path:
            cursor = int(path.split("after_sequence=")[1].split("&")[0])
            commands = [deepcopy(item) for item in self.commands if item["command"]["command_sequence"] > cursor]
            return {"commands": commands, "has_more": False,
                    "next_sequence": commands[-1]["command"]["command_sequence"] if commands else cursor}
        if path.endswith("/commands/ack"):
            if self.before_ack:
                self.before_ack(json_body)
            if self.ack_lost:
                self.ack_lost = False
                raise CenterTransportError("ACK 响应丢失", retryable=True)
            return {"acknowledged_sequence": json_body["command_sequence"]}
        if path.endswith("/task-package"):
            return deepcopy(self.package)
        if path.endswith("/claim"):
            self.claims += 1
            return {"invocation_uuid": self.package["payload"]["station_invocation_uuid"],
                    "owner_instance_uuid": INSTANCE, "replayed": self.claims > 1}
        raise AssertionError(path)


class Service:
    """仅替代领域端口以验证网络编排；数据库原子性使用独立真实服务验收。"""

    def __init__(self) -> None:
        self.task: dict[str, Any] | None = None
        self.calls: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None
        self.crash_after_commit = False
        self.store: DurableCenterStore | None = None
        self.package = None
        self.control_calls: list[dict[str, Any]] = []
        self.cancel_task_missing = False
        self.cancel_result: dict[str, Any] | None = None

    def install_center_material_source_authority(self, authority, session_provider):
        self.source_authority = authority
        self.source_session = session_provider

    def find_station_workflow_submission(self, **kwargs):
        if self.task is not None:
            assert kwargs == {key: value for key, value in self.calls[0].items()
                              if key != "center_invocation_uuid"}
        return deepcopy(self.task)

    def submit_station_workflow(self, **kwargs):
        self.calls.append(kwargs)
        assert self.task is None
        self.task = {"uuid": str(uuid4()), "status": "running", "output": {}, "error_info": []}
        self.package = next(package for package in self.store.packages()
                            if package.station_invocation_uuid == kwargs["center_invocation_uuid"])
        accepted_at = "2023-11-14T22:13:20.000Z"
        self.store.record_event(self.package.station_invocation_uuid, terminal=False, body={
            "local_task_uuid": self.task["uuid"], "event_uuid": str(uuid4()), "event_type": "accepted",
            "payload": {"task_payload_digest": self.package.payload_digest,
                        "accepted_at": accepted_at, "local_task_uuid": self.task["uuid"]},
            "occurred_at": accepted_at,
        })
        if self.crash_after_commit:
            self.crash_after_commit = False
            raise RuntimeError("模拟公共提交已提交、网络发送前崩溃")
        return deepcopy(self.task)

    def recover_center_workflow_invocation(self, invocation_uuid):
        assert self.package is None or invocation_uuid == self.package.station_invocation_uuid
        return deepcopy(self.task)

    def command_workflow_task(self, task_uuid: str, **kwargs):
        if self.cancel_task_missing:
            self.cancel_task_missing = False
            raise StoreNotFound(task_uuid)
        self.control_calls.append({"task_uuid": task_uuid, **kwargs})
        return self.cancel_result or {"uuid": str(uuid4()), "status": "succeeded"}

    def complete(self, status, *, result=None, error_info=None, occurred_at="2023-11-14T22:13:21.000Z"):
        """模拟领域事务已经提交的 outbox；轮询者不能负责从 Task 快照合成事件。"""
        self.task.update(status=status, error_info=error_info or [])
        self.result = result
        if status == "succeeded":
            assert result is not None
            output = result
        else:
            output = {"status": status, "error_info": self.task["error_info"]}
            if status == "timeout":
                output["failure_code"] = "timeout"
        self.store.record_event(self.package.station_invocation_uuid, terminal=True, body={
            "local_task_uuid": self.task["uuid"], "event_uuid": str(uuid4()), "event_type": "outcome",
            "payload": {"task_payload_digest": self.package.payload_digest,
                        "status": "failed" if status == "timeout" else status, "output": output},
            "occurred_at": occurred_at,
        })


def runtime(tmp_path, center, service):
    cfg = config(tmp_path)
    store = DurableCenterStore(tmp_path / "workflow_history.db", station_uuid=STATION, instance_uuid=INSTANCE)
    if isinstance(service, Service):
        service.store = store
    return CenterRuntime(cfg, CenterClient(cfg, center), store, service)


def test_inbox_commits_before_ack_and_ack_loss_recovers(tmp_path):
    center, service = Center(), Service()
    center.add()
    first = runtime(tmp_path, center, service)
    def verify_durable(_):
        peer = DurableCenterStore(tmp_path / "workflow_history.db", station_uuid=STATION, instance_uuid=INSTANCE)
        assert len(peer.commands()) == 1
        peer.close()
    center.before_ack = verify_durable
    center.ack_lost = True
    with pytest.raises(CenterTransportError):
        first.poll_once()
    assert not service.calls
    epoch, process = first.store.event_epoch_uuid, first.client.process_uuid
    first.close()
    second = runtime(tmp_path, center, service)
    assert second.store.event_epoch_uuid == epoch
    assert second.client.process_uuid != process
    second.poll_once()
    assert len(service.calls) == 1 and second.store.cursor() == 1
    second.close()


def test_duplicate_notifications_use_one_public_submission(tmp_path):
    center, service = Center(), Service()
    center.add()
    center.add()
    run = runtime(tmp_path, center, service)
    run.poll_once()
    assert len(service.calls) == 1 and center.claims == 1
    assert len(center.reports) == 1
    assert service.calls[0]["workflow_id"] == center.package["payload"]["workflow_uuid"]
    assert service.calls[0]["revision_fingerprint"] == center.package["payload"]["revision_fingerprint"]
    run.close()


@pytest.mark.parametrize("waiting_code", ["DEPENDENCY_NOT_READY", "RESULT_AMENDMENT_REVIEW_REQUIRED"])
def test_dependency_wait_keeps_original_call_and_processes_independent_command(tmp_path, waiting_code):
    center = Center()
    first = deepcopy(center.package)
    center.add()
    second = deepcopy(first)
    second["payload"]["station_invocation_uuid"] = str(uuid4())
    second["payload"]["invocation_key"] = second["payload"]["global_workflow_revision_uuid"] + ":" + str(uuid4())
    second["payload_digest"] = digest(second["payload"])
    center.package = second
    center.add()
    packages = {item["payload"]["station_invocation_uuid"]: item for item in (first, second)}
    services = {item["payload"]["invocation_key"]: Service() for item in (first, second)}
    blocked = True
    original = center.request

    def gated_request(method, path, **kwargs):
        if path.endswith("/task-package") or path.endswith("/claim"):
            invocation = path.split("/")[-2]
            center.package = packages[invocation]
            if path.endswith("/claim") and invocation == first["payload"]["station_invocation_uuid"] and blocked:
                raise CenterTransportError("上游证据待核实", code=waiting_code, retryable=False)
        return original(method, path, **kwargs)

    class MultipleTasks:
        def install_center_material_source_authority(self, authority, session_provider):
            for service in services.values():
                service.install_center_material_source_authority(authority, session_provider)

        def find_station_workflow_submission(self, **kwargs):
            return services[kwargs["invocation_key"]].find_station_workflow_submission(**kwargs)

        def submit_station_workflow(self, **kwargs):
            return services[kwargs["invocation_key"]].submit_station_workflow(**kwargs)

        def recover_center_workflow_invocation(self, invocation_uuid):
            key = packages[invocation_uuid]["payload"]["invocation_key"]
            return services[key].recover_center_workflow_invocation(invocation_uuid)

    center.request = gated_request
    run = runtime(tmp_path, center, MultipleTasks())
    for service in services.values():
        service.store = run.store
    first_service = services[first["payload"]["invocation_key"]]
    second_service = services[second["payload"]["invocation_key"]]
    try:
        run.poll_once()
        assert first_service.calls == []
        assert len(second_service.calls) == 1
        assert len(run.store.commands()) == 1
        assert not run.store.claim_confirmed(first["payload"]["station_invocation_uuid"])
        blocked = False
        run.poll_once()
        run.poll_once()
        assert len(first_service.calls) == len(second_service.calls) == 1
        assert first_service.calls[0]["center_invocation_uuid"] == first["payload"]["station_invocation_uuid"]
        assert run.store.commands() == []
        assert center.claims == 2
    finally:
        run.close()


def test_result_replay_keeps_exact_event_after_restart_and_fencing(tmp_path):
    center, service = Center(), Service()
    center.add()
    first = runtime(tmp_path, center, service)
    first.poll_once()
    service.complete("succeeded", result={"real_measurement": 42.5})
    center.report_lost = True
    with pytest.raises(CenterTransportError):
        first.poll_once()
    lost = deepcopy(center.reports[-1])
    assert lost["event_type"] == "outcome" and lost["payload"]["status"] == "succeeded" and lost["payload"]["output"] == service.result
    first.close()
    second = runtime(tmp_path, center, service)
    center.reject_control = True
    with pytest.raises(CenterTransportError):
        second.poll_once()
    assert center.reports[-1] == lost
    assert second.store.events() == []
    assert len(service.calls) == 1
    second.close()


def test_submission_committed_outbox_recovers_without_new_claim(tmp_path):
    center, service = Center(), Service()
    center.add()
    first = runtime(tmp_path, center, service)
    service.crash_after_commit = True
    with pytest.raises(RuntimeError, match="模拟公共提交"):
        first.poll_once()
    assert [event["event_type"] for _, event in first.store.events()] == ["accepted"]
    first.close()
    second = runtime(tmp_path, center, service)
    center.reject_control = True
    with pytest.raises(CenterTransportError):
        second.poll_once()
    assert len(service.calls) == 1 and center.claims == 1
    assert center.reports[-1]["event_type"] == "accepted"
    second.close()


def test_runtime_does_not_invent_outcome_from_task_snapshot(tmp_path):
    center, service = Center(), Service()
    center.add()
    run = runtime(tmp_path, center, service)
    run.poll_once()
    service.task["status"] = "succeeded"
    service.task["output"] = {"unverified": True}
    run.poll_once()
    assert [event["event_type"] for event in center.reports] == ["accepted"]
    service.complete("succeeded", result={})
    run.poll_once()
    assert center.reports[-1]["event_type"] == "outcome"
    assert center.reports[-1]["payload"]["output"] == {}
    run.close()


def test_canceled_command_tombstone_never_claims_or_submits(tmp_path):
    center, service = Center(), Service()
    center.add(canceled=True)
    run = runtime(tmp_path, center, service)
    run.poll_once()
    assert center.claims == 0 and not service.calls
    run.close()
    restart = runtime(tmp_path, center, service)
    restart.poll_once()
    assert restart.store.cursor() == 1 and not service.calls
    restart.close()


def test_task_cancel_command_calls_local_workflow_control(tmp_path):
    center, service = Center(), Service()
    center.add()
    run = runtime(tmp_path, center, service)
    run.poll_once()
    cancel = center.add_cancel()

    run._process(cancel)

    assert service.control_calls == [{
        "task_uuid": service.task["uuid"],
        "command_type": "cancel",
        "target_node_uuid": None,
        "idempotency_key": cancel["command"]["command_uuid"],
        "description": "用户取消",
        "meta_data": {"center_invocation_uuid": center.package["payload"]["station_invocation_uuid"]},
    }]
    run.close()


def test_task_cancel_waits_for_local_task_to_be_created(tmp_path):
    center, service = Center(), Service()
    cancel = center.add_cancel()
    run = runtime(tmp_path, center, service)
    run.poll_once()

    assert service.control_calls == []
    assert run.store.commands()
    center.add()
    run.poll_once()
    run.poll_once()
    assert len(service.control_calls) == 1
    assert run.store.commands() == []
    run.close()


def test_task_cancel_retries_when_local_scheduler_is_temporarily_unavailable(tmp_path):
    center, service = Center(), Service()
    center.add()
    run = runtime(tmp_path, center, service)
    run.poll_once()
    service.cancel_result = {
        "uuid": str(uuid4()),
        "status": "rejected",
        "result": {"reason": "scheduler_unavailable"},
    }
    cancel = center.add_cancel()

    run.poll_once()

    assert run.store.commands()
    service.cancel_result = None
    run.poll_once()
    assert run.store.commands() == []
    assert service.control_calls[-1]["idempotency_key"] == cancel["command"]["command_uuid"]
    run.close()


@pytest.mark.parametrize("change", ["digest", "sequence", "uuid", "station", "cursor", "boolean"])
def test_invalid_commands_never_acknowledge_or_execute(tmp_path, change):
    center, service = Center(), Service()
    envelope = center.add()
    if change == "digest":
        envelope["command_digest"] = "sha256:" + "0" * 64
    elif change == "sequence":
        envelope["command"]["command_sequence"] = 2
    elif change == "uuid":
        envelope["command"]["command_uuid"] = "invalid"
    elif change == "station":
        envelope["command"]["station_uuid"] = str(uuid4())
    elif change == "boolean":
        envelope["command"]["command_sequence"] = True
    if change != "digest":
        envelope["command_digest"] = digest(envelope["command"])
    if change == "cursor":
        with pytest.raises(CenterClientError):
            validate_command_page({"commands": [envelope], "has_more": False, "next_sequence": 2}, STATION, 0)
        return
    run = runtime(tmp_path, center, service)
    with pytest.raises(CenterClientError):
        run.poll_once()
    assert run.store.cursor() == 0 and not service.calls
    run.close()


def test_runtime_is_disabled_without_explicit_switch(monkeypatch):
    from unilabos.app.center_client.runtime import configured_runtime
    monkeypatch.delenv("CENTER_ENABLED", raising=False)
    assert configured_runtime("/must/not/be/opened", object()) is None


@pytest.mark.parametrize("code", ["WORKFLOW_REVISION_NOT_APPROVED", "RECOVERY_RECONCILIATION_REQUIRED"])
def test_rejected_claim_is_quarantined_without_blocking_later_commands(tmp_path, code):
    center, service = Center(), Service()
    first = center.add()
    center.add(canceled=True)
    run = runtime(tmp_path, center, service)
    original = center.request
    def reject_claim(method, path, **kwargs):
        if path.endswith("/claim"):
            raise CenterTransportError("调用不可领取", code=code)
        return original(method, path, **kwargs)
    center.request = reject_claim
    run.poll_once()
    assert not service.calls and run.store.commands() == []
    row = run.store._db.execute("SELECT error FROM center_command_inbox WHERE command_uuid=?",
                               (first["command"]["command_uuid"],)).fetchone()
    assert row[0] == code
    run.poll_once()
    assert not service.calls and not center.reports
    run.close()


def test_unavailable_local_contract_does_not_block_later_commands(tmp_path):
    from unilabos.workflow.service import WorkflowConflict
    center, service = Center(), Service()
    center.add()
    center.add(canceled=True)
    def reject(**kwargs):
        raise WorkflowConflict("workflow_revision_conflict")
    service.submit_station_workflow = reject
    run = runtime(tmp_path, center, service)
    run.poll_once()
    assert not service.calls and run.store.commands() == []
    assert len(center.reports) == 1 and center.reports[0]["event_type"] == "admission_failed"
    assert center.reports[0]["local_task_uuid"] is None
    run.close()


def test_timeout_uses_actual_failure_status_and_code(tmp_path):
    center, service = Center(), Service()
    center.add()
    run = runtime(tmp_path, center, service)
    run.poll_once()
    service.complete("timeout", error_info=[{"reason": "elapsed"}])
    run.poll_once()
    assert center.reports[-1]["event_type"] == "outcome"
    assert center.reports[-1]["payload"]["output"] == {
        "status": "timeout", "failure_code": "timeout", "error_info": [{"reason": "elapsed"}],
    }
    run.close()


def test_opt_in_composition_starts_after_publication_and_stops_before_store(tmp_path, monkeypatch):
    import threading

    from unilabos.workflow import composition
    composition.shutdown_workflow_runtime()
    calls = []
    started = threading.Event()
    class Runtime:
        def __init__(self, path, service):
            assert path == tmp_path / "workflow_history.db"
            self.service = service
            self.config = config(tmp_path)
            self.handoffs = None
        def start(self):
            assert composition.get_workflow_service() is self.service
            calls.append("start")
            started.set()
        def close(self):
            self.service.runtime_store._conn.execute("SELECT 1")
            calls.append("close")
    monkeypatch.setenv("CENTER_ENABLED", "1")
    monkeypatch.setenv("CENTER_INVENTORY_ENABLED", "0")
    monkeypatch.setattr("unilabos.app.center_client.runtime.configured_runtime", Runtime)
    monkeypatch.setattr("unilabos.app.center_client.connection.CenterConnection._connection_details",
                        lambda self, config: ({"http_reachability": "unknown"}, config))
    try:
        composition.compose_workflow_runtime(tmp_path, start_source_monitor=False)
        assert started.wait(2), "连接管理器应在身份检查完成后启动已发布的运行时"
        assert calls == ["start"]
    finally:
        composition.shutdown_workflow_runtime()
    assert calls == ["start", "close"]


def test_command_ack_larger_prefix_does_not_skip_local_records(tmp_path):
    center, service = Center(), Service()
    center.add(canceled=True)
    original = center.request
    def larger_prefix(method, path, **kwargs):
        if path.endswith("/commands/ack"):
            return {"acknowledged_sequence": 20}
        return original(method, path, **kwargs)
    center.request = larger_prefix
    run = runtime(tmp_path, center, service)
    run.poll_once()
    assert run.store.cursor() == 1
    run.close()


def test_actual_public_service_frozen_contract_and_result(tmp_path):
    """真实冻结合同→公共提交→边界结果→重启去重；不直接创建 Task。"""
    from tests.workflow.test_c1_published_workflow_contract import _applied_snapshot
    from unilabos.workflow.published_contract import PublishedWorkflowContractStore
    from unilabos.workflow.service import WorkflowService
    from unilabos.workflow.store import WorkflowStore

    path = tmp_path / "workflow_history.db"
    store = WorkflowStore(path)
    graph = _applied_snapshot()
    workflow_uuid = graph["workflow"]["uuid"]
    graph["workflow"]["revision"] = 1
    graph["workflow"]["meta_data"]["unilab"]["output_bindings"] = {
        "result": {"kind": "workflow_input", "parameter": "value"},
    }
    graph["nodes"][0].update(type="group", meta_data={})
    graph["node_templates"][0].update(type="group", node_type="group", name="group")
    graph["handle_templates"] = []
    store.create_workflow(workflow_uuid=workflow_uuid, name="Center boundary", tags=[],
                          description=None, meta_data={})
    published = PublishedWorkflowContractStore(store).publish(graph=graph, expected_revision=1)
    service = WorkflowService(store)
    center = Center()
    center.package["payload"]["workflow_uuid"] = workflow_uuid
    center.package["payload"]["revision_fingerprint"] = published["source_hash"]
    center.package["payload"]["input"] = {"value": 7.5}
    center.package["payload_digest"] = digest(center.package["payload"])
    center.add()
    run = None
    try:
        run = runtime(tmp_path, center, service)
        run.poll_once()
        assert [item["event_type"] for item in center.reports] == ["accepted", "outcome"]
        assert center.reports[-1]["payload"]["output"] == {"result": 7.5}
        first_uuid = center.reports[0]["local_task_uuid"]
        run.close()
        run = None
        service.close()
        store = WorkflowStore(path)
        service = WorkflowService(store)
        center.add()
        run = runtime(tmp_path, center, service)
        run.poll_once()
        assert center.claims == 1
        assert run.store.packages() == []
        assert store._conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 1
        assert service.get_station_workflow_result(first_uuid) == {"result": 7.5}
    finally:
        if run is not None:
            run.close()
        service.close()


@pytest.mark.parametrize("failure", ["network", "configuration"])
def test_handoff_failure_preserves_command_and_allows_independent_task(tmp_path, failure):
    center, service = Center(), Service()
    notification = center.add()
    notification["command"].update(type="handoff.available", payload={})
    notification["command_digest"] = digest(notification["command"])
    center.add()

    class Handoffs:
        ready = False

        def fail_if_pending(self):
            if not self.ready:
                if failure == "network":
                    raise CenterTransportError("交接重传断网", retryable=True)
                raise ValueError("尚缺本站接收模板")

        def pump(self):
            self.fail_if_pending()

        def handle_command(self, envelope):
            self.fail_if_pending()
            return True

    run = runtime(tmp_path, center, service)
    run.handoffs = Handoffs()
    try:
        run.poll_once()
        assert len(service.calls) == 1
        assert [entry["command"]["command_uuid"] for entry in run.store.commands()] == [
            notification["command"]["command_uuid"]]
        run.handoffs.ready = True
        run.poll_once()
        assert run.store.commands() == []
        assert len(service.calls) == 1
    finally:
        run.close()


def test_handoff_fencing_still_stops_new_control_work(tmp_path):
    from unilabos.app.center_client.client import SessionFencedError

    center, service = Center(), Service()
    center.add()

    class Handoffs:
        def pump(self):
            raise SessionFencedError("控制权已被接管")

    run = runtime(tmp_path, center, service)
    run.handoffs = Handoffs()
    try:
        with pytest.raises(SessionFencedError):
            run.poll_once()
        assert not service.calls
        assert center.claims == 0
    finally:
        run.close()
