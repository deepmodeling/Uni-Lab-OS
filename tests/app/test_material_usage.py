"""工作流对已准入物料的位置读取与“已使用”标记：保留库位，只退出自动选料。"""

import pytest

from tests.app.test_material_source_descendants import inventory as _inventory_fixture
from tests.app.test_material_source_descendants import request
from unilabos.app.scheduler.inventory.domain import (
    CommandRejected,
    InstanceState,
    check_instance_transition,
)
from unilabos.app.scheduler.inventory.material_usage import locate_material, mark_material_used, site_occupant

inventory = _inventory_fixture


def status(authority, uuid):
    return authority.store.query_one("SELECT status FROM material_instance WHERE edge_uuid=?", (uuid,))["status"]


def site_of(authority, uuid):
    return authority.store.query_one("SELECT name FROM site WHERE occupied_material_uuid=?", (uuid,))


def test_marked_material_keeps_its_site_and_is_skipped_by_later_selection(inventory):
    assert inventory.admit_task_materials("sample-1", [request("vial")], [])["allocations"]["vial"] == ["current-vial"]
    result = mark_material_used(inventory, task_uuid="sample-1", job_uuid="job-1", material={"uuid": "current-vial"}, reason="加样后")
    assert result == {"material": {"uuid": "current-vial"}, "previous_status": "reserved", "status": "used"}
    assert status(inventory, "current-vial") == "used"
    assert site_of(inventory, "current-vial")["name"] == "1"
    inventory.release_workflow("sample-1")
    # 任务结束只释放保管，不把已使用物料放回可选。
    assert status(inventory, "current-vial") == "used"
    assert inventory.admit_task_materials("sample-2", [request("vial")], [])["allocations"]["vial"] == ["spare-vial"]


def test_replaying_the_same_job_does_not_write_twice(inventory):
    inventory.admit_task_materials("sample-1", [request("vial")], [])
    first = mark_material_used(inventory, task_uuid="sample-1", job_uuid="job-1", material={"uuid": "current-vial"})
    again = mark_material_used(inventory, task_uuid="sample-1", job_uuid="job-1", material={"uuid": "current-vial"})
    assert again == first
    events = inventory.store.query_all("SELECT * FROM inventory_ledger WHERE op_type='instance.used'")
    assert len(events) == 1


def test_only_the_admitting_task_may_mark_its_exclusive_material(inventory):
    with pytest.raises(CommandRejected, match="准入"):
        mark_material_used(inventory, task_uuid="sample-1", job_uuid="job-1", material={"uuid": "current-vial"})
    inventory.admit_task_materials("owner", [request("vial")], [])
    with pytest.raises(CommandRejected, match="准入"):
        mark_material_used(inventory, task_uuid="stranger", job_uuid="job-2", material={"uuid": "current-vial"})
    with pytest.raises(CommandRejected, match="具体物料"):
        mark_material_used(inventory, task_uuid="owner", job_uuid="job-3", material="current-vial")
    assert status(inventory, "current-vial") == "reserved"


def test_used_material_can_be_released_back_by_review_or_retired():
    check_instance_transition(InstanceState.USED, InstanceState.WAREHOUSE)
    check_instance_transition(InstanceState.USED, InstanceState.CONSUMED)
    check_instance_transition(InstanceState.IN_USE, InstanceState.USED)


def test_locate_reads_the_current_site_and_owner_without_writing(inventory):
    with pytest.raises(CommandRejected, match="准入"):
        locate_material(inventory, task_uuid="owner", material={"uuid": "current-vial"})
    inventory.admit_task_materials("owner", [request("vial")], [])
    before = list(inventory.store._conn.iterdump())
    result = locate_material(inventory, task_uuid="owner", material={"uuid": "current-vial"})
    assert result["material"] == {"uuid": "current-vial"}
    assert result["owner"]["uuid"] == result["owner_uuid"] == "current"
    assert result["site_name"] == "1" and result["site_uuid"]
    assert list(inventory.store._conn.iterdump()) == before
    from unilabos.app.scheduler.inventory.store import clear_site_occupancy
    # 定位只读测试直接建立原 Task 动作已结算的在途现场，不绕过独占调用公开移动。
    with inventory.store.transaction() as c:
        c.execute("DELETE FROM resource_relation WHERE child_uuid='current-vial'")
        c.execute("UPDATE material SET parent_uuid=NULL WHERE uuid='current-vial'")
        clear_site_occupancy(c, material_uuid="current-vial")
    with pytest.raises(CommandRejected, match="不在任何已登记库位"):
        locate_material(inventory, task_uuid="owner", material={"uuid": "current-vial"})


def test_locate_can_read_the_container_of_an_admitted_material(inventory):
    inventory.admit_task_materials("owner", [request("vial")], [])
    tray = locate_material(inventory, task_uuid="owner", material={"uuid": "current"})
    assert tray["owner_uuid"] == "root" and tray["site_name"] == "staging"
    with pytest.raises(CommandRejected, match="容器"):
        locate_material(inventory, task_uuid="owner", material={"uuid": "spare"})


def test_site_occupant_reads_current_occupancy_without_writing(inventory):
    staged = site_occupant(inventory, task_uuid="any", owner={"uuid": "root"}, site="staging")
    assert staged["occupied"] is True and staged["occupant"]["uuid"] == "current"
    inventory.move_instance("current", parent_uuid="", slot_id="")
    empty = site_occupant(inventory, task_uuid="any", owner={"uuid": "root"}, site="staging")
    assert empty["occupied"] is False and empty["occupant_uuid"] == "" and empty["occupant_name"] == ""
    with pytest.raises(CommandRejected, match="不存在库位"):
        site_occupant(inventory, task_uuid="any", owner={"uuid": "root"}, site="nowhere")


def test_locate_reports_the_located_material_name(inventory):
    inventory.admit_task_materials("owner", [request("vial")], [])
    tray = locate_material(inventory, task_uuid="owner", material={"uuid": "current"})
    assert tray["material_name"]
