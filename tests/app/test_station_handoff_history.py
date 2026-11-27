"""复杂库存的历史、保护和错误映射必须跨站保持，不能变成虚假本地试剂。"""
from __future__ import annotations

import copy
import json

import pytest

from tests.app.test_station_handoff import frozen, package, prepared, receive, setup
from tests.app.test_station_handoff_adapter import add_nested_sample, settle
from unilabos.app.scheduler.inventory.backend_contract import BackendContractError
from unilabos.app.scheduler.inventory.content_contract import BackendContainerContentService
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, StationHandoffAuthority
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.app.scheduler.inventory.store import InventoryStore


def _content(s, added):
    return next(node["content"] for node in s["payload"]["items"][0]["source_snapshot"]["materials"]
                if node["local_material_uuid"] == added["child"]["uuid"])


@pytest.mark.parametrize("change", ["unknown_unit", "unknown_catalog", "wrong_chemistry", "bad_origin", "duplicate", "over_capacity", "unknown_concentration", "missing_concentration_value", "missing_concentration_unit", "nonstring_concentration_unit"])
def test_invalid_historical_substance_rolls_back_everything(setup, change):
    s = setup
    added = add_nested_sample(s, kind="substance")
    content = _content(s, added)
    if change == "unknown_unit":
        content["components"][0]["quantity_unit"] = "unknown"
    elif change == "unknown_catalog":
        s["plans"][s["ids"]["item"]]["adapter"]["reagent_info_uuids"] = {}
    elif change == "wrong_chemistry":
        content["components"][0]["chemical_identity"]["cas"] = "64-17-5"
    elif change == "bad_origin":
        content["components"][0]["source_station_uuid"] = "foreign-but-not-a-uuid"
    elif change == "duplicate":
        content["components"].append(copy.deepcopy(content["components"][0]))
    elif change == "over_capacity":
        content["values"]["quantity"] = 20
    else:
        component = content["components"][0]
        component.update(concentration_value=1, concentration_unit="unknown" if change == "unknown_concentration" else "mol/L")
        if change == "missing_concentration_value":
            del component["concentration_value"]
        elif change == "missing_concentration_unit":
            del component["concentration_unit"]
        elif change == "nonstring_concentration_unit":
            component["concentration_unit"] = {}
    with pytest.raises(HandoffInventoryError):
        prepared(s)
    target = s["stores"]["target"]
    assert not target.query_all("SELECT * FROM material WHERE inventory_status='incoming'")
    for table in ("container_content", "station_handoff", "station_ingress_reservation", "inventory_ledger"):
        assert not target.query_all(f"SELECT * FROM {table}")


def test_substance_freeze_and_catalog_changes_are_not_reinterpreted(setup):
    s = setup
    added = add_nested_sample(s, kind="substance")
    frozen(s)
    with pytest.raises(BackendContractError):
        added["service"].update_current_substance(added["content"]["uuid"], {**added["content"], "quantity": 1})
    receipt = receive(s)
    settle(s, receipt)
    target = s["stores"]["target"]
    service = BackendContainerContentService(target)
    row = target.query_one("SELECT * FROM container_content WHERE kind='substance' AND deleted_at IS NULL")
    detail = service.get_current_substance(row["uuid"])
    before = json.loads(row["composition"])
    changed = service.update_current_substance(row["uuid"], {**detail, "name": "本地标签"})
    assert changed["name"] == "本地标签"
    assert json.loads(target.query_one("SELECT composition FROM container_content WHERE uuid=?", (row["uuid"],))["composition"]) == before
    forged = copy.deepcopy(changed)
    forged["components"][0]["quantity"] = 100
    with pytest.raises(BackendContractError, match="historical component"):
        service.update_current_substance(row["uuid"], forged)
    forged_origin = copy.deepcopy(changed)
    forged_origin["components"][0]["source_station_uuid"] = {}
    with pytest.raises(BackendContractError, match="station identity"):
        service.update_current_substance(row["uuid"], forged_origin)
    with target.transaction() as c:
        c.execute("UPDATE reagent_info SET cas='64-17-5' WHERE uuid=?", (before[0]["reagent_info_uuid"],))
    refs = copy.deepcopy(receipt["target_material_refs"])
    # 用当前库存版本重建合法引用，失败必须来自历史化学身份，而不是旧版本。
    for ref in refs:
        ref["local_revision"] = s["backends"]["target"].get_material(ref["local_material_uuid"])["revision"]
    with pytest.raises(HandoffInventoryError, match="化学身份"):
        s["auth"]["target"].export_source_snapshot(container_uuid=s["ids"]["container"], source_material_refs=refs)


