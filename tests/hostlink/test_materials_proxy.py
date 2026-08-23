from __future__ import annotations

from uuid import uuid4
import time

from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.materials import HostLinkMaterialsClient, LocalMaterialsClient
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.hostlink.backend import HostLinkBackend
from unilabos.hostlink.client import HostLinkClient
from unilabos.devices.virtual.heating_platform import VirtualHeatingPlatform
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    MaterialDataWrite,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.scheduler.integration import set_materials_gateway
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(
        command_uuid=str(uuid4()),
        effect_key=f"proxy-test:{operation}:{uuid4()}",
        operation=operation,
        actor_type="test",
        actor_uuid="hostlink-materials-proxy",
    )


def test_hostlink_proxy_supports_demo_template_create_and_passive_data_put(
    tmp_path, monkeypatch
) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
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
    finally:
        if client is not None:
            client.close()
        runtime.stop()
        set_materials_gateway(None)
        service.repository.close()


def test_remote_heating_demo_provisions_after_connect_and_writes_host_materials(
    tmp_path, monkeypatch
) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "remote-materials.db"))
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
        service.repository.close()
