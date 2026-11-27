"""工作流数量型库存运行合同。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_contract import (
    RESOURCE_DATA_CONFLICT,
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.content_contract import (
    BackendContainerContentService,
)
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.workflow_quantity import (
    convert_quantity,
    format_quantity_shortage_message,
)
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput

from .quantity_inventory_test_support import (
    JOB_UUID,
    NODE_UUID,
    TASK_UUID,
    WORKFLOW_UUID,
    _inventory,
    _workflow,
)


class _InventoryReadBridge:
    """把数量型库存协调器暴露为工作流服务只读端口。"""

    def __init__(self, coordinator: WorkflowQuantityInventory) -> None:
        """绑定协调器；参数是既有库存事实读取器，返回无且不创建新存储。"""

        self._coordinator = coordinator

    def list_task_inventory_consumptions(self, task_uuid: str) -> list[dict[str, Any]]:
        """按任务读取消费；参数是任务 UUID，返回统一库存台账投影。"""

        return self._coordinator.list_task_consumptions(task_uuid)

    def list_job_inventory_consumptions(self, job_uuid: str) -> list[dict[str, Any]]:
        """按作业读取消费；参数是作业 UUID，返回统一库存台账投影。"""

        return self._coordinator.list_job_consumptions(job_uuid)

    def list_reagent_inventory_consumptions(
        self, reagent_uuid: str
    ) -> list[dict[str, Any]]:
        """按试剂读取消费；参数是试剂 UUID，返回统一库存台账投影。"""

        return self._coordinator.list_reagent_consumptions(reagent_uuid)

    def close(self) -> None:
        """释放测试只读端口；参数无、返回无，协调器由外层存储生命周期管理。"""


class _InventoryCreationBridge(_InventoryReadBridge):
    """暴露任务创建数量预留及回滚补偿的最小测试端口。"""

    def prepare_inventory_allocations(
        self,
        connection: Any,
        *,
        graph: dict[str, Any],
        prepared: PreparedTaskInput,
        task_uuid: str,
        bindings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """代理数量预留；参数与生产桥一致，返回待写分配，异常原样传播。"""

        return self._coordinator.prepare_task_allocations(
            connection,
            graph=graph,
            prepared=prepared,
            task_uuid=task_uuid,
            bindings=bindings,
        )

    def discard_uncommitted_inventory(self, task_uuid: str) -> None:
        """代理创建回滚补偿；参数是未提交任务身份，返回无且重复调用幂等。"""

        self._coordinator.discard_uncommitted_task(task_uuid)

    def preflight_inventory_allocations(
        self,
        *,
        graph: dict[str, Any],
        prepared: PreparedTaskInput,
        bindings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """代理只读数量预检；参数与生产桥一致，返回分配预览。"""

        return self._coordinator.preflight_task_allocations(
            graph=graph,
            prepared=prepared,
            bindings=bindings,
        )


def _create_task(
    store: WorkflowStore,
    coordinator: WorkflowQuantityInventory,
    *,
    graph: dict[str, Any],
    prepared: PreparedTaskInput,
    reagent_uuid: str,
) -> None:
    """以公共任务创建事务写入 Task、Job、allocation 与 Saga。

    参数：两个写权威、同一图快照、冻结输入和试剂 UUID。返回无；异常原样传播。
    """

    bindings = [
        {
            "requirement_key": "ethanol",
            "inventory_type": "reagent",
            "inventory_uuid": reagent_uuid,
            "reserved_quantity": 2,
            "quantity_unit": "mL",
        }
    ]
    store.create_task_with_jobs(
        workflow_uuid=WORKFLOW_UUID,
        task_uuid=TASK_UUID,
        run_mode="normal",
        target_node_uuid=None,
        description=None,
        meta_data={},
        plan_builder=lambda _graph: prepared,
        inventory_allocation_builder=lambda connection, _graph, frozen: (
            coordinator.prepare_task_allocations(
                connection,
                graph=graph,
                prepared=frozen,
                task_uuid=TASK_UUID,
                bindings=bindings,
            )
        ),
    )


def test_shortage_message_names_every_short_site() -> None:
    """两个及以上库位同时不够时，文案按检查顺序列出全部库位名。"""

    message = format_quantity_shortage_message(
        [
            {"site_name": "R1C4", "material_name": "试剂瓶堆栈 R1C4"},
            {"site_name": "R1C5", "material_name": "试剂瓶堆栈 R1C5"},
        ]
    )
    assert message == "R1C4、R1C5库存实例可用数量不足，无法完成整任务预留"


def test_quantity_preflight_is_all_or_nothing_and_read_only(tmp_path: Path) -> None:
    """共享试剂预检校验整任务数量且不得产生任何持久写入。

    参数：``tmp_path`` 隔离工作流和库存库。返回无；断言成功预检返回完整分配，
    试剂余量、活动预留、台账、Outbox、Task 与 allocation 行数均保持不变；把
    余量降到需求以下后整组失败且仍然零写入。
    """

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    assert WorkflowService._validated_applied_backend_graph(graph)[
        "inventory_requirements"
    ] == graph["inventory_requirements"]
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    bindings = [
        {
            "requirement_key": "ethanol",
            "inventory_type": "reagent",
            "inventory_uuid": reagent_uuid,
            "reserved_quantity": 2,
            "quantity_unit": "mL",
        }
    ]

    def facts() -> tuple[Any, ...]:
        """读取两库可观察写事实；返回稳定快照元组。"""

        return (
            inventory_store.query_one(
                "SELECT quantity,revision FROM reagent WHERE uuid=?",
                (reagent_uuid,),
            ),
            inventory_store.query_one(
                "SELECT COUNT(*) AS count FROM inventory_reservation"
            ),
            inventory_store.query_one("SELECT COUNT(*) AS count FROM inventory_ledger"),
            inventory_store.query_one("SELECT COUNT(*) AS count FROM sync_outbox"),
            workflow_store.count_rows("workflow_task"),
            workflow_store.count_rows("workflow_inventory_allocation"),
        )

    try:
        before = facts()
        allocations = coordinator.preflight_task_allocations(
            graph=graph,
            prepared=prepared,
            bindings=bindings,
        )
        assert len(allocations) == 1
        assert allocations[0]["reserved_quantity"] == 2
        assert facts() == before

        with inventory_store.transaction() as connection:
            connection.execute(
                "UPDATE reagent SET quantity=1 WHERE uuid=?",
                (reagent_uuid,),
            )
        insufficient_before = facts()
        with pytest.raises(StoreConflict, match="工作流乙醇瓶库存实例可用数量不足") as caught:
            coordinator.preflight_task_allocations(
                graph=graph,
                prepared=prepared,
                bindings=bindings,
            )
        assert caught.value.shortages == [
            {
                "site_name": "",
                "material_uuid": material_uuid,
                "material_name": "工作流乙醇瓶",
                "available_quantity": 1.0,
                "required_quantity": 2.0,
                "quantity_unit": "mL",
            }
        ]
        assert facts() == insufficient_before
    finally:
        workflow_store.close()
        inventory_store.close()


@pytest.mark.parametrize("inventory_type", ["reagent", "current_substance"])
def test_quantity_requirement_auto_binds_material_source_container_content(
    tmp_path: Path,
    inventory_type: str,
) -> None:
    """物料来源数量需求应自动绑定容器的试剂或当前内容物。"""

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    content = {"uuid": reagent_uuid}
    if inventory_type == "current_substance":
        BackendReagentService(inventory_store).delete_reagent(reagent_uuid)
        content = BackendContainerContentService(
            inventory_store
        ).create_current_substance(
            {
                "material_uuid": material_uuid,
                "name": "泵用溶剂",
                "quantity": 3,
                "quantity_unit": "mL",
                "physical_state": "liquid",
            }
        )
    template_uuid = inventory_store.query_one(
        "SELECT resource_template_uuid FROM material WHERE uuid=?",
        (material_uuid,),
    )["resource_template_uuid"]
    station = BackendResourceService(inventory_store).create_material(
        {
            "resource_template_uuid": template_uuid,
            "name": "液体试剂架",
        }
    )
    site_uuid = "23000000-0000-4000-8000-000000000001"
    with inventory_store.transaction() as connection:
        connection.execute(
            "INSERT INTO site(uuid,create_time,update_time,material_uuid,name,"
            "occupied_material_uuid) VALUES(?,?,?,?,?,?)",
            (
                site_uuid,
                "2026-01-01T00:00:00.000Z",
                "2026-01-01T00:00:00.000Z",
                station["uuid"],
                "R1C1",
                material_uuid,
            ),
        )
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    source_uuid = "21000000-0000-4000-8000-000000000001"
    requirement = {
        **graph["inventory_requirements"][0],
        "target_type": "current_substance",
        "reagent_info_uuid": None,
        "required_quantity": 2,
        "meta_data": {
            "unilab": {"material_source_node_uuid": source_uuid}
        },
    }
    graph["inventory_requirements"] = [requirement]
    source_node = {
        "uuid": source_uuid,
        "kind": "material_source",
        "material_requirements": [
            {
                "template_id": "22000000-0000-4000-8000-000000000001",
                "site_uuid": site_uuid,
            }
        ],
    }
    auto_prepared = replace(
        prepared,
        workflow_snapshot=graph,
        execution_plan={
            **prepared.execution_plan,
            "nodes": [source_node, *prepared.execution_plan["nodes"]],
        },
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    try:
        allocations = coordinator.preflight_task_allocations(
            graph=graph,
            prepared=auto_prepared,
            bindings=[],
        )
        assert allocations == [
            {
                "uuid": allocations[0]["uuid"],
                "workflow_task_uuid": "00000000-0000-4000-8000-000000000001",
                "workflow_node_job_uuid": JOB_UUID,
                "requirement_key": "ethanol",
                "inventory_type": inventory_type,
                "inventory_uuid": content["uuid"],
                "material_uuid": material_uuid,
                "reserved_quantity": 2,
                "quantity_unit": "mL",
            }
        ]

        workflow_store.create_task_with_jobs(
            workflow_uuid=WORKFLOW_UUID,
            task_uuid=TASK_UUID,
            run_mode="normal",
            target_node_uuid=None,
            description=None,
            meta_data={},
            plan_builder=lambda _graph: auto_prepared,
            inventory_allocation_builder=lambda connection, _graph, frozen: (
                coordinator.prepare_task_allocations(
                    connection,
                    graph=graph,
                    prepared=frozen,
                    task_uuid=TASK_UUID,
                    bindings=[],
                )
            ),
        )
        assert coordinator.task_allocations(TASK_UUID)[0][
            "material_source_node_uuid"
        ] == source_uuid
        coordinator.consume_successful_job(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            consumptions=[],
        )
        assert inventory_store.query_one(
            f"SELECT quantity FROM {inventory_type} WHERE uuid=?",
            (content["uuid"],),
        ) == {"quantity": 8.0 if inventory_type == "reagent" else 1.0}

        with inventory_store.transaction() as connection:
            connection.execute(
                f"UPDATE {inventory_type} SET quantity=1 WHERE uuid=?",
                (content["uuid"],),
            )
        with pytest.raises(StoreConflict, match="可用数量不足"):
            coordinator.preflight_task_allocations(
                graph=graph,
                prepared=auto_prepared,
                bindings=[],
            )
        if inventory_type == "reagent":
            BackendReagentService(inventory_store).delete_reagent(content["uuid"])
        else:
            BackendContainerContentService(
                inventory_store
            ).delete_current_substance(content["uuid"])
        with pytest.raises(StoreConflict, match="没有试剂或当前内容物数量"):
            coordinator.preflight_task_allocations(
                graph=graph,
                prepared=auto_prepared,
                bindings=[],
            )
    finally:
        workflow_store.close()
        inventory_store.close()


def test_quantity_preflight_rejects_lazy_job_requirements(tmp_path: Path) -> None:
    """惰性节点不得绕过数量库存准入，未支持逐轮分配前须失败关闭。"""

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    lazy_prepared = replace(prepared, jobs=[])
    bindings = [
        {
            "requirement_key": "ethanol",
            "inventory_type": "reagent",
            "inventory_uuid": reagent_uuid,
            "reserved_quantity": 2,
            "quantity_unit": "mL",
        }
    ]

    try:
        with pytest.raises(StoreConflict, match="尚未支持逐轮分配"):
            coordinator.preflight_task_allocations(
                graph=graph,
                prepared=lazy_prepared,
                bindings=bindings,
            )
    finally:
        workflow_store.close()
        inventory_store.close()


def test_quantity_preflight_http_reports_candidate_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST 运行预检应使用候选绑定返回可运行提示且保持两库零写入。"""

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    service = WorkflowService(
        workflow_store,
        task_scheduler_bridge=_InventoryCreationBridge(coordinator),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        service, "_prepare_task_input", lambda *args, **kwargs: prepared
    )
    monkeypatch.setattr(service, "get_graph", lambda _workflow_uuid: graph)
    service._material_resolver = lambda material_uuid: {"uuid": material_uuid}
    client = TestClient(create_workflow_app(service))
    before = (
        workflow_store.count_rows("workflow_task"),
        inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_reservation"
        ),
        inventory_store.query_one("SELECT COUNT(*) AS count FROM sync_outbox"),
    )
    try:
        response = client.post(
            f"/api/v1/workflows/{graph['workflow']['uuid']}/run-preflight",
            json={
                "run_mode": "normal",
                "input": {},
                "inventory_bindings": [
                    {
                        "requirement_key": "ethanol",
                        "inventory_type": "reagent",
                        "inventory_uuid": reagent_uuid,
                        "reserved_quantity": 2,
                        "quantity_unit": "mL",
                    }
                ],
            },
        )
        assert response.status_code == 200
        report = response.json()["data"]
        inventory_check = next(
            check for check in report["checks"] if check["type"] == "quantity_inventory"
        )
        assert inventory_check["status"] == "passed", inventory_check
        assert inventory_check["details"]["allocation_count"] == 1
        assert (
            workflow_store.count_rows("workflow_task"),
            inventory_store.query_one(
                "SELECT COUNT(*) AS count FROM inventory_reservation"
            ),
            inventory_store.query_one("SELECT COUNT(*) AS count FROM sync_outbox"),
        ) == before
    finally:
        service.close()
        inventory_store.close()


