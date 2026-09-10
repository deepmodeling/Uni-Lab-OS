"""EdgeScheduler 触发点与资源锁行为测试。

核心断言：**每个工作流提交、每个子 action 完成，都触发一次重排**。
"""

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import (
    Handle,
    NODE_TYPES,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    normalize_node_type,
)
from unilabos.app.scheduler.ordering import OrderingContext, StableLocalOrderer
from unilabos.app.scheduler.service import EdgeScheduler


def _node(node_id: str, device: str = "dev1", action: str = "run") -> WorkflowNode:
    return WorkflowNode(
        id=node_id, device_id=device, action_name=action, action_type="goal", param={}
    )


def _edge(src: str, dst: str, sh: str = "", th: str = "") -> WorkflowEdge:
    return WorkflowEdge(
        uuid=f"{src}->{dst}",
        source_node_id=src,
        target_node_id=dst,
        source_handle_uuid=sh,
        target_handle_uuid=th,
    )


def _chain_spec(workflow_id: str, device: str = "dev1", priority=1.0) -> WorkflowSpec:
    """A → B 两节点链。"""
    return WorkflowSpec(
        workflow_id=workflow_id,
        nodes=[_node("A", device), _node("B", device)],
        edges=[_edge("A", "B")],
        priority=priority,
    )


def _step_chain_spec(workflow_id: str, device: str = "dev1") -> WorkflowSpec:
    """A → B 两节点单步任务。"""
    spec = _chain_spec(workflow_id, device=device)
    spec.run_mode = "step"
    return spec


def _make() -> "tuple[EdgeScheduler, RecordingDispatcher]":
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    return scheduler, dispatcher


def test_transfer_node_type_is_canonical_but_not_executable_as_ilab():
    assert "Transfer" in NODE_TYPES
    node = WorkflowNode(id="transfer", node_type=normalize_node_type("transfer"))
    assert node.node_type == "Transfer"
    assert not node.is_ilab()


class TestTriggerOnSubmit:
    def test_submit_dispatches_ready_immediately(self):
        scheduler, dispatcher = _make()
        result = scheduler.submit_workflow(_chain_spec("wf1"))
        # 触发点 1：提交即重排，根节点 A 立即下发
        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["node_id"] == "A"
        assert len(dispatcher.dispatched) == 1
        assert dispatcher.dispatched[0]["action"] == "run"

    def test_each_submit_triggers_reschedule(self):
        scheduler, _ = _make()
        scheduler.submit_workflow(_chain_spec("wf1", device="d1"))
        scheduler.submit_workflow(_chain_spec("wf2", device="d2"))
        assert scheduler.snapshot()["reschedule_count"] == 2

    def test_duplicate_submit_rejected(self):
        scheduler, _ = _make()
        scheduler.submit_workflow(_chain_spec("wf1"))
        try:
            scheduler.submit_workflow(_chain_spec("wf1"))
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_step_submit_stays_paused_without_dispatch(self):
        scheduler, dispatcher = _make()

        result = scheduler.submit_workflow(_step_chain_spec("wf-step"))

        assert result["state"] == "paused"
        assert result["dispatched"] == []
        assert dispatcher.dispatched == []


class TestStepControl:
    def test_each_step_dispatches_one_node_and_pauses_before_next(self):
        scheduler, dispatcher = _make()
        scheduler.submit_workflow(_step_chain_spec("wf-step"))

        first = scheduler.step_workflow("wf-step")
        assert first["state"] == "paused"
        assert [item["node_id"] for item in first["dispatched"]] == ["A"]

        finished = scheduler.on_job_finished(
            first["dispatched"][0]["job_id"], success=True, ret_value={}
        )
        assert finished["workflow_state"] == "paused"
        assert finished["dispatched"] == []
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A"]

        second = scheduler.step_workflow("wf-step")
        assert [item["node_id"] for item in second["dispatched"]] == ["B"]


