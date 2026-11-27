"""复杂交接迁移实际嵌套结构和内容，保持映射、冻结、幂等与回滚。"""

from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest

from tests.app.test_station_handoff import frozen, package, prepared, receive, setup, uid
from unilabos.app.scheduler.inventory.backend_contract import BackendContractError, BackendResourceService
from unilabos.app.scheduler.inventory.content_contract import BackendContainerContentService
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError
from unilabos.app.scheduler.inventory.handoff_adapter import ADAPTER
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.resources.site_definition import normalize_available_sites


def add_nested_sample(s, *, kind="sample"):
    """只通过公开内容服务写事实；目标模板/目录由本站预先登记。"""
    source, target = s["stores"]["source"], s["stores"]["target"]
    target_template = s["templates"]["target"]["test.carrier"]
    s["backends"] = {side: BackendResourceService(store) for side, store in s["stores"].items()}
    child_templates = {}
    for side in ("source", "target"):
        child_templates[side] = s["backends"][side].sync_resource_templates([{
            "id": "test.transfer-vial", "display_name": "转运样品瓶", "registry_type": "material",
            "class": {}, "category": ["container"], "metadata": {"capacity": {"max_volume_ul": 10000}},
        }])["templates"][0]["uuid"]
    with target.transaction() as c:
        definitions = normalize_available_sites([{"label": "slot-a", "index": 0,
            "allowed_resource_template_uuids": [child_templates["target"]],
            "initial_material_template_uuid": child_templates["target"]}])
        c.execute("UPDATE resource_template SET available_sites=? WHERE uuid=?", (json.dumps(definitions), target_template))
    with source.transaction() as c:
        c.execute("UPDATE resource_template SET available_sites=? WHERE uuid=?", (
            json.dumps(normalize_available_sites([{"label": "well-1", "index": 0, "allowed_resource_template_uuids": [child_templates["source"]]}])), s["templates"]["source"]["test.carrier"]))
        site_uuid = uid()
        c.execute("INSERT INTO site(uuid,create_time,update_time,material_uuid,name) VALUES (?,'2026-01-01','2026-01-01',?,'well-1')", (site_uuid, s["carrier"]["uuid"]))
    child = s["backends"]["source"].create_material({
        "resource_template_uuid": child_templates["source"], "name": "sample-vial", "barcode": "VIAL-001",
        "site_placement": {"action": "place", "site_uuid": site_uuid},
    })
    info_map = {}
    if kind in {"sample", "historical_sample"}:
        service = BackendContainerContentService(source)
        content = service.create_sample({"material_uuid": child["uuid"], "code": "SAMPLE-001", "name": "原始样品", "quantity": 2.5, "quantity_unit": "ml"})
        if kind == "historical_sample":
            # 模拟已迁移的历史行；这些字段不属于普通新增样品接口。
            with source.transaction() as c:
                c.execute("UPDATE container_content SET sample_type=?,source=?,collected_at=?,expires_at=? WHERE uuid=?",
                          ("legacy-serum", "旧批次原文", "2020-01-02T03:04:05", None, content["uuid"]))
    else:
        infos = {}
        for side, store in s["stores"].items():
            reagent = BackendReagentService(store)
            infos[side] = reagent.create_reagent_info({"cas": "7732-18-5", "name": "水", "physical_state": "liquid", "density_g_per_ml": 1.0})
        service = BackendReagentService(source)
        material = child
        if kind == "substance":
            material = s["backends"]["source"].create_material({"resource_template_uuid": child_templates["source"], "name": "历史组分瓶"})
        reagent_content = service.create_reagent({"material_uuid": material["uuid"], "reagent_info_uuid": infos["source"]["uuid"], "quantity": 2.5, "quantity_unit": "ml",
            **({"concentration_value": 0.5, "concentration_unit": "mol/L"} if kind == "substance" else {})})
        content = reagent_content
        if kind == "empty_reagent":
            content = service.update_reagent(content["uuid"], {**content, "quantity": 0})
        elif kind == "substance":
            service = BackendContainerContentService(source)
            content = service.create_current_substance({"material_uuid": child["uuid"], "name": "历史混合物",
                "quantity": 2.5, "quantity_unit": "ml", "physical_state": "liquid", "observed_at": "2024-02-03T04:05:06+00:00",
                "meta_data": {"formula": "frozen-v1"}, "components": [{"reagent_uuid": reagent_content["uuid"], "quantity": 1.25, "quantity_unit": "ml"}]})
            BackendReagentService(source).delete_reagent(reagent_content["uuid"])
        info_map = {infos["source"]["uuid"]: infos["target"]["uuid"]}
    lineage = uid()
    item = s["payload"]["items"][0]
    item["source_material_refs"].append({"station_uuid": s["ids"]["source"], "local_material_uuid": child["uuid"], "local_revision": s["backends"]["source"].get_material(child["uuid"])["revision"], "material_lineage_uuid": lineage})
    item["source_material_refs"][0]["local_revision"] = s["backends"]["source"].get_material(s["carrier"]["uuid"])["revision"]
    item["source_snapshot"] = s["auth"]["source"].export_source_snapshot(container_uuid=s["ids"]["container"], source_material_refs=item["source_material_refs"])
    plan = s["plans"][s["ids"]["item"]]
    plan["lineage_template_uuids"][lineage] = child_templates["target"]
    plan["adapter"] = {"kind": ADAPTER, "site_names": {s["carrier"]["uuid"]: {"well-1": "slot-a"}, child["uuid"]: {}}, "reagent_info_uuids": info_map}
    s["child_templates"] = child_templates
    return {"child": child, "content": content, "service": service, "lineage": lineage}