def test_task_binding_and_successful_consumption_reuse_existing_tables(
    tmp_path: Path,
) -> None:
    """任务绑定与成功消费必须闭合且不创建第二套库存表。

    参数：``tmp_path`` 隔离两个 SQLite。返回无；断言 Task/Job/分配同事务创建，
    实际消耗 1.5 mL 后余量为 8.5，重复结果不再次扣减且仅追加一条消费台账。
    """

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    inventory_service = InventoryService(inventory_store)
    coordinator = WorkflowQuantityInventory(workflow_store, inventory_service)
    try:
        _create_task(
            workflow_store,
            coordinator,
            graph=graph,
            prepared=prepared,
            reagent_uuid=reagent_uuid,
        )
        allocation = workflow_store._conn.execute(
            "SELECT * FROM workflow_inventory_allocation"
        ).fetchone()

        first = coordinator.consume_successful_job(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            consumptions=[
                {
                    "inventory_type": "reagent",
                    "inventory_uuid": reagent_uuid,
                    "actual_quantity": 1500,
                    "quantity_unit": "uL",
                }
            ],
        )
        replay = coordinator.consume_successful_job(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            consumptions=[
                {
                    "inventory_type": "reagent",
                    "inventory_uuid": reagent_uuid,
                    "actual_quantity": 1500,
                    "quantity_unit": "uL",
                }
            ],
        )

        assert allocation is not None
        assert allocation["status"] == "reserved"
        assert first == replay
        assert first[0]["status"] == "consumed"
        assert first[0]["balance_before"] == 10
        assert first[0]["balance_after"] == 8.5
        assert first[0]["actual_quantity"] == 1.5
        assert first[0]["workflow_node_uuid"] == NODE_UUID
        assert first[0]["workflow_name"] == "数量型库存合同"
        assert first[0]["node_name"] == "分液"
        assert first[0]["display_name"] == "乙醇"
        assert first[0]["container_barcode"] == "WF-ETHANOL-001"
        assert first[0]["consumed_at"].endswith("Z")
        assert first[0]["meta_data"]["quantity_conversion"]["kind"] == (
            "same_dimension"
        )
        assert coordinator.list_reagent_consumptions(reagent_uuid) == first
        assert inventory_store.query_one(
            "SELECT quantity,revision FROM reagent WHERE uuid=?", (reagent_uuid,)
        ) == {"quantity": 8.5, "revision": 2}
        assert inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_ledger "
            "WHERE workflow_node_job_uuid=? "
            "AND subject_type IN ('reagent','current_substance')",
            (JOB_UUID,),
        ) == {"count": 1}
        reservation = inventory_store.query_one(
            "SELECT status,amounts_json FROM inventory_reservation "
            "WHERE workflow_id=? AND node_id=?",
            (TASK_UUID, JOB_UUID),
        )
        assert reservation is not None
        assert reservation["status"] == "consumed"
        table_names = {
            row["name"]
            for row in inventory_store.query_all(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "workflow_inventory_consumption" not in table_names
    finally:
        workflow_store.close()
        inventory_store.close()


def test_successful_consumption_uses_snapshot_when_definition_is_not_in_history(
    tmp_path: Path,
) -> None:
    """成功结果应结算仅在历史库持久化的真实 Task/Job。

    参数：``tmp_path`` 隔离定义目录、运行历史与库存。返回无；断言生产形态的
    运行历史库不含 Workflow 定义时，结算仍能用冻结任务快照生成消费审计并扣减
    一次库存。异常：定义未写入历史库不应报错；Task/Job 关系缺失仍须关闭式冲突。
    """

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    definition_store, graph, prepared = _workflow(
        tmp_path / "definition-catalog.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    workflow_store = WorkflowStore(
        tmp_path / "workflow_history.db",
        persist_workflow_definitions=False,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    try:
        workflow_store.create_task_with_jobs(
            workflow_uuid=WORKFLOW_UUID,
            task_uuid=TASK_UUID,
            run_mode="normal",
            target_node_uuid=None,
            description=None,
            meta_data={},
            plan_builder=lambda _graph: prepared,
            inventory_allocation_builder=lambda connection, _graph, frozen: (
                coordinator.prepare_task_allocations(
                    connection,
                    graph=graph,
                    prepared=frozen,
                    task_uuid=TASK_UUID,
                    bindings=[
                        {
                            "requirement_key": "ethanol",
                            "inventory_type": "reagent",
                            "inventory_uuid": reagent_uuid,
                            "reserved_quantity": 2,
                            "quantity_unit": "mL",
                        }
                    ],
                )
            ),
            applied_graph=graph,
        )
        assert workflow_store.count_rows("workflow_task") == 1
        assert workflow_store.count_rows("workflow_node_job") == 1
        assert workflow_store.count_rows("workflow") == 0

        consumptions = coordinator.consume_successful_job(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            consumptions=[
                {
                    "inventory_type": "reagent",
                    "inventory_uuid": reagent_uuid,
                    "actual_quantity": 1.5,
                    "quantity_unit": "mL",
                }
            ],
        )

        assert consumptions[0]["workflow_name"] == "数量型库存合同"
        assert consumptions[0]["node_name"] == "分液"
        assert inventory_store.query_one(
            "SELECT quantity FROM reagent WHERE uuid=?", (reagent_uuid,)
        ) == {"quantity": 8.5}
        assert inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_ledger "
            "WHERE workflow_node_job_uuid=? "
            "AND subject_type IN ('reagent','current_substance')",
            (JOB_UUID,),
        ) == {"count": 1}
    finally:
        workflow_store.close()
        definition_store.close()
        inventory_store.close()


def test_missing_binding_rolls_back_task_job_and_allocation(tmp_path: Path) -> None:
    """有活动逻辑需求但未绑定库存时必须保持任务创建零写入。

    参数：``tmp_path`` 隔离数据库。返回无；断言冲突后 Task、Job、allocation 和
    Saga 均未写入，避免出现不可执行的半任务。
    """

    inventory_store, material_uuid, info_uuid, _reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    try:
        with pytest.raises(StoreConflict):
            workflow_store.create_task_with_jobs(
                workflow_uuid=WORKFLOW_UUID,
                task_uuid=TASK_UUID,
                run_mode="normal",
                target_node_uuid=None,
                description=None,
                meta_data={},
                plan_builder=lambda _graph: prepared,
                inventory_allocation_builder=lambda connection, _graph, frozen: (
                    coordinator.prepare_task_allocations(
                        connection,
                        graph=graph,
                        prepared=frozen,
                        task_uuid=TASK_UUID,
                        bindings=[],
                    )
                ),
            )

        assert workflow_store.count_rows("workflow_task") == 0
        assert workflow_store.count_rows("workflow_node_job") == 0
        assert workflow_store.count_rows("workflow_inventory_allocation") == 0
        assert workflow_store.count_rows("workflow_inventory_saga") == 0
    finally:
        workflow_store.close()
        inventory_store.close()


def test_pending_consumption_recovery_does_not_deduct_twice(tmp_path: Path) -> None:
    """库存已提交而分配状态未提交的崩溃窗口必须只补状态。

    参数：``tmp_path`` 隔离数据库。返回无；先完成一次消费，再模拟工作流库回滚
    到 ``consume_pending``，重启恢复后余量和台账数量保持不变、分配变 consumed。
    """

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    try:
        _create_task(
            workflow_store,
            coordinator,
            graph=graph,
            prepared=prepared,
            reagent_uuid=reagent_uuid,
        )
        coordinator.consume_successful_job(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            consumptions=[],
        )
        with workflow_store.transaction() as connection:
            connection.execute(
                """
                UPDATE workflow_inventory_allocation
                SET status='reserved',consumed_at=NULL,revision=1
                WHERE workflow_task_uuid=?
                """,
                (TASK_UUID,),
            )
            connection.execute(
                """
                UPDATE workflow_inventory_saga SET status='consume_pending'
                WHERE workflow_task_uuid=?
                """,
                (TASK_UUID,),
            )

        restarted = WorkflowQuantityInventory(
            workflow_store,
            InventoryService(inventory_store),
        )
        restarted.recover_pending()

        assert inventory_store.query_one(
            "SELECT quantity FROM reagent WHERE uuid=?", (reagent_uuid,)
        ) == {"quantity": 8.0}
        assert inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_ledger "
            "WHERE workflow_node_job_uuid=? "
            "AND subject_type IN ('reagent','current_substance')",
            (JOB_UUID,),
        ) == {"count": 1}
        assert (
            workflow_store._conn.execute(
                "SELECT status FROM workflow_inventory_allocation"
            ).fetchone()["status"]
            == "consumed"
        )
    finally:
        workflow_store.close()
        inventory_store.close()


def test_inventory_consumption_http_paths_match_backend_contract(
    tmp_path: Path,
) -> None:
    """任务、作业和试剂消费查询必须使用 Backend 的公共 HTTP 路径。

    参数：``tmp_path`` 隔离数据库。返回无；断言三个 GET 接口均返回标准
    ``code/data`` 包装和同一消费事实，不依赖 SQLite 表名或新增消费表。
    """

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    service = WorkflowService(
        workflow_store,
        task_scheduler_bridge=_InventoryReadBridge(coordinator),  # type: ignore[arg-type]
    )
    try:
        _create_task(
            workflow_store,
            coordinator,
            graph=graph,
            prepared=prepared,
            reagent_uuid=reagent_uuid,
        )
        expected = coordinator.consume_successful_job(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            consumptions=[],
        )
        client = TestClient(create_workflow_app(service))

        task_response = client.get(
            f"/api/v1/workflow-tasks/{TASK_UUID}/inventory-consumptions"
        )
        job_response = client.get(
            f"/api/v1/workflow-node-jobs/{JOB_UUID}/inventory-consumptions"
        )
        reagent_response = client.get(
            f"/api/v1/reagents/{reagent_uuid}/inventory-consumptions"
        )

        assert task_response.status_code == 200
        assert task_response.json() == {"code": 0, "data": expected}
        assert job_response.status_code == 200
        assert job_response.json() == {"code": 0, "data": expected}
        assert reagent_response.status_code == 200
        assert reagent_response.json() == {"code": 0, "data": expected}
    finally:
        service.close()
        inventory_store.close()


def test_active_workflow_reservation_protects_reagent_quantity(
    tmp_path: Path,
) -> None:
    """活动工作流预留必须阻止普通试剂写操作侵占已承诺数量。

    参数：``tmp_path`` 隔离两个 SQLite。返回无；创建 2 mL 任务预留后，把试剂
    余量从 10 mL 降到 1 mL 必须返回数据冲突，增加余量仍允许；释放任务后允许
    再次减少，证明保护边界位于现有库存写权威而非工作流表名。
    """

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    reagent_service = BackendReagentService(inventory_store)
    try:
        _create_task(
            workflow_store,
            coordinator,
            graph=graph,
            prepared=prepared,
            reagent_uuid=reagent_uuid,
        )
        current = reagent_service.get_reagent(reagent_uuid)
        with pytest.raises(BackendContractError) as raised:
            reagent_service.update_reagent(
                reagent_uuid,
                {
                    **current,
                    "quantity": 1,
                    "expected_revision": current["revision"],
                },
            )
        assert raised.value.code == RESOURCE_DATA_CONFLICT
        increased = reagent_service.update_reagent(
            reagent_uuid,
            {
                **current,
                "quantity": 11,
                "expected_revision": current["revision"],
            },
        )
        assert increased["quantity"] == 11

        coordinator.release_task(TASK_UUID, reason="test_terminal")
        released = reagent_service.update_reagent(
            reagent_uuid,
            {
                **increased,
                "quantity": 1,
                "expected_revision": increased["revision"],
            },
        )
        assert released["quantity"] == 1
        assert inventory_store.query_one(
            "SELECT status FROM inventory_reservation "
            "WHERE workflow_id=? AND node_id=?",
            (TASK_UUID, JOB_UUID),
        ) == {"status": "released"}
    finally:
        workflow_store.close()
        inventory_store.close()


def test_task_creation_failure_compensates_inventory_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作流事务在库存预留后失败时必须立即补偿库存权威。

    参数：临时目录隔离数据库，``monkeypatch`` 注入预留之后的工作流写失败。返回
    无；断言 API 语义仍为非法创建、Task 未落库，并且现有库存预留已 released，
    不依赖进程重启才恢复可用量。
    """

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store,
        InventoryService(inventory_store),
    )
    bridge = _InventoryCreationBridge(coordinator)
    service = WorkflowService(
        workflow_store,
        task_scheduler_bridge=bridge,  # type: ignore[arg-type]
    )
    created_task_uuid = ""

    def fail_after_reservation(**values: Any) -> dict[str, Any]:
        """执行真实库存分配后模拟工作流事务失败；参数为创建载荷，不返回。"""

        nonlocal created_task_uuid
        created_task_uuid = str(values["task_uuid"])
        with workflow_store.transaction() as connection:
            frozen = values["plan_builder"](graph)
            values["inventory_allocation_builder"](connection, graph, frozen)
            raise StoreConflict("模拟工作流写入失败")

    monkeypatch.setattr(
        service,
        "_prepare_task_input",
        lambda *args, **kwargs: prepared,
    )
    monkeypatch.setattr(workflow_store, "create_task_with_jobs", fail_after_reservation)
    try:
        with pytest.raises(WorkflowError) as raised:
            service.create_workflow_task(
                workflow_uuid=WORKFLOW_UUID,
                run_mode="normal",
                target_node_uuid=None,
                input_value={},
                description=None,
                meta_data={},
                inventory_bindings=[
                    {
                        "requirement_key": "ethanol",
                        "inventory_type": "reagent",
                        "inventory_uuid": reagent_uuid,
                        "reserved_quantity": 2,
                        "quantity_unit": "mL",
                    }
                ],
            )
        assert raised.value.code == "invalid_input"
        assert workflow_store.count_rows("workflow_task") == 0
        assert inventory_store.query_one(
            "SELECT status FROM inventory_reservation WHERE workflow_id=?",
            (created_task_uuid,),
        ) == {"status": "released"}
    finally:
        service.close()
        inventory_store.close()


def test_same_discrete_unit_converts_without_mass_or_volume_table() -> None:
    piece = convert_quantity(
        5,
        "piece",
        "piece",
        inventory_type="substance",
        physical_state="solid",
        concentration_value=None,
        density_g_per_ml=None,
    )
    assert piece is not None and piece[0] == 5
    count = convert_quantity(
        5,
        "count",
        "count",
        inventory_type="substance",
        physical_state="solid",
        concentration_value=None,
        density_g_per_ml=None,
    )
    assert count is not None and count[0] == 5
    assert (
        convert_quantity(
            5,
            "piece",
            "g",
            inventory_type="substance",
            physical_state="solid",
            concentration_value=None,
            density_g_per_ml=None,
        )
        is None
    )
    grams = convert_quantity(
        5,
        "g",
        "mg",
        inventory_type="reagent",
        physical_state="solid",
        concentration_value=None,
        density_g_per_ml=None,
    )
    assert grams is not None and grams[0] == 5000
