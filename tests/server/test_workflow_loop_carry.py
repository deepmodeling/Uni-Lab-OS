"""原生 ``while`` / ``for ... in range(...)`` 与循环携带的变量 → 模板 → 实例化 → 调度器逐轮执行。

覆盖：初值来自工作流输入、循环体与条件读本轮值、循环体里重新赋值给下一轮、while 条件组合
and / not、循环结束后读最终值、for 循环变量（含 range(start, stop)）、内层循环读外层携带的变量
且外层每轮重新开始、重启后续用落库的值、组合子操作里的循环、默认 1000 轮上限，以及编译期 /
图校验的报错。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

import pytest

from unilabos.registry.workflow_ast import WorkflowSourceError, compile_workflow_source
from unilabos.registry.workflows import (
    DeviceCatalog,
    build_workflow_template_payload,
    clear_registered_workflows,
    get_registered_workflows,
    materialize_workflow_template,
    upsert_workflow,
    workflow,
)
from unilabos.server.services.runtime.workflow.service import WorkflowService


@pytest.fixture(autouse=True)
def _isolated_workflow_registry():
    snapshot = get_registered_workflows()
    clear_registered_workflows()
    yield
    clear_registered_workflows()
    from unilabos.registry.workflows import _registered_workflows

    for definition in snapshot.values():
        _registered_workflows[definition.uuid] = definition


class CarryDriver:
    """加倍加样：返回下一轮该加的量，够量时 ``enough`` 为真（须为模块级类，供类路径实例化）。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def dose(self, volume: float = 0.0) -> dict:
        self.calls.append(("dose", volume))
        return {"volume": volume, "next_volume": volume * 2, "enough": volume * 2 >= 8}

    def add(self, value: int = 0, delta: int = 1) -> dict:
        self.calls.append(("add", value, delta))
        return {"value": value + delta}

    def mark(self, index: int = 0) -> dict:
        self.calls.append(("mark", index))
        return {"index": index}

    def probe(self) -> dict:
        self.calls.append(("probe",))
        return {"ok": len(self.calls) >= 4}

    def note(self, text: str = "") -> dict:
        self.calls.append(("note", text))
        return {"text": text}


class _Stack:
    """HostLink 执行栈 + 本机调度器（同 test_workflow_loop_execution，换成 carry 用的驱动）。"""

    def __init__(self) -> None:
        from unilabos.backend.hostlink.backend import HostLinkBackend
        from unilabos.backend.hostlink.host_node import HostNode
        from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
        from unilabos.server.backend.execution import JobExecutionBackend
        from unilabos.server.backend.scheduler.service import BackendScheduler

        self.local = HostLinkLocalRuntime()
        actions = ("dose", "add", "note", "mark", "probe")
        self.node = self.local.add_driver(HostLinkDriverSpec(
            device_id="carry-device", driver_class=CarryDriver, config={}, action_names=actions,
            action_value_mappings={name: {"type": "UniLabJsonCommand"} for name in actions},
        ))
        self.runtime = HostLinkBackend(self.local, is_slave=False)
        self.local.start()
        self.adapter = HostNode("host_node", self.runtime, bridges=[])
        self.microbackend = JobExecutionBackend(host_node_getter=lambda: self.adapter)
        self.adapter.bridges = [self.microbackend]
        self.microbackend.start()
        self.service = WorkflowService(":memory:")
        self.scheduler = BackendScheduler(self.service, self.microbackend)
        self.service.set_task_submitter(self.scheduler.submit)
        self.scheduler.start(recover=True)

    @property
    def driver(self) -> CarryDriver:
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

    def run(self, input_value: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
        record = upsert_workflow(self.service, _payload())
        task = self.service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
                                                 target_node_uuid=None, input_value=input_value or {},
                                                 description=None, meta_data={})
        deadline = time.monotonic() + timeout
        current = self.service.get_workflow_task(task["uuid"])
        while current["status"] not in {"succeeded", "failed", "canceled"}:
            if time.monotonic() >= deadline:
                pytest.fail(f"workflow task 未在时限内结束: {current['status']}")
            time.sleep(0.02)
            current = self.service.get_workflow_task(task["uuid"])
        return current

    def runs_by_name(self, task_uuid: str) -> Dict[str, Dict[str, Any]]:
        snapshot = {node["uuid"]: node for node in self.service.get_workflow_task(task_uuid)["workflow_snapshot"]["nodes"]}
        return {snapshot[run["workflow_node_uuid"]]["name"]: run for run in self.service.list_workflow_node_runs(task_uuid)}

    def loop_runs(self, task_uuid: str) -> list[Dict[str, Any]]:
        """循环节点运行，按源码顺序（拓扑序）。"""

        return [run for run in self.service.list_workflow_node_runs(task_uuid) if run["executor_kind"] == "loop"]


