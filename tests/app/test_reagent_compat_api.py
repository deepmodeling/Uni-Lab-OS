from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.reagent_compat import ReagentCompatStore, create_reagent_compat_router


def test_reagent_catalog_and_container_lifecycle(tmp_path):
    app = FastAPI()
    app.include_router(create_reagent_compat_router(ReagentCompatStore(tmp_path / "reagents.db")))
    client = TestClient(app)

    created_info = client.post(
        "/api/v1/reagent-infos",
        json={"name": "无水乙醇", "cas": "64-17-5", "physical_state": "liquid"},
    )
    assert created_info.status_code == 201
    info = created_info.json()["data"]
    assert info["name"] == "无水乙醇"

    created_reagent = client.post(
        "/api/v1/reagents",
        json={
            "material_uuid": "container-ethanol-01",
            "reagent_info_uuid": info["uuid"],
            "quantity": 100,
            "quantity_unit": "mL",
        },
    )
    assert created_reagent.status_code == 201
    reagent = created_reagent.json()["data"]
    assert reagent["quantity"] == 100
    assert reagent["revision"] == 1

    listed = client.get("/api/v1/reagents?page=1&page_size=100")
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1

    updated = client.put(
        f"/api/v1/reagents/{reagent['uuid']}",
        json={"quantity": 80, "quantity_unit": "mL", "expected_revision": 1},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["quantity"] == 80
    assert updated.json()["data"]["revision"] == 2

    stale = client.put(
        f"/api/v1/reagents/{reagent['uuid']}",
        json={"quantity": 70, "expected_revision": 1},
    )
    assert stale.status_code == 409

    assert client.delete(f"/api/v1/reagents/{reagent['uuid']}").status_code == 200
    assert client.delete(f"/api/v1/reagent-infos/{info['uuid']}").status_code == 200

