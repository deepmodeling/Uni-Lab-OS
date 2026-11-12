"""画布工作流的入口参数：param 内引用 → 任务输入替换 → 声明输出 → 组合透传 → 暂停时修改。"""

from __future__ import annotations

import sqlite3
import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.database import DATABASE_SPECS
from unilabos.server.database.schema import rebuild_table_migration
from unilabos.server.database.tables.runtime.migrations import (
    _WORKFLOW_TASK_COMMAND_V4,
    _WORKFLOW_TASK_COMMAND_V4_INDEXES,
)
from unilabos.server.services.runtime.workflow.authoring_identity import expanded_node_uuid
from unilabos.server.services.runtime.workflow.service import WorkflowService

NUMBER = {"type": "number"}


def ref(name: str) -> dict:
    return {"kind": "workflow_input", "parameter": name}


class Executor:
    def __init__(self):
        self.dispatched = []

    def add_job_finished_listener(self, callback):
        self.callback = callback

    def dispatch(self, payload):
        self.dispatched.append(payload)

    def cancel_task(self, task_uuid):
        return []

    def finish(self, index, value=None, *, success=True):
        result = {"index": index} if value is None else value
        self.callback(self.dispatched[index]["job_id"], success, result, "normal", {})


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.fixture
def stack():
    service = WorkflowService(":memory:")
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    with TestClient(create_workflow_app(service)) as client:
        yield service, executor, client
    service.set_task_submitter(None)
    scheduler.stop()
    service.close()


def _ok(response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body.get("data")


def _code(response) -> int:
    return response.json()["code"]


def _node(node_uuid: str, name: str, param: dict, *, device: str, depends_on=()) -> dict:
    """编排画布形状：无模板，值直接写在 param，顺序写 depends_on。"""

    return {
        "uuid": node_uuid,
        "name": name,
        "type": "device_action",
        "material_uuid": str(uuid4()),
        "action_name": "dose",
        "action_type": "UniLabJsonCommand",
        "param": param,
        "pose": {"x": 0, "y": 0},
        "execution_policy": {"depends_on": list(depends_on)},
        "meta_data": {"target_device_id": device},
    }


def _insert(client, service, parent_uuid: str, revision: int, child_uuid: str, **body) -> Any:
    """V1 组合调用：按子工作流最新发布合同插入；成功时返回父工作流完整图（含私有子图）。"""

    publications = client.get(f"/api/v1/workflows/{child_uuid}/publications").json()["data"]
    response = client.post(f"/api/v1/workflows/{parent_uuid}/composite-invocations", json={
        "revision": revision, "contract_uuid": publications[-1]["uuid"], **body,
    })
    if response.json()["code"] != 0:
        return response
    _ok(response)
    return service.get_graph(parent_uuid)


def _workflow(client, name: str, nodes: list[dict]) -> dict:
    # V1：只有实验操作可以被引用；子工作流都以实验操作创建
    workflow = _ok(client.post("/api/v1/workflows", json={"name": name, "workflow_type": "experiment_operation"}), 201)
    return _ok(client.put(
        f"/api/v1/workflows/{workflow['uuid']}/graph",
        json={"revision": workflow["revision"], "nodes": nodes, "edges": [],
              "site_binding_mode": "preserve"},
    ))


def _declare(client, graph: dict, **body) -> Any:
    return client.put(
        f"/api/v1/workflows/{graph['workflow']['uuid']}/io",
        json={"revision": graph["workflow"]["revision"], **body},
    )


def _contract(*parameters: dict) -> dict:
    return {"version": 1, "parameters": list(parameters)}


def _outputs(*names: str) -> dict:
    return {"version": 1, "outputs": [{"name": name, "schema": NUMBER} for name in names]}


def _task(client, workflow_uuid: str, **body) -> Any:
    return client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow_uuid, **body})


def _command(client, task: dict, kind: str) -> Any:
    revision = client.get(f"/api/v1/workflow-tasks/{task['uuid']}").json()["data"]["control_revision"]
    return client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands", json={
        "type": kind, "expected_revision": revision, "idempotency_key": str(uuid4()),
    })


def _edit(client, task: dict, values: dict, *, revision: int | None = None) -> Any:
    if revision is None:
        revision = client.get(f"/api/v1/workflow-tasks/{task['uuid']}").json()["data"]["control_revision"]
    return client.patch(f"/api/v1/workflow-tasks/{task['uuid']}/input",
                        json={"expected_revision": revision, "input": values})