def _payload(device: str = "carry-device") -> Dict[str, Any]:
    definition = next(iter(get_registered_workflows().values()))
    catalog = DeviceCatalog()
    catalog.add(device, "carry_demo_class", str(uuid.uuid4()))
    return materialize_workflow_template(build_workflow_template_payload(definition), catalog)


@pytest.fixture
def stack():
    instance = _Stack()
    try:
        yield instance
    finally:
        instance.close()


def test_native_while_carries_variables_between_rounds(stack: _Stack) -> None:
    @workflow(display_name="加倍直到够量")
    def flow(ctx, *, start: float = 1.0):
        volume = start
        enough = False
        while not enough and volume < 100:
            dosed = ctx.run("carry-device/dose", {"volume": volume}, name="加样")
            if volume >= 4:
                ctx.run("carry-device/note", {"text": "大体积"}, name="记录")
            volume = dosed.next_volume
            enough = dosed.enough
        ctx.run("carry-device/dose", {"volume": volume}, name="收尾")

    task = stack.run({"start": 1.0})
    assert task["status"] == "succeeded", task["error_info"]
    assert stack.driver.calls == [("dose", 1.0), ("dose", 2.0), ("dose", 4.0), ("note", "大体积"), ("dose", 8.0)]
    [loop] = stack.loop_runs(task["uuid"])
    assert loop["return_info"]["return_value"] == {"iterations": 3, "mode": "while",
                                                   "carry": {"volume": 8.0, "enough": True}}
    # 每轮开始时携带的值随轮次落库（重启后从这里续跑）
    assert loop["control_data"]["loop"]["carry"] == {"volume": 4.0, "enough": False}
    assert loop["control_data"]["loop"]["condition"] == "条件表达式"
    runs = stack.runs_by_name(task["uuid"])
    assert [attempt["status"] for attempt in runs["记录"]["attempts"]] == ["skipped", "skipped", "succeeded"]


def test_whole_result_carried_into_the_while_condition(stack: _Stack) -> None:
    """至少测一次、达标即停：循环前的测量结果是初值，循环体里的复测是下一轮的值。"""

    @workflow(display_name="复测直到达标")
    def flow(ctx):
        measured = ctx.run("carry-device/probe", {}, name="测")
        while not measured.ok:
            ctx.run("carry-device/mark", {"index": 0}, name="处理")
            measured = ctx.run("carry-device/probe", {}, name="复测")

    task = stack.run()
    assert task["status"] == "succeeded", task["error_info"]
    assert stack.driver.calls == [("probe",), ("mark", 0), ("probe",), ("mark", 0), ("probe",)]
    [loop] = stack.loop_runs(task["uuid"])
    assert loop["return_info"]["return_value"]["carry"] == {"measured": {"ok": True}}


def test_carried_variable_may_be_reassigned_inside_group_and_lock_blocks() -> None:
    program = compile_workflow_source(
        'def flow(ctx):\n'
        '    n = 0\n'
        '    while n < 3:\n'
        '        with ctx.group("记录"):\n'
        '            out = ctx.run("d/a", {"v": n})\n'
        '            n = out.v\n')
    loop_step = program.ctx.steps[0]
    out = next(step for step in program.ctx.steps if step.kind == "run")
    assert loop_step.params["carry_next"] == {"n": {"kind": "node_output", "node_key": out.key, "data_key": "v"}}