class TestTriggerOnJobFinish:
    def test_finish_dispatches_next(self):
        scheduler, dispatcher = _make()
        result = scheduler.submit_workflow(_chain_spec("wf1"))
        job_id = result["dispatched"][0]["job_id"]

        # 触发点 2：A 完成 → 重排 → B 下发
        result2 = scheduler.on_job_finished(job_id, success=True, ret_value={})
        assert [d["node_id"] for d in result2["dispatched"]] == ["B"]
        assert len(dispatcher.dispatched) == 2
        assert scheduler.snapshot()["reschedule_count"] == 2

    def test_finish_last_node_completes_workflow(self):
        scheduler, _ = _make()
        r1 = scheduler.submit_workflow(_chain_spec("wf1"))
        r2 = scheduler.on_job_finished(r1["dispatched"][0]["job_id"], True)
        r3 = scheduler.on_job_finished(r2["dispatched"][0]["job_id"], True)
        assert r3["workflow_state"] == "success"
        assert r3["dispatched"] == []

    def test_failure_stops_workflow(self):
        scheduler, _ = _make()
        r1 = scheduler.submit_workflow(_chain_spec("wf1"))
        r2 = scheduler.on_job_finished(r1["dispatched"][0]["job_id"], success=False)
        assert r2["workflow_state"] == "failed"
        assert r2["dispatched"] == []

    def test_unknown_job_ignored(self):
        scheduler, _ = _make()
        assert scheduler.on_job_finished("nope", True)["dispatched"] == []


