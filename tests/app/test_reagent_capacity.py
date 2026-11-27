"""唯一最大装料量、旧限制兼容与各入口的原子校验。"""

import csv
import io
import json
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.commands import execute_command
from unilabos.app.scheduler.inventory.dispatch_admission import DispatchAdmissionRequest, DispatchResource
from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.resource_lock import material_lock_key
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot


@pytest.fixture
def scene(tmp_path):
    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    with TestClient(app) as client:
        info = client.post("/api/v1/reagent-infos", json={"name": "容量测试试剂", "physical_state": "liquid"}).json()["data"]["uuid"]
        yield client, store, info
    store.close()


def container(scene, *, capacity=None, rated={"max_volume_ul": 100000}):
    client, _, _ = scene
    template = {"id": str(uuid4()), "category": ["container"], "registry_type": "resource"}
    if rated is not None:
        template["metadata"] = {"capacity": rated}
    template_uuid = client.post("/api/v1/resource-templates", json={"resources": [template]}).json()["data"]["templates"][0]["uuid"]
    result = client.post("/api/v1/materials", json={
        "resource_template_uuid": template_uuid, "name": str(uuid4()),
        "config": {"category": "bottle", **({"capacity": capacity} if capacity is not None else {})},
    }).json()
    assert result["code"] == 0, result
    return result["data"]


def create(scene, material, quantity=50, unit="mL", **extra):
    client, _, info = scene
    return client.post("/api/v1/reagents", json={
        "material_uuid": material["uuid"], "reagent_info_uuid": info,
        "quantity": quantity, "quantity_unit": unit, **extra,
    }).json()


@pytest.mark.parametrize("quantity,unit,accepted", [
    (100, "mL", True), (0.1, "L", True), (100000, "µL", True),
    (100000, "μL", True), (100000, "uL", True), (100.01, "mL", False),
    (0.1001, "L", False), (100001, "µL", False), (1, "mmol", False),
])
def test_volume_boundary_and_unit_conversion(scene, quantity, unit, accepted):
    material = container(scene, rated={"max_volume_ul": 100000})
    result = create(scene, material, quantity, unit)
    assert (result["code"] == 0) is accepted, result
    assert scene[1].query_one("SELECT COUNT(*) AS n FROM reagent")["n"] == int(accepted)


def test_powder_loading_mass_is_per_container_not_inferred_from_volume(scene):
    client, _, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"physical_state": "solid", "density_g_per_ml": 10})
    bottle = container(scene, rated={"max_volume_ul": 300000, "max_mass_g": 30})
    capacity = {"max_mass_g": 20}
    assert create(scene, bottle, 20001, "mg", container_capacity=capacity)["code"] == 1000
    first = create(scene, bottle, 0.02, "kg", container_capacity=capacity)
    assert first["code"] == 0
    assert first["data"]["maximum_capacity"] == {"max_volume_ul": 300000, "max_mass_g": 20}
    assert "loading_limits" not in first["data"]["meta_data"]
    other = container(scene, rated={"max_volume_ul": 300000})
    assert create(scene, other, 50000, "g")["code"] == 1000
    assert create(scene, other, 20, "g", container_capacity=capacity)["code"] == 0


@pytest.mark.parametrize("limits", [
    {"max_mass_g": 0}, {"max_mass_g": -1}, {"max_mass_g": "NaN"},
    {"max_mass_g": "Infinity"}, {"max_mass_g": True}, {"max_mass": 100},
    {"max_volume_ul": 110000}, {"max_mass_g": 100},
])
def test_invalid_or_incompatible_limits_reject_registration(scene, limits):
    material = container(scene, rated={"max_volume_ul": 100000})
    assert create(scene, material, meta_data={"loading_limits": limits})["code"] == 1000
    assert scene[1].query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 0


def test_unconfigured_new_records_need_manual_limit_and_existing_plr_capacity_is_honored(scene):
    client, store, _ = scene
    material = container(scene, rated=None)
    assert create(scene, material, 10000, "mmol")["code"] == 1000
    assert create(scene, material, 50)["code"] == 1000
    assert create(scene, material, 50, container_capacity={"max_volume_ul": 100000})["code"] == 0
    legacy = container(scene, rated=None)
    with store.transaction() as conn:
        conn.execute("UPDATE material SET data=? WHERE uuid=?", (json.dumps({"max_volume": 10000}), legacy["uuid"]))
    assert create(scene, legacy, 11)["code"] == 1000
    detail = client.get(f"/api/v1/materials/{legacy['uuid']}").json()["data"]
    assert detail["capacity"] == {"max_volume_ul": 10000}


