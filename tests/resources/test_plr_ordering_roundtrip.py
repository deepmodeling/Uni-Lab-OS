"""PLR ItemizedResource ``ordering`` 键序在权威 round-trip 中必须无损。

PLR ``get_item("A2")`` 的语义是「ordering 键序中的位置 -> children 同位置」，
键序（列优先 A1, B1, C1, A2, ...）一旦被字典序重排（A1, A2, A3, ...），
重建实例的孔位标识会整体错位（get_well("A2") 拿到 B1 的孔）。A1 在两种
排序下都是首位，因此测试必须覆盖非首位孔。

两层防线分别验证：

- 存储层 ``stored_json`` 保序（materials.db round-trip 键序不变）；
- 适配层 ``resource_ulab_to_plr`` 在 ordering 被重排时按 children 顺序还原。
"""

from __future__ import annotations

import collections
from uuid import uuid4

from pylabrobot.resources import Plate, Well
from pylabrobot.resources.utils import create_ordered_items_2d

from unilabos.client.materials import LocalMaterialsClient
from unilabos.resources import materials
from unilabos.server.backend.composition import set_materials_gateway
from unilabos.server.services.materials import MaterialsService


def _plate_4x3(name: str) -> Plate:
    return Plate(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=44.0,
        lid=None,
        model="ordering_probe_plate",
        ordered_items=create_ordered_items_2d(
            Well,
            name_prefix=name,
            num_items_x=4,
            num_items_y=3,
            dx=12.0,
            dy=8.0,
            dz=4.0,
            item_dx=27.0,
            item_dy=27.0,
            size_x=20.0,
            size_y=20.0,
            size_z=38.0,
            max_volume=2200.0,
        ),
    )


#: 4x3 板的列优先键序（PLR transposed MS-Excel 序）。
COLUMN_MAJOR = [
    "A1", "B1", "C1", "A2", "B2", "C2", "A3", "B3", "C3", "A4", "B4", "C4",
]


def test_authority_roundtrip_preserves_well_identity(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    set_materials_gateway(gateway)
    try:
        draft = _plate_4x3("probe_plate")
        materials.set_substance_on_target(draft.get_well("A2"), "Water", 40.0)
        created = materials.create(draft, gateway=gateway)

        # 键序保持列优先，且非首位孔位（A2 / B1）身份不串
        assert list(created._ordering) == COLUMN_MAJOR
        assert created.get_well("A2").name == "probe_plate_well_A2"
        assert created.get_well("B1").name == "probe_plate_well_B1"

        # 权威落库读回同样保序（存储层 stored_json 不做键排序）
        stored = service.get_material(created.unilabos_uuid)
        assert list(stored.material.config["ordering"]) == COLUMN_MAJOR

        # 草稿阶段写入 A2 的液体，round-trip 后仍在 A2 名下
        tree = service.get_tree(created.unilabos_uuid)
        a2_node = next(
            node
            for node in tree.nodes
            if node.material.material_uuid
            == created.get_well("A2").unilabos_uuid
        )
        assert a2_node.material.name == "probe_plate_well_A2"
        assert [
            (entry.name, entry.quantity) for entry in a2_node.data.substances
        ] == [("Water", 40.0)]
    finally:
        set_materials_gateway(None)
        service.close()


def test_rebuild_repairs_sorted_ordering_from_children() -> None:
    """适配层按 children 顺序修复被字典序重排的 ordering。"""

    from unilabos.resources.resource_tracker import ResourceTreeSet

    draft = _plate_4x3("reordered_plate")
    draft.unilabos_uuid = str(uuid4())
    for child in draft.get_all_children():
        child.unilabos_uuid = str(uuid4())
    a2_uuid = draft.get_well("A2").unilabos_uuid

    tree_set = ResourceTreeSet.from_plr_resources([draft])
    root = tree_set.trees[0].root_node.res_content
    # 构造键按字典序排列、children 仍为列优先的输入。
    root.config["ordering"] = collections.OrderedDict(
        sorted(root.config["ordering"].items())
    )
    assert list(root.config["ordering"]) != COLUMN_MAJOR

    rebuilt = tree_set.to_plr_resources()[0]
    assert list(rebuilt._ordering) == COLUMN_MAJOR
    assert rebuilt.get_well("A2").name == "reordered_plate_well_A2"
    assert rebuilt.get_well("A2").unilabos_uuid == a2_uuid
