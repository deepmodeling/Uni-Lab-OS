"""EdgeScheduler × InventoryService 原子衔接测试.

覆盖测试门槛：
- submit 预留不足 → waiting_for_material，不进入执行队列；补料后自动恢复
- 节点开始（下发前）预留转消费；节点失败已用物料 quarantined、
  未消费预留在工作流终态时 release
- cancel/restart 依据 DB reservation 状态恢复，不依赖内存
- 旧 workflow 无物料字段：不产生任何 inventory 调用，行为完全不变
"""

import json

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.models import (
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    WorkflowState,
    node_from_dict,
    spec_from_dict,
)
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


def _stack(stock=100.0):
    svc = InventoryService(InventoryStore(":memory:"))
    if stock > 0:
        svc.inbound_lot("tpl-w", stock, lot_id="lot-1")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=svc)
    return scheduler, dispatcher, svc


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
        assert lot["quantity_reserved"] == 30.0  # B 还没开始，A 已消费
        assert lot["quantity_total"] == 80.0     # A 下发时消费了 20
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


class TestNodeLifecycle:
    def test_consume_on_dispatch_and_success_flow(self):
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
        assert svc.store.get_reservation("wf1", "A", 1)["status"] == "consumed"
        assert svc.store.get_reservation("wf1", "B", 1)["status"] == "active"

        job_a = dispatcher.dispatched[0]["job_id"]
        scheduler.on_job_finished(job_a, success=True, ret_value={"ok": 1})
        # B 下发时消费
        assert svc.store.get_reservation("wf1", "B", 1)["status"] == "consumed"
        job_b = dispatcher.dispatched[1]["job_id"]
        scheduler.on_job_finished(job_b, success=True)

        assert scheduler.workflow_snapshot("wf1")["state"] == "success"
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_total"] == 50.0
        assert lot["quantity_reserved"] == 0.0

    def test_failure_quarantines_used_releases_rest(self):
        """节点失败：已消费物料 quarantined；未开始节点的预留在终态时 release."""
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

        # A 已物理使用 → quarantined（lot 不虚假加回，实例进人工复核）
        assert svc.store.get_reservation("wf1", "A", 1)["status"] == "quarantined"
        assert svc.store.get_instance("mi-1")["status"] == "quarantined"
        # B 未开始 → 预留 release，数量回到 available
        assert svc.store.get_reservation("wf1", "B", 1)["status"] == "released"
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_total"] == 80.0     # A 消费的 20 不加回
        assert lot["quantity_reserved"] == 0.0
        assert lot["quantity_available"] == 80.0

    def test_cancel_releases_active_reservations(self):
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
        scheduler.cancel_workflow("wf1")
        # A 已消费不回滚；B 的 active 预留释放
        assert svc.store.get_reservation("wf1", "B", 1)["status"] == "released"
        lot = svc.store.get_lot("lot-1")
        assert lot["quantity_reserved"] == 0.0
        assert lot["quantity_available"] == 80.0

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
        # A(20) 已消费，B attempt=2 预留 30
        assert lot["quantity_total"] == 80.0
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