def test_consecutive_for_loops_may_reuse_the_loop_variable_name() -> None:
    program = compile_workflow_source(
        'def flow(ctx):\n'
        '    for i in range(2):\n'
        '        ctx.run("d/a", {"v": i})\n'
        '    for i in range(3):\n'
        '        ctx.run("d/b", {"v": i})\n')
    first, a, second, b = program.ctx.steps
    assert a.params["v"] == {"kind": "node_output", "node_key": first.key, "data_key": "value"}
    assert b.params["v"] == {"kind": "node_output", "node_key": second.key, "data_key": "value"}
    with pytest.raises(WorkflowSourceError, match="for 循环变量 i 与已有名称冲突"):
        compile_workflow_source('def flow(ctx):\n    for i in range(2):\n        for i in range(2):\n'
                                '            ctx.run("d/a", {})\n')


def test_native_for_range_index_and_nested_carry_restarts_each_outer_round(stack: _Stack) -> None:
    @workflow(display_name="嵌套携带")
    def flow(ctx):
        base = 10
        for i in range(2):
            ctx.run("carry-device/mark", {"index": i}, name="标记")
            step = base
            for j in range(1, 3):
                added = ctx.run("carry-device/add", {"value": step, "delta": j}, name="加")
                step = added.value
            base = step
        ctx.run("carry-device/mark", {"index": base}, name="结果")

    task = stack.run()
    assert task["status"] == "succeeded", task["error_info"]
    assert stack.driver.calls == [("mark", 0), ("add", 10, 1), ("add", 11, 2),
                                  ("mark", 1), ("add", 13, 1), ("add", 14, 2), ("mark", 16)]
    outer, inner = stack.loop_runs(task["uuid"])
    assert outer["return_info"]["return_value"]["carry"] == {"base": 16}
    assert [attempt["return_info"]["return_value"]["carry"] for attempt in inner["attempts"]] == [
        {"step": 13}, {"step": 16}]


def test_for_range_count_from_workflow_input(stack: _Stack) -> None:
    @workflow(display_name="按输入轮数")
    def flow(ctx, *, times: int = 1):
        for i in range(times):
            if i == 1:
                ctx.run("carry-device/note", {"text": "第二轮"}, name="记录")
            ctx.run("carry-device/mark", {"index": i}, name="标记")

    task = stack.run({"times": 3})
    assert task["status"] == "succeeded", task["error_info"]
    assert stack.driver.calls == [("mark", 0), ("note", "第二轮"), ("mark", 1), ("mark", 2)]
    planned = next(node for node in task["execution_plan"]["nodes"] if node["kind"] == "loop")
    assert planned["param"]["count"] == 3 and planned["entry_parameter_names"] == ["times"]


def test_native_while_fails_at_the_default_iteration_limit(stack: _Stack, monkeypatch) -> None:
    from unilabos.registry import workflow_ast

    monkeypatch.setattr(workflow_ast, "DEFAULT_MAX_ITERATIONS", 3)

    @workflow(display_name="停不下来")
    def flow(ctx):
        done = False
        while not done:
            ctx.run("carry-device/mark", {"index": 0}, name="试")
            done = False

    task = stack.run()
    assert task["status"] == "failed"
    assert stack.driver.calls == [("mark", 0)] * 3
    [loop] = stack.loop_runs(task["uuid"])
    assert "max_iterations=3" in loop["error_info"][0]["message"]


