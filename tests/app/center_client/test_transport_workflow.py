"""真实工作流创建验证搬运许可、连续车辆区间和持久结果。"""

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.center_client.test_atomic_workflow_events import _events, _start
from tests.app.center_client.test_center_client import INSTANCE, INVOCATION, STATION, package_response
from tests.workflow.test_c1_published_workflow_contract import _applied_snapshot
from unilabos.app.center_client.client import TaskPackage
from unilabos.app.center_client.digest import digest
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.runtime import submission
from unilabos.center_protocol.task_package import TaskPackageError, validate_task_package
from unilabos.center_protocol.transport import delivery_receipt
from unilabos.workflow.published_contract import PublishedWorkflowContractStore
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.store import WorkflowStore


def context():
    return {
        "schema_version": 1,
        "handoff_uuid": str(uuid4()),
        "authorization_uuid": str(uuid4()),
        "logistics_station_uuid": STATION,
        "source_station_uuid": str(uuid4()),
        "target_station_uuid": str(uuid4()),
        "vehicle_id": "agv-1",
        "container_uuids": [str(uuid4())],
    }


def publish(store, ctx, *, locked=True, vehicle_material_uuid=None, pickup=False, placement=False):
    graph = _applied_snapshot()
    graph["workflow"]["revision"] = 1
    if placement:
        graph["workflow"]["meta_data"]["center_capabilities"]=["center_transport.placement.v2"]
    meta = graph["workflow"]["meta_data"]["unilab"]
    schema = {"type": "string"}
    meta["input_contract"]["parameters"] = [{"name": "center_transport", "schema": schema, "required": True}]
    meta["output_contract"]["outputs"] = [{"name": "transport_receipt", "schema": schema, "implicit": False}]
    meta["output_bindings"] = {"transport_receipt": meta["output_bindings"]["result"]}
    if locked:
        meta["resources"] = ["vehicle"]
        meta["resource_bindings"] = {"vehicle": {"canonical_key": "/devices/" + (vehicle_material_uuid or "agv-1"), "kind": "device"}}
    node = graph["nodes"][0]
    if pickup:
        graph["workflow"]["meta_data"]["center_transport_pickup"] = {
            "node_uuid": node["uuid"], "output_key": "pickup_receipt",
        }
    node.update(action_name="unload", action_type="UniLabJsonCommand")
    unilab = node["meta_data"]["unilab"]
    unilab["executor_binding"] = {"mode": "fixed", "device_id": "agv-1"}
    for binding in unilab["input_bindings"].values():
        binding["parameter"] = "center_transport"
    for handle in graph["handle_templates"]:
        handle.update(type="string")
        handle["meta_data"]["unilab"]["value_schema"] = schema
    action = {
        "type": "object",
        "properties": {
            "goal": {"type": "object", "properties": {"value": schema}, "required": ["value"]},
            "result": {"type": "object", "properties": {"result": schema}, "required": ["result"]},
        },
        "required": ["goal"],
    }
    if pickup:
        action["properties"]["result"]["properties"]["pickup_receipt"] = schema
        action["properties"]["result"]["required"].append("pickup_receipt")
    graph["node_templates"][0].update(
        schema=action["properties"]["goal"],
        meta_data={"unilab": {"contract_kind": "typed", "action_contract_schema": action}},
    )
    store.create_workflow(
        workflow_uuid=graph["workflow"]["uuid"], name="AGV transport", tags=[], description=None, meta_data={}
    )
    contract = PublishedWorkflowContractStore(store).publish(graph=graph, expected_revision=1)
    body = package_response()
    body["payload"].update(
        workflow_uuid=graph["workflow"]["uuid"],
        revision_fingerprint=contract["source_hash"],
        input={"center_transport": json.dumps(ctx)},
        transport=ctx,
    )
    body["payload_digest"] = digest(body["payload"])
    return body


