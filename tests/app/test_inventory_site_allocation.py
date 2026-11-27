"""边缘调度器（EdgeScheduler）按仓库与库位范围自动分配物料合同。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.domain import (
    CommandRejected,
    InsufficientStock,
    MaterialClaimedByOtherTask,
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.site_selection import (
    InventorySiteSelectionError,
    build_inventory_site_selection_resolver,
)
from unilabos.app.scheduler.inventory.store import (
    InventoryStore,
    SiteOccupancyConflict,
    set_site_occupancy,
)
from unilabos.app.scheduler.inventory.workflow_quantity import (
    WorkflowQuantityReservationError,
)
from unilabos.app.scheduler.site_target import resolve_site_target
from unilabos.app.scheduler.transfer_resource_set import (
    resolve_transfer_resource_set,
)

SITE_A = "10000000-0000-4000-8000-000000000001"
SITE_B = "10000000-0000-4000-8000-000000000002"
SITE_EMPTY = "10000000-0000-4000-8000-000000000003"
TARGET_SITE = "10000000-0000-4000-8000-000000000004"
GRIPPER_SITE = "10000000-0000-4000-8000-000000000005"


@pytest.fixture()
def inventory(
    tmp_path: Path,
) -> tuple[InventoryStore, InventoryService, dict[str, str]]:
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
                ) VALUES (?,?,?,?,?,?,?,?,?,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    "2026-08-06T00:00:00Z",
                    "2026-08-06T00:00:00Z",
                    json.dumps(
                        {
                            "unilab": {
                                "site_groups": (
                                    ["process_input"]
                                    if site_uuid in {SITE_A, SITE_B}
                                    else []
                                )
                            }
                        }
                    ),
                    mount["uuid"],
                    name,
                    order,
                    json.dumps([template_by_name["test.plate"]]),
                    occupant,
                ),
            )
    try:
        yield (
            store,
            InventoryService(store),
            {
                "mount": mount["uuid"],
                "template": template_by_name["test.plate"],
                "first": first["uuid"],
                "second": second["uuid"],
            },
        )
    finally:
        store.close()


def test_named_site_group_and_qualified_site_reference_are_frozen_by_owner(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """任务准入只允许同一父资源内的命名组与 ``父资源.库位`` 引用。"""

    store, _service, identities = inventory
    resolve = build_inventory_site_selection_resolver(store)

    group = resolve(
        {
            "version": 1,
            "owner_material_uuid": identities["mount"],
            "group_key": "process_input",
            "exact_site_reference": "",
        }
    )
    exact = resolve(
        {
            "version": 1,
            "owner_material_uuid": identities["mount"],
            "group_key": "",
            "exact_site_reference": f"{identities['mount']}.A1",
        }
    )

    assert group["site_uuids"] == [SITE_B, SITE_A]
    assert str(group["fingerprint"]).startswith("sha256:")
    assert exact["site_uuids"] == [SITE_A]

    exact_in_group = resolve(
        {
            "version": 1,
            "owner_material_uuid": identities["mount"],
            "group_key": "process_input",
            "exact_site_reference": f"{identities['mount']}.A1",
        }
    )
    assert exact_in_group["site_uuids"] == [SITE_A]

    with pytest.raises(InventorySiteSelectionError, match="不属于命名库位组"):
        resolve(
            {
                "version": 1,
                "owner_material_uuid": identities["mount"],
                "group_key": "process_input",
                "exact_site_reference": f"{identities['mount']}.C1",
            }
        )

    with pytest.raises(
        InventorySiteSelectionError,
        match="不属于声明的父资源",
    ):
        resolve(
            {
                "version": 1,
                "owner_material_uuid": identities["mount"],
                "group_key": "",
                "exact_site_reference": f"{identities['first']}.A1",
            }
        )


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


def test_target_site_group_skips_claimed_first_member_by_sort_order(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """等价库位组应跳过已申领首项并选择排序后的下一个可用位置。

    参数：``inventory`` 提供同一父物料下两个兼容空库位及待放入物料。返回：无。
    断言调用方数组顺序不影响选择，且门禁 6 收到当前活动作业执行占用（Claim）
    的库位 UUID 后会原子回退到下一备选。异常：库存写入错误原样传播。
    """

    store, service, identities = inventory
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
                "2026-08-06T00:00:01Z",
                "2026-08-06T00:00:01Z",
                identities["mount"],
                "D1",
                1,
                json.dumps([identities["template"]]),
            ),
        )

    first = resolve_site_target(
        service.station_resources,
        owner_material_uuid=identities["mount"],
        site_uuids=(TARGET_SITE, SITE_EMPTY),
        occupant_material_uuid=identities["first"],
    )
    fallback = resolve_site_target(
        service.station_resources,
        owner_material_uuid=identities["mount"],
        site_uuids=(TARGET_SITE, SITE_EMPTY),
        occupant_material_uuid=identities["first"],
        unavailable_site_uuids=(SITE_EMPTY,),
    )

    assert first.uuid == SITE_EMPTY
    assert fallback.uuid == TARGET_SITE


def test_target_site_identity_can_be_resolved_before_gate7_availability_check(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """候选组构造应保留已占用成员，把可用性统一留给 Gate 7 事务。"""

    _store, service, identities = inventory

    target = resolve_site_target(
        service.station_resources,
        owner_material_uuid=identities["mount"],
        site_uuid=SITE_A,
        occupant_material_uuid=identities["second"],
        require_available=False,
    )

    assert target.uuid == SITE_A
    assert target.name == "A1"


def test_transfer_resource_set_contains_source_target_and_owner_device(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """机械臂转运应从库位事实解析来源并占用两端库位和所属设备。

    参数：``inventory`` 提供来源物料、来源库位与空目标库位。返回：无。断言完整
    资源集合包含物料当前来源库位、选定目标库位以及拥有两者的设备物料身份；
    工作流不得自行猜测来源位置。异常：库存事实缺失时生产解析器错误原样传播。
    """

    store, service, identities = inventory
    with store.transaction() as connection:
        # ``mount`` 在该用例中代表带库位的实际设备，而非普通仓库。
        connection.execute(
            "UPDATE material SET type = 'device' WHERE uuid = ?",
            (identities["mount"],),
        )
    target = resolve_site_target(
        service.station_resources,
        owner_material_uuid=identities["mount"],
        site_uuid=SITE_EMPTY,
        occupant_material_uuid=identities["first"],
    )

    resources = resolve_transfer_resource_set(
        service.station_resources,
        resource_material_uuid=identities["first"],
        target=target,
    )

    assert resources.source_site_uuid == SITE_A
    assert set(resources.lock_keys) == {
        f"/devices/{identities['mount']}",
        f"material/{identities['first']}/exclusive",
        f"material/{identities['mount']}/site/{SITE_A}/exclusive",
        f"material/{identities['mount']}/site/{SITE_EMPTY}/exclusive",
    }


def test_transfer_resource_set_requires_empty_gripper_and_both_devices(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """AST 转运合同必须一次取得物料、两端设备/位置、机械臂和空夹爪位置。

    参数：``inventory`` 提供真实库存库位与待搬物料。返回：无；断言来源设备、
    目标设备、机械臂执行器、来源/目标/夹爪库位和主物料全部进入一个资源集合。
    异常：任何设备或夹爪位置无法证明时生产解析器失败，测试不得降级为部分集合。
    """

    store, service, identities = inventory
    backend = BackendResourceService(store)
    owner_template_uuid = backend.get_material(identities["mount"])[
        "resource_template_uuid"
    ]
    target_device = backend.create_material(
        {
            "resource_template_uuid": owner_template_uuid,
            "barcode": "TARGET-DEVICE",
            "name": "目标设备",
        }
    )
    robot = backend.create_material(
        {
            "resource_template_uuid": owner_template_uuid,
            "barcode": "ROBOT-DEVICE",
            "name": "机械臂",
        }
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE material SET type='device' WHERE uuid IN (?,?,?)",
            (identities["mount"], target_device["uuid"], robot["uuid"]),
        )
        for site_uuid, owner_uuid, name, metadata in (
            (TARGET_SITE, target_device["uuid"], "IN", {}),
            (
                GRIPPER_SITE,
                robot["uuid"],
                "GRIPPER",
                {"unilab": {"resource_role": "robot.gripper"}},
            ),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,?,?,?,0,?,NULL,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    "2026-08-06T00:00:02Z",
                    "2026-08-06T00:00:02Z",
                    json.dumps(metadata),
                    owner_uuid,
                    name,
                    json.dumps([identities["template"]]),
                ),
            )
    target = resolve_site_target(
        service.station_resources,
        owner_material_uuid=target_device["uuid"],
        site_uuid=TARGET_SITE,
        occupant_material_uuid=identities["first"],
    )

    resources = resolve_transfer_resource_set(
        service.station_resources,
        resource_material_uuid=identities["first"],
        target=target,
        executor_material_uuid=robot["uuid"],
        gripper_site_role="robot.gripper",
        require_device_owners=True,
    )

    assert resources.source_device_material_uuid == identities["mount"]
    assert resources.target_device_material_uuid == target_device["uuid"]
    assert resources.gripper_site_uuid == GRIPPER_SITE
    assert set(resources.lock_keys) == {
        f"/devices/{identities['mount']}",
        f"/devices/{target_device['uuid']}",
        f"/devices/{robot['uuid']}",
        f"material/{identities['first']}/exclusive",
        f"material/{identities['mount']}/site/{SITE_A}/exclusive",
        f"material/{target_device['uuid']}/site/{TARGET_SITE}/exclusive",
        f"material/{robot['uuid']}/site/{GRIPPER_SITE}/exclusive",
    }


def test_shared_source_admits_two_tasks_without_task_reservation(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """两个任务应能冻结绑定同一共享试剂而不互相阻塞。

    参数：``inventory`` 提供同一库位（Site）中的候选物料与
    库存权威（Inventory Authority）。返回：无；通过
    ``admit_material_sources`` 公共接缝断言两个工作流任务
    （WorkflowTask）取得同一稳定绑定，且都不创建任务物料预留
    （TaskMaterialReservation）。
    """

    store, service, identities = inventory
    # ``source_request`` 是两个任务共用的固定位置试剂准入意图。
    source_request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=identities["template"],
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
            site_uuid=SITE_A,
        ),
    )

    first = service.admit_material_sources("workflow-shared-a", [source_request])
    second = service.admit_material_sources("workflow-shared-b", [source_request])

    expected_allocations = {"reagent-source": [identities["first"]]}
    assert first["allocations"] == expected_allocations
    assert second["allocations"] == expected_allocations
    assert first["reserved_nodes"] == []
    assert second["reserved_nodes"] == []
    assert store.get_instance(identities["first"])["status"] == "warehouse"
    bindings = store.query_all(
        "SELECT workflow_id,material_uuid,custody_policy,status "
        "FROM inventory_material_source_binding ORDER BY workflow_id"
    )
    assert bindings == [
        {
            "workflow_id": "workflow-shared-a",
            "material_uuid": identities["first"],
            "custody_policy": "shared_source",
            "status": "active",
        },
        {
            "workflow_id": "workflow-shared-b",
            "material_uuid": identities["first"],
            "custody_policy": "shared_source",
            "status": "active",
        },
    ]


def test_task_exclusive_source_skips_active_shared_material(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """任务独占来源不得抢占已被活跃共享来源绑定的物料。"""

    _store, service, identities = inventory
    shared = MaterialSourceAdmissionRequest(
        node_id="z-shared-pump",
        resource_template_uuid=identities["template"],
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
            site_uuid=SITE_A,
        ),
    )
    exclusive = MaterialSourceAdmissionRequest(
        node_id="a-moving-reagent",
        resource_template_uuid=identities["template"],
        custody_policy="task_exclusive",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
        ),
    )

    admitted = service.admit_material_sources(
        "workflow-mixed",
        [exclusive, shared],
    )

    assert admitted["allocations"] == {
        "a-moving-reagent": [identities["second"]],
        "z-shared-pump": [identities["first"]],
    }
    fixed_shared_material = MaterialSourceAdmissionRequest(
        node_id="fixed-moving-reagent",
        resource_template_uuid=identities["template"],
        custody_policy="task_exclusive",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            instance_uuid=identities["first"],
        ),
    )
    with pytest.raises(InsufficientStock, match="active shared source"):
        service.admit_material_sources(
            "workflow-fixed-exclusive",
            [fixed_shared_material],
        )


def test_fixed_exclusive_source_precedes_unpinned_shared_source(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """无库位共享来源不得抢占同任务中固定给独占来源的物料。"""

    _store, service, identities = inventory
    fixed_exclusive = MaterialSourceAdmissionRequest(
        node_id="moving-reagent",
        resource_template_uuid=identities["template"],
        custody_policy="task_exclusive",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
            # SITE_B 的 sort_order 更小；旧逻辑会先让无库位共享来源选走它。
            site_uuid=SITE_B,
        ),
    )
    unpinned_shared = MaterialSourceAdmissionRequest(
        node_id="solvent-pump",
        resource_template_uuid=identities["template"],
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
        ),
    )

    admitted = service.admit_material_sources(
        "workflow-fixed-exclusive-with-shared",
        [unpinned_shared, fixed_exclusive],
    )

    assert admitted["allocations"] == {
        "moving-reagent": [identities["second"]],
        "solvent-pump": [identities["first"]],
    }


def test_task_exclusive_source_blocks_second_task_until_release(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """任务全程独占来源必须继续阻止第二个任务占用同一试剂。

    参数：``inventory`` 提供固定库位中的单件试剂。返回：无；断言第一个任务
    创建库存预留并把实例置为 ``reserved``，第二个任务的同一来源准入失败。
    异常：库存不足必须以 ``InsufficientStock`` 失败关闭。
    """

    store, service, identities = inventory
    # ``exclusive_request`` 明确选择任务全程持有，而不是共享来源动作锁。
    exclusive_request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=identities["template"],
        custody_policy="task_exclusive",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
            site_uuid=SITE_A,
        ),
    )

    first = service.admit_material_sources("workflow-exclusive-a", [exclusive_request])
    replay = service.admit_material_sources("workflow-exclusive-a", [exclusive_request])

    assert first["reserved_nodes"] == ["reagent-source"]
    assert replay["allocations"] == first["allocations"]
    assert replay["reserved_nodes"] == []
    assert store.get_instance(identities["first"])["status"] == "reserved"
    with pytest.raises(InsufficientStock):
        service.admit_material_sources("workflow-exclusive-b", [exclusive_request])


def test_same_task_shared_source_reuses_its_reserved_instance(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """同一任务独占搬走的盒子，之后仍可作为共享来源再次绑定。

    参数：``inventory`` 提供已被本任务预留的孔板。返回：无；断言本任务按
    固定实例做共享来源时能绑上这只孔板，另一个任务仍然被拒绝。异常：其他
    任务以 ``InsufficientStock`` 失败。
    """

    _store, service, identities = inventory
    exclusive_request = MaterialSourceAdmissionRequest(
        node_id="move-box",
        resource_template_uuid=identities["template"],
        custody_policy="task_exclusive",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
            site_uuid=SITE_A,
        ),
    )
    shared_request = MaterialSourceAdmissionRequest(
        node_id="waste-box",
        resource_template_uuid=identities["template"],
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            instance_uuid=identities["first"],
        ),
    )

    service.admit_material_sources("workflow-move", [exclusive_request])
    admitted = service.admit_material_sources("workflow-move", [shared_request])

    assert admitted["allocations"]["waste-box"] == [identities["first"]]
    with pytest.raises(InsufficientStock):
        service.admit_material_sources("workflow-other", [shared_request])


def test_same_task_shared_source_finds_reserved_box_by_site(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """同一任务按库位再找已经独占预留的盒子时，共享来源仍能选中它。"""

    _store, service, identities = inventory
    service.admit_material_sources(
        "workflow-move",
        [
            MaterialSourceAdmissionRequest(
                node_id="move-box",
                resource_template_uuid=identities["template"],
                custody_policy="task_exclusive",
                requirement=MaterialRequirement(
                    template_id=identities["template"],
                    mount_uuid=identities["mount"],
                    site_uuid=SITE_A,
                ),
            )
        ],
    )
    admitted = service.admit_material_sources(
        "workflow-move",
        [
            MaterialSourceAdmissionRequest(
                node_id="waste-box",
                resource_template_uuid=identities["template"],
                custody_policy="shared_source",
                requirement=MaterialRequirement(
                    template_id=identities["template"],
                    mount_uuid=identities["mount"],
                    site_uuid=SITE_A,
                ),
            )
        ],
    )

    assert admitted["allocations"]["waste-box"] == [identities["first"]]
    with pytest.raises(InsufficientStock):
        service.admit_material_sources(
            "workflow-other",
            [
                MaterialSourceAdmissionRequest(
                    node_id="waste-box",
                    resource_template_uuid=identities["template"],
                    custody_policy="shared_source",
                    requirement=MaterialRequirement(
                        template_id=identities["template"],
                        mount_uuid=identities["mount"],
                        site_uuid=SITE_A,
                    ),
                )
            ],
        )


def test_material_source_admission_rolls_back_whole_request_set(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """任一物料来源不可用时必须回滚同任务的全部新绑定。

    参数：``inventory`` 提供一个有物料库位和一个空库位。返回：无；断言先选中
    的共享来源不会在后一来源失败后残留持久绑定。异常：空库位以
    ``InsufficientStock`` 终止整组准入。
    """

    store, service, identities = inventory
    requests = [
        MaterialSourceAdmissionRequest(
            node_id="a-shared-source",
            resource_template_uuid=identities["template"],
            custody_policy="shared_source",
            requirement=MaterialRequirement(
                template_id=identities["template"],
                mount_uuid=identities["mount"],
                site_uuid=SITE_A,
            ),
        ),
        MaterialSourceAdmissionRequest(
            node_id="z-missing-source",
            resource_template_uuid=identities["template"],
            custody_policy="task_exclusive",
            requirement=MaterialRequirement(
                template_id=identities["template"],
                mount_uuid=identities["mount"],
                site_uuid=SITE_EMPTY,
            ),
        ),
    ]

    with pytest.raises(InsufficientStock):
        service.admit_material_sources("workflow-atomic", requests)

    assert (
        store.query_all(
            "SELECT * FROM inventory_material_source_binding WHERE workflow_id=?",
            ("workflow-atomic",),
        )
        == []
    )
    assert store.reservations_for_workflow("workflow-atomic") == []


def test_task_material_admission_rolls_back_source_when_quantity_is_insufficient(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """来源实例与数量库存必须共享一个库存事务，任一失败整体零写入。"""

    store, service, identities = inventory
    content_uuid = "71000000-0000-4000-8000-000000000001"
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO current_substance(
                uuid,create_time,update_time,description,meta_data,material_uuid,
                name,composition,quantity,quantity_unit,physical_state,revision,observed_at
            ) VALUES(?,?,?,NULL,'{}',?,'测试内容','[]',1,'mL','liquid',1,?)""",
            (
                content_uuid,
                "2026-09-02T00:00:00Z",
                "2026-09-02T00:00:00Z",
                identities["first"],
                "2026-09-02T00:00:00Z",
            ),
        )
    source = MaterialSourceAdmissionRequest(
        node_id="source-node",
        resource_template_uuid=identities["template"],
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            instance_uuid=identities["first"],
        ),
    )
    task_uuid = "72000000-0000-4000-8000-000000000001"
    allocation = {
        "uuid": "73000000-0000-4000-8000-000000000001",
        "workflow_task_uuid": task_uuid,
        "workflow_node_job_uuid": "74000000-0000-4000-8000-000000000001",
        "requirement_key": "too-much",
        "inventory_type": "current_substance",
        "inventory_uuid": content_uuid,
        "material_uuid": identities["first"],
        "material_source_node_uuid": "source-node",
        "reserved_quantity": 2,
        "quantity_unit": "mL",
    }

    with pytest.raises(WorkflowQuantityReservationError):
        service.admit_task_materials(task_uuid, [source], [allocation])

    assert (
        store.query_all(
            "SELECT * FROM inventory_material_source_binding WHERE workflow_id=?",
            (task_uuid,),
        )
        == []
    )
    assert (
        store.query_all(
            "SELECT * FROM inventory_reservation WHERE workflow_id=?", (task_uuid,)
        )
        == []
    )


