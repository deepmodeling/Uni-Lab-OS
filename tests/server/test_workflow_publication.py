"""工作流即节点：声明 I/O → 发布不可变修订 → 作为组合调用插入父图 → 内联展开执行。"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.authoring_identity import expanded_node_uuid
from unilabos.server.services.runtime.workflow.service import WorkflowService

DEVICE_TEMPLATE = "81000000-0000-4000-8000-000000000001"
DEVICE_RESOURCE = "81000000-0000-4000-8000-000000000002"
VOLUME_TARGET = "81000000-0000-4000-8000-000000000003"
VOLUME_SOURCE = "81000000-0000-4000-8000-000000000004"
READY_TARGET = "81000000-0000-4000-8000-000000000005"
READY_SOURCE = "81000000-0000-4000-8000-000000000006"
NUMBER = {"type": "number"}


class Executor:
    def __init__(self):
        self.dispatched = []

    def add_job_finished_listener(self, callback):
        self.callback = callback

    def dispatch(self, payload):
        self.dispatched.append(payload)

    def cancel_task(self, task_uuid):
        return []

    def finish(self, index, *, success=True):
        self.callback(self.dispatched[index]["job_id"], success, {"index": index}, "normal", {})


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
    _install_device_template(service)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    with TestClient(create_workflow_app(service)) as client:
        yield service, executor, client
    service.set_task_submitter(None)
    scheduler.stop()
    service.close()


def _install_device_template(service: WorkflowService) -> None:
    """注册表尚未同步节点模板表：测试直接写入一个带 Handle 的设备动作模板。"""

    now = "2026-11-05T00:00:00Z"
    with service.transaction() as conn:
        conn.execute(
            """
            INSERT INTO workflow_node_template(
                uuid, create_time, update_time, deleted_at, description, meta_data,
                authority_id, resource_template_uuid, name, display_name, class,
                goal, goal_default, feedback, result, schema, type, node_type
            ) VALUES (?, ?, ?, NULL, NULL, '{}', 'fixture', ?, 'dose', '加样',
                      'fixture.devices:Pump', '{"volume":"volume"}', '{}', '{}',
                      '{"volume":"volume"}', NULL, 'action', 'device')
            """,
            (DEVICE_TEMPLATE, now, now, DEVICE_RESOURCE),
        )
        value_meta = '{"unilab":{"value_schema":{"type":"number"}}}'
        for identity, key, io_type, kind, required, source, data_key, meta in (
            (VOLUME_TARGET, "volume", "target", "number", 1, "executor", "volume", value_meta),
            (VOLUME_SOURCE, "volume", "source", "number", 0, "executor", "volume", value_meta),
            (READY_TARGET, "ready", "target", "default", 0, None, None, "{}"),
            (READY_SOURCE, "ready", "source", "default", 0, None, None, "{}"),
        ):
            conn.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, deleted_at, description, meta_data,
                    authority_id, workflow_node_template_uuid, handle_key, io_type,
                    display_name, type, required, data_source, data_key
                ) VALUES (?, ?, ?, NULL, NULL, ?, 'fixture', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (identity, now, now, meta, DEVICE_TEMPLATE, key, io_type, key, kind,
                 required, source, data_key),
            )


def _ok(response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body.get("data")


def _create_workflow(client, name: str) -> dict:
    return _ok(client.post("/api/v1/workflows", json={"name": name}), 201)


def _dose_node(node_uuid: str, *, volume: float = 1, device: str = "pump-1") -> dict:
    return {
        "uuid": node_uuid,
        "workflow_node_template_uuid": DEVICE_TEMPLATE,
        "name": "加样",
        "type": "device_action",
        "material_uuid": str(uuid4()),
        "action_name": "dose",
        "action_type": "UniLabJsonCommand",
        "param": {"volume": volume},
        "pose": {"x": 0, "y": 0},
        "meta_data": {"target_device_id": device},
    }


def _child_with_volume_input(client) -> tuple[dict, str, dict]:
    """子工作流：一个设备动作，volume 输入绑定到工作流输入，并把输出公开为 dosed。"""

    child = _create_workflow(client, "加样子流程")
    node_uuid = str(uuid4())
    graph = _ok(client.put(
        f"/api/v1/workflows/{child['uuid']}/graph",
        json={"revision": child["revision"], "nodes": [_dose_node(node_uuid)],
              "edges": [], "site_binding_mode": "preserve"},
    ))
    io = _ok(client.put(
        f"/api/v1/workflows/{child['uuid']}/io",
        json={
            "revision": graph["workflow"]["revision"],
            "input_contract": {"version": 1, "parameters": [
                {"name": "volume", "schema": NUMBER, "required": True, "title": "体积"},
            ]},
            "output_contract": {"version": 1, "outputs": [{"name": "dosed", "schema": NUMBER}]},
            "output_bindings": {"dosed": {
                "kind": "node_output", "workflow_node_uuid": node_uuid,
                "source_handle_uuid": VOLUME_SOURCE,
            }},
            "node_input_bindings": {node_uuid: {VOLUME_TARGET: {"parameter": "volume"}}},
        },
    ))
    return child, node_uuid, io


def _publish(client, workflow_uuid: str, revision: int) -> dict:
    return _ok(client.post(
        f"/api/v1/workflows/{workflow_uuid}/publications", json={"revision": revision},
    ), 201)


def _insert(client, parent: dict, child_uuid: str, **extra) -> Any:
    revision = client.get(f"/api/v1/workflows/{parent['uuid']}").json()["data"]["revision"]
    return client.post(
        f"/api/v1/workflows/{parent['uuid']}/composite-invocations",
        json={"revision": revision, "child_workflow_uuid": child_uuid, **extra},
    )


def test_declare_publish_insert_and_run_child_inline(stack) -> None:
    service, executor, client = stack
    child, node_uuid, io = _child_with_volume_input(client)

    # I/O 写入受保护的 meta_data.unilab，revision 与图保存同样 +1；绑定输入的静态值移除
    assert io["workflow"]["revision"] == child["revision"] + 2
    unilab = io["workflow"]["meta_data"]["unilab"]
    assert unilab["input_contract"]["parameters"][0]["name"] == "volume"
    inner = io["nodes"][0]
    assert inner["meta_data"]["unilab"]["input_bindings"] == {VOLUME_TARGET: {"parameter": "volume"}}
    assert "volume" not in inner["param"]

    # 之后的 PUT /graph（客户端不回传 unilab）保留这些服务端字段
    resubmitted = {**inner, "meta_data": {"target_device_id": "pump-1"}}
    regraph = _ok(client.put(
        f"/api/v1/workflows/{child['uuid']}/graph",
        json={"revision": io["workflow"]["revision"], "nodes": [resubmitted], "edges": [],
              "site_binding_mode": "preserve"},
    ))
    assert regraph["workflow"]["meta_data"]["unilab"] == unilab
    assert regraph["nodes"][0]["meta_data"]["unilab"]["input_bindings"] == {
        VOLUME_TARGET: {"parameter": "volume"}
    }
    revision = regraph["workflow"]["revision"]

    publication = _publish(client, child["uuid"], revision)
    assert publication["revision"] == revision
    assert publication["source_hash"].startswith("sha256:")
    assert publication["contract_digest"].startswith("sha256:")
    assert [item["name"] for item in publication["inputs"]] == ["volume"]
    assert [item["name"] for item in publication["outputs"]] == ["dosed"]
    template = publication["node_template"]
    assert template["node_type"] == "workflow" and isinstance(template["schema"], str)
    assert {(item["io_type"], item["handle_key"]) for item in publication["handle_templates"]} == {
        ("target", "volume"), ("source", "dosed"), ("target", "ready"), ("source", "ready"),
    }
    # 同一修订重复发布返回同一条；非当前修订不能新发布
    assert _publish(client, child["uuid"], revision)["uuid"] == publication["uuid"]
    stale = client.post(
        f"/api/v1/workflows/{child['uuid']}/publications", json={"revision": revision - 1},
    ).json()
    assert stale["code"] == 3003
    listed = _ok(client.get(f"/api/v1/workflows/{child['uuid']}/publications"))
    assert [item["uuid"] for item in listed] == [publication["uuid"]]
    palette = _ok(client.get("/api/v1/workflow-publications"))
    assert [(item["workflow_uuid"], item["revision"]) for item in palette] == [
        (child["uuid"], revision)
    ]
    assert palette[0]["node_template"]["uuid"] == template["uuid"]

    parent = _create_workflow(client, "父流程")
    invocation_uuid = str(uuid4())
    inserted = _ok(_insert(
        client, parent, child["uuid"],
        invocation_uuid=invocation_uuid, arguments={"volume": 5}, pose={"x": 10, "y": 20},
    ), 201)
    assert inserted["invocation_uuid"] == invocation_uuid
    assert inserted["contract_pin"]["child_workflow_revision"] == revision
    assert inserted["contract_pin"]["contract_digest"] == publication["contract_digest"]
    graph = inserted["graph"]
    assert graph["workflow"]["revision"] == parent["revision"] + 1
    by_uuid = {node["uuid"]: node for node in graph["nodes"]}
    expanded = expanded_node_uuid(invocation_uuid, node_uuid)
    assert set(by_uuid) == {invocation_uuid, expanded}
    assert by_uuid[invocation_uuid]["name"] == "加样子流程"
    assert by_uuid[invocation_uuid]["pose"] == {"x": 10, "y": 20}
    assert by_uuid[invocation_uuid]["param"] == {"volume": 5}
    assert by_uuid[expanded]["parent_uuid"] == invocation_uuid
    assert by_uuid[expanded]["param"]["volume"] == 5
    assert not by_uuid[expanded]["meta_data"]["unilab"].get("input_bindings")
    assert template["uuid"] in {item["uuid"] for item in graph["node_templates"]}
    # 公共读取（Backend 类型校验）接受落库的已发布模板；前端回存整图不丢组合元数据
    hydrated = _ok(client.get(f"/api/v1/workflows/{parent['uuid']}/graph"))
    stored = next(item for item in hydrated["node_templates"] if item["uuid"] == template["uuid"])
    assert stored["node_type"] == "workflow" and stored["schema"] == template["schema"]
    public_nodes = [
        {**node, "meta_data": {k: v for k, v in node["meta_data"].items() if k != "unilab"}}
        for node in hydrated["nodes"]
    ]
    resaved = _ok(client.put(
        f"/api/v1/workflows/{parent['uuid']}/graph",
        json={"revision": hydrated["workflow"]["revision"], "nodes": public_nodes,
              "edges": hydrated["edges"], "site_binding_mode": "preserve"},
    ))
    assert {node["uuid"]: node["meta_data"] for node in resaved["nodes"]} == {
        node["uuid"]: node["meta_data"] for node in hydrated["nodes"]
    }

    task = _ok(client.post("/api/v1/workflow-tasks", json={"workflow_uuid": parent["uuid"]}), 201)
    # 组合调用节点本身不产生 Job；只有展开出的设备动作被派发，静态实参已下推
    assert [node["uuid"] for node in task["execution_plan"]["nodes"]] == [expanded]
    wait_for(lambda: len(executor.dispatched) == 1)
    dispatched = executor.dispatched[0]
    assert dispatched["node_id"] == expanded
    assert dispatched["device_id"] == "pump-1"
    assert dispatched["action"] == "dose"
    assert dispatched["action_args"]["volume"] == 5
    executor.finish(0)
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")


def test_declare_io_maps_validation_and_revision_errors(stack) -> None:
    _service, _executor, client = stack
    child = _create_workflow(client, "坏绑定")
    node_uuid = str(uuid4())
    graph = _ok(client.put(
        f"/api/v1/workflows/{child['uuid']}/graph",
        json={"revision": child["revision"], "nodes": [_dose_node(node_uuid)],
              "edges": [], "site_binding_mode": "preserve"},
    ))
    revision = graph["workflow"]["revision"]
    contract = {"version": 1, "parameters": [{"name": "volume", "schema": NUMBER}]}

    unknown_parameter = client.put(f"/api/v1/workflows/{child['uuid']}/io", json={
        "revision": revision, "input_contract": contract,
        "node_input_bindings": {node_uuid: {VOLUME_TARGET: {"parameter": "missing"}}},
    }).json()
    assert unknown_parameter["code"] == 1000
    assert "workflow_io_invalid" in unknown_parameter["error"]["msg"]

    stale = client.put(f"/api/v1/workflows/{child['uuid']}/io", json={
        "revision": revision - 1, "input_contract": contract,
    }).json()
    assert stale["code"] == 3003

    unknown_node = client.put(f"/api/v1/workflows/{child['uuid']}/io", json={
        "revision": revision, "input_contract": contract,
        "node_input_bindings": {str(uuid4()): {VOLUME_TARGET: {"parameter": "volume"}}},
    }).json()
    assert unknown_node["code"] == 1000

    # 解绑必填输入时用 node_params 同时给回静态值
    bound = _ok(client.put(f"/api/v1/workflows/{child['uuid']}/io", json={
        "revision": revision, "input_contract": contract,
        "node_input_bindings": {node_uuid: {VOLUME_TARGET: {"parameter": "volume"}}},
    }))
    without_value = client.put(f"/api/v1/workflows/{child['uuid']}/io", json={
        "revision": bound["workflow"]["revision"], "input_contract": contract,
    }).json()
    assert without_value["code"] == 1000
    unbound = _ok(client.put(f"/api/v1/workflows/{child['uuid']}/io", json={
        "revision": bound["workflow"]["revision"], "input_contract": contract,
        "node_params": {node_uuid: {"volume": 2}},
    }))
    assert unbound["nodes"][0]["param"]["volume"] == 2
    assert "unilab" not in unbound["nodes"][0]["meta_data"]


def test_insert_rejects_unpublished_missing_argument_and_recursion(stack) -> None:
    _service, _executor, client = stack
    child, _node_uuid, io = _child_with_volume_input(client)
    parent = _create_workflow(client, "父流程")

    assert _insert(client, parent, child["uuid"], arguments={"volume": 1}).json()["code"] == 3002
    _publish(client, child["uuid"], io["workflow"]["revision"])

    missing = _insert(client, parent, child["uuid"]).json()
    assert missing["code"] == 1000
    assert "composite_boundary_mapping_invalid" in missing["error"]["msg"]
    wrong_type = _insert(client, parent, child["uuid"], arguments={"volume": "many"}).json()
    assert wrong_type["code"] == 1000

    itself = _insert(client, child, child["uuid"], arguments={"volume": 1}).json()
    assert itself["code"] == 1000
    assert "composite_recursive_reference" in itself["error"]["msg"]

    # parent 内含 child@n 并发布后，再把 parent 插回 child：子图包含父图，拒绝且不改动 child
    _ok(_insert(client, parent, child["uuid"], arguments={"volume": 1}), 201)
    parent_revision = client.get(f"/api/v1/workflows/{parent['uuid']}").json()["data"]["revision"]
    _publish(client, parent["uuid"], parent_revision)
    before = _ok(client.get(f"/api/v1/workflows/{child['uuid']}/graph"))
    cycle = _insert(client, child, parent["uuid"]).json()
    assert cycle["code"] == 1000
    assert "composite_recursive_reference" in cycle["error"]["msg"]
    assert _ok(client.get(f"/api/v1/workflows/{child['uuid']}/graph")) == before


def test_nested_publication_expands_each_leaf_once(stack) -> None:
    service, executor, client = stack
    child, node_uuid, io = _child_with_volume_input(client)
    _publish(client, child["uuid"], io["workflow"]["revision"])

    middle = _create_workflow(client, "中间流程")
    inner_invocation = str(uuid4())
    _ok(_insert(client, middle, child["uuid"], invocation_uuid=inner_invocation,
                arguments={"volume": 3}), 201)
    middle_revision = client.get(f"/api/v1/workflows/{middle['uuid']}").json()["data"]["revision"]
    _publish(client, middle["uuid"], middle_revision)

    outer = _create_workflow(client, "外层流程")
    outer_invocation = str(uuid4())
    graph = _ok(_insert(client, outer, middle["uuid"], invocation_uuid=outer_invocation), 201)["graph"]
    nested_invocation = expanded_node_uuid(outer_invocation, inner_invocation)
    leaf = expanded_node_uuid(nested_invocation, node_uuid)
    assert {node["uuid"] for node in graph["nodes"]} == {outer_invocation, nested_invocation, leaf}

    task = _ok(client.post("/api/v1/workflow-tasks", json={"workflow_uuid": outer["uuid"]}), 201)
    wait_for(lambda: len(executor.dispatched) == 1)
    assert executor.dispatched[0]["node_id"] == leaf
    assert executor.dispatched[0]["action_args"]["volume"] == 3
    executor.finish(0)
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
    assert len(executor.dispatched) == 1