def test_limit_only_edits_preserve_lineage_and_record_before_after(scene):
    client, store, _ = scene
    material = container(scene, rated={"max_volume_ul": 100000})
    initial = {"batch": "A", "source_reagent_uuid": "source", "loading_limits": {"max_volume_ul": 80000}}
    reagent = create(scene, material, meta_data=initial)["data"]
    path = f"/api/v1/reagents/{reagent['uuid']}"
    for extra in ({}, {"meta_data": {}}):
        result = client.put(path, json={"quantity": 50, "quantity_unit": "mL", **extra}).json()["data"]
        assert result["meta_data"] == initial
    rejected = client.put(path, json={"quantity": 50, "quantity_unit": "mL", "meta_data": {"loading_limits": {"max_volume_ul": 40000}}}).json()
    assert rejected["code"] == 1000
    updated = client.put(path, json={"quantity": 50, "quantity_unit": "mL", "meta_data": {"loading_limits": {"max_volume_ul": 60000}}}).json()["data"]
    assert updated["meta_data"]["source_reagent_uuid"] == "source"
    assert updated["meta_data"]["batch"] == "A"
    history = client.get(f"/api/v1/materials/{material['uuid']}/reagent-history").json()["data"]["items"]
    changed = next(row for row in history if row["revision"] == updated["revision"])
    assert changed["quantity_delta"] == 0
    assert changed["changes"]["previous"]["loading_limits"] == {"max_volume_ul": 80000}
    assert changed["changes"]["result"]["loading_limits"] == {"max_volume_ul": 60000}
    assert client.put(path, json={"quantity": 61, "quantity_unit": "mL"}).json()["code"] == 1000
    cleared = client.put(path, json={"quantity": 61, "quantity_unit": "mL", "meta_data": {"loading_limits": None}}).json()["data"]
    assert cleared["meta_data"] == {"batch": "A", "source_reagent_uuid": "source"}
    assert client.put(path, json={"quantity": 101, "quantity_unit": "mL"}).json()["code"] == 1000
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 3


def test_material_capacity_update_checks_stock_and_preserves_omitted_limits(scene):
    client, _, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"physical_state": "solid"})
    material = container(scene, capacity={"max_mass_g": 10}, rated={"max_volume_ul": 100000, "max_mass_g": 20})
    create(scene, material, 8, "g")
    path = f"/api/v1/materials/{material['uuid']}"
    assert client.put(path, json={"config": {"capacity": {"max_mass_g": 5}}}).json()["code"] == 1000
    assert client.put(path, json={"config": {"capacity": {"max_volume_ul": 110000}}}).json()["code"] == 1000
    assert client.get(path).json()["data"]["config"]["capacity"] == {"max_mass_g": 10}
    legacy_edit = client.put(path, json={"config": {"category": "powder"}}).json()["data"]
    assert legacy_edit["config"]["capacity"] == {"max_mass_g": 10}
    clear = client.put(path, json={"config": {"capacity": None}}).json()["data"]
    assert clear["capacity"] == {"max_volume_ul": 100000, "max_mass_g": 20}


def test_inline_and_registration_capacity_updates_are_atomic(scene):
    client, store, info = scene
    material = container(scene)
    result = create(scene, material, 51, container_capacity={"max_volume_ul": 50000}, expected_material_revision=material["revision"])
    assert result["code"] == 1000
    unchanged = client.get(f"/api/v1/materials/{material['uuid']}").json()["data"]
    assert unchanged["config"] == material["config"]
    assert unchanged["revision"] == material["revision"]
    assert create(scene, material, 50, container_capacity={"max_volume_ul": 50000})["code"] == 0
    created = client.get(f"/api/v1/materials/{material['uuid']}").json()["data"]
    assert created["config"]["category"] == "bottle"
    assert created["capacity"] == {"max_volume_ul": 50000}
    before = store.query_one("SELECT COUNT(*) AS n FROM material")["n"]
    failed = client.post("/api/v1/materials", json={
        "resource_template_uuid": material["resource_template_uuid"], "name": "失败的内联入库",
        "config": {"capacity": {"max_volume_ul": 10000}},
        "reagent": {"reagent_info_uuid": info, "quantity": 11, "quantity_unit": "mL"},
    }).json()
    assert failed["code"] == 1000
    assert store.query_one("SELECT COUNT(*) AS n FROM material")["n"] == before


@pytest.mark.parametrize("kind", ["json", "csv", "xlsx"])
@pytest.mark.parametrize("unit", ["mL", "g"])
@pytest.mark.parametrize("has_rating", [True, False])
def test_batch_import_limit_failure_rolls_back_prior_rows(scene, kind, unit, has_rating):
    client, store, info = scene
    if unit == "g":
        client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": 1})
    rated = {"max_volume_ul": 100000} if has_rating else None
    bottles = [container(scene, rated=rated), container(scene, rated=rated)]
    limit = {"max_volume_ul": 80000} if unit == "mL" else {"max_mass_g": 80}
    rows = [{"material_uuid": bottle["uuid"], "reagent_info_uuid": info, "quantity": quantity, "quantity_unit": unit,
             "container_capacity": limit, "meta_data": {"batch": "A"}}
            for bottle, quantity in zip(bottles, [70, 81])]
    if kind == "json":
        result = client.post("/api/v1/reagents/batch", json={"items": rows}).json()
    else:
        headers = list(rows[0])
        cells = [[json.dumps(row[key]) if isinstance(row[key], dict) else row[key] for key in headers] for row in rows]
        if kind == "csv":
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerows([headers, *cells])
            data = stream.getvalue().encode()
        else:
            book = Workbook()
            for row in [headers, *cells]:
                book.active.append(row)
            stream = io.BytesIO()
            book.save(stream)
            data = stream.getvalue()
        result = client.post("/api/v1/reagents/import", files={"file": (f"reagents.{kind}", data)}).json()
    assert result["code"] == 1000, result
    assert store.query_one("SELECT COUNT(*) AS n FROM reagent")["n"] == 0
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 0
    assert client.get(f"/api/v1/materials/{bottles[0]['uuid']}").json()["data"]["capacity"] == (rated or {})


