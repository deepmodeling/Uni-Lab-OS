"""真实库存导出、跨库崩溃恢复与稳定谱系回执。"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.center_client import test_material_outcomes as outcomes
from tests.app.center_client.test_atomic_workflow_events import _events, _start
from tests.app.center_client.test_center_client import INSTANCE, STATION
from unilabos.app.center_client.material import MaterialBridge
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.handoff import StationHandoffAuthority
from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.material import MaterialAllocationError, validate_allocation_receipt
from unilabos.workflow.store import StoreConflict


def uid():
    return str(uuid4())


class Allocator:
    def __init__(self):
        self.receipts = {}
        self.requests = []
        self.lose_response = False
        self.containers = {}
        self.boundary_requests = []
        self.boundary_receipts = {}
        self.lose_boundary_response = False

    def allocate_material_lineages(self, invocation, request, epoch):
        self.requests.append(deepcopy(request))
        if invocation not in self.receipts:
            self.receipts[invocation] = {
                "station_invocation_uuid": invocation, "owner_instance_uuid": INSTANCE,
                "task_payload_digest": request["task_payload_digest"], "request_digest": digest(request),
                "items": [{"material_port": i["material_port"], "item_key": i["item_key"], "origin": i["origin"],
                    "material_ref": {"station_uuid": STATION, "local_material_uuid": i["local_material_uuid"],
                        "local_revision": i["local_revision"], "material_lineage_uuid": uid(), "handoff_item_uuid": None}}
                    for i in request["items"]],
            }
        else:
            assert self.receipts[invocation]["request_digest"] == digest(request)
        if self.lose_response:
            self.lose_response = False
            raise CenterTransportError("模拟中心已提交、回执丢失", retryable=True)
        return deepcopy(self.receipts[invocation])

    def container(self, identity, epoch):
        return deepcopy(self.containers[identity])

    def publish_material_boundary(self, invocation, request, epoch):
        self.boundary_requests.append(deepcopy(request))
        receipt = {"station_invocation_uuid": invocation, "owner_instance_uuid": INSTANCE,
                   "request_digest": digest(request)}
        assert self.boundary_receipts.setdefault(invocation, receipt) == receipt
        if self.lose_boundary_response:
            self.lose_boundary_response = False
            raise CenterTransportError("模拟边界已提交但回执丢失", retryable=True)
        return deepcopy(receipt)


def bind(authority, client, local):
    handoffs = StationHandoffAuthority(authority.store, station_uuid=authority.station_uuid,
        instance_uuid=authority.instance_uuid, event_epoch_uuid=authority.event_epoch_uuid, lab_uuid=uid())
    container = uid()
    handoffs.bind_container(container_uuid=container, local_carrier_material_uuid=local,
        barcode_namespace="test", barcode="SAMPLE-1")
    client.containers[container] = {"container_uuid": container, "lab_uuid": handoffs.lab_uuid,
        "barcode_namespace": "test", "barcode": "SAMPLE-1", "current_station_uuid": STATION,
        "state_version": 7, "active_handoff_item_uuid": None, "custody_status": "available", "attention": False}
    return handoffs


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    store = InventoryStore(str(tmp_path / "inventory.db"))
    backend = BackendResourceService(store)
    template = backend.sync_resource_templates([
        {"id": "test.output", "display_name": "物料", "registry_type": "material", "class": {}}
    ])["templates"][0]["uuid"]
    declared = outcomes.port()
    declared["resource_template_uuid"] = template
    monkeypatch.setattr(outcomes, "port", lambda **_: deepcopy(declared))
    yield store, backend, template
    store.close()


def complete(store, task, material, *, inventory_store=None):
    inventory = None
    if inventory_store is not None:
        from unilabos.app.scheduler.inventory.outbound import admit_produced_outputs, protect_outputs
        from unilabos.app.scheduler.inventory.service import InventoryService
        inventory = InventoryService(inventory_store)
        identities = material if isinstance(material, list) else [material]
        outputs = []
        for local in set(value for value in identities if isinstance(value, str)):
            row = inventory_store.query_one('SELECT resource_template_uuid FROM material WHERE uuid=?', (local,))
            if row is not None:
                outputs.append({'local_material_uuid':local,'resource_template_uuid':row['resource_template_uuid']})
        if outputs:
            admit_produced_outputs(inventory,task_uuid=task['uuid'],outputs=outputs)
    projection, job = _start(store, task)
    projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": material})
    if inventory is not None:
        for source in store.list_center_material_exports():
            if source['local_task_uuid'] == task['uuid'] and all(inventory_store.query_one('SELECT 1 FROM material WHERE uuid=?',(output['local_material_uuid'],)) for output in source['outputs']):
                # 此夹具显式模拟生产时原 Task 的来源准入及成功后的库存移交。
                protect_outputs(inventory_store,invocation_uuid=source['invocation_uuid'],task_uuid=task['uuid'],outputs=outputs)
                inventory.release_workflow(task['uuid'],reason='workflow_succeeded')
    return job


def test_inventory_version_mapping_survives_lost_response_and_restart(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        job = complete(store, task, local, inventory_store=inv)
        before = inv.query_one("SELECT aggregate_version FROM material WHERE uuid=?", (local,))["aggregate_version"]
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        client = Allocator()
        handoffs = bind(authority, client, local)
        client.lose_response = True
        bridge = MaterialBridge(client, store, authority, handoff_authority=handoffs)
        bridge.pump()
        assert identity in bridge.errors
        pending = authority.pending_intents()[0]
        assert pending["request"]["items"][0]["local_revision"] == before
        assert len(_events(store)) == 1
        # 原库存版本和申请在重启后保持不变。
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        authority.resume_export(identity, "evidence")  # 显式核实后立即重试，否则等待持久退避。
        MaterialBridge(client, store, authority, handoff_authority=handoffs).pump()
        assert authority.pending_intents() == []
        assert client.requests[0] == client.requests[1]
        assert len(_events(store)) == 2
        assert _events(store)[-1]["payload"]["material_refs"][0]["local_revision"] == before
        assert _events(store)[-1]["payload"]["material_refs"][0]["material_lineage_uuid"] == client.receipts[identity]["items"][0]["material_ref"]["material_lineage_uuid"]
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert len([j for j in store.list_jobs(task["uuid"]) if j["uuid"] == job]) == 1
        assert _events(store)[-1]["payload"]["container_refs"][0]["state_version"] == 7
        assert authority.completed_boundaries()[0]["containers"][0]["source_snapshot"]["adapter"] == "unilab.inventory.v1"


def test_terminal_recovery_does_not_reclaim_output_transferred_to_a_new_task(tmp_path, inventory):
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
    from unilabos.app.scheduler.inventory.outbound import transfer_to_handoff
    from unilabos.app.scheduler.inventory.service import InventoryService
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, _):
        complete(store, task, local, inventory_store=inv)
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        client = Allocator()
        handoffs = bind(authority, client, local)
        MaterialBridge(client, store, authority, handoff_authority=handoffs).pump()
        assert store.list_center_material_exports() == []
        with inv.transaction() as c:
            transfer_to_handoff(c, carrier_uuid=local, materials={local})
        inventory_service = InventoryService(inv)
        next_task = uid()
        inventory_service.admit_material_sources(next_task, [MaterialSourceAdmissionRequest(
            node_id="next-source", resource_template_uuid=template, custody_policy="task_exclusive",
            requirement=MaterialRequirement(template_id=template, instance_uuid=local),
        )])
        recovery = TaskSchedulerBridge(store, scheduler=EdgeScheduler(dispatcher=RecordingDispatcher(), inventory=inventory_service))
        try:
            recovery.recover_active_tasks()
            recovery.recover_active_tasks()
            released = inventory_service.release_workflow(next_task)
            assert released["released_bindings"] == ["next-source"]
        finally:
            recovery.close()


def test_boundary_retry_after_restart_keeps_outcome_and_original_snapshot(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        client = Allocator()
        handoffs = bind(authority, client, local)
        client.lose_boundary_response = True
        bridge = MaterialBridge(client, store, authority, handoff_authority=handoffs)
        bridge.pump()
        assert authority.pending_intents() == []
        assert identity in bridge.errors
        assert len(authority.pending_boundaries()) == 1
        events = _events(store)
        assert len(events) == 2 and events[-1]["event_type"] == "outcome"
        jobs = store.list_jobs(task["uuid"])
        # 中心当前容器投影变化后，补报仍使用已冻结正文，不能重读而改写旧边界。
        for container in client.containers.values():
            container["state_version"] += 5
        restored = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        restored.resume_export(identity, "boundary")
        MaterialBridge(client, store, restored, handoff_authority=handoffs).pump()
        assert client.boundary_requests[0] == client.boundary_requests[1]
        assert restored.pending_boundaries() == []
        MaterialBridge(client, store, restored, handoff_authority=handoffs).pump()
        assert len(client.boundary_requests) == 2
        assert _events(store) == events and store.list_jobs(task["uuid"]) == jobs


@pytest.mark.parametrize("changed", ["station_invocation_uuid", "owner_instance_uuid", "request_digest"])
def test_boundary_receipt_rejects_wrong_identity(changed):
    from unilabos.center_protocol.material import validate_boundary_receipt
    invocation = uid()
    request = {"task_payload_digest": "sha256:" + "a" * 64, "items": []}
    receipt = {"station_invocation_uuid": invocation, "owner_instance_uuid": INSTANCE, "request_digest": digest(request)}
    receipt[changed] = uid() if changed != "request_digest" else "sha256:" + "0" * 64
    with pytest.raises(MaterialAllocationError):
        validate_boundary_receipt(receipt, invocation_uuid=invocation, instance_uuid=INSTANCE, request=request)


def test_boundary_client_uses_historical_evidence_session(tmp_path):
    from tests.app.center_client.test_center_client import EPOCH, INVOCATION, SESSION, FakeTransport, config
    from unilabos.app.center_client.client import CenterClient, ControlSession

    request = {"task_payload_digest": "sha256:" + "a" * 64, "items": []}
    receipt = {"station_invocation_uuid": INVOCATION, "owner_instance_uuid": INSTANCE, "request_digest": digest(request)}
    transport = FakeTransport([receipt])
    client = CenterClient(config(tmp_path), transport)
    original_session = ControlSession(uid(), SESSION, 1, STATION, INSTANCE)
    client.restore_evidence_session(original_session)
    assert client.publish_material_boundary(INVOCATION, request, EPOCH) == receipt
    assert transport.calls == [{"method": "POST", "path": f"/api/v1/station-invocations/{INVOCATION}/material-boundary",
        "body": request, "headers": original_session.headers()}]
    assert client.session is None


def test_workflow_commit_failure_reuses_cached_mapping(tmp_path, inventory, monkeypatch):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        client = Allocator()
        handoffs = bind(authority, client, local)
        original = store.record_center_outcome_evidence
        monkeypatch.setattr(store, "record_center_outcome_evidence", lambda **_: (_ for _ in ()).throw(StoreConflict("模拟工作流提交失败")))
        MaterialBridge(client, store, authority, handoff_authority=handoffs).pump()
        assert authority.pending_intents()[0]["evidence"] is not None
        assert len(_events(store)) == 1
        monkeypatch.setattr(store, "record_center_outcome_evidence", original)
        authority.resume_export(identity, "evidence")
        MaterialBridge(client, store, authority, handoff_authority=handoffs).pump()
        assert len(client.requests) == 1
        assert authority.pending_intents() == []
        assert len(_events(store)) == 2
        # outcome 提交后丢失库存完成标记，再重放也不追加结果或任务。
        with inv.transaction() as c:
            c.execute("UPDATE station_material_export SET completed=0 WHERE invocation_uuid=?", (identity,))
        MaterialBridge(client, store, authority, handoff_authority=handoffs).pump()
        assert len(_events(store)) == 2
        assert len(client.requests) == 1


def test_unknown_inventory_does_not_allocate_or_reexecute(tmp_path, inventory):
    inv, _, _ = inventory
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, uid())
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        client = Allocator()
        bridge = MaterialBridge(client, store, authority)
        bridge.pump()
        assert identity in bridge.errors and client.requests == []
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert len(_events(store)) == 1


def test_wrong_template_and_owner_fail_before_intent(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, _):
        complete(store, task, local, inventory_store=inv)
        source = store.list_center_material_exports()[0]
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        bad = deepcopy(source)
        bad["outputs"][0]["resource_template_uuid"] = uid()
        with pytest.raises(MaterialAllocationError, match="模板"):
            authority.prepare(bad)
        bad = deepcopy(source)
        bad["owner_instance_uuid"] = uid()
        with pytest.raises(MaterialAllocationError, match="owner"):
            authority.prepare(bad)
        assert authority.pending_intents() == []
        # 已记录的真实输出不能借修复模板之名被另一份输出覆盖。
        with pytest.raises(MaterialAllocationError, match="冻结"):
            authority.prepare(source)
        source = {**source, "invocation_uuid": uid(), "local_task_uuid": uid()}
        authority.prepare(source)
        with pytest.raises(MaterialAllocationError, match="冻结"):
            authority.prepare(source, origins={local: {"kind": "derived", "operation": "split", "parents": [uid()]}})


def test_receipt_cannot_change_versions_identity_or_proposal(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        intent = authority.prepare(store.list_center_material_exports()[0])
        receipt = Allocator().allocate_material_lineages(identity, intent["request"], durable.event_epoch_uuid)
        for key, value in [("local_revision", 999), ("local_material_uuid", uid()), ("handoff_item_uuid", uid())]:
            bad = deepcopy(receipt)
            bad["items"][0]["material_ref"][key] = value
            with pytest.raises(MaterialAllocationError):
                authority.remember_response(identity, bad)
        authority.remember_response(identity, receipt)
        bad = deepcopy(receipt)
        bad["items"][0]["material_ref"]["material_lineage_uuid"] = uid()
        with pytest.raises(MaterialAllocationError, match="冻结"):
            authority.remember_response(identity, bad)
        assert validate_allocation_receipt(receipt, invocation_uuid=identity, instance_uuid=INSTANCE,
            station_uuid=STATION, request=intent["request"]) == receipt


def test_inventory_drift_keeps_original_mapping_pending(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        client = Allocator()
        handoffs = bind(authority, client, local)
        client.lose_response = True
        MaterialBridge(client, store, authority, handoff_authority=handoffs).pump()
        backend.update_material(local, {"name": "changed after output"})
        authority.resume_export(identity, "evidence")
        bridge = MaterialBridge(client, store, authority, handoff_authority=handoffs)
        bridge.pump()
        assert identity in bridge.errors
        pending = authority.pending_intents()[0]
        assert pending["response"] is not None and pending["evidence"] is None
        assert len(client.receipts) == 1
        assert client.requests[0] == client.requests[1]
        assert len(_events(store)) == 1
        assert store.get_task(task["uuid"])["status"] == "succeeded"


def test_unbound_container_keeps_mapping_and_waits_for_configuration(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, local, inventory_store=inv)
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        client = Allocator()
        bridge = MaterialBridge(client, store, authority)
        bridge.pump()
        assert identity in bridge.errors
        assert authority.pending_intents()[0]["response"] is not None
        handoffs = bind(authority, client, local)
        authority.resume_export(identity, "evidence")
        MaterialBridge(client, store, authority, handoff_authority=handoffs).pump()
        assert len(client.requests) == 1 and len(_events(store)) == 2


@pytest.mark.parametrize("shape", ["many", "two_ports"])
@pytest.mark.parametrize("existing_intent", [False, True])
def test_duplicate_output_is_persistently_isolated_from_independent_exports(tmp_path, inventory, shape, existing_intent):
    from unilabos.app.center_client.client import TaskPackage
    from unilabos.app.center_client.runtime import submission
    from unilabos.workflow.published_contract import PublishedWorkflowContractStore
    from unilabos.workflow.store import WorkflowStore

    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, service, durable, package, valid, scheduler, identity):
        complete(store, valid, local, inventory_store=inv)
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        client = Allocator()
        handoffs = bind(authority, client, local)
        if existing_intent:
            authority.prepare(store.list_center_material_exports()[0])
        # 通过正式发布、提交和 Job 完成产生重复边界，不能靠修改结果表造坏数据。
        graph = deepcopy(store.get_task(valid["uuid"])["workflow_snapshot"])
        workflow = uid()
        graph["workflow"].update(uuid=workflow, revision=1)
        for node in graph["nodes"]:
            node["workflow_uuid"] = workflow
        metadata = graph["workflow"]["meta_data"]["unilab"]
        if shape == "many":
            schema = {"type": "array", "items": {"type": "string"}}
            metadata["output_contract"]["outputs"][0]["schema"] = schema
            metadata["material_ports"][0]["cardinality"] = "many"
            graph["node_templates"][0]["meta_data"]["unilab"]["action_contract_schema"]["properties"]["result"]["properties"]["result"] = schema
            for handle in graph["handle_templates"]:
                if handle["io_type"] == "source":
                    handle.update(type="array", meta_data={"unilab": {"value_schema": schema}})
            result = [uid()] * 2
        else:
            metadata["output_contract"]["outputs"].append({"name": "result2", "schema": {"type": "string"}})
            metadata["output_bindings"]["result2"] = deepcopy(metadata["output_bindings"]["result"])
            metadata["material_ports"].append({**metadata["material_ports"][0], "name": "other", "value_path": "$.result2"})
            result = uid()
        store.create_workflow(workflow_uuid=workflow, name="Duplicate output", tags=[], description=None, meta_data={})
        frozen = PublishedWorkflowContractStore(store).publish(graph=graph, expected_revision=1)
        payload = deepcopy(dict(package.raw))
        bad_id = uid()
        payload.update(workflow_uuid=workflow, revision_fingerprint=frozen["source_hash"], station_invocation_uuid=bad_id,
                       invocation_key=payload["global_workflow_revision_uuid"] + ":" + uid())
        bad_package = TaskPackage.from_response({"payload": payload, "payload_digest": digest(payload)})
        durable.remember_package(bad_package)
        durable.confirm_claim(bad_id)
        bad = service.submit_station_workflow(**submission(bad_package), center_invocation_uuid=bad_id)
        complete(store, bad, result)
        assert store.get_center_outcome_evidence_status(bad_id)["pending_reason"] == "material_evidence_required"
        submitted = list(scheduler.submissions)
        jobs = {task["uuid"]: store.list_jobs(task["uuid"]) for task in (bad, valid)}

        MaterialBridge(client, store, authority, handoff_authority=handoffs).pump()
        assert store.get_center_outcome_evidence_status(bad_id)["pending_reason"] == "material_output_invalid"
        assert authority.pending_intents() == []
        assert len(client.requests) == 1
        assert any(e["station_invocation_uuid"] == identity and e["event_type"] == "outcome" for e in _events(store))
        assert not any(e["station_invocation_uuid"] == bad_id and e["event_type"] == "outcome" for e in _events(store))
        assert store.get_task(bad["uuid"])["status"] == "succeeded"
        assert scheduler.submissions == submitted
        assert {task["uuid"]: store.list_jobs(task["uuid"]) for task in (bad, valid)} == jobs

        # 重新打开工作流数据库并重建桥，隔离标记不会变回可执行或阻塞扫描。
        reopened = WorkflowStore(store.path)
        try:
            restored = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
            MaterialBridge(client, reopened, restored, handoff_authority=handoffs).pump()
            assert reopened.get_center_outcome_evidence_status(bad_id)["pending_reason"] == "material_output_invalid"
            assert reopened.list_center_material_exports() == []
            assert len(client.requests) == 1
            assert reopened.get_task(bad["uuid"])["status"] == "succeeded"
        finally:
            reopened.close()


def test_raw_uuid_output_without_original_admission_cannot_allocate_or_export(tmp_path, inventory):
    inv,backend,template=inventory
    local=backend.create_material({'resource_template_uuid':template,'name':'passthrough','barcode':'SAMPLE-1'})['uuid']
    with outcomes.material_workflow(tmp_path) as (store,_,durable,_,task,_,identity):
        complete(store,task,local)
        authority=InventoryMaterialAuthority(inv,station_uuid=STATION,instance_uuid=INSTANCE,event_epoch_uuid=durable.event_epoch_uuid)
        client=Allocator()
        handoffs=bind(authority,client,local)
        MaterialBridge(client,store,authority,handoff_authority=handoffs).pump()
        failure=authority.export_failure(identity,'prepare')
        assert failure['status']=='blocked'
        assert '独占保护' in failure['last_error']
        assert not client.requests
        assert authority.pending_intents()==[]
        assert store.get_task(task['uuid'])['status']=='succeeded'


def test_completed_outbound_can_transfer_to_verified_local_history_input_atomically(tmp_path, inventory):
    from unilabos.app.scheduler.inventory.outbound import transfer_to_material_input
    inv,backend,template=inventory
    local=backend.create_material({'resource_template_uuid':template,'name':'history','barcode':'SAMPLE-1'})['uuid']
    with outcomes.material_workflow(tmp_path) as (store,_,durable,_,task,_,identity):
        complete(store,task,local,inventory_store=inv)
        authority=InventoryMaterialAuthority(inv,station_uuid=STATION,instance_uuid=INSTANCE,event_epoch_uuid=durable.event_epoch_uuid)
        client=Allocator()
        handoffs=bind(authority,client,local)
        MaterialBridge(client,store,authority,handoff_authority=handoffs).pump()
        evidence=authority.completed_boundaries()[0]['evidence']
        with pytest.raises(RuntimeError),inv.transaction() as c:
            transfer_to_material_input(c,references=evidence['material_refs'],container_refs=evidence['container_refs'])
            assert c.execute("SELECT status FROM inventory_reservation WHERE workflow_id=?",(identity,)).fetchone()['status']=='released'
            raise RuntimeError('新输入保护写入失败，原产物保护必须回滚')
        assert inv.query_one("SELECT 1 FROM inventory_reservation WHERE workflow_id=? AND status='active'",(identity,))
        with inv.transaction() as c:
            transfer_to_material_input(c,references=evidence['material_refs'],container_refs=evidence['container_refs'])
        assert inv.query_one("SELECT 1 FROM inventory_reservation WHERE workflow_id=? AND status='released'",(identity,))
