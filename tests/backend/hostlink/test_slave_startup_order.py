"""HostLink Slave 启动顺序：先建链、对齐权威，再装配设备。

设备装配（resolve_device_definition）按权威优先取 Site 与持有的物料，所以 Slave
必须在连上 Host 之后才装配设备；设备装配完成后立刻通告 Host，不等下一个心跳。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from unilabos.backend.hostlink import main_hostlink_run
from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.client import get_hostlink_client
from unilabos.backend.hostlink.local_runtime import HostLinkLocalRuntime
from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.resources.adapters import device_site
from unilabos.resources.adapters.device_site import apply_device_authority_state
from unilabos.resources.resource_tracker import (
    ResourceDict,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)
from unilabos.server.backend.composition import set_materials_gateway
from unilabos.server.services.materials import MaterialsService

REGISTRY_NAME = "slave_site_device"
SITES = [{"index": 1, "label": "slot_1", "allowed_resource_categories": ["plate"]}]


class _Driver:
    """post_init 时记录 HostLink 是否已在线：新顺序下驱动启动时链路必须已连上。"""

    def __init__(self, **_kwargs: Any) -> None:
        self.online_at_post_init: bool | None = None

    def post_init(self, node: Any) -> None:
        client = get_hostlink_client()
        self.online_at_post_init = bool(client is not None and client.online)


def _resolve(device_id: str, device_config: ResourceDictInstance, *, backend_name=None):
    # 走真实的权威优先装配（经 Slave 的 HostLink 网关），只把注册表查找替换掉
    assert backend_name == "hostlink"
    apply_device_authority_state(device_config, {"available_sites": SITES}, REGISTRY_NAME)
    return SimpleNamespace(
        driver_class=_Driver,
        runtime_config={},
        registry_name=REGISTRY_NAME,
        display_name=device_id,
        action_value_mappings={},
        status_types={},
        hardware_interface={},
        resource_uuid=device_config.res_content.uuid,
    )


def _slave_graph(device_uuid: str, plate_uuid: str) -> ResourceTreeSet:
    device = ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": "slave-heater",
                "uuid": device_uuid,
                "name": "slave-heater",
                "type": "device",
                "class": REGISTRY_NAME,
                "template_name": REGISTRY_NAME,
                "config": {},
                "data": {},
                "extra": {},
            }
        )
    )
    plate = ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": "plate-1",
                "uuid": plate_uuid,
                "parent_uuid": device_uuid,
                "name": "plate-1",
                "type": "container",
                "class": "Container",
                "template_name": "test-plate-template",
                "config": {"type": "Container"},
                "data": {},
                "extra": {},
                "sites": [],
                "sites_initialized": True,
            }
        )
    )
    plate.res_content.parent = device.res_content
    device.children.append(plate)
    return ResourceTreeSet([ResourceTreeInstance(device)])


def test_slave_connects_then_aligns_then_assembles_devices_from_authority(
    tmp_path, monkeypatch
) -> None:
    service = MaterialsService(tmp_path / "host-materials.db")
    set_materials_gateway(LocalMaterialsClient(service))
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "host", "")
    # 心跳放到远大于测试时长：设备可见性只能来自装配后的即时通告
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 60.0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 120.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)
    monkeypatch.setattr(BasicConfig, "machine_name", "slave-startup-order")
    monkeypatch.setattr(main_hostlink_run, "resolve_device_definition", _resolve)
    monkeypatch.setattr(
        device_site, "registry_device_site_templates", lambda: {REGISTRY_NAME: SITES}
    )

    host = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    slave = None
    try:
        host.start()
        assert host.server is not None
        HostLinkConfig.host = "127.0.0.1"
        HostLinkConfig.port = host.server.port
        BasicConfig.is_host_mode = False

        device_uuid, plate_uuid = str(uuid4()), str(uuid4())
        graph = _slave_graph(device_uuid, plate_uuid)
        device_config = graph.root_nodes[0]
        assert device_config.res_content.sites_initialized is False

        slave = HostLinkBackend(HostLinkLocalRuntime(), is_slave=True)
        slave.start(
            populate=main_hostlink_run.startup_populate(graph, graph, is_slave=True)
        )

        # 1) 对齐先于装配：设备与它持有的板都在 Host 权威里，Site 由权威发放
        authority_device = service.get_material(device_uuid)
        assert [site.label for site in authority_device.sites] == ["slot_1"]
        assert service.get_material(plate_uuid).material.parent_material_uuid == device_uuid

        # 2) 装配时权威可达：设备配置里的 Site 就是权威那一份，持有的板进了 tracker
        resource = device_config.res_content
        assert resource.sites_initialized is True
        assert [site.uuid for site in resource.sites] == [
            site.site_uuid for site in authority_device.sites
        ]
        node = slave.local.devices["slave-heater"]
        assert plate_uuid in node.resource_tracker.uuid_to_resources
        assert node.driver.online_at_post_init is True

        # 3) 装配完立刻通告：Host 不用等心跳就看到 Slave 的设备
        assert host.server.has_device("slave-heater")
    finally:
        if slave is not None:
            slave.stop()
        host.stop()
        set_materials_gateway(None)
        service.close()
