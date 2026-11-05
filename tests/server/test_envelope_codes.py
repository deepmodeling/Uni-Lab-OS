"""信封式路由共用一张业务码表与 ``error: {msg}`` 形状。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.client.utils.envelope import parse_envelope
from unilabos.server.api.envelope import BUSINESS_CODES
from unilabos.server.api.materials.chemicals import install_chemical_api
from unilabos.server.api.materials.graph import install_graph_api
from unilabos.server.services.materials.chemicals import ChemicalService
from unilabos.server.services.materials.graph import GraphService


def _app(tmp_path) -> TestClient:
    app = FastAPI()
    chemicals = ChemicalService(tmp_path / "materials.db")
    install_chemical_api(app, chemicals)
    install_graph_api(app, GraphService(chemicals))
    return TestClient(app)


def test_code_table_is_shared() -> None:
    assert BUSINESS_CODES == {
        "ok": 0, "internal": 1, "invalid": 1000, "not_found": 3002, "conflict": 3003, "unavailable": 5001,
    }


def test_graph_and_chemical_errors_use_the_shared_shape(tmp_path) -> None:
    client = _app(tmp_path)
    missing_graph = client.get("/api/v1/graphs/gone").json()
    assert missing_graph["code"] == 3002 and isinstance(missing_graph["error"]["msg"], str)

    created = client.post("/api/v1/chemicals", json={"name": "水", "cas": "7732-18-5"}).json()
    duplicate = client.post("/api/v1/chemicals", json={"name": "水", "cas": "7732-18-5"}).json()
    assert created["code"] == 0 and duplicate["code"] == 3003 and duplicate["error"]["msg"]


def test_request_validation_returns_the_envelope_on_envelope_routes(tmp_path) -> None:
    client = _app(tmp_path)
    # 缺少必填字段 name：信封 1000，而不是 FastAPI 默认 422
    response = client.post("/api/v1/chemicals", json={"cas": "7732-18-5"})
    assert response.status_code == 200
    assert response.json()["code"] == 1000 and "name" in response.json()["error"]["msg"]
    bad_query = client.get("/api/v1/graphs", params={"page": 0})
    assert bad_query.status_code == 200 and bad_query.json()["code"] == 1000


def test_python_client_reads_the_message_from_the_error_object() -> None:
    envelope = parse_envelope({"code": 3003, "error": {"msg": "已有相同 CAS 登记号的化学品"}})
    assert envelope.error == "已有相同 CAS 登记号的化学品"
    assert parse_envelope({"code": 2, "error": "旧格式"}).error == "旧格式"
