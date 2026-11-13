"""设备侧把物料放到仓库（PLR ``ItemizedCarrier`` / ``WareHouse``）库位上（FRAMEWORK_GAPS #29 / #30）。

仓库只接受 ``assign_resource_to_site``：物料挂在库位 holder 下，仓库的 canonical Site
同时记占用。``materials.assign``（RESOURCE_APPEND）、权威 transfer 的 load、资源树下行
（RESOURCE_TREE_SYNC）都要走这条路径；之后整棵仓库树从权威反序列化（下次启动）不出错，
观察者的快照也不报错。
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import HostLinkConfig
from unilabos.protocol.materials import InventoryMutation, MaterialMove
from unilabos.resources import materials
from unilabos.resources.presets.itemized_carrier import Bottle
from unilabos.resources.presets.warehouse import warehouse_factory
from unilabos.server.backend.composition import set_materials_gateway
from unilabos.server.services.materials import MaterialsService

DEVICE_ID = "stocker"


class _StockerDriver:
    def __init__(self, **_kwargs) -> None:
        self.transfers: list[tuple] = []

    def resource_tree_transfer(self, old_parent, resource, new_parent) -> None:
        self.transfers.append((old_parent, resource, new_parent))


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


@pytest.fixture
def stocker(tmp_path, monkeypatch):
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    set_materials_gateway(gateway)
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    local = HostLinkLocalRuntime()
    node = local.add_driver(HostLinkDriverSpec(DEVICE_ID, _StockerDriver, {}, resource_uuid=str(uuid4())))
    backend = HostLinkBackend(local, is_slave=False)
    backend.start()
    try:
        warehouse = materials.create(
            warehouse_factory("stock", num_items_x=3, num_items_y=1, num_items_z=1), node=node
        )
        materials.assign(node, warehouse)
        yield service, gateway, node, warehouse
    finally:
        backend.stop()
        set_materials_gateway(None)
        service.close()


def _authority_placement(service: MaterialsService, warehouse) -> dict[str, tuple[str, str]]:
    """权威里仓库各 Site 的 {label: (占用物料名, 占用物料父节点名)}。"""

    tree = service.get_tree(warehouse.unilabos_uuid)
    by_uuid = {node.material.material_uuid: node.material for node in tree.nodes}
    owner = next(node for node in tree.nodes if node.material.material_uuid == warehouse.unilabos_uuid)
    return {
        site.label: (
            by_uuid[site.occupied_material_uuid].name,
            by_uuid[by_uuid[site.occupied_material_uuid].parent_material_uuid].name,
        )
        for site in owner.sites
        if site.occupied_material_uuid
    }


def _restored(gateway, warehouse):
    """下次启动的形态：整棵仓库树从权威反序列化。"""

    return materials.get(warehouse.unilabos_uuid, gateway=gateway).to_plr_resources()[0]


def _bottle(name: str) -> Bottle:
    return Bottle(name, diameter=20, height=50, max_volume=100)


def _observer_errors(node) -> tuple:
    observer = node.resource_tracker._material_snapshot_observer
    return observer.errors if observer is not None else ()


def test_assign_onto_a_warehouse_site(stocker) -> None:
    """#29：materials.assign / RESOURCE_APPEND 按库位放上仓库。"""

    service, gateway, node, warehouse = stocker
    bottle = materials.create(_bottle("assigned"), node=node)

    materials.assign(node, bottle, parent=warehouse, slot="A02")

    assert warehouse["A02"].resource is bottle
    assert _authority_placement(service, warehouse) == {"A02": ("assigned", warehouse["A02"].name)}
    assert _restored(gateway, warehouse)["A02"].resource.name == "assigned"
    assert _observer_errors(node) == ()

    # 不指定库位：仓库只能按库位放物料，明确报错，权威不变
    loose = materials.create(_bottle("loose"), node=node)
    with pytest.raises(Exception, match="挂载"):
        materials.assign(node, loose, parent=warehouse)
    assert _authority_placement(service, warehouse) == {"A02": ("assigned", warehouse["A02"].name)}


