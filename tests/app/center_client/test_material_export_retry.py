"""补报失败必须持久、有界，重试只补原证据而不重新执行任务。"""
from __future__ import annotations

import json
import logging

import pytest

from tests.app.center_client import test_material_outcomes as outcomes
from tests.app.center_client.test_atomic_workflow_events import _events
from tests.app.center_client.test_center_client import INSTANCE, STATION
from tests.app.center_client.test_material_exports import Allocator, bind, complete, inventory, uid
from unilabos.app.center_client.client import SessionFencedError
from unilabos.app.center_client.material import MaterialBridge
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
from unilabos.app.scheduler.inventory.store import InventoryStore


def authority(inv, durable, clock):
    return InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE,
        event_epoch_uuid=durable.event_epoch_uuid, now_ms=lambda: clock[0])


def pump_cycle(bridge):
    # 空页使原有分批游标回绕；不等待墙上时钟。
    bridge.pump()
    bridge.pump()


def test_produced_container_registration_replays_original_request_after_lost_response(tmp_path, inventory):
    from unilabos.app.scheduler.inventory.handoff import StationHandoffAuthority

    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "runtime product", "barcode": "PRODUCED-1",
        "meta_data": {"runtime_production": {"evidence_job_uuid": uid(), "evidence_refs": ["device-production://one"], "simulation": False}}})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        jobs = store.list_jobs(task["uuid"])
        a = authority(inv, durable, clock)
        handoffs = StationHandoffAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE,
            event_epoch_uuid=durable.event_epoch_uuid, lab_uuid=uid())

        class RegistrationClient(Allocator):
            def __init__(self):
                super().__init__()
                self.registrations = []
            def register_container(self, request, epoch):
                self.registrations.append(request)
                self.containers[request["container_uuid"]] = {
                    **{key: request[key] for key in ("container_uuid", "barcode_namespace", "barcode")},
                    "lab_uuid": handoffs.lab_uuid, "current_station_uuid": STATION, "state_version": 1,
                    "active_handoff_item_uuid": None, "custody_status": "available", "attention": False}
                if len(self.registrations) == 1:
                    raise CenterTransportError("产物容器登记回执丢失", retryable=True)

        client = RegistrationClient()
        pump_cycle(MaterialBridge(client, store, a, handoff_authority=handoffs))
        assert a.export_failure(identity, "prepare")["status"] == "retrying"
        assert len(client.registrations) == 1
        restored = authority(inv, durable, clock)
        clock[0] = 12_000
        pump_cycle(MaterialBridge(client, store, restored, handoff_authority=handoffs))
        assert client.registrations[0] == client.registrations[1]
        assert restored.export_failure(identity, "prepare") is None
        assert restored.completed_boundaries()[0]["invocation_uuid"] == identity
        assert store.list_jobs(task["uuid"]) == jobs


def test_prepare_failure_survives_restart_and_stops_busy_retry(tmp_path, inventory, caplog):
    inv, backend, _ = inventory
    wrong = backend.sync_resource_templates([
        {"id": "test.wrong", "display_name": "错误模板", "registry_type": "material", "class": {}}
    ])["templates"][0]["uuid"]
    local = backend.create_material({"resource_template_uuid": wrong, "name": "wrong output"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, scheduler, identity):
        complete(store, task, local, inventory_store=inv)
        jobs, submitted = store.list_jobs(task["uuid"]), list(scheduler.submissions)
        client = Allocator()
        a = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE,
            event_epoch_uuid=durable.event_epoch_uuid)
        with caplog.at_level(logging.WARNING):
            bridge = MaterialBridge(client, store, a)
            pump_cycle(bridge)
        assert "实际库存模板与冻结输出端口不匹配" in caplog.text
        assert a.pending_intents() == []
        saved = inv.query_one("SELECT * FROM station_material_export WHERE invocation_uuid=?", (identity,))
        assert saved is not None and "模板" in saved["last_error"]
        assert json.loads(saved["request_json"]) is None
        assert json.loads(saved["snapshot_json"]) is None
        assert saved["completed"] == 0
        assert not inv.db.table_exists("station_material_export_failure")
        # 尚未准备的导出尝试和失败原因保存在同一行
        reopened = InventoryStore(inv.path)
        try:
            restored = authority(reopened, durable, clock)
            failure = restored.export_failure(identity, "prepare")
            assert failure["status"] == "blocked"
            assert failure["attempts"] == 1
            assert failure["local_task_uuid"] == task["uuid"]
            assert failure["error_type"] == "MaterialAllocationError"
            assert "模板" in failure["last_error"]
            clock[0] += 86_400_000
            pump_cycle(MaterialBridge(client, store, restored))
            assert restored.export_failure(identity, "prepare")["attempts"] == 1
            assert client.requests == []
            assert store.get_task(task["uuid"])["status"] == "succeeded"
            assert store.list_jobs(task["uuid"]) == jobs and scheduler.submissions == submitted
            assert len(_events(store)) == 1
        finally:
            reopened.close()


