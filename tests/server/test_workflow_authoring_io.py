"""作者输入输出合同穿过模板、HTTP 图事务、任务冻结和真实 HostLink 执行。"""
from __future__ import annotations

import time
from copy import deepcopy
from typing import Annotated, Literal, TypedDict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import Field

from unilabos.registry.workflow_ast import collect_static_workflow, WorkflowSourceError
from unilabos.registry.workflows import (WorkflowDefinition, DeviceCatalog, build_workflow_template_payload,
    materialize_workflow_template, upsert_workflow)
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import WorkflowError, WorkflowService
from tests.server.test_workflow_loop_execution import _Stack
from tests.server.test_workflow_entry_params import Executor, wait_for, stack
from unilabos.registry.annotations import JSONValue
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.server.backend.scheduler.service import BackendScheduler


class DoseOutput(TypedDict):
    amount: Annotated[int, Field(ge=1, le=10, unit="count")]
    label: Literal["a", "b"]


def dose_flow(ctx, *, amount: Annotated[int, Field(ge=1, le=10, unit="count")] = 3,
              label: Literal["a", "b"] = "a") -> DoseOutput:
    result = ctx.run("loop-device/record", {"value": amount, "label": label})
    return {"amount": result.value, "label": label}


def payload_for(fn=dose_flow):
    definition = WorkflowDefinition(uuid=str(uuid4()), source_path=f"test:{fn.__name__}",
        display_name="带输入输出的流程", description="", tags=[], fn=fn, compiler="ast")
    catalog = DeviceCatalog()
    catalog.add("loop-device", "loop_demo_class", str(uuid4()))
    template = build_workflow_template_payload(definition)
    return template, materialize_workflow_template(template, catalog)


def submit(service, record, values):
    return service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
        target_node_uuid=None, input_value=values, description=None, meta_data={})


def test_typed_authoring_hostlink_defaults_overrides_and_frozen_output():
    stack = _Stack()
    try:
        template, payload = payload_for()
        record = upsert_workflow(stack.service, payload)
        contract = stack.service.get_graph(record["uuid"])["workflow"]["meta_data"]["unilab"]
        assert contract["input_contract"]["parameters"][0]["unit"] == "count"
        assert contract["output_contract"]["outputs"][0]["schema"]["maximum"] == 10
        for values, expected in [({}, {"amount": 3, "label": "a"}), ({"amount": 7, "label": "b"}, {"amount": 7, "label": "b"})]:
            task = submit(stack.service, record, values)
            wait_for(lambda: stack.service.get_workflow_task(task["uuid"])["status"] in {"succeeded", "failed"})
            done = stack.service.get_workflow_task(task["uuid"])
            assert done["status"] == "succeeded", done
            assert done["input"] == done["output"] == expected
        assert stack.driver.calls == [("a", 3), ("b", 7)]
        assert template["nodes"][0]["param"]["value"] == {"kind": "workflow_input", "parameter": "amount"}
    finally:
        stack.close()


@pytest.mark.parametrize("values", [{"amount": 0}, {"amount": 11}, {"amount": True}, {"amount": "2"}, {"label": "c"}, {"unknown": 1}])
def test_typed_input_rejected_before_dispatch(values):
    service = WorkflowService(":memory:")
    try:
        _, payload = payload_for()
        record = upsert_workflow(service, payload)
        with pytest.raises(WorkflowError):
            submit(service, record, values)
        assert service.list_workflow_tasks()["total"] == 0
    finally:
        service.close()


def test_graph_and_contract_save_is_atomic_and_checks_revision():
    service = WorkflowService(":memory:")
    try:
        _, payload = payload_for()
        with TestClient(create_workflow_app(service)) as client:
            workflow = client.post("/api/v1/workflows", json={"name": "事务"}).json()["data"]
            body = {"revision": workflow["revision"], "nodes": payload["nodes"], "edges": [],
                    "workflow_io": payload["io"], "site_binding_mode": "preserve"}
            broken = deepcopy(body)
            broken["workflow_io"]["output_bindings"]["amount"]["workflow_node_uuid"] = str(uuid4())
            assert client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json=broken).json()["code"] == 1000
            graph = service.get_graph(workflow["uuid"])
            assert graph["nodes"] == [] and graph["workflow"]["revision"] == workflow["revision"]
            assert "input_contract" not in graph["workflow"]["meta_data"].get("unilab", {})
            response = client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json=body)
            assert response.json()["code"] == 0, response.text
            assert client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json=body).json()["code"] == 3003
    finally:
        service.close()