def settle(s, receipt):
    s["payload"].update(status="received", revision=4)
    s["payload"]["items"][0]["receipt"] = receipt
    s["payload"]["items"][0]["current_state_version"] = 3
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))


@pytest.mark.parametrize(
    "existing_suffixes, expected_name",
    [
        ((), "beaker_incoming_2026110802"),
        (("",), "beaker_incoming_2026110802_01"),
        (("", "_01"), "beaker_incoming_2026110802_02"),
    ],
)
def test_workflow_incoming_name_retains_source_and_skips_existing_name(setup, existing_suffixes, expected_name):
    s = setup
    s["clock"][0] = int(datetime(2026, 11, 8, 12, 34, 2).timestamp() * 1000)
    s["payload"].update(
        global_task_uuid=uid(),
        transfer_definition_uuid=uid(),
        transfer_source_kind="node_output",
        created_at_ms=s["clock"][0],
        in_transit_deadline_ms=s["clock"][0] + 600_000,
    )
    target = s["stores"]["target"]
    backend = BackendResourceService(target)
    for suffix in existing_suffixes:
        backend.create_material({
            "resource_template_uuid": s["templates"]["target"]["test.carrier"],
            "name": f"beaker_incoming_2026110802{suffix}",
        })

    frozen(s)
    receipt = receive(s)
    assert receive(s) == receipt
    settle(s, receipt)

    local = receipt["target_material_refs"][0]["local_material_uuid"]
    assert backend.get_material(local)["name"] == expected_name
    assert s["payload"]["items"][0]["source_snapshot"]["materials"][0]["name"] == "beaker"


@pytest.mark.parametrize("kind", ["sample", "reagent", "historical_sample", "empty_reagent", "substance"])
def test_complex_transfer_keeps_live_structure_and_content(setup, kind):
    s = setup
    added = add_nested_sample(s, kind=kind)
    frozen(s)
    target = s["stores"]["target"]
    incoming = target.query_one("SELECT * FROM container_content WHERE deleted_at IS NULL")
    assert incoming["uuid"] != added["content"]["uuid"]
    assert incoming["quantity"] == (0 if kind == "empty_reagent" else 2.5)
    assert incoming["quantity_unit"] == "ml"
    site = target.query_one("SELECT * FROM site WHERE name='slot-a'")
    assert site["occupied_material_uuid"] == incoming["material_uuid"]
    assert target.query_one("SELECT parent_uuid FROM material WHERE uuid=?", (incoming["material_uuid"],))["parent_uuid"] == site["material_uuid"]
    assert len(target.query_all("SELECT * FROM material WHERE inventory_status='incoming'")) == 2
    receipt = receive(s)
    assert receive(s) == receipt
    assert receipt["target_snapshot"]["inventory_snapshot"]["adapter"] == ADAPTER
    settle(s, receipt)
    assert not s["stores"]["source"].query_all("SELECT * FROM container_content WHERE deleted_at IS NULL")
    assert target.query_one("SELECT inventory_status FROM material WHERE uuid=?", (incoming["material_uuid"],))["inventory_status"] == "warehouse"
    assert target.query_one("SELECT quantity FROM container_content WHERE uuid=?", (incoming["uuid"],))["quantity"] == (0 if kind == "empty_reagent" else 2.5)


