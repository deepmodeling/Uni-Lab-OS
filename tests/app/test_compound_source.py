"""OS 外部化合物数据源适配测试。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.compound_source import (
    CompoundNotFoundError,
    PubChemCompoundSource,
)
from unilabos.app.scheduler.inventory.store import InventoryStore


def test_pubchem_source_maps_pug_properties(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b'{"PropertyTable":{"Properties":[{"Title":"Ethanol","MolecularFormula":"C2H6O","IsomericSMILES":"CCO","InChIKey":"LFQSCWFLJHTTHZ-UHFFFAOYSA-N","MolecularWeight":"46.07"}]}}'

    monkeypatch.setattr(
        "unilabos.app.scheduler.inventory.compound_source.urlopen",
        lambda request, timeout: Response(),
    )
    source = PubChemCompoundSource("https://pubchem.ncbi.nlm.nih.gov", 30)

    assert source.lookup_by_cas("64-17-5") == {
        "name": "Ethanol",
        "molecular_formula": "C2H6O",
        "smiles": "CCO",
        "inchi_key": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        "molecular_weight": 46.07,
    }


def test_compound_endpoint_uses_external_source_when_local_identity_missing(tmp_path) -> None:
    class Source:
        def lookup_by_cas(self, cas):
            if cas == "67-56-1":
                raise CompoundNotFoundError(cas)
            return {"name": "Ethanol", "molecular_formula": "C2H6O"}

    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(
        app,
        BackendResourceService(store),
        compound_source=Source(),
    )
    client = TestClient(app)

    assert client.get("/api/v1/compounds/64-17-5").json()["data"] == {
        "cas": "64-17-5",
        "status": "ok",
        "compound": {"name": "Ethanol", "molecular_formula": "C2H6O"},
    }
    assert client.get("/api/v1/compounds/67-56-1").json()["data"]["status"] == "not_found"
    store.close()