def test_task_material_admission_rechecks_quantity_source_container(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """来源解析变化时数量分配不得预留到另一个容器。"""

    store, service, identities = inventory
    content_uuid = "75000000-0000-4000-8000-000000000001"
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO current_substance(
                uuid,create_time,update_time,description,meta_data,material_uuid,
                name,composition,quantity,quantity_unit,physical_state,revision,observed_at
            ) VALUES(?,?,?,NULL,'{}',?,'测试内容','[]',3,'mL','liquid',1,?)""",
            (
                content_uuid,
                "2026-09-02T00:00:00Z",
                "2026-09-02T00:00:00Z",
                identities["first"],
                "2026-09-02T00:00:00Z",
            ),
        )
    source = MaterialSourceAdmissionRequest(
        node_id="source-node",
        resource_template_uuid=identities["template"],
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            instance_uuid=identities["first"],
        ),
    )
    task_uuid = "76000000-0000-4000-8000-000000000001"
    allocation = {
        "uuid": "77000000-0000-4000-8000-000000000001",
        "workflow_task_uuid": task_uuid,
        "workflow_node_job_uuid": "78000000-0000-4000-8000-000000000001",
        "requirement_key": "changed-container",
        "inventory_type": "current_substance",
        "inventory_uuid": content_uuid,
        "material_uuid": identities["second"],
        "material_source_node_uuid": "source-node",
        "reserved_quantity": 2,
        "quantity_unit": "mL",
    }

    with pytest.raises(InsufficientStock, match="容器.*变化"):
        service.admit_task_materials(task_uuid, [source], [allocation])

    assert store.query_all(
        "SELECT * FROM inventory_material_source_binding WHERE workflow_id=?",
        (task_uuid,),
    ) == []
    assert store.query_all(
        "SELECT * FROM inventory_reservation WHERE workflow_id=?",
        (task_uuid,),
    ) == []


def test_child_lock_binding_uses_normal_selection_and_replays(inventory):
    """多候选按原有顺序选料，重启和运行时重放仍复用冻结的锁身份。"""
    store, service, ids = inventory
    req = MaterialSourceAdmissionRequest(
        node_id="child", resource_template_uuid=ids["template"], custody_policy="task_exclusive",
        requirement=MaterialRequirement(template_id=ids["template"], mount_uuid=ids["mount"]),
    )
    assert service.admit_task_materials("owner", [req], [])["allocations"] == {"child": [ids["second"]]}
    # 另一任务仍可拿到余下的板；两任务不会绑定同一块板。
    assert service.admit_task_materials("competitor", [req], [])["allocations"] == {"child": [ids["first"]]}
    with pytest.raises(MaterialClaimedByOtherTask):
        service.admit_task_materials("third", [req], [])
    runtime = replace(req, planned_material_uuid=ids["second"])
    restarted = InventoryService(store)
    assert restarted.admit_task_materials("owner", [runtime], [])["allocations"]["child"] == [ids["second"]]
    with pytest.raises(CommandRejected, match="已冻结资源锁"):
        restarted.admit_task_materials("owner", [replace(runtime, planned_material_uuid=ids["first"])], [])
    restarted.release_workflow("owner", reason="workflow_canceled")
    restarted.release_workflow("competitor", reason="workflow_canceled")
    with store.transaction() as conn:
        conn.execute("UPDATE site SET sort_order=0 WHERE uuid=?", (SITE_A,))
    with pytest.raises(CommandRejected, match="已冻结资源锁"):
        restarted.admit_material_sources("owner", [runtime], attempt=2)
    assert not store.query_all("SELECT * FROM inventory_reservation WHERE attempt=2")


def test_material_source_binding_replays_after_service_restart_and_rejects_change(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """持久绑定必须跨服务重建重放，并拒绝同尝试偷换选择器。

    参数：``inventory`` 提供同一个 SQLite 库及库存身份。返回：无；断言新建
    ``InventoryService`` 后仍返回首次选择的物料。异常：同一任务尝试修改保管
    策略时抛 ``CommandRejected``，避免重放漂移。
    """

    store, service, identities = inventory
    shared_request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=identities["template"],
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
            site_uuid=SITE_A,
        ),
    )
    first = service.admit_material_sources("workflow-replay", [shared_request])

    restarted_service = InventoryService(store)
    replay = restarted_service.admit_material_sources(
        "workflow-replay", [shared_request]
    )

    assert replay["allocations"] == first["allocations"]
    changed_request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=identities["template"],
        custody_policy="task_exclusive",
        requirement=shared_request.requirement,
    )
    with pytest.raises(CommandRejected):
        restarted_service.admit_material_sources("workflow-replay", [changed_request])
    released = restarted_service.release_workflow(
        "workflow-replay",
        reason="workflow_succeeded",
    )
    second_attempt = restarted_service.admit_material_sources(
        "workflow-replay",
        [shared_request],
        attempt=2,
    )

    assert released["released_bindings"] == ["reagent-source"]
    assert second_attempt["allocations"] == first["allocations"]


def test_fixed_material_source_rejects_instance_from_another_template(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """固定物料身份必须仍满足来源节点声明的资源模板。

    参数：``inventory`` 提供一个真实孔板实例。返回：无；断言伪造的另一模板
    选择器不能把该实例冻结为共享绑定。异常：模板与实例事实不一致时库存权威
    抛 ``CommandRejected``，且不留下绑定。
    """

    store, service, identities = inventory
    wrong_template_uuid = "90000000-0000-4000-8000-000000000099"
    request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=wrong_template_uuid,
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=wrong_template_uuid,
            instance_uuid=identities["first"],
        ),
    )

    with pytest.raises(CommandRejected):
        service.admit_material_sources("workflow-wrong-template", [request])

    assert (
        store.query_all(
            "SELECT * FROM inventory_material_source_binding WHERE workflow_id=?",
            ("workflow-wrong-template",),
        )
        == []
    )


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
    replayed = service.move_instance(
        identities["first"],
        parent_uuid=target["uuid"],
        slot_id="A1",
        actor="station_scheduler.material_transfer",
        causation_id="workflow-node-job:job-1:material-transfer",
    )

    assert moved["parent_uuid"] == target["uuid"]
    assert replayed == moved
    assert (
        store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?", (SITE_A,)
        )["occupied_material_uuid"]
        is None
    )
    assert (
        store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?", (TARGET_SITE,)
        )["occupied_material_uuid"]
        == identities["first"]
    )
    ledger = store.query_one(
        "SELECT op_type,delta_json,actor FROM inventory_ledger "
        "WHERE aggregate_id=? ORDER BY ledger_id DESC LIMIT 1",
        (identities["first"],),
    )
    assert ledger["op_type"] == "instance.moved"
    assert ledger["actor"] == "host_node.transfer_resource"
    assert json.loads(ledger["delta_json"])["to_slot"] == "A1"
    assert (
        store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_ledger "
            "WHERE aggregate_id=? AND op_type='instance.moved'",
            (identities["first"],),
        )["count"]
        == 1
    )


def test_compatibility_move_rejects_occupied_target_without_losing_either_material(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """兼容 move 不能覆盖目标 Site 原有物料，并且失败必须零写入。"""

    store, service, identities = inventory

    with pytest.raises(CommandRejected, match="occupied"):
        service.move_instance(
            identities["first"],
            parent_uuid=identities["mount"],
            slot_id="B1",
            actor="compatibility.move",
        )

    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SITE_A,),
    )["occupied_material_uuid"] == identities["first"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SITE_B,),
    )["occupied_material_uuid"] == identities["second"]
    assert service.store.get_instance(identities["first"])["parent_uuid"] == identities[
        "mount"
    ]


def test_site_occupancy_api_is_idempotent_and_rejects_another_material(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """统一原子 API 接受同 Material/Site 重放，但不能覆盖另一个 Material。"""

    store, _service, identities = inventory
    with store.transaction() as connection:
        assert set_site_occupancy(
            connection,
            site_uuid=SITE_A,
            material_uuid=identities["first"],
        ) == SITE_A
        with pytest.raises(SiteOccupancyConflict) as raised:
            set_site_occupancy(
                connection,
                site_uuid=SITE_B,
                material_uuid=identities["first"],
            )

    assert raised.value.code == "site_occupied"
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SITE_A,),
    )["occupied_material_uuid"] == identities["first"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SITE_B,),
    )["occupied_material_uuid"] == identities["second"]


def test_site_occupancy_unique_index_is_the_last_line_of_defense(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """绕过领域 API 的直接 SQL 也不能让一个 Material 同时占两个活动 Site。"""

    store, _service, identities = inventory
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as connection:
            connection.execute(
                "UPDATE site SET occupied_material_uuid=? WHERE uuid=?",
                (identities["first"], SITE_EMPTY),
            )

    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SITE_A,),
    )["occupied_material_uuid"] == identities["first"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SITE_EMPTY,),
    )["occupied_material_uuid"] is None


def test_legacy_relation_write_rejects_occupied_target_atomically(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """旧 relation 写入口也不能覆盖目标 Site，并且冲突时不移动来源物料。"""

    store, _service, identities = inventory
    with pytest.raises(sqlite3.IntegrityError, match="target site is occupied"):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO resource_relation(parent_uuid,slot_id,child_uuid,version) "
                "VALUES (?,?,?,1)",
                (identities["mount"], "B1", identities["first"]),
            )

    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SITE_A,),
    )["occupied_material_uuid"] == identities["first"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SITE_B,),
    )["occupied_material_uuid"] == identities["second"]