def test_content_change_without_material_revision_rejects_freeze(setup):
    s = setup
    added = add_nested_sample(s)
    prepared(s)
    added["service"].update_sample(added["content"]["uuid"], {**added["content"], "quantity": 1.0})
    with pytest.raises(HandoffInventoryError, match="来源结构、容量或内容已变化"):
        s["auth"]["source"].freeze_source(package(s["payload"]), physical_ready=True, evidence_refs=["scan:1"])
    assert not s["stores"]["source"].query_all("SELECT * FROM station_handoff")


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_frozen_sample_cannot_be_edited_or_deleted(setup, operation):
    s = setup
    added = add_nested_sample(s)
    frozen(s)
    with pytest.raises(BackendContractError):
        if operation == "update":
            added["service"].update_sample(added["content"]["uuid"], {**added["content"], "quantity": 1.0})
        else:
            added["service"].delete_sample(added["content"]["uuid"])
    assert added["service"].get_sample(added["content"]["uuid"])["quantity"] == 2.5


@pytest.mark.parametrize("change", ["missing_site", "parent_cycle", "unknown_field", "quantity_over_capacity", "chemical_identity"])
def test_invalid_adapter_is_atomic(setup, change):
    s = setup
    added = add_nested_sample(s, kind="reagent")
    plan = s["plans"][s["ids"]["item"]]
    snapshot = s["payload"]["items"][0]["source_snapshot"]
    child = next(node for node in snapshot["materials"] if node["local_material_uuid"] == added["child"]["uuid"])
    if change == "missing_site":
        plan["adapter"]["site_names"][s["carrier"]["uuid"]] = {}
    elif change == "parent_cycle":
        child["parent_material_uuid"] = added["child"]["uuid"]
    elif change == "unknown_field":
        child["content"]["values"]["execute"] = "no"
    elif change == "quantity_over_capacity":
        child["content"]["values"]["quantity"] = 20
    else:
        child["content"]["chemical_identity"]["cas"] = "64-17-5"
    with pytest.raises((HandoffInventoryError, BackendContractError)):
        prepared(s)
    target = s["stores"]["target"]
    assert not target.query_all("SELECT * FROM material WHERE inventory_status='incoming'")
    assert not target.query_all("SELECT * FROM container_content")
    assert not target.query_all("SELECT * FROM station_handoff")
    assert not target.query_all("SELECT * FROM station_ingress_reservation")


def test_complex_prepare_replays_exactly_and_cancel_preserves_source(setup):
    s = setup
    added = add_nested_sample(s)
    original = prepared(s)
    assert s["auth"]["target"].prepare_incoming(package(s["payload"]), container_details=s["details"], plans=s["plans"]) == original
    s["auth"]["source"].freeze_source(package(s["payload"]), physical_ready=True, evidence_refs=["scan:1"])
    s["payload"].update(status="canceling", revision=3)
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))
    assert added["service"].get_sample(added["content"]["uuid"])["quantity"] == 2.5
    assert not s["stores"]["target"].query_all("SELECT * FROM container_content WHERE deleted_at IS NULL")


def test_missing_adapter_never_silently_drops_complex_contents(setup):
    s = setup
    add_nested_sample(s)
    del s["plans"][s["ids"]["item"]]["adapter"]
    with pytest.raises(HandoffInventoryError, match="显式适配"):
        prepared(s)


