"""初始化时拆下的非 PLR 物料在权威里移出设备子树，这棵树之后的严格快照照常对得上。

升级前的旧数据里，PLR 物料下面可能挂着没有 ``config.type`` 的物料。设备初始化把它从本地树拆掉；
权威那棵树里若还挂着它，本地树与权威永远对不上，严格快照（观察者的整树提交）全被拒，这棵树上的
状态变化都会丢。节点拿到物料服务时把拆下的各枝移成独立根物料，两边重新一致。
"""

from __future__ import annotations

from uuid import uuid4

from pylabrobot.resources import Coordinate, Resource

from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.host.runtime.resource import AuthorityResourceService
from unilabos.client.materials import LocalMaterialsClient
from unilabos.resources import materials
from unilabos.resources.adapters.device_site import apply_device_authority_state
from unilabos.resources.resource_tracker import ResourceDict, ResourceDictInstance
from unilabos.server.services.materials import MaterialsService
from tests.plr_fixtures import plate

DEVICE_TEMPLATE = "legacy_detach_test_device"


class _Device:
    def __init__(self, **_kwargs) -> None:
        pass


def _device_config(device_uuid: str, gateway) -> ResourceDictInstance:
    config = ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": "placer", "uuid": device_uuid, "name": "placer", "type": "device",
                "class": DEVICE_TEMPLATE, "template_name": DEVICE_TEMPLATE,
                "config": {}, "data": {}, "extra": {}, "sites": None, "sites_initialized": False,
            }
        )
    )
    apply_device_authority_state(
        config, {"available_sites": [{"index": 1, "label": "slot_a"}]}, DEVICE_TEMPLATE, gateway=gateway
    )
    return config


def _start(device_uuid: str, gateway) -> tuple[HostLinkLocalRuntime, object]:
    runtime = HostLinkLocalRuntime()
    node = runtime.add_driver(
        HostLinkDriverSpec(
            "placer", _Device, {}, resource_uuid=device_uuid, device_config=_device_config(device_uuid, gateway)
        )
    )
    runtime.set_resource_service(AuthorityResourceService(gateway))
    runtime.start()
    return runtime, node


def test_a_legacy_child_is_moved_out_so_its_tree_snapshots_strictly_again(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    device_uuid = str(uuid4())
    try:
        runtime, node = _start(device_uuid, gateway)
        holder = Resource("holder", size_x=300, size_y=100, size_z=10, category="container")
        holder.assign_child_resource(plate("keep"), location=Coordinate(0, 0, 0))
        holder.assign_child_resource(
            Resource("legacy", size_x=10, size_y=10, size_z=10, category="container"),
            location=Coordinate(200, 0, 0),
        )
        created = materials.create(holder, node=node, site="slot_a", gateway=gateway)
        holder_uuid = created.unilabos_uuid
        legacy_uuid = next(child.unilabos_uuid for child in created.children if child.name == "legacy")
        runtime.stop()
        # 升级前留下的行：没有 config.type
        with service.write():
            service.connection.execute(
                "UPDATE material SET config_json=json_remove(config_json,'$.type') WHERE material_uuid=?",
                (legacy_uuid,),
            )

        runtime, node = _start(device_uuid, gateway)
        try:
            local_holder = node.resource_tracker.uuid_to_resources[holder_uuid]
            assert legacy_uuid not in node.resource_tracker.uuid_to_resources
            assert node.resource_tracker.legacy_material_uuids == []
            # 权威里它已是独立根物料，holder 那棵树与本地一致
            assert service.get_material(legacy_uuid).material.parent_material_uuid is None
            assert legacy_uuid not in {
                item.material.material_uuid for item in service.get_tree(holder_uuid).nodes
            }
            AuthorityResourceService(gateway)._update_sync(
                "placer", device_uuid, local_holder, allow_partial=False, conflict_retries=1
            )
        finally:
            runtime.stop()
    finally:
        service.close()
