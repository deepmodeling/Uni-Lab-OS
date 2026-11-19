"""组合调用保留子实验操作的资源作用域：子操作里的 ``ctx.lock`` 在父工作流里照样生效。"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.scheduler.interval_plan import compile_interval_plan, effective_resource_scopes
from unilabos.server.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.authoring_identity import expanded_node_uuid
from unilabos.server.services.runtime.workflow.resource_intervals import ResourceIntervals
from unilabos.server.services.runtime.workflow.service import WorkflowService
from tests.server.test_workflow_resource_intervals import make_task
from tests.server.test_workflow_task_cancel import Executor, wait_for


@pytest.fixture
def stack():
    service = WorkflowService(":memory:")
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    with TestClient(create_workflow_app(service)) as client:
        yield service, executor, scheduler, client
    service.set_task_submitter(None)
    scheduler.stop()
    service.close()


def _ok(response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body.get("data")


def _node(node_uuid: str, name: str, device: str, depends_on: list[str]) -> dict:
    return {
        "uuid": node_uuid, "name": name, "type": "device_action", "material_uuid": str(uuid4()),
        "action_name": "operate", "action_type": "UniLabJsonCommand", "param": {"step": name},
        "pose": {"x": 0, "y": 0}, "execution_policy": {"depends_on": depends_on},
        "meta_data": {"target_device_id": device},
    }


def _workflow(client, name: str) -> dict:
    return _ok(client.post("/api/v1/workflows", json={"name": name, "workflow_type": "experiment_operation"}), 201)


def _locked_child(client) -> tuple[dict, str, str]:
    """子操作：反应器加料 → 泵转移，整段 ``with ctx.lock("reactor")``（反应器在泵转移时也不放）。"""

    child = _workflow(client, "锁住反应器的子操作")
    first, second = str(uuid4()), str(uuid4())
    graph = _ok(client.put(f"/api/v1/workflows/{child['uuid']}/graph", json={
        "revision": child["revision"], "site_binding_mode": "preserve", "edges": [],
        "nodes": [_node(first, "加料", "reactor", []), _node(second, "转移", "pump", [first])],
        "resource_scopes": [{"scope_id": "hold-reactor", "kind": "with", "resources": ["reactor"],
                             "node_uuids": [first, second]}],
    }))
    _ok(client.post(f"/api/v1/workflows/{child['uuid']}/publications",
                    json={"revision": graph["workflow"]["revision"]}), 201)
    return child, first, second


def _insert(service, parent: dict, child_uuid: str, invocation: str) -> dict:
    revision = service.get_workflow(parent["uuid"])["revision"]
    return service.insert_composite_invocation(parent["uuid"], revision=revision,
                                               child_workflow_uuid=child_uuid, invocation_uuid=invocation)


def _run(service, workflow_uuid: str) -> dict:
    return service.create_workflow_task(workflow_uuid=workflow_uuid, run_mode="normal", target_node_uuid=None,
                                        input_value={}, description=None, meta_data={})


def _explicit_scopes(task: dict) -> dict[str, set[str]]:
    """冻结计划里显式边界区间：作用域 → 覆盖的节点。"""

    result: dict[str, set[str]] = {}
    for frame in task["execution_plan"]["resource_intervals"]["frames"]:
        for interval in frame["plan"]["intervals"]:
            if interval["explicit_boundary"]:
                result.setdefault(interval["scope_id"], set()).update(interval["node_uuids"])
    return result


def _assert_reactor_held_through_pump(service, executor, scheduler, owner: dict) -> None:
    """真实调度：加料完成后反应器仍被持有，竞争任务要等泵转移结束才拿到反应器。"""

    competitor = make_task(service, ("reactor",))
    scheduler.submit(owner["uuid"])
    wait_for(lambda: len(executor.dispatched) == 1)
    scheduler.submit(competitor["uuid"])
    executor.finish(0)
    wait_for(lambda: len(executor.dispatched) == 2)
    assert executor.dispatched[1]["task_id"] == owner["uuid"]
    assert executor.dispatched[1]["device_id"] == "pump"
    wait_for(lambda: bool(scheduler._waiting_interval_jobs))
    time.sleep(0.1)
    assert len(executor.dispatched) == 2, "反应器仍在子操作的锁内，竞争任务不能插进来"
    held = ResourceIntervals(service).snapshot(owner["uuid"])
    assert held and all(row["state"] == "held" for row in held)
    assert any(row["definition"]["explicit"] for row in held)
    executor.finish(1)
    wait_for(lambda: len(executor.dispatched) == 3)
    assert executor.dispatched[2]["task_id"] == competitor["uuid"]
    executor.finish(2)
    wait_for(lambda: service.get_workflow_task(owner["uuid"])["status"] == "succeeded")
    wait_for(lambda: service.get_workflow_task(competitor["uuid"])["status"] == "succeeded")
    assert ResourceIntervals(service).snapshot() == []


def test_child_lock_applies_like_the_child_run_directly(stack) -> None:
    service, executor, scheduler, client = stack
    child, first, second = _locked_child(client)
    _assert_reactor_held_through_pump(service, executor, scheduler, _run(service, child["uuid"]))


def test_child_lock_still_holds_when_invoked_from_parent(stack) -> None:
    service, executor, scheduler, client = stack
    child, first, second = _locked_child(client)
    parent = _workflow(client, "父流程")
    invocation = str(uuid4())
    _insert(service, parent, child["uuid"], invocation)

    inner_first, inner_second = expanded_node_uuid(invocation, first), expanded_node_uuid(invocation, second)
    nodes = {node["uuid"]: node for node in service.get_graph(parent["uuid"])["nodes"]}
    [scope] = nodes[invocation]["meta_data"]["unilab"]["composite"]["resource_scopes"]
    assert scope["source_scope_id"] == "hold-reactor" and scope["scope_id"].startswith("resource-scope-")
    assert scope["node_uuids"] == [inner_first, inner_second] and scope["resources"] == ["reactor"]
    # 父图自己没有声明作用域；子操作的锁不写进父工作流元数据
    assert not service.get_graph(parent["uuid"])["workflow"]["meta_data"].get("unilab", {}).get("resource_scopes")

    task = _run(service, parent["uuid"])
    assert _explicit_scopes(task) == {scope["scope_id"]: {inner_first, inner_second}}
    _assert_reactor_held_through_pump(service, executor, scheduler, task)


def test_two_invocations_of_one_child_get_distinct_scopes(stack) -> None:
    service, _executor, _scheduler, client = stack
    child, first, _second = _locked_child(client)
    parent = _workflow(client, "调用两次")
    a, b = str(uuid4()), str(uuid4())
    _insert(service, parent, child["uuid"], a)
    _insert(service, parent, child["uuid"], b)
    task = _run(service, parent["uuid"])
    scopes = _explicit_scopes(task)
    assert len(scopes) == 2
    assert sorted(expanded_node_uuid(owner, first) in members for owner in (a, b) for members in scopes.values()) \
        == [False, False, True, True]


def test_parent_lock_around_invocation_nests_the_child_scope(stack) -> None:
    service, executor, scheduler, client = stack
    child, first, second = _locked_child(client)
    parent = _workflow(client, "外层锁")
    invocation = str(uuid4())
    graph = _insert(service, parent, child["uuid"], invocation)["graph"]
    # 父图作者在定义层只看得到调用节点：用它声明外层锁
    graph = service.declare_workflow_resource_scopes(parent["uuid"], revision=graph["workflow"]["revision"], scopes=[
        {"scope_id": "outer", "kind": "with", "resources": ["reactor", "pump"], "node_uuids": [invocation]}])

    full = service.get_graph(parent["uuid"])
    sources = {node["uuid"]: node for node in full["nodes"]}
    inner_first, inner_second = expanded_node_uuid(invocation, first), expanded_node_uuid(invocation, second)
    outer, inner = effective_resource_scopes(full["workflow"]["meta_data"]["unilab"]["resource_scopes"], sources,
                                             planned=[inner_first, inner_second])
    assert outer["scope_id"] == "outer" and sorted(outer["node_uuids"]) == sorted([inner_first, inner_second])
    assert inner["parent_scope_id"] == "outer" and inner["composite_invocation_uuid"] == invocation

    task = _run(service, parent["uuid"])
    scopes = {scope["scope_id"]: scope for frame in task["execution_plan"]["resource_intervals"]["frames"]
              for scope in frame["plan"]["scopes"]}
    assert scopes[inner["scope_id"]]["parent_scope_id"] == "outer"
    scheduler.submit(task["uuid"])
    for index in range(2):
        wait_for(lambda: len(executor.dispatched) == index + 1)
        executor.finish(index)
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
    assert ResourceIntervals(service).snapshot() == []


def test_nested_invocation_keeps_innermost_child_scope(stack) -> None:
    service, executor, scheduler, client = stack
    child, first, second = _locked_child(client)
    middle = _workflow(client, "中间层")
    inner_invocation = str(uuid4())
    _insert(service, middle, child["uuid"], inner_invocation)
    _ok(client.post(f"/api/v1/workflows/{middle['uuid']}/publications",
                    json={"revision": service.get_workflow(middle["uuid"])["revision"]}), 201)
    outer = _workflow(client, "外层")
    outer_invocation = str(uuid4())
    _insert(service, outer, middle["uuid"], outer_invocation)

    nested = expanded_node_uuid(outer_invocation, inner_invocation)
    leaves = [expanded_node_uuid(nested, first), expanded_node_uuid(nested, second)]
    nodes = {node["uuid"]: node for node in service.get_graph(outer["uuid"])["nodes"]}
    [scope] = nodes[nested]["meta_data"]["unilab"]["composite"]["resource_scopes"]
    assert scope["node_uuids"] == leaves
    assert "resource_scopes" not in nodes[outer_invocation]["meta_data"]["unilab"]["composite"]

    task = _run(service, outer["uuid"])
    assert _explicit_scopes(task) == {scope["scope_id"]: set(leaves)}
    _assert_reactor_held_through_pump(service, executor, scheduler, task)


def test_child_scope_over_its_own_nested_invocation_reaches_grandchild_leaves(stack) -> None:
    """中间层把锁包在自己的组合调用外面：投影到外层时成员换成外层身份下的孙节点。"""

    service, _executor, _scheduler, client = stack
    child, first, second = _locked_child(client)
    middle = _workflow(client, "中间层锁")
    inner_invocation = str(uuid4())
    graph = _insert(service, middle, child["uuid"], inner_invocation)["graph"]
    leaf = expanded_node_uuid(inner_invocation, second)
    graph = service.declare_workflow_resource_scopes(middle["uuid"], revision=graph["workflow"]["revision"], scopes=[
        {"scope_id": "middle-call", "kind": "with", "resources": ["pump"], "node_uuids": [inner_invocation]},
        {"scope_id": "middle-leaf", "kind": "with", "resources": ["pump"], "node_uuids": [leaf]}])
    _ok(client.post(f"/api/v1/workflows/{middle['uuid']}/publications",
                    json={"revision": graph["workflow"]["revision"]}), 201)
    outer = _workflow(client, "外层")
    outer_invocation = str(uuid4())
    _insert(service, outer, middle["uuid"], outer_invocation)

    nested = expanded_node_uuid(outer_invocation, inner_invocation)
    nodes = {node["uuid"]: node for node in service.get_graph(outer["uuid"])["nodes"]}
    call_scope, leaf_scope = nodes[outer_invocation]["meta_data"]["unilab"]["composite"]["resource_scopes"]
    assert call_scope["node_uuids"] == [nested]
    assert leaf_scope["node_uuids"] == [expanded_node_uuid(nested, second)]
    [child_scope] = nodes[nested]["meta_data"]["unilab"]["composite"]["resource_scopes"]
    task = _run(service, outer["uuid"])
    scopes = {scope["scope_id"]: scope for frame in task["execution_plan"]["resource_intervals"]["frames"]
              for scope in frame["plan"]["scopes"]}
    # 子操作的顶层锁挂在中间层「包住调用」的锁下面
    assert scopes[child_scope["scope_id"]]["parent_scope_id"] == call_scope["scope_id"]


def test_disabled_or_unselected_child_members_drop_out_of_the_scope() -> None:
    invocation, a, b = "inv", "a", "b"
    sources = {
        invocation: {"uuid": invocation, "meta_data": {"unilab": {"composite": {"resource_scopes": [
            {"scope_id": "s1", "kind": "with", "resources": ["reactor"], "node_uuids": [a, b]},
            {"scope_id": "s2", "kind": "with", "resources": ["reactor"], "node_uuids": [b], "parent_scope_id": "s1"},
        ]}}}},
        a: {"uuid": a, "parent_uuid": invocation},
        b: {"uuid": b, "parent_uuid": invocation},
    }
    assert effective_resource_scopes([], sources, planned=[a]) == [
        {"scope_id": "s1", "kind": "with", "resources": ["reactor"], "node_uuids": [a],
         "composite_invocation_uuid": invocation}]
    outer, inner = effective_resource_scopes([], sources, planned=[b])
    assert outer["node_uuids"] == inner["node_uuids"] == [b] and inner["parent_scope_id"] == "s1"
    assert effective_resource_scopes([], sources, planned=[]) == []
    # 图里没有组合调用：直接声明原样返回
    direct = [{"scope_id": "x", "resources": ["r"], "node_uuids": ["n"]}]
    assert effective_resource_scopes(direct, {"n": {"uuid": "n"}}, planned=["n"]) is direct
    nodes = [{"uuid": a, "kind": "device_action", "material_uuid": "reactor"}]
    graph = {"nodes": list(sources.values()), "workflow": {"uuid": "w", "meta_data": {}}}
    [frame] = compile_interval_plan({"nodes": nodes, "edges": []}, graph)["frames"]
    assert [interval["node_uuids"] for interval in frame["plan"]["intervals"]] == [[a]]
