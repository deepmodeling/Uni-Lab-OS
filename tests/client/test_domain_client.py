from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from unilabos.client.domain import DomainBackendClient, DomainClientError, DomainSource


def _source(authority: str = "local") -> DomainSource:
    return DomainSource(
        authority=authority,
        endpoint="http://domain.test",
        workspace_path="/workspace",
        host_revision=12,
        generation="backend-generation" if authority == "local" else None,
        edge_generation="edge-generation",
    )


def test_domain_source_uses_backend_url_without_claiming_remote_generation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class Host:
        def snapshot(self):
            return {
                "workspacePath": str(tmp_path),
                "revision": 9,
                "configuration": {
                    "domainMode": "backend",
                    "backendUrl": "http://backend.test/",
                },
                "components": {
                    "backend": {
                        "phase": "ready",
                        "address": "http://127.0.0.1:48197",
                        "generation": "authoring-only-generation",
                    },
                    "edge": {"generation": "edge-9"},
                },
            }

    monkeypatch.setattr(
        "unilabos.client.domain.ensure_workspace_host", lambda _workspace: Host()
    )

    source = DomainSource.discover(tmp_path)

    assert source.authority == "backend"
    assert source.api_base_url == "http://backend.test/api/v1"
    assert source.generation is None
    assert source.edge_generation == "edge-9"
    assert source.source_id.startswith("backend:sha256:")


def test_run_and_debug_return_stable_operation_source_and_generation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = request.read().decode("utf-8")
        if request.url.path.endswith("/debug/workflow-tasks"):
            assert '"start_node_uuids":["node-start"]' in body
            assert '"breakpoint_node_uuids":["node-break"]' in body
            return httpx.Response(201, json={"code": 0, "data": {"uuid": "debug-task"}})
        assert request.url.path.endswith("/workflow-tasks")
        assert '"run_mode":"single_node"' in body
        return httpx.Response(
            201,
            json={
                "code": 0,
                "data": {
                    "uuid": "task-1",
                    "workflow_snapshot": {"workflow": {"revision": 7}},
                },
            },
        )

    with DomainBackendClient(
        _source(), transport=httpx.MockTransport(handler)
    ) as client:
        run = client.create_task(
            "workflow-1",
            run_mode="single_node",
            target_node_uuid="node-1",
            operation_id="operation-1",
        )
        debug = client.create_debug_task(
            "workflow-1",
            start_node_uuid="node-start",
            breakpoint_node_uuids=["node-break"],
            operation_id="operation-2",
        )

    assert run["operationId"] == "operation-1"
    assert run["taskUuid"] == "task-1"
    assert run["revision"] == 7
    assert run["generation"] == "backend-generation"
    assert run["edgeGeneration"] == "edge-generation"
    assert debug["taskUuid"] == "debug-task"
    assert [request.url.path for request in requests] == [
        "/api/v1/workflow-tasks",
        "/api/v1/debug/workflow-tasks",
    ]


def test_watch_uses_exclusive_cursor_without_duplicate_and_stops_at_terminal() -> None:
    event_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal event_calls
        if request.url.path.endswith("/events"):
            event_calls += 1
            assert request.url.params["after_sequence"] in {"0", "4"}
            if event_calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "sequence": 4,
                                    "kind": "job_transition",
                                    "to_status": "succeeded",
                                }
                            ],
                            "next_cursor": 4,
                            "has_more": False,
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [], "next_cursor": 4, "has_more": False},
                },
            )
        if request.url.path.endswith("/workflow-tasks/task-1"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"uuid": "task-1", "status": "succeeded"}},
            )
        raise AssertionError(request.url)

    with DomainBackendClient(
        _source(), transport=httpx.MockTransport(handler)
    ) as client:
        events = list(client.watch_task("task-1", timeout=1, poll_interval=0))

    assert [event.get("cursor") for event in events] == [4, 4]
    assert events[0]["data"]["kind"] == "job_transition"
    assert events[1]["data"]["kind"] == "task_terminal"


@pytest.mark.parametrize("new_cursor", [0, 2, 20])
def test_sse_reset_rehydrates_task_even_when_cursor_moves_backwards(new_cursor: int) -> None:
    """窗口过期或数据库重置的通知不能被去重逻辑吞掉。"""
    task_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_reads
        if request.url.path.endswith("/workflow-tasks/task-1/events"):
            return httpx.Response(404)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
                f'id: {new_cursor}\nevent: stream.reset\ndata: {{"reason":"cursor_expired"}}\n\n'
            ))
        if request.url.path.endswith("/workflow-tasks/task-1/jobs"):
            return httpx.Response(200, json={"code": 0, "data": []})
        if request.url.path.endswith("/workflow-tasks/task-1"):
            task_reads += 1
            return httpx.Response(200, json={"code": 0, "data": {
                "uuid": "task-1", "status": "pending" if task_reads == 1 else "succeeded",
            }})
        raise AssertionError(request.url)

    with DomainBackendClient(_source(), transport=httpx.MockTransport(handler)) as client:
        events = list(client.watch_task("task-1", after=7, timeout=1))
    assert len(events) == 1
    assert events[0]["cursor"] == new_cursor
    assert events[0]["data"]["task"]["status"] == "succeeded"


def test_backend_watch_falls_back_to_durable_sse_and_hydrates_task() -> None:
    task_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_reads
        if request.url.path.endswith("/workflow-tasks/task-1/events"):
            return httpx.Response(404)
        if request.url.path.endswith("/events"):
            assert request.headers["last-event-id"] == "7"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'id: 8\nevent: workflow.task.changed\n'
                    'data: {"workflow_task_uuid":"task-1"}\n\n'
                ),
            )
        if request.url.path.endswith("/workflow-tasks/task-1/jobs"):
            return httpx.Response(200, json={"code": 0, "data": []})
        if request.url.path.endswith("/workflow-tasks/task-1"):
            task_reads += 1
            status = "pending" if task_reads == 1 else "succeeded"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"uuid": "task-1", "status": status}},
            )
        raise AssertionError(request.url)

    with DomainBackendClient(
        _source("backend"), transport=httpx.MockTransport(handler)
    ) as client:
        events = list(client.watch_task("task-1", after=7, timeout=1))

    assert len(events) == 1
    assert events[0]["cursor"] == 8
    assert events[0]["data"]["kind"] == "task_changed"
    assert events[0]["data"]["task"]["status"] == "succeeded"


def test_backend_authority_rejects_code_driven_authoring_wait() -> None:
    with DomainBackendClient(
        _source("backend"),
        transport=httpx.MockTransport(lambda request: pytest.fail(str(request.url))),
    ) as client:
        with pytest.raises(DomainClientError) as captured:
            client.wait_authoring("workflow-1", after_revision=3, timeout=0)

    assert captured.value.code == "authoring_not_active"
    assert "代码不驱动画布" in captured.value.message
