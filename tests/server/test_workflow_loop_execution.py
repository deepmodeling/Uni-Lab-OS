"""循环容器节点：@workflow 声明 -> 模板 -> 实例化 -> 本机调度器逐轮执行。

覆盖 for（迭代变量占位符）、while（设备状态 / 节点输出条件）、安全上限、循环体失败、
嵌套循环，以及图校验与执行计划里的层级信息。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, Optional

import pytest

from unilabos.registry.workflows import (
    DeviceCatalog,
    WorkflowBuildContext,
    build_workflow_template_payload,
    clear_registered_workflows,
    get_registered_workflows,
    materialize_workflow_template,
    upsert_workflow,
    workflow,
)
from unilabos.server.services.runtime.workflow.service import WorkflowError, WorkflowService


@pytest.fixture(autouse=True)
def _isolated_workflow_registry():
    snapshot = get_registered_workflows()
    clear_registered_workflows()
    yield
    clear_registered_workflows()
    from unilabos.registry.workflows import _registered_workflows

    for definition in snapshot.values():
        _registered_workflows[definition.uuid] = definition


class LoopDriver:
    """记录动作参数以断言轮次与顺序（须为模块级类，供类路径实例化）。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.fail_on: Optional[Any] = None

    def record(self, value: Any = None, label: str = "") -> dict:
        if self.fail_on is not None and value == self.fail_on:
            raise RuntimeError(f"boom at {value}")
        self.calls.append(value if not label else (label, value))
        return {"value": value, "label": label, "count": len(self.calls)}

    def probe(self) -> dict:
        return {"ready": len(self.calls) >= 3, "count": len(self.calls)}


