"""化学品目录纠错不能重解释冻结来源或目标 incoming 的真实身份。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from tests.app.test_station_handoff import frozen, package, prepared, receive
from tests.app.test_station_handoff import setup as setup
from tests.app.test_station_handoff_adapter import add_nested_sample
from unilabos.app.scheduler.inventory.backend_contract import BackendContractError
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.app.scheduler.inventory.store import InventoryStore

IDENTITY_EDITS = [
    {"cas": "64-17-5"},
    {"inchi_key": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"},
    {"molecular_formula": "C2H6O"},
    {"density_g_per_ml": 0.789},
    {"physical_state": "solid"},
    {"name": "乙醇"},
]


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("edit", IDENTITY_EDITS)
def test_protected_reagent_identity_cannot_be_reinterpreted(setup, side, edit):
    s = setup
    add_nested_sample(s, kind="reagent")
    frozen(s)
    service = BackendReagentService(s["stores"][side])
    info = service.list_reagent_infos()["items"][0]
    with pytest.raises(BackendContractError, match="跨站交接保护"):
        service.update_reagent_info(info["uuid"], edit)
    assert service.get_reagent_info(info["uuid"]) == info
    receipt = receive(s)
    contents = [node["content"] for node in receipt["target_snapshot"]["inventory_snapshot"]["materials"] if node["content"]]
    assert contents[0]["chemical_identity"]["cas"] == "7732-18-5"
    assert contents[0]["values"]["physical_state"] == "liquid"
    assert contents[0]["values"]["density_g_per_ml"] == 1.0


@pytest.mark.parametrize("side", ["source", "target"])
def test_protected_reagent_identity_cannot_be_deleted(setup, side):
    s = setup
    add_nested_sample(s, kind="reagent")
    frozen(s)
    service = BackendReagentService(s["stores"][side])
    info = service.list_reagent_infos()["items"][0]
    with pytest.raises(BackendContractError, match="跨站交接保护"):
        service.delete_reagent_info(info["uuid"])
    assert service.get_reagent_info(info["uuid"]) == info
    unrelated = service.create_reagent_info({"name": "独立物质", "physical_state": "solid"})
    assert service.update_reagent_info(unrelated["uuid"], {"name": "已纠错"})["name"] == "已纠错"
    service.delete_reagent_info(unrelated["uuid"])


@pytest.mark.parametrize("side", ["source", "target"])
def test_catalog_edit_waiting_on_freeze_rechecks_protection(setup, monkeypatch, side):
    s = setup
    add_nested_sample(s, kind="reagent")
    if side == "source":
        prepared(s)
    authority = s["auth"][side]
    other = s.get("open_inventory", InventoryStore)(s["stores"][side].path)
    service = BackendReagentService(other)
    info = service.list_reagent_infos()["items"][0]
    entered, continue_write, edit_started = Event(), Event(), Event()
    original = authority._protect_material

    def pause(*args, **kwargs):
        entered.set()
        assert continue_write.wait(10)
        return original(*args, **kwargs)

    def edit():
        edit_started.set()
        return service.update_reagent_info(info["uuid"], {"cas": "64-17-5", "density_g_per_ml": 0.789})

    monkeypatch.setattr(authority, "_protect_material", pause)
    try:
        with ThreadPoolExecutor(2) as pool:
            if side == "source":
                protected = pool.submit(authority.freeze_source, package(s["payload"]), physical_ready=True, evidence_refs=["scan:ready"])
            else:
                protected = pool.submit(authority.prepare_incoming, package(s["payload"]), container_details=s["details"], plans=s["plans"])
            assert entered.wait(10)
            editing = pool.submit(edit)
            assert edit_started.wait(10)
            continue_write.set()
            assert protected.result(timeout=10)["payload"]["kind"] == ("released" if side == "source" else "prepared")
            with pytest.raises(BackendContractError, match="跨站交接保护"):
                editing.result(timeout=10)
        assert service.get_reagent_info(info["uuid"]) == info
    finally:
        continue_write.set()
        other.close()
