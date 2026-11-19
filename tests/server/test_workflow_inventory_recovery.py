"""真实双 SQLite 库故障注入：提交应答丢失、重启与人工核实不重复扣库存。"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.client.materials import LocalMaterialsClient
from unilabos.protocol.materials import InventoryRequirement, InventoryReservationCreate, InventoryReservationTransition
from unilabos.registry.workflows import upsert_workflow
from unilabos.server.inventory import ExecutionInventoryCoordinator
from unilabos.client.host.execution_start import ExecutionStartGuard, ExecutionStartUnknown
from unilabos.server.scheduler.inventory_recovery import InventoryRecovery
from unilabos.server.scheduler.service import BackendScheduler
from unilabos.server.scheduler.payloads import build_job_start_payload
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.runtime.workflow.service import WorkflowService
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from tests.server.test_scheduler_inventory_authority import _template, _inbound, _mutation, _ListenerOnlyBackend
from tests.server.test_workflow_execution_claims import request
from tests.server.test_workflow_parallel_execution import Executor, wait_for


class InventoryDriver:
    def __init__(self, device_id: str = ""):
        self.callback = None

    def operate(self, solvent: dict) -> dict:
        return self.callback(solvent)


class Stack:
    def __init__(self, path):
        self.path = path
        self.workflow = WorkflowService(path / "runtime.db")
        self.materials = MaterialsService(path / "materials.db")
        self.client = LocalMaterialsClient(self.materials)
        self.claims = ExecutionClaims(self.workflow)
        self.epoch = self.claims.activate_authority()
        self.recovery = InventoryRecovery(self.workflow, self.client, self.epoch)

    def close(self):
        self.workflow.close()
        self.materials.close()

    def create(self, *, with_tenancy=False):
        _template(self.materials, "reagent-template", "reagent")
        _inbound(self.materials, lot_uuid="lot-a", quantity=20)
        requirement = InventoryRequirement(key="solvent", kind="lot", template_uuid="reagent-template", quantity=5, unit="ul")
        action = {"uuid": str(uuid4()), "name": "操作", "type": "device_action", "material_uuid": str(uuid4()),
                "action_name": "operate", "meta_data": {"target_device_id": "reactor", "inventory_requirements": [requirement.model_dump(mode="json")]},
                "param": {}}
        nodes, edges = [action], []
        if with_tenancy:
            load = {"uuid": str(uuid4()), "name": "装载", "type": "device_action", "material_uuid": str(uuid4()),
                    "action_name": "load", "meta_data": {"target_device_id": "robot"}, "param": {}}
            nodes.insert(0, load)
            action["execution_policy"] = {"depends_on": [load["uuid"]]}
        record = upsert_workflow(self.workflow, {"workflow_uuid": str(uuid4()), "name": "库存恢复", "description": "", "tags": [],
            "nodes": nodes, "edges": edges})
        task = self.workflow.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
            input_value={}, description=None, meta_data={})
        if with_tenancy:
            from tests.server.test_workflow_resource_custody import tenancy_request, finish
            from unilabos.server.services.runtime.workflow.resource_intervals import ResourceIntervals
            intervals = ResourceIntervals(self.workflow)
            loading = tenancy_request(self.workflow, task, 0, str(uuid4()), acquire="reactor")
            assert intervals.admit(loading, self.epoch)
            finish(self.workflow, intervals, loading)
        run = next(row for row in self.workflow.list_workflow_node_runs(task["uuid"]) if row["workflow_node_uuid"] == action["uuid"])
        job = run["current_job_uuid"]
        reservation = self.materials.reserve_inventory(_mutation("reserve_inventory", job_uuid=job),
            InventoryReservationCreate(task_uuid=task["uuid"], node_uuid=run["workflow_node_uuid"], job_uuid=job,
                scheduler_revision=1, requirements=[requirement])).data
        payload = build_job_start_payload(job, task["uuid"], task["workflow_uuid"], run["workflow_node_uuid"],
            "reactor", "operate", "", {}, node_run_uuid=run["uuid"], scheduler_revision=1,
            inventory_requirements=[requirement.model_dump(mode="json")], inventory_reservation_uuid=reservation.reservation_uuid)
        return task, job, payload

    def issue(self, task, job, payload):
        self.claims.reserve(request(task, job), authority_epoch=self.epoch)
        self.workflow.mark_workflow_node_job_running(job)
        payload["execution_permit"] = self.claims.seal_dispatch(job, payload, self.epoch)

    def finish(self, job, status):
        self.workflow.record_workflow_node_job_terminal(job, status=status, return_info={"return_value": {}}, error_info=[])

    def quantities(self):
        lot = self.materials.get_inventory_lot("lot-a")
        return (lot.quantity_total, lot.quantity_available, lot.quantity_reserved)


@pytest.fixture
def stack(tmp_path):
    value = Stack(tmp_path)
    yield value
    value.close()


def test_terminal_commit_survives_reopen_and_reconciles_once(tmp_path):
    first = Stack(tmp_path)
    task, job, payload = first.create()
    first.issue(task, job, payload)
    first.finish(job, "succeeded")
    first.close()  # runtime 成功已提交，materials 的结算还没有发生。
    reopened = Stack(tmp_path)
    try:
        report = reopened.recovery.reconcile()
        assert not report["issues"], report
        assert report["changed"][0]["status"] == "consumed"
        assert reopened.quantities() == (15, 15, 0)
        assert reopened.recovery.reconcile()["changed"] == []
        assert reopened.quantities() == (15, 15, 0)
    finally:
        reopened.close()


def test_consume_commit_lost_reply_keeps_unknown_and_never_calls_driver(tmp_path):
    first = Stack(tmp_path)
    task, job, payload = first.create()
    first.issue(task, job, payload)
    coordinator = ExecutionInventoryCoordinator(first.client)
    coordinator.prepare(payload)
    def lose_reply():
        coordinator.consume(job)
        raise ConnectionError("扣减已提交，网络应答丢失")
    physical_calls = []
    guard = ExecutionStartGuard(first.claims, payload, "host-1", lambda: False, lose_reply)
    with pytest.raises(ExecutionStartUnknown):
        guard()
        physical_calls.append(True)
    first.close()
    reopened = Stack(tmp_path)
    try:
        report = reopened.recovery.reconcile()
        assert not report["issues"], report
        assert report["held"] and not report["changed"]
        retry = ExecutionStartGuard(reopened.claims, payload, "host-2", lambda: False, lambda: physical_calls.append(True))
        with pytest.raises(ExecutionStartUnknown):
            retry()
        assert not physical_calls
        assert reopened.quantities() == (15, 15, 0)
        # 人工确认该次确已完成后，对账只认已有扣减。
        reopened.finish(job, "succeeded")
        assert not reopened.recovery.reconcile()["issues"]
        assert reopened.quantities() == (15, 15, 0)
    finally:
        reopened.close()


@pytest.mark.parametrize("authorized,consumed,expected,quantities", [
    (False, False, "released", (20,20,0)),
    (True, False, "quarantined", (15,15,0)),
    (True, True, "quarantined", (15,15,0)),
])
def test_terminal_failure_only_refunds_proven_unstarted(stack, authorized, consumed, expected, quantities):
    from unilabos.protocol.runtime.execution_start import execution_identity
    task, job, payload = stack.create()
    stack.issue(task, job, payload)
    if authorized:
        assert stack.claims.begin_execution(job, {"permit": payload["execution_permit"], "invocation_uuid": str(uuid4()),
            "executor_epoch": "host", "identity": execution_identity(payload)})["decision"] == "START"
    if consumed:
        coordinator = ExecutionInventoryCoordinator(stack.client)
        coordinator.prepare(payload)
        coordinator.consume(job)
    stack.finish(job, "canceled")
    report = stack.recovery.reconcile()
    assert not report["issues"], report
    assert stack.materials.get_inventory_reservation_by_job(job).status == expected
    assert stack.quantities() == quantities
    assert not stack.recovery.reconcile()["changed"]


def test_pending_cancel_releases_but_running_unknown_does_not(stack):
    task, job, payload = stack.create()
    assert not stack.recovery.reconcile()["changed"]
    stack.finish(job, "canceled")
    assert not stack.recovery.reconcile()["issues"]
    assert stack.quantities() == (20,20,0)


def test_recovery_write_and_read_reply_loss_retries_from_durable_facts(stack):
    task, job, payload = stack.create()
    stack.issue(task, job, payload)
    stack.finish(job, "succeeded")
    class FaultyGateway:
        committed = False
        def __getattr__(self, name):
            return getattr(stack.client, name)
        def consume_inventory_reservation(self, *args):
            result = stack.client.consume_inventory_reservation(*args)
            self.committed = True
            raise ConnectionError("提交成功但应答丢失")
        def get_inventory_reservation(self, key):
            if self.committed:
                raise ConnectionError("库存查询暂时不可用")
            return stack.client.get_inventory_reservation(key)
    failing = InventoryRecovery(stack.workflow, FaultyGateway(), stack.epoch)
    assert failing.reconcile()["issues"]
    assert stack.quantities() == (15,15,0)
    assert not stack.recovery.reconcile()["issues"]
    assert stack.quantities() == (15,15,0)


def test_identity_mismatch_preserves_inventory(stack):
    task, job, payload = stack.create()
    stack.finish(job, "succeeded")
    with stack.workflow.transaction() as conn:
        conn.execute("UPDATE workflow_task SET meta_data=? WHERE uuid=?", ('{"scheduler_revision":2}',task["uuid"]))
    assert stack.recovery.reconcile()["issues"]
    assert stack.quantities() == (20,15,5)


def test_success_cannot_reconsume_quarantined_inventory(stack):
    task, job, payload = stack.create()
    reservation = stack.materials.get_inventory_reservation_by_job(job)
    stack.materials.quarantine_inventory_reservation(_mutation("quarantine_inventory_reservation", job_uuid=job),
        InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid,reason="人工隔离"))
    stack.finish(job, "succeeded")
    assert stack.recovery.reconcile()["issues"]
    assert stack.quantities() == (15,15,0)


def test_genuine_success_conflict_still_pauses_dispatch(stack):
    """设备回执的成功遇到已隔离的库存仍是冲突：报告问题并暂停派发，库存不动。"""
    task, job, payload = stack.create()
    reservation = stack.materials.get_inventory_reservation_by_job(job)
    stack.materials.quarantine_inventory_reservation(_mutation("quarantine_inventory_reservation", job_uuid=job),
        InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid, reason="人工隔离"))
    stack.workflow.record_workflow_node_job_terminal(job, status="succeeded", error_info=[],
        return_info={"suc": True, "suc_type": "normal", "return_value": {}})
    scheduler = BackendScheduler(stack.workflow, _ListenerOnlyBackend(), materials_gateway=stack.client)
    try:
        assert "inventory_reconciliation" in scheduler.dispatch_pause_reasons
        assert scheduler.inventory_recovery_snapshot()["issues"][0]["job_uuid"] == job
        assert stack.quantities() == (15, 15, 0)
    finally:
        scheduler.stop()


@pytest.mark.parametrize("settled,quantities", [("quarantine", (15, 15, 0)), ("release", (20, 20, 0))])
def test_operator_success_keeps_the_failed_attempt_settlement(stack, settled, quantities):
    """人工替换为成功：失败时已到达的隔离 / 释放就是结算结果，不报冲突、不再改账。"""
    task, job, payload = stack.create()
    reservation = stack.materials.get_inventory_reservation_by_job(job)
    getattr(stack.materials, f"{settled}_inventory_reservation")(
        _mutation(f"{settled}_inventory_reservation", job_uuid=job),
        InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid, reason="action_failed"))
    stack.workflow.record_workflow_node_job_terminal(job, status="succeeded", error_info=[],
        return_info={"suc": True, "suc_type": "operator_intervention", "return_value": {}},
        error_resolution={"decision_id": "decision-1", "selected_action": "operator_intervention"})
    for _ in range(2):
        report = stack.recovery.reconcile()
        assert not report["issues"] and not report["changed"], report
    assert stack.materials.get_inventory_reservation_by_job(job).status == f"{settled}d"
    assert stack.quantities() == quantities


def test_operator_success_on_active_reservation_deducts_like_a_real_success(stack):
    task, job, payload = stack.create()
    stack.workflow.record_workflow_node_job_terminal(job, status="succeeded", error_info=[],
        return_info={"suc": True, "suc_type": "operator_intervention", "return_value": {}})
    report = stack.recovery.reconcile()
    assert not report["issues"] and report["changed"][0]["status"] == "consumed", report
    assert stack.quantities() == (15, 15, 0)


def test_startup_reuses_pending_reservation_and_preserves_new_batch_identity(stack):
    task, job, payload = stack.create()
    scheduler = BackendScheduler(stack.workflow, _ListenerOnlyBackend(), materials_gateway=stack.client)
    try:
        spec = {"workflow_node_uuid": payload["node_id"], "inventory_requirements": payload["inventory_requirements"],
                "scheduler_revision": 1, "current_job_uuid": job}
        scheduler._reserve_task_inventory(task, {"run":spec})
        assert spec["inventory_reservation_uuid"] == payload["inventory_reservation_uuid"]
        assert len(stack.materials.list_inventory_reservations()) == 1
        assert stack.quantities() == (20,15,5)
        # 重放不能重申请同一 attempt；结束节点也不能再次加入预留批次。
        scheduler._reserve_task_inventory(task, {"run":deepcopy(spec)})
        stack.finish(job, "succeeded")
        scheduler._reconcile_inventory()
        spec["inventory_job_status"] = "succeeded"
        scheduler._reserve_task_inventory(task, {"run":spec})
        assert stack.quantities() == (15,15,0)
    finally:
        scheduler.stop()


def test_scheduler_startup_failure_pauses_dispatch_and_retry_clears_only_own_reason(stack):
    task, job, payload = stack.create()
    stack.finish(job, "succeeded")
    class Offline:
        offline = True
        def __getattr__(self, name):
            if self.offline:
                raise ConnectionError("库存离线")
            return getattr(stack.client, name)
    gateway = Offline()
    scheduler = BackendScheduler(stack.workflow, _ListenerOnlyBackend(), materials_gateway=gateway)
    try:
        assert "inventory_reconciliation" in scheduler.dispatch_pause_reasons
        scheduler.pause_dispatch(reason="manual")
        gateway.offline = False
        scheduler._reconcile_resources()
        assert scheduler.dispatch_pause_reasons == ("manual",)
        assert stack.quantities() == (15,15,0)
    finally:
        scheduler.stop()


@pytest.mark.parametrize("reopen", [False, True])
def test_tenancy_keeps_unused_reservation_until_manual_release(tmp_path, reopen):
    from unilabos.server.services.runtime.workflow.device_tenancy import DeviceTenancies
    stack = Stack(tmp_path)
    task, job, payload = stack.create(with_tenancy=True)
    stack.finish(job, "canceled")
    stack.workflow.finish_workflow_task(task['uuid'], status='canceled', output={}, error_info=[])
    report = stack.recovery.reconcile()
    assert not report["issues"], report
    assert report["held"] and stack.quantities() == (20,15,5)
    if reopen:
        stack.close()
        stack = Stack(tmp_path)
    scheduler = BackendScheduler(stack.workflow, _ListenerOnlyBackend(), materials_gateway=stack.client)
    try:
        assert stack.quantities() == (20,15,5)
        held = DeviceTenancies.snapshot(stack.workflow.connection)[0]
        assert held['state'] == 'loaded'
        stack.workflow.change_resource_custody('release_tenancy', held['tenancy_uuid'],
            expected_version=held['version'], reason='现场确认样品已取走')
        # 人工处置事件立即唤醒双库对账，不依赖原进程内存中的任务。
        assert stack.quantities() == (20,20,0)
        scheduler._reconcile_resources()
        assert stack.quantities() == (20,20,0)
    finally:
        scheduler.stop()
        stack.close()


def test_old_authority_cannot_mutate_inventory_after_takeover(stack):
    task, job, payload = stack.create()
    stack.finish(job, "succeeded")
    stack.claims.activate_authority()
    assert stack.recovery.reconcile()["issues"]
    assert stack.quantities() == (20,15,5)


def test_report_endpoint_exposes_preserved_and_failed_reconciliation(stack):
    from unilabos.server.api.runtime.diagnostics import create_backend_app
    task, job, payload = stack.create()
    stack.issue(task, job, payload)
    scheduler = BackendScheduler(stack.workflow, _ListenerOnlyBackend(), materials_gateway=stack.client)
    try:
        with TestClient(create_backend_app(lambda: scheduler, lambda: None)) as client:
            report = ok_data(client.get("/api/v1/scheduler/inventory-recovery"))
            assert report["held"][0]["job_uuid"] == job
            assert report["held"][0]["task_uuid"] == task['uuid']
            # 前端重复请求对账不代表确认设备完成，不得改变未核实的库存。
            for _ in range(2):
                retry = ok_data(client.post('/api/v1/scheduler/inventory-recovery/reconcile'))
                assert retry['held'][0]['job_uuid'] == job
                assert stack.quantities() == (20, 15, 5)
    finally:
        scheduler.stop()


def test_real_scheduler_completion_settles_inventory_without_host_callback(stack):
    task, job, payload = stack.create()
    executor = Executor()
    scheduler = BackendScheduler(stack.workflow, executor, materials_gateway=stack.client)
    try:
        scheduler.submit(task["uuid"])
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["inventory_reservation_uuid"] == payload["inventory_reservation_uuid"]
        executor.finish(0, {"done":True})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        assert stack.quantities() == (15,15,0)
        assert not scheduler.inventory_recovery_snapshot()["issues"]
        assert len(executor.dispatched) == 1
    finally:
        scheduler.stop()


def test_hostlink_consumes_before_one_driver_call_and_reopen_never_deducts_again(tmp_path):
    from unilabos.client.host.hostlink.backend import HostLinkBackend
    from unilabos.client.host.hostlink.host_node import HostNode
    from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
    from unilabos.client.host.hostlink.adapter_registry import clear_execution_adapter
    from unilabos.client.host.execution import JobExecutionBackend
    value = Stack(tmp_path)
    task, job, payload = value.create()
    calls = []
    def on_operate(solvent):
        assert value.materials.get_inventory_reservation_by_job(job).status == "consumed"
        assert value.quantities() == (15,15,0)
        calls.append(solvent["quantity"])
        return {"done": True}
    local = HostLinkLocalRuntime()
    node = local.add_driver(HostLinkDriverSpec(device_id="reactor", driver_class=InventoryDriver, config={},
        action_names=("operate",), action_value_mappings={"operate":{"type":"UniLabJsonCommand"}}))
    node.driver.callback = on_operate
    runtime = HostLinkBackend(local, is_slave=False)
    local.start()
    adapter = HostNode("host_node", runtime, bridges=[])
    backend = JobExecutionBackend(host_node_getter=lambda: adapter, materials_gateway=value.client)
    adapter.bridges = [backend]
    backend.start()
    scheduler = BackendScheduler(value.workflow, backend, materials_gateway=value.client)
    try:
        scheduler.submit(task["uuid"])
        wait_for(lambda: value.workflow.get_workflow_task(task["uuid"])["status"] in {"succeeded","failed"})
        assert value.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded"
        assert calls == [5]
        assert value.quantities() == (15,15,0)
    finally:
        scheduler.stop()
        backend.stop()
        clear_execution_adapter(adapter)
        adapter.stop()
        HostNode.reset_state()
        runtime.stop()
        value.close()
    reopened = Stack(tmp_path)
    try:
        assert not reopened.recovery.reconcile()["issues"]
        assert reopened.quantities() == (15,15,0)
        assert calls == [5]
    finally:
        reopened.close()


def test_failed_action_replaced_by_operator_success_does_not_pause_dispatch(tmp_path):
    """真实 HostLink 驱动失败：开始时已扣减，失败后 Host 隔离预留再打开决策；操作员经
    ``/api/v1/error-decisions`` 人工替换为成功后，库存对账不得把它当冲突而全局暂停派发。"""
    from unilabos.client.host.hostlink.backend import HostLinkBackend
    from unilabos.client.host.hostlink.host_node import HostNode
    from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
    from unilabos.client.host.hostlink.adapter_registry import clear_execution_adapter
    from unilabos.server.api.runtime.diagnostics import create_backend_app
    from unilabos.client.host.execution import JobExecutionBackend
    value = Stack(tmp_path)
    task, job, payload = value.create()
    calls = []
    def on_operate(solvent):
        assert value.materials.get_inventory_reservation_by_job(job).status == "consumed"
        calls.append(solvent["quantity"])
        raise RuntimeError("设备报错：液位传感器无读数")
    local = HostLinkLocalRuntime()
    node = local.add_driver(HostLinkDriverSpec(device_id="reactor", driver_class=InventoryDriver, config={},
        action_names=("operate",), action_value_mappings={"operate":{"type":"UniLabJsonCommand"}}))
    node.driver.callback = on_operate
    runtime = HostLinkBackend(local, is_slave=False)
    local.start()
    adapter = HostNode("host_node", runtime, bridges=[])
    backend = JobExecutionBackend(host_node_getter=lambda: adapter, materials_gateway=value.client)
    adapter.bridges = [backend]
    backend.start()
    scheduler = BackendScheduler(value.workflow, backend, materials_gateway=value.client)
    # 与本机装配一致：调度器是本机派发 job 的失败决策 owner
    backend.result_bridges.append(scheduler)
    try:
        scheduler.submit(task["uuid"])
        wait_for(lambda: backend.list_error_decisions())
        assert value.materials.get_inventory_reservation_by_job(job).status == "quarantined"
        [decision] = backend.list_error_decisions()
        with TestClient(create_backend_app(lambda: scheduler, lambda: backend)) as client:
            ok_data(client.post(f"/api/v1/error-decisions/{decision['decision_id']}", json={
                "action": "operator_intervention", "result": {"done": True}, "reason": "现场确认已完成"}))
        wait_for(lambda: value.workflow.get_workflow_task(task["uuid"])["status"] in {"succeeded", "failed"})
        assert value.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded"
        assert value.workflow.get_workflow_node_job(job)["return_info"]["suc_type"] == "operator_intervention"
        report = scheduler._reconcile_inventory()
        assert not report["issues"], report
        assert "inventory_reconciliation" not in scheduler.dispatch_pause_reasons
        assert value.materials.get_inventory_reservation_by_job(job).status == "quarantined"
        assert value.quantities() == (15, 15, 0)
        assert calls == [5]
    finally:
        scheduler.stop()
        backend.stop()
        clear_execution_adapter(adapter)
        adapter.stop()
        HostNode.reset_state()
        runtime.stop()
        value.close()


def test_idle_scheduler_retries_failed_inventory_without_new_task_events(stack):
    import time
    task, job, payload = stack.create()
    stack.finish(job, "succeeded")
    class Offline:
        offline = True
        def __getattr__(self, name):
            if self.offline:
                raise ConnectionError("临时断线")
            return getattr(stack.client, name)
    gateway = Offline()
    scheduler = BackendScheduler(stack.workflow, _ListenerOnlyBackend(), materials_gateway=gateway)
    try:
        assert scheduler.dispatch_paused
        scheduler.start(recover=False)
        gateway.offline = False
        deadline = time.monotonic() + 8
        while scheduler.dispatch_paused and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not scheduler.dispatch_paused
        assert stack.quantities() == (15,15,0)
    finally:
        scheduler.stop()


def test_concurrent_recovery_connections_commit_one_inventory_effect(stack):
    from concurrent.futures import ThreadPoolExecutor
    task, job, payload = stack.create()
    stack.finish(job, "succeeded")
    other_workflow = WorkflowService(stack.path / "runtime.db")
    other_materials = MaterialsService(stack.path / "materials.db")
    other = InventoryRecovery(other_workflow, LocalMaterialsClient(other_materials), stack.epoch)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda owner: owner.reconcile(), (stack.recovery,other)))
        assert all(not report["issues"] for report in reports), reports
        assert sum(len(report["changed"]) for report in reports) == 1
        assert stack.quantities() == (15,15,0)
    finally:
        other_workflow.close()
        other_materials.close()


@pytest.mark.parametrize("gateway_missing", [False, True])
def test_frozen_dispatch_missing_ledger_or_gateway_cannot_silently_pass(stack, gateway_missing):
    task, job, payload = stack.create()
    stack.issue(task, job, payload)
    class EmptyLedger:
        def list_inventory_reservations(self):
            return []
    recovery = InventoryRecovery(stack.workflow, None if gateway_missing else EmptyLedger(), stack.epoch)
    assert recovery.reconcile()["issues"]
    assert stack.quantities() == (20,15,5)
