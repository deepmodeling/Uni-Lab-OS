"""数量型库存的本地 Edge 结果回放合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.edge_control.local_authority import (
    LocalEdgeAuthorityStore,
    LocalEdgeControlAuthority,
    create_local_edge_control_router,
)
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.stack import create_edge_stack
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

from .quantity_inventory_test_support import (
    JOB_UUID,
    NODE_UUID,
    TASK_UUID,
    WORKFLOW_UUID,
    _inventory,
    _workflow,
)


def _device_material(inventory_store: InventoryStore) -> str:
    """创建数量分液器物料并返回其 UUID。

    参数：``inventory_store`` 是隔离库存写权威。返回：可绑定设备物料 UUID。
    异常：模板或物料创建失败时由公共库存服务原样传播。
    """

    inventory_backend = BackendResourceService(inventory_store)
    device_template = inventory_backend.sync_resource_templates(
        [
            {
                "id": "test.quantity-dispenser",
                "display_name": "数量库存分液器",
                "registry_type": "device",
                "class": {},
            }
        ]
    )["templates"][0]
    device = inventory_backend.create_material(
        {
            "resource_template_uuid": device_template["uuid"],
            "barcode": "QUANTITY-DISPENSER-001",
            "name": "数量库存分液器",
        }
    )
    with inventory_store.transaction() as connection:
        connection.execute(
            "UPDATE material SET type='device' WHERE uuid=?",
            (device["uuid"],),
        )
    return str(device["uuid"])


def _runtime_input(
    *,
    graph: dict[str, Any],
    prepared: PreparedTaskInput,
    device_uuid: str,
) -> PreparedTaskInput:
    """构造可由本地 Edge 调度器执行的冻结输入。

    参数：``graph`` 是已应用工作流快照；``prepared`` 提供稳定 Job 身份；
    ``device_uuid`` 是动作绑定设备。返回：包含完整设备动作合同的冻结任务输入。
    异常：输入不满足 ``PreparedTaskInput`` 合同时由其构造器原样传播。
    """

    return PreparedTaskInput(
        workflow_snapshot=graph,
        resolved_input={},
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": [
                {
                    "uuid": NODE_UUID,
                    "topological_index": 0,
                    "kind": "device_action",
                    "device_id": "quantity-dispenser",
                    "device_selector": {},
                    "material_uuid": device_uuid,
                    "action_name": "dispense",
                    "action_type": "UniLabJsonCommand",
                    "param": {},
                    "param_schema": {
                        "type": "object",
                        "properties": {
                            "goal": {
                                "type": "object",
                                "properties": {},
                            }
                        },
                        "required": ["goal"],
                    },
                    "execution_policy": {},
                    "action_resource_contract": {},
                    "inputs": [],
                    "source_handle_uuids": [],
                    "material_requirements": [],
                }
            ],
            "handles": [],
            "edges": [],
        },
        jobs=prepared.jobs,
    )


def test_local_edge_outcome_replay_settles_quantity_once_with_in_memory_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """双进程结果投影失败后应从 Edge 发件箱幂等重放完整结算。

    参数：``tmp_path`` 隔离四个 SQLite；``monkeypatch`` 只在首次结果投影注入
    工作流库故障。返回无；断言 LocalEdgeControlAuthority、EdgeScheduler 与
    TaskSchedulerBridge 的真实链路先保留 pending outcome，恢复后把 Task/Job
    推进成功、确认 outcome，并且数量库存只扣减和记账一次。异常：首次投影的
    注入故障必须向提交方传播，回放不应再抛出异常。
    """

    inventory_store, _bottle_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    device_uuid = _device_material(inventory_store)
    definition_store, graph, prepared = _workflow(
        tmp_path / "definition-catalog-integration.db",
        reagent_info_uuid=info_uuid,
        material_uuid=device_uuid,
    )
    runtime_prepared = _runtime_input(
        graph=graph,
        prepared=prepared,
        device_uuid=device_uuid,
    )
    workflow_store = WorkflowStore(
        tmp_path / "workflow_history-integration.db",
        persist_workflow_definitions=False,
    )
    authority = LocalEdgeControlAuthority(
        LocalEdgeAuthorityStore(tmp_path / "edge_authority.db"),
        api_key="managed-local-secret",
    )
    registration = authority.store.register_session(
        {
            "edge_key": "quantity-edge",
            "instance_uuid": str(uuid4()),
            "devices": [
                {
                    "local_id": "quantity-dispenser",
                    "material_uuid": device_uuid,
                    "actions": [{"name": "dispense", "type": "command"}],
                    "online": True,
                    "unknown_command_ids": [],
                }
            ],
        }
    )
    authority.store.set_session_connected(registration["session_uuid"], True)
    scheduler, execution_backend = create_edge_stack(
        execution_backend=authority,
        inventory=InventoryService(inventory_store),
    )
    assert execution_backend is authority
    bridge = TaskSchedulerBridge(workflow_store, scheduler=scheduler)
    try:
        task = workflow_store.create_task_with_jobs(
            workflow_uuid=WORKFLOW_UUID,
            task_uuid=TASK_UUID,
            run_mode="normal",
            target_node_uuid=None,
            description=None,
            meta_data={},
            plan_builder=lambda _graph: runtime_prepared,
            inventory_allocation_builder=lambda connection, _graph, frozen: (
                bridge.prepare_inventory_allocations(
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
        bridge.submit(task)
        assert workflow_store.count_rows("workflow") == 0
        assert workflow_store.get_job(JOB_UUID)["status"] == "dispatched"

        command = authority.store.pending_commands()[0]
        command_payload = command["payload"]
        # 命令入队不是设备已开始；先通过真实执行开始协议，再验证数量结算重放。
        application = FastAPI()
        application.include_router(create_local_edge_control_router(authority))
        with TestClient(application) as client:
            started = client.post(
                f"/api/v1/edge/jobs/{JOB_UUID}/begin",
                headers={
                    "X-Command-UUID": command["message_uuid"],
                    "X-Job-Token": command_payload["job_access_token"],
                },
            )
            assert started.status_code == 200, started.text
            assert started.json()["data"]["start"] == "START"
        assert workflow_store.get_job(JOB_UUID)["status"] == "running"
        original_project_job_finished = bridge._projection.project_job_finished

        def fail_first_projection(**_values: Any) -> NoReturn:
            """模拟工作流终态库故障。

            参数：忽略投影器传入的所有终态字段。返回：不会返回。
            异常：始终抛出 ``RuntimeError``，模拟消费已提交后的暂时不可写。
            """

            raise RuntimeError("workflow projection unavailable")

        monkeypatch.setattr(
            bridge._projection,
            "project_job_finished",
            fail_first_projection,
        )
        outcome = {
            "job_uuid": JOB_UUID,
            "task_uuid": TASK_UUID,
            "node_uuid": NODE_UUID,
            "command_uuid": command["message_uuid"],
            "claim_uuid": command_payload["claim_uuid"],
            "attempt": command_payload["attempt"],
            "fences": command_payload["fences"],
            "outcome": "succeeded",
            "return_info": {"return_value": {"dispensed": True}},
            "error_info": [],
            "unknown_command_ids": [],
            "inventory_consumptions": [
                {
                    "inventory_type": "reagent",
                    "inventory_uuid": reagent_uuid,
                    "actual_quantity": 1.5,
                    "quantity_unit": "mL",
                }
            ],
        }
        with pytest.raises(RuntimeError, match="workflow projection unavailable"):
            authority.commit_outcome(
                JOB_UUID,
                command_uuid=command["message_uuid"],
                job_token=command_payload["job_access_token"],
                idempotency_key="quantity-outcome",
                payload=outcome,
            )

        assert authority.store.job(JOB_UUID)["status"] == "outcome_pending"
        assert authority.store.is_outcome_projected(JOB_UUID) is False
        assert inventory_store.query_one(
            "SELECT quantity FROM reagent WHERE uuid=?", (reagent_uuid,)
        ) == {"quantity": 8.5}
        assert inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_ledger "
            "WHERE workflow_node_job_uuid=? "
            "AND subject_type IN ('reagent','current_substance')",
            (JOB_UUID,),
        ) == {"count": 1}

        monkeypatch.setattr(
            bridge._projection,
            "project_job_finished",
            original_project_job_finished,
        )
        assert authority.replay_pending_projections() == {
            "feedback": 0,
            "outcomes": 1,
        }

        assert workflow_store.get_task(TASK_UUID)["status"] == "succeeded"
        assert workflow_store.get_job(JOB_UUID)["status"] == "succeeded"
        assert authority.store.job(JOB_UUID)["status"] == "completed"
        assert authority.store.is_outcome_projected(JOB_UUID) is True
        assert authority.replay_pending_projections() == {
            "feedback": 0,
            "outcomes": 0,
        }
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
        bridge.close()
        authority.stop()
        workflow_store.close()
        definition_store.close()
        inventory_store.close()
