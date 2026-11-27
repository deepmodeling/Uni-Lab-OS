"""真实到货、跨库预留身份与库存派发互斥；不制造 Task 或 Job 夹具。"""

import json
from copy import deepcopy

import pytest

from tests.app.center_client.test_center_client import package_response
from tests.app.test_station_handoff import add_second_item, frozen, handoff_inventory_fixture, package, receive, uid
from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionRequest,
    DispatchResource,
    InventoryMutationConflict,
    acquire_dispatch_permit,
    assert_inventory_mutation_unclaimed,
)
from unilabos.app.scheduler.inventory.material_inputs import MaterialInputError, StationMaterialInputAuthority
from unilabos.app.scheduler.resource_lock import device_lock_key, material_lock_key
from unilabos.center_protocol.digest import digest


@pytest.fixture
def received(tmp_path):
    for s in handoff_inventory_fixture(tmp_path):
        body = package_response()
        s["payload"].update(global_task_uuid=body["payload"]["global_task_uuid"], transfer_definition_uuid=uid(),
                            transfer_source_kind="node_output")
        frozen(s)
        receipt = receive(s)
        s["payload"]["items"][0].update(receipt=receipt, current_state_version=3)
        s["payload"].update(status="received", revision=4)
        for authority in s["auth"].values():
            authority.apply_center_snapshot(package(s["payload"]))
        reference = receipt["target_material_refs"][0]
        local = reference["local_material_uuid"]
        body["payload"].update(
            station_uuid=s["ids"]["target"], input={"sample": local}, handoff_uuids=[s["ids"]["handoff"]],
            material_refs=[{**reference, "handoff_item_uuid": s["ids"]["item"]}],
            container_refs=[{"container_uuid": s["ids"]["container"], "state_version": 3}],
        )
        body["payload_digest"] = digest(body["payload"])
        s.update(body=body, local=local, inputs=[{"local_material_uuid": local,
                 "resource_template_uuid": s["templates"]["target"]["test.carrier"]}],
                 authority=StationMaterialInputAuthority(s["auth"]["target"]))
        yield s


def permit(c, task, local, *, device=False, receipt=None):
    key = device_lock_key(local) if device else material_lock_key(local)
    return acquire_dispatch_permit(c, DispatchAdmissionRequest(
        effect_uuid=uid(), task_uuid=task, job_uuid=uid(), attempt=1, parameter_hash="hash",
        material_input_receipt=receipt,
        expected_change_set={}, resources=(DispatchResource(key, "device" if device else "material", local),),
    ))


@pytest.fixture
def received_containers(tmp_path):
    """同一张交接收到两只容器，第一只包含载体与子物料。"""
    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
    from unilabos.app.scheduler.inventory.handoff_adapter import ADAPTER

    for s in handoff_inventory_fixture(tmp_path):
        body = package_response()
        s["payload"].update(global_task_uuid=body["payload"]["global_task_uuid"], transfer_definition_uuid=uid(),
                            transfer_source_kind="station_material")
        _, second_item, _ = add_second_item(s)
        child = BackendResourceService(s["stores"]["source"]).create_material({
            "resource_template_uuid": s["templates"]["source"]["test.carrier"], "name": "sample",
            "parent_uuid": s["carrier"]["uuid"],
        })
        lineage = uid()
        s["payload"]["items"][0]["source_material_refs"].append({
            "station_uuid": s["ids"]["source"], "local_material_uuid": child["uuid"],
            "local_revision": child["revision"], "material_lineage_uuid": lineage,
        })
        for ref in s["payload"]["items"][0]["source_material_refs"]:
            ref["local_revision"] = s["stores"]["source"].query_one(
                "SELECT aggregate_version FROM material WHERE uuid=?", (ref["local_material_uuid"],)
            )["aggregate_version"]
        s["plans"][s["ids"]["item"]]["lineage_template_uuids"][lineage] = s["templates"]["target"]["test.carrier"]
        for item in s["payload"]["items"]:
            item["source_snapshot"] = s["auth"]["source"].export_source_snapshot(
                container_uuid=item["container_uuid"], source_material_refs=item["source_material_refs"]
            )
            s["plans"][item["handoff_item_uuid"]]["adapter"] = {
                "kind": ADAPTER, "site_names": {r["local_material_uuid"]: {} for r in item["source_material_refs"]},
                "reagent_info_uuids": {},
            }
        frozen(s)
        receipts = [receive(s), s["auth"]["target"].receive(
            s["ids"]["handoff"], second_item, barcode_namespace="lab", barcode="BKR-2", evidence_refs=["scan:2"]
        )]
        for item, receipt in zip(s["payload"]["items"], receipts):
            item.update(receipt=receipt, current_state_version=3)
        s["payload"].update(status="received", revision=4)
        for authority in s["auth"].values():
            authority.apply_center_snapshot(package(s["payload"]))
        packages, inputs = [], []
        for item, receipt in zip(s["payload"]["items"], receipts):
            selected = deepcopy(body)
            local = receipt["target_material_refs"][0]["local_material_uuid"]
            selected["payload"].update(
                station_invocation_uuid=uid(), station_uuid=s["ids"]["target"], input={"sample": local},
                handoff_uuids=[s["ids"]["handoff"]],
                material_refs=[{**r, "handoff_item_uuid": item["handoff_item_uuid"]} for r in receipt["target_material_refs"]],
                container_refs=[{"container_uuid": item["container_uuid"], "state_version": 3}],
            )
            selected["payload_digest"] = digest(selected["payload"])
            packages.append(selected)
            inputs.append([{"local_material_uuid": local, "resource_template_uuid": s["templates"]["target"]["test.carrier"]}])
        s.update(packages=packages, inputs=inputs, authority=StationMaterialInputAuthority(s["auth"]["target"]))
        yield s


