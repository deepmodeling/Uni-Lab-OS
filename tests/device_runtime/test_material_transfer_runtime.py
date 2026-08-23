from __future__ import annotations

import asyncio

from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.device_runtime.resource import AuthorityResourceService
from unilabos.hostlink.backend import HostLinkBackend
from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.resources.presets.container import RegularContainer
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.scheduler.integration import set_materials_gateway
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
        "unilabos_resource_class": "runtime-transfer-container"
    }
    return resource


class _Driver:
    def __init__(self, device_id: str, **_kwargs) -> None:
        self.device_id = device_id
        self.added = []
        self.removed = []
        self.transferred = []

    def resource_tree_add(self, resources) -> None:
        self.added.extend(resources)

    def resource_tree_remove(self, resources) -> None:
        self.removed.extend(resources)

    def resource_tree_transfer(self, old_parent, resource, new_parent) -> None:
        self.transferred.append((old_parent, resource, new_parent))


def test_hostlink_runtime_transfer_is_committed_and_dispatched_by_authority(
    tmp_path,
    monkeypatch,
) -> None:
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    gateway = LocalMaterialsClient(materials)
    set_materials_gateway(gateway)
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)

    authority = AuthorityResourceService(gateway)
    material = asyncio.run(
        authority.create_resources("setup", "setup", _container("tube"))
    ).resources[0]
    mount = asyncio.run(
        authority.create_resources("setup", "setup", _container("mount"))
    ).resources[0]

    runtime = HostLinkLocalRuntime()
    source = runtime.add_driver(HostLinkDriverSpec("source", _Driver, {}))
    target = runtime.add_driver(HostLinkDriverSpec("target", _Driver, {}))
    source.resource_tracker.add_resource(material)
    target.resource_tracker.add_resource(mount)
    backend = HostLinkBackend(runtime, is_slave=False)
    try:
        backend.start()
        result = asyncio.run(
            source.transfer_resource_to_another(
                [material],
                "target",
                [mount],
                [None],
            )
        )

        material_uuid = material.unilabos_uuid
        assert result["success"] is True
        assert (
            materials.get_material(material_uuid).material.parent_material_uuid
            == mount.unilabos_uuid
        )
        assert material_uuid not in source.resource_tracker.uuid_to_resources
        attached = target.resource_tracker.uuid_to_resources[material_uuid]
        assert attached.parent is mount
        assert source.driver.removed == [material]
        assert target.driver.added == [attached]
        assert target.driver.transferred == [(None, attached, mount)]
    finally:
        backend.stop()
        set_materials_gateway(None)
        materials.repository.close()