def test_dispense_enforces_each_target_atomically_and_does_not_copy_source_limits(scene):
    client, store, _ = scene
    source = create(scene, container(scene, rated={"max_volume_ul": 300000}), 200, meta_data={"loading_limits": {"max_volume_ul": 300000}})["data"]
    targets = [container(scene, rated={"max_volume_ul": 100000}) for _ in range(2)]
    service = InventoryService(store)
    command = {"command_id": str(uuid4()), "type": "reagent.dispense", "actor": "test", "payload": {
        "source_reagent_uuid": source["uuid"], "quantity_unit": "mL", "expected_revision": 1,
        "targets": [{"material_uuid": targets[0]["uuid"], "quantity": 50,
                     "container_capacity": {"max_volume_ul": 60000}, "expected_material_revision": 1},
                    {"material_uuid": targets[1]["uuid"], "quantity": 101}],
    }}
    failed = execute_command(service, command)
    assert failed["status"] != "completed"
    assert store.query_one("SELECT COUNT(*) AS n FROM reagent")["n"] == 1
    assert client.get(f"/api/v1/reagents/{source['uuid']}").json()["data"]["quantity"] == 200
    assert client.get(f"/api/v1/materials/{targets[0]['uuid']}").json()["data"]["config"] == targets[0]["config"]
    command["command_id"] = str(uuid4())
    command["payload"]["targets"][1]["quantity"] = 100
    result = execute_command(service, command)
    assert result["status"] == "completed", result
    assert execute_command(service, command)["replayed"] is True
    reagents = client.get("/api/v1/reagents").json()["data"]["items"]
    first = next(row for row in reagents if row["material_uuid"] == targets[0]["uuid"])
    second = next(row for row in reagents if row["material_uuid"] == targets[1]["uuid"])
    assert first["maximum_capacity"] == {"max_volume_ul": 60000}
    assert "loading_limits" not in first["meta_data"]
    assert "loading_limits" not in second["meta_data"]
    assert first["meta_data"]["source_reagent_uuid"] == source["uuid"]
    history = client.get(f"/api/v1/materials/{targets[0]['uuid']}/reagent-history").json()["data"]["items"]
    assert history[0]["changes"]["result"]["maximum_capacity"] == {"max_volume_ul": 60000}


def test_registry_snapshot_preserves_explicit_capacity_and_template_identity(scene):
    client, store, _ = scene
    definition = {"id": "capacity.template", "category": ["container"], "metadata": {"capacity": {"max_volume_ul": 100000}}}

    class Registry:
        def obtain_registry_device_info(self):
            return []

        def obtain_registry_resource_info(self):
            return [definition]

    snapshot = RegistryTemplateSnapshot.from_registry(Registry())
    service = BackendResourceService(store)
    first = service.sync_resource_templates(snapshot.detached_resources())
    assert first == service.sync_resource_templates(snapshot.detached_resources())
    identity = first["templates"][0]["uuid"]
    material = client.post("/api/v1/materials", json={"resource_template_uuid": identity, "name": "默认容量容器"}).json()["data"]
    assert material["capacity"] == {"max_volume_ul": 100000}
    assert create(scene, material, 101)["code"] == 1000


def test_single_maximum_replaces_legacy_and_combines_quantity_update_atomically(scene):
    client, store, _ = scene
    material = container(scene, capacity={"max_volume_ul": 90000}, rated={"max_volume_ul": 100000})
    reagent = create(scene, material, 70, meta_data={
        "loading_limits": {"max_volume_ul": 80000}, "batch": "A", "source_reagent_uuid": "origin",
    })["data"]
    path = f"/api/v1/reagents/{reagent['uuid']}"
    assert reagent["maximum_capacity"] == {"max_volume_ul": 80000}
    assert reagent["rated_capacity"] == {"max_volume_ul": 100000}
    assert reagent["material_revision"] == material["revision"]
    # 只改余量的旧请求继续受旧限制约束。
    assert client.put(path, json={"quantity": 81, "quantity_unit": "mL"}).json()["code"] == 1000
    update = {"quantity": 60, "quantity_unit": "mL", "container_capacity": {"max_volume_ul": 60000},
              "expected_revision": reagent["revision"], "expected_material_revision": reagent["material_revision"]}
    # 新上限低于旧余量，但新余量满足上限，允许在一次编辑里一起下调。
    response = client.put(path, json=update).json()
    assert response["code"] == 0, response
    updated = response["data"]
    assert updated["meta_data"] == {"batch": "A", "source_reagent_uuid": "origin"}
    assert updated["quantity"] == 60
    assert updated["maximum_capacity"] == {"max_volume_ul": 60000}
    assert updated["material_revision"] == reagent["material_revision"] + 1
    saved_material = client.get(f"/api/v1/materials/{material['uuid']}").json()["data"]
    assert saved_material["config"] == {"category": "bottle", "capacity": {"max_volume_ul": 60000}}
    history = client.get(f"/api/v1/materials/{material['uuid']}/reagent-history").json()["data"]["items"]
    assert history[0]["changes"]["previous"]["maximum_capacity"] == {"max_volume_ul": 80000}
    assert history[0]["changes"]["result"]["maximum_capacity"] == {"max_volume_ul": 60000}
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 2


