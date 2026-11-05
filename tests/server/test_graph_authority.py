"""Graph Authority：GraphService CRUD、/api/v1/graphs envelope 与 HTTP 客户端契约。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.client.materials.graph import HTTPGraphClient
from unilabos.server.api.materials.graph import install_graph_api
from unilabos.server.services.materials.graph import (
    GraphError,
    GraphService,
    graph_uuid_for_name,
)

PAYLOAD = {
    "nodes": [{"id": "pump", "type": "device"}],
    "links": [],
}


@pytest.fixture()
def service(tmp_path):
    # 图快照与物料同库：lab_graph 表落 materials.db。
    instance = GraphService(tmp_path / "materials.db")
    try:
        yield instance
    finally:
        instance.close()


class TestGraphService:
    def test_upsert_is_idempotent_on_name(self, service) -> None:
        first = service.upsert_graph(name="lan-demo", payload=PAYLOAD)
        second = service.upsert_graph(name="lan-demo", payload=PAYLOAD)

        assert first["uuid"] == second["uuid"] == graph_uuid_for_name("lan-demo")
        # upsert 是对账入口：内容一致不递增 revision，内容变化才递增。
        assert first["revision"] == second["revision"] == 1
        third = service.upsert_graph(
            name="lan-demo", payload={"nodes": [{"id": "pump"}, {"id": "valve"}]}
        )
        assert third["revision"] == 2

    def test_explicit_uuid_wins_over_derivation(self, service) -> None:
        # 图 uuid 同时是节点发号的 uuid5 命名空间，必须是合法 UUID。
        fixed_uuid = "11111111-2222-4333-8444-555555555555"
        created = service.upsert_graph(name="custom", payload=PAYLOAD, uuid=fixed_uuid)
        assert created["uuid"] == fixed_uuid
        assert service.get_graph(fixed_uuid)["name"] == "custom"

    def test_get_resolves_uuid_and_name(self, service) -> None:
        created = service.upsert_graph(name="demo", payload=PAYLOAD)
        assert service.get_graph(created["uuid"])["name"] == "demo"
        # 存档的是发号后的权威 payload（节点带 uuid），uuid/名称双通道读取一致。
        stored = created["payload"]
        assert [node["id"] for node in stored["nodes"]] == ["pump"]
        assert stored["nodes"][0]["uuid"]
        assert service.get_graph("demo")["payload"] == stored
        assert service.get_payload("demo") == stored

    def test_get_unknown_raises_not_found(self, service) -> None:
        with pytest.raises(GraphError) as exc:
            service.get_graph("missing")
        assert exc.value.code == "not_found"

    def test_upsert_rejects_invalid_payload(self, service) -> None:
        with pytest.raises(GraphError) as exc:
            service.upsert_graph(name="bad", payload={"nodes": "not-a-list"})
        assert exc.value.code == "invalid_payload"
        with pytest.raises(GraphError):
            service.upsert_graph(name="bad", payload=["not", "object"])
        with pytest.raises(GraphError) as name_error:
            service.upsert_graph(name="  ", payload=PAYLOAD)
        assert name_error.value.code == "invalid_input"

    def test_list_paginates_and_filters(self, service) -> None:
        service.upsert_graph(name="alpha", payload=PAYLOAD)
        service.upsert_graph(name="beta", payload=PAYLOAD)

        listing = service.list_graphs(page=1, page_size=1)
        assert listing["total"] == 2 and len(listing["items"]) == 1

        filtered = service.list_graphs(name="alp")
        assert [item["name"] for item in filtered["items"]] == ["alpha"]
        item = filtered["items"][0]
        assert item["node_count"] == 1
        assert "payload" not in item

    def test_deleted_name_is_revived_with_same_uuid(self, service) -> None:
        created = service.upsert_graph(name="demo", payload=PAYLOAD)
        service.delete_graph("demo")
        with pytest.raises(GraphError):
            service.get_graph("demo")

        recreated = service.upsert_graph(name="demo", payload=PAYLOAD)
        assert recreated["uuid"] == created["uuid"]
        assert recreated["revision"] == created["revision"] + 1
        # 复活后节点身份沿图 uuid 稳定派生，与首次登记一致。
        assert recreated["payload"] == created["payload"]
        assert service.get_graph("demo")["payload"] == recreated["payload"]

    def test_delete_unknown_raises_not_found(self, service) -> None:
        with pytest.raises(GraphError) as exc:
            service.delete_graph("missing")
        assert exc.value.code == "not_found"


@pytest.fixture()
def api_client(service):
    app = FastAPI()
    install_graph_api(app, service)
    with TestClient(app) as client:
        yield client


class TestGraphAPI:
    def test_upsert_and_fetch_use_envelope(self, api_client) -> None:
        response = api_client.post(
            "/api/v1/graphs",
            json={"name": "lan-demo", "payload": PAYLOAD},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["code"] == 0
        assert body["data"]["uuid"] == graph_uuid_for_name("lan-demo")

        stored = body["data"]["payload"]
        assert stored["nodes"][0]["id"] == "pump" and stored["nodes"][0]["uuid"]

        fetched = api_client.get("/api/v1/graphs/lan-demo").json()
        assert fetched["code"] == 0
        assert fetched["data"]["payload"] == stored

        payload_only = api_client.get("/api/v1/graphs/lan-demo/payload").json()
        assert payload_only["data"] == stored

        listing = api_client.get("/api/v1/graphs").json()
        assert listing["code"] == 0 and listing["data"]["total"] == 1

    def test_not_found_maps_to_business_code(self, api_client) -> None:
        body = api_client.get("/api/v1/graphs/missing").json()
        assert body["code"] == 3002
        assert "error" in body

    def test_delete_then_fetch_returns_not_found(self, api_client) -> None:
        api_client.post(
            "/api/v1/graphs", json={"name": "gone", "payload": PAYLOAD}
        )
        deleted = api_client.delete("/api/v1/graphs/gone").json()
        assert deleted["code"] == 0
        assert api_client.get("/api/v1/graphs/gone").json()["code"] == 3002

    def test_invalid_payload_maps_to_input_code(self, api_client) -> None:
        body = api_client.post(
            "/api/v1/graphs",
            json={"name": "bad", "payload": {"nodes": "not-a-list"}},
        ).json()
        assert body["code"] == 1000


class _EnvelopeHTTPStub:
    """模拟 unilabos.client.http.HTTPClient：记录调用并返回已解包 data。"""

    def __init__(self, service: GraphService) -> None:
        self.service = service
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, path: str, params: dict[str, Any] | None = None):
        self.calls.append(("GET", path, dict(params or {})))
        if path == "/graphs":
            return self.service.list_graphs(**(params or {}))
        if path.endswith("/payload"):
            return self.service.get_payload(path.split("/")[2])
        return self.service.get_graph(path.split("/")[2])

    def post(self, path: str, json: dict[str, Any]):
        self.calls.append(("POST", path, json))
        return self.service.upsert_graph(
            name=json["name"],
            payload=json["payload"],
            uuid=json.get("uuid"),
            tags=json.get("tags") or [],
            description=json.get("description"),
            meta_data=json.get("meta_data") or {},
        )

    def delete(self, path: str):
        self.calls.append(("DELETE", path, {}))
        self.service.delete_graph(path.split("/")[2])
        return {}

    def close(self) -> None:
        self.calls.append(("CLOSE", "", {}))


class TestHTTPGraphClient:
    def test_full_lifecycle_over_envelope_contract(self, service) -> None:
        stub = _EnvelopeHTTPStub(service)
        client = HTTPGraphClient("http://127.0.0.1:8002", http_client=stub)

        created = client.upsert_graph(name="lan-demo", payload=PAYLOAD)
        assert created["revision"] == 1

        listing = client.list_graphs(name="lan")
        assert listing["total"] == 1

        assert client.get_graph("lan-demo")["uuid"] == created["uuid"]
        assert client.download_graph("lan-demo") == created["payload"]

        client.delete_graph("lan-demo")
        assert service.list_graphs()["total"] == 0

        methods = [call[0] for call in stub.calls]
        assert methods == ["POST", "GET", "GET", "GET", "DELETE"]
