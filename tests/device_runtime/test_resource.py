from __future__ import annotations

import asyncio

from pylabrobot.resources import Coordinate

from unilabos.device_runtime import resource as resource_module
from unilabos.device_runtime.resource import AuthorityResourceService
from unilabos.resources.container import RegularContainer
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.clients.materials import LocalMaterialsClient
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
    materials = MaterialsService(tmp_path / "materials.db")
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
        materials.close()


def test_resource_service_has_no_implicit_runtime_store() -> None:
    assert not hasattr(resource_module, "ResourceStore")
    assert not hasattr(resource_module, "LocalResourceService")


def test_resource_service_accepts_internal_create_draft(tmp_path) -> None:
    materials = MaterialsService(tmp_path / "materials.db")
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
        materials.close()
