from __future__ import annotations

import asyncio
from typing import Any

from unilabos.basic.runtime import BasicDeviceNode
from unilabos.device_runtime.resource import LocalResourceService, ResourceStore
from unilabos.resources.resource_tracker import ResourceTreeSet


class Driver:
    pass


def _resource(
    resource_id: str,
    resource_uuid: str,
    *,
    parent_uuid: str | None = None,
    resource_type: str = "container",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": resource_id,
        "uuid": resource_uuid,
        "name": resource_id,
        "parent_uuid": parent_uuid,
        "type": resource_type,
        "class": "",
        "config": {},
        "data": dict(data or {}),
        "extra": {},
    }


def test_resource_store_mounts_replaces_and_queries_subtrees() -> None:
    initial = ResourceTreeSet.from_raw_dict_list(
        [
            _resource("device", "device-uuid", resource_type="device"),
            _resource("material", "material-uuid", parent_uuid="device-uuid"),
        ]
    )
    store = ResourceStore(initial)
    replacement = ResourceTreeSet.from_raw_dict_list(
        [
            _resource(
                "material",
                "material-uuid",
                parent_uuid="device-uuid",
                data={"volume": 10},
            ),
            _resource("well", "well-uuid", parent_uuid="material-uuid"),
        ]
    )

    mapping = store.apply_update(replacement)

    assert mapping == {
        "material-uuid": "material-uuid",
        "well-uuid": "well-uuid",
    }
    material = store.resources.find_by_uuid("material-uuid")
    assert material is not None
    assert material.res_content.data == {"volume": 10}
    assert [child.res_content.uuid for child in material.children] == ["well-uuid"]

    complete = store.get_resources(["material-uuid"], with_children=True)
    shallow = store.get_resources(["material-uuid"], with_children=False)
    assert complete.all_nodes_uuid == ["material-uuid", "well-uuid"]
    assert shallow.all_nodes_uuid == ["material-uuid"]
    assert shallow.root_nodes[0].res_content.parent_uuid == "device-uuid"


def test_basic_device_node_uses_local_resource_service() -> None:
    initial = ResourceTreeSet.from_raw_dict_list(
        [_resource("device", "device-uuid", resource_type="device")]
    )
    store = ResourceStore(initial)
    node = BasicDeviceNode(
        Driver(),
        "device",
        resource_uuid="device-uuid",
    )
    node.set_resource_service(LocalResourceService(store))
    material = ResourceTreeSet.from_raw_dict_list(
        [_resource("material", "material-uuid")]
    )

    asyncio.run(node.update_resource(material))
    result = asyncio.run(node.get_resource(["material-uuid"]))

    assert result.all_nodes_uuid == ["material-uuid"]
    stored = store.resources.find_by_uuid("material-uuid")
    assert stored is not None
    assert stored.res_content.parent_uuid == "device-uuid"
