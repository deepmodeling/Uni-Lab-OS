"""同名、不同条码载体取货及签收必须按 UUID 清位，历史保持可追溯。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.test_station_handoff import frozen, package, receive, setup
from tests.app.test_station_handoff_pickup import picked_up
from unilabos.app.scheduler.inventory.backend_contract import (
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.store import InventoryStore


@pytest.mark.parametrize("pair", [False, True])
def test_same_name_pickup_and_receipt_preserve_identity_and_replay(setup, pair):
    s = setup
    backend = BackendResourceService(s["stores"]["source"])
    source_sites = [s["sites"]["source"]]
    material_ids = [s["carrier"]["uuid"]]
    if pair:
        sites = {}
        for side in ("source", "target"):
            sites[side] = str(uuid4())
            with s["stores"][side].transaction() as c:
                row = dict(
                    c.execute(
                        "SELECT * FROM site WHERE uuid=?", (s["sites"][side],)
                    ).fetchone()
                )
                row.update(
                    uuid=sites[side], name="second-site", occupied_material_uuid=None
                )
                c.execute(
                    "INSERT INTO site ("
                    + ",".join(row)
                    + ") VALUES ("
                    + ",".join("?" for _ in row)
                    + ")",
                    tuple(row.values()),
                )
        second = backend.create_material(
            {
                "resource_template_uuid": s["templates"]["source"]["test.carrier"],
                "name": s["carrier"]["name"],
                "barcode": "BKR-2",
                "site_placement": {"action": "place", "site_uuid": sites["source"]},
            }
        )
        source_sites.append(sites["source"])
        material_ids.append(second["uuid"])
        item = deepcopy(s["payload"]["items"][0])
        original = s["payload"]["items"][0]
        item_id, container_id, lineage_id = [str(uuid4()) for _ in range(3)]
        item.update(handoff_item_uuid=item_id, container_uuid=container_id)
        item["source_material_refs"][0].update(
            local_material_uuid=second["uuid"],
            local_revision=second["revision"],
            material_lineage_uuid=lineage_id,
        )
        s["payload"]["items"].append(item)
        s["auth"]["source"].bind_container(
            container_uuid=container_id,
            local_carrier_material_uuid=second["uuid"],
            barcode_namespace="lab",
            barcode="BKR-2",
        )
        detail = deepcopy(s["details"][original["container_uuid"]])
        detail.update(
            container_uuid=container_id,
            barcode="BKR-2",
            active_handoff_item_uuid=item_id,
        )
        s["details"][container_id] = detail
        plan = deepcopy(s["plans"][original["handoff_item_uuid"]])
        plan.update(
            carrier_lineage_uuid=lineage_id,
            lineage_template_uuids={
                lineage_id: s["templates"]["target"]["test.carrier"]
            },
            candidate_site_uuids=[sites["target"]],
        )
        s["plans"][item_id] = plan
    else:
        old = backend.create_material(
            {
                "resource_template_uuid": s["templates"]["source"]["test.carrier"],
                "name": s["carrier"]["name"],
                "barcode": "HISTORICAL-BOTTLE",
            }
        )
        with s["stores"]["source"].transaction() as c:
            c.execute(
                "UPDATE material SET inventory_status='handoff_departed' WHERE uuid=?",
                (old["uuid"],),
            )
    frozen(s)
    payload = picked_up(s)
    s["auth"]["source"].apply_center_snapshot(package(payload))
    for site in source_sites:
        assert (
            s["stores"]["source"].query_one(
                "SELECT occupied_material_uuid FROM site WHERE uuid=?", (site,)
            )["occupied_material_uuid"]
            is None
        )
    # 原位复用后，迟到取货和最终回执不可清除新瓶。
    replacement = backend.create_material(
        {
            "resource_template_uuid": s["templates"]["source"]["test.carrier"],
            "name": s["carrier"]["name"],
            "barcode": "REPLACEMENT",
            "site_placement": {"action": "place", "site_uuid": source_sites[0]},
        }
    )
    s["auth"]["source"].apply_center_snapshot(package(payload))
    s["auth"]["target"].apply_center_snapshot(package(payload))
    for index, item in enumerate(payload["items"]):
        item["receipt"] = s["auth"]["target"].receive(
            s["ids"]["handoff"],
            item["handoff_item_uuid"],
            barcode_namespace="lab",
            barcode=f"BKR-{index+1}",
            evidence_refs=["scan:actual"],
        )
        item["current_state_version"] = 3
    payload.update(status="received", revision=5)
    for _ in range(2):
        s["auth"]["source"].apply_center_snapshot(package(payload))
    for identity in material_ids:
        row = backend.get_material(identity)
        assert row["name"] == s["carrier"]["name"] and row["parent_uuid"] is None
        assert (
            s["stores"]["source"].query_one(
                "SELECT inventory_status FROM material WHERE uuid=?", (identity,)
            )["inventory_status"]
            == "handoff_departed"
        )
    assert (
        s["stores"]["source"].query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?", (source_sites[0],)
        )["occupied_material_uuid"]
        == replacement["uuid"]
    )


def test_upgrade_allows_duplicate_material_names_and_optional_unique_barcodes(tmp_path):
    path = str(tmp_path / "inventory.db")
    store = InventoryStore(path)
    backend = BackendResourceService(store)
    template = backend.sync_resource_templates(
        [
            {
                "id": "vessel",
                "display_name": "容器",
                "registry_type": "material",
                "class": {},
            }
        ]
    )["templates"][0]["uuid"]
    original = backend.create_material(
        {"resource_template_uuid": template, "name": "A", "barcode": "FIRST"}
    )
    with store.transaction() as c:
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_material_root_name_active ON material(LOWER(name)) WHERE deleted_at IS NULL AND parent_uuid IS NULL"
        )
        c.execute("PRAGMA user_version=19")
    store.close()
    store = InventoryStore(path)
    backend = BackendResourceService(store)
    try:
        for barcode in (None, "", "   "):
            row = backend.create_material(
                {"resource_template_uuid": template, "name": "A", "barcode": barcode}
            )
            assert row["barcode"] == "" and row["uuid"] != original["uuid"]
        other = backend.create_material(
            {"resource_template_uuid": template, "name": "A", "barcode": "SECOND"}
        )
        assert (
            backend.update_material(other["uuid"], {"barcode": "  "})["barcode"] == ""
        )
        with pytest.raises(BackendContractError):
            backend.update_material(other["uuid"], {"barcode": " first "})
        assert backend.get_material(original["uuid"])["barcode"] == "FIRST"
    finally:
        store.close()