def test_carry_initial_value_that_cannot_be_read_fails_the_loop(stack: _Stack) -> None:
    @workflow(display_name="初值缺字段")
    def flow(ctx):
        first = ctx.run("carry-device/add", {"value": 1}, name="先做")
        value = first.missing
        for i in range(2):
            out = ctx.run("carry-device/add", {"value": value}, name="b")
            value = out.value

    task = stack.run()
    assert task["status"] == "failed"
    assert stack.driver.calls == [("add", 1, 1)]
    [loop] = stack.loop_runs(task["uuid"])
    assert "carry 初值无法求值" in loop["error_info"][0]["message"]


def test_carried_value_survives_restart_between_rounds(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from unilabos.server.api.runtime.workflow import create_workflow_app
    from unilabos.server.backend.scheduler.service import BackendScheduler
    from tests.server.test_workflow_task_cancel import Executor, command, wait_for

    @workflow(display_name="重启续用")
    def flow(ctx):
        value = 100
        for i in range(3):
            out = ctx.run("carry-device/note", {"text": value}, name="b")
            value = out.index

    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    record = upsert_workflow(service, _payload())
    task = service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                        input_value={}, description=None, meta_data={})
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    client = TestClient(create_workflow_app(service))

    def loop_state(current: WorkflowService) -> Dict[str, Any]:
        run = next(run for run in current.list_workflow_node_runs(task["uuid"]) if run["executor_kind"] == "loop")
        return (run.get("control_data") or {}).get("loop") or {}

    try:
        scheduler.submit(task["uuid"])
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["action_args"]["text"] == 100
        assert command(client, task, "pause").status_code == 201
        executor.finish(0)  # 第 1 轮返回 index=0 → 第 2 轮 value=0
        wait_for(lambda: loop_state(service).get("iteration") == 1)
        assert loop_state(service)["carry"] == {"value": 0}
        assert len(executor.dispatched) == 1
    finally:
        scheduler.stop()
        client.close()
        service.close()

    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    client = TestClient(create_workflow_app(service))
    try:
        scheduler.submit(task["uuid"])
        assert command(client, task, "resume").status_code == 201
        wait_for(lambda: len(executor.dispatched) == 1)
        # 新进程续用落库的第 2 轮值，不是重新从初值 100 开始
        assert executor.dispatched[0]["action_args"]["text"] == 0
        executor.finish(0)
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["action_args"]["text"] == 0
        executor.finish(1)
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
        loop = next(run for run in service.list_workflow_node_runs(task["uuid"]) if run["executor_kind"] == "loop")
        assert loop["return_info"]["return_value"]["carry"] == {"value": 1}
    finally:
        scheduler.stop()
        client.close()
        service.close()


def test_compiled_native_loops_record_carry_and_expression_with_instance_uuids() -> None:
    source = """
from unilabos.registry.workflows import workflow


@workflow("携带")
def flow(ctx, *, limit: int = 3):
    n = 0
    retry = False
    while n < limit or retry:
        out = ctx.run("carry-device/add", {"value": n})
        n = out.value
    ctx.run("carry-device/note", {"text": n})
"""
    program = compile_workflow_source(source)
    loop_step, body, after = program.ctx.steps
    assert loop_step.params["max_iterations"] == 1000
    assert loop_step.params["carry"] == {"n": 0}  # retry 没在循环体里改写：不携带，按字面量读
    assert loop_step.params["carry_next"] == {"n": {"kind": "node_output", "node_key": body.key, "data_key": "value"}}
    carried_n = {"field": {"field": {"node_key": loop_step.key}, "name": "carry"}, "name": "n"}
    assert loop_step.params["expression"] == {"op": "or", "args": [
        {"op": "lt", "args": [carried_n, {"input": "limit"}]}, {"lit": False}]}
    assert body.params["value"] == {"kind": "node_output", "node_key": loop_step.key, "data_key": "carry.n"}
    assert after.params["text"] == {"kind": "node_output", "node_key": loop_step.key, "data_key": "carry.n"}

    from types import SimpleNamespace

    service = WorkflowService(":memory:")
    try:
        definition = SimpleNamespace(collect=lambda: program.ctx, source_path="test:carry", uuid=str(uuid.uuid4()),
                                     display_name="携带", description="", tags=[], guide=None)
        catalog = DeviceCatalog()
        catalog.add("carry-device", "carry_demo_class", str(uuid.uuid4()))
        payload = materialize_workflow_template(build_workflow_template_payload(definition), catalog)
        record = upsert_workflow(service, payload)
        loop = next(node for node in service.get_graph(record["uuid"])["nodes"] if node["type"] == "loop")
        assert loop["param"]["expression"]["args"][0]["args"][0] == {
            "field": {"field": {"node_uuid": loop["uuid"]}, "name": "carry"}, "name": "n"}
        task = service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                            input_value={"limit": 5}, description=None, meta_data={})
        planned = task["execution_plan"]["nodes"]
        inside = next(node for node in planned if node.get("parent_uuid") == loop["uuid"])
        after_loop = next(node for node in planned if node["kind"] == "device_action" and not node.get("parent_uuid"))
        loop_node = next(node for node in planned if node["kind"] == "loop")
        # 循环体读外层循环携带的值、循环读自己的循环体：都不是先后依赖；循环后读最终值才是
        assert loop["uuid"] not in inside["execution_policy"].get("depends_on", [])
        assert inside["uuid"] not in loop_node["execution_policy"].get("depends_on", [])
        assert loop["uuid"] in after_loop["execution_policy"]["depends_on"]
        assert loop_node["entry_parameter_names"] == ["limit"]
    finally:
        service.close()


