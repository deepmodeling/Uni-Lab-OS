"""空容器候选过滤复用内容登记事实，先过滤再分页。"""

import pytest

from tests.app.test_backend_container_content_api import _client, _container, _reagent_info


@pytest.mark.parametrize("kind", ["reagents", "samples", "current-substances"])
@pytest.mark.parametrize("quantity", [0, 1])
def test_empty_container_filter_excludes_active_content_and_restores_removed(tmp_path, kind, quantity):
    client, store = _client(tmp_path)
    try:
        empty = _container(client, "空瓶")
        filled = _container(client, "已装料")
        values = {"material_uuid": filled, "quantity": 1, "quantity_unit": "mL"}
        if kind == "reagents":
            values["reagent_info_uuid"] = _reagent_info(client)
        elif kind == "samples":
            values.update(code="sample", name="样品")
        else:
            values.update(name="溶液", physical_state="liquid")
        created = client.post(f"/api/v1/{kind}", json=values)
        assert created.status_code == 201, created.text
        if quantity == 0:
            updated = client.put(f"/api/v1/{kind}/{created.json()['data']['uuid']}", json={**values, "quantity": 0})
            assert updated.json()["code"] == 0, updated.text

        # 默认行为不变，过滤仅由录入空容器的调用方显式启用。
        regular = client.get("/api/v1/materials", params={"category": "material"}).json()["data"]
        assert {item["uuid"] for item in regular["items"]} == {empty, filled}
        params = {"empty_containers_only": "true", "page": 1, "page_size": 1}
        filtered = client.get("/api/v1/materials", params=params).json()["data"]
        assert filtered["total"] == 1
        assert [item["uuid"] for item in filtered["items"]] == [empty]
        assert client.get("/api/v1/materials", params={**params, "page": 2}).json()["data"]["items"] == []
        assert client.get("/api/v1/materials", params={**params, "name": "已装料"}).json()["data"]["total"] == 0

        # 余量归零不等于清空登记；真正移除内容后才重新成为空容器。
        removed = client.delete(f"/api/v1/{kind}/{created.json()['data']['uuid']}")
        assert removed.json()["code"] == 0, removed.text
        restored = client.get("/api/v1/materials", params={**params, "page_size": 20}).json()["data"]
        assert restored["total"] == 2
        assert {item["uuid"] for item in restored["items"]} == {empty, filled}
    finally:
        store.close()


@pytest.mark.parametrize("tags,expected", [('["container"]', 1), ('[" Container "]', 1), ('["notcontainer"]', 0), ('[]', 0)])
def test_empty_container_filter_requires_container_template(tmp_path, tags, expected):
    client, store = _client(tmp_path)
    try:
        container = _container(client, "候选")
        with store.transaction() as connection:
            connection.execute("UPDATE resource_template SET tags=?", (tags,))
        filtered = client.get("/api/v1/materials", params={"empty_containers_only": "true"}).json()["data"]
        assert filtered["total"] == expected
        assert [item["uuid"] for item in filtered["items"]] == ([container] if expected else [])
    finally:
        store.close()
