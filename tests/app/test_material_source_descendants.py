"""来源在既有任务准入中跨容器选料，保持整组回滚与稳定绑定。"""

from uuid import uuid4

import pytest

from unilabos.app.scheduler.inventory.domain import (
    InsufficientStock,
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore


def add_site(authority, parent, name, order):
    ident = str(uuid4())
    with authority.store.transaction() as conn:
        conn.execute(
            "INSERT INTO site(uuid,create_time,update_time,material_uuid,name,sort_order) VALUES (?, '2026-09-13', '2026-09-13', ?, ?, ?)",
            (ident, parent, name, order),
        )
    return ident


@pytest.fixture
def inventory():
    authority = InventoryService(InventoryStore(":memory:"))
    authority.register_instance(template_id="container", edge_uuid="root")
    add_site(authority, "root", "staging", 0)
    add_site(authority, "root", "rack", 1)
    for tray, slot in [("current", "staging"), ("spare", "rack")]:
        authority.register_instance(
            template_id="tray", edge_uuid=tray, parent_uuid="root", slot_id=slot
        )
        add_site(authority, tray, "1", 0)
        authority.register_instance(
            template_id="vial", edge_uuid=tray + "-vial", parent_uuid=tray, slot_id="1"
        )
    yield authority
    authority.store.close()


def request(node, template="vial", recursive=True, sites=()):
    return MaterialSourceAdmissionRequest(
        node_id=node,
        resource_template_uuid=template,
        custody_policy="task_exclusive",
        requirement=MaterialRequirement(
            template_id=template,
            mount_uuid="root",
            search_descendants=recursive,
            slot_uuids=list(sites),
        ),
    )


def test_prioritizes_current_container_and_reuses_task_binding(inventory):
    first = inventory.admit_task_materials("sample-1", [request("vial")], [])
    assert first["allocations"]["vial"] == ["current-vial"]
    assert (
        inventory.admit_task_materials("sample-1", [request("vial")], [])["allocations"]
        == first["allocations"]
    )
    second = inventory.admit_task_materials("sample-2", [request("vial")], [])
    assert second["allocations"]["vial"] == ["spare-vial"]
    with pytest.raises(InsufficientStock):
        inventory.admit_task_materials("sample-3", [request("vial")], [])
    assert (
        len(
            inventory.store.query_all("SELECT * FROM inventory_material_source_binding")
        )
        == 2
    )


def test_pair_failure_rolls_back_entire_existing_admission(inventory):
    with pytest.raises(InsufficientStock):
        inventory.admit_task_materials(
            "pair", [request("a-vial"), request("z-missing", "collector")], []
        )
    assert (
        inventory.store.query_all("SELECT * FROM inventory_material_source_binding")
        == []
    )
    assert inventory.store.query_all("SELECT * FROM inventory_reservation") == []
    assert (
        inventory.store.query_one(
            "SELECT status FROM material_instance WHERE edge_uuid='current-vial'"
        )["status"]
        == "warehouse"
    )


def test_direct_selection_stays_direct_and_root_site_limits_subtree(inventory):
    with pytest.raises(InsufficientStock):
        inventory.admit_task_materials("direct", [request("vial", recursive=False)], [])
    rack = inventory.store.query_one(
        "SELECT uuid FROM site WHERE material_uuid='root' AND name='rack'"
    )["uuid"]
    assert inventory.admit_task_materials(
        "rack-only", [request("vial", sites=[rack])], []
    )["allocations"]["vial"] == ["spare-vial"]


def test_detached_tray_cannot_supply_material(inventory):
    inventory.move_instance("current", parent_uuid="", slot_id="")
    assert inventory.admit_task_materials("task", [request("vial")], [])["allocations"][
        "vial"
    ] == ["spare-vial"]


def test_material_claimed_by_other_task_is_not_reported_as_shortage(inventory):
    """匹配物料只是被其他任务占用时报“被占用”，释放后可选；真缺料仍是普通不足。"""

    from unilabos.app.scheduler.inventory.domain import MaterialClaimedByOtherTask

    inventory.admit_task_materials("holder-1", [request("vial")], [])
    inventory.admit_task_materials("holder-2", [request("vial")], [])
    with pytest.raises(MaterialClaimedByOtherTask):
        inventory.admit_task_materials("waiter", [request("vial")], [])
    direct = MaterialSourceAdmissionRequest(
        node_id="direct",
        resource_template_uuid="vial",
        custody_policy="task_exclusive",
        requirement=MaterialRequirement(template_id="vial", mount_uuid="current"),
    )
    with pytest.raises(MaterialClaimedByOtherTask):
        inventory.admit_task_materials("waiter", [direct], [])
    with pytest.raises(InsufficientStock) as missing:
        inventory.admit_task_materials("waiter", [request("vial", template="collector")], [])
    assert not isinstance(missing.value, MaterialClaimedByOtherTask)
    inventory.release_workflow("holder-1", reason="done")
    assert inventory.admit_task_materials("waiter", [request("vial")], [])["allocations"]["vial"] == [
        "current-vial"
    ]


def test_mount_without_sites_is_a_parameter_error_not_shortage(inventory):
    """挂载位下没有任何库位是参数错误，不能当作缺料触发补料（技术方案 4.4）。"""

    from unilabos.app.scheduler.inventory.domain import CommandRejected

    with pytest.raises(CommandRejected, match="has no sites"):
        inventory.admit_task_materials(
            "task",
            [
                MaterialSourceAdmissionRequest(
                    node_id="vial",
                    resource_template_uuid="vial",
                    custody_policy="task_exclusive",
                    requirement=MaterialRequirement(template_id="vial", mount_uuid="current-vial"),
                )
            ],
            [],
        )
