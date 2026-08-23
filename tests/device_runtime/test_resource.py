from __future__ import annotations

import asyncio

from pylabrobot.resources import Coordinate
import pytest

from unilabos.device_runtime import resource as resource_module
from unilabos.device_runtime.resource import (
    AuthorityResourceService,
    MaterialSnapshotObserver,
)
from unilabos.resources.presets.container import RegularContainer
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.client.materials import LocalMaterialsClient
from unilabos.server.database.repositories.materials import MaterialsRepository
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
        "unilabos_resource_class": "authority-container"
    }
    return resource


def test_resource_service_create_get_and_partial_snapshot_update(tmp_path) -> None:
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    parent = _container("parent")
    child = _container("child")
    parent.assign_child_resource(child, Coordinate(1, 2, 3))
    try:
        created = asyncio.run(
            service.create_resources("device-1", "device-uuid", parent)
        )
        assert not getattr(parent, "unilabos_uuid", "")
        authoritative_parent = created.resources[0]
        authoritative_child = authoritative_parent.children[0]
        parent_uuid = authoritative_parent.unilabos_uuid
        child_uuid = authoritative_child.unilabos_uuid

        authoritative_child.tracker.set_liquids(
            [("NaCl", 250.0, "ug")]
        )
        updated = asyncio.run(
            service.update_resources(
                "device-1",
                "device-uuid",
                authoritative_child,
            )
        )

        assert updated.all_nodes_uuid == [parent_uuid, child_uuid]
        stored_child = materials.get_material(child_uuid)
        assert [
            (item.name, item.quantity, item.quantity_unit)
            for item in stored_child.data.substances
        ] == [("NaCl", 250.0, "ug")]

        downloaded = asyncio.run(
            service.get_resources(
                "device-1",
                [parent_uuid],
                with_children=False,
            )
        )
        assert downloaded.all_nodes_uuid == [parent_uuid]

        downloaded_by_id = service.get_resource_by_id_sync(
            "parent",
            with_children=True,
        )
        assert downloaded_by_id.all_nodes_uuid == [parent_uuid, child_uuid]

        deleted = service.delete_resources_sync(
            "device-1",
            "device-uuid",
            [parent_uuid],
        )
        assert set(deleted) == {parent_uuid, child_uuid}
        assert materials.list_materials() == []
    finally:
        materials.repository.close()


def test_resource_service_has_no_implicit_runtime_store() -> None:
    assert not hasattr(resource_module, "ResourceStore")
    assert not hasattr(resource_module, "LocalResourceService")


def test_resource_service_accepts_internal_create_draft(tmp_path) -> None:
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    draft = ResourceTreeSet.from_plr_resources(
        [_container("draft")], known_random_uuid=True
    )
    draft_uuid = draft.all_nodes_uuid[0]
    try:
        created = asyncio.run(
            service.create_resources("device-1", "device-uuid", draft)
        )
        assert created.tree.all_nodes_uuid != [draft_uuid]
        assert created.result.data.client_ref_map == {
            "node-0": created.tree.all_nodes_uuid[0]
        }
    finally:
        materials.repository.close()


def test_snapshot_observer_diffs_the_complete_root_with_all_descendants(
    tmp_path,
) -> None:
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    draft_root = _container("rack")
    draft_left = _container("left")
    draft_right = _container("right")
    draft_deep = _container("deep")
    draft_left.assign_child_resource(draft_deep, Coordinate(1, 1, 1))
    draft_root.assign_child_resource(draft_left, Coordinate(1, 2, 3))
    draft_root.assign_child_resource(draft_right, Coordinate(4, 5, 6))

    async def run() -> None:
        created = await service.create_resources(
            "device-1", "device-uuid", draft_root
        )
        root = created.resources[0]
        left, right = root.children
        deep = left.children[0]
        observer = MaterialSnapshotObserver(
            service,
            device_id=lambda: "device-1",
            device_uuid=lambda: "device-uuid",
            schedule=asyncio.create_task,
        )
        assert observer.observe(root) is True

        # 同一个 tick 修改两个不同深度的 child，只排一轮根树 snapshot。
        deep.tracker.set_liquids([("catalyst", 7.0, "ug")])
        right.tracker.set_liquids([("solvent", 12.0, "ul")])
        await observer.wait_idle()
        assert observer.errors == ()

        stored = materials.get_tree(root.unilabos_uuid)
        assert len(stored.nodes) == 4
        by_name = {node.material.name: node for node in stored.nodes}
        assert [
            (item.name, item.quantity, item.quantity_unit)
            for item in by_name["deep"].data.substances
        ] == [("catalyst", 7.0, "ug")]
        assert [
            (item.name, item.quantity, item.quantity_unit)
            for item in by_name["right"].data.substances
        ] == [("solvent", 12.0, "ul")]
        assert by_name["left"].material.parent_material_uuid == (
            by_name["rack"].material.material_uuid
        )
        assert by_name["deep"].material.parent_material_uuid == (
            by_name["left"].material.material_uuid
        )

        # 权威回灌期间的 PLR state 变化不允许反向形成 snapshot 回声。
        before_versions = {
            node.material.material_uuid: node.material.version
            for node in stored.nodes
        }
        with observer.suppress_authority_projection():
            right.tracker.set_liquids([("authority", 1.0, "ul")])
        await asyncio.sleep(0)
        await observer.wait_idle()
        unchanged = materials.get_tree(root.unilabos_uuid)
        assert {
            node.material.material_uuid: node.material.version
            for node in unchanged.nodes
        } == before_versions

    try:
        asyncio.run(run())
    finally:
        materials.repository.close()


def test_strict_snapshot_rejects_a_child_only_partial_tree(tmp_path) -> None:
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    parent = _container("parent")
    parent.assign_child_resource(_container("child"), Coordinate.zero())

    async def run() -> None:
        created = await service.create_resources(
            "device-1", "device-uuid", parent
        )
        with pytest.raises(
            ValueError,
            match="does not match downloaded material UUID set",
        ):
            await service.snapshot_resource_tree(
                "device-1",
                "device-uuid",
                created.resources[0].children[0],
            )

    try:
        asyncio.run(run())
    finally:
        materials.repository.close()