@pytest.mark.parametrize("failure", ["quantity", "rated", "material_revision", "reagent_revision", "invalid"])
def test_combined_maximum_edit_failure_rolls_back_all_changes(scene, failure):
    client, store, _ = scene
    material = container(scene, rated={"max_volume_ul": 100000})
    reagent = create(scene, material, meta_data={"loading_limits": {"max_volume_ul": 80000}, "batch": "A"})["data"]
    reagent.pop("reagent_info")
    body = {"quantity": 60, "quantity_unit": "mL", "container_capacity": {"max_volume_ul": 70000},
            "expected_revision": reagent["revision"], "expected_material_revision": reagent["material_revision"]}
    if failure == "quantity":
        body["quantity"] = 71
    elif failure == "rated":
        body["container_capacity"] = {"max_volume_ul": 110000}
    elif failure == "invalid":
        body["container_capacity"] = {"max_volume_ul": "NaN"}
    else:
        body["expected_material_revision" if failure == "material_revision" else "expected_revision"] = 999
    response = client.put(f"/api/v1/reagents/{reagent['uuid']}", json=body).json()
    assert response["code"] in (1000, 4002), response
    assert client.get(f"/api/v1/reagents/{reagent['uuid']}").json()["data"] == reagent
    assert client.get(f"/api/v1/materials/{material['uuid']}").json()["data"]["config"] == material["config"]
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 1
    assert store.query_one("SELECT COUNT(*) AS n FROM sync_outbox WHERE aggregate_type='reagent'")["n"] == 1


@pytest.mark.parametrize("rated", [{"max_volume_ul": 100000}, {"max_volume_ul": 150000}])
def test_maximum_only_edit_and_clear_record_effective_before_after(scene, rated):
    client, _, _ = scene
    material = container(scene, rated=rated)
    reagent = create(scene, material, meta_data={"loading_limits": {"max_volume_ul": 80000}})["data"]
    path = f"/api/v1/reagents/{reagent['uuid']}"
    for requested, expected in [({"max_volume_ul": 90000}, {"max_volume_ul": 90000}), ({}, rated or {})]:
        previous = reagent["maximum_capacity"]
        result = client.put(path, json={"quantity": 50, "quantity_unit": "mL", "container_capacity": requested,
                                       "expected_material_revision": reagent["material_revision"]}).json()
        assert result["code"] == 0, result
        reagent = result["data"]
        assert reagent["maximum_capacity"] == expected
        assert "loading_limits" not in reagent["meta_data"]
        history = client.get(f"/api/v1/materials/{material['uuid']}/reagent-history").json()["data"]["items"]
        assert history[0]["quantity_delta"] == 0
        assert history[0]["changes"]["previous"]["maximum_capacity"] == previous
        assert history[0]["changes"]["result"]["maximum_capacity"] == expected
    assert client.put(path, json={"quantity": 99, "quantity_unit": "mL"}).json()["code"] == 0


@pytest.mark.parametrize("capacity", [{"max_volume_ul": 90000}, {}])
def test_material_maximum_edit_folds_legacy_and_records_history(scene, capacity):
    client, _, _ = scene
    material = container(scene, capacity={"max_volume_ul": 90000}, rated={"max_volume_ul": 100000})
    reagent = create(scene, material, meta_data={"loading_limits": {"max_volume_ul": 80000}, "batch": "A"})["data"]
    path = f"/api/v1/materials/{material['uuid']}"
    # 省略 capacity 的配置更新不能偷偷清掉旧试剂限制。
    client.put(path, json={"config": {"category": "bottle", "label": "kept"}})
    assert client.get(f"/api/v1/reagents/{reagent['uuid']}").json()["data"]["maximum_capacity"] == {"max_volume_ul": 80000}
    revision = client.get(path).json()["data"]["revision"]
    rejected = client.put(path, json={"config": {"capacity": {"max_volume_ul": 40000}}, "expected_revision": revision}).json()
    assert rejected["code"] == 1000
    updated = client.put(path, json={"config": {"capacity": capacity}, "expected_revision": revision}).json()
    assert updated["code"] == 0, updated
    assert updated["data"]["config"]["label"] == "kept"
    result = client.get(f"/api/v1/reagents/{reagent['uuid']}").json()["data"]
    expected = capacity or {"max_volume_ul": 100000}
    assert result["maximum_capacity"] == expected
    assert result["meta_data"] == {"batch": "A"}
    assert result["revision"] == reagent["revision"] + 1
    history = client.get(path + "/reagent-history").json()["data"]["items"]
    assert history[0]["quantity_delta"] == 0
    assert history[0]["changes"]["previous"]["maximum_capacity"] == {"max_volume_ul": 80000}
    assert history[0]["changes"]["result"]["maximum_capacity"] == expected
    assert client.put(f"/api/v1/reagents/{reagent['uuid']}", json={"quantity": 85, "quantity_unit": "mL"}).json()["code"] == 0


def test_explicit_maximum_takes_precedence_over_legacy_create_fields(scene):
    material = container(scene)
    result = create(scene, material, 70, container_capacity={"max_volume_ul": 80000},
                    meta_data={"loading_limits": {"max_volume_ul": 60000}, "batch": "A"})
    assert result["code"] == 0, result
    assert result["data"]["meta_data"] == {"batch": "A"}
    assert result["data"]["maximum_capacity"] == {"max_volume_ul": 80000}


