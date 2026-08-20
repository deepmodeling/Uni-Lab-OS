from __future__ import annotations

import asyncio

from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.resources.presets.container import RegularContainer
from unilabos.resources.resource_tracker import ResourceTreeSet


def _container(name: str, material_uuid: str) -> RegularContainer:
    resource = RegularContainer(
        name=name,
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    resource.unilabos_uuid = material_uuid
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


class _MoveResourceService:
    def __init__(self, moved_resource: RegularContainer) -> None:
        self.tree = ResourceTreeSet.from_plr_resources([moved_resource])
        self.moves = []

    async def move_resources(
        self,
        device_id,
        device_uuid,
        resources_uuid,
        target_resources_uuid,
        sites,
    ):
        self.moves.append(
            (
                device_id,
                device_uuid,
                list(resources_uuid),
                list(target_resources_uuid),
                list(sites),
            )
        )
        self.tree.root_nodes[0].res_content.parent_uuid = target_resources_uuid[0]
        return []

    async def get_resources(self, _device_id, resources_uuid, _with_children):
        assert list(resources_uuid) == self.tree.root_nodes_uuid
        return ResourceTreeSet.load(self.tree.dump())


def test_hostlink_runtime_moves_material_through_common_authority_and_service() -> None:
    runtime = HostLinkLocalRuntime()
    source = runtime.add_driver(HostLinkDriverSpec("source", _Driver, {}))
    target = runtime.add_driver(HostLinkDriverSpec("target", _Driver, {}))
    material = _container("tube", "material-1")
    mount = _container("deck", "mount-1")
    source.resource_tracker.add_resource(material)
    target.resource_tracker.add_resource(mount)
    authority = _MoveResourceService(material)
    runtime.set_resource_service(authority)  # type: ignore[arg-type]
    runtime.start()
    try:
        result = asyncio.run(
            source.transfer_resource_to_another(
                [material],
                "target",
                [mount],
                [None],
            )
        )

        assert result["success"] is True
        assert authority.moves == [
            (
                "source",
                "",
                ["material-1"],
                ["mount-1"],
                [None],
            )
        ]
        assert "material-1" not in source.resource_tracker.uuid_to_resources
        attached = target.resource_tracker.uuid_to_resources["material-1"]
        assert attached.parent is mount
        assert source.driver.removed == [material]
        assert target.driver.added == [attached]
        assert target.driver.transferred == [(None, attached, mount)]
    finally:
        runtime.stop()