class TestResourceLock:
    def test_same_device_action_serialized(self):
        """两个工作流抢同一 device+action：后者等前者完成的那次重排。"""
        scheduler, dispatcher = _make()
        r1 = scheduler.submit_workflow(_chain_spec("wf1", device="shared"))
        r2 = scheduler.submit_workflow(_chain_spec("wf2", device="shared"))
        # wf2 的 A 因锁忙未下发
        assert r2["dispatched"] == []
        assert len(dispatcher.dispatched) == 1

        # wf1.A 完成 → 释放锁 → 这次重排同时下发 wf1.B(等待) 或 wf2.A（顺序由排序器决定）
        r3 = scheduler.on_job_finished(r1["dispatched"][0]["job_id"], True)
        # 同一设备只能有一个在跑
        assert len(r3["dispatched"]) == 1

    def test_same_device_different_actions_are_serialized(self):
        """证明同一设备的不同动作仍共享设备级互斥，后提交作业保持待处理。

        参数：无；测试构造两个指向同一设备、动作名不同的工作流（Workflow）。
        返回：无；通过派发摘要和记录适配器断言设备级准入行为。
        异常：若第二个动作越过执行器（Executor）边界，断言失败。
        """
        scheduler, dispatcher = _make()
        # 两个工作流身份分别代表已取得同一设备执行器分配的独立运行。
        first_result = scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-device-run",
                nodes=[_node("run-node", device="shared", action="run")],
            )
        )
        second_result = scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-device-inspect",
                nodes=[_node("inspect-node", device="shared", action="inspect")],
            )
        )

        assert len(first_result["dispatched"]) == 1
        assert second_result["dispatched"] == []
        assert [item["action"] for item in dispatcher.dispatched] == ["run"]

    def test_device_level_external_busy_key_blocks_every_action(self):
        """证明设备级外部占用键会阻止该设备的任意动作进入物理派发。

        参数：无；测试向调度器注入设备级外部忙碌键。
        返回：无；断言动作级键不同也不能绕过设备级互斥。
        异常：若动作被派发到执行器（Executor），断言失败。
        """
        # 设备级外部忙碌键表示该设备已被本调度器之外的执行占用。
        external_device_busy_keys = {"/devices/shared"}
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(
            dispatcher=dispatcher,
            external_busy_keys=external_device_busy_keys,
        )
        result = scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-external-device-busy",
                nodes=[_node("inspect-node", device="shared", action="inspect")],
            )
        )

        assert result["dispatched"] == []
        assert dispatcher.dispatched == []

    def test_malformed_external_action_keys_are_not_promoted_to_device_keys(self):
        """证明只有严格动作键形状才会提升为设备级内存互斥键。

        参数：无；测试注入多段路径、空设备和空动作等非规范外部键。
        返回：无；断言这些键被原样保留但不会误阻塞合法设备动作。
        异常：若宽松路径解析把无关外部事实提升为设备互斥，断言失败。
        """
        # 非规范键覆盖多余层级、空设备和空动作，均不得推导设备身份。
        malformed_external_keys = {
            "/devices/shared/run/status",
            "/devices//run",
            "/devices/shared/",
        }
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(
            dispatcher=dispatcher,
            external_busy_keys=malformed_external_keys,
        )
        result = scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-malformed-external-key",
                nodes=[_node("inspect-node", device="shared", action="inspect")],
            )
        )

        assert len(result["dispatched"]) == 1
        assert [item["action"] for item in dispatcher.dispatched] == ["inspect"]

    def test_completion_releases_device_for_only_one_waiting_action(self):
        """证明设备完成一个作业后，同轮准入只放行一个等待中的不同动作。

        参数：无；测试构造同一设备上的一个在途作业和两个等待作业。
        返回：无；断言完成回调触发的重排只产生一个新派发。
        异常：若同轮放行多个动作而形成设备并发，断言失败。
        """
        scheduler, dispatcher = _make()
        first_result = scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-active",
                nodes=[_node("active-node", device="shared", action="run")],
            )
        )
        for workflow_id, action_name in (
            ("wf-waiting-inspect", "inspect"),
            ("wf-waiting-clean", "clean"),
        ):
            # 每个等待工作流都持有独立身份，但共享同一执行设备。
            waiting_result = scheduler.submit_workflow(
                WorkflowSpec(
                    workflow_id=workflow_id,
                    nodes=[
                        _node(
                            f"{action_name}-node",
                            device="shared",
                            action=action_name,
                        )
                    ],
                )
            )
            assert waiting_result["dispatched"] == []

        # 完成作业身份来自第一次派发；其释放只允许下一项取得设备级互斥。
        active_job_uuid = first_result["dispatched"][0]["job_id"]
        completion_result = scheduler.on_job_finished(active_job_uuid, success=True)

        assert len(completion_result["dispatched"]) == 1
        assert len(dispatcher.dispatched) == 2

    def test_different_devices_keep_parallel_admission(self):
        """证明设备级互斥不会扩大成全局互斥，不同设备仍可并行派发。

        参数：无；测试构造两个设备上动作名不同的独立工作流（Workflow）。
        返回：无；断言两个执行器分配均越过派发边界。
        异常：若第二个设备被无关占用阻塞，断言失败。
        """
        scheduler, dispatcher = _make()
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-device-one",
                nodes=[_node("one", device="device-one", action="run")],
            )
        )
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-device-two",
                nodes=[_node("two", device="device-two", action="inspect")],
            )
        )

        assert {
            (item["device_id"], item["action"]) for item in dispatcher.dispatched
        } == {
            ("device-one", "run"),
            ("device-two", "inspect"),
        }

    def test_different_devices_parallel(self):
        scheduler, dispatcher = _make()
        scheduler.submit_workflow(_chain_spec("wf1", device="d1"))
        scheduler.submit_workflow(_chain_spec("wf2", device="d2"))
        assert len(dispatcher.dispatched) == 2

    def test_external_busy_key_blocks(self):
        busy = {"/devices/dev1/run"}
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher, external_busy_keys=busy)
        r = scheduler.submit_workflow(_chain_spec("wf1", device="dev1"))
        assert r["dispatched"] == []
        # 外部锁释放后，手动/下次触发重排即可下发
        busy.clear()
        assert [d["node_id"] for d in scheduler.reschedule()] == ["A"]


