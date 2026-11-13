"""显式所有权跨循环保留，使用权仍按动作与轮次核实。"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from unilabos.registry.workflows import (
    DeviceCatalog, WorkflowDefinition, build_workflow_template_payload,
    materialize_workflow_template, upsert_workflow,
)
from unilabos.server.backend.scheduler.interval_plan import compile_interval_plan
from unilabos.server.services.runtime.workflow.resource_intervals import ResourceIntervals
from tests.server.test_workflow_loop_execution import _Stack
from tests.server.test_workflow_task_cancel import wait_for


def _record(stack, flow):
    definition = WorkflowDefinition(fn=flow, uuid=str(uuid4()), source_path="test:cross-loop",
        display_name="跨循环资源作用域", description="", tags=[], compiler="ast")
    catalog = DeviceCatalog()
    catalog.add("loop-device", "loop_demo_class", str(uuid4()))
    return upsert_workflow(stack.service, materialize_workflow_template(build_workflow_template_payload(definition), catalog))


def _run_scoped(stack, flow):
    record = _record(stack, flow)
    graph = stack.service.get_graph(record["uuid"])
    stack.service.declare_workflow_resource_scopes(record["uuid"], revision=record["revision"],
        scopes=[{"scope_id": "experiment", "kind": "root", "resources": ["loop-device"],
                 "node_uuids": [node["uuid"] for node in graph["nodes"]]}])
    return stack.service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
        target_node_uuid=None, input_value={}, description=None, meta_data={})


def test_explicit_scope_holds_one_owner_across_real_hostlink_loop_rounds():
    def flow(ctx):
        ctx.run("loop-device/record", {"value": 99})
        with ctx.loop_for(3):
            ctx.run("loop-device/record", {"value": "{{loop.index}}"})
            ctx.run("loop-device/record", {"value": "{{loop.index}}"})
        ctx.run("loop-device/record", {"value": 100})

    stack = _Stack()
    try:
        task = _run_scoped(stack, flow)
        wait_for(lambda: stack.service.get_workflow_task(task["uuid"])["status"] in {"succeeded", "failed"})
        assert stack.service.get_workflow_task(task["uuid"])["status"] == "succeeded"
        assert stack.driver.calls == [99, 0, 0, 1, 1, 2, 2, 100]
        owners = stack.service.connection.execute("SELECT * FROM workflow_resource_interval").fetchall()
        assert len(owners) == 1 and owners[0]["state"] == "released"
        assert json.loads(owners[0]["definition"])["generation"] == []
        assert stack.service.connection.execute("SELECT count(*) FROM workflow_resource_admission").fetchone()[0] == 8
        assert ResourceIntervals(stack.service).snapshot() == []
    finally:
        stack.close()


def test_nested_loops_reuse_outer_explicit_scope():
    def flow(ctx):
        with ctx.loop_for(2):
            ctx.run("loop-device/record", {"value": 1})
            with ctx.loop_for(2):
                ctx.run("loop-device/record", {"value": 2})
        ctx.run("loop-device/record", {"value": 3})

    stack = _Stack()
    try:
        task = _run_scoped(stack, flow)
        wait_for(lambda: stack.service.get_workflow_task(task["uuid"])["status"] in {"succeeded", "failed"})
        assert stack.service.get_workflow_task(task["uuid"])["status"] == "succeeded"
        assert stack.driver.calls == [1, 2, 2, 1, 2, 2, 3]
        owners = stack.service.connection.execute("SELECT * FROM workflow_resource_interval").fetchall()
        assert len(owners) == 1 and owners[0]["state"] == "released"
    finally:
        stack.close()


def test_scope_members_in_different_frames_lift_to_common_loop_boundary():
    nodes = [
        {"uuid": "before", "kind": "device_action", "material_uuid": "reactor"},
        {"uuid": "repeat", "kind": "loop"},
        {"uuid": "inside", "kind": "device_action", "material_uuid": "reactor", "parent_uuid": "repeat"},
    ]
    graph = {"nodes": nodes, "workflow": {"uuid": "w", "meta_data": {"unilab": {"resource_scopes": [
        {"scope_id": "outer", "kind": "with", "resources": ["reactor"], "node_uuids": ["before", "inside"]},
    ]}}}}
    plan = compile_interval_plan({"nodes": nodes, "edges": []}, graph)
    outer = next(frame for frame in plan["frames"] if frame["parent_node_uuid"] is None)
    assert list(outer["admission_members"].values()) == [["before", "inside", "repeat"]]
    assert set(outer["plan"]["intervals"][0]["node_uuids"]) == {"before", "repeat"}


def test_nested_scope_cannot_claim_members_outside_parent():
    nodes = [{"uuid": key, "kind": "device_action", "material_uuid": "reactor"} for key in ["a", "b"]]
    graph = {"nodes": nodes, "workflow": {"meta_data": {"unilab": {"resource_scopes": [
        {"scope_id": "outer", "kind": "with", "resources": ["reactor"], "node_uuids": ["a"]},
        {"scope_id": "inner", "kind": "with", "parent_scope_id": "outer", "resources": ["reactor"], "node_uuids": ["b"]},
    ]}}}}
    with pytest.raises(ValueError, match="完整位于"):
        compile_interval_plan({"nodes": nodes, "edges": []}, graph)


def test_ast_scope_roundtrip_and_nested_scope_across_loop():
    def flow(ctx):
        with ctx.resource_scope("loop-device", name="experiment"):
            ctx.run("loop-device/record", {"value": 1})
            with ctx.loop_for(2):
                with ctx.resource_scope("loop-device", name="round"):
                    ctx.run("loop-device/record", {"value": 2})
            ctx.run("loop-device/record", {"value": 3})

    stack = _Stack()
    try:
        record = _record(stack, flow)
        scopes = stack.service.get_graph(record['uuid'])['workflow']['meta_data']['unilab']['resource_scopes']
        assert scopes[1]['parent_scope_id'] == 'experiment'
        task = stack.service.create_workflow_task(workflow_uuid=record['uuid'], run_mode='normal',
            target_node_uuid=None, input_value={}, description=None, meta_data={})
        wait_for(lambda: stack.service.get_workflow_task(task['uuid'])['status'] in {'succeeded', 'failed'})
        assert stack.service.get_workflow_task(task['uuid'])['status'] == 'succeeded'
        assert stack.driver.calls == [1, 2, 2, 3]
        rows = stack.service.connection.execute('SELECT * FROM workflow_resource_interval').fetchall()
        assert len(rows) == 1 and rows[0]['state'] == 'released'
    finally:
        stack.close()


def test_paused_loop_owner_survives_database_reopen_and_blocks_competitor(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from unilabos.server.api.runtime.workflow import create_workflow_app
    from unilabos.server.backend.scheduler.service import BackendScheduler
    from unilabos.server.services.runtime.workflow.service import WorkflowService
    from tests.server.test_workflow_resource_intervals import make_task
    from tests.server.test_workflow_task_cancel import Executor, command

    def flow(ctx):
        with ctx.resource_scope("loop-device"):
            with ctx.loop_for(2):
                ctx.run("loop-device/record", {"value": 1})
            ctx.run("loop-device/record", {"value": 2})

    path = tmp_path / 'runtime.db'
    service = WorkflowService(path)
    record = _record(SimpleNamespace(service=service), flow)
    owner = service.create_workflow_task(workflow_uuid=record['uuid'], run_mode='normal',
        target_node_uuid=None, input_value={}, description=None, meta_data={})
    competitor = make_task(service, ('loop-device',))
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    client = TestClient(create_workflow_app(service))
    try:
        scheduler.submit(owner['uuid'])
        wait_for(lambda: len(executor.dispatched) == 1)
        assert command(client, owner, 'pause').status_code == 201
        executor.finish(0)
        wait_for(lambda: not service.connection.execute('SELECT 1 FROM workflow_resource_use').fetchone())
        held = ResourceIntervals(service).snapshot(owner['uuid'])
        assert len(held) == 1 and held[0]['state'] == 'held'
    finally:
        scheduler.stop()
        client.close()
        service.close()

    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    client = TestClient(create_workflow_app(service))
    try:
        assert ResourceIntervals(service).snapshot(owner['uuid']) == held
        scheduler.submit(competitor['uuid'])
        wait_for(lambda: bool(scheduler._waiting_interval_jobs))
        assert not executor.dispatched
        assert command(client, owner, 'resume').status_code == 201
        for index in range(2):
            wait_for(lambda: len(executor.dispatched) > index)
            assert executor.dispatched[index]['task_id'] == owner['uuid']
            assert ResourceIntervals(service).snapshot(owner['uuid'])[0]['owner_uuid'] == held[0]['owner_uuid']
            executor.finish(index)
        wait_for(lambda: len(executor.dispatched) == 3)
        assert executor.dispatched[2]['task_id'] == competitor['uuid']
        executor.finish(2)
        wait_for(lambda: service.get_workflow_task(competitor['uuid'])['status'] == 'succeeded')
        assert not ResourceIntervals(service).snapshot()
    finally:
        scheduler.stop()
        client.close()
        service.close()


def test_failed_loop_retains_explicit_scope_until_physical_confirmation():
    def flow(ctx):
        with ctx.resource_scope("loop-device"):
            with ctx.loop_for(3):
                ctx.run("loop-device/record", {"value": "{{loop.index}}"})
            ctx.run("loop-device/record", {"value": 99})

    stack = _Stack()
    try:
        stack.driver.fail_on = 1
        record = _record(stack, flow)
        task = stack.service.create_workflow_task(workflow_uuid=record['uuid'], run_mode='normal',
            target_node_uuid=None, input_value={}, description=None, meta_data={})
        wait_for(lambda: stack.service.get_workflow_task(task['uuid'])['status'] == 'failed')
        assert stack.driver.calls == [0]
        rows = ResourceIntervals(stack.service).snapshot(task['uuid'])
        assert len(rows) == 1 and rows[0]['state'] == 'retained'
    finally:
        stack.close()
