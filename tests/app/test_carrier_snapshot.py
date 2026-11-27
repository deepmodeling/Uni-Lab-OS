"""已准入孔的容器快照从实际位置读取，不能另选物料或写账。"""

import pytest

from tests.app.test_material_source_descendants import (
    add_site,
    request,
)
from tests.app.test_material_source_descendants import (
    inventory as _inventory_fixture,
)
from unilabos.app.scheduler.inventory.carrier_snapshot import inspect_carrier_transfer
from unilabos.app.scheduler.inventory.domain import (
    CommandRejected,
    InsufficientStock,
    MaterialRequirement,
)

inventory = _inventory_fixture


def snapshot(authority, task="owner", material="current-vial"):
    return inspect_carrier_transfer(
        authority,
        task_uuid=task,
        material={"uuid": material},
        target_owner={"uuid": "root"},
        target_site="staging",
    )


def test_snapshot_requires_current_task_binding_and_never_writes(inventory):
    with pytest.raises(CommandRejected, match="准入"):
        snapshot(inventory)
    inventory.admit_task_materials("owner", [request("vial")], [])
    before = list(inventory.store._conn.iterdump())
    result = snapshot(inventory)
    assert result["material"] == {"uuid": "current-vial"}
    assert result["carrier_uuid"] == result["target_occupant_uuid"] == "current"
    assert result["item_site"] == "1"
    assert list(inventory.store._conn.iterdump()) == before
    with pytest.raises(CommandRejected, match="准入"):
        snapshot(inventory, task="stranger")
    inventory.release_workflow("owner")
    with pytest.raises(CommandRejected, match="准入"):
        snapshot(inventory)


@pytest.mark.parametrize("source_binding", [False, True])
def test_foreign_reservation_blocks_swap_then_fresh_read_allows_it(
    inventory, source_binding
):
    if source_binding:
        inventory.admit_task_materials("other", [request("vial")], [])
    else:
        inventory.reserve_workflow(
            "other", {"vial": [MaterialRequirement(instance_uuid="current-vial")]}
        )
    inventory.admit_task_materials("owner", [request("vial")], [])
    with pytest.raises(InsufficientStock, match="其他任务"):
        snapshot(inventory, material="spare-vial")
    inventory.release_workflow("other")
    result = snapshot(inventory, material="spare-vial")
    assert result["carrier_uuid"] == "spare"
    assert result["target_occupant_uuid"] == "current"
    assert result["foreign_content_claims"] == 0


def test_snapshot_uses_actual_site_and_rejects_detached_carrier(inventory):
    inventory.admit_task_materials("owner", [request("vial")], [])
    # 快照读取测试：直接建立本任务动作已结算的离架事实，不调用会绕过保管的公开移动。
    from unilabos.app.scheduler.inventory.store import clear_site_occupancy
    with inventory.store.transaction() as c:
        c.execute("DELETE FROM resource_relation WHERE child_uuid='current'")
        c.execute("UPDATE material SET parent_uuid=NULL WHERE uuid='current'")
        clear_site_occupancy(c, material_uuid="current")
    with pytest.raises(CommandRejected, match="离架或在途"):
        snapshot(inventory)


@pytest.mark.parametrize("source_binding", [False, True])
@pytest.mark.parametrize("in_place", [False, True])
def test_moving_source_carrier_preserves_other_tasks_contents(inventory, source_binding, in_place):
    inventory.move_instance("current", parent_uuid="", slot_id="")
    if in_place:
        # 在任务准入前固定另一份已到位现场；不在其他任务持料期间移动其载体。
        inventory.move_instance("spare", parent_uuid="root", slot_id="staging")
    add_site(inventory, "spare", "2", 1)
    inventory.register_instance(
        template_id="vial", edge_uuid="spare-vial-2", parent_uuid="spare", slot_id="2"
    )
    if source_binding:
        inventory.admit_task_materials("other", [request("vial")], [])
    else:
        inventory.reserve_workflow(
            "other", {"vial": [MaterialRequirement(instance_uuid="spare-vial")]}
        )
    inventory.admit_task_materials("owner", [request("vial")], [])
    if not in_place:
        with pytest.raises(InsufficientStock, match="来源容器"):
            snapshot(inventory, material="spare-vial-2")
    else:
        result = snapshot(inventory, material="spare-vial-2")
        assert result["carrier_uuid"] == result["target_occupant_uuid"] == "spare"
        assert result["foreign_content_claims"] == 1