class TestPriorityOrdering:
    def test_high_priority_first_on_contention(self):
        """同设备争抢时，高优先级工作流先拿到锁。"""
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher, external_busy_keys={"/devices/s/run"})
        scheduler.submit_workflow(_chain_spec("wf-low", device="s", priority="low"))
        scheduler.submit_workflow(_chain_spec("wf-high", device="s", priority="high"))
        # 解锁后重排：high 先下发
        scheduler._external_busy_keys.clear()
        dispatched = scheduler.reschedule()
        assert dispatched[0]["workflow_id"] == "wf-high"

    def test_stable_orderer_key(self):
        orderer = StableLocalOrderer()
        from unilabos.app.scheduler.models import ReadyTask

        t1 = ReadyTask("wf1", _node("A"), priority_weight=50.0, submitted_at=1.0)
        t2 = ReadyTask("wf2", _node("A"), priority_weight=200.0, submitted_at=2.0)
        t3 = ReadyTask("wf3", _node("A"), priority_weight=200.0, submitted_at=1.5)
        ordered = orderer.order([t1, t2, t3], OrderingContext(set()))
        assert [t.workflow_id for t in ordered] == ["wf3", "wf2", "wf1"]

    def test_device_lock_precedes_priority(self):
        """设备锁忙时，urgent 也不能排在可用设备动作之前。"""
        orderer = StableLocalOrderer()
        from unilabos.app.scheduler.models import ReadyTask

        busy_urgent = ReadyTask(
            "wf-urgent-busy",
            _node("A", device="shared"),
            priority_weight=300.0,
            submitted_at=1.0,
        )
        available_low = ReadyTask(
            "wf-low-free",
            _node("A", device="other"),
            priority_weight=50.0,
            submitted_at=2.0,
        )
        ordered = orderer.order(
            [busy_urgent, available_low],
            OrderingContext({"/devices/shared"}),
        )
        assert [task.workflow_id for task in ordered] == [
            "wf-low-free",
            "wf-urgent-busy",
        ]

    def test_resource_unblocking_precedes_every_user_priority_after_lock_admission(self):
        """解阻动作不越过设备锁，但在可准入候选中领先任意用户权重。"""
        orderer = StableLocalOrderer()
        from unilabos.app.scheduler.models import ReadyTask

        blocked_replenishment = ReadyTask(
            "wf-replenish-busy",
            _node("replace", device="busy-robot"),
            priority_weight=50.0,
            submitted_at=2.0,
            is_resource_unblocking=True,
        )
        free_user_action = ReadyTask(
            "wf-user-free",
            _node("run", device="free-robot"),
            priority_weight=9999.0,
            submitted_at=1.0,
        )
        assert [
            task.workflow_id
            for task in orderer.order(
                [blocked_replenishment, free_user_action],
                OrderingContext({"/devices/busy-robot"}),
            )
        ] == ["wf-user-free", "wf-replenish-busy"]

        admitted_replenishment = ReadyTask(
            "wf-replenish-free",
            _node("replace", device="shared"),
            priority_weight=50.0,
            submitted_at=2.0,
            is_resource_unblocking=True,
        )
        highest_user_action = ReadyTask(
            "wf-user-highest",
            _node("run", device="shared"),
            priority_weight=9999.0,
            submitted_at=1.0,
        )
        assert [
            task.workflow_id
            for task in orderer.order(
                [highest_user_action, admitted_replenishment],
                OrderingContext(set()),
            )
        ] == ["wf-replenish-free", "wf-user-highest"]

    def test_material_lock_precedes_resource_unblocking_priority(self):
        """换盒动作持有冲突物料锁时，不能越过锁去压过普通动作。"""
        orderer = StableLocalOrderer()
        from unilabos.app.scheduler.models import ReadyTask

        blocked_replenishment = ReadyTask(
            "wf-replenish-material-busy",
            _node("replace", device="robot"),
            priority_weight=50.0,
            submitted_at=1.0,
            is_resource_unblocking=True,
        )
        blocked_replenishment.resource_lock_keys = {"material/tip/exclusive"}
        free_user_action = ReadyTask(
            "wf-user-free",
            _node("run", device="other"),
            priority_weight=9999.0,
            submitted_at=2.0,
        )
        ordered = orderer.order(
            [blocked_replenishment, free_user_action],
            OrderingContext(set(), {"material/tip/exclusive"}),
        )
        assert [task.workflow_id for task in ordered] == [
            "wf-user-free",
            "wf-replenish-material-busy",
        ]

    def test_inflight_device_lock_cannot_be_overridden_by_late_urgent_workflow(self):
        """低优先级动作一旦持有设备锁，迟到 urgent 必须等待完成。"""
        scheduler, dispatcher = _make()
        low = scheduler.submit_workflow(
            _chain_spec("wf-low-running", device="shared", priority="low")
        )
        urgent = scheduler.submit_workflow(
            _chain_spec("wf-urgent-late", device="shared", priority="urgent")
        )

        assert [item["workflow_id"] for item in dispatcher.dispatched] == [
            "wf-low-running"
        ]
        assert urgent["dispatched"] == []

        released = scheduler.on_job_finished(low["dispatched"][0]["job_id"], True)
        assert [item["workflow_id"] for item in released["dispatched"]] == [
            "wf-urgent-late"
        ]

    def test_busy_urgent_device_does_not_block_free_low_priority_device(self):
        """锁忙的 urgent 节点不能挡住另一台空闲设备上的 low 节点。"""
        dispatcher = RecordingDispatcher()
        busy = {"/devices/shared"}
        scheduler = EdgeScheduler(dispatcher=dispatcher, external_busy_keys=busy)

        urgent = scheduler.submit_workflow(
            _chain_spec("wf-urgent-busy", device="shared", priority="urgent")
        )
        assert urgent["dispatched"] == []

        low = scheduler.submit_workflow(
            _chain_spec("wf-low-free", device="other", priority="low")
        )
        assert [item["workflow_id"] for item in low["dispatched"]] == [
            "wf-low-free"
        ]

        busy.clear()
        released = scheduler.reschedule()
        assert [item["workflow_id"] for item in released] == ["wf-urgent-busy"]

    def test_waiting_workflow_uses_robot_during_urgent_stir(self):
        """U 搅拌占用搅拌器时，空闲机械臂应先执行等待中的 W4。"""
        scheduler, dispatcher = _make()
        u = WorkflowSpec(
            workflow_id="U",
            priority="urgent",
            nodes=[
                _node("move-to-stir", device="robot", action="transfer"),
                _node("stir", device="stirrer", action="stir"),
            ],
            edges=[_edge("move-to-stir", "stir")],
        )
        w4 = WorkflowSpec(
            workflow_id="W4",
            priority="normal",
            nodes=[_node("move-to-liquid", device="robot", action="transfer")],
        )

        u_result = scheduler.submit_workflow(u)
        assert [(item["workflow_id"], item["node_id"]) for item in u_result["dispatched"]] == [
            ("U", "move-to-stir")
        ]
        w4_result = scheduler.submit_workflow(w4)
        assert w4_result["dispatched"] == []

        after_move = scheduler.on_job_finished(u_result["dispatched"][0]["job_id"], True)
        assert [
            (item["workflow_id"], item["node_id"])
            for item in after_move["dispatched"]
        ] == [("U", "stir"), ("W4", "move-to-liquid")]

    def test_urgent_transfer_waits_for_unique_site_then_jumps_admission_queue(self):
        """唯一工位占用期间不派发机器人；释放后 urgent 先进入工位。"""

        class SiteInventory:
            def __init__(self) -> None:
                self.occupant = "beaker-in-pump"

            def site_occupant(self, owner_uuid: str, site_identity: str):
                assert owner_uuid == "s06-process-warehouse"
                assert site_identity == "S061"
                return self.occupant

        transfer_schema = {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": {
                        "resource": {"type": "object"},
                        "target_warehouse": {"type": "object"},
                        "target_site": {"type": "string"},
                    },
                    "required": ["resource", "target_warehouse", "target_site"],
                }
            },
            "x-unilabos-action-contract": {
                "resource_contract": {
                    "transfer": {
                        "material_param": "resource",
                        "target_owner_param": "target_warehouse",
                        "target_site_name_param": "target_site",
                    }
                }
            },
        }

        def workflow(
            workflow_id: str, priority: str, device_id: str = "robot"
        ) -> WorkflowSpec:
            return WorkflowSpec(
                workflow_id=workflow_id,
                priority=priority,
                nodes=[
                    WorkflowNode(
                        id="enter-s06",
                        device_id=device_id,
                        action_name="transfer_material_atomic",
                        action_type="goal",
                        param={
                            "resource": {"uuid": f"beaker-{workflow_id}"},
                            "target_warehouse": {"uuid": "s06-process-warehouse"},
                            "target_site": "S061",
                        },
                        param_schema=transfer_schema,
                    )
                ],
            )

        inventory = SiteInventory()
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)

        for index in range(1, 5):
            # 第四项故意使用另一执行器：Site 释放后的同一轮不能让两个机器人
            # 都基于尚未落账的“空位”同时驶向 S06。
            device_id = "robot-2" if index == 4 else "robot"
            result = scheduler.submit_workflow(
                workflow(f"wf-{index}", "normal", device_id)
            )
            assert result["dispatched"] == []
        urgent = scheduler.submit_workflow(workflow("wf-urgent", "urgent"))

        # S06 尚有 W3 的烧杯时，连 urgent 也不能占用机械臂去提前取杯。
        assert urgent["dispatched"] == []
        assert dispatcher.dispatched == []

        inventory.occupant = None
        admitted = scheduler.reschedule()

        assert [item["workflow_id"] for item in admitted] == ["wf-urgent"]
        assert [item["workflow_id"] for item in dispatcher.dispatched] == ["wf-urgent"]

    def test_running_liquid_workflow_must_leave_site_before_late_urgent_starts(self):
        """W3 加液完成后先移出烧杯，U 不能越过这个释放动作。"""

        class SiteInventory:
            def __init__(self) -> None:
                self.occupants = {
                    "S061": "beaker-w3",
                    "STAGING": None,
                }

            def site_occupant(self, owner_uuid: str, site_identity: str):
                assert owner_uuid == "s06-process-warehouse"
                return self.occupants.get(site_identity)

        transfer_schema = {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": {
                        "resource": {"type": "object"},
                        "target_warehouse": {"type": "object"},
                        "target_site": {"type": "string"},
                    },
                    "required": ["resource", "target_warehouse", "target_site"],
                }
            },
            "x-unilabos-action-contract": {
                "resource_contract": {
                    "transfer": {
                        "material_param": "resource",
                        "target_owner_param": "target_warehouse",
                        "target_site_name_param": "target_site",
                    }
                }
            },
        }

        def transfer_workflow(workflow_id: str, priority: str, material: str, site: str):
            return WorkflowSpec(
                workflow_id=workflow_id,
                priority=priority,
                nodes=[
                    WorkflowNode(
                        id="transfer",
                        device_id="robot",
                        action_name="transfer_material_atomic",
                        action_type="goal",
                        param={
                            "resource": {"uuid": material},
                            "target_warehouse": {"uuid": "s06-process-warehouse"},
                            "target_site": site,
                        },
                        param_schema=transfer_schema,
                    )
                ],
            )

        inventory = SiteInventory()
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)

        # 前两个工作流已完成；此时 W3 正在唯一加液泵上执行，W4 只能等待 S061。
        for workflow_id in ("W1", "W2"):
            result = scheduler.submit_workflow(
                _chain_spec(workflow_id, device="robot", priority="normal")
            )
            for item in result["dispatched"]:
                scheduler.on_job_finished(item["job_id"], True)
            while scheduler.snapshot()["inflight_jobs"]:
                job_id = next(iter(scheduler.snapshot()["inflight_jobs"]))
                scheduler.on_job_finished(job_id, True)

        w3 = WorkflowSpec(
            workflow_id="W3",
            priority="normal",
            nodes=[
                WorkflowNode(
                    id="liquid",
                    device_id="liquid-pump",
                    action_name="add_liquid",
                    action_type="goal",
                    param={},
                ),
                WorkflowNode(
                    id="remove",
                    device_id="robot",
                    action_name="transfer_material_atomic",
                    action_type="goal",
                    param={
                        "resource": {"uuid": "beaker-w3"},
                        "target_warehouse": {"uuid": "s06-process-warehouse"},
                        "target_site": "STAGING",
                    },
                    param_schema=transfer_schema,
                ),
            ],
            edges=[_edge("liquid", "remove")],
        )
        w3_result = scheduler.submit_workflow(w3)
        assert [(item["workflow_id"], item["node_id"]) for item in w3_result["dispatched"]] == [("W3", "liquid")]

        w4_result = scheduler.submit_workflow(
            transfer_workflow("W4", "normal", "beaker-w4", "S061")
        )
        u_result = scheduler.submit_workflow(
            transfer_workflow("U", "urgent", "beaker-u", "S061")
        )
        assert w4_result["dispatched"] == []
        assert u_result["dispatched"] == []

        liquid_job = w3_result["dispatched"][0]["job_id"]
        after_liquid = scheduler.on_job_finished(liquid_job, True)
        # U 的 priority 更高，但它的目标工位仍被 W3 占用；先派发 W3.remove。
        assert [(item["workflow_id"], item["node_id"]) for item in after_liquid["dispatched"]] == [("W3", "remove")]

        inventory.occupants["S061"] = None
        remove_job = after_liquid["dispatched"][0]["job_id"]
        after_remove = scheduler.on_job_finished(remove_job, True)
        assert [(item["workflow_id"], item["node_id"]) for item in after_remove["dispatched"]] == [("U", "transfer")]


