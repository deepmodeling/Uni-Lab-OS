"""盘点只自动校正未分配库存，预留和实例身份必须受保护。"""
from uuid import uuid4

import pytest

from unilabos.app.scheduler.inventory.commands import execute_command
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore


@pytest.fixture
def inventory():
    svc = InventoryService(InventoryStore(":memory:"))
    svc.register_instance(template_id="container", edge_uuid="magazine")
    svc.inbound_lot("plate", 10, unit="piece", lot_id="plates", warehouse_zone_id="magazine")
    yield svc
    svc.store.close()


def measure(svc, count, command_id=None, version=None):
    return execute_command(svc, {
        "command_id": command_id or str(uuid4()), "type": "inventory.reconcile_count",
        "actor": "measurement-runtime",
        "expected_version": version or svc.store.get_lot("plates")["version"],
        "payload": {"lot_id": "plates", "mount_uuid": "magazine", "measured_count": count,
                    "evidence_job_uuid": "measurement-job"},
    })


def test_unassigned_difference_is_corrected_and_replay_is_once(inventory):
    first = measure(inventory, 8, command_id="count-1")
    assert first["status"] == "completed"
    assert first["result"]["changed"] is True
    assert inventory.store.get_lot("plates")["quantity_total"] == 8
    second = measure(inventory, 8, command_id="count-1")
    assert second["replayed"] is True
    assert inventory.store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger WHERE op_type='lot.measurement_reconciled'")["n"] == 1


@pytest.mark.parametrize("count", [8, 12])
def test_reserved_difference_requires_review_even_when_adding_stock(inventory, count):
    inventory.reserve_workflow("task-1", {"node": [MaterialRequirement(lot_id="plates", quantity=1)]})
    before = inventory.store.get_lot("plates")
    rejected = measure(inventory, count)
    assert rejected["error_code"] == "stocktake_review_required"
    assert inventory.store.get_lot("plates") == before
    assert measure(inventory, 10)["status"] == "completed"


@pytest.mark.parametrize("origin_lot", ["plates", "another-source-batch"])
def test_registered_identity_in_measured_container_is_not_deleted(inventory, origin_lot):
    inventory.register_instance(template_id="plate", lot_id=origin_lot, edge_uuid="sample-plate", parent_uuid="magazine")
    rejected = measure(inventory, 9)
    assert rejected["error_code"] == "stocktake_review_required"
    assert inventory.store.get_lot("plates")["quantity_total"] == 10
    assert inventory.store.query_one("SELECT parent_uuid FROM material_instance WHERE edge_uuid='sample-plate'")["parent_uuid"] == "magazine"


def test_stale_version_and_invalid_count_cannot_modify_inventory(inventory):
    assert measure(inventory, 9)["status"] == "completed"
    assert measure(inventory, 7, version=1)["error_code"] == "version_conflict"
    for value in (True, -1, 1.2):
        assert measure(inventory, value)["status"] == "rejected"
    assert inventory.store.get_lot("plates")["quantity_total"] == 9