@pytest.mark.parametrize("endpoint", ["reagent", "material"])
def test_clear_local_maximum_logs_even_when_rated_default_is_the_same(scene, endpoint):
    client, store, _ = scene
    material = container(scene, capacity={"max_volume_ul": 100000}, rated={"max_volume_ul": 100000})
    reagent = create(scene, material)["data"]
    if endpoint == "reagent":
        result = client.put(f"/api/v1/reagents/{reagent['uuid']}", json={
            "quantity": 50, "quantity_unit": "mL", "container_capacity": {},
        }).json()
    else:
        result = client.put(f"/api/v1/materials/{material['uuid']}", json={"config": {"capacity": {}}}).json()
    assert result["code"] == 0, result
    history = client.get(f"/api/v1/materials/{material['uuid']}/reagent-history").json()["data"]["items"]
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 2
    assert history[0]["quantity_delta"] == 0
    assert history[0]["changes"]["previous"]["maximum_capacity"] == {"max_volume_ul": 100000}
    assert history[0]["changes"]["result"]["maximum_capacity"] == {"max_volume_ul": 100000}


def test_combined_edit_cannot_take_workflow_reserved_quantity(scene):
    client, store, _ = scene
    material = container(scene, rated={"max_volume_ul": 100000})
    reagent = create(scene, material, 80)["data"]
    task_uuid = str(uuid4())
    source = MaterialSourceAdmissionRequest(
        node_id="capacity-source", resource_template_uuid=material["resource_template_uuid"],
        custody_policy="shared_source", requirement=MaterialRequirement(
            template_id=material["resource_template_uuid"], instance_uuid=material["uuid"]),
    )
    InventoryService(store).admit_task_materials(task_uuid, [source], [{
        "uuid": str(uuid4()), "workflow_task_uuid": task_uuid, "workflow_node_job_uuid": str(uuid4()),
        "requirement_key": "reagent", "inventory_type": "reagent", "inventory_uuid": reagent["uuid"],
        "material_uuid": material["uuid"], "reserved_quantity": 70, "quantity_unit": "mL",
    }])
    path = f"/api/v1/reagents/{reagent['uuid']}"
    before = client.get(path).json()["data"]
    result = client.put(path, json={"quantity": 60, "quantity_unit": "mL",
                                   "container_capacity": {"max_volume_ul": 60000}}).json()
    assert result["code"] == 4002, result
    assert client.get(path).json()["data"] == before
    assert client.get(f"/api/v1/materials/{material['uuid']}").json()["data"]["config"] == material["config"]


def test_active_claim_blocks_maximum_edits_from_reagent_and_material(scene):
    client, store, _ = scene
    material = container(scene, rated={"max_volume_ul": 100000})
    reagent = create(scene, material)["data"]
    reagent.pop("reagent_info")
    permit = InventoryService(store).station_resources.acquire_dispatch_permit(DispatchAdmissionRequest(
        effect_uuid=str(uuid4()), task_uuid=str(uuid4()), job_uuid=str(uuid4()), attempt=1,
        parameter_hash="capacity-edit", expected_change_set={"kind": "no_inventory_change"},
        resources=(DispatchResource(lock_key=material_lock_key(material["uuid"]), scope="material",
                                    material_uuid=material["uuid"]),),
    ))
    assert permit.acquired
    result = client.put(f"/api/v1/reagents/{reagent['uuid']}", json={
        "quantity": 50, "quantity_unit": "mL", "container_capacity": {"max_volume_ul": 90000},
    }).json()
    assert result["code"] == 4002, result
    result = client.put(f"/api/v1/materials/{material['uuid']}", json={
        "config": {"capacity": {"max_volume_ul": 90000}},
    }).json()
    assert result["code"] == 6011, result
    assert client.get(f"/api/v1/reagents/{reagent['uuid']}").json()["data"] == reagent


@pytest.mark.parametrize("quantity,maximum,accepted", [
    (100, None, True), (100.01, None, False), (200, 200, False),
    (50, 200, False), (80, 80, True), (81, 80, False),
])
def test_liquid_mass_and_manual_maximum_both_respect_rated_volume(scene, quantity, maximum, accepted):
    client, store, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": 1})
    material = container(scene)
    extra = {} if maximum is None else {"container_capacity": {"max_mass_g": maximum}}
    result = create(scene, material, quantity, "g", **extra)
    assert (result["code"] == 0) is accepted, result
    after = client.get(f"/api/v1/materials/{material['uuid']}").json()["data"]
    assert after["rated_capacity"] == {"max_volume_ul": 100000}
    if not accepted:
        assert after["config"] == material["config"]
        assert after["revision"] == material["revision"]
        assert store.query_one("SELECT COUNT(*) AS n FROM reagent")["n"] == 0
        assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 0


@pytest.mark.parametrize("unit", ["µL", "μL", "uL"])
def test_liquid_density_conversion_accepts_all_microliter_aliases(scene, unit):
    client, _, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": 1})
    material = container(scene, rated={"max_mass_g": 100})
    assert create(scene, material, 100001, unit)["code"] == 1000
    assert create(scene, material, 100000, unit)["code"] == 0


@pytest.mark.parametrize("density", [None, float("inf")])
@pytest.mark.parametrize("rated", [{"max_volume_ul": 100000}, {"max_mass_g": 100}])
def test_liquid_mass_needs_eligible_density_even_with_a_mass_rating(scene, density, rated):
    _, store, info = scene
    # 旧目录可能没有通过当前 DTO 校验；库存入口仍须防御无效密度。
    with store.transaction() as conn:
        conn.execute("UPDATE reagent_info SET density_g_per_ml=? WHERE uuid=?", (density, info))
    material = container(scene, rated=rated)
    result = create(scene, material, 1, "g")
    assert result["code"] == 1000
    assert "密度" in result["error"]["msg"]


