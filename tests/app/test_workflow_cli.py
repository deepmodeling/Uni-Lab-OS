from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any

from unilabos.app.cli import workflow as workflow_cli
from unilabos.app.cli.parser import build_parser
from unilabos.app.cli.router import run_client_command
from unilabos.client.workflow import HTTPWorkflowClient


def test_workflow_parser_exposes_only_supported_authority_commands() -> None:
    parser = build_parser()
    cases = [
        (["workflow", "list", "--json"], "list"),
        (["workflow", "inspect", "task-1", "--kind", "task"], "inspect"),
        (
            ["workflow", "run", "workflow-1", "--follow", "--jsonl"],
            "run",
        ),
        (
            ["workflow", "watch", "task-1", "--after", "8", "--jsonl"],
            "watch",
        ),
        (
            [
                "workflow",
                "authoring",
                "workflow-1",
                "--after-revision",
                "9",
            ],
            "authoring",
        ),
    ]

    for argv, action in cases:
        parsed = parser.parse_args(argv)
        assert parsed.command == "workflow"
        assert parsed.workflow_command == action
        assert not hasattr(parsed, "legacy")

    workflow_choices = (
        parser._subparsers._group_actions[0]
        .choices["workflow"]
        ._subparsers._group_actions[0]
        .choices
    )
    assert set(workflow_choices) == {
        "upload",
        "list",
        "inspect",
        "run",
        "watch",
        "authoring",
    }


