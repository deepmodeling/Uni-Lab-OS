"""取货证明解除来源物理落位，物料交接保护及冻结历史保持。"""
from __future__ import annotations

from copy import deepcopy

import pytest

from tests.app.test_station_handoff import frozen, package, receive, setup, uid  # noqa: F401
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.dispatch_admission import (
    InventoryMutationConflict,
    assert_inventory_mutation_unclaimed,
)
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, StationHandoffAuthority
from unilabos.center_protocol.digest import digest


def picked_up(s):
    """模拟中心校验原 G 的成功装载结果后发出的可信交接包。"""
    payload = deepcopy(s["payload"])
    logistics = uid()
    payload.update(status="in_transit", revision=4,
                   permit={"authorization_uuid": uid(), "logistics_subject_uuid": logistics})
    payload["pickup"] = {
        "schema_version": 1, "event_uuid": uid(), "event_digest": digest({"load": "committed"}),
        "station_invocation_uuid": uid(), "logistics_station_uuid": logistics,
        "instance_uuid": uid(), "local_task_uuid": uid(), "task_payload_digest": digest({"task": "frozen"}),
        "handoff_uuid": payload["handoff_uuid"],
        "authorization_uuid": payload["permit"]["authorization_uuid"], "vehicle_id": "sim_agv",
        "container_uuids": [item["container_uuid"] for item in payload["items"]],
        "job_uuid": uid(), "node_uuid": uid(), "picked_up_at_ms": s["clock"][0],
        "evidence_refs": ["sim-agv://sim_agv/pickup-confirmed"],
    }
    return payload


def test_pickup_releases_physical_source_site_before_receipt_but_keeps_material_protected(setup):
    s = setup
    s["payload"].update(global_task_uuid=uid(), transfer_definition_uuid=uid(), transfer_source_kind="node_output")
    frozen(s)
    backend = BackendResourceService(s["stores"]["source"])
    carrier = s["carrier"]["uuid"]
    source_snapshot = deepcopy(s["payload"]["items"][0]["source_snapshot"])
    payload = picked_up(s)
    s["auth"]["source"].apply_center_snapshot(package(payload))

    site = next(site for site in backend.list_sites(s["carrier"]["parent_uuid"]) if site["uuid"] == s["sites"]["source"])
    assert site["occupied_material_uuid"] is None
    assert s["stores"]["source"].get_instance(carrier)["status"] == "handoff_outgoing"
    with pytest.raises(InventoryMutationConflict), s["stores"]["source"].transaction() as connection:
        assert_inventory_mutation_unclaimed(connection, material_uuids=[carrier])
    next_carrier = backend.create_material({
        "resource_template_uuid": s["templates"]["source"]["test.carrier"], "name": "next-carrier",
        "site_placement": {"action": "place", "site_uuid": s["sites"]["source"]},
    })
    assert next_carrier["parent_uuid"] == s["carrier"]["parent_uuid"]
    assert payload["items"][0]["source_snapshot"] == source_snapshot
    assert payload["items"][0]["receipt"] is None


def test_pickup_replay_restart_and_later_receipt_cannot_clear_reused_site(setup):
    s = setup
    frozen(s)
    payload = picked_up(s)
    source = s["stores"]["source"]
    backend = BackendResourceService(source)
    s["auth"]["source"].apply_center_snapshot(package(payload))
    replacement = backend.create_material({
        "resource_template_uuid": s["templates"]["source"]["test.carrier"], "name": "replacement",
        "site_placement": {"action": "place", "site_uuid": s["sites"]["source"]},
    })
    restarted = StationHandoffAuthority(source, station_uuid=s["ids"]["source"],
        instance_uuid=s["ids"]["source_instance"], event_epoch_uuid=s["ids"]["source_epoch"],
        lab_uuid=s["ids"]["lab"], now_ms=lambda:s["clock"][0])
    restarted.apply_center_snapshot(package(payload))
    restarted.apply_center_snapshot(package(s["payload"]))  # 旧修订没有 pickup，不能覆盖新事实。
    s["auth"]["target"].apply_center_snapshot(package(payload))
    receipt = receive(s)
    payload["items"][0]["receipt"] = receipt
    payload["items"][0]["current_state_version"] = 3
    payload.update(status="received", revision=5)
    restarted.apply_center_snapshot(package(payload))
    restarted.apply_center_snapshot(package(payload))
    assert next(x for x in backend.list_sites(replacement["parent_uuid"]) if x["uuid"]==s["sites"]["source"])["occupied_material_uuid"] == replacement["uuid"]
    assert source.get_instance(s["carrier"]["uuid"])["status"] == "handoff_departed"


