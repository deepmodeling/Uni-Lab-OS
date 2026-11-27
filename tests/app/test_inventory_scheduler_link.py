"""EdgeScheduler × InventoryService 原子衔接测试.

覆盖测试门槛：
- submit 预留不足 → waiting_for_material，不进入执行队列；补料后自动恢复
- 节点明确成功后预留转消费；失败、跳过和取消不扣减，终态 release
- cancel/restart 依据 DB reservation 状态恢复，不依赖内存
- 旧 workflow 无物料字段：不产生任何 inventory 调用，行为完全不变
"""

import json
import threading
import time
import traceback
from types import SimpleNamespace

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.models import (
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    node_from_dict,
    spec_from_dict,
)
from unilabos.app.scheduler.monitor import MonitorBus
from unilabos.app.scheduler.service import EdgeScheduler


def _node(node_id, device="dev1", action="run", materials=None):
    return WorkflowNode(
        id=node_id, device_id=device, action_name=action, action_type="goal",
        param={}, material_requirements=materials or [],
    )


def _edge(src, dst):
    return WorkflowEdge(uuid=f"{src}->{dst}", source_node_id=src, target_node_id=dst)


def _req(lot="", qty=0.0, instance=""):
    return MaterialRequirement(lot_id=lot, quantity=qty, instance_uuid=instance)


def _stack(stock=100.0, *, monitor=None):
    svc = InventoryService(InventoryStore(":memory:"), monitor=monitor)
    if stock > 0:
        svc.inbound_lot("tpl-w", stock, lot_id="lot-1")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=svc, monitor=monitor)
    return scheduler, dispatcher, svc


def _call_with_timeout(function, timeout=3.0):
    """在子线程跑调度调用，超时视为死锁。"""

    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["result"] = function()
        except Exception:
            box["error"] = traceback.format_exc()

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout)
    assert not worker.is_alive(), "调度调用超时，可能再次死锁"
    if "error" in box:
        raise AssertionError(box["error"])
    return box["result"]