def test_workflow_list_uses_current_api_without_legacy(
    monkeypatch,
    capsys,
) -> None:
    calls: list[Any] = []

    class _Client:
        def list_workflows(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(("list", kwargs))
            return {"items": [{"uuid": "workflow-1"}], "total": 1}

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(
        workflow_cli,
        "_create_workflow_client",
        lambda _args, _session: _Client(),
    )
    parser = build_parser()
    args = parser.parse_args(["workflow", "list", "--page", "2", "--page-size", "5"])

    assert run_client_command(args, parser, session_manager=object()) is True
    assert calls == [
        ("list", {"page": 2, "page_size": 5, "name": ""}),
        ("close",),
    ]
    assert "workflow-1" in capsys.readouterr().out


def test_workflow_cli_dispatches_inspect_run_watch_and_authoring(
    monkeypatch,
    capsys,
) -> None:
    calls: list[Any] = []

    class _Client:
        def inspect_task(self, identity: str) -> dict[str, Any]:
            calls.append(("inspect_task", identity))
            return {"task": {"uuid": identity}, "jobs": []}

        def create_task(self, identity: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(("create_task", identity, kwargs))
            return {"uuid": "task-created", "status": "pending"}

        def watch_task(self, identity: str, **kwargs: Any):
            calls.append(("watch_task", identity, kwargs))
            yield {
                "kind": "task_snapshot",
                "task": {"uuid": identity, "status": "running"},
                "jobs": [],
                "cursor": kwargs["after"],
            }

        def wait_authoring(self, identity: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(("wait_authoring", identity, kwargs))
            return {"workflow_uuid": identity, "workflow_revision": 3}

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(
        workflow_cli,
        "_create_workflow_client",
        lambda _args, _session: _Client(),
    )
    parser = build_parser()

    commands = [
        ["workflow", "inspect", "task-1"],
        [
            "workflow",
            "run",
            "workflow-1",
            "--mode",
            "single_node",
            "--target-node",
            "node-1",
        ],
        ["workflow", "watch", "task-2", "--after", "7"],
        [
            "workflow",
            "authoring",
            "workflow-2",
            "--after-revision",
            "2",
        ],
    ]
    for argv in commands:
        assert (
            run_client_command(
                parser.parse_args(argv),
                parser,
                session_manager=object(),
            )
            is True
        )

    assert ("inspect_task", "task-1") in calls
    assert (
        "create_task",
        "workflow-1",
        {
            "run_mode": "single_node",
            "target_node_uuid": "node-1",
            "operation_id": None,
        },
    ) in calls
    assert (
        "watch_task",
        "task-2",
        {"after": 7, "timeout": 300.0, "max_events": 500},
    ) in calls
    assert (
        "wait_authoring",
        "workflow-2",
        {"after_revision": 2, "timeout": 30.0},
    ) in calls
    assert capsys.readouterr().err == ""


class _FakeHTTPClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.task_reads = 0

    def get(self, path: str, **kwargs: Any) -> Any:
        self.requests.append(("GET", path, kwargs))
        if path == "/workflow-tasks/task-1":
            self.task_reads += 1
            status = "running" if self.task_reads <= 2 else "succeeded"
            return {"uuid": "task-1", "status": status}
        if path == "/workflow-tasks/task-1/jobs":
            return [{"uuid": "job-1", "status": "succeeded"}]
        raise AssertionError(path)

    def post(self, path: str, **kwargs: Any) -> Any:
        self.requests.append(("POST", path, kwargs))
        return {"uuid": "task-created"}


class _FakeNoticeSocket:
    def __init__(self, notices: list[str]) -> None:
        self.notices = iter(notices)

    def recv(self, timeout: float | None = None) -> str:
        del timeout
        return next(self.notices)


class _FakeNoticeContext(AbstractContextManager[_FakeNoticeSocket]):
    def __init__(self, notices: list[str]) -> None:
        self.socket = _FakeNoticeSocket(notices)

    def __enter__(self) -> _FakeNoticeSocket:
        return self.socket

    def __exit__(self, *_args: object) -> None:
        return None


def test_workflow_watch_uses_ws_only_as_invalidation_then_pulls_http() -> None:
    http = _FakeHTTPClient()
    connections: list[tuple[str, dict[str, str], float]] = []
    forged_notice = json.dumps(
        {
            "action": "edge_change",
            "data": {
                "event_sequence": 9,
                "event_type": "workflow.task.changed",
                "aggregate_type": "workflow_task",
                "aggregate_uuid": "task-1",
                "task": {"uuid": "task-1", "status": "forged"},
            },
        }
    )

    def connect(url: str, headers: dict[str, str], timeout: float):
        connections.append((url, headers, timeout))
        return _FakeNoticeContext([forged_notice])

    client = HTTPWorkflowClient(
        "http://microbackend:8002",
        http_client=http,
        notice_connector=connect,
    )
    events = list(client.watch_task("task-1", after=8, timeout=1, max_events=2))

    assert [event["task"]["status"] for event in events] == [
        "running",
        "succeeded",
    ]
    assert all(event["task"]["status"] != "forged" for event in events)
    assert events[-1]["cursor"] == 9
    assert connections == [
        (
            "ws://microbackend:8003/api/v1/ws/schedule",
            {"Accept": "application/json"},
            1,
        )
    ]
    assert [request[1] for request in http.requests] == [
        "/workflow-tasks/task-1",
        "/workflow-tasks/task-1/jobs",
        "/workflow-tasks/task-1",
        "/workflow-tasks/task-1/jobs",
        "/workflow-tasks/task-1",
        "/workflow-tasks/task-1/jobs",
    ]


def test_workflow_run_payload_matches_current_api() -> None:
    http = _FakeHTTPClient()
    client = HTTPWorkflowClient(
        "http://microbackend:8002/api/v1",
        http_client=http,
        notice_connector=lambda *_args: _FakeNoticeContext([]),
    )

    client.create_task(
        "workflow-1",
        run_mode="single_node",
        target_node_uuid="node-1",
        operation_id="operation-1",
    )

    method, path, kwargs = http.requests[-1]
    assert (method, path) == ("POST", "/workflow-tasks")
    assert kwargs["json"] == {
        "workflow_uuid": "workflow-1",
        "run_mode": "single_node",
        "target_node_uuid": "node-1",
        "description": "由 unilab workflow CLI 启动",
        "meta_data": {
            "source": "unilab-workflow-client",
            "operation_id": "operation-1",
        },
    }
    assert "input" not in kwargs["json"]


def test_workflow_client_roundtrips_through_the_microbackend_api() -> None:
    """薄客户端必须与实际 Workflow Authority API 使用同一份协议。"""

    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.server.workflow.api import install_workflow_api
    from unilabos.server.workflow.models import WorkflowNodeWrite
    from unilabos.server.workflow.service import WorkflowService
    from unilabos.server.workflow.store import WorkflowStore

    service = WorkflowService(WorkflowStore(":memory:"))
    workflow = service.create_workflow(
        name="CLI authority contract",
        tags=[],
        description=None,
        meta_data={},
    )
    node_uuid = str(uuid4())
    service.save_graph(
        workflow["uuid"],
        revision=workflow["revision"],
        nodes=[
            WorkflowNodeWrite(
                uuid=node_uuid,
                name="人工确认",
                type="manual_confirm",
            )
        ],
        edges=[],
    )
    app = FastAPI()
    install_workflow_api(app, service)
    api = TestClient(app)

    class _AuthorityHTTP:
        @staticmethod
        def _data(response: Any) -> Any:
            assert response.status_code < 400, response.text
            envelope = response.json()
            assert envelope["code"] == 0, envelope
            return envelope["data"]

        def get(self, path: str, **kwargs: Any) -> Any:
            return self._data(
                api.get(f"/api/v1{path}", params=kwargs.get("params"))
            )

        def post(self, path: str, **kwargs: Any) -> Any:
            return self._data(
                api.post(f"/api/v1{path}", json=kwargs.get("json"))
            )

    client = HTTPWorkflowClient(
        "http://microbackend:8002",
        http_client=_AuthorityHTTP(),
        notice_connector=lambda *_args: _FakeNoticeContext([]),
    )
    listed = client.list_workflows(name="CLI authority")
    assert [item["uuid"] for item in listed["items"]] == [workflow["uuid"]]

    graph = client.get_workflow_graph(workflow["uuid"])
    assert [node["uuid"] for node in graph["nodes"]] == [node_uuid]

    task = client.create_task(workflow["uuid"], operation_id="cli-operation")
    inspected = client.inspect_task(task["uuid"])
    assert inspected["task"]["workflow_uuid"] == workflow["uuid"]
    assert [job["workflow_node_uuid"] for job in inspected["jobs"]] == [
        node_uuid
    ]