def test_canvas_input_used_by_two_nodes_fills_params_and_declared_output(stack) -> None:
    service, executor, client = stack
    first, second = str(uuid4()), str(uuid4())
    graph = _workflow(client, "画布入口参数", [
        _node(first, "加样", {"volume": 1}, device="pump-1"),
        _node(second, "混合", {"volume": 1, "speed": 1}, device="mixer-1", depends_on=[first]),
    ])

    # 未声明的入口参数不能被引用（图保存与 PUT /io 都拒绝）
    undeclared = client.put(f"/api/v1/workflows/{graph['workflow']['uuid']}/graph", json={
        "revision": graph["workflow"]["revision"], "edges": [], "site_binding_mode": "preserve",
        "nodes": [_node(first, "加样", {"volume": ref("volume")}, device="pump-1")],
    })
    assert _code(undeclared) == 1000
    assert _code(_declare(client, graph, input_contract=_contract(
        {"name": "volume", "schema": NUMBER, "required": True},
    ), node_params={first: {"volume": ref("missing")}})) == 1000

    # 画布工作流不需要 Handle 绑定：合同 + param 内引用 + 按返回值路径声明输出
    declared = _ok(_declare(
        client, graph,
        input_contract=_contract(
            {"name": "volume", "schema": NUMBER, "required": True},
            {"name": "speed", "schema": NUMBER, "default": 2},
            {"name": "unused", "schema": NUMBER, "required": True},
        ),
        output_contract=_outputs("total", "echo"),
        output_bindings={
            "total": {"kind": "node_output", "workflow_node_uuid": second,
                      "data_key": "result.total"},
            "echo": {"kind": "workflow_input", "parameter": "volume"},
        },
        node_params={first: {"volume": ref("volume")},
                     second: {"volume": ref("volume"), "speed": ref("speed")}},
    ))
    workflow_uuid = declared["workflow"]["uuid"]
    bindings = declared["workflow"]["meta_data"]["unilab"]["output_bindings"]
    assert bindings["total"] == {"kind": "node_output", "workflow_node_uuid": second,
                                 "data_key": "result.total"}

    # 输入校验：缺必填、类型不符、未知键；未声明合同的工作流仍只接受 {}
    assert "workflow_input_required" in _task(client, workflow_uuid, input={}).json()["error"]["msg"]
    assert _code(_task(client, workflow_uuid, input={"volume": "lots", "unused": 0})) == 1000
    assert "workflow_input_unknown" in _task(
        client, workflow_uuid, input={"volume": 3, "unused": 0, "extra": 1}
    ).json()["error"]["msg"]
    plain = _workflow(client, "无合同", [_node(str(uuid4()), "x", {}, device="dev-x")])
    assert _code(_task(client, plain["workflow"]["uuid"], input={"a": 1})) == 1000
    assert executor.dispatched == []

    task = _ok(_task(client, workflow_uuid, input={"volume": 3, "unused": 0},
                     idempotency_key="run-1"), 201)
    assert task["input"] == {"volume": 3, "speed": 2, "unused": 0}
    planned = {node["uuid"]: node for node in task["execution_plan"]["nodes"]}
    assert planned[first]["param"] == {"volume": 3}
    assert planned[second]["param"] == {"volume": 3, "speed": 2}
    assert planned[second]["entry_bindings"] == {"volume": "volume", "speed": "speed"}
    # 幂等键：同输入复用，同键不同输入冲突
    assert _ok(_task(client, workflow_uuid, input={"volume": 3, "unused": 0},
                     idempotency_key="run-1"), 201)["uuid"] == task["uuid"]
    assert _code(_task(client, workflow_uuid, input={"volume": 4, "unused": 0},
                       idempotency_key="run-1")) == 3003

    wait_for(lambda: len(executor.dispatched) == 1)
    assert executor.dispatched[0]["action_args"] == {"volume": 3}
    executor.finish(0)
    wait_for(lambda: len(executor.dispatched) == 2)
    assert executor.dispatched[1]["action_args"] == {"volume": 3, "speed": 2}
    executor.finish(1, {"result": {"total": 9}})
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
    public = _ok(client.get(f"/api/v1/workflow-tasks/{task['uuid']}"))
    assert public["output"] == {"total": 9, "echo": 3}
    assert public["input"] == {"volume": 3, "speed": 2, "unused": 0}