def test_separate_calls_consume_complete_selected_containers_independently(received_containers):
    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService

    s = received_containers
    first, second = s["packages"]
    first_receipt = s["authority"].prepare(first, input_materials=s["inputs"][0])
    s["authority"].release(first, first_receipt)
    first_local = first["payload"]["material_refs"][0]["local_material_uuid"]
    BackendResourceService(s["stores"]["target"]).update_material(first_local, {"name": "processed"})
    second_receipt = s["authority"].prepare(second, input_materials=s["inputs"][1])
    assert second_receipt["local_task_uuid"] != first_receipt["local_task_uuid"]
    held = s["stores"]["target"].query_all(
        "SELECT material_uuid FROM station_material_input_resource WHERE invocation_uuid=?",
        (second["payload"]["station_invocation_uuid"],),
    )
    assert {row["material_uuid"] for row in held} == {second["payload"]["material_refs"][0]["local_material_uuid"]}
    assert s["authority"].prepare(second, input_materials=s["inputs"][1]) == second_receipt


def test_selected_container_cannot_omit_its_other_materials(received_containers):
    s = received_containers
    partial = deepcopy(s["packages"][0])
    assert len(partial["payload"]["material_refs"]) == 2
    partial["payload"]["material_refs"] = partial["payload"]["material_refs"][:1]
    partial["payload_digest"] = digest(partial["payload"])
    with pytest.raises(MaterialInputError, match="精确覆盖"):
        s["authority"].prepare(partial, input_materials=s["inputs"][0])
    assert s["stores"]["target"].query_one("SELECT 1 FROM station_material_input") is None


def test_input_hold_survives_restart_and_only_fixed_task_can_dispatch(received):
    s = received
    store, authority = s["stores"]["target"], s["authority"]
    receipt = authority.prepare(s["body"], input_materials=s["inputs"])
    restarted = StationMaterialInputAuthority(s["auth"]["target"])
    assert restarted.prepare(s["body"], input_materials=s["inputs"]) == receipt
    assert store.query_one("SELECT count(*) n FROM station_material_input")["n"] == 1
    with pytest.raises(InventoryMutationConflict), store.transaction() as c:
        assert_inventory_mutation_unclaimed(c, material_uuids=[s["local"]])
    with pytest.raises(Exception, match="active claim"), store.transaction() as c:
        c.execute("INSERT INTO resource_relation(child_uuid,parent_uuid,slot_id) VALUES (?,?,'')", (s["local"], uid()))
    with store.transaction() as c:
        for device in (False, True):
            decision = permit(c, uid(), s["local"], device=device)
            assert not decision.acquired
            assert decision.blocking_task_uuid == receipt["local_task_uuid"]
        assert permit(c, receipt["local_task_uuid"], s["local"], receipt=receipt).acquired


def test_transport_call_keeps_independent_inventory_admission_and_fixed_task(received):
    """配送许可不替代本地物料准入，也不能替换原库存占用分配的 Task。"""
    from tests.app.center_client.test_transport_workflow import context

    s = received
    transport = context()
    transport["logistics_station_uuid"] = s["ids"]["target"]
    s["body"]["payload"]["input"]["center_transport"] = json.dumps(transport)
    s["body"]["payload"]["transport"] = transport
    s["body"]["payload_digest"] = digest(s["body"]["payload"])
    receipt = s["authority"].prepare(s["body"], input_materials=s["inputs"])
    assert s["authority"].prepare(s["body"], input_materials=s["inputs"]) == receipt
    with s["stores"]["target"].transaction() as c:
        assert not permit(c, uid(), s["local"]).acquired
        assert permit(c, receipt["local_task_uuid"], s["local"], receipt=receipt).acquired