def test_liquid_checks_all_rated_and_user_dimensions(scene):
    client, _, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": 2})
    material = container(scene, rated={"max_volume_ul": 100000, "max_mass_g": 80})
    assert create(scene, material, 41, "mL")["code"] == 1000
    assert create(scene, material, 30, "mL", container_capacity={"max_volume_ul": 50000})["code"] == 1000
    assert create(scene, material, 31, "mL", container_capacity={"max_mass_g": 60})["code"] == 1000
    assert create(scene, material, 30, "mL", container_capacity={"max_mass_g": 60})["code"] == 0


@pytest.mark.parametrize("state", ["unknown", "other", "gas", "solid"])
def test_new_intake_cannot_pick_volume_to_evade_phase_policy(scene, state):
    client, _, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"physical_state": state})
    result = create(scene, container(scene, rated={"max_volume_ul": 100000, "max_mass_g": 100}), 10)
    assert result["code"] == 1000


@pytest.mark.parametrize("endpoint", ["reagent", "material"])
def test_capacity_edit_checks_actual_stock_and_custom_limit_using_bottle_density(scene, endpoint):
    client, _, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": 1})
    material = container(scene)
    reagent = create(scene, material, 80, "g")["data"]
    client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": 10})
    path = f"/api/v1/reagents/{reagent['uuid']}" if endpoint == "reagent" else f"/api/v1/materials/{material['uuid']}"
    for maximum in [200, 70]:
        payload = {"quantity": 80, "quantity_unit": "g", "container_capacity": {"max_mass_g": maximum}} if endpoint == "reagent" else {"config": {"capacity": {"max_mass_g": maximum}}}
        assert client.put(path, json=payload).json()["code"] == 1000
    payload = {"quantity": 80, "quantity_unit": "g", "container_capacity": {"max_mass_g": 90}} if endpoint == "reagent" else {"config": {"capacity": {"max_mass_g": 90}}}
    assert client.put(path, json=payload).json()["code"] == 0
    saved = client.get(f"/api/v1/reagents/{reagent['uuid']}").json()["data"]
    assert saved["density_g_per_ml"] == 1
    assert saved["quantity"] == 80


def test_liquid_mass_rejects_concentration_on_create_and_edit(scene):
    client, _, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": 1})
    material = container(scene)
    assert create(scene, material, 50, "g", concentration_value=10, concentration_unit="%")["code"] == 1000
    reagent = create(scene, material, 50, "g")["data"]
    path = f"/api/v1/reagents/{reagent['uuid']}"
    assert client.put(path, json={"quantity": 40, "quantity_unit": "g", "concentration_value": 10,
                                  "concentration_unit": "%"}).json()["code"] == 1000
    assert client.get(path).json()["data"]["quantity"] == 50


@pytest.mark.parametrize("phase,unit", [("unknown", "mmol"), ("solid", "mL"), ("liquid", "g")])
def test_legacy_records_can_reduce_or_keep_without_creating_new_capacity_proof(scene, phase, unit):
    client, store, _ = scene
    material = container(scene)
    reagent = create(scene, material)["data"]
    # 模拟升级前留下的无额定规格、旧单位库存，不经新入库入口制造旧数据。
    with store.transaction() as conn:
        conn.execute("UPDATE reagent SET physical_state=?,quantity_unit=?,quantity=200 WHERE uuid=?", (phase, unit, reagent["uuid"]))
        conn.execute("UPDATE resource_template SET meta_data='{}' WHERE uuid=?", (material["resource_template_uuid"],))
    path = f"/api/v1/reagents/{reagent['uuid']}"
    for quantity in [200, 180]:
        assert client.put(path, json={"quantity": quantity, "quantity_unit": unit,
                                      "meta_data": {"note": "保留旧台账"}}).json()["code"] == 0
    base = {"quantity": 180, "quantity_unit": unit}
    for changes in [{"quantity": 181}, {"container_capacity": {}},
                    {"container_capacity": {"max_mass_g": 300}},
                    {"meta_data": {"loading_limits": {"max_mass_g": 300}}},
                    {"concentration_value": 1, "concentration_unit": "%"}]:
        assert client.put(path, json={**base, **changes}).json()["code"] == 1000
    material_path = f"/api/v1/materials/{material['uuid']}"
    assert client.put(material_path, json={"config": {"note": "兼容普通配置"}}).json()["code"] == 0
    assert client.put(material_path, json={"config": {"capacity": {}}}).json()["code"] == 1000
    saved = client.get(path).json()["data"]
    assert saved["quantity"] == 180
    assert saved["quantity_unit"] == unit
    assert saved["meta_data"]["note"] == "保留旧台账"


def test_inline_liquid_mass_uses_resolved_identity_and_rolls_back_bad_limit(scene):
    client, store, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": 1})
    material = container(scene)
    before = store.query_one("SELECT COUNT(*) AS n FROM material")["n"]
    payload = {"resource_template_uuid": material["resource_template_uuid"], "name": "内联称重",
               "config": {"capacity": {"max_mass_g": 200}},
               "reagent": {"reagent_info_uuid": info, "quantity": 50, "quantity_unit": "g"}}
    assert client.post("/api/v1/materials", json=payload).json()["code"] == 1000
    assert store.query_one("SELECT COUNT(*) AS n FROM material")["n"] == before
    payload["config"]["capacity"]["max_mass_g"] = 80
    assert client.post("/api/v1/materials", json=payload).json()["code"] == 0