def test_transient_failure_backoff_survives_restart_and_keeps_frozen_request(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        jobs = store.list_jobs(task["uuid"])
        a = authority(inv, durable, clock)
        class OfflineOnce(Allocator):
            def allocate_material_lineages(self, invocation, request, epoch):
                response = super().allocate_material_lineages(invocation, request, epoch)
                if len(self.requests) == 1:
                    raise CenterTransportError("中心回执丢失", retryable=True)
                return response
        client = OfflineOnce()
        handoffs = bind(a, client, local)
        bridge = MaterialBridge(client, store, a, handoff_authority=handoffs)
        pump_cycle(bridge)
        failure = a.export_failure(identity, "evidence")
        assert failure["status"] == "retrying" and failure["attempts"] == 1
        assert failure["next_attempt_ms"] == 12_000
        restored = authority(inv, durable, clock)
        bridge = MaterialBridge(client, store, restored, handoff_authority=handoffs)
        clock[0] = 11_999
        pump_cycle(bridge)
        assert len(client.requests) == 1
        clock[0] = 12_000
        pump_cycle(bridge)
        assert len(client.requests) == 2 and client.requests[0] == client.requests[1]
        assert restored.pending_intents() == []
        assert restored.export_failure(identity, "evidence") is None
        assert restored.completed_boundaries()[0]["invocation_uuid"] == identity
        assert len(_events(store)) == 2 and store.list_jobs(task["uuid"]) == jobs


@pytest.mark.parametrize("retryable,status_code", [(False, 409), (True, 503)])
def test_blocked_or_exhausted_calls_require_explicit_resume(tmp_path, inventory, retryable, status_code):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        jobs = store.list_jobs(task["uuid"])
        a = authority(inv, durable, clock)
        class Unavailable(Allocator):
            unavailable = True
            def allocate_material_lineages(self, invocation, request, epoch):
                response = super().allocate_material_lineages(invocation, request, epoch)
                if self.unavailable:
                    raise CenterTransportError("需要核实" if not retryable else "服务暂不可用",
                        retryable=retryable, status_code=status_code, code="TEST_FAILURE")
                return response
        client = Unavailable()
        handoffs = bind(a, client, local)
        bridge = MaterialBridge(client, store, a, handoff_authority=handoffs)
        for _ in range(8):
            pump_cycle(bridge)
            clock[0] += 1_000_000
        failure = a.export_failure(identity, "evidence")
        assert failure["status"] == ("exhausted" if retryable else "blocked")
        assert failure["attempts"] == (5 if retryable else 1)
        assert len(client.requests) == failure["attempts"]
        assert failure["error_code"] == "TEST_FAILURE"
        client.unavailable = False
        pump_cycle(MaterialBridge(client, store, authority(inv, durable, clock), handoff_authority=handoffs))
        assert a.pending_intents()  # 重启和配置恢复都不能绕过持久暂停。
        a.resume_export(identity, "evidence")
        pump_cycle(bridge)
        assert a.pending_intents() == [] and a.export_failure(identity, "evidence") is None
        assert all(request == client.requests[0] for request in client.requests)
        assert len(_events(store)) == 2 and store.list_jobs(task["uuid"]) == jobs


@pytest.mark.parametrize("retry_after,status,next_attempt", [
    (60, "retrying", 70_000), (9_999_999_999_999_999, "blocked", None),
])
def test_retry_after_is_respected_without_blocking_other_pump_work(tmp_path, inventory, retry_after, status, next_attempt):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        jobs = store.list_jobs(task["uuid"])
        a = authority(inv, durable, clock)
        class Busy(Allocator):
            def allocate_material_lineages(self, invocation, request, epoch):
                super().allocate_material_lineages(invocation, request, epoch)
                raise CenterTransportError("请稍后", retryable=True, status_code=429, retry_after_seconds=retry_after)
        client = Busy()
        pump_cycle(MaterialBridge(client, store, a))
        failure = a.export_failure(identity, "evidence")
        assert failure["next_attempt_ms"] == next_attempt and failure["status"] == status
        clock[0] = 69_999
        pump_cycle(MaterialBridge(client, store, a))
        assert len(client.requests) == 1


def test_boundary_failure_has_own_budget_and_never_rewrites_outcome(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        jobs = store.list_jobs(task["uuid"])
        a = authority(inv, durable, clock)
        class BoundaryOffline(Allocator):
            def publish_material_boundary(self, invocation, request, epoch):
                receipt = super().publish_material_boundary(invocation, request, epoch)
                if len(self.boundary_requests) == 1:
                    raise CenterTransportError("边界回执丢失", retryable=True)
                return receipt
        client = BoundaryOffline()
        handoffs = bind(a, client, local)
        bridge = MaterialBridge(client, store, a, handoff_authority=handoffs)
        pump_cycle(bridge)
        assert a.export_failure(identity, "evidence") is None
        assert a.export_failure(identity, "boundary")["attempts"] == 1
        events, jobs = _events(store), store.list_jobs(task["uuid"])
        assert len(events) == 2 and a.pending_intents() == []
        clock[0] = 12_000
        pump_cycle(MaterialBridge(client, store, authority(inv, durable, clock), handoff_authority=handoffs))
        assert a.pending_boundaries() == [] and a.export_failure(identity, "boundary") is None
        assert client.boundary_requests[0] == client.boundary_requests[1]
        assert _events(store) == events and store.list_jobs(task["uuid"]) == jobs


def test_fenced_session_still_stops_runtime_instead_of_becoming_export_retry(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        jobs = store.list_jobs(task["uuid"])
        a = authority(inv, durable, clock)
        class Fenced(Allocator):
            def allocate_material_lineages(self, *args):
                raise SessionFencedError("会话已失效")
        with pytest.raises(SessionFencedError):
            MaterialBridge(Fenced(), store, a).pump()
        assert a.export_failure(identity, "evidence") is None


def test_blocked_prepare_does_not_starve_an_independent_success(tmp_path, inventory):
    from copy import deepcopy

    from unilabos.app.center_client.client import TaskPackage
    from unilabos.app.center_client.runtime import submission
    from unilabos.center_protocol.digest import digest

    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, service, durable, package, task, _, identity):
        complete(store, task, uid())  # 真实成功，但输出身份在库存中不存在。
        payload = deepcopy(dict(package.raw))
        second_id = uid()
        payload.update(station_invocation_uuid=second_id,
            invocation_key=payload["global_workflow_revision_uuid"] + ":" + uid())
        second = TaskPackage.from_response({"payload": payload, "payload_digest": digest(payload)})
        durable.remember_package(second)
        durable.confirm_claim(second_id)
        next_task = service.submit_station_workflow(**submission(second), center_invocation_uuid=second_id)
        complete(store, next_task, local, inventory_store=inv)
        a = authority(inv, durable, clock)
        client = Allocator()
        handoffs = bind(a, client, local)
        bridge = MaterialBridge(client, store, a, handoff_authority=handoffs)
        pump_cycle(bridge)
        assert a.export_failure(identity, "prepare")["status"] == "blocked"
        assert a.pending_intents() == []
        outcomes_sent = [event["station_invocation_uuid"] for event in _events(store) if event["event_type"] == "outcome"]
        assert outcomes_sent == [second_id]
        assert len(client.requests) == 1
        jobs = {t["uuid"]: store.list_jobs(t["uuid"]) for t in (task, next_task)}
        clock[0] += 86_400_000
        pump_cycle(bridge)
        assert a.export_failure(identity, "prepare")["attempts"] == 1
        assert {t["uuid"]: store.list_jobs(t["uuid"]) for t in (task, next_task)} == jobs


@pytest.mark.parametrize("phase", ["evidence", "boundary"])
def test_success_acknowledgment_clears_diagnostic_in_same_transaction(tmp_path, inventory, monkeypatch, phase):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        a = authority(inv, durable, clock)
        client = Allocator()
        handoffs = bind(a, client, local)
        a.prepare(store.list_center_material_exports()[0])
        a.record_failure(identity, "前次网络失败", phase=phase, local_task_uuid=task["uuid"], retryable=True)
        clock[0] = 12_000
        method = "acknowledge" if phase == "evidence" else "acknowledge_boundary"
        original = getattr(a, method)
        def crash_after_commit(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("模拟确认事务提交后立即崩溃")
        # 保留真实确认事务，只在返回桥之前模拟进程退出。
        with monkeypatch.context() as patch:
            patch.setattr(a, method, crash_after_commit)
            with pytest.raises(RuntimeError, match="立即崩溃"):
                MaterialBridge(client, store, a, handoff_authority=handoffs).pump()
        assert a.export_failure(identity, phase) is None
        events, jobs = _events(store), store.list_jobs(task["uuid"])
        restored = authority(inv, durable, clock)
        pump_cycle(MaterialBridge(client, store, restored, handoff_authority=handoffs))
        assert restored.export_failure(identity, phase) is None
        assert restored.pending_intents() == [] and restored.pending_boundaries() == []
        assert _events(store) == events and store.list_jobs(task["uuid"]) == jobs


def test_unprepared_attempt_cannot_accept_receipt_or_acknowledgment(tmp_path, inventory):
    from unilabos.center_protocol.material import MaterialAllocationError
    inv, _, _ = inventory
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, uid())
        a = authority(inv, durable, clock)
        MaterialBridge(Allocator(), store, a).pump()
        saved = inv.query_one("SELECT * FROM station_material_export WHERE invocation_uuid=?", (identity,))
        assert saved is not None
        with pytest.raises(MaterialAllocationError):
            a.remember_response(identity, {})
        with pytest.raises(MaterialAllocationError):
            a.acknowledge(identity)
        assert a.pending_intents() == [] and a.completed_boundaries() == []
        assert inv.query_one("SELECT completed FROM station_material_export WHERE invocation_uuid=?", (identity,))["completed"] == 0


def test_restart_after_attempt_creation_finishes_same_export(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        jobs = store.list_jobs(task["uuid"])
        a = authority(inv, durable, clock)
        a.begin_export(store.list_center_material_exports()[0])
        assert a.pending_intents() == [] and len(_events(store)) == 1
        client = Allocator()
        handoffs = bind(a, client, local)
        pump_cycle(MaterialBridge(client, store, authority(inv, durable, clock), handoff_authority=handoffs))
        assert len(client.requests) == 1 and len(_events(store)) == 2
        assert inv.query_one("SELECT completed FROM station_material_export WHERE invocation_uuid=?", (identity,))["completed"] == 1
        assert store.list_jobs(task["uuid"]) == jobs


def test_legacy_export_table_adds_only_retry_column_and_keeps_request(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        a = authority(inv, durable, clock)
        original = a.prepare(store.list_center_material_exports()[0])
        # 模拟已存在的旧导出表；原请求、快照和约束均保留。
        with inv.transaction() as c:
            c.execute("ALTER TABLE station_material_export DROP COLUMN retry_json")
        restored = authority(inv, durable, clock)
        pending = restored.pending_intents()[0]
        assert pending["request"] == original["request"] and pending["source"] == original["source"]
        assert restored.export_failure(identity, "evidence") is None
        assert not inv.db.table_exists("station_material_export_failure")
        client = Allocator()
        handoffs = bind(restored, client, local)
        pump_cycle(MaterialBridge(client, store, restored, handoff_authority=handoffs))
        assert restored.pending_intents() == [] and len(_events(store)) == 2


def test_origin_resolution_failure_is_saved_before_prepare_and_can_resume(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    clock = [10_000]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        jobs = store.list_jobs(task["uuid"])
        a = authority(inv, durable, clock)
        client = Allocator()
        handoffs = bind(a, client, local)
        def invalid_origin(source):
            raise ValueError("谱系来源配置尚未完成")
        bridge = MaterialBridge(client, store, a, handoff_authority=handoffs, origins=invalid_origin)
        pump_cycle(bridge)
        assert a.export_failure(identity, "prepare")["status"] == "blocked"
        assert a.pending_intents() == [] and client.requests == []
        a.resume_export(identity, "prepare")
        pump_cycle(MaterialBridge(client, store, a, handoff_authority=handoffs))
        assert a.export_failure(identity, "prepare") is None
        assert len(client.requests) == 1 and len(_events(store)) == 2
        assert store.list_jobs(task["uuid"]) == jobs
