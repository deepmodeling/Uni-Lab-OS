from __future__ import annotations

import sys
from types import ModuleType

import pytest

from unilabos.agent_tools.workflow import WorkflowAgentTools, build_mcp_server
from unilabos.workspace_host.model import WorkspaceHostError


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def create_task(self, workflow_uuid, **options):
        self.calls.append(("run", (workflow_uuid, options)))
        return {"taskUuid": "task-1", "sourceIdentity": {"authority": "backend"}}

    def wait_authoring(self, workflow_uuid, **options):
        self.calls.append(("authoring", (workflow_uuid, options)))
        return {"revision": 8, "frontendRefresh": "authoring-sse"}


def test_agent_tools_delegate_to_shared_domain_client(monkeypatch, tmp_path) -> None:
    client = RecordingClient()
    tools = WorkflowAgentTools(tmp_path)
    monkeypatch.setattr(tools, "_client", lambda: client)

    run = tools.run_workflow(
        "workflow-1",
        run_mode="single_node",
        target_node_uuid="node-1",
        operation_id="operation-1",
    )
    authoring = tools.wait_authoring(
        "workflow-1", after_revision=7, timeout=2
    )

    assert run["sourceIdentity"]["authority"] == "backend"
    assert authoring["revision"] == 8
    assert client.calls == [
        (
            "run",
            (
                "workflow-1",
                {
                    "run_mode": "single_node",
                    "target_node_uuid": "node-1",
                    "input_value": None,
                    "operation_id": "operation-1",
                },
            ),
        ),
        (
            "authoring",
            ("workflow-1", {"after_revision": 7, "timeout": 2}),
        ),
    ]


def test_agent_material_tools_delegate_to_attached_renderer(monkeypatch, tmp_path) -> None:
    class Renderer:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def inspect_scene(self, **options):
            return {"kind": "inspect", "options": options}

        def capture_scene(self, output, **options):
            return {"kind": "capture", "output": output, "options": options}

    monkeypatch.setattr(
        "unilabos.agent_tools.workflow.MaterialRendererClient.discover",
        lambda *_args, **_kwargs: Renderer(),
    )
    tools = WorkflowAgentTools(tmp_path)

    inspected = tools.inspect_material_scene(view="3d", hidden_material_ids=["m-2"])
    captured = tools.capture_material_scene(
        str(tmp_path / "scene.png"), view="2.5d", viewport_width=800, viewport_height=600
    )

    assert inspected["options"]["view"] == "3d"
    assert inspected["options"]["hidden_material_ids"] == ["m-2"]
    assert captured["options"]["viewport"] == (800, 600)


def test_agent_workspace_tools_delegate_to_the_shared_host_client(
    monkeypatch, tmp_path
) -> None:
    class HostClient:
        def __init__(self) -> None:
            self.calls = []

        def snapshot(self):
            self.calls.append(("snapshot", None))
            return {"revision": 12, "components": {"edge": {"phase": "ready"}}}

        def execute(self, command, **options):
            self.calls.append(("execute", (command, options)))
            return {"operationId": options["operation_id"], "phase": "succeeded"}

        def logs(self, component, **options):
            self.calls.append(("logs", (component, options)))
            return {"component": component, "content": "ready\n"}

    host = HostClient()
    tools = WorkflowAgentTools(tmp_path)
    monkeypatch.setattr(tools, "_workspace_client", lambda **_kwargs: host)
    monkeypatch.setattr(
        "unilabos.agent_tools.workflow.WorkspaceHostClient.status",
        lambda workspace: {
            "revision": 12,
            "workspacePath": workspace,
            "components": {"edge": {"phase": "ready"}},
        },
    )

    status = tools.workspace_status()
    restarted = tools.restart_workspace_component(
        "os",
        runtime_mode="normal",
        operation_id="mcp-restart",
        timeout=8,
    )
    switched = tools.switch_workspace_authority(
        "backend",
        backend_url="http://127.0.0.1:18080",
        operation_id="mcp-authority",
        timeout=6,
    )
    reset = tools.reset_local_workspace_state(
        confirm=True,
        graph_path="deployment/graphs/lab.json",
        runtime_mode="normal",
        operation_id="mcp-reset-local",
        timeout=11,
    )
    logs = tools.read_workspace_logs("edge", max_bytes=2048)

    assert status["revision"] == 12
    assert restarted["operationId"] == "mcp-restart"
    assert switched["operationId"] == "mcp-authority"
    assert reset["operationId"] == "mcp-reset-local"
    assert logs["content"] == "ready\n"
    assert host.calls == [
        (
            "execute",
            (
                "os.restart",
                {
                    "parameters": {"runtimeMode": "normal"},
                    "operation_id": "mcp-restart",
                    "timeout": 8,
                },
            ),
        ),
        (
            "execute",
            (
                "authority.switch",
                {
                    "parameters": {
                        "mode": "backend",
                        "backendUrl": "http://127.0.0.1:18080",
                    },
                    "operation_id": "mcp-authority",
                    "timeout": 6,
                },
            ),
        ),
        (
            "execute",
            (
                "local.reset-state",
                {
                    "parameters": {
                        "graphPath": "deployment/graphs/lab.json",
                        "runtimeMode": "normal",
                    },
                    "operation_id": "mcp-reset-local",
                    "timeout": 11,
                },
            ),
        ),
        ("logs", ("edge", {"max_bytes": 2048})),
    ]


def test_agent_local_reset_requires_explicit_confirmation(tmp_path) -> None:
    tools = WorkflowAgentTools(tmp_path)

    with pytest.raises(WorkspaceHostError) as caught:
        tools.reset_local_workspace_state()

    assert caught.value.code == "confirmation_required"


def test_mcp_registers_workspace_lifecycle_tools(monkeypatch, tmp_path) -> None:
    class FastMCP:
        def __init__(self, name):
            self.name = name
            self.tools = []

        def tool(self):
            def register(handler):
                self.tools.append(handler.__name__)
                return handler

            return register

    mcp = ModuleType("mcp")
    server = ModuleType("mcp.server")
    fastmcp = ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = FastMCP
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.server", server)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)

    adapter = build_mcp_server(tmp_path)

    assert adapter.name == "UniLab Workspace"
    assert {
        "workspace_status",
        "start_workspace_component",
        "stop_workspace_component",
        "restart_workspace_component",
        "read_workspace_logs",
        "switch_workspace_authority",
        "reset_local_workspace_state",
        "run_workflow",
        "watch_task",
        "capture_material_scene",
        "apply_material_layout",
    }.issubset(adapter.tools)
