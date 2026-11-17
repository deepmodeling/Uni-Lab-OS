"""物料命名：``display_name`` 是人读标签，PLR 键单独保存，物料层 ``name`` 等于 uuid。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.plr_fixtures import plate
from unilabos.client.materials import LocalMaterialsClient
from unilabos.protocol.materials import InventoryMutation, MaterialPatch
from unilabos.resources import materials
from unilabos.resources.adapters.plr_materials import resource_tree_to_snapshot
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation)


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    materials_service = MaterialsService(tmp_path / "materials.db")
    yield materials_service
    materials_service.close()


def test_plr_labels_become_display_names() -> None:
    """根的标签是 PLR 名，孔位取 ordering 标识，而不是带板名前缀的 PLR 名。"""

    tree = ResourceTreeSet.from_plr_resources([plate("p1")], known_random_uuid=True)
    root = tree.root_nodes[0]
    assert root.res_content.display_name == "p1"
    first = root.children[0].res_content
    assert first.plr_name == "p1_well_A1"
    assert first.display_name == "A1"


def test_runtime_snapshot_keeps_the_authority_display_name(service) -> None:
    """设备快照不改展示名：用户在权威里改过的标签不被 PLR 序列化出的标签覆盖。"""

    root = materials.create(plate("p1"), gateway=LocalMaterialsClient(service))
    service.patch_material(
        _mutation("patch_material"), root.unilabos_uuid, MaterialPatch(display_name="我的板")
    )
    baseline = service.get_tree(root.unilabos_uuid)
    snapshot = resource_tree_to_snapshot(ResourceTreeSet.from_plr_resources([root]), baseline)
    by_uuid = {node.material.material_uuid: node.material for node in snapshot.nodes}
    assert by_uuid[root.unilabos_uuid].display_name == "我的板"
