"""化学品定义：CAS / InChIKey 可空唯一键、软删除让出。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.api.materials.chemicals import create_chemical_router
from unilabos.server.services.materials.chemicals import (
    ChemicalError,
    ChemicalService,
    normalize_cas,
    normalize_inchi_key,
)

WATER = {"name": "水", "name_en": "water", "cas": "7732-18-5", "inchi_key": "XLYOFNOQVPJJNP-UHFFFAOYSA-N",
         "molecular_formula": "H2O", "molecular_weight": 18.015, "density_g_per_ml": 1.0, "physical_state": "liquid"}
ETHANOL = {"name": "乙醇", "aliases": ["酒精", "EtOH"], "cas": "64-17-5",
           "inchi_key": "lfqscwfljhtthz-uhfffaoysa-n", "molecular_formula": "C2H6O", "physical_state": "liquid"}


@pytest.fixture
def service(tmp_path):
    service = ChemicalService(tmp_path / "materials.db")
    yield service
    service.close()


def test_cas_and_inchi_key_are_validated() -> None:
    assert normalize_cas(" 7732-18-5 ") == "7732-18-5"
    with pytest.raises(ChemicalError, match="校验位"):
        normalize_cas("7732-18-4")
    with pytest.raises(ChemicalError, match="格式"):
        normalize_cas("water")
    assert normalize_inchi_key("xlyofnoqvpjjnp-uhfffaoysa-n") == "XLYOFNOQVPJJNP-UHFFFAOYSA-N"
    with pytest.raises(ChemicalError):
        normalize_inchi_key("not-a-key")


def test_create_get_search_and_optional_keys(service) -> None:
    water = service.create_chemical(WATER)
    ethanol = service.create_chemical(ETHANOL)
    unnamed = service.create_chemical({"name": "未知样品"})  # CAS / InChIKey 都可以为空
    second_unnamed = service.create_chemical({"name": "另一个未知样品"})

    assert ethanol["inchi_key"] == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert unnamed["cas"] is None and second_unnamed["inchi_key"] is None
    assert service.get_chemical(water["chemical_uuid"])["molecular_weight"] == pytest.approx(18.015)
    assert [item["name"] for item in service.list_chemicals(q="酒精")["items"]] == ["乙醇"]
    assert [item["name"] for item in service.list_chemicals(cas="7732-18-5")["items"]] == ["水"]
    assert service.list_chemicals(inchi_key="xlyofnoqvpjjnp-uhfffaoysa-n")["total"] == 1
    assert service.list_chemicals(physical_state="liquid")["total"] == 2


def test_duplicate_keys_conflict_until_soft_deleted(service) -> None:
    water = service.create_chemical(WATER)
    with pytest.raises(ChemicalError) as cas_conflict:
        service.create_chemical({"name": "重复的水", "cas": "7732-18-5"})
    assert cas_conflict.value.code == "conflict"
    with pytest.raises(ChemicalError) as key_conflict:
        service.create_chemical({"name": "重复的水", "inchi_key": "XLYOFNOQVPJJNP-UHFFFAOYSA-N"})
    assert key_conflict.value.code == "conflict"

    service.delete_chemical(water["chemical_uuid"], expected_version=water["version"])
    with pytest.raises(ChemicalError) as missing:
        service.get_chemical(water["chemical_uuid"])
    assert missing.value.code == "not_found"
    # 软删除让出唯一键，可以重新登记
    assert service.create_chemical(WATER)["cas"] == "7732-18-5"


def test_update_uses_optimistic_version(service) -> None:
    ethanol = service.create_chemical(ETHANOL)
    updated = service.update_chemical(
        ethanol["chemical_uuid"], {"density_g_per_ml": 0.789, "aliases": ["酒精"]},
        expected_version=ethanol["version"],
    )
    assert updated["version"] == ethanol["version"] + 1
    assert updated["density_g_per_ml"] == pytest.approx(0.789)
    assert updated["cas"] == "64-17-5"  # 未提交的字段保持原值
    with pytest.raises(ChemicalError) as stale:
        service.update_chemical(ethanol["chemical_uuid"], {"name": "x"}, expected_version=ethanol["version"])
    assert stale.value.code == "conflict"
    with pytest.raises(ChemicalError) as invalid:
        service.update_chemical(ethanol["chemical_uuid"], {"molecular_weight": -1}, expected_version=updated["version"])
    assert invalid.value.code == "invalid_input"


def test_http_envelope(service) -> None:
    app = FastAPI()
    app.include_router(create_chemical_router(service))
    client = TestClient(app)

    created = client.post("/api/v1/chemicals", json=WATER)
    assert created.status_code == 201 and created.json()["code"] == 0
    chemical = created.json()["data"]
    # 重复 CAS：化学品（V1 试剂信息）按资源口径，冲突为 4002
    assert client.post("/api/v1/chemicals", json=WATER).json()["code"] == 4002
    assert client.post("/api/v1/chemicals", json={"name": "坏的", "cas": "1-1-1"}).json()["code"] == 1000
    assert client.get("/api/v1/chemicals", params={"q": "water"}).json()["data"]["total"] == 1

    patched = client.patch(f"/api/v1/chemicals/{chemical['chemical_uuid']}",
                           json={"expected_version": chemical["version"], "description": "去离子"})
    assert patched.json()["data"]["description"] == "去离子"
    assert client.delete(f"/api/v1/chemicals/{chemical['chemical_uuid']}",
                         params={"expected_version": chemical["version"]}).json()["code"] == 4002
    assert client.delete(f"/api/v1/chemicals/{chemical['chemical_uuid']}",
                         params={"expected_version": patched.json()["data"]["version"]}).json()["code"] == 0
    assert client.get(f"/api/v1/chemicals/{chemical['chemical_uuid']}").json()["code"] == 4001