@pytest.mark.parametrize("wrapped", [False, True])
def test_pickup_event_uses_declared_successful_job_and_replays_original_shared_sequence(tmp_path, wrapped, monkeypatch):
    from unilabos.center_protocol.transport import pickup_receipt

    path = tmp_path / "pickup.db"
    store = WorkflowStore(path)
    ctx = context()
    body = publish(store, ctx, pickup=True)
    durable = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    service = WorkflowService(store)
    try:
        package = TaskPackage.from_response(body)
        durable.remember_package(package)
        durable.confirm_claim(INVOCATION)
        task = service.submit_station_workflow(**submission(package), center_invocation_uuid=INVOCATION)
        assert durable.capture_transport_pickups() == []
        projection, job = _start(store, task)
        output = {
            "result": delivery_receipt(ctx, delivered=True, evidence_refs=["scan:unload"]),
            "pickup_receipt": pickup_receipt(ctx, picked_up=True, evidence_refs=["scan:load"]),
        }
        # 实际 UniLabJsonCommand 在 return_value 内保存动作返回值。
        returned = {"suc": True, "return_value": output} if wrapped else output
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info=returned)
        original = _events(store)
        assert [event["event_type"] for event in original] == ["accepted", "outcome"]
        from unilabos.workflow import transport_pickup

        append = transport_pickup.append_transport_progress_in_transaction
        attempted = []
        def fail_after_event(*args, **kwargs):
            attempted.append(append(*args, **kwargs))
            raise RuntimeError("取货事件提交前故障")
        with monkeypatch.context() as fault:
            fault.setattr(transport_pickup, "append_transport_progress_in_transaction", fail_after_event)
            with pytest.raises(RuntimeError, match="取货事件提交前"):
                durable.capture_transport_pickups()
        assert _events(store) == original
        assert store.get_job(job)["return_info"] == returned
        events = durable.capture_transport_pickups()
        assert len(events) == 1
        event = events[0]
        assert attempted == events
        progress = event["payload"]["transport_progress"]
        assert event["event_type"] == "transport_progress" and event["event_sequence"] == 3
        assert event["local_task_uuid"] == task["uuid"]
        assert progress["stage"] == "picked_up" and progress["job_uuid"] == job
        assert progress["container_uuids"] == ctx["container_uuids"]
        assert _events(store)[:2] == original
        assert durable.capture_transport_pickups() == []
        durable.close()
        durable = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
        assert durable.capture_transport_pickups() == []
        assert _events(store) == original + events
        assert any(pending == event for _, pending in durable.events())
    finally:
        durable.close()
        service.close()


@pytest.mark.parametrize("case", ["no_declaration", "failed", "wrong_container", "missing_receipt"])
def test_pickup_does_not_infer_from_success_or_foreign_receipt(tmp_path, case):
    from unilabos.center_protocol.transport import pickup_receipt

    path = tmp_path / "invalid-pickup.db"
    store = WorkflowStore(path)
    ctx = context()
    body = publish(store, ctx, pickup=case != "no_declaration")
    durable = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    service = WorkflowService(store)
    try:
        package = TaskPackage.from_response(body)
        durable.remember_package(package)
        durable.confirm_claim(INVOCATION)
        task = service.submit_station_workflow(**submission(package), center_invocation_uuid=INVOCATION)
        projection, job = _start(store, task)
        receipt_context = dict(ctx, container_uuids=[str(uuid4())]) if case=="wrong_container" else ctx
        output = {"result":delivery_receipt(ctx, delivered=True, evidence_refs=["scan:unload"])}
        if case != "missing_receipt":
            output["pickup_receipt"] = pickup_receipt(receipt_context, picked_up=True, evidence_refs=["scan:load"])
        projection.project_job_finished(job_uuid=job, scheduler_state="failed" if case=="failed" else "success", return_info=output)
        prior = _events(store)
        assert durable.capture_transport_pickups() == []
        assert _events(store) == prior
        assert store.get_job(job)["return_info"] == output
    finally:
        durable.close()
        service.close()


