"""数量结算的浮点边界、持久失败隔离与人工纠正合同。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Iterator
from uuid import uuid4

import pytest

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.workflow_quantity import (
    WorkflowQuantityReservationError,
    subtract_quantity,
)
from unilabos.workflow.models import WorkflowInventoryRequirementWrite, WorkflowNodeWrite
from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput

from .quantity_inventory_test_support import TASK_UUID, WORKFLOW_UUID, _inventory


@contextmanager
def _scenario(
    tmp_path: Path, quantities: tuple[float, ...] = (2.0,), *, available: float = 10.0,
    split: bool = False,
) -> Iterator[dict]:
    """通过公共创建接口建立一瓶试剂、一个任务及指定数量的消费作业。"""

    inventory, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow = WorkflowStore(tmp_path / "workflow.db")
    try:
        if available != 10.0:
            BackendReagentService(inventory).update_reagent(
                reagent_uuid, {"quantity": available, "quantity_unit": "mL"}
            )
        reagent_uuids = [reagent_uuid]
        if split:
            resources = BackendResourceService(inventory)
            other_material = resources.create_material({
                "resource_template_uuid": resources.get_material(material_uuid)["resource_template_uuid"],
                "name": "同批次第二瓶", "barcode": "WF-ETHANOL-002",
            })
            reagent_uuids.append(BackendReagentService(inventory).create_reagent({
                "material_uuid": other_material["uuid"], "reagent_info_uuid": info_uuid,
                "quantity": available, "quantity_unit": "mL", "meta_data": {},
            })["uuid"])
        workflow.create_workflow(
            workflow_uuid=WORKFLOW_UUID, name="数量对账", tags=[], description=None, meta_data={}
        )
        nodes = [str(uuid4()) for _ in quantities]
        jobs = [str(uuid4()) for _ in quantities]
        graph = workflow.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=[
                WorkflowNodeWrite(
                    uuid=node, name="分液", type="device_action", action_name="dispense",
                    action_type="UniLabJsonCommand", param={}, material_uuid=material_uuid,
                )
                for node in nodes
            ],
            edges=[],
            inventory_requirements=[
                WorkflowInventoryRequirementWrite(
                    consume_node_uuid=node, requirement_key=f"reagent-{index}",
                    target_type="reagent_info", reagent_info_uuid=info_uuid,
                    required_quantity=quantity, quantity_unit="mL",
                    allow_split=split,
                )
                for index, (node, quantity) in enumerate(zip(nodes, quantities))
            ],
        )
        prepared = PreparedTaskInput(
            workflow_snapshot=graph,
            resolved_input={},
            execution_plan={
                "version": 1, "run_mode": "normal", "target_node_uuid": None,
                "nodes": [{"uuid": node} for node in nodes], "handles": [], "edges": [],
            },
            jobs=[
                {
                    "uuid": job, "workflow_node_uuid": node, "topological_index": index,
                    "executor_kind": "device_action", "execution_policy": {},
                    "execution_timeout_seconds": 60, "param": {},
                }
                for index, (node, job) in enumerate(zip(nodes, jobs))
            ],
        )
        coordinator = WorkflowQuantityInventory(workflow, InventoryService(inventory))
        bindings = [
            {
                "requirement_key": f"reagent-{index}", "inventory_type": "reagent",
                "inventory_uuid": instance, "reserved_quantity": quantity / len(reagent_uuids),
                "quantity_unit": "mL",
            }
            for index, quantity in enumerate(quantities)
            for instance in reagent_uuids
        ]
        scenario = {
            "inventory": inventory, "workflow": workflow, "coordinator": coordinator,
            "prepared": prepared, "bindings": bindings, "jobs": jobs,
            "reagent_uuid": reagent_uuid,
            "reagent_uuids": reagent_uuids,
        }
        _create_task(scenario, TASK_UUID, prepared)
        yield scenario
    finally:
        workflow.close()
        inventory.close()


def _create_task(scenario: dict, task_uuid: str, prepared: PreparedTaskInput) -> None:
    """按正常任务创建事务冻结分配和库存预留。"""

    scenario["workflow"].create_task_with_jobs(
        workflow_uuid=WORKFLOW_UUID, task_uuid=task_uuid, run_mode="normal",
        target_node_uuid=None, description=None, meta_data={}, plan_builder=lambda _: prepared,
        inventory_allocation_builder=lambda connection, graph, frozen: (
            scenario["coordinator"].prepare_task_allocations(
                connection, graph=graph, prepared=frozen, task_uuid=task_uuid,
                bindings=scenario["bindings"],
            )
        ),
    )


def _reported(scenario: dict, quantity: float, unit: str = "mL") -> list[dict]:
    """构造一个实例的 Edge 实际用量回执。"""

    return [{
        "inventory_type": "reagent", "inventory_uuid": scenario["reagent_uuid"],
        "actual_quantity": quantity, "quantity_unit": unit,
    }]


def _saga(workflow: WorkflowStore, task_uuid: str = TASK_UUID) -> dict:
    """读取隔离测试任务的持久 Saga 证据。"""

    with workflow.transaction() as connection:
        return dict(connection.execute(
            "SELECT * FROM workflow_inventory_saga WHERE workflow_task_uuid=?", (task_uuid,)
        ).fetchone())


def test_decimal_exhaustion_and_reopened_replay_preserve_zero_balance(tmp_path: Path) -> None:
    """0.3 mL 分两次耗尽后可重新打开数据库恢复，台账只出现两次实际消费。"""

    with _scenario(tmp_path, (.1, .2), available=.3) as scenario:
        coordinator = scenario["coordinator"]
        for job in scenario["jobs"]:
            coordinator.consume_successful_job(task_uuid=TASK_UUID, job_uuid=job, consumptions=[])
        assert scenario["inventory"].query_one(
            "SELECT quantity FROM reagent WHERE uuid=?", (scenario["reagent_uuid"],)
        ) == {"quantity": 0.0}
        assert _saga(scenario["workflow"])["status"] == "settled"
    with closing(InventoryStore(str(tmp_path / "inventory.db"))) as inventory:
        with closing(WorkflowStore(tmp_path / "workflow.db")) as workflow:
            coordinator = WorkflowQuantityInventory(workflow, InventoryService(inventory))
            assert coordinator.recover_pending() == {}
            assert len(coordinator.list_task_consumptions(TASK_UUID)) == 2
            assert inventory.query_one("SELECT quantity FROM reagent") == {"quantity": 0.0}


@pytest.mark.parametrize("available,consumed", [(0.0, 1e-15), (1e-12, 2e-12), (1000, 1000.00001)])
def test_rounding_tolerance_does_not_allow_real_overconsumption(
    available: float, consumed: float
) -> None:
    """单位缩放后的真实超量不能被固定绝对容差吞掉。"""

    with pytest.raises(WorkflowQuantityReservationError, match="余量不足"):
        subtract_quantity(available, consumed)


def test_unit_mismatch_retains_reservation_and_replay_cannot_replace_intent(tmp_path: Path) -> None:
    """单位不匹配不落消费账、不释放预留，普通重放不能偷偷替换首次上报。"""

    with _scenario(tmp_path) as scenario:
        coordinator = scenario["coordinator"]
        job = scenario["jobs"][0]
        for consumptions in (_reported(scenario, 1.5, "g"), _reported(scenario, 1.5)):
            with pytest.raises(WorkflowQuantityReservationError, match="无法安全换算"):
                coordinator.consume_successful_job(
                    task_uuid=TASK_UUID, job_uuid=job, consumptions=consumptions
                )
        assert coordinator.list_task_consumptions(TASK_UUID) == []
        assert scenario["inventory"].query_one("SELECT quantity FROM reagent") == {"quantity": 10.0}
        assert scenario["inventory"].query_one("SELECT status FROM inventory_reservation") == {
            "status": "active"
        }
        pending = coordinator.pending_consumption_reconciliations()
        assert pending[0]["job_uuid"] == job
        assert "无法安全换算" in pending[0]["reason"]
        before = _saga(scenario["workflow"])
        with pytest.raises(StoreConflict, match="必须完成对账"):
            coordinator.release_task(TASK_UUID, reason="workflow_failed_recovery")
        assert _saga(scenario["workflow"])["payload"] == before["payload"]
        assert _saga(scenario["workflow"])["status"] == "consume_pending"


def test_failed_intent_cannot_be_overwritten_by_another_job(tmp_path: Path) -> None:
    """同任务第二个作业完成时必须保留第一个作业尚待对账的意图。"""

    with _scenario(tmp_path, (2.0, 2.0)) as scenario:
        coordinator = scenario["coordinator"]
        with pytest.raises(WorkflowQuantityReservationError):
            coordinator.consume_successful_job(
                task_uuid=TASK_UUID, job_uuid=scenario["jobs"][0],
                consumptions=_reported(scenario, 3),
            )
        before = _saga(scenario["workflow"])
        with pytest.raises(StoreConflict, match="不能结算其他作业"):
            coordinator.consume_successful_job(
                task_uuid=TASK_UUID, job_uuid=scenario["jobs"][1], consumptions=[]
            )
        assert _saga(scenario["workflow"])["payload"] == before["payload"]
        assert _saga(scenario["workflow"])["operation_key"] == before["operation_key"]


@pytest.mark.parametrize("corrupt_payload", [False, True])
def test_recovery_isolates_failed_task_and_replays_other_committed_consumption(
    tmp_path: Path, corrupt_payload: bool
) -> None:
    """一个永久失败的消费不会挡住另一任务已扣库存但尚未提交投影的恢复。"""

    with _scenario(tmp_path) as scenario:
        coordinator = scenario["coordinator"]
        with pytest.raises(WorkflowQuantityReservationError):
            coordinator.consume_successful_job(
                task_uuid=TASK_UUID, job_uuid=scenario["jobs"][0],
                consumptions=_reported(scenario, 3),
            )
        if corrupt_payload:
            payload = json.loads(_saga(scenario["workflow"])["payload"])
            del payload["records"][0]["event_uuid"]
            with scenario["workflow"].transaction() as connection:
                connection.execute(
                    "UPDATE workflow_inventory_saga SET payload=? WHERE workflow_task_uuid=?",
                    (json.dumps(payload), TASK_UUID),
                )
        other_task, other_job = str(uuid4()), str(uuid4())
        prepared = replace(
            scenario["prepared"], jobs=[{**scenario["prepared"].jobs[0], "uuid": other_job}]
        )
        _create_task(scenario, other_task, prepared)
        coordinator.consume_successful_job(task_uuid=other_task, job_uuid=other_job, consumptions=[])
        with scenario["workflow"].transaction() as connection:
            connection.execute(
                "UPDATE workflow_inventory_saga SET status='consume_pending' WHERE workflow_task_uuid=?",
                (other_task,),
            )
            connection.execute(
                "UPDATE workflow_inventory_allocation SET status='reserved',consumed_at=NULL "
                "WHERE workflow_task_uuid=?", (other_task,),
            )
        assert set(coordinator.recover_pending()) == {TASK_UUID}
        assert _saga(scenario["workflow"], other_task)["status"] == "settled"
        assert _saga(scenario["workflow"])["status"] == "consume_pending"
        assert scenario["inventory"].query_one("SELECT quantity FROM reagent") == {"quantity": 8.0}
        assert len(coordinator.list_task_consumptions(other_task)) == 1
        assert coordinator.pending_consumption_reconciliations()[0]["task_uuid"] == TASK_UUID


def test_manual_reconciliation_audits_correction_and_old_outcome_replay_is_idempotent(tmp_path: Path) -> None:
    """人工核对单位后仅扣实际量，保留纠正证据，迟到的原结果不能重复扣量。"""

    with _scenario(tmp_path) as scenario:
        coordinator = scenario["coordinator"]
        job = scenario["jobs"][0]
        with pytest.raises(WorkflowQuantityReservationError):
            coordinator.consume_successful_job(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1.5, "g")
            )
        facts = coordinator.reconcile_consumption(
            task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1500, "uL"),
            reason="核对泵回执，单位应为 µL，设备已停止",
        )
        assert facts[0]["actual_quantity"] == 1.5
        assert facts[0]["balance_after"] == 8.5
        assert facts[0]["meta_data"]["reconciliation"]["previous_reported_quantity_unit"] == "g"
        assert facts[0]["meta_data"]["reconciliation"]["request"] == {
            "consumptions": _reported(scenario, 1500, "uL"),
            "reason": "核对泵回执，单位应为 µL，设备已停止",
        }
        history = json.loads(_saga(scenario["workflow"])["payload"])["reconciliations"]
        assert history[0]["previous_records"][0]["actual_quantity"] == 1.5
        assert history[0]["corrected_records"][0]["actual_quantity"] == 1500
        assert history[0]["reason"] == "核对泵回执，单位应为 µL，设备已停止"
        assert coordinator.consume_successful_job(
            task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1.5, "g")
        ) == facts
        assert len(coordinator.list_task_consumptions(TASK_UUID)) == 1
        assert coordinator.pending_consumption_reconciliations() == []
        assert coordinator.reconcile_consumption(
            task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1500, "uL"),
            reason="核对泵回执，单位应为 µL，设备已停止",
        ) == facts
        assert len(json.loads(_saga(scenario["workflow"])["payload"])["reconciliations"]) == 1
        with pytest.raises(StoreConflict, match="消费已落账"):
            coordinator.reconcile_consumption(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1), reason="重复纠正"
            )


def test_partial_correction_records_complete_request_on_every_consumption(tmp_path: Path) -> None:
    """部分实例更正后，每条落账事实都证明同一完整请求，省略实例也不能丢证据。"""

    with _scenario(tmp_path, split=True) as scenario:
        coordinator = scenario["coordinator"]
        job = scenario["jobs"][0]
        with pytest.raises(WorkflowQuantityReservationError):
            coordinator.consume_successful_job(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, .5, "g")
            )
        corrections = _reported(scenario, .5)
        facts = coordinator.reconcile_consumption(
            task_uuid=TASK_UUID, job_uuid=job, consumptions=corrections, reason="  核对第一瓶单位  "
        )
        assert len(facts) == 2
        assert {fact["actual_quantity"] for fact in facts} == {.5, 1.0}
        assert all(fact["meta_data"]["reconciliation"]["request"] == {
            "consumptions": corrections, "reason": "核对第一瓶单位",
        } for fact in facts)
        expanded = [*corrections, {
            "inventory_type": "reagent", "inventory_uuid": scenario["reagent_uuids"][1],
            "actual_quantity": 1, "quantity_unit": "mL",
        }]
        with pytest.raises(StoreConflict, match="消费已落账"):
            coordinator.reconcile_consumption(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=expanded, reason="核对第一瓶单位"
            )


def test_restarted_correction_reuses_durable_request_after_inventory_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库存已落账但响应丢失时，重开两库只接受已记账的完整请求且不重复扣量。"""

    with _scenario(tmp_path) as scenario:
        coordinator = scenario["coordinator"]
        job = scenario["jobs"][0]
        corrections = _reported(scenario, 1.5)
        with pytest.raises(WorkflowQuantityReservationError):
            coordinator.consume_successful_job(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1.5, "g")
            )
        apply = coordinator._authority.apply_consumption

        def committed_then_response_lost(**kwargs):
            """保留真实库存提交，模拟工作流投影提交前进程失去结果。"""

            apply(**kwargs)
            raise RuntimeError("模拟提交后响应丢失")

        monkeypatch.setattr(coordinator._authority, "apply_consumption", committed_then_response_lost)
        with pytest.raises(RuntimeError, match="响应丢失"):
            coordinator.reconcile_consumption(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=corrections, reason="  核对为 mL  "
            )
        audit = json.loads(_saga(scenario["workflow"])["payload"])["records"][0]["reconciliation"]
    with closing(InventoryStore(str(tmp_path / "inventory.db"))) as inventory:
        with closing(WorkflowStore(tmp_path / "workflow.db")) as workflow:
            coordinator = WorkflowQuantityInventory(workflow, InventoryService(inventory))
            with pytest.raises(StoreConflict, match="消费已落账"):
                coordinator.reconcile_consumption(
                    task_uuid=TASK_UUID, job_uuid=job, consumptions=corrections, reason="不同原因"
                )
            facts = coordinator.reconcile_consumption(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=corrections, reason="核对为 mL"
            )
            assert len(facts) == 1
            assert facts[0]["meta_data"]["reconciliation"] == audit
            assert audit["request"] == {"consumptions": corrections, "reason": "核对为 mL"}
            assert inventory.query_one("SELECT quantity FROM reagent") == {"quantity": 8.5}
            assert len(json.loads(_saga(workflow)["payload"])["reconciliations"]) == 1


