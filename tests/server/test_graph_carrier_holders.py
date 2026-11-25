"""设备图里的载架：槽位 holder 是 Site 的结构节点，不是 Site 的占用物料。

载架（``ItemizedCarrier`` / ``WareHouse``）的直接子节点只能是槽位 holder，holder 就放在它
那个 Site 的位置上；占用物料挂在 holder 下。图里按位置补占用时把 holder 当成占用物料，空 Site
就被记成「被自己的 holder 占着」，Host 反序列化载架时按 canonical 快照核对占用直接失败。
"""

from __future__ import annotations

from uuid import uuid4

from pylabrobot.resources import Coordinate

from unilabos.resources.presets.itemized_carrier import Bottle
from unilabos.resources.presets.warehouse import warehouse_factory
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.services.materials.graph import graph_resource_tree, reconcile_graph_payload


def _carrier_graph_nodes() -> tuple[list[dict], str]:
    """三个 Site 的仓库，A01 上有一个瓶子；按 ResourceTreeSet 导出成图节点。"""

    draft = warehouse_factory("wh", num_items_x=3, num_items_y=1, num_items_z=1)
    bottle = Bottle("wh_bottle", diameter=20, height=50, max_volume=100)
    bottle.location = Coordinate.zero()
    draft["A01"] = bottle
    nodes = [node for group in ResourceTreeSet.from_plr_resources([draft], known_random_uuid=True).dump() for node in group]
    return nodes, bottle.unilabos_uuid


def test_carrier_holders_do_not_occupy_their_own_sites() -> None:
    nodes, bottle_uuid = _carrier_graph_nodes()

    normalized, _ = reconcile_graph_payload({"nodes": nodes, "links": []}, None, str(uuid4()))

    (carrier,) = [node for node in normalized["nodes"] if node.get("sites")]
    assert {site["label"]: site["occupied_material_uuid"] for site in carrier["sites"]} == {
        "A01": bottle_uuid,
        "A02": None,
        "A03": None,
    }


def test_carrier_graph_with_holders_restores_on_the_host() -> None:
    nodes, bottle_uuid = _carrier_graph_nodes()
    normalized, _ = reconcile_graph_payload({"nodes": nodes, "links": []}, None, str(uuid4()))

    (restored,) = graph_resource_tree(list(normalized["nodes"])).to_plr_resources()

    assert restored["A01"].resource.unilabos_uuid == bottle_uuid
    assert restored["A02"].resource is None
    assert restored["A03"].resource is None


def _device_with_gripper_and_deck(*, occupied: str | None = None) -> tuple[list[dict], str, str]:
    """设备的夹爪 Site 是原点占位，台面载架挂在设备原点：坐标相同，台面并不在夹爪上。"""

    device_uuid, deck_uuid = str(uuid4()), str(uuid4())
    origin = {"x": 0.0, "y": 0.0, "z": 0.0}
    device = {
        "id": "liquid_handler", "uuid": device_uuid, "type": "device", "class": "test_liquid_handler",
        "template_name": "test_liquid_handler", "parent": None, "config": {}, "pose": {"position": origin},
        "sites": [
            {"label": "transfer_gripper", "index": "transfer_gripper", "pose": {"position": origin},
             "meta_data": {"role": "handler.transfer_gripper"}, "occupied_material_uuid": occupied},
            {"label": "waste_position", "index": "waste_position", "pose": {"position": {"x": 300.0, "y": 0.0, "z": 0.0}},
             "occupied_material_uuid": None},
        ],
    }
    deck = {
        "id": "workdeck", "uuid": deck_uuid, "type": "deck", "class": "test_deck", "template_name": "test_deck",
        "parent_uuid": device_uuid, "config": {"type": "Deck"}, "pose": {"position": origin},
    }
    waste = {
        "id": "waste_plate", "uuid": str(uuid4()), "type": "plate", "class": "test_plate", "template_name": "test_plate",
        "parent_uuid": device_uuid, "config": {"type": "Plate"}, "pose": {"position": {"x": 300.0, "y": 0.0, "z": 0.0}},
    }
    return [device, deck, waste], device_uuid, deck_uuid


def test_equal_coordinates_do_not_make_a_child_occupy_a_site() -> None:
    """占用只认显式声明与 PLR 结构：台面在设备原点不等于它在原点的夹爪 Site 上（以前搬运一直等
    ``gripper_site_occupied``），子物料坐标等于普通 Site 也不代表占用。"""

    nodes, device_uuid, _deck_uuid = _device_with_gripper_and_deck()

    normalized, _ = reconcile_graph_payload({"nodes": nodes, "links": []}, None, str(uuid4()))

    device = next(node for node in normalized["nodes"] if node["uuid"] == device_uuid)
    assert {site["label"]: site["occupied_material_uuid"] for site in device["sites"]} == {
        "transfer_gripper": None,
        "waste_position": None,
    }


def test_explicit_site_occupancy_is_kept() -> None:
    plate_uuid = str(uuid4())
    nodes, device_uuid, _deck_uuid = _device_with_gripper_and_deck(occupied=plate_uuid)
    nodes.append({
        "id": "held_plate", "uuid": plate_uuid, "type": "plate", "class": "test_plate", "template_name": "test_plate",
        "parent_uuid": device_uuid, "config": {"type": "Plate"}, "pose": {"position": {"x": 0.0, "y": 0.0, "z": 0.0}},
    })

    normalized, _ = reconcile_graph_payload({"nodes": nodes, "links": []}, None, str(uuid4()))

    device = next(node for node in normalized["nodes"] if node["uuid"] == device_uuid)
    assert device["sites"][0]["occupied_material_uuid"] == plate_uuid
    assert device["sites"][1]["occupied_material_uuid"] is None
