"""未冻结源交接的取消仍须收尾原成功任务的出站责任。"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.app.center_client import test_material_outcomes as outcomes
from tests.app.center_client.test_center_client import INSTANCE, STATION
from tests.app.center_client.test_material_exports import Allocator, complete, inventory  # noqa: F401
from tests.app.test_station_handoff import handoff_inventory_fixture, package, prepared, uid
from unilabos.app.center_client.handoff import configure_handoffs
from unilabos.app.center_client.material import MaterialBridge
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.content_contract import BackendContainerContentService
from unilabos.app.scheduler.inventory.domain import (
    MaterialClaimedByOtherTask,
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError
from unilabos.app.scheduler.inventory.handoff_adapter import ADAPTER
from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
from unilabos.app.scheduler.inventory.outbound import protect_outputs
from unilabos.app.scheduler.inventory.service import InventoryService


@pytest.fixture
def outbound_handoff(tmp_path, inventory, request, monkeypatch):
    """正式成功边界、出站保护、目标预留及生产桥装配；仅设备与中心传输用协议夹具。"""
    source, backend, template = inventory
    backend.sync_resource_templates([{"id": "test.output", "display_name": "物料", "registry_type": "material", "class": {}, "category": ["container"], "metadata": {"capacity": {"max_volume_ul": 10000}}}])
    fixture = handoff_inventory_fixture(tmp_path)
    s = next(fixture)
    BackendResourceService(s["stores"]["target"]).sync_resource_templates([{"id": "test.carrier", "display_name": "载体", "registry_type": "material", "class": {}, "category": ["container"], "metadata": {"capacity": {"max_volume_ul": 10000}}}])
    rack = backend.create_material({"resource_template_uuid": template, "name": "源仓库"})
    with source.transaction() as c:
        c.execute("INSERT INTO site(uuid,create_time,update_time,material_uuid,name) VALUES (?,'2026-01-01','2026-01-01',?,'source')",
                  (s["sites"]["source"], rack["uuid"]))
    carrier = backend.create_material({"resource_template_uuid": template, "name": "样品", "barcode": "BKR-1",
        "site_placement": {"action": "place", "site_uuid": s["sites"]["source"]}})
    sample = BackendContainerContentService(source).create_sample({"material_uuid": carrier["uuid"],
        "code": "SAMPLE-1", "name": "原样品", "quantity": 2.5, "quantity_unit": "ml"})
    other = None
    if getattr(request, "param", 1) == 2:
        other = backend.create_material({"resource_template_uuid": template, "name": "另一个产物", "barcode": "BKR-2"})
        declared = outcomes.port()
        declared.update(cardinality="many")
        monkeypatch.setattr(outcomes, "port", lambda **_: deepcopy(declared))
    s["stores"]["source"].close()
    s["stores"]["source"] = source
    s["carrier"] = carrier
    path = tmp_path / "workflow"
    path.mkdir()
    try:
        shape = {"output_schema": {"type": "array", "items": {"type": "string"}}, "cardinality": "many"} if other else {}
        with outcomes.material_workflow(path, **shape) as (workflow, service, durable, task_package, task, scheduler, invocation):
            center = Allocator()
            runtime = SimpleNamespace(store=durable, service=service, client=center, install_handoff_bridge=lambda build: build())
            bridge = configure_handoffs(runtime, source, lab_uuid=s["ids"]["lab"])
            handoffs = s["auth"]["source"] = bridge.authority
            s["ids"].update(source=STATION, source_instance=INSTANCE, source_epoch=durable.event_epoch_uuid)
            handoffs.bind_container(container_uuid=s["ids"]["container"], local_carrier_material_uuid=carrier["uuid"],
                barcode_namespace="lab", barcode="BKR-1")
            other_container = uid()
            if other:
                handoffs.bind_container(container_uuid=other_container, local_carrier_material_uuid=other["uuid"],
                    barcode_namespace="lab", barcode="BKR-2")
            complete(workflow, task, [carrier["uuid"], other["uuid"]] if other else carrier["uuid"], inventory_store=source)
            authority = InventoryMaterialAuthority(source, station_uuid=STATION, instance_uuid=INSTANCE,
                event_epoch_uuid=durable.event_epoch_uuid)
            center.containers[s["ids"]["container"]] = {**s["details"][s["ids"]["container"]],
                "current_station_uuid": STATION, "state_version": 1, "active_handoff_item_uuid": None, "custody_status": "available"}
            if other:
                center.containers[other_container] = {**center.containers[s["ids"]["container"]],
                    "container_uuid": other_container, "barcode": "BKR-2"}
            materials = MaterialBridge(center, workflow, authority, handoff_authority=handoffs)
            materials.pump()
            assert not materials.errors
            container = next(item for item in authority.completed_boundaries()[0]["containers"]
                             if item["container_uuid"] == s["ids"]["container"])
            s["payload"].update(source_station_uuid=STATION, source_instance_uuid=INSTANCE,
                global_task_uuid=task_package.global_task_uuid, transfer_definition_uuid=uid(), transfer_source_kind="node_output")
            s["payload"]["items"][0].update(source_material_refs=container["source_material_refs"], source_snapshot=container["source_snapshot"])
            s["details"][s["ids"]["container"]]["current_station_uuid"] = STATION
            lineage = container["source_material_refs"][0]["material_lineage_uuid"]
            s["plans"][s["ids"]["item"]].update(carrier_lineage_uuid=lineage,
                lineage_template_uuids={lineage: s["templates"]["target"]["test.carrier"]},
                adapter={"kind": ADAPTER, "site_names": {carrier["uuid"]: {}}, "reagent_info_uuids": {}})
            prepared(s)
            yield SimpleNamespace(s=s, workflow=workflow, task=task, invocation=invocation, package=task_package,
                inventory=InventoryService(source), template=template, bridge=bridge, backend=backend, sample=sample, other=other)
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def cancel(r):
    r.s["payload"].update(status="canceling", revision=r.s["payload"]["revision"] + 1)
    for authority in r.s["auth"].values():
        authority.apply_center_snapshot(package(r.s["payload"]))


def admit(r, task=None):
    task = task or uid()
    return r.inventory.admit_material_sources(task, [MaterialSourceAdmissionRequest(node_id=uid(),
        resource_template_uuid=r.template, custody_policy="task_exclusive",
        requirement=MaterialRequirement(template_id=r.template, instance_uuid=r.s["carrier"]["uuid"]))])


def test_prepared_unfrozen_cancel_releases_original_outbound_and_preserves_success(outbound_handoff):
    r = outbound_handoff
    before_result = r.workflow.get_station_workflow_result(r.task["uuid"])
    before_jobs = r.workflow.list_jobs(r.task["uuid"])
    cancel(r)
    admit(r)
    assert r.workflow.get_task(r.task["uuid"])["status"] == "succeeded"
    assert r.workflow.get_station_workflow_result(r.task["uuid"]) == before_result
    assert r.workflow.list_jobs(r.task["uuid"]) == before_jobs
    assert r.inventory.store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (r.s["sites"]["source"],))["occupied_material_uuid"] == r.s["carrier"]["uuid"]


@pytest.mark.parametrize("final_status", ["canceling", "canceled"])
def test_cancel_proof_replay_never_reads_or_releases_later_outbound(outbound_handoff, final_status):
    r = outbound_handoff
    cancel(r)
    next_task, next_invocation = uid(), uid()
    admit(r, next_task)
    protect_outputs(r.inventory.store, invocation_uuid=next_invocation, task_uuid=next_task,
        outputs=[{"local_material_uuid": r.s["carrier"]["uuid"], "resource_template_uuid": r.template}])
    r.inventory.release_workflow(next_task, reason="workflow_succeeded")
    before = r.inventory.store.query_all("SELECT * FROM inventory_reservation WHERE workflow_id=?", (next_invocation,))
    original_acks = [event for event in r.s["auth"]["source"].pending_intents() if event["payload"]["kind"] == "cancel_ack"]
    def unrelated_context(identity):
        raise AssertionError("旧取消证明重放不应读取后来任务的上下文")
    r.s["auth"]["source"].source_context_resolver = unrelated_context
    replay = deepcopy(r.s["payload"])
    replay.update(status=final_status, revision=replay["revision"] + 1)
    r.s["auth"]["source"].apply_center_snapshot(package(replay))
    assert r.inventory.store.query_all("SELECT * FROM inventory_reservation WHERE workflow_id=?", (next_invocation,)) == before
    assert [event for event in r.s["auth"]["source"].pending_intents() if event["payload"]["kind"] == "cancel_ack"] == original_acks


def test_unversioned_sample_drift_cannot_release_original_outbound(outbound_handoff):
    r = outbound_handoff
    # 模拟旧存储/恢复导致内容事实漂移而物料修订未变；正常写接口会拒绝活动出站占用。
    with r.inventory.store.transaction() as c:
        c.execute("UPDATE container_content SET quantity=99 WHERE uuid=?", (r.sample["uuid"],))
    with pytest.raises(HandoffInventoryError, match="内容已变化"):
        cancel(r)
    assert not [event for event in r.s["auth"]["source"].pending_intents() if event["payload"]["kind"] == "cancel_ack"]


def test_frozen_source_keeps_existing_handoff_cancel_path(outbound_handoff):
    r = outbound_handoff
    event = r.s["auth"]["source"].freeze_source(package(r.s["payload"]), physical_ready=True, evidence_refs=["operator:ready"])
    r.s["payload"].update(status="released", revision=3,
        released={"event_epoch_uuid": r.s["ids"]["source_epoch"],
                  **{key: event["payload"][key] for key in ("release_uuid", "evidence_refs")}})
    cancel(r)
    admit(r)


@pytest.mark.parametrize("change", ["global_task", "ref", "snapshot", "center_version", "missing_context", "inventory_revision"])
def test_unverified_original_cancellation_keeps_protection_without_ack(outbound_handoff, change):
    r = outbound_handoff
    if change == "global_task":
        r.s["payload"]["global_task_uuid"] = uid()
    elif change == "ref":
        r.s["payload"]["items"][0]["source_material_refs"][0]["local_revision"] += 1
    elif change == "snapshot":
        r.s["payload"]["items"][0]["source_snapshot"]["materials"][0]["barcode"] = "CHANGED"
    elif change == "center_version":
        for key in ("expected_state_version", "reserved_state_version", "current_state_version"):
            r.s["payload"]["items"][0][key] += 1
    elif change == "missing_context":
        r.s["auth"]["source"].source_context_resolver = None
    else:
        # 模拟恢复后库存修订已与原证明分叉；不通过取消刷新旧证明。
        with r.inventory.store.transaction() as c:
            c.execute("UPDATE material SET aggregate_version=aggregate_version+1 WHERE uuid=?", (r.s["carrier"]["uuid"],))
    before = r.inventory.store.query_all("SELECT * FROM inventory_reservation")
    with pytest.raises(HandoffInventoryError):
        cancel(r)
    assert r.inventory.store.query_all("SELECT * FROM inventory_reservation") == before
    assert not [event for event in r.s["auth"]["source"].pending_intents() if event["payload"]["kind"] == "cancel_ack"]


@pytest.mark.parametrize("condition", ["station_material", "attention", "released", "permit"])
def test_other_source_or_uncertain_movement_never_releases_outbound(outbound_handoff, condition):
    r = outbound_handoff
    payload = deepcopy(r.s["payload"])
    payload.update(status="canceling", revision=3)
    if condition == "station_material":
        payload["transfer_source_kind"] = "station_material"
    elif condition == "attention":
        payload.update(attention=True, attention_reasons=["physical-location-review"])
    elif condition == "released":
        payload["released"] = {"event_epoch_uuid": r.s["ids"]["source_epoch"], "release_uuid": uid(), "evidence_refs": ["original:source-ready"]}
    else:
        payload["permit"] = {"authorization_uuid": uid(), "logistics_subject_uuid": uid()}
    before = r.inventory.store.query_all("SELECT * FROM inventory_reservation")
    r.s["auth"]["source"].apply_center_snapshot(package(payload))
    assert r.inventory.store.query_all("SELECT * FROM inventory_reservation") == before
    with pytest.raises(MaterialClaimedByOtherTask):
        admit(r)


@pytest.mark.parametrize("outbound_handoff", [2], indirect=True)
def test_cancel_one_container_preserves_other_output_of_same_invocation(outbound_handoff):
    r = outbound_handoff
    cancel(r)
    admit(r)
    with pytest.raises(MaterialClaimedByOtherTask):
        r.inventory.admit_material_sources(uid(), [MaterialSourceAdmissionRequest(node_id=uid(),
            resource_template_uuid=r.template, custody_policy="task_exclusive",
            requirement=MaterialRequirement(template_id=r.template, instance_uuid=r.other["uuid"]))])


def test_legacy_unprepared_cancel_replay_settles_once_without_replacing_ack(outbound_handoff, monkeypatch):
    from unilabos.app.scheduler.inventory import handoff_outbound_cancel

    r = outbound_handoff
    # 只在第一次应用模拟升级前没有产物收尾的旧分支；墓碑和 ACK 仍由真实交接权威写入。
    with monkeypatch.context() as old:
        old.setattr(handoff_outbound_cancel, "settle_outbound_cancellation", lambda *args: None)
        cancel(r)
    original = r.s["auth"]["source"].pending_intents()
    with pytest.raises(MaterialClaimedByOtherTask):
        admit(r)
    r.s["auth"]["source"].apply_center_snapshot(package(r.s["payload"]))
    next_task = uid()
    admit(r, next_task)
    r.s["auth"]["source"].apply_center_snapshot(package(r.s["payload"]))
    assert r.s["auth"]["source"].pending_intents() == original
    assert r.inventory.store.query_one("SELECT status FROM inventory_material_source_binding WHERE workflow_id=?", (next_task,))["status"] == "active"


@pytest.mark.parametrize("legacy", [False, True])
def test_original_source_context_is_read_only_and_never_rebuilds_accepted(outbound_handoff, legacy):
    r = outbound_handoff
    if legacy:
        # 旧账本缺原 accepted 时只能保留责任，不能为自动取消补建事件历史。
        with r.workflow.transaction() as c:
            c.execute("UPDATE center_invocation_inbox SET acceptance_version=0 WHERE invocation_uuid=?", (r.invocation,))
            c.execute("DELETE FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=0", (r.invocation,))
    def facts():
        return [[dict(row) for row in r.workflow.db.execute(f"SELECT * FROM {table}").fetchall()]
                for table in ("center_invocation_inbox", "center_boundary_outbox", "center_runtime_state")]
    before = facts()
    context = r.workflow.center_material_source_context(r.invocation)
    assert (context is None) == legacy
    assert facts() == before