def test_pickup_does_not_disassemble_carrier_internal_sites_or_content(setup):
    from tests.app.test_station_handoff_adapter import add_nested_sample

    s = setup
    added = add_nested_sample(s)
    backend = BackendResourceService(s["stores"]["source"])
    internal_sites = backend.list_sites(s["carrier"]["uuid"])
    child_before = backend.get_material(added["child"]["uuid"])
    assert backend.list_all_sites(material_uuid=s["carrier"]["uuid"])["items"]
    frozen(s)
    payload = picked_up(s)
    s["auth"]["source"].apply_center_snapshot(package(payload))
    assert backend.list_sites(s["carrier"]["uuid"]) == internal_sites
    assert backend.list_all_sites(material_uuid=s["carrier"]["uuid"])["items"] == []
    child_after = backend.get_material(added["child"]["uuid"])
    assert child_after["parent_uuid"] == child_before["parent_uuid"]
    assert child_after["revision"] == child_before["revision"]
    assert added["service"].get_sample(added["content"]["uuid"]) == added["content"]


def test_pickup_keeps_protection_on_failure_and_does_not_restore_source_position(setup):
    s = setup
    frozen(s)
    payload = picked_up(s)
    s["auth"]["source"].apply_center_snapshot(package(payload))
    payload.update(status="canceling", revision=5, attention=True, attention_reasons=["TRANSPORT_EXECUTION_UNCERTAIN"])
    s["auth"]["source"].apply_center_snapshot(package(payload))
    backend = BackendResourceService(s["stores"]["source"])
    assert backend.get_material(s["carrier"]["uuid"])["parent_uuid"] is None
    assert next(x for x in backend.list_sites(s["carrier"]["parent_uuid"]) if x["uuid"]==s["sites"]["source"])["occupied_material_uuid"] is None
    with pytest.raises(InventoryMutationConflict), s["stores"]["source"].transaction() as connection:
        assert_inventory_mutation_unclaimed(connection, material_uuids=[s["carrier"]["uuid"]])


def test_pickup_proof_cannot_change_or_disappear_after_application(setup):
    s = setup
    frozen(s)
    payload = picked_up(s)
    s["auth"]["source"].apply_center_snapshot(package(payload))
    for remove in (False, True):
        changed = deepcopy(payload)
        changed["revision"] += 1
        if remove:
            del changed["pickup"]
        else:
            changed["pickup"]["job_uuid"] = uid()
        with pytest.raises(HandoffInventoryError, match="取货证明"):
            s["auth"]["source"].apply_center_snapshot(package(changed))