@pytest.mark.parametrize("body, message", [
    ('while True:\n        ctx.run("d/a", {})\n        break', "不支持 break / continue"),
    ('for i in range(2):\n        ctx.run("d/a", {})\n        continue', "不支持 break / continue"),
    ('n = 0\n    while n < 3:\n        ctx.run("d/a", {})\n        n = n + 1', r"不支持运算：n \+ 1"),
    ('while True:\n        ctx.run("d/a", {})\n    else:\n        ctx.run("d/b", {})', "循环不支持 else"),
    ('for x in [1, 2]:\n        ctx.run("d/a", {})', r"for 只支持 range\(n\)"),
    ('for i in range("3"):\n        ctx.run("d/a", {})', "range 的参数只能是整数字面量或工作流输入"),
    ('for i in range(3, 3):\n        ctx.run("d/a", {})', "range 至少要有一轮"),
    ('n = 0\n    while n < 3:\n        out = ctx.run("d/a", {})\n        if out.ok:\n            n = out.v',
     "不能在条件分支"),
    ('n = 0\n    first = ctx.run("d/a", {})\n    if first.ok:\n        n = 1', "不能在条件分支"),
    ('n = 0\n    with ctx.loop_for(2):\n        out = ctx.run("d/a", {})\n        n = out.v', "with ctx.loop_"),
    ('for i in range(2):\n        ctx.run("d/a", {})\n        i = 3', "不能在循环体里改写 for 循环变量 i"),
    ('for i in range(2):\n        ctx.run("d/a", {"v": i})\n    ctx.run("d/a", {"v": i})', "只能在循环体里读取"),
    ('while out.ok:\n        out = ctx.run("d/a", {})', "未知常量或当前作用域外的动作结果：out"),
    ('while ctx.device_state("d", "t", "<", 3):\n        ctx.run("d/a", {})', "while 条件不能读设备状态"),
    ('n = len([1])\n    ctx.run("d/a", {"v": n})', "不能调用 len"),
])
def test_compile_errors_point_at_the_unsupported_loop_code(body: str, message: str) -> None:
    source = f"def flow(ctx):\n    {body}\n"
    with pytest.raises(WorkflowSourceError, match=message) as raised:
        compile_workflow_source(source)
    assert raised.value.line is not None