class _Stack:
    """HostLink 执行栈 + 本机调度器；device_state 由测试注入的读取器提供。"""

    def __init__(self, device_state_reader: Optional[Callable[[str], Dict[str, Any]]] = None) -> None:
        from unilabos.backend.hostlink.backend import HostLinkBackend
        from unilabos.backend.hostlink.host_node import HostNode
        from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
        from unilabos.server.backend.execution import JobExecutionBackend
        from unilabos.server.backend.scheduler.service import BackendScheduler

        self.local = HostLinkLocalRuntime()
        self.node = self.local.add_driver(
            HostLinkDriverSpec(
                device_id="loop-device",
                driver_class=LoopDriver,
                config={},
                action_names=("record", "probe"),
                action_value_mappings={
                    "record": {"type": "UniLabJsonCommand"},
                    "probe": {"type": "UniLabJsonCommand"},
                },
            )
        )
        self.runtime = HostLinkBackend(self.local, is_slave=False)
        self.local.start()
        self.adapter = HostNode("host_node", self.runtime, bridges=[])
        self.microbackend = JobExecutionBackend(host_node_getter=lambda: self.adapter)
        self.adapter.bridges = [self.microbackend]
        self.microbackend.start()
        self.service = WorkflowService(":memory:")
        self.scheduler = BackendScheduler(
            self.service, self.microbackend, device_state_reader=device_state_reader
        )
        self.service.set_task_submitter(self.scheduler.submit)
        self.scheduler.start(recover=True)

    @property
    def driver(self) -> LoopDriver:
        return self.node.driver

    def close(self) -> None:
        from unilabos.backend.hostlink.adapter_registry import clear_execution_adapter
        from unilabos.backend.hostlink.host_node import HostNode

        self.service.set_task_submitter(None)
        self.scheduler.stop()
        self.service.close()
        clear_execution_adapter(self.adapter)
        self.microbackend.stop()
        self.adapter.stop()
        HostNode.reset_state()
        self.runtime.stop()

    def run(self, timeout: float = 8.0) -> Dict[str, Any]:
        definition = next(iter(get_registered_workflows().values()))
        catalog = DeviceCatalog()
        catalog.add("loop-device", "loop_demo_class", str(uuid.uuid4()))
        template = build_workflow_template_payload(definition)
        record = upsert_workflow(self.service, materialize_workflow_template(template, catalog))
        task = self.service.create_workflow_task(
            workflow_uuid=record["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        deadline = time.monotonic() + timeout
        current = self.service.get_workflow_task(task["uuid"])
        while current["status"] not in {"succeeded", "failed", "canceled"}:
            if time.monotonic() >= deadline:
                pytest.fail(f"workflow task 未在时限内结束: {current['status']}")
            time.sleep(0.02)
            current = self.service.get_workflow_task(task["uuid"])
        return current

    def runs_by_name(self, task_uuid: str) -> Dict[str, Dict[str, Any]]:
        snapshot = {
            node["uuid"]: node
            for node in self.service.get_workflow_task(task_uuid)["workflow_snapshot"]["nodes"]
        }
        return {
            snapshot[run["workflow_node_uuid"]]["name"]: run
            for run in self.service.list_workflow_node_runs(task_uuid)
        }


@pytest.fixture
def stack():
    instance = _Stack()
    try:
        yield instance
    finally:
        instance.close()


def test_static_condition_uses_action_result_and_joins_after_skipped_branch(stack: _Stack) -> None:
    @workflow(display_name="返回值分支", compiler="ast")
    def flow(ctx):
        result = ctx.run("loop-device/record", {"value": "probe"}, name="测量")
        if result["count"] >= 1 and not result["value"] == "bad":
            ctx.run("loop-device/record", {"value": "selected"}, name="选择")
        else:
            ctx.run("loop-device/record", {"value": "wrong"}, name="跳过")
        ctx.run("loop-device/record", {"value": "joined"}, name="汇合")

    task = stack.run()
    assert task["status"] == "succeeded", task["error_info"]
    assert stack.driver.calls == ["probe", "selected", "joined"]
    runs = stack.runs_by_name(task["uuid"])
    assert runs["跳过"]["status"] == "skipped"
    condition = next(run for run in runs.values() if run["executor_kind"] == "condition")
    assert condition["return_info"]["return_value"] == {"matched": True}


def test_static_condition_rechecks_current_attempt_inside_loop(stack: _Stack) -> None:
    @workflow(display_name="逐轮条件", compiler="ast")
    def flow(ctx):
        with ctx.loop_for(4, name="四轮"):
            result = ctx.run("loop-device/probe", {}, name="检测")
            if result.ready:
                ctx.run("loop-device/record", {"value": "done"}, name="达标")
            else:
                ctx.run("loop-device/record", {"value": "{{loop.index}}"}, name="处理")

    task = stack.run()
    assert task["status"] == "succeeded", task["error_info"]
    assert stack.driver.calls == [0, 1, 2, "done"]
    runs = stack.runs_by_name(task["uuid"])
    assert [attempt["status"] for attempt in runs["达标"]["attempts"]] == ["skipped"] * 3 + ["succeeded"]
    assert [attempt["status"] for attempt in runs["处理"]["attempts"]] == ["succeeded"] * 3 + ["skipped"]
    condition = next(run for run in runs.values() if run["executor_kind"] == "condition")
    assert [attempt["return_info"]["return_value"]["matched"] for attempt in condition["attempts"]] == [False, False, False, True]


def test_static_condition_skips_nested_condition_and_loop_body(stack: _Stack) -> None:
    @workflow(display_name="嵌套跳过", compiler="ast")
    def flow(ctx):
        result = ctx.run("loop-device/probe", {}, name="检测")
        if result.ready:
            if result.missing:
                ctx.run("loop-device/record", {"value": "wrong"}, name="不可达")
            with ctx.loop_for(2, name="跳过循环"):
                ctx.run("loop-device/record", {"value": "wrong"}, name="跳过循环体")
        elif result.count == 0:
            ctx.run("loop-device/record", {"value": "fallback"}, name="兜底")
        else:
            ctx.run("loop-device/record", {"value": "wrong"}, name="跳过 else")
        ctx.run("loop-device/record", {"value": "end"}, name="结束")

    task = stack.run()
    assert task["status"] == "succeeded", task["error_info"]
    assert stack.driver.calls == ["fallback", "end"]
    runs = stack.runs_by_name(task["uuid"])
    for name in ("不可达", "跳过循环", "跳过循环体", "跳过 else"):
        assert runs[name]["status"] == "skipped"


@pytest.mark.parametrize("bad_field", ["missing", "count"])
def test_static_condition_rejects_missing_or_non_boolean_result(stack: _Stack, bad_field: str) -> None:
    @workflow(display_name="非法条件", compiler="ast")
    def flow(ctx):
        result = ctx.run("loop-device/probe", {}, name="检测")
        if result["missing"]:
            ctx.run("loop-device/record", {"value": "wrong"}, name="不能执行")

    # 用同一份静态图测试两个运行时数据错误。
    definition = next(iter(get_registered_workflows().values()))
    template = build_workflow_template_payload(definition)
    template["nodes"][1]["param"]["expression"]["name"] = bad_field
    catalog = DeviceCatalog()
    catalog.add("loop-device", "loop_demo_class", str(uuid.uuid4()))
    record = upsert_workflow(stack.service, materialize_workflow_template(template, catalog))
    task = stack.service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
                                            target_node_uuid=None, input_value={}, description=None, meta_data={})
    deadline = time.monotonic() + 5
    while stack.service.get_workflow_task(task["uuid"])["status"] not in {"failed", "succeeded"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert stack.service.get_workflow_task(task["uuid"])["status"] == "failed"
    assert stack.driver.calls == []
    condition = next(run for run in stack.runs_by_name(task["uuid"]).values() if run["executor_kind"] == "condition")
    assert condition["error_info"][0]["code"] == "condition_failed"


def test_static_compiler_never_executes_unsupported_author_code() -> None:
    effects = []

    @workflow(display_name="拒绝执行作者代码", compiler="ast")
    def flow(ctx):
        effects.append("executed")
        ctx.run("loop-device/probe", {})

    definition = next(iter(get_registered_workflows().values()))
    with pytest.raises(ValueError, match="仅支持"):
        definition.collect_steps()
    assert effects == []


def test_static_compiler_rejects_branch_result_leaking_to_join() -> None:
    @workflow(display_name="拒绝跨分支引用", compiler="ast")
    def flow(ctx):
        if True:
            selected = ctx.run("loop-device/probe", {})
        if selected.ready:
            ctx.run("loop-device/record", {})

    definition = next(iter(get_registered_workflows().values()))
    with pytest.raises(ValueError, match="作用域外"):
        definition.collect_steps()


def test_condition_and_skipped_branch_do_not_consume_device_step(stack: _Stack) -> None:
    @workflow(display_name="条件单步", compiler="ast")
    def flow(ctx):
        if False:
            ctx.run("loop-device/record", {"value": "wrong"}, name="跳过")
        else:
            ctx.run("loop-device/record", {"value": "chosen"}, name="选中")

    catalog = DeviceCatalog()
    catalog.add("loop-device", "loop_demo_class", str(uuid.uuid4()))
    definition = next(iter(get_registered_workflows().values()))
    record = upsert_workflow(stack.service, materialize_workflow_template(build_workflow_template_payload(definition), catalog))
    task = stack.service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="step",
                                            target_node_uuid=None, input_value={}, description=None, meta_data={})
    time.sleep(0.05)
    assert stack.driver.calls == []
    current = stack.service.get_workflow_task(task["uuid"])
    stack.service.command_workflow_task(task["uuid"], command_type="step",
                                       expected_revision=current["control_revision"], idempotency_key="condition-step")
    deadline = time.monotonic() + 5
    while stack.service.get_workflow_task(task["uuid"])["status"] != "succeeded":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert stack.driver.calls == ["chosen"]
    assert stack.runs_by_name(task["uuid"])["跳过"]["status"] == "skipped"


@pytest.mark.parametrize("settled", [False, True])
def test_condition_recovers_from_persisted_plan_and_result(tmp_path, settled: bool) -> None:
    @workflow(display_name="条件恢复", compiler="ast")
    def flow(ctx):
        if True:
            ctx.run("loop-device/record", {}, name="动作")

    path = str(tmp_path / "workflow.db")
    service = WorkflowService(path)
    service.set_task_submitter(lambda _: None)
    catalog = DeviceCatalog()
    catalog.add("loop-device", "loop_demo_class", str(uuid.uuid4()))
    definition = next(iter(get_registered_workflows().values()))
    payload = materialize_workflow_template(build_workflow_template_payload(definition), catalog)
    record = upsert_workflow(service, payload)
    task = service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
                                        target_node_uuid=None, input_value={}, description=None, meta_data={})
    prepared = service.prepare_workflow_task_execution(task["uuid"])
    run = next(run for run in prepared["runs"] if run["executor_kind"] == "condition")
    service.mark_workflow_node_job_running(run["current_job_uuid"])
    if settled:
        service.record_workflow_node_job_terminal(run["current_job_uuid"], status="succeeded",
                                                 return_info={"return_value": {"matched": True}}, error_info=[])
    # 修改的是定义；已创建任务仍持有最初的条件计划。
    payload["nodes"][0]["param"]["expression"] = {"lit": False}
    upsert_workflow(service, payload)
    service.close()
    restored = WorkflowService(path)
    try:
        recovery = restored.prepare_workflow_task_execution(task["uuid"])
        assert recovery["state"] != "waiting_reconciliation"
        current = restored.get_workflow_node_run(run["uuid"])
        assert current["status"] == ("succeeded" if settled else "pending")
        assert current["attempt_count"] == 1
        if settled:
            assert current["return_info"]["return_value"] == {"matched": True}
        planned = next(node for node in recovery["task"]["execution_plan"]["nodes"] if node["kind"] == "condition")
        assert planned["param"]["expression"] == {"lit": True}
    finally:
        restored.close()


