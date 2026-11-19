from __future__ import annotations

import json
import uuid
from contextlib import AbstractContextManager
from typing import Any

from unilabos.app.cli import workflow as workflow_cli
from unilabos.app.cli.parser import build_parser
from unilabos.app.cli.router import run_client_command
from unilabos.client.runtime.workflow import HTTPWorkflowClient


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
        "command",
        "authoring",
    }


def test_workflow_list_calls_runtime_workflow_api(
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
            "input_value": {},
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


def test_workflow_watch_uses_sse_only_as_invalidation_then_pulls_http() -> None:
    http = _FakeHTTPClient()
    connections: list[tuple[str, dict[str, str], float]] = []
    forged_notice = json.dumps(
        {
            "action": "workflow.runtime.changed",
            "id": 9,
            "data": {
                "kind": "task",
                "workflow_task_uuid": "task-1",
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
    # 观察端只订阅 SSE /api/v1/events，绝不连接 Host 的执行控制 WS。
    assert connections == [
        (
            "http://microbackend:8002/api/v1/events",
            {"Accept": "text/event-stream", "Cache-Control": "no-cache", "Last-Event-ID": "8"},
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
        "input": {},
        "description": "由 unilab workflow CLI 启动",
        "meta_data": {
            "source": "unilab-workflow-client",
            "operation_id": "operation-1",
        },
    }

    client.create_task("workflow-1", input_value={"count": 3}, operation_id="operation-2")
    assert http.requests[-1][2]["json"]["input"] == {"count": 3}


def test_workflow_run_reads_inputs_from_json_text_or_file(monkeypatch, tmp_path, capsys) -> None:
    calls: list[Any] = []

    class _Client:
        def create_task(self, identity: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((identity, kwargs["input_value"]))
            return {"uuid": "task-created", "status": "pending"}

        def close(self) -> None:
            return None

    monkeypatch.setattr(workflow_cli, "_create_workflow_client", lambda _args, _session: _Client())
    parser = build_parser()
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"sample": "S-1", "volume": 2.5}, ensure_ascii=False), encoding="utf-8")

    for argv in (
        ["workflow", "run", "workflow-1", "--inputs", '{"count": 3}'],
        ["workflow", "run", "workflow-1", "--inputs", f"@{inputs}"],
    ):
        assert run_client_command(parser.parse_args(argv), parser, session_manager=object()) is True
    assert calls == [("workflow-1", {"count": 3}), ("workflow-1", {"sample": "S-1", "volume": 2.5})]

    for bad in ("[1, 2]", "{not json", f"@{tmp_path / 'missing.json'}"):
        try:
            run_client_command(
                parser.parse_args(["workflow", "run", "workflow-1", "--inputs", bad]), parser, session_manager=object(),
            )
        except SystemExit as exited:
            assert exited.code == 1
        else:
            raise AssertionError(f"--inputs {bad!r} 应当被拒绝")
    assert len(calls) == 2, "非法 --inputs 不得提交任务"
    assert "--inputs" in capsys.readouterr().err


def test_workflow_command_submits_task_control_commands(monkeypatch, capsys) -> None:
    calls: list[Any] = []

    class _Client:
        def command_task(self, task_uuid: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((task_uuid, kwargs))
            if kwargs["command_type"] == "cancel":
                return {"type": "cancel", "status": "rejected", "result": {"reason": "task_is_terminal"}}
            return {"type": kwargs["command_type"], "status": "succeeded", "result": {}}

        def close(self) -> None:
            return None

    monkeypatch.setattr(workflow_cli, "_create_workflow_client", lambda _args, _session: _Client())
    parser = build_parser()

    for argv in (
        ["workflow", "command", "task-1", "pause"],
        ["workflow", "command", "task-1", "continue", "--idempotency-key", "resume-once"],
        ["workflow", "command", "task-1", "step", "--target-node", "node-1"],
    ):
        assert run_client_command(parser.parse_args(argv), parser, session_manager=object()) is True
    try:
        run_client_command(parser.parse_args(["workflow", "command", "task-1", "cancel"]), parser,
                           session_manager=object())
    except SystemExit as exited:
        assert exited.code == 1
    else:
        raise AssertionError("被拒绝的命令应以非零退出码结束")

    assert [(task, kwargs["command_type"]) for task, kwargs in calls] == [
        ("task-1", "pause"), ("task-1", "resume"), ("task-1", "step"), ("task-1", "cancel"),
    ]
    assert calls[0][1]["idempotency_key"].startswith("cli-pause-")
    assert calls[0][1]["target_node_uuid"] is None
    assert calls[1][1]["idempotency_key"] == "resume-once"
    assert calls[2][1]["target_node_uuid"] == "node-1"
    assert "task_is_terminal" in capsys.readouterr().err


def test_workflow_cli_runs_with_inputs_and_controls_the_task_through_the_api(monkeypatch, capsys) -> None:
    """``--inputs`` 与 ``workflow command`` 走真实 Workflow Authority API。"""

    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.protocol.runtime.workflow import WorkflowNodeWrite
    from unilabos.server.api.runtime.workflow import install_workflow_api
    from unilabos.client.utils import output
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    # --json 会替换全局输出格式；测试结束时恢复，避免影响其它用例的文本输出
    monkeypatch.setattr(output, "_formatter", output._formatter)
    service = WorkflowService(":memory:")
    workflow = service.create_workflow(name="带入口参数", tags=[], description=None, meta_data={})
    saved = service.save_graph(
        workflow["uuid"], revision=workflow["revision"],
        nodes=[WorkflowNodeWrite(uuid=str(uuid4()), name="人工确认", type="manual_confirm")], edges=[],
    )
    service.declare_workflow_io(
        workflow["uuid"], revision=saved["workflow"]["revision"],
        input_contract={"version": 1, "parameters": [
            {"name": "count", "schema": {"type": "integer"}, "required": True}]},
        output_contract=None, output_bindings={}, node_input_bindings={},
    )
    app = FastAPI()
    install_workflow_api(app, service)
    api = TestClient(app)

    class _AuthorityHTTP:
        @staticmethod
        def _data(response: Any) -> Any:
            assert response.status_code < 400, response.text
            envelope = response.json()
            if envelope["code"] != 0:
                from unilabos.client import EnvelopeError

                raise EnvelopeError(envelope["code"], str(envelope.get("error")))
            return envelope["data"]

        def get(self, path: str, **kwargs: Any) -> Any:
            return self._data(api.get(f"/api/v1{path}", params=kwargs.get("params")))

        def post(self, path: str, **kwargs: Any) -> Any:
            return self._data(api.post(f"/api/v1{path}", json=kwargs.get("json")))

    monkeypatch.setattr(workflow_cli, "_create_workflow_client", lambda _args, _session: HTTPWorkflowClient(
        "http://microbackend:8002", http_client=_AuthorityHTTP(),
        notice_connector=lambda *_args: _FakeNoticeContext([]),
    ))
    parser = build_parser()

    def run(*argv: str) -> int:
        try:
            run_client_command(parser.parse_args(["workflow", *argv, "--json"]), parser, session_manager=object())
        except SystemExit as exited:
            return int(exited.code or 0)
        return 0

    # 必填入口参数缺失时建任务被拒；--inputs 给出后任务带着入口参数创建
    assert run("run", workflow["uuid"]) == 1
    assert service.list_workflow_tasks(page=1, page_size=10, workflow_uuid=workflow["uuid"])["items"] == []
    capsys.readouterr()
    assert run("run", workflow["uuid"], "--inputs", '{"count": 3}') == 0
    task = json.loads(capsys.readouterr().out)["data"]
    assert service.get_workflow_task(task["uuid"])["input"] == {"count": 3}

    assert run("command", task["uuid"], "pause") == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "succeeded"
    assert service.get_workflow_task(task["uuid"])["dispatch_gate"] == "paused"
    assert run("command", task["uuid"], "resume") == 0
    assert service.get_workflow_task(task["uuid"])["dispatch_gate"] == "active"
    capsys.readouterr()
    # 未暂停时再恢复是业务拒绝：命令投影照常输出，退出码非零
    assert run("command", task["uuid"], "resume") == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["data"]["result"]["reason"] == "task_is_not_paused"
    assert "task_is_not_paused" in captured.err
    assert run("command", task["uuid"], "cancel") == 0
    assert service.get_workflow_task(task["uuid"])["status"] in {"canceling", "canceled"}


def test_workflow_client_roundtrips_through_the_microbackend_api() -> None:
    """薄客户端必须与实际 Workflow Authority API 使用同一份协议。"""

    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.server.api.runtime.workflow import install_workflow_api
    from unilabos.protocol.runtime.workflow import WorkflowNodeWrite
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    service = WorkflowService(":memory:")
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


def test_workflow_from_template_endpoint_instantiates_registry_template(tmp_path, monkeypatch) -> None:
    """脚本 / e2e 的"运行模板"入口：注册表里的 @workflow 模板经
    ``POST /workflows/from-template`` 按角色绑定实例化成工作流，幂等复用同一 uuid。"""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.registry.workflows import class_role_id
    from unilabos.server.api.runtime.workflow import install_workflow_api
    from unilabos.server.services.runtime.registry import (
        RegistryService,
        set_registry_service,
    )
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    template = {
        "id": "demo.workflows:tour",
        "registry_type": "workflow",
        "uuid": "6c4a2f9e-3b1d-5f0a-9c2e-7d8b1a4e5f60",
        "display_name": "位点操作演示",
        "description": "d",
        "tags": ["demo"],
        "roles": [
            {"role": class_role_id("rack_demo"), "label": "rack_demo", "kind": "class",
             "device_class": "rack_demo", "matches": ["rack_demo"]},
            {"role": "bench", "label": "bench", "kind": "device", "device_id": "bench", "matches": ["bench"]},
        ],
        "nodes": [
            {"key": "step-0", "kind": "action", "role": class_role_id("rack_demo"),
             "action_name": "load", "name": "装载", "param": {"site": "A1"}},
            {"key": "step-1", "kind": "action", "role": "bench", "action_name": "report", "name": "报告", "param": {}},
        ],
        "edges": [{"source": "step-0", "target": "step-1"}],
    }
    registry = RegistryService(tmp_path / "runtime.db")
    registry.report([template], edge_uuid="host")
    set_registry_service(registry)
    service = WorkflowService(":memory:")
    app = FastAPI()
    install_workflow_api(app, service)
    api = TestClient(app)

    class _Material:
        def __init__(self, resource_id: str, klass: str, material_uuid: str) -> None:
            self.resource_id = resource_id
            self.template_name = klass
            self.resource_type = "device"
            self.material_uuid = material_uuid

    rack_uuid = "9d2c7e40-5b1a-4f76-a3c8-000000002001"

    class _Materials:
        def list_materials(self, *, roots_only: bool = False):
            return [
                _Material("rack01", "rack_demo", rack_uuid),
                _Material("bench", "bench_demo", "9d2c7e40-5b1a-4f76-a3c8-000000002002"),
            ]

    monkeypatch.setattr(
        "unilabos.server.execution_composition.get_materials_service", lambda: _Materials()
    )
    try:
        # 缺模板：3002
        missing = api.post("/api/v1/workflows/from-template", json={"template_uuid": str(uuid.uuid4())}).json()
        assert missing["code"] == 3002, missing

        # 此用例只提供角色目录，不提供动作 schema；保存待校验草稿。
        created = api.post("/api/v1/workflows/from-template", json={"template_uuid": template["uuid"], "site_binding_mode": "preserve"}).json()
        assert created["code"] == 0, created
        data = created["data"]
        # 类角色单实例自动解析；设备角色即其设备 id
        assert data["bindings"] == {class_role_id("rack_demo"): "rack01", "bench": "bench"}
        workflow_uuid = data["workflow"]["uuid"]
        graph = service.get_graph(workflow_uuid)
        assert [node["meta_data"]["target_device_id"] for node in graph["nodes"]] == ["rack01", "bench"]
        assert graph["nodes"][0]["material_uuid"] == rack_uuid

        # 同模板同绑定：幂等复用同一工作流
        again = api.post("/api/v1/workflows/from-template", json={"template_uuid": template["uuid"], "site_binding_mode": "preserve"}).json()
        assert again["data"]["workflow"]["uuid"] == workflow_uuid
        assert service.list_workflows(page=1, page_size=10, name="位点操作演示")["total"] == 1

        # 绑到模板没有的角色：1000，且带具体原因
        bad = api.post(
            "/api/v1/workflows/from-template",
            json={"template_uuid": template["uuid"], "bindings": {"ghost": "x"}},
        ).json()
        assert bad["code"] == 1000 and "ghost" in bad["error"]["msg"], bad
    finally:
        set_registry_service(None)
        registry.close()
        service.close()