def test_unfinished_old_pickups_cannot_starve_later_successful_job(tmp_path, monkeypatch):
    from unilabos.app.center_client import durable as module
    from unilabos.center_protocol.transport import pickup_receipt

    monkeypatch.setattr(module, "RECOVERY_BATCH_SIZE", 2)
    path = tmp_path / "pickup-batch.db"
    store = WorkflowStore(path)
    ctx = context()
    body = publish(store, ctx, pickup=True)
    durable = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    service = WorkflowService(store)
    try:
        tasks = []
        for index in range(4):
            current = deepcopy(body)
            invocation = f"a0000000-0000-4000-8000-{index+1:012d}"
            current["payload"].update(station_invocation_uuid=invocation,
                                      invocation_key=current["payload"]["global_workflow_revision_uuid"] + ":" + str(uuid4()))
            current["payload_digest"] = digest(current["payload"])
            package = TaskPackage.from_response(current)
            durable.remember_package(package)
            durable.confirm_claim(invocation)
            tasks.append(service.submit_station_workflow(**submission(package), center_invocation_uuid=invocation))
        projection, job = _start(store, tasks[-1])
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={
            "result":delivery_receipt(ctx, delivered=True, evidence_refs=["scan:unload"]),
            "pickup_receipt":pickup_receipt(ctx, picked_up=True, evidence_refs=["scan:load"]),
        })
        events = durable.capture_transport_pickups()
        assert len(events)==1 and events[0]["local_task_uuid"]==tasks[-1]["uuid"]
    finally:
        durable.close()
        service.close()


@pytest.mark.parametrize("locked", [True, False])
def test_actual_workflow_permit_lock_and_result(tmp_path, locked):
    path = tmp_path / "workflow.db"
    store = WorkflowStore(path)
    ctx = context()
    body = publish(store, ctx, locked=locked)
    durable = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    service = WorkflowService(store)
    try:
        package = TaskPackage.from_response(body)
        durable.remember_package(package)
        durable.confirm_claim(INVOCATION)
        if not locked:
            with pytest.raises(WorkflowConflict):
                service.submit_station_workflow(**submission(package), center_invocation_uuid=INVOCATION)
            assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
            return
        task = service.submit_station_workflow(**submission(package), center_invocation_uuid=INVOCATION)
        replay = service.submit_station_workflow(**submission(package), center_invocation_uuid=INVOCATION)
        assert task["uuid"] == replay["uuid"]
        projection, job = _start(store, task)
        receipt = delivery_receipt(ctx, delivered=True, evidence_refs=["scan:unloaded"])
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": receipt})
        assert _events(store)[-1]["payload"]["output"] == {"transport_receipt": receipt}
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        service.recover_center_workflow_invocation(INVOCATION)
        assert len(_events(store)) == 2
    finally:
        durable.close()
        service.close()


@pytest.mark.parametrize("resolution", ["current", "missing", "different"])
def test_center_vehicle_alias_uses_authoritative_inventory_lock(tmp_path, resolution):
    """冻结许可保留车辆业务 ID，实际连续锁使用库存 UUID，两者经权威解析匹配。"""
    from unilabos.workflow.resource_lock_key import device_lock_key

    path = tmp_path / "workflow.db"
    store = WorkflowStore(path)
    ctx = context()
    material_uuid, template_uuid = str(uuid4()), str(uuid4())
    body = publish(store, ctx, vehicle_material_uuid=material_uuid)
    durable = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)

    def resolve(identity):
        assert identity == ctx["vehicle_id"]
        if resolution == "missing":
            return None
        return {"uuid": material_uuid if resolution == "current" else str(uuid4()),
                "resource_template_uuid": template_uuid}

    service = WorkflowService(store, resource_reference_resolver=resolve)
    try:
        package = TaskPackage.from_response(body)
        durable.remember_package(package)
        durable.confirm_claim(INVOCATION)
        if resolution != "current":
            with pytest.raises(WorkflowConflict):
                service.submit_station_workflow(**submission(package), center_invocation_uuid=INVOCATION)
            assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
            return
        task = service.submit_station_workflow(**submission(package), center_invocation_uuid=INVOCATION)
        replay = service.submit_station_workflow(**submission(package), center_invocation_uuid=INVOCATION)
        assert replay["uuid"] == task["uuid"]
        assert task["input"]["center_transport"] == body["payload"]["input"]["center_transport"]
        plan = store.get_task(task["uuid"])["execution_plan"]
        assert any(r["canonical_key"] == device_lock_key(material_uuid) for r in plan["resource_plan"]["resources"])
    finally:
        durable.close()
        service.close()