def test_step_mode_executes_one_hostlink_leaf_inside_nested_loops(stack: _Stack) -> None:
    """一次 step 不代表跑完整个循环：真实 HostLink 驱动每次只能收到一个叶动作。"""
    @workflow(display_name="嵌套逐步")
    def flow(ctx: WorkflowBuildContext) -> None:
        with ctx.loop_for(2, name="外层"):
            with ctx.loop_for(2, name="内层"):
                ctx.run("loop-device/record", {"value": "{{loop.index}}"}, name="记录")

    definition = next(iter(get_registered_workflows().values()))
    catalog = DeviceCatalog()
    catalog.add("loop-device", "loop_demo_class", str(uuid.uuid4()))
    record = upsert_workflow(stack.service, materialize_workflow_template(build_workflow_template_payload(definition), catalog))
    task = stack.service.create_workflow_task(
        workflow_uuid=record["uuid"], run_mode="step", target_node_uuid=None,
        input_value={}, description=None, meta_data={},
    )
    time.sleep(0.05)
    assert stack.driver.calls == []
    for count in range(1, 5):
        current = stack.service.get_workflow_task(task["uuid"])
        stack.service.command_workflow_task(task["uuid"], command_type="step",
                                           expected_revision=current["control_revision"],
                                           idempotency_key=f"loop-click-{count}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = stack.service.get_workflow_task(task["uuid"])
            if len(stack.driver.calls) == count and current["control_status"] == "paused":
                break
            time.sleep(0.01)
        time.sleep(0.04)
        assert len(stack.driver.calls) == count
        assert current["control_status"] == "paused"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and stack.service.get_workflow_task(task["uuid"])["status"] != "succeeded":
        time.sleep(0.01)
    assert stack.service.get_workflow_task(task["uuid"])["status"] == "succeeded"
    assert stack.driver.calls == [0, 1, 0, 1]
    runs = stack.runs_by_name(task["uuid"])
    assert runs["记录"]["attempt_count"] == 4
    assert len(stack.service.list_workflow_tasks(page=1, page_size=10)["items"]) == 1


def test_for_loop_runs_the_body_per_iteration_with_loop_variables(stack: _Stack) -> None:
    @workflow(display_name="for 循环")
    def flow(ctx: WorkflowBuildContext) -> None:
        ctx.run("loop-device/record", {"value": "start"}, name="开始")
        with ctx.loop_for(3, name="三轮"):
            ctx.run("loop-device/record", {"value": "{{loop.index}}", "label": "r{{loop.iteration}}/{{loop.count}}"}, name="记录")
        ctx.run("loop-device/record", {"value": "end"}, name="结束")

    task = stack.run()
    assert task["status"] == "succeeded", task["error_info"]
    # 占位符：整串占位符保留 int 类型，嵌入式做文本替换；循环体按轮执行、前后步骤各一次
    assert stack.driver.calls == ["start", ("r1/3", 0), ("r2/3", 1), ("r3/3", 2), "end"]

    runs = stack.runs_by_name(task["uuid"])
    loop_run = runs["三轮"]
    assert loop_run["status"] == "succeeded"
    assert loop_run["return_info"]["return_value"]["iterations"] == 3
    assert loop_run["control_data"]["loop"]["iteration"] == 2
    assert loop_run["control_data"]["loop"]["count"] == 3
    body_run = runs["记录"]
    assert body_run["status"] == "succeeded"
    # 每轮一个 attempt：首轮 initial，其后 loop_iteration
    assert [attempt["trigger"] for attempt in body_run["attempts"]] == [
        "initial",
        "loop_iteration",
        "loop_iteration",
    ]
    assert body_run["attempt_count"] == 3
    # 任务输出取节点最后一轮结果
    assert task["output"][body_run["workflow_node_uuid"]]["return_value"]["value"] == 2


def test_while_loop_polls_device_state_until_the_condition_turns_false() -> None:
    state: Dict[str, Any] = {"progress": 0}

    def reader(device_id: str) -> Dict[str, Any]:
        assert device_id == "loop-device"
        return {"progress": {"value": state["progress"], "updated_at": 0}}

    stack = _Stack(device_state_reader=reader)
    try:

        @workflow(display_name="while 设备状态")
        def flow(ctx: WorkflowBuildContext) -> None:
            with ctx.loop_while(
                ctx.device_state("loop-device", "progress", "<", 3), name="等进度"
            ):
                ctx.run("loop-device/record", {"value": "tick"}, name="推进")

        # 每次循环体执行后设备状态推进一格（模拟设备上报）
        original = stack.driver.record

        def record(value: Any = None, label: str = "") -> dict:
            result = original(value, label)
            state["progress"] += 1
            return result

        stack.driver.record = record  # type: ignore[method-assign]
        task = stack.run()
        assert task["status"] == "succeeded", task["error_info"]
        assert stack.driver.calls == ["tick", "tick", "tick"]
        loop_run = stack.runs_by_name(task["uuid"])["等进度"]
        assert loop_run["return_info"]["return_value"]["iterations"] == 3
        assert loop_run["control_data"]["loop"]["condition"] == "loop-device.progress < 3"
    finally:
        stack.close()


def test_while_loop_repeats_until_a_body_step_reports_ready(stack: _Stack) -> None:
    """条件引用循环体里的探测步骤：首轮前没有产出 => 先跑一轮（do-while 语义），
    之后每轮看最新探测结果。"""

    @workflow(display_name="重复直到就绪")
    def flow(ctx: WorkflowBuildContext) -> None:
        with ctx.loop_while(ctx.step_output("探测", "ready", "==", False), name="直到就绪") as loop:
            ctx.run("loop-device/record", {"value": "work"}, name="干活")
            ctx.run("loop-device/probe", {}, name="探测")
        assert loop.is_loop
        ctx.run("loop-device/record", {"value": "done"}, name="收尾")

    task = stack.run()
    assert task["status"] == "succeeded", task["error_info"]
    # probe 在 record 满 3 次后 ready=True：干活 3 轮，然后收尾
    assert stack.driver.calls == ["work", "work", "work", "done"]
    runs = stack.runs_by_name(task["uuid"])
    assert runs["直到就绪"]["return_info"]["return_value"]["iterations"] == 3
    assert runs["探测"]["attempt_count"] == 3
    assert runs["探测"]["return_info"]["return_value"]["ready"] is True


def test_step_output_by_name_must_resolve_to_exactly_one_step() -> None:
    with pytest.raises(ValueError, match="找不到该步骤"):

        @workflow(display_name="坏引用")
        def flow(ctx: WorkflowBuildContext) -> None:
            with ctx.loop_while(ctx.step_output("不存在", "ready", "==", False)):
                ctx.run("loop-device/record", {}, name="干活")

        build_workflow_template_payload(next(iter(get_registered_workflows().values())))


def test_while_loop_hits_max_iterations_and_fails_the_task(stack: _Stack) -> None:
    @workflow(display_name="死循环保护")
    def flow(ctx: WorkflowBuildContext) -> None:
        first = ctx.run("loop-device/record", {"value": "seed"}, name="种子")
        with ctx.loop_while(
            ctx.step_output(first, "value", "==", "seed"), max_iterations=2, name="永真"
        ):
            ctx.run("loop-device/record", {"value": "spin"}, name="空转")

    task = stack.run()
    assert task["status"] == "failed"
    loop_run = stack.runs_by_name(task["uuid"])["永真"]
    assert loop_run["status"] == "failed"
    assert loop_run["error_info"][0]["code"] == "loop_failed"
    assert "max_iterations=2" in loop_run["error_info"][0]["message"]
    assert loop_run["return_info"]["return_value"]["iterations"] == 2
    assert stack.driver.calls == ["seed", "spin", "spin"]


def test_body_failure_fails_the_loop_and_the_task(stack: _Stack) -> None:
    @workflow(display_name="循环体失败")
    def flow(ctx: WorkflowBuildContext) -> None:
        with ctx.loop_for(3, name="三轮"):
            ctx.run("loop-device/record", {"value": "{{loop.index}}"}, name="记录")
        ctx.run("loop-device/record", {"value": "never"}, name="不该执行")

    stack.driver.fail_on = 1
    task = stack.run()
    assert task["status"] == "failed"
    runs = stack.runs_by_name(task["uuid"])
    assert stack.driver.calls == [0]
    assert runs["三轮"]["status"] == "failed"
    assert runs["三轮"]["return_info"]["return_value"] == {"iterations": 1, "failed_iteration": 2}
    assert runs["记录"]["status"] == "failed"
    assert runs["不该执行"]["status"] == "canceled"


def test_nested_loops_reset_the_inner_iteration_each_outer_round(stack: _Stack) -> None:
    @workflow(display_name="嵌套循环")
    def flow(ctx: WorkflowBuildContext) -> None:
        with ctx.loop_for(2, name="外层"):
            ctx.run("loop-device/record", {"value": "outer-{{loop.index}}"}, name="外层步")
            with ctx.loop_for(2, name="内层"):
                ctx.run("loop-device/record", {"value": "inner-{{loop.index}}"}, name="内层步")

    task = stack.run()
    assert task["status"] == "succeeded", task["error_info"]
    assert stack.driver.calls == [
        "outer-0",
        "inner-0",
        "inner-1",
        "outer-1",
        "inner-0",
        "inner-1",
    ]
    runs = stack.runs_by_name(task["uuid"])
    assert runs["内层"]["attempt_count"] == 2  # 外层每轮重臂一次
    assert runs["内层步"]["attempt_count"] == 4
    assert runs["外层"]["return_info"]["return_value"]["iterations"] == 2


def test_execution_plan_records_the_loop_hierarchy_and_order() -> None:
    service = WorkflowService(":memory:")
    try:

        @workflow(display_name="计划层级")
        def flow(ctx: WorkflowBuildContext) -> None:
            ctx.run("loop-device/record", {"value": 1}, name="a")
            with ctx.loop_for(2, name="L"):
                ctx.run("loop-device/record", {"value": 2}, name="b")
                ctx.run("loop-device/record", {"value": 3}, name="c")
            ctx.run("loop-device/record", {"value": 4}, name="d")

        definition = next(iter(get_registered_workflows().values()))
        template = build_workflow_template_payload(definition)
        assert [node["kind"] for node in template["nodes"]] == ["action", "loop", "action", "action", "action"]
        assert template["nodes"][2]["parent"] == template["nodes"][1]["key"]
        # 顶层 a -> L -> d，循环体 b -> c；容器与自己的循环体之间没有边
        assert template["edges"] == [
            {"source": "step-0", "target": "step-1"},
            {"source": "step-2", "target": "step-3"},
            {"source": "step-1", "target": "step-4"},
        ]
        catalog = DeviceCatalog()
        catalog.add("loop-device", "loop_demo_class", str(uuid.uuid4()))
        materialized = materialize_workflow_template(template, catalog)
        loop_node = next(node for node in materialized["nodes"] if node["type"] == "loop")
        body = [node for node in materialized["nodes"] if node.get("parent_uuid") == loop_node["uuid"]]
        assert [node["name"] for node in body] == ["b", "c"]
        record = upsert_workflow(service, materialized)
        service.set_task_submitter(None)
        task = service.create_workflow_task(
            workflow_uuid=record["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        plan_nodes = task["execution_plan"]["nodes"]
        names = {node["uuid"]: node["name"] for node in materialized["nodes"]}
        assert [names[node["uuid"]] for node in plan_nodes] == ["a", "L", "b", "c", "d"]
        assert [node["kind"] for node in plan_nodes] == [
            "device_action",
            "loop",
            "device_action",
            "device_action",
            "device_action",
        ]
        assert plan_nodes[2]["parent_uuid"] == loop_node["uuid"]
        assert "parent_uuid" not in plan_nodes[4]
        runs = service.list_workflow_node_runs(task["uuid"])
        assert [run["executor_kind"] for run in runs] == [
            "device_action",
            "loop",
            "device_action",
            "device_action",
            "device_action",
        ]
    finally:
        service.close()


def test_restart_resets_in_flight_loop_attempt_instead_of_asking_for_reconciliation() -> None:
    """循环容器没有设备副作用：进程重启时在飞的循环 attempt 退回 pending 续跑，
    只有循环体里在飞的设备 attempt 才转 execution_unknown 等人裁决。"""

    service = WorkflowService(":memory:")
    try:

        @workflow(display_name="重启恢复")
        def flow(ctx: WorkflowBuildContext) -> None:
            with ctx.loop_for(3, name="L"):
                ctx.run("loop-device/record", {"value": "{{loop.index}}"}, name="b")

        definition = next(iter(get_registered_workflows().values()))
        catalog = DeviceCatalog()
        catalog.add("loop-device", "loop_demo_class", str(uuid.uuid4()))
        record = upsert_workflow(
            service, materialize_workflow_template(build_workflow_template_payload(definition), catalog)
        )
        task = service.create_workflow_task(
            workflow_uuid=record["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        runs = {run["executor_kind"]: run for run in service.list_workflow_node_runs(task["uuid"])}
        loop_run, body_run = runs["loop"], runs["device_action"]
        # 模拟：第 2 轮进行中，循环 attempt running，循环体 attempt 也 running
        service.mark_workflow_node_job_running(loop_run["current_job_uuid"])
        service.mark_workflow_node_job_running(body_run["current_job_uuid"])
        service.record_workflow_node_job_terminal(
            body_run["current_job_uuid"], status="succeeded", return_info={"return_value": {"value": 0}}
        )
        outcome = service.begin_workflow_loop_iteration(
            loop_run["uuid"],
            iteration=1,
            progress={"mode": "for", "count": 3},
            body_run_uuids=[body_run["uuid"]],
        )
        next_job = outcome["next_jobs"][body_run["uuid"]]
        assert next_job["trigger"] == "loop_iteration"
        assert next_job["attempt_no"] == 2
        assert outcome["run"]["control_data"]["loop"] == {"mode": "for", "count": 3, "iteration": 1}
        assert outcome["run"]["return_info"]["return_value"] == {"index": 1, "iteration": 2, "count": 3}
        service.mark_workflow_node_job_running(next_job["uuid"])

        # 新进程接管
        prepared = service.prepare_workflow_task_execution(task["uuid"])
        assert prepared["state"] == "waiting_reconciliation"
        by_kind = {run["executor_kind"]: run for run in prepared["runs"]}
        assert by_kind["loop"]["status"] == "pending"  # 循环续跑，不裁决
        assert by_kind["loop"]["control_data"]["loop"]["iteration"] == 1
        assert by_kind["device_action"]["status"] == "execution_unknown"  # 设备 attempt 要裁决
    finally:
        service.close()


def _loop_graph_nodes(*, loop_param: Dict[str, Any], body: bool = True, extra: Optional[list] = None) -> list:
    # 节点 uuid 在权威里全局唯一，每张图都用新的
    loop_uuid = str(uuid.uuid4())
    body_uuid = str(uuid.uuid4())
    nodes = [
        {
            "uuid": loop_uuid,
            "name": "L",
            "type": "loop",
            "param": loop_param,
            "pose": {},
            "meta_data": {},
            "execution_policy": {},
        }
    ]
    if body:
        nodes.append(
            {
                "uuid": body_uuid,
                "name": "b",
                "type": "device_action",
                "parent_uuid": loop_uuid,
                "material_uuid": str(uuid.uuid4()),
                "action_name": "record",
                "param": {"value": 1},
                "pose": {},
                "meta_data": {"target_device_id": "loop-device"},
                "execution_policy": {},
            }
        )
    nodes.extend(extra or [])
    return nodes


def test_graph_validation_rejects_bad_loops() -> None:
    service = WorkflowService(":memory:")
    try:

        def save(nodes: list) -> None:
            record = service.create_workflow(
                name="v", tags=[], description=None, meta_data={}, workflow_uuid=str(uuid.uuid4())
            )
            service.save_graph(record["uuid"], revision=record["revision"], nodes=nodes, edges=[])

        with pytest.raises(WorkflowError):
            save(_loop_graph_nodes(loop_param={"mode": "for"}))
        # 空循环体的 while 必须有轮询间隔
        with pytest.raises(WorkflowError):
            save(
                _loop_graph_nodes(
                    loop_param={
                        "mode": "while",
                        "condition": {"source": "device_state", "device_id": "d", "field": "f", "op": "==", "value": 1},
                    },
                    body=False,
                )
            )
        save(
            _loop_graph_nodes(
                loop_param={
                    "mode": "while",
                    "condition": {"source": "device_state", "device_id": "d", "field": "f", "op": "==", "value": 1},
                    "interval_seconds": 1,
                },
                body=False,
            )
        )
        # 循环体外的节点不能引用 {{loop.*}}
        outsider = {
            "uuid": str(uuid.uuid4()),
            "name": "z",
            "type": "device_action",
            "material_uuid": str(uuid.uuid4()),
            "action_name": "record",
            "param": {"value": "{{loop.index}}"},
            "pose": {},
            "meta_data": {"target_device_id": "loop-device"},
            "execution_policy": {},
        }
        with pytest.raises(WorkflowError):
            save(_loop_graph_nodes(loop_param={"mode": "for", "count": 2}, extra=[outsider]))
        save(_loop_graph_nodes(loop_param={"mode": "for", "count": 2}))
    finally:
        service.close()


def test_dynamic_result_params_read_this_loop_attempt(stack: _Stack) -> None:
    @workflow(display_name="逐轮传参", compiler="ast")
    def flow(ctx):
        with ctx.loop_for(3):
            produced = ctx.run("loop-device/record", {"value": "{{loop.index}}"}, name="生产")
            ctx.run("loop-device/record", {"value": {"item": produced.value,
                    "nested": [produced["count"]]}}, name="消费")
    task = stack.run()
    assert task["status"] == "succeeded"
    assert stack.driver.calls == [0, {"item": 0, "nested": [1]},
                                  1, {"item": 1, "nested": [3]},
                                  2, {"item": 2, "nested": [5]}]
    runs = stack.runs_by_name(task["uuid"])
    assert runs["消费"]["return_info"]["return_value"]["value"]["item"] == 2


def test_dynamic_result_missing_field_fails_before_device_dispatch(stack: _Stack) -> None:
    @workflow(display_name="拒绝缺失值", compiler="ast")
    def flow(ctx):
        produced = ctx.run("loop-device/record", {"value": 12}, name="生产")
        ctx.run("loop-device/record", {"value": produced.missing}, name="不能执行")
    task = stack.run()
    assert task["status"] == "failed"
    assert stack.driver.calls == [12]


def test_dynamic_result_array_path_and_escaped_field(stack: _Stack) -> None:
    @workflow(display_name="嵌套结果路径", compiler="ast")
    def flow(ctx):
        produced = ctx.run("loop-device/record", {"value": [{"a.b": 42}]})
        ctx.run("loop-device/record", {"value": produced.value[0]["a.b"]})
    assert stack.run()["status"] == "succeeded"
    assert stack.driver.calls == [[{"a.b": 42}], 42]


def test_parallel_ast_rejects_sibling_value_before_join() -> None:
    from unilabos.registry.workflow_ast import collect_static_steps, WorkflowSourceError
    def flow(ctx):
        with ctx.parallel():
            left = ctx.run("a/record", {})
            ctx.run("b/record", {"value": left.value})
    with pytest.raises(WorkflowSourceError, match="作用域"):
        collect_static_steps(flow)


def test_group_inside_loop_keeps_current_iteration(stack: _Stack) -> None:
    @workflow(display_name="循环内分组", compiler="ast")
    def flow(ctx):
        with ctx.loop_for(2):
            with ctx.group("读取和传参"):
                produced = ctx.run("loop-device/record", {"value": "{{loop.index}}"})
                ctx.run("loop-device/record", {"value": produced.value})
    assert stack.run()["status"] == "succeeded"
    assert stack.driver.calls == [0, 0, 1, 1]