def test_reconciliation_cannot_change_already_committed_inventory_fact(tmp_path: Path) -> None:
    """库存已提交而工作流投影未提交时，只允许重放，禁止人工覆盖既有事实。"""

    with _scenario(tmp_path) as scenario:
        coordinator = scenario["coordinator"]
        job = scenario["jobs"][0]
        coordinator.consume_successful_job(task_uuid=TASK_UUID, job_uuid=job, consumptions=[])
        with scenario["workflow"].transaction() as connection:
            connection.execute("UPDATE workflow_inventory_saga SET status='consume_pending'")
            connection.execute("UPDATE workflow_inventory_allocation SET status='reserved',consumed_at=NULL")
        with pytest.raises(StoreConflict, match="消费已落账"):
            coordinator.reconcile_consumption(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1), reason="不能改账"
            )
        assert coordinator.recover_pending() == {}
        assert scenario["inventory"].query_one("SELECT quantity FROM reagent") == {"quantity": 8.0}


def test_manual_overrun_records_actual_use_without_taking_other_jobs_reservations(tmp_path: Path) -> None:
    """人工可如实结算超耗，但同任务其他作业的预留仍受到保护。"""

    with _scenario(tmp_path, (2.0, 7.0)) as scenario:
        coordinator = scenario["coordinator"]
        job = scenario["jobs"][0]
        with pytest.raises(WorkflowQuantityReservationError, match="预留量"):
            coordinator.consume_successful_job(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 3)
            )
        with pytest.raises(WorkflowQuantityReservationError, match="其他作业"):
            coordinator.reconcile_consumption(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 4),
                reason="核对实际用量",
            )
        assert scenario["inventory"].query_one("SELECT quantity FROM reagent") == {"quantity": 10.0}
        assert coordinator.list_job_consumptions(job) == []
        facts = coordinator.reconcile_consumption(
            task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 3),
            reason="再次核对实际用量为 3 mL",
        )
        assert facts[0]["actual_quantity"] == 3.0
        assert facts[0]["meta_data"]["overrun_quantity"] == 1.0
        assert facts[0]["meta_data"]["overrun_authority"] == "manual_reconciliation"
        assert scenario["inventory"].query_one("SELECT quantity FROM reagent") == {"quantity": 7.0}
        coordinator.consume_successful_job(
            task_uuid=TASK_UUID, job_uuid=scenario["jobs"][1], consumptions=[]
        )
        assert scenario["inventory"].query_one("SELECT quantity FROM reagent") == {"quantity": 0.0}