class TestParamFlow:
    def test_ret_value_passed_via_handles(self):
        """A 的返回值经 handle 传参写入 B 的 action_args。"""
        sh = Handle(uuid="sh", data_source="executor", handle_key="out", data_key="volume")
        th = Handle(uuid="th", data_source="handle", handle_key="in", data_key="target_volume")
        spec = WorkflowSpec(
            workflow_id="wf-param",
            nodes=[
                _node("A", device="d1"),
                WorkflowNode(
                    id="B",
                    device_id="d2",
                    action_name="run",
                    action_type="goal",
                    param={"target_volume": 0},
                ),
            ],
            edges=[_edge("A", "B", sh="sh", th="th")],
            handles=[sh, th],
        )
        scheduler, dispatcher = _make()
        r1 = scheduler.submit_workflow(spec)
        scheduler.on_job_finished(r1["dispatched"][0]["job_id"], True, ret_value={"volume": 42})
        assert dispatcher.dispatched[-1]["node_id"] == "B"
        assert dispatcher.dispatched[-1]["action_args"] == {"target_volume": 42}

    def test_param_resolve_failure_fails_node(self):
        sh = Handle(uuid="sh", data_source="executor", handle_key="out", data_key="missing")
        th = Handle(uuid="th", data_source="handle", handle_key="in", data_key="k")
        spec = WorkflowSpec(
            workflow_id="wf-bad",
            nodes=[_node("A", device="d1"), _node("B", device="d2")],
            edges=[_edge("A", "B", sh="sh", th="th")],
            handles=[sh, th],
        )
        scheduler, _ = _make()
        r1 = scheduler.submit_workflow(spec)
        r2 = scheduler.on_job_finished(r1["dispatched"][0]["job_id"], True, ret_value={"other": 1})
        assert r2["dispatched"] == []
        assert r2["workflow_state"] == "failed"


