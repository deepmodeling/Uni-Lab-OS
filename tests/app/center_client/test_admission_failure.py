"""未建 Task 的明确拒绝必须持久补报，不能阻塞工站轮询。"""
from pathlib import Path

import pytest

from tests.app.center_client.test_runtime import Center, Service, runtime
from unilabos.workflow.service import WorkflowConflict, WorkflowError


def rejecting_service():
    service = Service()
    def reject(**kwargs):
        service.calls.append(kwargs)
        raise WorkflowError("inventory_admission_failed", message="来源容器 10000000-0000-4000-8000-000000000001 没有试剂或当前内容物数量")
    service.submit_station_workflow = reject
    return service


def test_uncreated_task_failure_is_reported_once_and_catalog_continues(tmp_path: Path):
    center, service = Center(), rejecting_service()
    command = center.add()
    run = runtime(tmp_path, center, service)
    catalog = []
    run._sync_catalog = lambda **kwargs: catalog.append(kwargs)
    try:
        for _ in range(3):
            run.poll_once()
        assert len(center.reports) == 1, "明确拒绝须通过原事件入口通知中心"
        event = center.reports[0]
        assert event["event_type"] == "admission_failed"
        assert event["local_task_uuid"] is None
        assert event["payload"]["output"]["error_info"] == [
            {"code": "inventory_admission_failed", "message": "来源容器 10000000-0000-4000-8000-000000000001 没有试剂或当前内容物数量"}
        ]
        assert service.task is None and len(service.calls) == 1 and center.claims == 1
        assert len(catalog) == 3
        row = run.store._db.execute("SELECT acknowledged,handled,error FROM center_command_inbox WHERE command_uuid=?", (command["command"]["command_uuid"],)).fetchone()
        assert tuple(row) == (1, 1, "inventory_admission_failed")
        assert run.store.package(event["station_invocation_uuid"]) is not None
        assert run.store.events() == []
    finally:
        run.close()


def test_admission_failure_replays_exact_event_after_restart(tmp_path: Path):
    center, service = Center(), rejecting_service()
    center.add()
    center.report_lost = True
    run = runtime(tmp_path, center, service)
    try:
        run.poll_once()
        assert len(center.reports) == 1
        original = center.reports[0]
        assert len(run.store.events()) == 1
    finally:
        run.close()
    recovered_service = Service()
    run = runtime(tmp_path, center, recovered_service)
    try:
        run.poll_once()
        assert center.reports == [original, original]
        assert center.claims == 1 and recovered_service.calls == []
        assert run.store.events() == []
    finally:
        run.close()


def test_unclassified_conflict_is_not_reported_as_permanent_failure(tmp_path: Path):
    center, service = Center(), Service()
    center.add()
    def reject(**kwargs):
        raise WorkflowConflict("conflict", message="需要恢复核实的冲突")
    service.submit_station_workflow = reject
    run = runtime(tmp_path, center, service)
    try:
        run.poll_once()
        assert not center.reports and run.store.commands()
    finally:
        run.close()


def test_failure_and_command_finish_roll_back_together(tmp_path, monkeypatch):
    from unilabos.persistence import center_events
    center, service = Center(), rejecting_service()
    center.add()
    run = runtime(tmp_path, center, service)
    original = center_events.append_event_in_transaction
    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("模拟发件箱写入后的崩溃")
    try:
        with monkeypatch.context() as m:
            m.setattr(center_events, "append_event_in_transaction", fail_after_write)
            with pytest.raises(RuntimeError):
                run.poll_once()
        assert run.store.events() == []
        assert len(run.store.commands()) == 1 and not center.reports
        row = run.store._db.execute("SELECT terminal_recorded,local_task_uuid FROM center_invocation_inbox").fetchone()
        assert tuple(row) == (0, None)
        run.poll_once()
        assert len(center.reports) == 1 and run.store.commands() == []
    finally:
        run.close()


def test_existing_task_is_recovered_instead_of_rejected(tmp_path):
    from tests.app.center_client.test_atomic_workflow_events import _submit, _workflow
    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        command = Center().add()["command"]["command_uuid"]
        assert not durable.reject_submission(package, command, code="invalid_input", message="不能覆盖真实任务")
        assert store.get_task(task["uuid"])["status"] == task["status"]
        assert all(e[1]["event_type"] != "admission_failed" for e in durable.events())