def test_canvas_child_receives_parent_entry_parameter(stack) -> None:
    service, executor, client = stack
    inner = str(uuid4())
    child = _workflow(client, "画布子流程", [_node(inner, "加样", {"volume": 1}, device="pump-1")])
    child = _ok(_declare(
        client, child,
        input_contract=_contract({"name": "volume", "schema": NUMBER, "required": True}),
        output_contract=_outputs("total"),
        output_bindings={"total": {"kind": "node_output", "workflow_node_uuid": inner,
                                   "data_key": "total"}},
        node_params={inner: {"volume": ref("volume")}},
    ))
    child_uuid = child["workflow"]["uuid"]
    _ok(client.post(f"/api/v1/workflows/{child_uuid}/publications",
                    json={"revision": child["workflow"]["revision"]}), 201)

    parent = _ok(client.post("/api/v1/workflows", json={"name": "父流程"}), 201)
    parent_graph = _ok(client.get(f"/api/v1/workflows/{parent['uuid']}/graph"))
    parent_graph = _ok(_declare(
        client, parent_graph,
        input_contract=_contract({"name": "amount", "schema": NUMBER, "required": True}),
    ))
    invocation = str(uuid4())
    inserted = _insert(client, service, parent["uuid"], parent_graph["workflow"]["revision"], child_uuid,
                       invocation_uuid=invocation, param={"volume": ref("amount")})
    leaf = expanded_node_uuid(invocation, inner)
    by_uuid = {node["uuid"]: node for node in inserted["nodes"]}
    # 透传：子工作流的引用改指父工作流入口参数
    assert by_uuid[leaf]["param"] == {"volume": ref("amount")}
    assert by_uuid[invocation]["param"] == {"volume": ref("amount")}

    # 父工作流输出绑在组合调用的边界 Handle 上，求值时沿冻结映射追到叶子
    template_uuid = by_uuid[invocation]["workflow_node_template_uuid"]
    boundary = next(
        item["uuid"] for item in inserted["handle_templates"]
        if item["workflow_node_template_uuid"] == template_uuid
        and item["io_type"] == "source" and item["handle_key"] == "total"
    )
    _ok(_declare(
        client, inserted,
        input_contract=_contract({"name": "amount", "schema": NUMBER, "required": True}),
        output_contract=_outputs("total"),
        output_bindings={"total": {"kind": "node_output", "workflow_node_uuid": invocation,
                                   "source_handle_uuid": boundary}},
    ))

    task = _ok(_task(client, parent["uuid"], input={"amount": 7}), 201)
    wait_for(lambda: len(executor.dispatched) == 1)
    assert executor.dispatched[0]["node_id"] == leaf
    assert executor.dispatched[0]["action_args"] == {"volume": 7}
    executor.finish(0, {"total": 11})
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
    assert service.get_workflow_task(task["uuid"])["output"] == {"total": 11}

    # 静态实参：插入时直接写进内部节点
    other = _ok(client.post("/api/v1/workflows", json={"name": "静态父流程"}), 201)
    static_invocation = str(uuid4())
    static = _insert(client, service, other["uuid"], other["revision"], child_uuid,
                     invocation_uuid=static_invocation, param={"volume": 4})
    static_leaf = expanded_node_uuid(static_invocation, inner)
    assert {node["uuid"]: node for node in static["nodes"]}[static_leaf]["param"] == {"volume": 4}
    # 引用父工作流未声明的入口参数被拒绝
    rejected = _insert(client, service, other["uuid"], static["workflow"]["revision"], child_uuid,
                       param={"volume": ref("amount")})
    assert _code(rejected) == 1000