def test_old_with_loop_forms_compile_unchanged() -> None:
    program = compile_workflow_source(
        'def flow(ctx):\n'
        '    with ctx.loop_for(2, name="L"):\n'
        '        probe = ctx.run("d/probe", {"i": "{{loop.index}}"}, name="检测")\n'
        '    with ctx.loop_while(ctx.step_output("检测", "ok", "==", False), max_iterations=5):\n'
        '        ctx.run("d/probe", {}, name="再测")\n')
    first, _, second, _ = program.ctx.steps
    assert first.params == {"mode": "for", "count": 2, "interval_seconds": 0.0}
    assert second.params["condition"]["source"] == "node_output" and "carry" not in second.params


def test_graph_validation_rejects_carry_next_from_a_conditional_body_step() -> None:
    """下一轮的值必须来自每轮都会执行的步骤：条件分支里的结果不保证存在。"""

    @workflow(display_name="分支里的结果")
    def flow(ctx):
        n = 0
        for i in range(2):
            out = ctx.run("carry-device/add", {"value": n}, name="b")
            if out.value > 1:
                late = ctx.run("carry-device/add", {"value": 0}, name="c")
            n = out.value

    service = WorkflowService(":memory:")
    try:
        payload = _payload()
        loop = next(node for node in payload["nodes"] if node["type"] == "loop")
        late = next(node for node in payload["nodes"] if node["name"] == "c")
        loop["param"]["carry_next"] = {"n": {"kind": "node_output", "workflow_node_uuid": late["uuid"],
                                             "data_key": "value"}}
        with pytest.raises(Exception, match="其他分支"):
            upsert_workflow(service, payload)
    finally:
        service.close()


def test_carry_loop_inside_a_composite_child_runs_in_the_parent() -> None:
    """组合展开：子操作循环的条件表达式、携带引用换成展开后的身份，条件里的输入改绑到实参。"""

    from unilabos.server.backend.scheduler.service import BackendScheduler
    from unilabos.server.services.runtime.workflow.authoring_identity import expanded_node_uuid
    from tests.server.test_workflow_task_cancel import Executor, wait_for

    @workflow(display_name="子循环")
    def child(ctx, *, limit: int = 3):
        n = 0
        while n < limit:
            out = ctx.run("carry-device/note", {"text": n}, name="b")
            n = out.index

    service = WorkflowService(":memory:")
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    try:
        record = upsert_workflow(service, _payload())
        published = service.publish_workflow(record["uuid"], revision=service.get_workflow(record["uuid"])["revision"])
        parent = service.create_workflow(name="父流程", tags=[], description=None, meta_data={})
        invocation = str(uuid.uuid4())
        service.insert_composite_invocation(parent["uuid"], revision=parent["revision"],
                                            child_workflow_uuid=record["uuid"], child_revision=published["revision"],
                                            invocation_uuid=invocation, arguments={"limit": 2})
        child_nodes = {node["type"] if node["type"] == "loop" else node["name"]: node
                       for node in service.get_graph(record["uuid"])["nodes"]}
        nodes = {node["uuid"]: node for node in service.get_graph(parent["uuid"])["nodes"]}
        loop = nodes[expanded_node_uuid(invocation, child_nodes["loop"]["uuid"])]
        body = expanded_node_uuid(invocation, child_nodes["b"]["uuid"])
        assert loop["param"]["expression"]["args"] == [
            {"field": {"field": {"node_uuid": loop["uuid"]}, "name": "carry"}, "name": "n"}, {"value": 2}]
        assert loop["param"]["carry_next"]["n"]["workflow_node_uuid"] == body
        assert nodes[body]["param"]["text"]["workflow_node_uuid"] == loop["uuid"]

        task = service.create_workflow_task(workflow_uuid=parent["uuid"], run_mode="normal", target_node_uuid=None,
                                            input_value={}, description=None, meta_data={})
        for index in range(3):
            wait_for(lambda: len(executor.dispatched) == index + 1)
            executor.finish(index)
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
        assert [item["action_args"]["text"] for item in executor.dispatched] == [0, 0, 1]
    finally:
        service.set_task_submitter(None)
        scheduler.stop()
        service.close()
