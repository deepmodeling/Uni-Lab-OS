"""material_link 拓扑边：开机对齐幂等、CRUD、随物料删除清理与当前态导出。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.api_envelope import ok_data

from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import (
    MaterialDataWrite,
    MaterialDelete,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials.graph import GraphError, GraphService
from unilabos.server.services.materials import (
    MaterialNotFoundError,
    MaterialValidationError,
    MaterialsService,
    material_link_uuid,
)


def _mutation(operation: str, **values):
    return InventoryMutation(
        command_uuid=str(uuid4()),
        effect_key=operation,
        operation=operation,
        **values,
    )


def _create_root(service: MaterialsService, ref: str) -> str:
    result = service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(
            nodes=[
                MaterialNodeCreate(
                    client_ref=ref,
                    identity=MaterialIdentityWrite(
                        resource_id=f"resource-{ref}",
                        name=ref,
                        resource_type="container",
                        class_name="Container",
                        template_name="tube",
                    ),
                    data=MaterialDataWrite(),
                )
            ],
        ),
    )
    return result.data.root_material_uuid


@pytest.fixture()
def service(tmp_path):
    instance = MaterialsService(tmp_path / "materials.db")
    instance.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(
            template_uuid="tube-template",
            name="tube",
            display_name="Tube",
            resource_type="container",
            class_name="Container",
        ),
    )
    try:
        yield instance
    finally:
        instance.close()


class TestEnsureLinks:
    def test_startup_alignment_is_idempotent(self, service) -> None:
        source = _create_root(service, "pump")
        target = _create_root(service, "reactor")
        link = {
            "source": "resource-pump",
            "target": "resource-reactor",
            "source_uuid": source,
            "target_uuid": target,
            "type": "physical",
            "sourceHandle": "out",
            "targetHandle": "in",
            "id": "edge-1",
        }

        first = service.ensure_links([link])
        assert first == {"created": 1, "updated": 0, "unchanged": 0, "skipped": 0}

        second = service.ensure_links([link])
        assert second == {"created": 0, "updated": 0, "unchanged": 1, "skipped": 0}

        stored = service.list_links()
        assert len(stored) == 1
        record = stored[0]
        assert record["link_uuid"] == material_link_uuid(
            source,
            target,
            source_handle="out",
            target_handle="in",
            link_type="physical",
        )
        assert record["source_material_uuid"] == source
        assert record["target_material_uuid"] == target
        # 身份字段之外的原始 link 内容原样入 extra。
        assert record["extra"] == {"id": "edge-1"}
        assert record["version"] == 1

        # 内容变化（extra）时同一身份边递增版本，而不是新建。
        third = service.ensure_links([{**link, "id": "edge-1-renamed"}])
        assert third == {"created": 0, "updated": 1, "unchanged": 0, "skipped": 0}
        renamed = service.list_links()[0]
        assert renamed["link_uuid"] == record["link_uuid"]
        assert renamed["version"] == 2
        assert renamed["extra"] == {"id": "edge-1-renamed"}

    def test_unknown_endpoints_are_skipped(self, service) -> None:
        source = _create_root(service, "pump")
        stats = service.ensure_links(
            [
                {"source_uuid": source, "target_uuid": str(uuid4())},
                {"source": "no-uuid-at-all", "target": "still-none"},
            ]
        )
        assert stats == {"created": 0, "updated": 0, "unchanged": 0, "skipped": 2}
        assert service.list_links() == []

    @pytest.mark.parametrize("transport", ["local", "http"])
    def test_materials_client_aligns_links_on_the_authority(self, service, monkeypatch, transport) -> None:
        """Host 用同一个物料客户端对齐节点与连线：HTTP 客户端（默认拓扑的 Host 子进程）写的是权威的库。"""

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from tests.server.test_v1_inventory_api import _TestClientOpener
        from unilabos.client.materials import HTTPMaterialsClient, LocalMaterialsClient
        from unilabos.server.api.materials import install_materials_api

        source = _create_root(service, "pump")
        target = _create_root(service, "reactor")
        links = [
            {"source": "resource-pump", "target": "resource-reactor", "source_uuid": source, "target_uuid": target,
             "type": "fluid", "sourceHandle": "out", "targetHandle": "in", "id": "edge-1"},
            {"source": "resource-pump", "target": "off-graph", "source_uuid": source, "target_uuid": str(uuid4())},
        ]
        app = FastAPI()
        install_materials_api(app, service)
        with TestClient(app) as http:
            if transport == "http":
                monkeypatch.setattr("unilabos.client.materials.core.urlopen", _TestClientOpener(http))
                client = HTTPMaterialsClient("http://testserver")
            else:
                client = LocalMaterialsClient(service)
            assert client.ensure_links(links) == {"created": 1, "updated": 0, "unchanged": 0, "skipped": 1}
            assert client.ensure_links(links) == {"created": 0, "updated": 0, "unchanged": 1, "skipped": 1}
            relations = ok_data(http.get("/api/v1/inventory/relations"))
        [stored] = relations["links"]
        assert (stored["source_material_uuid"], stored["target_material_uuid"]) == (source, target)
        assert (stored["link_type"], stored["source_handle"], stored["target_handle"]) == ("fluid", "out", "in")
        assert stored["extra"] == {"id": "edge-1"}

    def test_material_uuid_filter_matches_both_sides(self, service) -> None:
        source = _create_root(service, "pump")
        middle = _create_root(service, "valve")
        target = _create_root(service, "reactor")
        service.ensure_links(
            [
                {"source_uuid": source, "target_uuid": middle},
                {"source_uuid": middle, "target_uuid": target},
            ]
        )

        assert len(service.list_links()) == 2
        assert len(service.list_links(material_uuid=middle)) == 2
        assert len(service.list_links(source_material_uuid=source)) == 1
        assert len(service.list_links(target_material_uuid=target)) == 1


class TestLinkCrud:
    def test_upsert_requires_existing_endpoints(self, service) -> None:
        source = _create_root(service, "pump")
        with pytest.raises(MaterialNotFoundError):
            service.upsert_link(
                source_material_uuid=source,
                target_material_uuid=str(uuid4()),
            )
        with pytest.raises(MaterialValidationError):
            service.upsert_link(
                source_material_uuid=source, target_material_uuid="  "
            )

    def test_upsert_and_delete_roundtrip(self, service) -> None:
        source = _create_root(service, "pump")
        target = _create_root(service, "reactor")
        created = service.upsert_link(
            source_material_uuid=source,
            target_material_uuid=target,
            link_type="communication",
            extra={"baud": 9600},
        )
        assert created["extra"] == {"baud": 9600}

        assert service.delete_link_record(created["link_uuid"]) is True
        assert service.delete_link_record(created["link_uuid"]) is False
        assert service.list_links() == []

    def test_delete_material_clears_touching_links(self, service) -> None:
        source = _create_root(service, "pump")
        target = _create_root(service, "reactor")
        service.upsert_link(
            source_material_uuid=source, target_material_uuid=target
        )

        service.delete_material(
            _mutation("delete_material"),
            MaterialDelete(material_uuid=target, recursive=True),
        )

        assert service.list_links() == []


class TestLivePayload:
    def test_requires_materials_service(self, tmp_path) -> None:
        # 纯库路径构造（如 -g 启动物化场景）没有物料服务，导出实时拓扑应报错；
        # 传入 MaterialsService 实例时构造函数自动绑定，不触发本分支。
        graph = GraphService(tmp_path / "materials.db")
        try:
            with pytest.raises(GraphError) as exc:
                graph.live_payload()
        finally:
            graph.close()
        assert exc.value.code == "unsupported"

    def test_serializes_authority_nodes_and_links(self, service) -> None:
        source = _create_root(service, "pump")
        target = _create_root(service, "reactor")
        service.ensure_links(
            [
                {
                    "source_uuid": source,
                    "target_uuid": target,
                    "type": "physical",
                    "sourceHandle": "out",
                    "targetHandle": "in",
                    "id": "edge-1",
                }
            ]
        )

        payload = GraphService(service).live_payload()

        node_ids = {node["id"] for node in payload["nodes"]}
        assert {"resource-pump", "resource-reactor"} <= node_ids
        node_uuids = {node["uuid"] for node in payload["nodes"]}
        assert {source, target} <= node_uuids

        assert payload["links"] == [
            {
                "source": "resource-pump",
                "target": "resource-reactor",
                "source_uuid": source,
                "target_uuid": target,
                "type": "physical",
                "sourceHandle": "out",
                "targetHandle": "in",
                "id": "edge-1",
            }
        ]

    def test_live_payload_keeps_parent_hierarchy(self, service) -> None:
        """运行期挂到设备下的台面（parent=设备物料）在实时拓扑里必须带 parent，
        前端才会把它和它的 sites 画进设备卡片，而不是当成独立根节点。"""

        service.put_template(
            _mutation("put_template"),
            ResourceTemplateWrite(
                template_uuid="bench-template", name="bench_demo", display_name="bench",
                resource_type="device", class_name="bench_demo",
            ),
        )
        device_uuid = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(nodes=[MaterialNodeCreate(
                client_ref="dev",
                identity=MaterialIdentityWrite(
                    resource_id="bench", name="物料工作台", resource_type="device",
                    class_name="bench_demo", template_name="bench_demo",
                ),
                data=MaterialDataWrite(),
            )]),
        ).data.root_material_uuid
        deck_uuid = _create_root(service, "deck")
        from unilabos.protocol.materials import MaterialMove

        service.move_material(
            _mutation("move_material"),
            MaterialMove(material_uuid=deck_uuid, parent_material_uuid=device_uuid),
        )

        payload = GraphService(service).live_payload()
        by_uuid = {node["uuid"]: node for node in payload["nodes"]}
        assert by_uuid[deck_uuid]["parent"] == "bench"
        assert by_uuid[deck_uuid]["parent_uuid"] == device_uuid
        assert by_uuid[device_uuid]["parent"] is None