def test_invalid_receipt_is_retained_but_never_replayed_as_planned_consumption(tmp_path: Path) -> None:
    """重复上报等无效证据也冻结预留，人工明确实际值前不能按默认计划自动记账。"""

    with _scenario(tmp_path) as scenario:
        coordinator = scenario["coordinator"]
        job = scenario["jobs"][0]
        invalid = _reported(scenario, 1) * 2
        with pytest.raises(StoreConflict, match="重复上报"):
            coordinator.consume_successful_job(task_uuid=TASK_UUID, job_uuid=job, consumptions=invalid)
        payload = json.loads(_saga(scenario["workflow"])["payload"])
        assert payload["needs_correction"] is True
        assert payload["rejected_consumptions"] == invalid
        assert set(coordinator.recover_pending()) == {TASK_UUID}
        assert coordinator.list_job_consumptions(job) == []
        assert scenario["inventory"].query_one("SELECT quantity FROM reagent") == {"quantity": 10.0}
        with pytest.raises(StoreConflict, match="全部已预留"):
            coordinator.reconcile_consumption(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=[], reason="未提供实际值"
            )
        facts = coordinator.reconcile_consumption(
            task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1),
            reason="确认仅耗用 1 mL，原回执重复列出同一瓶",
        )
        assert facts[0]["balance_after"] == 9.0