def test_repeated_notification_cannot_recreate_rejected_task(tmp_path):
    center, service = Center(), rejecting_service()
    center.add()
    run = runtime(tmp_path, center, service)
    try:
        run.poll_once()
        center.add()
        run.poll_once()
        assert len(center.reports) == len(service.calls) == center.claims == 1
        assert run.store.commands() == []
    finally:
        run.close()


def test_real_empty_source_rolls_back_task_and_preserves_inventory_reason(tmp_path, monkeypatch):
    from dataclasses import replace
    from uuid import uuid4

    from tests.workflow.quantity_inventory_test_support import WORKFLOW_UUID, _inventory, _workflow
    from tests.workflow.test_quantity_inventory_runtime import _InventoryCreationBridge
    from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
    from unilabos.app.scheduler.inventory.service import InventoryService
    from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
    from unilabos.workflow.service import WorkflowService
    class QuantityCreationBridge(_InventoryCreationBridge):
        def preflight_material_sources(self, prepared, *, task_uuid=""):
            # 此接缝验证真实数量准入；来源选料由独立的完整工作流回归覆盖。
            return []
    inventory, material, info, reagent = _inventory(tmp_path)
    store, graph, prepared = _workflow(tmp_path / "workflow.db", reagent_info_uuid=info, material_uuid=material)
    source_uuid = str(uuid4())
    graph["inventory_requirements"][0].update(target_type="current_substance", reagent_info_uuid=None,
        meta_data={"unilab": {"material_source_node_uuid": source_uuid}})
    source = {"uuid": source_uuid, "kind": "material_source", "material_requirements": [{"instance_uuid": material}]}
    prepared = replace(prepared, workflow_snapshot=graph, execution_plan={**prepared.execution_plan, "nodes": [source, *prepared.execution_plan["nodes"]]})
    coordinator = WorkflowQuantityInventory(store, InventoryService(inventory))
    service = WorkflowService(store, task_scheduler_bridge=QuantityCreationBridge(coordinator))
    monkeypatch.setattr(service, "_prepare_task_input", lambda *args, **kwargs: prepared)
    BackendReagentService(inventory).delete_reagent(reagent)
    try:
        with pytest.raises(WorkflowError) as caught:
            service.create_workflow_task(workflow_uuid=WORKFLOW_UUID, run_mode="normal", target_node_uuid=None,
                input_value={}, description=None, meta_data={}, frozen_graph=graph,
                backend_task_uuid=str(uuid4()), invocation_key="empty-source", request_fingerprint="sha256:" + "1" * 64)
        assert caught.value.code == "conflict", "纯本地/原工站提交接口保留原错误码"
        assert "没有试剂或当前内容物数量" in caught.value.message
        assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 0
        # 同一真实库存错误经已领取的中心入口，才分类为明确受理拒绝。
        from tests.app.center_client.test_atomic_workflow_events import _submit
        from tests.app.center_client.test_atomic_workflow_events import _workflow as center_workflow
        with center_workflow(tmp_path / "center") as (center_store, center_service, durable, package, _):
            center_coordinator = WorkflowQuantityInventory(center_store, InventoryService(inventory))
            center_service._task_scheduler_bridge = QuantityCreationBridge(center_coordinator)
            monkeypatch.setattr(center_service, "_prepare_task_input", lambda *args, **kwargs: prepared)
            with pytest.raises(WorkflowError) as rejected:
                _submit(center_service, package)
            assert rejected.value.code == "inventory_admission_failed"
            assert "没有试剂或当前内容物数量" in rejected.value.message
            assert center_store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
            # 同一失败继续走真实轮询者和发件箱，验证库存原因直到上报出口。
            center = Center()
            center.package = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
            center.add()
            bridge_runtime = runtime(tmp_path / "center", center, center_service)
            try:
                for _ in range(3):
                    bridge_runtime.poll_once()
                assert center.claims == 1 and len(center.reports) == 1
                report = center.reports[0]
                assert report["event_type"] == "admission_failed" and report["local_task_uuid"] is None
                assert report["payload"]["output"]["error_info"][0]["message"] == f"来源容器 {material} 没有试剂或当前内容物数量"
            finally:
                bridge_runtime.close()
    finally:
        service.close()
        inventory.close()