def test_dispense_inherits_source_density_and_state_and_preserves_rated_target(scene):
    client, store, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": 1})
    source = create(scene, container(scene), 80, "g")["data"]
    target = container(scene, rated={"max_volume_ul": 50000})
    client.put(f"/api/v1/reagent-infos/{info}", json={"physical_state": "solid", "density_g_per_ml": 10})
    service = InventoryService(store)
    payload = {"source_reagent_uuid": source["uuid"], "quantity_unit": "g", "expected_revision": 1,
               "targets": [{"material_uuid": target["uuid"], "quantity": 40,
                            "container_capacity": {"max_mass_g": 200}}]}
    command = {"command_id": str(uuid4()), "type": "reagent.dispense", "actor": "test", "payload": payload}
    assert execute_command(service, command)["status"] != "completed"
    assert client.get(f"/api/v1/reagents/{source['uuid']}").json()["data"]["quantity"] == 80
    assert client.get(f"/api/v1/materials/{target['uuid']}").json()["data"]["config"] == target["config"]
    payload["targets"][0]["container_capacity"] = {"max_mass_g": 50}
    command["command_id"] = str(uuid4())
    assert execute_command(service, command)["status"] == "completed"
    target_reagent = next(item for item in client.get("/api/v1/reagents").json()["data"]["items"]
                          if item["material_uuid"] == target["uuid"])
    assert target_reagent["physical_state"] == "liquid"
    assert target_reagent["density_g_per_ml"] == 1
    assert target_reagent["density_source"] == "dictionary"
    assert target_reagent["rated_capacity"] == {"max_volume_ul": 50000}


def test_legacy_concentrated_bottle_reduction_preserves_omitted_context(scene):
    client, store, _ = scene
    material = container(scene)
    reagent = create(scene, material, concentration_value=95, concentration_unit="%")["data"]
    with store.transaction() as conn:
        conn.execute("UPDATE resource_template SET meta_data='{}' WHERE uuid=?", (material["resource_template_uuid"],))
    path = f"/api/v1/reagents/{reagent['uuid']}"
    reduced = client.put(path, json={"quantity": 40, "quantity_unit": "mL"}).json()["data"]
    assert reduced["concentration_value"] == 95
    assert reduced["concentration_unit"] == "%"
    assert client.put(path, json={"quantity": 30, "quantity_unit": "mL", "concentration_value": None,
                                  "concentration_unit": None}).json()["code"] == 1000
    assert client.get(path).json()["data"]["quantity"] == 40


def test_legacy_volume_source_change_still_validates_existing_stock(scene):
    client, _, _ = scene
    material = container(scene)
    create(scene, material)
    path = f"/api/v1/materials/{material['uuid']}"
    before = client.get(path).json()["data"]
    assert client.put(path, json={"config": {"max_volume": 10000}}).json()["code"] == 1000
    assert client.get(path).json()["data"] == before


@pytest.mark.parametrize("source", ["config", "loading_limits"])
def test_solid_read_distinguishes_geometric_rating_from_unverifiable_manual_volume(scene, source):
    client, store, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"physical_state": "solid"})
    material = container(scene, rated={"max_volume_ul": 100000, "max_mass_g": 20})
    reagent = create(scene, material, 10, "g")["data"]
    assert reagent["configured_capacity"] == {}
    assert reagent["rated_capacity"] == {"max_volume_ul": 100000, "max_mass_g": 20}
    manual_volume = {"max_volume_ul": 50000}
    with store.transaction() as conn:
        if source == "config":
            conn.execute("UPDATE material SET config=? WHERE uuid=?",
                         (json.dumps({**material["config"], "capacity": manual_volume}), material["uuid"]))
        else:
            conn.execute("UPDATE reagent SET meta_data=? WHERE uuid=?",
                         (json.dumps({"loading_limits": manual_volume}), reagent["uuid"]))
    path = f"/api/v1/reagents/{reagent['uuid']}"
    item = client.get(path).json()["data"]
    assert item["configured_capacity"] == (manual_volume if source == "config" else {})
    listed = client.get("/api/v1/reagents").json()["data"]["items"][0]
    assert listed["configured_capacity"] == item["configured_capacity"]
    assert client.put(path, json={"quantity": 11, "quantity_unit": "g"}).json()["code"] == 1000
    fixed = client.put(path, json={"quantity": 11, "quantity_unit": "g",
                                   "container_capacity": {"max_mass_g": 15}}).json()["data"]
    assert fixed["configured_capacity"] == {"max_mass_g": 15}
    assert "loading_limits" not in fixed["meta_data"]


