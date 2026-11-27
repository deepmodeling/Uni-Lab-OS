"""EdgeScheduler 触发点与资源锁行为测试。

核心断言：**每个工作流提交、每个子 action 完成，都触发一次重排**。
"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from unilabos.app.scheduler.device_target import (
    DeviceTargetUnavailable,
    ResolvedDeviceTarget,
)
from unilabos.app.scheduler.dispatch import CallbackDispatcher, RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.models import (
    NODE_TYPES,
    Handle,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    normalize_node_type,
)
from unilabos.app.scheduler.ordering import OrderingContext, StableLocalOrderer
from unilabos.app.scheduler.service import EdgeScheduler, ExecutionPolicyError
from unilabos.app.scheduler.site_target import SiteTargetResolutionError


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


def test_serial_queue_finishes_the_head_before_using_another_device() -> None:
    scheduler = EdgeScheduler(serial_queue=True)
    try:
        head = scheduler.submit_workflow(_chain_spec("head", "device-a"))
        queued = scheduler.submit_workflow(_chain_spec("queued", "device-b", priority="urgent"))
        assert queued["dispatched"] == []

        first = head["dispatched"][0]
        second, = scheduler.on_job_finished(first["job_id"], True)["dispatched"]
        assert (second["workflow_id"], second["node_id"]) == ("head", "B")
        next_task, = scheduler.on_job_finished(second["job_id"], True)["dispatched"]
        assert (next_task["workflow_id"], next_task["node_id"]) == ("queued", "A")
    finally:
        scheduler.close()


@pytest.mark.parametrize("all_disabled", [False, True])
def test_serial_queue_settles_workflows_without_enabled_nodes(all_disabled: bool) -> None:
    scheduler = EdgeScheduler(serial_queue=True)
    try:
        node = _node("disabled")
        node.disabled = True
        empty = WorkflowSpec(workflow_id="empty", nodes=[node] if all_disabled else [])
        result = scheduler.submit_workflow(empty)
        assert result["state"] == "success"
        assert result["dispatched"] == []
        assert len(scheduler.submit_workflow(_chain_spec("next"))["dispatched"]) == 1
    finally:
        scheduler.close()


def test_serial_queue_reports_pending_until_material_reservation_is_attempted() -> None:
    reserve = Mock()
    scheduler = EdgeScheduler(serial_queue=True, inventory=SimpleNamespace(reserve_workflow=reserve))
    try:
        head, = scheduler.submit_workflow(
            WorkflowSpec(workflow_id="head", nodes=[_node("head")])
        )["dispatched"]
        node = _node("material-action")
        node.material_requirements = [MaterialRequirement(template_id="sample", quantity=1)]
        queued = scheduler.submit_workflow(WorkflowSpec(workflow_id="queued", nodes=[node]))
        assert queued["state"] == "pending"
        assert queued["dispatched"] == []
        reserve.assert_not_called()

        next_task, = scheduler.on_job_finished(head["job_id"], True)["dispatched"]
        assert next_task["workflow_id"] == "queued"
        reserve.assert_called_once_with("queued", {"material-action": node.material_requirements})
    finally:
        scheduler.close()


def test_serial_queue_keeps_a_paused_head_until_canceled() -> None:
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, serial_queue=True)
    try:
        assert scheduler.submit_workflow(_step_chain_spec("head"))["dispatched"] == []
        assert scheduler.submit_workflow(_step_chain_spec("later-step"))["dispatched"] == []
        assert scheduler.submit_workflow(_chain_spec("queued", "device-b"))["dispatched"] == []

        assert scheduler.step_state("head")["can_step"] is True
        assert scheduler.step_state("later-step")["can_step"] is False
        with pytest.raises(ValueError, match="尚未轮到执行"):
            scheduler.step_workflow("later-step")
        assert scheduler.cancel_workflow("later-step")
        assert scheduler.cancel_workflow("head")
        scheduler._wake_reconcile()
        assert len(dispatcher.dispatched) == 1
    finally:
        scheduler.close()


def test_submit_only_wakes_reconcile_worker() -> None:
    """首次提交不得在 API/调用线程直接执行重排和物理派发。"""

    scheduler, _dispatcher = _make()
    submit_thread = threading.current_thread().ident
    reconcile_threads: list[int | None] = []
    original = scheduler._reschedule_locked

    def _recording_reschedule() -> list[dict[str, object]]:
        reconcile_threads.append(threading.current_thread().ident)
        return original()

    scheduler._reschedule_locked = _recording_reschedule  # type: ignore[method-assign]
    submitted = scheduler.submit_workflow(_chain_spec("wf-submit-wakeup"))

    assert [item["node_id"] for item in submitted["dispatched"]] == ["A"]
    assert reconcile_threads
    assert all(thread_id != submit_thread for thread_id in reconcile_threads)


def test_persistent_drain_scan_does_not_hold_the_scheduler_lock() -> None:
    """慢持久扫描不得阻塞设备状态等 Scheduler 内存读接口。"""

    scheduler, _dispatcher = _make()
    provider_started = threading.Event()
    release_provider = threading.Event()
    drain_finished = threading.Event()

    def blocking_provider() -> set[str]:
        provider_started.set()
        assert release_provider.wait(timeout=2)
        return {"persisted-job"}

    scheduler.set_drain_blocker_provider(blocking_provider)

    def read_drain() -> None:
        scheduler.drain_status()
        drain_finished.set()

    drain_thread = threading.Thread(target=read_drain)
    drain_thread.start()
    assert provider_started.wait(timeout=1)

    device_read_finished = threading.Event()

    def read_devices() -> None:
        scheduler.device_status()
        device_read_finished.set()

    device_thread = threading.Thread(target=read_devices)
    device_thread.start()
    try:
        assert device_read_finished.wait(timeout=0.5)
        assert not drain_finished.is_set()
    finally:
        release_provider.set()
        drain_thread.join(timeout=2)
        device_thread.join(timeout=2)

    assert drain_finished.is_set()


def test_transfer_node_type_is_canonical_but_not_executable_as_ilab():
    assert "Transfer" in NODE_TYPES
    node = WorkflowNode(id="transfer", node_type=normalize_node_type("transfer"))
    assert node.node_type == "Transfer"
    assert not node.is_ilab()


def test_physical_dispatch_requires_persistent_admission_authority() -> None:
    """物理执行适配器未装配持久准入权威时必须关闭失败。

    参数：无。返回：无；断言工作流不会越过执行边界。异常：调度器应抛稳定
    ``ExecutionPolicyError``，且执行适配器不得收到任何载荷。
    """

    dispatched: list[dict] = []
    scheduler = EdgeScheduler(
        dispatcher=CallbackDispatcher(lambda payload: dispatched.append(dict(payload))),
        device_target_resolver=lambda selector, _action, _busy: ResolvedDeviceTarget(
            local_device_id=str(selector["local_device_id"]),
            material_uuid=str(selector.get("material_uuid") or ""),
        ),
    )

    with pytest.raises(ExecutionPolicyError, match="持久派发准入权威未装配"):
        scheduler.submit_workflow(_chain_spec("wf-missing-dispatch-authority"))

    assert dispatched == []


def test_admission_authority_cannot_return_without_complete_permit() -> None:
    """持久准入权威未写入完整 Permit 时不能越过物理边界。

    参数：无。返回：无；断言缺少 Command、Claim 与 Fence 时不执行物理动作。
    异常：调度器必须报告凭据不完整，证明安全接口不能被布尔监听器绕过。
    """

    dispatched: list[dict] = []
    scheduler = EdgeScheduler(
        dispatcher=CallbackDispatcher(lambda payload: dispatched.append(dict(payload))),
        device_target_resolver=lambda selector, _action, _busy: ResolvedDeviceTarget(
            local_device_id=str(selector["local_device_id"]),
            material_uuid=str(selector.get("material_uuid") or ""),
        ),
    )
    scheduler.bind_dispatch_admission_authority(lambda _dispatching: True)

    with pytest.raises(ExecutionPolicyError, match="持久派发凭据不完整"):
        scheduler.submit_workflow(_chain_spec("wf-incomplete-dispatch-permit"))

    assert dispatched == []


def test_dispatch_admission_authority_is_single_assignment() -> None:
    """调度器只能绑定一个库存权威，禁止多监听器形成部分资源事务。

    参数：无。返回：无；断言第二个准入权威无法覆盖或追加。异常：重复绑定必须
    抛 ``ExecutionPolicyError``，第一个权威仍保持有效。
    """

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())

    def first(_dispatching: object) -> bool:
        return True

    scheduler.bind_dispatch_admission_authority(first)

    with pytest.raises(ExecutionPolicyError, match="已经绑定"):
        scheduler.bind_dispatch_admission_authority(lambda _dispatching: True)

    assert scheduler._dispatch_admission_authority is first


class TestTriggerOnSubmit:
    def test_submit_accepts_legacy_spec_without_trace_context(self) -> None:
        """本地覆盖的新调度器必须兼容旧基础镜像生成的 WorkflowSpec。"""

        scheduler, dispatcher = _make()
        spec = _chain_spec("wf-legacy-spec")
        if hasattr(spec, "trace_context"):
            del spec.trace_context

        result = scheduler.submit_workflow(spec)

        assert result["dispatched"][0]["node_id"] == "A"
        assert len(dispatcher.dispatched) == 1

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


def test_dynamic_device_selector_chooses_another_available_instance() -> None:
    """同一设备类型的并发节点应按本轮忙碌事实选择不同实例。

    参数：无。返回：无。断言工作流冻结的资源模板选择器在门禁阶段解析为具体
    本地设备，首个实例忙碌后选择第二个；派发参数只携带已选实例，不修改冻结
    工作流节点。异常：解析器收到非法选择器时由断言直接失败。
    """

    dispatcher = RecordingDispatcher()
    # ``device_instances`` 是注册表与库存权威共同证明的两个同类型设备实例。
    device_instances = (
        ResolvedDeviceTarget("reactor-a", "device-material-a"),
        ResolvedDeviceTarget("reactor-b", "device-material-b"),
    )

    def resolve_device(
        selector: dict[str, str],
        action_name: str,
        busy_keys: set[str],
    ) -> ResolvedDeviceTarget:
        """按稳定实例顺序返回本轮第一个未被设备键占用的目标。

        参数：``selector`` 是冻结类型选择器，``action_name`` 是目标动作，
        ``busy_keys`` 是当前在途设备/动作键。返回：首个可用实例。异常：测试输入
        合同或容量不足时抛 ``AssertionError``，不得伪造第三个设备。
        """

        assert selector == {
            "mode": "resource_template",
            "resource_template_uuid": "device-template-a",
        }
        assert action_name == "run"
        for instance in device_instances:
            if f"/devices/{instance.material_uuid}" not in busy_keys:
                return instance
        raise AssertionError("测试设备容量不足")

    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        device_target_resolver=resolve_device,
    )

    def dynamic_node(node_id: str) -> WorkflowNode:
        """构造只冻结设备类型而不冻结实例的工作流节点。

        参数：``node_id`` 是计划工作流节点稳定身份。返回：带资源模板选择器的
        动态设备动作。异常：无；选择器形状由测试固定。
        """

        return WorkflowNode(
            id=node_id,
            device_selector={
                "mode": "resource_template",
                "resource_template_uuid": "device-template-a",
            },
            action_name="run",
            action_type="goal",
            param={},
        )

    first = scheduler.submit_workflow(
        WorkflowSpec(workflow_id="wf-dynamic-a", nodes=[dynamic_node("node-a")])
    )
    second = scheduler.submit_workflow(
        WorkflowSpec(workflow_id="wf-dynamic-b", nodes=[dynamic_node("node-b")])
    )

    assert [first["dispatched"][0]["node_id"], second["dispatched"][0]["node_id"]] == [
        "node-a",
        "node-b",
    ]
    assert [item["device_id"] for item in dispatcher.dispatched] == [
        "reactor-a",
        "reactor-b",
    ]


def test_dynamic_device_wait_notifies_specific_busy_candidates() -> None:
    """调度等待事件必须把动态选择器已知的忙设备继续传给持久投影。"""

    waiting: list[dict] = []

    def resolve_device(
        _selector: dict[str, str],
        _action_name: str,
        _busy_keys: set[str],
    ) -> ResolvedDeviceTarget:
        raise DeviceTargetUnavailable(
            "device_busy",
            "匹配设备当前全部忙碌，等待下一轮调度",
            resources=(
                {"scope": "device", "device_id": "device-material-a"},
            ),
        )

    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        device_target_resolver=resolve_device,
    )
    scheduler.add_job_execution_wait_listener(waiting.append)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-dynamic-wait",
            nodes=[
                WorkflowNode(
                    id="node-wait",
                    device_selector={
                        "mode": "resource_template",
                        "resource_template_uuid": "device-template-a",
                    },
                    action_name="run",
                    action_type="goal",
                    param={},
                )
            ],
        )
    )

    assert result["dispatched"] == []
    assert waiting[0]["wait_resources"] == [
        {"scope": "device", "device_id": "device-material-a"},
    ]


def test_temporary_site_conflict_notifies_wait_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目标库位暂时占用时必须保留作业等待，不能让完成回调异常退出。"""

    waiting: list[dict] = []
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    scheduler.add_job_execution_wait_listener(waiting.append)

    def raise_occupied_site(*_args: object, **_kwargs: object) -> None:
        raise SiteTargetResolutionError(
            "site_occupied",
            "目标库位 R1C1 已被其他物料占用",
            resources=(
                {
                    "scope": "material_site",
                    "site_uuid": "site-r1c1",
                    "material_uuid": "blocking-material",
                },
            ),
        )

    monkeypatch.setattr(
        scheduler,
        "_resolve_transfer_site_target",
        raise_occupied_site,
    )

    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-site-occupied",
            nodes=[_node("return-reagent", device="robot")],
        )
    )

    assert result["state"] == "running"
    assert result["dispatched"] == []
    assert waiting == [
        {
            "job_id": waiting[0]["job_id"],
            "workflow_id": "wf-site-occupied",
            "node_id": "return-reagent",
            "resolved_args": {},
            "execution_locks": [],
            "blocking_job_id": None,
            "blocking_workflow_id": None,
            "wait_code": "site_occupied",
            "wait_message": "目标库位 R1C1 已被其他物料占用",
            "wait_resources": [
                {
                    "scope": "material_site",
                    "site_uuid": "site-r1c1",
                    "material_uuid": "blocking-material",
                },
            ],
        }
    ]


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

    def test_rejects_another_step_while_previous_job_is_in_flight(self):
        """单步闸门必须等当前 Job 终态，不能并发放行普通 DAG 分叉。"""

        scheduler, dispatcher = _make()
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-step-fork",
                nodes=[_node("A", "d1"), _node("B", "d2")],
                run_mode="step",
            )
        )

        with pytest.raises(ValueError, match="multiple step targets"):
            scheduler.step_workflow("wf-step-fork")

        scheduler.step_workflow("wf-step-fork", target_node_id="A")
        with pytest.raises(ValueError, match="step is still in progress"):
            scheduler.step_workflow("wf-step-fork", target_node_id="B")

        first_job = dispatcher.dispatched[0]["job_id"]
        scheduler.on_job_finished(first_job, success=True, ret_value={})
        second = scheduler.step_workflow("wf-step-fork", target_node_id="B")
        assert [item["node_id"] for item in second["dispatched"]] == ["B"]

    def test_backend_reports_authoritative_step_candidates(self):
        """候选集合由调度器 DAG 事实计算，普通分叉保持全部可选。"""

        scheduler, _dispatcher = _make()
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-step-candidates",
                nodes=[_node("A", "d1"), _node("B", "d2")],
                run_mode="step",
            )
        )

        state = scheduler.step_state("wf-step-candidates")

        assert state["execution_mode"] == "step"
        assert state["requires_selection"] is True
        assert [item["node_id"] for item in state["candidates"]] == ["A", "B"]

    def test_normal_to_step_drains_in_flight_job_before_pausing(self):
        """自动转单步先关闭新派发，待在途 Job 结束后才进入稳定暂停态。"""

        scheduler, dispatcher = _make()
        submitted = scheduler.submit_workflow(_chain_spec("wf-switch-to-step"))

        switching = scheduler.switch_to_step("wf-switch-to-step")
        assert switching["execution_mode"] == "switching_to_step"
        assert switching["in_flight_job_count"] == 1

        scheduler.on_job_finished(
            submitted["dispatched"][0]["job_id"], success=True, ret_value={}
        )
        state = scheduler.step_state("wf-switch-to-step")
        assert state["execution_mode"] == "step"
        assert [item["node_id"] for item in state["candidates"]] == ["B"]
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A"]

    def test_step_to_normal_continues_same_workflow(self):
        """稳定暂停的单步任务可以沿用已完成事实恢复自动调度。"""

        scheduler, dispatcher = _make()
        scheduler.submit_workflow(_step_chain_spec("wf-continue-auto"))
        first = scheduler.step_workflow("wf-continue-auto")
        scheduler.on_job_finished(
            first["dispatched"][0]["job_id"], success=True, ret_value={}
        )

        resumed = scheduler.continue_automatic("wf-continue-auto")

        assert resumed["execution_mode"] == "normal"
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A", "B"]