def test_invalid_input_does_not_upload_raw_reference_or_secret(tmp_path):
    center, service = Center(), Service()
    center.add()
    secret = "https://token-secret:password@private.invalid/source"
    def reject(**kwargs):
        raise WorkflowError("invalid_input", message="来源库位不合法：" + secret)
    service.submit_station_workflow = reject
    run = runtime(tmp_path, center, service)
    try:
        run.poll_once()
        assert secret not in str(center.reports)
        assert "password" not in str(center.reports)
    finally:
        run.close()


def test_uncreated_failure_history_restores_terminal_phase_and_guard(tmp_path):
    from tests.app.center_client.test_center_client import INSTANCE, STATION
    from tests.app.center_client.test_event_history import response
    from unilabos.app.center_client.event_history import EventHistoryStore
    center, service = Center(), rejecting_service()
    center.add()
    run = runtime(tmp_path, center, service)
    run.poll_once()
    event = center.reports[0]
    with run.store._db.transaction():
        run.store._db.execute("DELETE FROM center_boundary_outbox")
        run.store._db.execute("UPDATE center_invocation_inbox SET terminal_recorded=0")
    run.close()
    history = EventHistoryStore(tmp_path / "workflow_history.db", station_uuid=STATION, instance_uuid=INSTANCE)
    try:
        history.prepare(event["event_sequence"])
        request = {"event_epoch_uuid": event["event_epoch_uuid"], "event_sequence": event["event_sequence"], "identity": None}
        assert history.accept(request, response(event, prefix=1))["state"] == "recovered"
        assert history._db.execute("SELECT phase FROM center_boundary_outbox").fetchone()[0] == 1
        assert history._db.execute("SELECT terminal_recorded FROM center_invocation_inbox").fetchone()[0] == 1
    finally:
        history.close()


from tests.app.test_station_material_inputs import received  # noqa: E402,F401


def test_rejected_creation_releases_received_material_without_task(tmp_path, received, monkeypatch):
    from tests.app.center_client.test_material_inputs import material_runtime
    with material_runtime(tmp_path, received, monkeypatch) as (run, center, service, store, durable):
        def reject(**kwargs):
            raise WorkflowError("invalid_input")
        monkeypatch.setattr(service, "submit_station_workflow", reject)
        run.poll_once()
        assert center.reports[0]["event_type"] == "admission_failed"
        assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
        assert durable.material_input(center.package["payload"]["station_invocation_uuid"])["state"] == "released"
        assert received["stores"]["target"].query_one("SELECT state FROM station_material_input")["state"] == "released"


def test_shared_admission_envelope_preserves_normal_task_identity_rules():
    import json
    from copy import deepcopy

    from unilabos.center_protocol.digest import digest
    from unilabos.center_protocol.events import EventEnvelopeError, validate_event
    source = Path(__file__).parents[2] / "fixtures" / "center_admission_failed_v1.json"
    original = json.loads(source.read_text())
    validate_event(original)
    for key, value in [("local_task_uuid", "66666666-6666-4666-8666-666666666666"), ("event_type", "outcome"), ("event_type", "running")]:
        changed = deepcopy(original)
        changed[key] = value
        changed.pop("event_digest")
        changed["event_digest"] = digest(changed)
        with pytest.raises(EventEnvelopeError):
            validate_event(changed)


def test_terminal_outbox_blocks_recreation_when_invocation_row_was_lost(tmp_path):
    center, service = Center(), rejecting_service()
    center.add()
    run = runtime(tmp_path, center, service)
    run.poll_once()
    with run.store._db.transaction():
        run.store._db.execute("DELETE FROM center_invocation_inbox")
    run.close()
    replacement = Service()
    run = runtime(tmp_path, center, replacement)
    try:
        center.add()
        run.poll_once()
        assert replacement.calls == [], "已恢复的终态发件箱不能因收件映射缺失而重建执行"
        assert center.claims == 1
    finally:
        run.close()