def test_published_child_condition_references_its_expanded_nodes(stack) -> None:
    service, executor, client = stack
    probe, condition, chosen, skipped = (str(uuid4()) for _ in range(4))
    child = _workflow(client, "条件子流程", [
        _node(probe, "检测", {}, device="pump-1"),
        {"uuid": condition, "type": "condition", "name": "判定", "param": {
            "version": 1, "expression": {"field": {"node_uuid": probe}, "name": "ready"},
        }},
        {**_node(chosen, "选中", {}, device="pump-1"), "execution_policy": {
            "condition_guards": [{"node_uuid": condition, "equals": True}],
        }},
        {**_node(skipped, "跳过", {}, device="pump-1"), "execution_policy": {
            "condition_guards": [{"node_uuid": condition, "equals": False}],
        }},
    ])
    child_uuid = child["workflow"]["uuid"]
    _ok(client.post(f"/api/v1/workflows/{child_uuid}/publications", json={"revision": child["workflow"]["revision"]}), 201)
    parent = _ok(client.post("/api/v1/workflows", json={"name": "条件父流程"}), 201)
    invocation = str(uuid4())
    inserted = _insert(client, service, parent["uuid"], parent["revision"], child_uuid,
                       invocation_uuid=invocation, param={})
    nodes = {node["uuid"]: node for node in inserted["nodes"]}
    expanded_condition = expanded_node_uuid(invocation, condition)
    expanded_probe = expanded_node_uuid(invocation, probe)
    assert nodes[expanded_condition]["param"]["expression"]["field"] == {"node_uuid": expanded_probe}
    task = _ok(_task(client, parent["uuid"]), 201)
    wait_for(lambda: len(executor.dispatched) == 1)
    assert executor.dispatched[0]["node_id"] == expanded_probe
    executor.finish(0, {"ready": True})
    wait_for(lambda: len(executor.dispatched) == 2)
    assert executor.dispatched[1]["node_id"] == expanded_node_uuid(invocation, chosen)
    executor.finish(1)
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
    runs = {run["workflow_node_uuid"]: run for run in service.list_workflow_node_runs(task["uuid"])}
    assert runs[expanded_node_uuid(invocation, skipped)]["status"] == "skipped"


def test_paused_edit_changes_only_unconsumed_entry_parameters(stack) -> None:
    service, executor, client = stack
    first, second = str(uuid4()), str(uuid4())
    graph = _workflow(client, "暂停改参", [
        _node(first, "加样", {"volume": 1}, device="pump-1"),
        _node(second, "加热", {"temperature": 1}, device="heater-1", depends_on=[first]),
    ])
    workflow = _ok(_declare(
        client, graph,
        input_contract=_contract(
            {"name": "volume", "schema": NUMBER, "required": True},
            {"name": "temperature", "schema": NUMBER, "required": True},
        ),
        node_params={first: {"volume": ref("volume")}, second: {"temperature": ref("temperature")}},
    ))
    task = _ok(_task(client, workflow["workflow"]["uuid"],
                     input={"volume": 1, "temperature": 20}), 201)
    wait_for(lambda: len(executor.dispatched) == 1)

    # 运行中（未暂停）不能改
    assert _code(_edit(client, task, {"temperature": 30})) == 3003
    assert _ok(_command(client, task, "pause"), 201)["status"] == "succeeded"
    assert service.get_workflow_task(task["uuid"])["control_status"] == "paused"
    revision = service.get_workflow_task(task["uuid"])["control_revision"]

    assert _code(_edit(client, task, {"temperature": 30}, revision=revision - 1)) == 3003
    assert _code(_edit(client, task, {"pressure": 1})) == 1000
    assert _code(_edit(client, task, {"temperature": "hot"})) == 1000
    frozen = _edit(client, task, {"volume": 9})
    assert _code(frozen) == 3003 and "volume" in frozen.json()["error"]["msg"]

    edited = _ok(_edit(client, task, {"temperature": 30}))
    assert edited["input"] == {"volume": 1, "temperature": 30}
    assert edited["control_revision"] == revision + 1
    assert edited["control_status"] == "paused"
    planned = {node["uuid"]: node for node in edited["execution_plan"]["nodes"]}
    assert planned[second]["param"] == {"temperature": 30}
    assert planned[first]["param"] == {"volume": 1}
    # 已被下发节点用过的值不变；未改动的冻结参数同值提交是空操作
    assert _ok(_edit(client, task, {"volume": 1}))["input"]["volume"] == 1

    executor.finish(0)
    time.sleep(0.1)
    assert len(executor.dispatched) == 1, "暂停中不派发后继"
    assert _ok(_command(client, task, "resume"), 201)["status"] == "succeeded"
    assert service.get_workflow_task(task["uuid"])["control_status"] == "active"
    wait_for(lambda: len(executor.dispatched) == 2)
    assert executor.dispatched[1]["node_id"] == second
    assert executor.dispatched[1]["action_args"] == {"temperature": 30}
    executor.finish(1)
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
    jobs = {job["workflow_node_uuid"]: job for job in service.list_workflow_node_jobs(task["uuid"])}
    assert jobs[second]["param"] == {"temperature": 30}
    assert _code(_edit(client, task, {"temperature": 40})) == 3003