class TestCancel:
    def test_cancel_stops_dispatch(self):
        scheduler, dispatcher = _make()
        r1 = scheduler.submit_workflow(_chain_spec("wf1"))
        assert scheduler.cancel_workflow("wf1") is True
        # 完成回调后不再推进
        scheduler.on_job_finished(r1["dispatched"][0]["job_id"], True)
        assert len(dispatcher.dispatched) == 1


class TestManualConfirmNodes:
    """manual_confirm 特殊节点：不进执行器、不占设备锁，靠 finish_job 人工放行。"""

    def _manual_spec(self, workflow_id: str) -> WorkflowSpec:
        manual = WorkflowNode(
            id="M",
            device_id="operator",
            action_name="confirm",
            action_type="goal",
            param={"prompt": "确认无误后继续"},
            node_type="manual_confirm",
        )
        return WorkflowSpec(
            workflow_id=workflow_id,
            nodes=[_node("A"), manual, _node("B")],
            edges=[_edge("A", "M"), _edge("M", "B")],
        )

    def test_manual_confirm_parks_without_dispatch(self):
        scheduler, dispatcher = _make()
        scheduler.submit_workflow(self._manual_spec("wf-manual"))
        job_a = dispatcher.dispatched[0]["job_id"]
        scheduler.on_job_finished(job_a, True, {}, "normal")
        # M 已进入 dispatched 停驻，但执行器只收到过 A
        assert len(dispatcher.dispatched) == 1
        snap = scheduler.workflow_snapshot("wf-manual")
        assert snap["nodes"]["M"]["state"] == "dispatched"
        # 快照必须带 job_id，前端凭它调 /jobs/{id}/finish
        manual_job = snap["nodes"]["M"].get("job_id")
        assert manual_job

    def test_manual_confirm_finish_releases_downstream(self):
        scheduler, dispatcher = _make()
        scheduler.submit_workflow(self._manual_spec("wf-manual2"))
        scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], True, {}, "normal")
        manual_job = scheduler.workflow_snapshot("wf-manual2")["nodes"]["M"]["job_id"]
        scheduler.on_job_finished(manual_job, True, {"confirmed": True}, "normal")
        # 人工放行后 B 正常下发，链路走完工作流成功
        assert dispatcher.dispatched[-1]["action"] == "run"
        scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], True, {}, "normal")
        assert scheduler.workflow_snapshot("wf-manual2")["state"] == "success"

    def test_manual_confirm_ignores_device_busy(self):
        scheduler, dispatcher = _make()
        # 同 key 的设备 job 占着锁
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-busy",
                nodes=[_node("A", device="operator", action="confirm")],
            )
        )
        # manual_confirm 与其同 key，但 always-free：照样立即停驻下发
        manual_only = WorkflowSpec(
            workflow_id="wf-manual3",
            nodes=[
                WorkflowNode(
                    id="M",
                    device_id="operator",
                    action_name="confirm",
                    node_type="manual_confirm",
                )
            ],
        )
        scheduler.submit_workflow(manual_only)
        snap = scheduler.workflow_snapshot("wf-manual3")
        assert snap["nodes"]["M"]["state"] == "dispatched"

    def test_manual_confirm_does_not_occupy_device_for_an_action(self):
        """证明人工确认节点停驻期间不建立设备级或动作级内存互斥。

        参数：无；测试先停驻人工确认节点，再提交同设备同动作的普通节点。
        返回：无；断言普通动作仍可越过执行器（Executor）派发边界。
        异常：若人工确认被错误计入设备忙碌集合，断言失败。
        """
        scheduler, dispatcher = _make()
        manual_only = WorkflowSpec(
            workflow_id="wf-manual-free",
            nodes=[
                WorkflowNode(
                    id="manual-node",
                    device_id="operator",
                    action_name="confirm",
                    node_type="manual_confirm",
                )
            ],
        )
        scheduler.submit_workflow(manual_only)
        # 普通动作与人工确认复用相同设备和动作身份，仍应立即实际派发。
        ordinary_result = scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-ordinary-after-manual",
                nodes=[_node("ordinary-node", device="operator", action="confirm")],
            )
        )

        assert len(ordinary_result["dispatched"]) == 1
        assert [
            (item["device_id"], item["action"]) for item in dispatcher.dispatched
        ] == [("operator", "confirm")]