@pytest.mark.parametrize("had_pickup", [False, True])
def test_real_return_reuses_local_identity_without_old_pickup_marking_it_departed(setup, had_pickup):
    from unilabos.app.scheduler.inventory.handoff_adapter import ADAPTER

    s = setup
    frozen(s)
    original = picked_up(s) if had_pickup else deepcopy(s["payload"])
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(original))
    receipt = receive(s)
    original.update(status="received", revision=5)
    original["items"][0].update(receipt=receipt, current_state_version=3)
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(original))
    # 正式反向交接，通过 prepare/receive 复用 A 的已离站身份。
    reverse = deepcopy(s["payload"])
    item_uuid = uid()
    snapshot = s["auth"]["target"].export_source_snapshot(
        container_uuid=s["ids"]["container"], source_material_refs=receipt["target_material_refs"])
    reverse.update(handoff_uuid=uid(), source_station_uuid=s["ids"]["target"], target_station_uuid=s["ids"]["source"],
                   source_instance_uuid=s["ids"]["target_instance"], target_instance_uuid=s["ids"]["source_instance"],
                   status="requested", revision=1, prepared=None, released=None, permit=None)
    reverse["items"] = [{**reverse["items"][0], "handoff_item_uuid":item_uuid, "expected_state_version":3,
                         "reserved_state_version":4,"current_state_version":4,
                         "source_material_refs":receipt["target_material_refs"],"source_snapshot":snapshot,"receipt":None}]
    details = deepcopy(s["details"])
    details[s["ids"]["container"]].update(current_station_uuid=s["ids"]["target"],state_version=4,active_handoff_item_uuid=item_uuid)
    plan = {"carrier_template_uuid":s["templates"]["source"]["test.carrier"],"carrier_lineage_uuid":s["ids"]["lineage"],
            "lineage_template_uuids":{s["ids"]["lineage"]:s["templates"]["source"]["test.carrier"]},
            "candidate_site_uuids":[s["sites"]["source"]],"ttl_seconds":60,
            "adapter":{"kind":ADAPTER,"site_names":{snapshot["carrier_material_uuid"]:{}},"reagent_info_uuids":{}}}
    ready = s["auth"]["source"].prepare_incoming(package(reverse), container_details=details, plans={item_uuid:plan})
    reverse.update(status="prepared",revision=2,prepared={"event_epoch_uuid":s["ids"]["source_epoch"],
        **{key:ready["payload"][key] for key in ("protection_uuid","reservation_until_ms","evidence_refs")}})
    s["auth"]["target"].freeze_source(package(reverse),physical_ready=True,evidence_refs=["scan:return-ready"])
    returned = s["auth"]["source"].receive(reverse["handoff_uuid"],item_uuid,barcode_namespace="lab",barcode="BKR-1",evidence_refs=["scan:return"])
    reverse.update(status="received",revision=3)
    reverse["items"][0].update(receipt=returned,current_state_version=5)
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(reverse))
    backend = BackendResourceService(s["stores"]["source"])
    material = backend.get_material(s["carrier"]["uuid"])
    assert material["inventory_status"] == "warehouse" and material["inventory_presence"] == "current"
    assert returned["target_material_refs"][0]["local_material_uuid"] == s["carrier"]["uuid"]
    # Center 对首次迟到取货只保管事件，不产生新 attention 包；仍是原 received 快照。
    s["auth"]["source"].apply_center_snapshot(package(original))
    assert backend.get_material(s["carrier"]["uuid"])["inventory_presence"] == "current"
    assert next(x for x in backend.list_sites(s["carrier"]["parent_uuid"]) if x["uuid"]==s["sites"]["source"])["occupied_material_uuid"] == s["carrier"]["uuid"]


def test_unapplied_pickup_with_attention_preserves_original_occupancy(setup):
    s = setup
    frozen(s)
    payload = picked_up(s)
    payload.update(attention=True, attention_reasons=["TRANSPORT_EXECUTION_UNCERTAIN"])
    s["auth"]["source"].apply_center_snapshot(package(payload))
    backend = BackendResourceService(s["stores"]["source"])
    assert next(x for x in backend.list_sites(s["carrier"]["parent_uuid"]) if x["uuid"]==s["sites"]["source"])["occupied_material_uuid"] == s["carrier"]["uuid"]


def test_first_late_pickup_does_not_clear_foreign_occupant_and_rolls_back_snapshot(setup):
    s = setup
    backend = BackendResourceService(s["stores"]["source"])
    foreign = backend.create_material({"resource_template_uuid":s["templates"]["source"]["test.carrier"],"name":"foreign"})
    frozen(s)
    # 模拟库外现场变动后导入的冲突事实；普通库存接口本来会拒绝改动冻结物料。
    store = s["stores"]["source"]
    with store.transaction() as c:
        c.execute("UPDATE site SET occupied_material_uuid=? WHERE uuid=?",(foreign["uuid"],s["sites"]["source"]))
    with pytest.raises(HandoffInventoryError, match="冻结位置"):
        s["auth"]["source"].apply_center_snapshot(package(picked_up(s)))
    assert next(x for x in backend.list_sites(s["carrier"]["parent_uuid"]) if x["uuid"]==s["sites"]["source"])["occupied_material_uuid"] == foreign["uuid"]
    assert store.query_one("SELECT pickup_json FROM station_handoff_source_location")["pickup_json"] is None
    assert store.query_one("SELECT revision FROM station_handoff")["revision"] == 3