class TestSchemeThreeReplenishment:
    def test_replenishment_is_triggered_at_second_action_not_workflow_submit(self):
        scheduler, dispatcher, svc = _stack(stock=1.0)
        factory_calls = []

        def build_replenishment(run, requirements):
            factory_calls.append(run.run_id)
            return WorkflowSpec(
                workflow_id="wf-two-liquid-replenish",
                nodes=[_node("R", device="replenisher", action="replace_tip_box")],
            )

        scheduler = EdgeScheduler(
            dispatcher=dispatcher,
            inventory=svc,
            material_replenishment_factory=build_replenishment,
        )
        spec = WorkflowSpec(
            workflow_id="wf-two-liquid",
            nodes=[
                _node("liquid-a", materials=[_req(lot="lot-1", qty=1.0)]),
                _node("liquid-b", device="dev2", materials=[_req(lot="lot-1", qty=1.0)]),
            ],
            edges=[_edge("liquid-a", "liquid-b")],
        )

        result = scheduler.submit_workflow(spec)
        assert result["state"] == "running"
        assert factory_calls == []
        assert dispatcher.dispatched[0]["node_id"] == "liquid-a"

        # 第一段加液真实消耗现有唯一 TIP；第二段 claim 前才发现缺 1 支。
        scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], success=True)
        assert factory_calls == ["wf-two-liquid"]
        assert dispatcher.dispatched[1]["workflow_id"] == "wf-two-liquid-replenish"
        assert dispatcher.dispatched[1]["action"] == "go_to_safe_position"
        assert scheduler.workflow_snapshot("wf-two-liquid")["state"] == "waiting_for_material"

        scheduler.on_job_finished(dispatcher.dispatched[1]["job_id"], success=True)
        assert dispatcher.dispatched[2]["action"] == "replace_tip_box"
        svc.inbound_lot("tpl-w", 1.0, lot_id="lot-1")
        scheduler.on_job_finished(dispatcher.dispatched[2]["job_id"], success=True)
        assert dispatcher.dispatched[3]["node_id"] == "liquid-b"
        scheduler.on_job_finished(dispatcher.dispatched[3]["job_id"], success=True)
        assert scheduler.workflow_snapshot("wf-two-liquid")["state"] == "success"

    def test_shortage_creates_independent_urgent_workflow_and_resumes_after_recheck(self):
        scheduler, dispatcher, svc = _stack(stock=1.0)
        factory_calls = []

        def build_replenishment(run, requirements):
            factory_calls.append((run.run_id, requirements))
            return WorkflowSpec(
                workflow_id="wf1-replenish",
                priority="low",  # scheduler must enforce Scheme 3 priority
                nodes=[_node("R", device="replenisher", action="replace_tip_box")],
            )

        scheduler = EdgeScheduler(
            dispatcher=dispatcher,
            inventory=svc,
            material_replenishment_factory=build_replenishment,
        )
        original = WorkflowSpec(
            workflow_id="wf1",
            priority="normal",
            nodes=[_node("A", materials=[_req(lot="lot-1", qty=2.0)])],
        )

        result = scheduler.submit_workflow(original)
        assert result["state"] == "waiting_for_material"
        assert len(factory_calls) == 1
        assert dispatcher.dispatched[0]["workflow_id"] == "wf1-replenish"
        assert scheduler.workflow_snapshot("wf1")["state"] == "waiting_for_material"
        snap = scheduler.snapshot()
        assert (
            snap["material_replenishments"]["wf1"]["replenishment_workflow_id"]
            == "wf1-replenish"
        )
        assert snap["workflows"]["wf1-replenish"]["state"] == "running"
        assert scheduler._workflows["wf1-replenish"].spec.priority == "urgent"
        assert scheduler._workflows["wf1-replenish"].spec.nodes[0].action_name == (
            "go_to_safe_position"
        )

        # 重排不会重复创建补料 Run，也不会绕过尚未完成的依赖。
        scheduler.reschedule()
        assert len(factory_calls) == 1
        assert len(dispatcher.dispatched) == 1

        # S09 还原完成后才能进入换盒动作；补料动作完成前写入库存，
        # 完成回调会再次检查并预留原工作流。
        scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], success=True)
        assert dispatcher.dispatched[1]["action"] == "replace_tip_box"
        svc.inbound_lot("tpl-w", 1.0, lot_id="lot-1")
        scheduler.on_job_finished(dispatcher.dispatched[1]["job_id"], success=True)

        assert scheduler.workflow_snapshot("wf1")["state"] == "running"
        assert dispatcher.dispatched[2]["workflow_id"] == "wf1"
        assert dispatcher.dispatched[2]["node_id"] == "A"

    def test_replenishment_waits_for_device_lock_then_precedes_user_priority(self):
        """机械臂忙时换盒不抢占；锁释放后换盒领先任意用户 Priority。"""
        scheduler, dispatcher, svc = _stack(stock=0.0)
        scheduler._external_busy_keys.add("/devices/robot")

        def build_replenishment(run, requirements):
            return WorkflowSpec(
                workflow_id="tip-replenishment",
                priority="low",
                nodes=[
                    _node(
                        "replace-tip",
                        device="robot",
                        action="replace_tip_box",
                    )
                ],
            )

        scheduler._material_replenishment_factory = build_replenishment
        shortage = scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="liquid-workflow",
                nodes=[_node("liquid", materials=[_req(lot="lot-1", qty=1.0)])],
            )
        )
        user = scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="user-highest",
                priority=9999.0,
                nodes=[_node("user-move", device="robot", action="move")],
            )
        )

        assert shortage["state"] == "waiting_for_material"
        # S09 还原本身占用 S09，而不是机械臂；它可以先执行。
        assert shortage["dispatched"][0]["node_id"] == "tip-replenishment:s09-restore"
        assert user["dispatched"] == []

        scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], success=True)
        assert len(dispatcher.dispatched) == 1

        scheduler._external_busy_keys.clear()
        released = scheduler.reschedule()
        assert [item["workflow_id"] for item in released] == ["tip-replenishment"]
        assert dispatcher.dispatched[1]["action"] == "replace_tip_box"
        assert dispatcher.dispatched[1]["workflow_id"] == "tip-replenishment"

    def test_replenishment_failure_keeps_requester_waiting(self):
        scheduler, dispatcher, svc = _stack(stock=0.0)

        def build_replenishment(run, requirements):
            return WorkflowSpec(
                workflow_id="wf-fail-replenish",
                nodes=[_node("R", device="replenisher", action="replace_tip_box")],
            )

        scheduler = EdgeScheduler(
            dispatcher=dispatcher,
            inventory=svc,
            material_replenishment_factory=build_replenishment,
        )
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-fail",
                nodes=[_node("A", materials=[_req(lot="lot-1", qty=1.0)])],
            )
        )
        scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], success=False)
        assert scheduler.workflow_snapshot("wf-fail")["state"] == "waiting_for_material"
        assert scheduler.workflow_snapshot("wf-fail-replenish")["state"] == "failed"

    def test_tip_box_transfer_chain_cannot_bypass_s09_restore(self):
        scheduler, dispatcher, _ = _stack(stock=0.0)

        def build_replenishment(run, requirements):
            return WorkflowSpec(
                workflow_id="wf-tip-chain",
                nodes=[
                    _node("pick-old", device="robot", action="submit_pick_from_s09"),
                    _node("place-old", device="robot", action="submit_place_to_s02"),
                    _node("pick-new", device="robot", action="submit_pick_from_s02"),
                    _node("place-new", device="robot", action="submit_place_to_s09"),
                ],
                edges=[
                    _edge("pick-old", "place-old"),
                    _edge("place-old", "pick-new"),
                    _edge("pick-new", "place-new"),
                ],
            )

        scheduler._material_replenishment_factory = build_replenishment
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-tip-user",
                nodes=[_node("liquid", materials=[_req(lot="lot-1", qty=1.0)])],
            )
        )

        assert dispatcher.dispatched[0]["action"] == "go_to_safe_position"
        restore = scheduler._workflows["wf-tip-chain"].spec.nodes[0]
        assert restore.device_id == "szlab_mixer_pipetting_station"
        assert restore.param == {"home_position": 1, "require_allow": True}

        expected_actions = [
            "submit_pick_from_s09",
            "submit_place_to_s02",
            "submit_pick_from_s02",
            "submit_place_to_s09",
        ]
        for expected_action in expected_actions:
            scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], success=True)
            assert dispatcher.dispatched[-1]["action"] == expected_action
