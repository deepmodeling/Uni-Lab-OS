"""工作流定义变更（创建 / 修改 / 保存图 / 发布 / 删除）与变更同一事务写 ``workflow.authoring.changed``。

其他打开的页面靠这条失效通知重读工作流列表；以前只有 ``@workflow`` 源码创作路径会发。
用例经 HTTP 写入，再从 ``/api/v1/events`` 以游标 0 回放读取通知。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from unilabos.server.api.runtime import events
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import WorkflowService

AUTHORING_EVENT = "workflow.authoring.changed"


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(events, "shutting_down", lambda: False)
    workflow = WorkflowService(":memory:")
    yield workflow
    workflow.close()


def _ok(response, status: int = 200):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body.get("data")


async def _replay(app, *, through: int) -> str:
    """从游标 0 回放 /api/v1/events（TestClient 会缓冲流式响应，这里直接驱动 ASGI），读到序号 ``through`` 后断开。"""

    chunks: list[str] = []
    changed = asyncio.Event()
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b"").decode("utf-8"))
            changed.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/api/v1/events", "raw_path": b"/api/v1/events", "query_string": b"",
        "root_path": "", "headers": [(b"last-event-id", b"0")], "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        while f"id: {through}\nevent:" not in "".join(chunks):
            changed.clear()
            await asyncio.wait_for(changed.wait(), 5.0)
    finally:
        disconnect.set()
        await asyncio.wait_for(task, 5.0)
    return "".join(chunks)


def _authoring_notices(app, service: WorkflowService) -> list[dict]:
    latest = service.latest_event_sequence()
    if latest == 0:
        return []
    notices = []
    for frame in asyncio.run(_replay(app, through=latest)).split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.split("\n") if ": " in line)
        if fields.get("event") == AUTHORING_EVENT:
            notices.append(json.loads(fields["data"]))
    return notices


def test_definition_changes_over_http_notify_other_pages(service: WorkflowService) -> None:
    app = create_workflow_app(service)
    with TestClient(app) as client:
        workflow = _ok(client.post("/api/v1/workflows", json={"name": "加样"}), 201)
        identity = workflow["uuid"]
        _ok(client.put(f"/api/v1/workflows/{identity}", json={"name": "加样 v2", "tags": [], "meta_data": {}}))
        graph = _ok(client.put(
            f"/api/v1/workflows/{identity}/graph",
            json={"revision": workflow["revision"], "nodes": [], "edges": []},
        ))
        # 声明 I/O 走服务端保留元数据的整图保存（save_graph_with_reserved_metadata）
        declared = _ok(client.put(f"/api/v1/workflows/{identity}/io", json={
            "revision": graph["workflow"]["revision"],
            "input_contract": {"version": 1, "parameters": []},
            "output_contract": {"version": 1, "outputs": []},
            "output_bindings": {},
            "node_input_bindings": {},
        }))
        revision = declared["workflow"]["revision"]
        _ok(client.post(f"/api/v1/workflows/{identity}/publications", json={"revision": revision}), 201)
        # 同一修订重复发布返回既有发布，不是新的变更
        _ok(client.post(f"/api/v1/workflows/{identity}/publications", json={"revision": revision}), 201)
        _ok(client.delete(f"/api/v1/workflows/{identity}"))

    assert _authoring_notices(app, service) == [
        {"workflow_uuid": identity, "change": change}
        for change in ("created", "updated", "graph_saved", "graph_saved", "published", "deleted")
    ]


def test_operation_kind_is_written_and_announced_with_the_workflow(service: WorkflowService) -> None:
    """实验操作的类型 / 类别随创建同一事务写入（一条 created）；之后单独改类别也有通知，未变化不发。"""

    app = create_workflow_app(service)
    with TestClient(app) as client:
        first, second = _ok(client.get("/api/v1/experiment-operation-categories"))["items"][:2]
        workflow = _ok(client.post("/api/v1/workflows", json={
            "name": "离心", "workflow_type": "experiment_operation",
            "operation_category_uuid": first["uuid"],
        }), 201)
        identity = workflow["uuid"]
        assert (workflow["workflow_type"], workflow["operation_category_uuid"]) == (
            "experiment_operation", first["uuid"],
        )
        assert _authoring_notices(app, service) == [{"workflow_uuid": identity, "change": "created"}]

        service.operation_categories.set_workflow_kind(
            identity, workflow_type="experiment_operation", category=first["uuid"],
        )
        assert len(_authoring_notices(app, service)) == 1

        _ok(client.put(f"/api/v1/workflows/{identity}", json={
            "name": "离心", "tags": [], "meta_data": {}, "operation_category_uuid": second["uuid"],
        }))
        assert _ok(client.get(f"/api/v1/workflows/{identity}"))["operation_category_uuid"] == second["uuid"]
    later = _authoring_notices(app, service)[1:]
    assert later and all(notice == {"workflow_uuid": identity, "change": "updated"} for notice in later)