_V3_COMMAND_CHECK = "type IN ('step', 'pause', 'resume', 'cancel')"


def test_v3_runtime_database_migrates_task_commands_to_v4(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    workflow = service.create_workflow(name="迁移", tags=[], description=None, meta_data={})
    service.save_graph(workflow["uuid"], revision=1, nodes=[
        _node(str(uuid4()), "加样", {"volume": 1}, device="pump-1"),
    ], edges=[])
    task = service.create_workflow_task(
        workflow_uuid=workflow["uuid"], run_mode="step", target_node_uuid=None,
        input_value={}, description=None, meta_data={},
    )
    service.command_workflow_task(task["uuid"], command_type="resume",
                                  expected_revision=0, idempotency_key="resume-1")
    service.close()

    v3_sql = _WORKFLOW_TASK_COMMAND_V4.replace(
        "type IN ('step', 'pause', 'resume', 'cancel', 'edit_input')", _V3_COMMAND_CHECK,
    )
    assert v3_sql != _WORKFLOW_TASK_COMMAND_V4
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        rebuild_table_migration(
            3, "test downgrade", table="workflow_task_command",
            create_sql=v3_sql, indexes=_WORKFLOW_TASK_COMMAND_V4_INDEXES,
        ).apply(connection)
        connection.execute("UPDATE schema_identity SET checksum = 'runtime-v3' WHERE database_key = 'runtime'")
        connection.execute("PRAGMA user_version = 3")
        connection.execute("COMMIT")
    finally:
        connection.close()

    service = WorkflowService(path)
    try:
        migrated = service.get_workflow_task(task["uuid"])
        assert migrated["control_revision"] == 1
        with service.transaction() as conn:
            conn.execute(
                "INSERT INTO workflow_task_command(uuid, create_time, update_time, workflow_task_uuid, "
                "type, idempotency_key, status) VALUES (?, 't', 't', ?, 'edit_input', 'edit-1', 'succeeded')",
                (str(uuid4()), task["uuid"]),
            )
        assert service.get_workflow_task(task["uuid"])["control_revision"] == 2
    finally:
        service.close()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SPECS["runtime"].schema_version
        history = [tuple(row) for row in connection.execute("SELECT version, description FROM schema_migration")]
        assert history == [(4, "workflow_task_command 允许 edit_input（暂停时修改入口参数）"),
                           (5, "持久化派发资源声明、租约与栅栏计数"),
                           (6, "执行代次、一次开始许可与不可变开始回执"),
                           (7, "区间资源所有权和单次动作使用清单"),
                           (8, "设备托管、资源借还和等待环路"),
                           (9, "工作流类型、实验操作类别与节点人工确认配置（V1 作者合同）"),
                           (10, "V1 任务生命周期：优先级、库存绑定、派发闸门、命令类型与人工交互"),
                           (11, "V1 工站错误处置：工站控制、控制命令、处置会话与工站错误"),
                           (12, "V1 Workspace Host：工作区操作记录与快照修订号")]
    finally:
        connection.close()


def test_task_list_omits_output_but_detail_keeps_it(stack) -> None:
    """列表只给摘要；output 可能很大，只在任务详情里返回。"""

    _service, executor, client = stack
    graph = _workflow(client, "列表摘要", [_node(str(uuid4()), "加样", {"volume": 1}, device="pump-1")])
    task = _ok(_task(client, graph["workflow"]["uuid"]), 201)
    wait_for(lambda: len(executor.dispatched) == 1)
    executor.finish(0, {"volume": 1})
    wait_for(lambda: _ok(client.get(f"/api/v1/workflow-tasks/{task['uuid']}"))["status"] == "succeeded")

    listed = client.get("/api/v1/workflow-tasks").json()["data"]
    assert listed["items"], "需要至少一个任务"
    for item in listed["items"]:
        assert "output" not in item
        detail = client.get(f"/api/v1/workflow-tasks/{item['uuid']}").json()["data"]
        assert "output" in detail and "input" in detail
