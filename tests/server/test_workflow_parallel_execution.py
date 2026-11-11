"""并行分支先同时派发，汇合等待全部完成；组框不生成执行节点。"""
from __future__ import annotations

import time
from uuid import uuid4

import pytest

from unilabos.registry.workflow_ast import collect_static_steps
from unilabos.registry.workflows import (WorkflowDefinition, DeviceCatalog, build_workflow_template_payload,
    materialize_workflow_template, upsert_workflow)
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService


class Executor:
    def __init__(self):
        self.dispatched = []
    def add_job_finished_listener(self, callback):
        self.callback = callback
    def dispatch(self, payload):
        self.dispatched.append(payload)
    def cancel_task(self, task_uuid):
        return []
    def finish(self, index, result):
        self.callback(self.dispatched[index]["job_id"], True, result, "normal", {})


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_parallel_groups_dispatch_independently_and_join_all_results():
    def flow(ctx):
        with ctx.parallel("两路检测"):
            with ctx.group("左路"):
                left = ctx.run("left/probe", {}, name="左检测")
                ctx.run("left/report", {"value": left.value}, name="左报告")
            right = ctx.run("right/probe", {}, name="右检测")
        ctx.run("collector/report", {"value": [left.value, right.value]}, name="汇总")
    definition = WorkflowDefinition(fn=flow, uuid=str(uuid4()), source_path="test:parallel",
        display_name="并行汇合", description="", tags=[], compiler="ast")
    catalog = DeviceCatalog()
    for device in ("left", "right", "collector"):
        catalog.add(device, "unknown", str(uuid4()))
    service = WorkflowService(":memory:")
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    try:
        payload = materialize_workflow_template(build_workflow_template_payload(definition), catalog)
        record = upsert_workflow(service, payload)
        task = service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
            target_node_uuid=None, input_value={}, description=None, meta_data={})
        wait_for(lambda: len(executor.dispatched) == 2)
        # 两个 probe 在任何返回之前均已派发；组框自身没有 attempt。
        assert {p["device_id"] for p in executor.dispatched} == {"left", "right"}
        right_index = next(i for i,p in enumerate(executor.dispatched) if p["device_id"] == "right")
        left_index = 1 - right_index
        executor.finish(right_index, {"value": 20})
        time.sleep(0.05)
        assert len(executor.dispatched) == 2
        executor.finish(left_index, {"value": 10})
        wait_for(lambda: len(executor.dispatched) == 3)
        assert executor.dispatched[2]["device_id"] == "left"
        assert executor.dispatched[2]["action_args"] == {"value": 10}
        executor.finish(2, {"reported": True})
        wait_for(lambda: len(executor.dispatched) == 4)
        assert executor.dispatched[3]["action_args"] == {"value": [10, 20]}
        executor.finish(3, {})
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
        assert len(service.list_workflow_node_runs(task["uuid"])) == 4
    finally:
        service.set_task_submitter(None)
        scheduler.stop()
        service.close()


def test_parallel_condition_branch_does_not_serialize_sibling():
    def flow(ctx):
        with ctx.parallel():
            if True:
                ctx.run("left/a", {})
                ctx.run("left/b", {})
            else:
                ctx.run("left/c", {})
            ctx.run("right/probe", {})
        ctx.run("collector/report", {})
    steps = collect_static_steps(flow)
    right = next(step for step in steps if step.target == "right")
    report = steps[-1]
    assert right.dependencies == []
    assert right.key in report.dependencies
    assert len(report.dependencies) == 3


def test_builder_cannot_accidentally_serialize_parallel_siblings():
    from unilabos.registry.workflows import WorkflowBuildContext
    ctx = WorkflowBuildContext()
    with pytest.raises(ValueError, match="兄弟分支"):
        with ctx.parallel():
            with ctx.group():
                source = ctx.run("left/probe", {})
            with ctx.group():
                ctx.run("right/report", {"value": source.output("value")})


def test_result_reference_adds_dispatch_dependency_without_explicit_order():
    source, target = str(uuid4()), str(uuid4())
    service = WorkflowService(":memory:")
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    try:
        nodes = [
            {"uuid": target, "name": "消费", "type": "device_action", "material_uuid": str(uuid4()),
             "action_name": "report", "meta_data": {"target_device_id": "target"},
             "param": {"value": {"kind": "node_output", "workflow_node_uuid": source, "data_key": "count"}}},
            {"uuid": source, "name": "生产", "type": "device_action", "material_uuid": str(uuid4()),
             "action_name": "probe", "meta_data": {"target_device_id": "source"}, "param": {}},
        ]
        record = upsert_workflow(service, {"workflow_uuid": str(uuid4()), "name": "自动结果依赖",
            "description": "", "tags": [], "nodes": nodes, "edges": []})
        task = service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
            target_node_uuid=None, input_value={}, description=None, meta_data={})
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["device_id"] == "source"
        executor.finish(0, {"count": 7})
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["action_args"] == {"value": 7}
        executor.finish(1, {})
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
    finally:
        service.set_task_submitter(None)
        scheduler.stop()
        service.close()