@pytest.mark.parametrize("limit_source", ["request", "config", "loading_limits"])
def test_powder_300ml_bottle_accepts_manual_50g_and_rejects_overflow(scene, limit_source):
    client, store, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"physical_state": "solid", "density_g_per_ml": 10})
    rating = {"max_volume_ul": 300000}
    maximum = {"max_mass_g": 50}
    material = container(scene, rated=rating, capacity=maximum if limit_source == "config" else None)
    extra = {"container_capacity": maximum} if limit_source == "request" else (
        {"meta_data": {"loading_limits": maximum}} if limit_source == "loading_limits" else {})
    if limit_source != "config":
        missing = create(scene, material, 1, "g")
        assert missing["code"] == 1000
        assert "手动设置" in missing["error"]["msg"]
    assert create(scene, material, 51, "g", **extra)["code"] == 1000
    unchanged = client.get(f"/api/v1/materials/{material['uuid']}").json()["data"]
    assert unchanged["config"] == material["config"]
    assert unchanged["revision"] == material["revision"]
    assert store.query_one("SELECT COUNT(*) AS n FROM reagent")["n"] == 0
    saved = create(scene, material, 50, "g", **extra)["data"]
    assert saved["maximum_capacity"]["max_mass_g"] == 50
    assert saved["rated_capacity"] == rating


@pytest.mark.parametrize("unit,density", [("mL", None), ("g", 1)])
@pytest.mark.parametrize("stored", [True, False])
def test_unrated_liquid_manual_capacity_enforces_boundary_and_rolls_back(scene, unit, density, stored):
    client, store, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"density_g_per_ml": density})
    maximum = {"max_volume_ul": 50000} if unit == "mL" else {"max_mass_g": 50}
    material = container(scene, rated=None, capacity=maximum if stored else None)
    extra = {} if stored else {"container_capacity": maximum}
    assert create(scene, material, 51, unit, **extra)["code"] == 1000
    unchanged = client.get(f"/api/v1/materials/{material['uuid']}").json()["data"]
    assert unchanged["config"] == material["config"]
    assert unchanged["revision"] == material["revision"]
    assert store.query_one("SELECT COUNT(*) AS n FROM reagent")["n"] == 0
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 0
    saved = create(scene, material, 50, unit, **extra)["data"]
    assert saved["rated_capacity"] == {}
    assert saved["configured_capacity"] == maximum


def test_solid_bead_count_is_checked_against_count_capacity():
    from unilabos.app.scheduler.inventory.backend_contract import BackendContractError
    from unilabos.app.scheduler.inventory.capacity import validate_quantity

    context = {"physical_state": "solid"}
    validate_quantity(
        10,
        "粒",
        {},
        rated_capacity={},
        configured_capacity={"max_count": 100},
        context=context,
    )
    with pytest.raises(BackendContractError, match="超过上限"):
        validate_quantity(
            101,
            "粒",
            {},
            rated_capacity={},
            configured_capacity={"max_count": 100},
            context=context,
        )


def test_manual_fallback_never_relaxes_existing_mass_or_unconvertible_liquid_limits(scene):
    client, _, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"physical_state": "solid"})
    powder = container(scene, rated={"max_volume_ul": 300000, "max_mass_g": 40})
    assert create(scene, powder, 10, "g", container_capacity={"max_mass_g": 50})["code"] == 1000
    client.put(f"/api/v1/reagent-infos/{info}", json={"physical_state": "liquid", "density_g_per_ml": None})
    liquid = container(scene, rated={"max_mass_g": 100})
    assert create(scene, liquid, 10, "mL", container_capacity={"max_volume_ul": 50000})["code"] == 1000
    no_rating = container(scene, rated=None)
    assert create(scene, no_rating, 10, "g", container_capacity={"max_mass_g": 50})["code"] == 1000


def test_unrated_manual_limit_cannot_be_cleared_while_stock_has_no_other_limit(scene):
    client, _, _ = scene
    material = container(scene, rated=None, capacity={"max_volume_ul": 50000})
    reagent = create(scene, material, 50)["data"]
    reagent_path = f"/api/v1/reagents/{reagent['uuid']}"
    material_path = f"/api/v1/materials/{material['uuid']}"
    assert client.put(reagent_path, json={"quantity": 40, "quantity_unit": "mL", "container_capacity": {}}).json()["code"] == 1000
    assert client.put(material_path, json={"config": {"capacity": {}}}).json()["code"] == 1000
    saved = client.put(reagent_path, json={"quantity": 40, "quantity_unit": "mL",
                                         "container_capacity": {"max_volume_ul": 40000}}).json()["data"]
    assert saved["quantity"] == 40
    assert saved["configured_capacity"] == {"max_volume_ul": 40000}


def test_powder_dispense_uses_target_manual_mass_limit_without_deriving_from_volume(scene):
    client, store, info = scene
    client.put(f"/api/v1/reagent-infos/{info}", json={"physical_state": "solid"})
    source = create(scene, container(scene, rated={"max_mass_g": 100}), 60, "g")["data"]
    target = container(scene, rated={"max_volume_ul": 300000})
    payload = {"source_reagent_uuid": source["uuid"], "quantity_unit": "g", "expected_revision": 1,
               "targets": [{"material_uuid": target["uuid"], "quantity": 51,
                            "container_capacity": {"max_mass_g": 50}}]}
    command = {"command_id": str(uuid4()), "type": "reagent.dispense", "actor": "test", "payload": payload}
    service = InventoryService(store)
    assert execute_command(service, command)["status"] != "completed"
    assert client.get(f"/api/v1/reagents/{source['uuid']}").json()["data"]["quantity"] == 60
    assert client.get(f"/api/v1/materials/{target['uuid']}").json()["data"]["config"] == target["config"]
    payload["targets"][0]["quantity"] = 50
    command["command_id"] = str(uuid4())
    assert execute_command(service, command)["status"] == "completed"
    assert client.get(f"/api/v1/reagents/{source['uuid']}").json()["data"]["quantity"] == 10