class TestPauseResumeControl:
    def test_pause_stops_future_dispatch_and_resume_continues_same_run(self):
        scheduler, dispatcher = _make()
        submitted = scheduler.submit_workflow(_chain_spec("wf-paused"))

        paused = scheduler.pause_workflow("wf-paused")
        assert paused["state"] == "running"
        assert paused["execution_mode"] == "normal"
        assert paused["dispatch_gate"] == "paused"

        settled = scheduler.on_job_finished(
            submitted["dispatched"][0]["job_id"], success=True, ret_value={}
        )
        assert settled["workflow_state"] == "running"
        assert settled["dispatched"] == []
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A"]

        resumed = scheduler.resume_workflow("wf-paused")
        assert resumed["state"] == "running"
        assert [item["node_id"] for item in resumed["dispatched"]] == ["B"]
        assert [item["node_id"] for item in dispatcher.dispatched] == ["A", "B"]


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

    def test_finish_callback_only_wakes_reconcile_worker(self) -> None:
        """设备完成回调不得在回调线程/持锁栈内直接执行重排和派发。"""

        scheduler, _dispatcher = _make()
        submitted = scheduler.submit_workflow(_chain_spec("wf-wakeup"))
        callback_thread = threading.current_thread().ident
        reconcile_threads: list[int | None] = []
        original = scheduler._reschedule_locked

        def _recording_reschedule() -> list[dict[str, object]]:
            reconcile_threads.append(threading.current_thread().ident)
            return original()

        scheduler._reschedule_locked = _recording_reschedule  # type: ignore[method-assign]
        result = scheduler.on_job_finished(
            submitted["dispatched"][0]["job_id"],
            True,
        )

        assert [item["node_id"] for item in result["dispatched"]] == ["B"]
        assert reconcile_threads
        assert all(thread_id != callback_thread for thread_id in reconcile_threads)

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
        """相同有效优先级按提交时间与稳定身份排序。"""

        orderer = StableLocalOrderer(clock=lambda: 2.0)
        from unilabos.app.scheduler.models import ReadyTask

        t1 = ReadyTask("wf1", _node("A"), priority_weight=50.0, submitted_at=1.0)
        t2 = ReadyTask("wf2", _node("A"), priority_weight=200.0, submitted_at=2.0)
        t3 = ReadyTask("wf3", _node("A"), priority_weight=200.0, submitted_at=1.5)
        ordered = orderer.order([t1, t2, t3], OrderingContext(set()))
        assert [t.workflow_id for t in ordered] == ["wf3", "wf2", "wf1"]

    def test_waiting_task_ages_without_preempting_running_action(self):
        """等待候选按固定周期提高有效优先级，且排序器不触碰在途动作。

        参数无。返回无；断言等待六个周期的低优先级候选可以超过新到候选。
        异常：老化公式或稳定排序变化会使测试失败。
        """

        orderer = StableLocalOrderer(
            aging_interval_seconds=30,
            clock=lambda: 180.0,
        )
        from unilabos.app.scheduler.models import ReadyTask

        aged = ReadyTask(
            "wf-aged",
            _node("A"),
            priority_weight=50.0,
            submitted_at=0.0,
        )
        fresh = ReadyTask(
            "wf-fresh",
            _node("A"),
            priority_weight=55.0,
            submitted_at=180.0,
        )

        ordered = orderer.order([fresh, aged], OrderingContext(set()))

        assert [task.workflow_id for task in ordered] == ["wf-aged", "wf-fresh"]


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
    """manual_confirm 先占资源，批准后才以同一 Job 派发真实设备动作。"""

    def _manual_spec(self, workflow_id: str) -> WorkflowSpec:
        manual = WorkflowNode(
            id="M",
            device_id="operator",
            action_name="confirm",
            action_type="goal",
            param={"target_volume": 10},
            node_type="manual_confirm",
            manual_confirmation={"timeout_seconds": 3600},
        )
        return WorkflowSpec(
            workflow_id=workflow_id,
            nodes=[_node("A"), manual, _node("B")],
            edges=[_edge("A", "M"), _edge("M", "B")],
        )

    def test_manual_confirm_parks_without_dispatch(self) -> None:
        scheduler, dispatcher = _make()
        scheduler.submit_workflow(self._manual_spec("wf-manual"))
        job_a = dispatcher.dispatched[0]["job_id"]
        scheduler.on_job_finished(job_a, True, {}, "normal")
        # M 已占用执行槽并等待人工批准，但执行器只收到过 A。
        assert len(dispatcher.dispatched) == 1
        snap = scheduler.workflow_snapshot("wf-manual")
        assert snap["nodes"]["M"]["state"] == "dispatched"
        manual_job = snap["nodes"]["M"].get("job_id")
        assert manual_job

    def test_approve_dispatches_same_job_then_device_result_releases_downstream(
        self,
    ) -> None:
        scheduler, dispatcher = _make()
        scheduler.submit_workflow(self._manual_spec("wf-manual2"))
        scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], True, {}, "normal")
        manual_job = scheduler.workflow_snapshot("wf-manual2")["nodes"]["M"]["job_id"]
        decision = scheduler.resolve_manual_confirmation(manual_job, approved=True)
        assert decision["dispatched"][0]["job_id"] == manual_job
        assert dispatcher.dispatched[-1]["job_id"] == manual_job
        assert dispatcher.dispatched[-1]["action"] == "confirm"
        assert dispatcher.dispatched[-1]["action_args"] == {"target_volume": 10}
        scheduler.on_job_finished(manual_job, True, {"confirmed": True}, "normal")
        assert dispatcher.dispatched[-1]["action"] == "run"
        scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], True, {}, "normal")
        assert scheduler.workflow_snapshot("wf-manual2")["state"] == "success"

    def test_manual_confirmation_waits_for_busy_device(self) -> None:
        scheduler, dispatcher = _make()
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-busy",
                nodes=[_node("A", device="operator", action="confirm")],
            )
        )
        manual_only = WorkflowSpec(
            workflow_id="wf-manual3",
            nodes=[
                WorkflowNode(
                    id="M",
                    device_id="operator",
                    action_name="confirm",
                    node_type="manual_confirm",
                    manual_confirmation={"timeout_seconds": 3600},
                )
            ],
        )
        scheduler.submit_workflow(manual_only)
        snap = scheduler.workflow_snapshot("wf-manual3")
        assert snap["nodes"]["M"]["state"] == "ready"
        scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], True, {}, "normal")
        assert scheduler.workflow_snapshot("wf-manual3")["nodes"]["M"]["state"] == "dispatched"

    def test_manual_confirmation_occupies_device_until_action_finishes(self) -> None:
        """人工节点即使包装 always_free 动作，也必须独占设备直到动作终态。"""

        scheduler, dispatcher = _make()
        manual_only = WorkflowSpec(
            workflow_id="wf-manual-free",
            nodes=[
                WorkflowNode(
                    id="manual-node",
                    device_id="operator",
                    action_name="confirm",
                    node_type="manual_confirm",
                    manual_confirmation={"timeout_seconds": 3600},
                    always_free=True,
                )
            ],
        )
        manual_result = scheduler.submit_workflow(manual_only)
        manual_job = manual_result["dispatched"][0]["job_id"]
        ordinary_result = scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="wf-ordinary-after-manual",
                nodes=[_node("ordinary-node", device="operator", action="confirm")],
            )
        )
        assert ordinary_result["dispatched"] == []
        scheduler.resolve_manual_confirmation(manual_job, approved=True)
        scheduler.on_job_finished(manual_job, True, {}, "normal")
        assert dispatcher.dispatched[-1]["node_id"] == "ordinary-node"
