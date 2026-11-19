from __future__ import annotations

from uuid import uuid4
import time

from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.materials import HostLinkMaterialsClient, LocalMaterialsClient
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.client import HostLinkClient
from unilabos.devices.virtual.heating_platform import VirtualHeatingPlatform
from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import (
    MaterialDataWrite,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.backend.composition import set_materials_gateway
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(
        command_uuid=str(uuid4()),
        effect_key=f"proxy-test:{operation}:{uuid4()}",
        operation=operation,
        actor_type="test",
        actor_uuid="hostlink-materials-proxy",
    )


def test_template_list_query_decodes_hostlink_payload() -> None:
    from unilabos.backend.hostlink.materials_proxy import template_list_query

    # 链路默认目录模式：不带 include_definition 的对端拿 name/uuid/hash，不拿 definition
    assert template_list_query(None) == {"name": None, "include_definition": False}
    assert template_list_query({"name": "", "include_definition": True}) == {
        "name": None,
        "include_definition": True,
    }
    assert template_list_query({"name": "deck"}) == {
        "name": "deck",
        "include_definition": False,
    }


def test_template_list_warns_once_per_peer_about_full_definitions(monkeypatch) -> None:
    """显式要完整 definition 又不带 name 的全量列表是 Slave 侧最大的浪费，Host 提示一次；
    默认的目录模式和按 name 取正文都不算。"""

    from unilabos.backend.hostlink import materials_proxy

    warnings: list[str] = []
    monkeypatch.setattr(materials_proxy, "_warned_peers", set())
    monkeypatch.setattr(
        materials_proxy.logger, "warning", lambda msg, *args: warnings.append(msg % args)
    )

    class Gateway:
        def list_templates(self, *, name=None, include_definition=False):
            return []

    peer = {"node_id": "slave-1", "machine_name": "bench"}
    materials_proxy.template_list(Gateway(), {}, peer)
    materials_proxy.template_list(Gateway(), {"name": "deck"}, peer)
    materials_proxy.template_list(Gateway(), {"name": "deck", "include_definition": True}, peer)
    assert warnings == []

    materials_proxy.template_list(Gateway(), {"include_definition": True}, peer)
    materials_proxy.template_list(Gateway(), {"include_definition": True}, peer)
    assert len(warnings) == 1
    assert "bench" in warnings[0] and "definition" in warnings[0]


def test_hostlink_proxy_supports_demo_template_create_and_passive_data_put(
    tmp_path, monkeypatch
) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    set_materials_gateway(LocalMaterialsClient(service))
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 1.0)

    runtime = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    client = None
    try:
        runtime.start()
        assert runtime.server is not None
        client = HostLinkClient(
            "127.0.0.1",
            runtime.server.port,
            machine_name="materials-proxy-test",
            heartbeat_interval=0.05,
            connect_timeout=1.0,
            request_timeout=1.0,
        )
        assert client.connect_blocking(1.0)
        materials = HostLinkMaterialsClient(client)

        materials.create_template(
            _mutation("put_template"),
            ResourceTemplateWrite(
                name="proxy-demo-sample",
                display_name="Proxy demo sample",
                class_name="Resource",
                category=["heating_sample"],
            ),
        )
        assert [item.name for item in materials.list_templates()] == [
            "proxy-demo-sample"
        ]
        # 存在性检查 / 按名取 uuid：筛选在权威侧完成，链路上默认只回目录字段
        found = materials.list_templates(name="proxy-demo-sample")
        assert [item.name for item in found] == ["proxy-demo-sample"]
        assert found[0].template_uuid
        assert found[0].definition == {}
        assert materials.list_templates(name="no-such-template") == []
        # 只有显式开 include_definition 才把 definition 正文拖过链路
        full = materials.list_templates(name="proxy-demo-sample", include_definition=True)
        assert full[0].definition_hash == found[0].definition_hash

        created = materials.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[
                    MaterialNodeCreate(
                        client_ref="sample",
                        identity=MaterialIdentityWrite(
                            resource_id="proxy-demo-sample-1",
                            name="Proxy sample 1",
                            template_name="proxy-demo-sample",
                        ),
                        data=MaterialDataWrite(data={"temperature_c": 25.0}),
                    )
                ]
            ),
        )
        material_uuid = created.data.nodes[0].material.material_uuid
        materials.put_data(
            _mutation("put_data"),
            material_uuid,
            MaterialDataWrite(
                data={
                    "temperature_c": 63.5,
                    "temperature_source": {
                        "device_id": "virtual-heater",
                        "property": "site_1_temperature_c",
                    },
                },
                source_job_uuid="demo-job",
            ),
        )

        material = service.get_material(material_uuid)
        assert material.data.data["temperature_c"] == 63.5
        assert material.data.source_job_uuid == "demo-job"

        # 增量上报也经 Host 代理：只带一个节点的一段，回 affected 版本，不回整树
        from unilabos.protocol.materials import MaterialDataDelta, MaterialDelta, MaterialNodeDelta

        delta_result = materials.apply_delta(
            _mutation("apply_material_delta"),
            MaterialDelta(
                root_material_uuid=material_uuid,
                nodes=[
                    MaterialNodeDelta(
                        material_uuid=material_uuid,
                        expected_version=material.material.version,
                        data=MaterialDataDelta(data={**material.data.data, "temperature_c": 70.0}),
                    )
                ],
            ),
        )
        assert delta_result.data.applied_material_uuids == [material_uuid]
        assert delta_result.affected[0].version == material.material.version + 1
        assert service.get_material(material_uuid).data.data["temperature_c"] == 70.0
    finally:
        if client is not None:
            client.close()
        runtime.stop()
        set_materials_gateway(None)
        service.close()


