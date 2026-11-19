from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from unilabos.client.materials import HostLinkMaterialsClient, LocalMaterialsClient
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.backend.runtime.resource import AuthorityResourceService
from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.backend.hostlink.protocol import RemoteError
from unilabos.resources.presets.container import RegularContainer
from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import MaterialTransfer, MaterialTransferItem
from unilabos.server.backend.composition import set_materials_gateway
from unilabos.server.services.materials import MaterialsService


def _container(name: str) -> RegularContainer:
    resource = RegularContainer(
        name=name,
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    resource.unilabos_extra = {
        "unilabos_resource_class": "hostlink-transfer-container"
    }
    return resource


class _Driver:
    def __init__(self, device_id: str, **_kwargs) -> None:
        self.device_id = device_id
        self.added = []
        self.removed = []

    def resource_tree_add(self, resources) -> None:
        self.added.extend(resources)

    def resource_tree_remove(self, resources) -> None:
        self.removed.extend(resources)


def _runtime(device_id: str) -> tuple[HostLinkLocalRuntime, object]:
    runtime = HostLinkLocalRuntime()
    node = runtime.add_driver(
        HostLinkDriverSpec(device_id, _Driver, {}, resource_uuid=f"{device_id}-uuid")
    )
    return runtime, node


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_slave_transfer_is_authorized_by_host_and_loads_target_service(
    tmp_path,
    monkeypatch,
) -> None:
    materials = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(materials)
    set_materials_gateway(gateway)
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    authority = AuthorityResourceService(gateway)
    source_material = asyncio.run(
        authority.create_resources("setup", "setup", _container("tube"))
    ).resources[0]
    target_mount = asyncio.run(
        authority.create_resources("setup", "setup", _container("mount"))
    ).resources[0]

    host = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    source_runtime, source_node = _runtime("source")
    target_runtime, target_node = _runtime("target")
    source_node.resource_tracker.add_resource(source_material)
    target_node.resource_tracker.add_resource(target_mount)
    source = HostLinkBackend(source_runtime, is_slave=True)
    target = HostLinkBackend(target_runtime, is_slave=True)

    try:
        host.start()
        assert host.server is not None
        HostLinkConfig.port = host.server.port
        BasicConfig.machine_name = "source-slave"
        source.start()
        BasicConfig.machine_name = "target-slave"
        target.start()
        assert _wait_until(
            lambda: {"source", "target"}.issubset(host.devices())
        )
        assert source.client is not None
        assert target.client is not None
        unauthorized_uuid = str(uuid4())
        with pytest.raises(RemoteError, match="未注册物料转移来源设备"):
            HostLinkMaterialsClient(target.client).transfer_material(
                InventoryMutation(
                    command_uuid=unauthorized_uuid,
                    effect_key=f"transfer_material:{unauthorized_uuid}",
                    operation="transfer_material",
                    actor_type="device",
                    actor_uuid="target",
                ),
                MaterialTransfer(
                    source_device_id="source",
                    target_device_id="target",
                    items=[
                        MaterialTransferItem(
                            material_uuid=source_material.unilabos_uuid,
                            target_material_uuid=target_mount.unilabos_uuid,
                        )
                    ],
                ),
            )
        from unilabos.resources import materials as materials_helper

        # 两个 Slave 在生产中是独立进程；测试同进程运行时显式选择来源
        # Slave 的连接，避免进程级 convenience getter 指向后启动的目标端。
        monkeypatch.setattr(
            materials_helper,
            "resolve_materials_gateway",
            lambda: HostLinkMaterialsClient(source.client),
        )

        result = asyncio.run(
            materials_helper.transfer(
                [source_material],
                "target",
                [target_mount],
                [None],
                source_device_id=source_node.device_id,
                source_device_uuid=source_node.resource_uuid,
            )
        )

        material_uuid = source_material.unilabos_uuid
        assert result["success"] is True
        assert (
            materials.get_material(material_uuid).material.parent_material_uuid
            == target_mount.unilabos_uuid
        )
        assert material_uuid not in source_node.resource_tracker.uuid_to_resources
        attached = target_node.resource_tracker.uuid_to_resources[material_uuid]
        assert attached.parent is target_mount
        assert source_node.driver.removed == [source_material]
        assert target_node.driver.added == [attached]
    finally:
        target.stop()
        source.stop()
        host.stop()
        set_materials_gateway(None)
        materials.close()