@pytest.mark.parametrize("kind", ["sample", "reagent", "historical_sample", "empty_reagent", "substance"])
def test_complex_tree_roundtrip_keeps_identity_and_content(setup, kind):
    s = setup
    added = add_nested_sample(s, kind=kind)
    local_config = {"size_x": 100, "size_y": 80, "size_z": 60, "label": "本站载体配置"}
    source_backend = BackendResourceService(s["stores"]["source"])
    source_backend.update_material(s["carrier"]["uuid"], {"config": local_config})
    source_item = s["payload"]["items"][0]
    source_item["source_material_refs"][0]["local_revision"] = source_backend.get_material(s["carrier"]["uuid"])["revision"]
    source_item["source_snapshot"] = s["auth"]["source"].export_source_snapshot(
        container_uuid=s["ids"]["container"], source_material_refs=source_item["source_material_refs"])
    frozen(s)
    receipt = receive(s)
    settle(s, receipt)
    refs = receipt["target_material_refs"]
    for leg, (source_side, target_side) in enumerate((("target", "source"), ("source", "target")), 1):
        source, target = s["auth"][source_side], s["auth"][target_side]
        payload = copy.deepcopy(s["payload"])
        item_id = uid()
        snapshot = source.export_source_snapshot(container_uuid=s["ids"]["container"], source_material_refs=refs)
        payload.update(handoff_uuid=uid(), source_station_uuid=s["ids"][source_side], target_station_uuid=s["ids"][target_side], source_instance_uuid=s["ids"][source_side+"_instance"], target_instance_uuid=s["ids"][target_side+"_instance"], status="requested", revision=1, prepared=None, released=None)
        payload["items"] = [{**payload["items"][0], "handoff_item_uuid": item_id, "expected_state_version": 1+2*leg, "reserved_state_version": 2+2*leg, "current_state_version": 2+2*leg, "source_material_refs": refs, "source_snapshot": snapshot, "receipt": None}]
        details = copy.deepcopy(s["details"])
        details[s["ids"]["container"]].update(current_station_uuid=s["ids"][source_side], state_version=2+2*leg, active_handoff_item_uuid=item_id)
        source_carrier = snapshot["carrier_material_uuid"]
        info_map = {}
        for node in snapshot["materials"]:
            if node["content"] and node["content"]["kind"] == "reagent":
                local_info = s["stores"][target_side].query_one("SELECT uuid FROM reagent_info WHERE cas='7732-18-5'")["uuid"]
                info_map[node["content"]["source_reagent_info_uuid"]] = local_info
            elif node["content"] and node["content"]["kind"] == "substance":
                local_info = s["stores"][target_side].query_one("SELECT uuid FROM reagent_info WHERE cas='7732-18-5'")["uuid"]
                for component in node["content"]["components"]:
                    info_map[component["reagent_info_uuid"]] = local_info
        plan = {"carrier_template_uuid": s["templates"][target_side]["test.carrier"], "carrier_lineage_uuid": s["ids"]["lineage"],
            "lineage_template_uuids": {s["ids"]["lineage"]: s["templates"][target_side]["test.carrier"], added["lineage"]: s["child_templates"][target_side]},
            "candidate_site_uuids": [s["sites"][target_side]], "ttl_seconds": 60,
            "adapter": {"kind": ADAPTER, "site_names": {node["local_material_uuid"]: {site["name"]: ("well-1" if target_side == "source" else "slot-a") for site in node["sites"]} for node in snapshot["materials"]}, "reagent_info_uuids": info_map}}
        ready = target.prepare_incoming(package(payload), container_details=details, plans={item_id: plan})
        payload.update(status="prepared", revision=2, prepared={"event_epoch_uuid": s["ids"][target_side+"_epoch"], **{key: ready["payload"][key] for key in ("protection_uuid", "reservation_until_ms", "evidence_refs")}})
        source.freeze_source(package(payload), physical_ready=True, evidence_refs=["scan:ready"])
        receipt = target.receive(payload["handoff_uuid"], item_id, barcode_namespace="lab", barcode="BKR-1", evidence_refs=["scan:return"])
        payload.update(status="received", revision=3)
        payload["items"][0].update(receipt=receipt, current_state_version=3+2*leg)
        for authority in (source, target):
            authority.apply_center_snapshot(package(payload))
        refs = receipt["target_material_refs"]
        current = s["stores"][target_side].query_all("SELECT * FROM container_content WHERE deleted_at IS NULL")
        assert len(current) == 1 and current[0]["quantity"] == (0 if kind == "empty_reagent" else 2.5)
        if kind == "historical_sample":
            assert tuple(current[0][field] for field in ("sample_type", "source", "collected_at", "expires_at")) == ("legacy-serum", "旧批次原文", "2020-01-02T03:04:05", None)
        if kind == "substance":
            composition = json.loads(current[0]["composition"])
            assert composition[0]["source_station_uuid"] == s["ids"]["source"]
            assert composition[0]["reagent_uuid"] == added["content"]["components"][0]["reagent_uuid"]
            assert composition[0]["chemical_identity"]["cas"] == "7732-18-5"
            assert composition[0]["quantity"] == 1.25
            assert composition[0]["concentration_value"] == 0.5
            assert composition[0]["concentration_unit"] == "mol/L"
            assert composition[0]["reagent_info_uuid"] == local_info
            assert current[0]["observed_at"] == added["content"]["observed_at"]
            assert json.loads(current[0]["meta_data"])["formula"] == "frozen-v1"
            assert not s["stores"][target_side].query_all("SELECT * FROM container_content WHERE kind='reagent' AND deleted_at IS NULL")
            assert target.receive(payload["handoff_uuid"], item_id, barcode_namespace="lab", barcode="BKR-1", evidence_refs=["scan:return"]) == receipt
        assert not s["stores"][source_side].query_all("SELECT * FROM container_content WHERE deleted_at IS NULL")
        assert source_carrier not in {ref["local_material_uuid"] for ref in refs}
        current_config = source_backend.get_material(s["carrier"]["uuid"])["config"]
        assert all(current_config[key] == value for key, value in local_config.items())