@pytest.mark.parametrize("result,code", [({}, "workflow_output_missing"), ({"value": "bad"}, "workflow_output_invalid"), ({"value": 12}, "workflow_output_invalid")])
def test_missing_or_invalid_declared_output_makes_task_fail(result, code):
    service = WorkflowService(":memory:")
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    try:
        _, payload = payload_for()
        record = upsert_workflow(service, payload)
        task = submit(service, record, {})
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, result)
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] in {"succeeded", "failed"})
        done = service.get_workflow_task(task["uuid"])
        assert done["status"] == "failed"
        assert done["error_info"][0]["code"] == code
        assert service.list_workflow_node_runs(task["uuid"])[0]["status"] == "succeeded"
    finally:
        service.set_task_submitter(None)
        scheduler.stop()
        service.close()


def test_annotated_outputs_require_exact_top_level_return():
    def missing(ctx, *, amount: int = 1) -> DoseOutput:
        ctx.run("loop-device/record", {"value": amount})
    with pytest.raises(WorkflowSourceError, match="没有.*返回"):
        collect_static_workflow(missing)
    def extra(ctx) -> DoseOutput:
        value = ctx.run("loop-device/record", {})
        return {"amount": value.value, "label": value.label, "extra": value}
    with pytest.raises(WorkflowSourceError, match="字段一致"):
        collect_static_workflow(extra)


class NestedOutput(TypedDict):
    payload: dict[str, JSONValue]


def nested_flow(ctx, *, amount: int = 2, label: str = "sample") -> NestedOutput:
    result = ctx.run("loop-device/record", {"value": {"values": [amount, {"amount": amount}], "label": label}})
    return {"payload": result.value}


def test_nested_input_edit_updates_all_occurrences_and_cannot_change_after_dispatch(stack):
    service, executor, client = stack
    _, payload = payload_for(nested_flow)
    record = upsert_workflow(service, payload)
    response = client.post("/api/v1/workflow-tasks", json={"workflow_uuid": record["uuid"], "run_mode": "step"})
    assert response.status_code == 201, response.text
    task = response.json()["data"]
    edited = client.patch(f"/api/v1/workflow-tasks/{task['uuid']}/input",
        json={"expected_revision": task["control_revision"], "input": {"amount": 7, "label": "changed"}})
    assert edited.json()["code"] == 0, edited.text
    current = service.get_workflow_task(task["uuid"])
    assert current["execution_plan"]["nodes"][0]["param"] == {"value": {"values": [7, {"amount": 7}], "label": "changed"}}
    command = client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands", json={
        "type": "step", "expected_revision": current["control_revision"], "idempotency_key": str(uuid4())})
    assert command.json()["code"] == 0, command.text
    wait_for(lambda: len(executor.dispatched) == 1)
    assert executor.dispatched[0]["action_args"] == {"value": {"values": [7, {"amount": 7}], "label": "changed"}}
    current = service.get_workflow_task(task["uuid"])
    rejected = client.patch(f"/api/v1/workflow-tasks/{task['uuid']}/input",
        json={"expected_revision": current["control_revision"], "input": {"amount": 8}})
    assert rejected.json()["code"] == 3003, rejected.text
    executor.finish(0, {"value": {"values": [7, {"amount": 7}], "label": "changed"}})
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")


def test_literal_input_markers_and_loop_placeholders_are_not_reinterpreted():
    def literal_flow(ctx, *, data: dict[str, JSONValue]):
        with ctx.loop_for(2):
            ctx.run("loop-device/record", {"value": data})
    stack = _Stack()
    try:
        _, payload = payload_for(literal_flow)
        record = upsert_workflow(stack.service, payload)
        data = {"kind": "node_output", "workflow_node_uuid": str(uuid4()), "data_key": "{{loop.index}}"}
        task = submit(stack.service, record, {"data": data})
        wait_for(lambda: stack.service.get_workflow_task(task["uuid"])["status"] in {"succeeded", "failed"})
        assert stack.service.get_workflow_task(task["uuid"])["status"] == "succeeded"
        assert stack.driver.calls == [data, data]
    finally:
        stack.close()