@pytest.mark.parametrize("kind", ["substance", "historical_sample", "empty_reagent"])
def test_historical_prepare_restart_replays_original_inventory(setup, kind):
    s = setup
    add_nested_sample(s, kind=kind)
    original = prepared(s)
    target = s["stores"]["target"]
    before = target.query_all("SELECT uuid,material_uuid,quantity,composition,sample_type,source,collected_at,expires_at FROM container_content")
    reopened = s.get("open_inventory", InventoryStore)(target.path)
    try:
        authority = StationHandoffAuthority(reopened, station_uuid=s["ids"]["target"],
            instance_uuid=s["ids"]["target_instance"], event_epoch_uuid=s["ids"]["target_epoch"],
            lab_uuid=s["ids"]["lab"], now_ms=lambda: s["clock"][0])
        assert authority.prepare_incoming(package(s["payload"]), container_details=s["details"], plans=s["plans"]) == original
        assert reopened.query_all("SELECT uuid,material_uuid,quantity,composition,sample_type,source,collected_at,expires_at FROM container_content") == before
        assert authority.pending_intents() == [original]
    finally:
        reopened.close()


def test_zero_reagent_does_not_relax_ordinary_creation(setup):
    s = setup
    added = add_nested_sample(s, kind="empty_reagent")
    frozen(s)
    target = s["stores"]["target"]
    empty = target.query_one("SELECT * FROM container_content WHERE kind='reagent'")
    assert empty["quantity"] == 0 and empty["revision"] >= 1
    assert added["service"].get_reagent(added["content"]["uuid"])["quantity"] == 0
    service = BackendReagentService(target)
    with pytest.raises(BackendContractError):
        service.create_reagent({"material_uuid": empty["material_uuid"], "reagent_info_uuid": empty["reagent_info_uuid"], "quantity": 0, "quantity_unit": "ml"})


@pytest.mark.parametrize("legacy_missing_identity", [False, True])
def test_first_export_never_guesses_historical_chemical_identity(setup, legacy_missing_identity):
    s = setup
    added = add_nested_sample(s, kind="substance")
    source = s["stores"]["source"]
    row = source.query_one("SELECT composition FROM container_content WHERE uuid=?", (added["content"]["uuid"],))
    components = json.loads(row["composition"])
    assert components[0]["chemical_identity"]["cas"] == "7732-18-5"
    if legacy_missing_identity:
        del components[0]["chemical_identity"]
        with source.transaction() as c:
            c.execute("UPDATE container_content SET composition=? WHERE uuid=?", (json.dumps(components), added["content"]["uuid"]))
    else:
        BackendReagentService(source).update_reagent_info(components[0]["reagent_info_uuid"], {"cas":"64-17-5"})
        detail = added["service"].get_current_substance(added["content"]["uuid"])
        # 源试剂已软删除；普通标签编辑也不丢失原始化学事实或复活试剂。
        added["service"].update_current_substance(added["content"]["uuid"], {**detail, "name":"新标签"})
    refs = s["payload"]["items"][0]["source_material_refs"]
    for ref in refs:
        ref["local_revision"] = s["backends"]["source"].get_material(ref["local_material_uuid"])["revision"]
    with pytest.raises(HandoffInventoryError, match="化学身份"):
        s["auth"]["source"].export_source_snapshot(container_uuid=s["ids"]["container"], source_material_refs=refs)