def test_correction_waits_for_inflight_replay_before_replacing_pending_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """更正不能越过仍在库存事务边界内的旧结果重放，避免同一事件记录相互覆盖。"""

    with _scenario(tmp_path) as scenario:
        coordinator = scenario["coordinator"]
        job = scenario["jobs"][0]
        with pytest.raises(WorkflowQuantityReservationError):
            coordinator.consume_successful_job(
                task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1.5, "g")
            )
        first_entered, second_entered, release = Event(), Event(), Event()
        original = coordinator._authority.apply_consumption

        def delayed_application(**kwargs):
            """暂停库存应用边界，让两个真实协调器入口并发竞争。"""

            (second_entered if first_entered.is_set() else first_entered).set()
            assert release.wait(5), "库存结算测试屏障超时"
            return original(**kwargs)

        monkeypatch.setattr(coordinator._authority, "apply_consumption", delayed_application)
        with ThreadPoolExecutor(max_workers=2) as executor:
            replay = executor.submit(
                coordinator.consume_successful_job,
                task_uuid=TASK_UUID, job_uuid=job, consumptions=[],
            )
            assert first_entered.wait(5)
            correction = executor.submit(
                coordinator.reconcile_consumption,
                task_uuid=TASK_UUID, job_uuid=job, consumptions=_reported(scenario, 1.5),
                reason="核对为 1.5 mL",
            )
            try:
                assert not second_entered.wait(.1)
                assert "reconciliations" not in json.loads(_saga(scenario["workflow"])["payload"])
            finally:
                release.set()
            with pytest.raises(WorkflowQuantityReservationError, match="无法安全换算"):
                replay.result(timeout=5)
            assert correction.result(timeout=5)[0]["balance_after"] == 8.5
        assert _saga(scenario["workflow"])["last_error"] is None
        assert len(coordinator.list_job_consumptions(job)) == 1