def test_transfer_onto_moves_between_and_off_warehouse_sites(stocker) -> None:
    """#30：transfer 的 load 经仓库放到库位；换位、移走后本地与权威一致，可重新反序列化。"""

    service, gateway, node, warehouse = stocker
    bottle = materials.create(_bottle("moved"), node=node)

    asyncio.run(materials.transfer(bottle, DEVICE_ID, warehouse, "A01", source_device_id=DEVICE_ID))
    assert warehouse["A01"].resource is not None and warehouse["A01"].resource.name == "moved"
    assert _authority_placement(service, warehouse) == {"A01": ("moved", warehouse["A01"].name)}
    # load 返回前已补报位姿：transfer 一结束整棵树就能从权威反序列化
    assert _restored(gateway, warehouse)["A01"].resource.name == "moved"

    asyncio.run(
        materials.transfer(bottle.unilabos_uuid, DEVICE_ID, warehouse, "A03", source_device_id=DEVICE_ID)
    )
    assert warehouse["A01"].resource is None
    assert warehouse["A03"].resource.name == "moved"
    assert _authority_placement(service, warehouse) == {"A03": ("moved", warehouse["A03"].name)}
    # 仓库的 canonical Site 与 holder 同步：快照不残留旧占用
    assert [site.occupied_material_uuid for site in warehouse.resource_sites] == [
        None, None, bottle.unilabos_uuid,
    ]
    assert _restored(gateway, warehouse)["A03"].resource.name == "moved"
    moved = warehouse["A03"].resource
    moved.tracker.set_liquids([("water", 5.0, "ul")])
    materials.update(node, moved)
    assert _observer_errors(node) == ()

    # 删除：权威删物料，资源树下行把本地实例从 holder 上卸下（直接经 holder，不经仓库）
    materials.remove(
        bottle.unilabos_uuid, source_device_id=DEVICE_ID, source_device_uuid=node.resource_uuid
    )
    asyncio.run(node.apply_resource_tree_update([{"action": "remove", "data": [bottle.unilabos_uuid]}]))
    assert warehouse["A03"].resource is None
    assert [site.occupied_material_uuid for site in warehouse.resource_sites] == [None, None, None]
    assert _authority_placement(service, warehouse) == {}
    # 设备整树上报与权威一致：canonical Site 不残留已删除的物料
    materials.update(node, warehouse)
    assert _restored(gateway, warehouse)["A03"].resource is None
    assert _observer_errors(node) == ()


def test_tree_update_after_an_authority_move_lands_on_the_site(stocker) -> None:
    """#29：UI 拖拽 / 权威 move 之后的资源树下行（add）同样经仓库放到库位。"""

    service, gateway, node, warehouse = stocker
    bottle = materials.create(_bottle("dragged"), gateway=gateway)
    site = next(item for item in service.get_material(warehouse.unilabos_uuid).sites if item.label == "A02")
    service.move_material(
        InventoryMutation(
            command_uuid=str(uuid4()), effect_key="drag", operation="move_material", actor_type="human"
        ),
        MaterialMove(material_uuid=bottle.unilabos_uuid, destination_site_uuid=site.site_uuid),
    )

    result = asyncio.run(
        node.apply_resource_tree_update([{"action": "add", "data": [bottle.unilabos_uuid]}])
    )

    assert result["results"] == [{"success": True, "action": "add"}]
    assert warehouse["A02"].resource is not None and warehouse["A02"].resource.name == "dragged"
    assert warehouse.resource_sites[1].occupied_material_uuid == bottle.unilabos_uuid
    assert _authority_placement(service, warehouse) == {"A02": ("dragged", warehouse["A02"].name)}
    assert _restored(gateway, warehouse)["A02"].resource.name == "dragged"