def test_remote_heating_demo_provisions_after_connect_and_writes_host_materials(
    tmp_path, monkeypatch
) -> None:
    service = MaterialsService(tmp_path / "remote-materials.db")
    set_materials_gateway(LocalMaterialsClient(service))
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "host", "")
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 1.0)
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)
    monkeypatch.setattr(BasicConfig, "machine_name", "remote-heating-demo")

    host = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    slave = None
    try:
        host.start()
        assert host.server is not None
        HostLinkConfig.host = "127.0.0.1"
        HostLinkConfig.port = host.server.port
        BasicConfig.is_host_mode = False

        local = HostLinkLocalRuntime()
        local.add_driver(
            HostLinkDriverSpec(
                device_id="remote-virtual-heater",
                driver_class=VirtualHeatingPlatform,
                config={"update_interval_s": 0.05},
                registry_name="virtual_heating_platform",
                action_names=("heat_site",),
                status_names=(
                    "site_1_temperature_c",
                    "site_2_temperature_c",
                    "site_3_temperature_c",
                ),
            )
        )
        slave = HostLinkBackend(local, is_slave=True)
        slave.start()

        deadline = time.monotonic() + 3.0
        root = None
        while time.monotonic() < deadline:
            try:
                root = service.get_material_by_resource_id("remote-virtual-heater")
                if len(root.sites) == 3 and all(
                    site.occupied_material_uuid for site in root.sites
                ):
                    break
            except Exception:
                root = None
            time.sleep(0.05)
        assert root is not None
        assert len(root.sites) == 3
        assert all(site.occupied_material_uuid for site in root.sites)

        result = host.call_action(
            "remote-virtual-heater",
            "heat_site",
            site_id=3,
            target_temperature_c=68.0,
            duration_seconds=0.1,
        )
        material = service.get_material(result["material_uuid"])
        assert material.data.data["temperature_c"] == 68.0
        assert "temperature_history" not in material.data.data
        assert material.data.data["temperature_source"]["property"] == (
            "site_3_temperature_c"
        )
    finally:
        if slave is not None:
            slave.stop()
        host.stop()
        set_materials_gateway(None)
        service.close()
