"""边缘调度器（EdgeScheduler）按仓库与库位范围自动分配物料合同。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.domain import (
    InsufficientStock,
    MaterialRequirement,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore

SITE_A = "10000000-0000-4000-8000-000000000001"
SITE_B = "10000000-0000-4000-8000-000000000002"
SITE_EMPTY = "10000000-0000-4000-8000-000000000003"
TARGET_SITE = "10000000-0000-4000-8000-000000000004"


@pytest.fixture()
def inventory(tmp_path: Path) -> tuple[InventoryStore, InventoryService, dict[str, str]]:
    """建立同一仓库下有序库位与两件兼容物料（Material）的库存事实。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    backend = BackendResourceService(store)
    templates = backend.sync_resource_templates(
        [
            {
                "id": "test.warehouse",
                "display_name": "测试仓库",
                "registry_type": "resource",
                "class": {},
            },
            {
                "id": "test.plate",
                "display_name": "测试孔板",
                "registry_type": "material",
                "class": {},
            },
        ]
    )["templates"]
    template_by_name = {item["name"]: item["uuid"] for item in templates}
    mount = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.warehouse"],
            "barcode": "WAREHOUSE-1",
            "name": "一号仓库",
        }
    )
    first = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.plate"],
            "parent_uuid": mount["uuid"],
            "barcode": "PLATE-A",
            "name": "孔板 A",
        }
    )
    second = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.plate"],
            "parent_uuid": mount["uuid"],
            "barcode": "PLATE-B",
            "name": "孔板 B",
        }
    )
    with store.transaction() as connection:
        for site_uuid, name, order, occupant in (
            (SITE_A, "A1", 10, first["uuid"]),
            (SITE_B, "B1", 5, second["uuid"]),
            (SITE_EMPTY, "C1", 0, None),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,'{}',?,?,?,?,?,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    "2026-08-06T00:00:00Z",
                    "2026-08-06T00:00:00Z",
                    mount["uuid"],
                    name,
                    order,
                    json.dumps([template_by_name["test.plate"]]),
                    occupant,
                ),
            )
    try:
        yield store, InventoryService(store), {
            "mount": mount["uuid"],
            "template": template_by_name["test.plate"],
            "first": first["uuid"],
            "second": second["uuid"],
        }
    finally:
        store.close()


def test_exact_site_selects_and_reserves_its_occupant_atomically(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """精确库位（Site）应返回并占用该位置的兼容物料（Material）。"""

    store, service, identities = inventory
    result = service.reserve_workflow(
        "workflow-exact",
        {
            "source": [
                MaterialRequirement(
                    template_id=identities["template"],
                    mount_uuid=identities["mount"],
                    site_uuid=SITE_A,
                )
            ]
        },
    )

    assert result["allocations"] == {"source": [identities["first"]]}
    assert store.get_instance(identities["first"])["status"] == "reserved"
    assert store.get_instance(identities["second"])["status"] == "warehouse"


def test_slot_range_uses_site_order_and_whole_set_failure_rolls_back(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """库位范围按位置顺序选取；同批另一来源不足时整组零占用。"""

    store, service, identities = inventory
    with pytest.raises(InsufficientStock):
        service.reserve_workflow(
            "workflow-range",
            {
                "available": [
                    MaterialRequirement(
                        template_id=identities["template"],
                        mount_uuid=identities["mount"],
                        slot_uuids=[SITE_A, SITE_B],
                    )
                ],
                "empty": [
                    MaterialRequirement(
                        template_id=identities["template"],
                        mount_uuid=identities["mount"],
                        site_uuid=SITE_EMPTY,
                    )
                ],
            },
        )

    assert store.get_instance(identities["first"])["status"] == "warehouse"
    assert store.get_instance(identities["second"])["status"] == "warehouse"
    assert store.reservations_for_workflow("workflow-range") == []


def test_slot_range_selects_lowest_site_order_deterministically(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """库位范围不依赖调用方数组顺序，始终选择 ``sort_order`` 最小的位置。"""

    store, service, identities = inventory
    result = service.reserve_workflow(
        "workflow-range-success",
        {
            "source": [
                MaterialRequirement(
                    template_id=identities["template"],
                    mount_uuid=identities["mount"],
                    slot_uuids=[SITE_A, SITE_B],
                )
            ]
        },
    )

    assert result["allocations"] == {"source": [identities["second"]]}
    replay = service.reserve_workflow(
        "workflow-range-success",
        {
            "source": [
                MaterialRequirement(
                    template_id=identities["template"],
                    mount_uuid=identities["mount"],
                    slot_uuids=[SITE_A, SITE_B],
                )
            ]
        },
    )
    assert replay["allocations"] == result["allocations"]
    assert store.get_instance(identities["second"])["status"] == "reserved"


def test_move_instance_commits_parent_and_site_occupancy_atomically(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """系统转运必须同时清空来源库位并占用目标库位（Site）。

    参数：``inventory`` 提供共享资源、库存实例及来源库位事实。返回：无；断言
    ``move_instance`` 在一个事务中更新物料父级、来源/目标库位占用及可同步账本，
    从而为主机转运动作提供正式库存提交入口。异常：目标身份或库存结构非法时由
    库存服务失败关闭。
    """

    store, service, identities = inventory
    backend = BackendResourceService(store)
    target = backend.create_material(
        {
            "resource_template_uuid": backend.get_material(identities["mount"])[
                "resource_template_uuid"
            ],
            "barcode": "WAREHOUSE-2",
            "name": "二号仓库",
        }
    )
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO site(
                uuid,create_time,update_time,meta_data,material_uuid,name,
                sort_order,allowed_resource_template_uuids,
                occupied_material_uuid,position_x,position_y,position_z,
                depth,length,width
            ) VALUES (?,?,?,'{}',?,?,?,?,NULL,0,0,0,0,0,0)
            """,
            (
                TARGET_SITE,
                "2026-08-06T00:00:00Z",
                "2026-08-06T00:00:00Z",
                target["uuid"],
                "A1",
                0,
                json.dumps([identities["template"]]),
            ),
        )

    moved = service.move_instance(
        identities["first"],
        parent_uuid=target["uuid"],
        slot_id="A1",
        actor="host_node.transfer_resource",
    )

    assert moved["parent_uuid"] == target["uuid"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (SITE_A,)
    )["occupied_material_uuid"] is None
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (TARGET_SITE,)
    )["occupied_material_uuid"] == identities["first"]
    ledger = store.query_one(
        "SELECT op_type,delta_json,actor FROM inventory_ledger "
        "WHERE aggregate_id=? ORDER BY ledger_id DESC LIMIT 1",
        (identities["first"],),
    )
    assert ledger["op_type"] == "instance.moved"
    assert ledger["actor"] == "host_node.transfer_resource"
    assert json.loads(ledger["delta_json"])["to_slot"] == "A1"