def test_published_typed_child_preserves_nested_parent_input_and_output(stack):
    service, executor, client = stack
    _, payload = payload_for(nested_flow)
    child = upsert_workflow(service, payload)
    published = client.post(f"/api/v1/workflows/{child['uuid']}/publications", json={"revision": child["revision"]})
    assert published.status_code == 201, published.text
    parent = client.post("/api/v1/workflows", json={"name": "父流程"}).json()["data"]
    declared = client.put(f"/api/v1/workflows/{parent['uuid']}/io", json={"revision": parent["revision"],
        "input_contract": {"version": 1, "parameters": [{"name": "count", "schema": {"type": "integer"}, "required": True}]}})
    assert declared.json()["code"] == 0, declared.text
    inserted = client.post(f"/api/v1/workflows/{parent['uuid']}/composite-invocations", json={
        "revision": declared.json()["data"]["workflow"]["revision"], "child_workflow_uuid": child["uuid"],
        "arguments": {"amount": {"kind": "workflow_input", "parameter": "count"}, "label": "parent"}})
    assert inserted.status_code == 201, inserted.text
    graph = inserted.json()["data"]["graph"]
    invocation = next(node for node in graph["nodes"] if node["type"] == "workflow")
    boundary = next(handle["uuid"] for handle in graph["handle_templates"] if handle["io_type"] == "source" and handle["handle_key"] == "payload")
    declared = client.put(f"/api/v1/workflows/{parent['uuid']}/io", json={
        "revision": graph["workflow"]["revision"], "input_contract": graph["workflow"]["meta_data"]["unilab"]["input_contract"],
        "output_contract": {"version": 1, "outputs": [{"name": "payload", "schema": {"type": "object"}}]},
        "output_bindings": {"payload": {"kind": "node_output", "workflow_node_uuid": invocation["uuid"], "source_handle_uuid": boundary}}})
    assert declared.json()["code"] == 0, declared.text
    task = submit(service, parent, {"count": 8})
    wait_for(lambda: len(executor.dispatched) == 1)
    value = {"values": [8, {"amount": 8}], "label": "parent"}
    assert executor.dispatched[0]["action_args"] == {"value": value}
    executor.finish(0, {"value": value})
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] in {"succeeded", "failed"})
    assert service.get_workflow_task(task["uuid"])["output"] == {"payload": value}


@pytest.mark.parametrize("threshold,expected", [(2, [5, "yes"]), (8, [5, "no"])])
def test_condition_reads_declared_task_input(threshold, expected):
    def flow(ctx, *, threshold: int = 2):
        result = ctx.run("loop-device/record", {"value": 5})
        if result.value >= threshold:
            ctx.run("loop-device/record", {"value": "yes"})
        else:
            ctx.run("loop-device/record", {"value": "no"})
    stack = _Stack()
    try:
        _, payload = payload_for(flow)
        record = upsert_workflow(stack.service, payload)
        task = submit(stack.service, record, {"threshold": threshold})
        wait_for(lambda: stack.service.get_workflow_task(task["uuid"])["status"] in {"succeeded", "failed"})
        assert stack.service.get_workflow_task(task["uuid"])["status"] == "succeeded"
        assert stack.driver.calls == expected
    finally:
        stack.close()


def test_published_child_condition_uses_its_static_argument(stack):
    def flow(ctx, *, enabled: bool = True):
        if enabled:
            ctx.run("loop-device/record", {"value": "yes"})
        else:
            ctx.run("loop-device/record", {"value": "no"})
    service, executor, client = stack
    _, payload = payload_for(flow)
    child = upsert_workflow(service, payload)
    assert client.post(f"/api/v1/workflows/{child['uuid']}/publications", json={"revision": child["revision"]}).status_code == 201
    parent = client.post("/api/v1/workflows", json={"name": "父条件"}).json()["data"]
    result = client.post(f"/api/v1/workflows/{parent['uuid']}/composite-invocations", json={
        "revision": parent["revision"], "child_workflow_uuid": child["uuid"], "arguments": {"enabled": False}})
    assert result.status_code == 201, result.text
    task = submit(service, parent, {})
    wait_for(lambda: len(executor.dispatched) == 1)
    assert executor.dispatched[0]["action_args"] == {"value": "no"}
    executor.finish(0, {})
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")


def test_resource_input_is_an_implicit_output_at_workflow_boundary():
    def flow(ctx, *, sample: ResourceSlot):
        ctx.run("loop-device/record", {"value": sample})
    stack = _Stack()
    try:
        template, payload = payload_for(flow)
        assert template["io"]["output_contract"]["outputs"] == [{
            "name": "sample", "schema": {"$slot": "ResourceSlot"}, "implicit": True}]
        record = upsert_workflow(stack.service, payload)
        value = {"uuid": str(uuid4())}
        task = submit(stack.service, record, {"sample": value})
        wait_for(lambda: stack.service.get_workflow_task(task["uuid"])["status"] in {"succeeded", "failed"})
        assert stack.service.get_workflow_task(task["uuid"])["output"] == {"sample": value}
    finally:
        stack.close()