@pytest.mark.parametrize(
    "change", ["missing_marker", "wrong_station", "changed_input", "boolean_version", "duplicate_container"]
)
def test_forged_or_malformed_transport_package_rejected(change):
    body = package_response()
    ctx = context()
    body["payload"].update(input={"center_transport": json.dumps(ctx)}, transport=deepcopy(ctx))
    if change == "missing_marker":
        del body["payload"]["transport"]
    elif change == "wrong_station":
        body["payload"]["transport"]["logistics_station_uuid"] = str(uuid4())
    elif change == "changed_input":
        changed = deepcopy(ctx)
        changed["vehicle_id"] = "other"
        body["payload"]["input"]["center_transport"] = json.dumps(changed)
    elif change == "boolean_version":
        body["payload"]["transport"]["schema_version"] = True
    else:
        body["payload"]["transport"]["container_uuids"] *= 2
    body["payload_digest"] = digest(body["payload"])
    with pytest.raises(TaskPackageError):
        validate_task_package(body)


def test_receipt_requires_actual_delivery_evidence():
    with pytest.raises(ValueError):
        delivery_receipt(context(), delivered=False, evidence_refs=["nav:complete"])
    with pytest.raises(ValueError):
        delivery_receipt(context(), delivered=True, evidence_refs=[])


def test_transport_call_can_use_independent_station_inventory():
    """搬运仍是普通调用；其设备用品与本次承运货物保持不同占用归属。"""
    body = package_response()
    ctx = context()
    body["payload"].update(
        input={"center_transport": json.dumps(ctx), "supply": str(uuid4())}, transport=ctx,
        material_refs=[{"station_uuid": STATION, "local_material_uuid": str(uuid4()),
                        "local_revision": 1, "material_lineage_uuid": str(uuid4()),
                        "handoff_item_uuid": None}],
        container_refs=[{"container_uuid": str(uuid4()), "state_version": 1}],
    )
    body["payload_digest"] = digest(body["payload"])
    assert validate_task_package(body) == body


@pytest.mark.parametrize("overlap", ["handoff", "container"])
def test_transport_call_cannot_consume_its_own_cargo(overlap):
    body = package_response()
    ctx = context()
    body["payload"].update(input={"center_transport": json.dumps(ctx)}, transport=ctx)
    if overlap == "handoff":
        body["payload"]["handoff_uuids"] = [ctx["handoff_uuid"]]
    else:
        body["payload"]["container_refs"] = [{"container_uuid": ctx["container_uuids"][0], "state_version": 1}]
    body["payload_digest"] = digest(body["payload"])
    with pytest.raises(TaskPackageError):
        validate_task_package(body)


@pytest.mark.parametrize("input_value", [None, {}, [], "invalid"])
def test_ordinary_workflow_input_compatibility(input_value):
    from tests.workflow.test_f05_task_scheduler_composition import (
        WORKFLOW_UUID,
        _RecordingStore,
        _RecordingTaskBridge,
    )
    from unilabos.workflow.service import WorkflowError

    events = []
    store = _RecordingStore(events)
    definition = WorkflowStore(":memory:")
    definition.create_workflow(workflow_uuid=WORKFLOW_UUID, name="普通任务", tags=[], description=None, meta_data={})
    service = WorkflowService(
        store, definition_store=definition, task_scheduler_bridge=_RecordingTaskBridge(store, events)
    )
    try:
        args = dict(
            workflow_uuid=WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value=input_value,
            description=None,
            meta_data={},
        )
        if input_value is None or isinstance(input_value, dict):
            assert service.create_workflow_task(**args)["status"] == "running"
        else:
            with pytest.raises(WorkflowError) as caught:
                service.create_workflow_task(**args)
            assert caught.value.code == "invalid_input"
            assert not events
    finally:
        service.close()
        definition.close()


def test_local_transport_input_returns_conflict_without_creating_a_task(tmp_path):
    """本地自带搬运许可应返回标准业务冲突，不能在错误映射中变成 500。"""
    from fastapi.testclient import TestClient

    from unilabos.app.workflow_api import create_workflow_app

    store = WorkflowStore(tmp_path / "local-transport.db")
    ctx = context()
    package = publish(store, ctx)
    service = WorkflowService(store)
    try:
        response = TestClient(create_workflow_app(service)).post("/api/v1/workflow-tasks", json={
            "workflow_uuid": package["payload"]["workflow_uuid"],
            "input": {"center_transport": json.dumps(ctx)},
        })
        assert response.status_code == 200, response.text
        assert response.json()["code"] == 1
        assert "禁止本地输入自行授权" in response.json()["error"]["msg"]
        assert store.count_rows("workflow_task") == 0
    finally:
        service.close()