def _wait_state(scheduler, workflow_id, state, timeout=2.0):
    """等待物料唤醒把工作流推到目标状态。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = scheduler.workflow_snapshot(workflow_id)
        if snapshot and snapshot["state"] == state:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"workflow {workflow_id} did not reach {state}")


class TestSubmitReserve:
    def test_submit_reserves_whole_dag(self):
        scheduler, dispatcher, svc = _stack(stock=100.0)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", materials=[_req(lot="lot-1", qty=20.0)]),
                _node("B", device="dev2", materials=[_req(lot="lot-1", qty=30.0)]),
            ],
            edges=[_edge("A", "B")],
        )
        result = scheduler.submit_workflow(spec)
        assert result["state"] == "running"
        lot = svc.store.get_lot("lot-1")
        # 整 DAG（A+B）在入队时一次性预留
        assert lot["quantity_reserved"] == 50.0  # A、B 均只预留，尚未结算
        assert lot["quantity_total"] == 100.0
        assert len(dispatcher.dispatched) == 1   # A 已下发

    def test_insufficient_waits_not_queued(self):
        scheduler, dispatcher, svc = _stack(stock=10.0)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[_node("A", materials=[_req(lot="lot-1", qty=50.0)])],
        )
        result = scheduler.submit_workflow(spec)
        assert result["state"] == "waiting_for_material"
        assert dispatcher.dispatched == []  # 不进入执行队列
        assert svc.store.get_lot("lot-1")["quantity_reserved"] == 0.0

    def test_resumes_after_inbound(self):
        """补料后下一次重排自动恢复 RUNNING 并下发."""
        scheduler, dispatcher, svc = _stack(stock=10.0)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[_node("A", materials=[_req(lot="lot-1", qty=50.0)])],
        )
        scheduler.submit_workflow(spec)
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")  # 补料
        scheduler.reschedule()  # 任何触发点都会重试预留
        snap = scheduler.workflow_snapshot("wf1")
        assert snap["state"] == "running"
        assert len(dispatcher.dispatched) == 1

    @pytest.mark.parametrize("recovering_station", [False, True])
    def test_serial_recovery_waits_for_its_turn_before_reserving(
        self, recovering_station: bool
    ) -> None:
        inventory = InventoryService(InventoryStore(":memory:"))
        inventory.inbound_lot("tpl-w", 1.0, lot_id="lot-1")
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory, serial_queue=True)
        head = WorkflowSpec(
            workflow_id="head",
            nodes=[_node("head-node", materials=[_req(lot="lot-1", qty=2.0)])],
        )
        later = WorkflowSpec(
            workflow_id="later",
            nodes=[_node("later-node", materials=[_req(lot="lot-1", qty=1.0)])],
        )
        try:
            if recovering_station:
                scheduler.bind_station_control(
                    SimpleNamespace(recovering=True, allows_task=lambda _: False)
                )
            head_state = "pending" if recovering_station else "waiting_for_material"
            assert scheduler.restore_workflow(head, {})["state"] == head_state
            restored_later = scheduler.restore_workflow(
                later,
                {},
                execution_facts=[{
                    "uuid": "later-job",
                    "workflow_node_uuid": "later-node",
                    "status": "pending",
                }],
            )
            assert restored_later["state"] == "pending"
            assert inventory.store.reservations_for_workflow("later") == []
            assert dispatcher.dispatched == []

            if recovering_station:
                scheduler.bind_station_control(None)
                assert scheduler.reschedule() == []
                assert scheduler.workflow_snapshot("head")["state"] == "waiting_for_material"
            inventory.inbound_lot("tpl-w", 1.0, lot_id="lot-1")
            head_job, = scheduler.reschedule()
            assert head_job["workflow_id"] == "head"
            assert inventory.store.reservations_for_workflow("later") == []

            scheduler.on_job_finished(head_job["job_id"], True)
            assert scheduler.workflow_snapshot("later")["state"] == "waiting_for_material"
            inventory.inbound_lot("tpl-w", 1.0, lot_id="lot-1")
            later_job, = scheduler.reschedule()
            assert later_job["workflow_id"] == "later"
            assert [job["node_id"] for job in dispatcher.dispatched] == ["head-node", "later-node"]
        finally:
            scheduler.close()

    def test_no_material_workflow_untouched(self):
        """旧 workflow（无物料字段）：不产生任何 inventory 调用."""

        class ExplodingInventory:
            def __getattr__(self, name):
                raise AssertionError(f"inventory.{name} must not be called")

        scheduler = EdgeScheduler(
            dispatcher=RecordingDispatcher(), inventory=ExplodingInventory()
        )
        spec = WorkflowSpec(workflow_id="wf-old", nodes=[_node("A"), _node("B")],
                            edges=[_edge("A", "B")])
        result = scheduler.submit_workflow(spec)
        assert result["state"] == "running"
        assert len(result["dispatched"]) == 1


class TestSharedSourceResolution:
    """共享物料来源只读解析合同。"""

    def test_shared_source_does_not_create_inventory_reservation(self):
        """两个任务可解析同一共享实例且不会创建独占预留。

        参数：无。返回无；断言实例保持 warehouse，库存预留表为空，两次解析都
        返回同一稳定物料身份。
        """

        service = InventoryService(InventoryStore(":memory:"))
        service.register_instance(edge_uuid="mi-shared", template_id="tpl-shared")
        requirements = {"source": [_req(instance="mi-shared")]}

        first = service.resolve_shared_workflow_materials("task-a", requirements)
        second = service.resolve_shared_workflow_materials("task-b", requirements)

        assert first["allocations"] == {"source": ["mi-shared"]}
        assert second["allocations"] == first["allocations"]
        assert service.store.get_instance("mi-shared")["status"] == "warehouse"
        assert service.store.list_reservations() == []


class TestNodeLifecycle:
    def test_consume_only_after_successful_outcome(self):
        scheduler, dispatcher, svc = _stack(stock=100.0)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", materials=[_req(lot="lot-1", qty=20.0)]),
                _node("B", device="dev2", materials=[_req(lot="lot-1", qty=30.0)]),
            ],
            edges=[_edge("A", "B")],
        )
        scheduler.submit_workflow(spec)
        assert svc.store.get_reservation("wf1", "A", 1)["status"] == "active"
        assert svc.store.get_reservation("wf1", "B", 1)["status"] == "active"

        job_a = dispatcher.dispatched[0]["job_id"]
        scheduler.on_job_finished(job_a, success=True, ret_value={"ok": 1})
        assert svc.store.get_reservation("wf1", "A", 1)["status"] == "consumed"
        # B 已下发但尚无明确成功结果，因此仍只保留预留。
        assert svc.store.get_reservation("wf1", "B", 1)["status"] == "active"
        job_b = dispatcher.dispatched[1]["job_id"]
        scheduler.on_job_finished(job_b, success=True)
        assert svc.store.get_reservation("wf1", "B", 1)["status"] == "consumed"

        assert scheduler.workflow_snapshot("wf1")["state"] == "success"
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_total"] == 50.0
        assert lot["quantity_reserved"] == 0.0

    def test_success_result_replay_never_double_consumes_inventory(self):
        """工作流结果投影瞬态失败后，重放同一成功结果不重复扣减库存。"""

        scheduler, dispatcher, service = _stack(stock=100.0)
        remaining_failures = 1

        def fail_first_projection(
            _job_id: str,
            _success: bool,
            _ret_value: object,
            _suc_type: str,
        ) -> None:
            """首次模拟工作流库提交失败，第二次接受同一完成事实。"""

            nonlocal remaining_failures
            if remaining_failures:
                remaining_failures -= 1
                raise RuntimeError("workflow result commit failed")

        scheduler.add_job_finished_listener(fail_first_projection)
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-replay",
                nodes=[_node("A", materials=[_req(lot="lot-1", qty=20.0)])],
            )
        )
        job_id = dispatcher.dispatched[0]["job_id"]

        try:
            scheduler.on_job_finished(job_id, success=True, ret_value={"ok": True})
        except RuntimeError as error:
            assert str(error) == "workflow result commit failed"
        else:
            raise AssertionError("首次完成投影必须失败")

        assert service.store.get_reservation("wf-replay", "A", 1)["status"] == "consumed"
        assert service.store.get_lot("lot-1")["quantity_total"] == 80.0
        assert job_id in scheduler.snapshot()["inflight_jobs"]

        scheduler.on_job_finished(job_id, success=True, ret_value={"ok": True})

        assert service.store.get_lot("lot-1")["quantity_total"] == 80.0
        assert scheduler.snapshot()["inflight_jobs"] == {}

    def test_failure_releases_all_unconsumed_reservations(self):
        """明确失败不扣库存，终态释放该任务全部未消费预留。"""
        scheduler, dispatcher, svc = _stack(stock=100.0)
        svc.register_instance(edge_uuid="mi-1")
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", materials=[_req(lot="lot-1", qty=20.0), _req(instance="mi-1")]),
                _node("B", device="dev2", materials=[_req(lot="lot-1", qty=30.0)]),
            ],
            edges=[_edge("A", "B")],
        )
        scheduler.submit_workflow(spec)
        job_a = dispatcher.dispatched[0]["job_id"]
        scheduler.on_job_finished(job_a, success=False)

        assert svc.store.get_reservation("wf1", "A", 1)["status"] == "released"
        assert svc.store.get_instance("mi-1")["status"] == "warehouse"
        assert svc.store.get_reservation("wf1", "B", 1)["status"] == "released"
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_total"] == 100.0
        assert lot["quantity_reserved"] == 0.0
        assert lot["quantity_available"] == 100.0

    def test_cancel_releases_active_reservations(self):
        scheduler, _dispatcher, svc = _stack(stock=100.0)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", materials=[_req(lot="lot-1", qty=20.0)]),
                _node("B", device="dev2", materials=[_req(lot="lot-1", qty=30.0)]),
            ],
            edges=[_edge("A", "B")],
        )
        scheduler.submit_workflow(spec)
        scheduler.cancel_workflow("wf1")
        # 尚无明确成功结果，A、B 的 active 预留均释放且不扣库存。
        assert svc.store.get_reservation("wf1", "A", 1)["status"] == "released"
        assert svc.store.get_reservation("wf1", "B", 1)["status"] == "released"
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_reserved"] == 0.0
        assert lot["quantity_available"] == 100.0

    def test_restart_recovers_from_db_not_memory(self):
        """restart：换一个全新 scheduler（内存清空），仅凭 DB 状态恢复.

        cancel 后 attempt=1 的 released 预留不会阻碍 attempt=2 重新预留。
        """
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")

        sched1 = EdgeScheduler(dispatcher=RecordingDispatcher(), inventory=svc)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", materials=[_req(lot="lot-1", qty=20.0)]),
                _node("B", device="dev2", materials=[_req(lot="lot-1", qty=30.0)]),
            ],
            edges=[_edge("A", "B")],
        )
        sched1.submit_workflow(spec)
        sched1.cancel_workflow("wf1")
        # 模拟进程重启：全新 scheduler，凭 DB 重新预留（attempt=2 是新幂等键）
        svc.reserve_workflow(
            "wf1", {"B": [_req(lot="lot-1", qty=30.0)]}, attempt=2
        )
        assert svc.store.get_reservation("wf1", "B", 2)["status"] == "active"
        lot = svc.store.get_lot("lot-1")
        # attempt=1 未收到成功结果，因此未扣减；attempt=2 只预留 B 的 30。
        assert lot["quantity_total"] == 100.0
        assert lot["quantity_reserved"] == 30.0


class TestSpecSerialization:
    """物料字段向后兼容 schema：解析/序列化."""

    def test_spec_without_materials_parses(self):
        spec = spec_from_dict({
            "workflow_id": "wf1",
            "nodes": [{"id": "A", "device_id": "d1", "action_name": "run"}],
        })
        assert spec.nodes[0].material_requirements == []
        assert spec.material_requirements_by_node() == {}

    def test_spec_with_materials_parses(self):
        node = node_from_dict({
            "id": "A", "device_id": "d1", "action_name": "run",
            "material_requirements": [
                {"lot_id": "lot-1", "quantity": 5, "unit": "mL"},
                {"instance_uuid": "mi-1"},
                {"barcode": "BC-2"},
            ],
        })
        assert len(node.material_requirements) == 3
        assert node.material_requirements[0].quantity == 5.0
        assert node.material_requirements[1].is_instance_requirement()
        assert node.material_requirements[2].barcode == "BC-2"

    def test_requirement_roundtrip(self):
        req = MaterialRequirement(lot_id="lot-1", quantity=3.5, unit="mL")
        assert MaterialRequirement.from_dict(req.to_dict()) == req

    def test_requirement_roundtrip_via_json(self):
        req = MaterialRequirement(template_id="tpl", quantity=2.0, barcode="BC")
        parsed = MaterialRequirement.from_dict(json.loads(json.dumps(req.to_dict())))
        assert parsed == req

    def test_disabled_node_materials_excluded(self):
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", materials=[_req(lot="lot-1", qty=5.0)]),
                WorkflowNode(id="B", disabled=True,
                             material_requirements=[_req(lot="lot-1", qty=99.0)]),
            ],
        )
        assert list(spec.material_requirements_by_node().keys()) == ["A"]


class TestMaterialMonitorWakeup:
    def test_submit_returns_dispatched_jobs_when_reserve_emits(self):
        """预留事件不得抢走 submit 等待的那一轮派发结果。"""

        bus = MonitorBus()
        scheduler, dispatcher, _svc = _stack(monitor=bus)
        spec = WorkflowSpec(
            workflow_id="wf-monitor-submit",
            nodes=[
                _node("A", materials=[_req(lot="lot-1", qty=20.0)]),
                _node("B", device="dev2", materials=[_req(lot="lot-1", qty=10.0)]),
            ],
            edges=[_edge("A", "B")],
        )
        result = _call_with_timeout(lambda: scheduler.submit_workflow(spec))
        assert result["state"] == "running"
        assert [item["node_id"] for item in result["dispatched"]] == ["A"]
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A"]

    def test_finish_returns_next_dispatch_when_consume_emits(self):
        """结算事件不得让完成回调等到空的下一轮重排。"""

        bus = MonitorBus()
        scheduler, dispatcher, _svc = _stack(monitor=bus)
        spec = WorkflowSpec(
            workflow_id="wf-monitor-finish",
            nodes=[
                _node("A", materials=[_req(lot="lot-1", qty=20.0)]),
                _node("B", device="dev2", materials=[_req(lot="lot-1", qty=10.0)]),
            ],
            edges=[_edge("A", "B")],
        )
        submitted = _call_with_timeout(lambda: scheduler.submit_workflow(spec))
        job_id = submitted["dispatched"][0]["job_id"]
        finished = _call_with_timeout(
            lambda: scheduler.on_job_finished(job_id, True, {})
        )
        assert [item["node_id"] for item in finished["dispatched"]] == ["B"]
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A", "B"]

    def test_inbound_resumes_waiting_workflow_without_manual_reschedule(self):
        """补料后物料监听必须自动恢复等待中的工作流。"""

        bus = MonitorBus()
        scheduler, dispatcher, svc = _stack(stock=10.0, monitor=bus)
        spec = WorkflowSpec(
            workflow_id="wf-monitor-wait",
            nodes=[_node("A", materials=[_req(lot="lot-1", qty=50.0)])],
        )
        result = _call_with_timeout(lambda: scheduler.submit_workflow(spec))
        assert result["state"] == "waiting_for_material"
        assert dispatcher.dispatched == []
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        snapshot = _wait_state(scheduler, "wf-monitor-wait", "running")
        assert snapshot["state"] == "running"
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A"]

    def test_wake_while_holding_lock_after_emit_does_not_hang(self):
        """持锁发布物料事件后再等待重排，不得与重排线程互相等待。"""

        bus = MonitorBus()
        scheduler, _dispatcher, _svc = _stack(monitor=bus)

        def probe():
            with scheduler._lock:
                bus.emit("material", "lot.inbound", {"lot_id": "lot-1"})
                return scheduler._wake_reconcile()

        assert _call_with_timeout(probe, timeout=2.0) == []

    def test_submit_does_not_join_stale_empty_reconcile(self):
        """无物料提交不得并进已经查过空队列的在途重排。

        生产里物料总线先唤醒一轮空重排后，用户再点运行；若 submit 并进该轮
        且不标脏，登记后的工作流不会再被派发，看起来像整条流程不通。
        """

        bus = MonitorBus()
        scheduler, dispatcher, _svc = _stack(monitor=bus)
        spec = WorkflowSpec(
            workflow_id="wf-stale-submit",
            nodes=[_node("A")],
        )
        entered = threading.Event()
        release = threading.Event()
        rounds = {"count": 0}
        original_reschedule = scheduler.reschedule

        def delaying_reschedule() -> list[dict[str, object]]:
            dispatched = original_reschedule()
            rounds["count"] += 1
            if rounds["count"] == 1:
                entered.set()
                assert release.wait(timeout=2)
            return dispatched

        scheduler.reschedule = delaying_reschedule  # type: ignore[method-assign]
        bus.emit("material", "lot.inbound", {"lot_id": "lot-1"})
        assert entered.wait(timeout=2)

        def release_after_submit_joins() -> None:
            time.sleep(0.05)
            release.set()

        threading.Thread(target=release_after_submit_joins, daemon=True).start()
        result = _call_with_timeout(lambda: scheduler.submit_workflow(spec), timeout=3.0)
        assert result["state"] == "running"
        assert [item["node_id"] for item in result["dispatched"]] == ["A"]
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A"]

    def test_inbound_during_in_flight_reconcile_is_not_dropped(self):
        """重排已经做过等料检查后到达的补料，必须再跑一轮。"""

        bus = MonitorBus()
        scheduler, dispatcher, svc = _stack(stock=10.0, monitor=bus)
        spec = WorkflowSpec(
            workflow_id="wf-monitor-dirty",
            nodes=[_node("A", materials=[_req(lot="lot-1", qty=50.0)])],
        )
        result = _call_with_timeout(lambda: scheduler.submit_workflow(spec))
        assert result["state"] == "waiting_for_material"

        entered = threading.Event()
        release = threading.Event()
        rounds = {"count": 0}
        original_impl = scheduler._reschedule_impl

        def delaying_impl() -> list[dict[str, object]]:
            dispatched = original_impl()
            rounds["count"] += 1
            if rounds["count"] == 1:
                entered.set()
                assert release.wait(timeout=2)
            return dispatched

        scheduler._reschedule_impl = delaying_impl  # type: ignore[method-assign]
        bus.emit("material", "lot.inbound", {"lot_id": "lot-1"})
        assert entered.wait(timeout=2)
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        release.set()
        snapshot = _wait_state(scheduler, "wf-monitor-dirty", "running")
        assert snapshot["state"] == "running"
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A"]
