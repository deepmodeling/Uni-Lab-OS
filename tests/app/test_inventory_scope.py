"""当前库存与离站历史经公共物料 API 按持久物理事实分离。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.test_backend_resource_api import _client, _sync_template
from tests.app.test_station_handoff import frozen, package, setup  # noqa: F401
from tests.app.test_station_handoff_pickup import picked_up
from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService


def assert_history_is_read_only(client, identity):
    """普通字段、位姿、状态和删除都不能改写离站记录。"""
    path = f"/api/v1/materials/{identity}"
    before = client.get(path).json()["data"]
    for values in ({"name": "changed"}, {"barcode": "CHANGED"},
                   {"description": "changed"}, {"meta_data": {"changed": True}},
                   {"relative_position": {"position_x": 7}}, {"config": {"changed": True}}):
        assert client.put(path, json=values).json()["code"] == 6011
        assert client.get(path).json()["data"] == before
    assert client.post(path + "/states", json={"state_data": {"changed": True}}).json()["code"] == 6011
    assert client.delete(path).json()["code"] == 6011
    assert client.delete(path, params={"scope": "material_with_contents"}).json()["code"] == 6011
    assert client.get(path).json()["data"] == before


def test_history_filters_search_totals_pages_graph_and_keeps_detail(tmp_path):
    """离站与已撤销接收物料不出现在默认搜索，历史可查且状态不丢失。"""
    client, store = _client(tmp_path)
    template = _sync_template(client)
    ids = {}
    states = ("warehouse", "handoff_outgoing", "handoff_departed", "incoming_canceled")
    for index, state in enumerate(states):
        material = client.post("/api/v1/materials", json={
            "resource_template_uuid": template, "name": f"scope-{index}",
            "barcode": f"SCOPE-{index}",
        }).json()["data"]
        ids[state] = material["uuid"]
        with store.transaction() as connection:
            connection.execute("UPDATE material SET inventory_status=?,create_time=? WHERE uuid=?",
                               (state, f"2026-10-31T00:00:0{index}Z", material["uuid"]))
    for scope, expected in (("current", states[:2]), ("history", states[2:])):
        data = client.get("/api/v1/materials", params={"inventory_scope": scope}).json()["data"]
        assert data["total"] == 2
        assert {row["uuid"] for row in data["items"]} == {ids[state] for state in expected}
        for page, state in enumerate(reversed(expected), start=1):
            data = client.get("/api/v1/materials", params={
                "inventory_scope": scope, "page": page, "page_size": 1,
                "name": "scope", "resource_template_uuid": template,
            }).json()["data"]
            assert data["total"] == 2
            assert [row["uuid"] for row in data["items"]] == [ids[state]]
        graph = client.get("/api/v1/materials/graph", params={"inventory_scope": scope}).json()["data"]
        assert {node["material"]["uuid"] for node in graph["nodes"]} == {ids[state] for state in expected}
        assert {node["material"]["inventory_presence"] for node in graph["nodes"]} == {
            "current" if scope == "current" else "departed"
        }
    assert client.get("/api/v1/materials", params={"barcode": "SCOPE-2"}).json()["data"]["total"] == 0
    history = client.get("/api/v1/materials", params={"barcode": "SCOPE-2", "inventory_scope": "history"}).json()["data"]
    assert history["total"] == 1
    detail = client.get(f"/api/v1/materials/{ids['handoff_departed']}").json()["data"]
    assert detail["inventory_status"] == "handoff_departed"
    assert detail["disposition"] == "active"
    assert detail["inventory_presence"] == "departed"
    for state in states[2:]:
        assert_history_is_read_only(client, ids[state])
    assert client.put(f"/api/v1/materials/{ids['warehouse']}", json={"name": "still-current"}).json()["code"] == 0
    store.close()


@pytest.mark.parametrize("path", ["/api/v1/materials", "/api/v1/materials/graph"])
def test_inventory_scope_rejects_unknown_values(tmp_path, path):
    """非法范围不得静默降级为当前或全部物料。"""
    client, store = _client(tmp_path)
    assert client.get(path, params={"inventory_scope": "all"}).json()["code"] == 1000
    store.close()


def test_pickup_moves_protected_carrier_and_children_to_history_before_receipt(setup):
    """真实取货应用后即离站；库存锁保留且取消不会使在途物料回流列表。"""
    from tests.app.test_station_handoff_adapter import add_nested_sample

    s = setup
    added = add_nested_sample(s)
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(s["stores"]["source"]))
    client = TestClient(app)
    identities = {s["carrier"]["uuid"], added["child"]["uuid"]}
    frozen(s)
    before = client.get("/api/v1/materials").json()["data"]["items"]
    assert identities <= {row["uuid"] for row in before}
    payload = picked_up(s)
    s["auth"]["source"].apply_center_snapshot(package(payload))
    for canceled in (False, True):
        if canceled:
            payload.update(status="canceling", revision=5, attention=True,
                           attention_reasons=["TRANSPORT_EXECUTION_UNCERTAIN"])
            s["auth"]["source"].apply_center_snapshot(package(payload))
        current = client.get("/api/v1/materials").json()["data"]["items"]
        assert identities.isdisjoint({row["uuid"] for row in current})
        history = client.get("/api/v1/materials/graph", params={"inventory_scope": "history"}).json()["data"]["nodes"]
        assert identities <= {node["material"]["uuid"] for node in history}
        for identity in identities:
            detail = client.get(f"/api/v1/materials/{identity}").json()["data"]
            assert detail["inventory_presence"] == "departed"
            assert detail["inventory_status"] != "warehouse"
    assert payload["items"][0]["receipt"] is None
    for identity in identities:
        assert_history_is_read_only(client, identity)