@pytest.mark.parametrize("fault", ["version", "template", "missing_material", "container_version", "foreign_station", "protected"])
def test_wrong_or_changed_stock_never_creates_hold(received, fault):
    s = received
    if fault == "version":
        with s["stores"]["target"].transaction() as c:
            c.execute("UPDATE material SET aggregate_version=aggregate_version+1 WHERE uuid=?", (s["local"],))
    elif fault == "template":
        s["inputs"][0]["resource_template_uuid"] = uid()
    elif fault == "missing_material":
        s["body"]["payload"]["material_refs"] = []
    elif fault == "container_version":
        s["body"]["payload"]["container_refs"][0]["state_version"] += 1
    elif fault == "foreign_station":
        s["body"]["payload"]["material_refs"][0]["station_uuid"] = s["ids"]["source"]
    else:
        with s["stores"]["target"].transaction() as c:
            c.execute("UPDATE material SET inventory_status='handoff_quarantined' WHERE uuid=?", (s["local"],))
    s["body"]["payload_digest"] = digest(s["body"]["payload"])
    with pytest.raises(MaterialInputError):
        s["authority"].prepare(s["body"], input_materials=s["inputs"])
    assert s["stores"]["target"].query_one("SELECT count(*) n FROM station_material_input")["n"] == 0


def test_other_invocation_and_preexisting_job_cannot_take_same_material(received):
    s = received
    receipt = s["authority"].prepare(s["body"], input_materials=s["inputs"])
    other = deepcopy(s["body"])
    other["payload"]["station_invocation_uuid"] = uid()
    other["payload_digest"] = digest(other["payload"])
    with pytest.raises(InventoryMutationConflict):
        s["authority"].prepare(other, input_materials=s["inputs"])
    s["authority"].release(s["body"], receipt)
    with s["stores"]["target"].transaction() as c:
        assert permit(c, uid(), s["local"]).acquired
    with pytest.raises(InventoryMutationConflict):
        s["authority"].prepare(other, input_materials=s["inputs"])


def test_missing_hold_and_released_hold_cannot_be_recreated_as_execution_authority(received):
    s = received
    receipt = s["authority"].prepare(s["body"], input_materials=s["inputs"])
    s["authority"].release(s["body"], receipt)
    s["authority"].release(s["body"], receipt)
    s["authority"].verify(s["body"], receipt, allow_released=True)
    with pytest.raises(MaterialInputError):
        s["authority"].verify(s["body"], receipt)
    with pytest.raises(MaterialInputError):
        s["authority"].prepare(s["body"], input_materials=s["inputs"])
    with s["stores"]["target"].transaction() as c:
        c.execute("DELETE FROM station_material_input_resource")
    with pytest.raises(MaterialInputError, match="资源明细"):
        s["authority"].verify(s["body"], receipt, allow_released=True)


def test_task_dispatch_keeps_requiring_original_inventory_hold_after_inventory_loss(received):
    s = received
    receipt = s["authority"].prepare(s["body"], input_materials=s["inputs"])
    with s["stores"]["target"].transaction() as c:
        c.execute("DELETE FROM station_material_input_resource")
        c.execute("DELETE FROM station_material_input")
        decision = permit(c, receipt["local_task_uuid"], s["local"], receipt=receipt)
        assert not decision.acquired
        assert decision.wait_code == "material_input_missing"


def test_resolved_new_movement_uses_current_template_proof_and_keeps_original_receipt(received):
    from tests.app.test_station_handoff import settlement_payload
    from tests.app.test_station_handoff_movement import finish, reopen, verify

    s = received
    original = deepcopy(s["payload"]["items"][0]["receipt"])
    s["payload"] = settlement_payload(s, decision="target_received", proof=original["receipt_digest"])
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))
    reopen(s)
    verify(s, "source", False)
    verify(s, "target", True)
    finish(s, "target")
    item = s["payload"]["items"][0]
    current = item["movement_review"]["target_verification"]
    assert current["inventory_snapshot"]["materials"][0]["resource_template_uuid"] == s["inputs"][0]["resource_template_uuid"]
    assert item["receipt"] == original
    s["body"]["payload"]["material_refs"] = [{**r, "handoff_item_uuid": item["handoff_item_uuid"]} for r in current["material_refs"]]
    s["body"]["payload"]["container_refs"][0]["state_version"] = item["current_state_version"]
    s["body"]["payload_digest"] = digest(s["body"]["payload"])
    assert s["authority"].prepare(s["body"], input_materials=s["inputs"])