def test_sample_capacity_rejects_oversize_without_guessing_density(setup):
    s = setup
    added = add_nested_sample(s)
    node = next(n for n in s["payload"]["items"][0]["source_snapshot"]["materials"] if n["local_material_uuid"] == added["child"]["uuid"])
    node["content"]["values"]["quantity"] = 20.0
    with pytest.raises(HandoffInventoryError, match="样品数量超过"):
        prepared(s)
    assert not s["stores"]["target"].query_all("SELECT * FROM station_handoff")


@pytest.mark.parametrize("kind", ["sample", "reagent"])
def test_freeze_serializes_against_other_connection_content_edit(setup, monkeypatch, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from unilabos.app.scheduler.inventory.store import InventoryStore

    s = setup
    added = add_nested_sample(s, kind=kind)
    prepared(s)
    other = s.get("open_inventory", InventoryStore)(s["stores"]["source"].path)
    entered, allow_commit = Event(), Event()
    original = s["auth"]["source"]._protect_material

    def pause(*args, **kwargs):
        entered.set()
        assert allow_commit.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(s["auth"]["source"], "_protect_material", pause)
    try:
        service = BackendContainerContentService(other) if kind == "sample" else BackendReagentService(other)
        edit = service.update_sample if kind == "sample" else service.update_reagent
        with ThreadPoolExecutor(2) as pool:
            freezing = pool.submit(s["auth"]["source"].freeze_source, package(s["payload"]), physical_ready=True, evidence_refs=["operator:ready"])
            assert entered.wait(10)
            editing = pool.submit(edit, added["content"]["uuid"], {**added["content"], "quantity": 1.0})
            allow_commit.set()
            assert freezing.result(timeout=10)["payload"]["kind"] == "released"
            with pytest.raises(BackendContractError):
                editing.result(timeout=10)
        assert service.get_sample(added["content"]["uuid"])["quantity"] == 2.5 if kind == "sample" else service.get_reagent(added["content"]["uuid"])["quantity"] == 2.5
    finally:
        allow_commit.set()
        other.close()


def test_quantity_reservation_cannot_bypass_handoff_freeze(setup):
    from unilabos.app.scheduler.inventory.workflow_quantity import (
        WorkflowQuantityInventoryAuthority,
        WorkflowQuantityReservationError,
    )

    s = setup
    added = add_nested_sample(s, kind="reagent")
    authority = WorkflowQuantityInventoryAuthority(s["stores"]["source"], edge_id=s["ids"]["source"], lab_id=s["ids"]["lab"])
    allocation = {"uuid": uid(), "workflow_node_job_uuid": uid(), "inventory_type": "reagent", "inventory_uuid": added["content"]["uuid"], "reserved_quantity": 1.0, "quantity_unit": "ml"}
    frozen(s)
    with pytest.raises(WorkflowQuantityReservationError, match="跨站交接保护"):
        authority.reserve_task(uid(), [allocation])
    assert not s["stores"]["source"].query_all("SELECT * FROM inventory_reservation")


@pytest.mark.parametrize("field,value", [
    ("occupant", []), ("root", {}), ("content_kind", []), ("chemical", []),
    ("content_uuid", {}), ("revision", True), ("physical_state", []),
    ("target_info", []), ("chemical_fields", {"cas": [], "inchi_key": None, "molecular_formula": None}),
])
def test_malformed_nested_identity_is_domain_rejection(setup, field, value):
    s = setup
    added = add_nested_sample(s, kind="reagent")
    snapshot = s["payload"]["items"][0]["source_snapshot"]
    root = next(node for node in snapshot["materials"] if node["local_material_uuid"] == s["carrier"]["uuid"])
    child = next(node for node in snapshot["materials"] if node["local_material_uuid"] == added["child"]["uuid"])
    content = child["content"]
    if field == "occupant": root["sites"][0]["occupied_material_uuid"] = value
    elif field == "root": snapshot["carrier_material_uuid"] = value
    elif field == "content_kind": content["kind"] = value
    elif field == "chemical": content["source_reagent_info_uuid"] = value
    elif field == "content_uuid": content["source_content_uuid"] = value
    elif field == "revision": content["source_revision"] = value
    elif field == "physical_state": content["values"]["physical_state"] = value
    elif field == "chemical_fields": content["chemical_identity"] = value
    else:
        s["plans"][s["ids"]["item"]]["adapter"]["reagent_info_uuids"][content["source_reagent_info_uuid"]] = value
    with pytest.raises(HandoffInventoryError):
        prepared(s)
    assert not s["stores"]["target"].query_all("SELECT * FROM station_handoff")
    assert not s["stores"]["target"].query_all("SELECT * FROM material WHERE inventory_status='incoming'")


def test_malformed_snapshot_keeps_notification_and_independent_task(setup, tmp_path):
    from tests.app.center_client.test_runtime import Center, Service, runtime
    from unilabos.center_protocol.digest import digest

    s = setup
    add_nested_sample(s, kind="reagent")
    nodes = s["payload"]["items"][0]["source_snapshot"]["materials"]
    site = next(node for node in nodes if node["local_material_uuid"] == s["carrier"]["uuid"])["sites"][0]
    original = site["occupied_material_uuid"]
    site["occupied_material_uuid"] = []
    center, service = Center(), Service()
    notice = center.add()
    notice["command"].update(type="handoff.available", payload={})
    notice["command_digest"] = digest(notice["command"])
    center.add()

    class Handoffs:
        def pump(self):
            pass

        def handle_command(self, envelope):
            prepared(s)
            return True

    run = runtime(tmp_path, center, service)
    run.handoffs = Handoffs()
    try:
        run.poll_once()
        assert len(service.calls) == 1
        assert [entry["command"]["command_uuid"] for entry in run.store.commands()] == [notice["command"]["command_uuid"]]
        site["occupied_material_uuid"] = original
        run.poll_once()
        assert not run.store.commands()
        assert len(service.calls) == 1
    finally:
        run.close()
