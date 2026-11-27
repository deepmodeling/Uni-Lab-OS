"""AIW-02 Workspace Host lifecycle, idempotency, and discovery contracts."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from unilabos.app.edge_control.addressing import derive_scheduler_address
from unilabos.app.edge_control.store import EdgeControlStore
from unilabos.workspace_host.client import WorkspaceHostClient
from unilabos.workspace_host.discovery import WorkspaceHostLock, ensure_local_token
from unilabos.workspace_host.host import (
    WorkspaceHost,
    _handler_type,
    _pid_exists,
    _renderer_process_environment,
)
from unilabos.workspace_host.launch import (
    LaunchPlan,
    _with_conda_ros_environment,
    resolve_backend_launch,
    resolve_edge_launch,
    resolve_plc_launch,
)
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "deployment" / "graphs").mkdir(parents=True)
    (root / "deployment" / "graphs" / "graph.json").write_text("{}\n")
    (root / "deployment" / "local_config.py").write_text("# fixture\n")
    return root


def test_workspace_lock_fails_closed_with_discovery_details(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    paths.session.write_text(json.dumps({"endpoint": "http://127.0.0.1:1"}))
    first = WorkspaceHostLock(paths)
    first.acquire()
    try:
        with pytest.raises(WorkspaceHostError) as caught:
            WorkspaceHostLock(paths).acquire()
        assert caught.value.code == "host_already_running"
        assert caught.value.details == {"endpoint": "http://127.0.0.1:1"}
    finally:
        first.release()


def test_workspace_token_is_private_and_stable(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    first = ensure_local_token(paths)
    second = ensure_local_token(paths)
    assert first == second
    assert len(first) == 64
    if os.name != "nt":
        assert paths.token.stat().st_mode & 0o777 == 0o600


def test_host_drops_legacy_startup_mode_from_environment_configuration(
    workspace: Path,
) -> None:
    """新 Host 不再把旧环境文件中的 startupMode 传播到运行态配置。"""

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    paths.environment.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runtimeMode": "normal",
                "domainMode": "local",
                "startupMode": "develop",
            }
        ),
        encoding="utf-8",
    )
    token = ensure_local_token(paths)
    host = WorkspaceHost(paths, token, readiness_timeout=0.1)
    try:
        assert "startupMode" not in host.snapshot()["configuration"]
        persisted = json.loads(paths.environment.read_text(encoding="utf-8"))
        assert "startupMode" not in persisted
    finally:
        host.close()


def test_detached_windows_runtime_restores_conda_ros_activation(tmp_path: Path) -> None:
    prefix = tmp_path / "szlab-unilab"
    (prefix / "conda-meta").mkdir(parents=True)
    library = prefix / "Library"
    library.mkdir()
    (library / "local_setup.bat").write_text("@rem fixture\n")
    environment = {
        "PATH": r"C:\Windows\System32",
        "AMENT_PREFIX_PATH": r"D:\explicit\ament",
        "PYTHONHOME": r"D:\wrong-python-home",
    }

    result = _with_conda_ros_environment(
        environment,
        platform="win32",
        prefix=prefix,
        executable=str(prefix / "python.exe"),
    )

    assert result["CONDA_PREFIX"] == str(prefix.resolve())
    assert result["CONDA_DEFAULT_ENV"] == "szlab-unilab"
    assert result["AMENT_PREFIX_PATH"] == r"D:\explicit\ament"
    assert result["AMENT_PYTHON_EXECUTABLE"] == str(prefix / "python.exe")
    assert result["ROS_DISTRO"] == "humble"
    assert result["ROS_VERSION"] == "2"
    assert result["CONDA_DLL_SEARCH_MODIFICATION_ENABLE"] == "1"
    assert result["PYTHONHOME"] == ""
    assert result["PATH"].split(os.pathsep)[0] == str(library / "bin")


def test_workspace_client_executes_and_normalizes_one_host_operation(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    client = WorkspaceHostClient(paths, "http://127.0.0.1:48100", "token")
    calls: list[tuple[str, object]] = []

    def submit(command: str, **options: object) -> dict[str, object]:
        calls.append(("submit", (command, options)))
        return {"operationId": "operation-1", "phase": "pending"}

    def wait(operation_id: str, **options: object) -> dict[str, object]:
        calls.append(("wait", (operation_id, options)))
        return {
            "operationId": operation_id,
            "phase": "succeeded",
            "result": {"components": {"edge": {"phase": "ready"}}},
        }

    monkeypatch.setattr(client, "submit", submit)
    monkeypatch.setattr(client, "wait", wait)

    result = client.execute(
        "os.restart",
        parameters={"runtimeMode": "normal"},
        operation_id="operation-1",
        expected_revision=7,
        timeout=4.0,
    )

    assert result["phase"] == "succeeded"
    assert calls == [
        (
            "submit",
            (
                "os.restart",
                {
                    "parameters": {"runtimeMode": "normal"},
                    "operation_id": "operation-1",
                    "expected_revision": 7,
                },
            ),
        ),
        ("wait", ("operation-1", {"timeout": 4.0})),
    ]


def test_workspace_client_raises_the_host_operation_failure(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    client = WorkspaceHostClient(paths, "http://127.0.0.1:48100", "token")
    monkeypatch.setattr(
        client,
        "submit",
        lambda *_args, **_kwargs: {"operationId": "operation-failed"},
    )
    monkeypatch.setattr(
        client,
        "wait",
        lambda *_args, **_kwargs: {
            "operationId": "operation-failed",
            "phase": "failed",
            "error": {
                "code": "os_readiness_failed",
                "message": "OS 未就绪",
                "details": {"logPath": "/tmp/os.log"},
            },
        },
    )

    with pytest.raises(WorkspaceHostError) as caught:
        client.execute("os.restart")

    assert caught.value.code == "os_readiness_failed"
    assert caught.value.details == {"logPath": "/tmp/os.log"}


def test_workspace_client_status_is_stable_while_host_is_offline(
    workspace: Path,
) -> None:
    result = WorkspaceHostClient.status(workspace)

    assert result["schemaVersion"] == "unilab-workspace-host/v1"
    assert result["workspacePath"] == str(workspace)
    assert result["host"] == {"phase": "offline", "pid": None, "endpoint": None}
    assert set(result["components"]) == {"backend", "edge", "plc", "renderer"}
    assert result["diagnostic"]["code"] == "host_not_found"


def test_attached_renderer_records_a_reusable_headless_adapter(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    snapshot = host._dispatch(
        "renderer.attach",
        {
            "pid": os.getpid(),
            "address": "http://127.0.0.1:3100",
            "generation": "renderer-1",
            "workbenchProjectPath": "/opt/unilab/workbench",
            "nodeExecutable": "/opt/unilab/node",
        },
    )

    renderer = snapshot["components"]["renderer"]
    assert "material-scene-reload" in renderer["capabilities"]
    assert renderer["metadata"]["workbenchProjectPath"] == "/opt/unilab/workbench"
    host._material_renderer_usable = lambda _renderer: True  # type: ignore[method-assign]
    ensured = host._dispatch("renderer.headless.ensure", {})
    assert ensured["adapter"] == "attached"
    host.close()


def test_headless_renderer_inherits_selected_backend_authority() -> None:
    environment = _renderer_process_environment(
        {
            "domainMode": "backend",
            "backendUrl": "http://127.0.0.1:18080",
        },
        base_environment={"PATH": "/fixture/bin"},
    )

    assert environment["UNILAB_RENDERER_MANAGED_HEADLESS"] == "1"
    assert environment["UNILAB_BACKEND_PROXY_TARGET"] == "http://127.0.0.1:18080"
    assert environment["PATH"] == "/fixture/bin"


@pytest.mark.skipif(os.name != "nt", reason="Windows PID probing contract")
def test_pid_probe_never_uses_os_kill_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def destructive_probe(_pid: int, _signal: int) -> None:
        raise AssertionError("Windows PID probing must not call os.kill")

    monkeypatch.setattr(os, "kill", destructive_probe)

    assert _pid_exists(os.getpid()) is True
    assert _pid_exists(987654) is False


def test_host_restart_adopts_a_live_workbench_renderer(workspace: Path) -> None:
    renderer_server = ThreadingHTTPServer(("127.0.0.1", 0), _RendererHandler)
    threading.Thread(target=renderer_server.serve_forever, daemon=True).start()
    renderer_port = int(renderer_server.server_address[1])
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    token = ensure_local_token(paths)
    first = WorkspaceHost(paths, token, readiness_timeout=0.1)
    first._dispatch(
        "renderer.attach",
        {
            "pid": os.getpid(),
            "address": f"http://127.0.0.1:{renderer_port}",
            "generation": "renderer-before-host-restart",
        },
    )
    first.close()

    restarted = WorkspaceHost(paths, token, readiness_timeout=0.1)
    try:
        assert restarted.snapshot()["components"]["renderer"]["phase"] == "ready"
    finally:
        restarted.close()
        renderer_server.shutdown()
        renderer_server.server_close()


def test_split_runtime_launches_share_local_edge_protocol_and_stable_state(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)
    backend = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_101,
    )
    backend_component = {
        "address": backend.address,
        "metadata": backend.metadata,
    }
    first_edge = resolve_edge_launch(paths, backend_component)
    second_edge = resolve_edge_launch(paths, backend_component)

    assert backend.environment["UNILABOS_EDGECONTROLCONFIG_API_KEY"] == token
    assert backend.environment["UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR"] == (
        "http://127.0.0.1:48101"
    )
    assert "--process_role" in backend.command
    assert "workspace_backend" in backend.command
    assert "--run_mode" not in backend.command
    assert "startupMode" not in backend.metadata
    assert "edge_control" in first_edge.command
    assert "--run_mode" not in first_edge.command
    assert "startupMode" not in first_edge.metadata
    assert "fastapi" not in first_edge.command
    assert "--is_slave" not in first_edge.command
    assert "--hostlink_addr" not in first_edge.command
    assert first_edge.environment["UNILABOS_EDGECONTROLCONFIG_API_KEY"] == token
    assert first_edge.environment["UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR"] == (
        backend.address
    )
    assert first_edge.environment["UNILABOS_EDGECONTROLCONFIG_SCHEDULER_ADDR"] == (
        backend.address
    )
    stable_state = str(paths.runtime / "edge" / "edge_control.db")
    assert first_edge.environment["UNILABOS_EDGECONTROLCONFIG_STATE_DB"] == stable_state
    assert second_edge.environment["UNILABOS_EDGECONTROLCONFIG_STATE_DB"] == stable_state
    assert first_edge.metadata["runtimeDirectory"] != second_edge.metadata[
        "runtimeDirectory"
    ]


def test_backend_launch_keeps_local_domain_store_across_process_generations(
    workspace: Path,
) -> None:
    """Backend crash recovery must not create a fresh Local Domain database."""

    paths = WorkspacePaths.resolve(workspace)
    ensure_local_token(paths)

    first = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_121,
    )
    second = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_123,
    )

    expected_state = str(paths.runtime / "backend" / "local-domain")
    assert _argument_value(first.command, "--working_dir") == expected_state
    assert _argument_value(second.command, "--working_dir") == expected_state
    assert "--preserve_runtime_databases" in first.command
    assert "--preserve_runtime_databases" in second.command
    assert first.metadata["stateDirectory"] == expected_state
    assert second.metadata["stateDirectory"] == expected_state
    assert first.metadata["runtimeDirectory"] != second.metadata["runtimeDirectory"]


def test_managed_graph_copy_preserves_inventory_source_identity(
    workspace: Path,
) -> None:
    """受管代次副本必须保留图文件名，避免伪造库存来源变更。"""

    paths = WorkspacePaths.resolve(workspace)
    ensure_local_token(paths)

    backend = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        runtime_mode="dry-run",
        backend_port=48_127,
    )
    edge = resolve_edge_launch(
        paths,
        {"address": backend.address, "metadata": backend.metadata},
    )

    backend_graph = Path(_argument_value(backend.command, "--graph"))
    edge_graph = Path(_argument_value(edge.command, "--graph"))
    assert backend_graph.name == "graph.json"
    assert edge_graph.name == "graph.json"
    assert backend_graph.parent != edge_graph.parent


def test_backend_launch_migrates_the_last_generation_into_stable_local_domain(
    workspace: Path,
) -> None:
    """The first split-runtime upgrade preserves the previous Backend facts."""

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    ensure_local_token(paths)
    legacy = paths.runtime / "backend" / "legacy-generation"
    legacy.mkdir(parents=True)
    legacy_database = legacy / "workflow_history.db"
    connection = sqlite3.connect(legacy_database)
    connection.execute("CREATE TABLE acceptance (value TEXT NOT NULL)")
    connection.execute("INSERT INTO acceptance(value) VALUES ('preserved')")
    connection.commit()
    connection.close()
    paths.session.write_text(
        json.dumps(
            {
                "schemaVersion": "unilab-workspace-host/v1",
                "components": {
                    "backend": {
                        "metadata": {"runtimeDirectory": str(legacy)},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    plan = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_125,
    )

    migrated = paths.runtime / "backend" / "local-domain" / "workflow_history.db"
    connection = sqlite3.connect(migrated)
    try:
        assert connection.execute("SELECT value FROM acceptance").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()
    assert plan.metadata["legacyStateMigratedFrom"] == str(legacy)


def test_backend_domain_mode_is_rejected(
    workspace: Path,
) -> None:
    """已删除的 Backend 控制面不会再生成本地启动计划。"""

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    ensure_local_token(paths)
    paths.environment.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "graphPath": "deployment/graphs/graph.json",
                "runtimeMode": "normal",
                "domainMode": "backend",
                "backendUrl": "https://backend.example.test",
            }
        )
    )

    with pytest.raises(WorkspaceHostError, match="不再支持 backend 模式") as raised:
        resolve_backend_launch(paths, backend_port=48_111)
    assert raised.value.code == "backend_mode_removed"


def test_backend_domain_mode_with_explicit_upstream_is_rejected(
    workspace: Path,
) -> None:
    """旧 Backend 地址也不会绕过已删除的控制面。"""

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    ensure_local_token(paths)
    paths.environment.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "graphPath": "deployment/graphs/graph.json",
                "runtimeMode": "normal",
                "domainMode": "backend",
                "backendUrl": "http://127.0.0.1:8080",
            }
        )
    )

    with pytest.raises(WorkspaceHostError, match="不再支持 backend 模式") as raised:
        resolve_backend_launch(paths, backend_port=48_111)
    assert raised.value.code == "backend_mode_removed"


def test_legacy_backend_scheduler_override_is_rejected(
    workspace: Path,
) -> None:
    """包含旧上游 Scheduler 配置的 Backend 环境不会被启动。"""

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    ensure_local_token(paths)
    paths.environment.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "graphPath": "deployment/graphs/graph.json",
                "runtimeMode": "normal",
                "domainMode": "backend",
                "backendUrl": "http://127.0.0.1:8080",
                "schedulerUrl": "http://scheduler.example.test:9081",
            }
        )
    )

    with pytest.raises(WorkspaceHostError, match="不再支持 backend 模式") as raised:
        resolve_backend_launch(paths, backend_port=48_111)
    assert raised.value.code == "backend_mode_removed"


def test_scheduler_configuration_override_can_return_to_automatic(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)

    snapshot = host._dispatch(
        "configuration.update",
        {"schedulerUrl": "http://127.0.0.1:39081/"},
    )

    assert snapshot["configuration"]["schedulerUrl"] == "http://127.0.0.1:39081"
    persisted = json.loads(paths.environment.read_text(encoding="utf-8"))
    assert persisted["schedulerUrl"] == "http://127.0.0.1:39081"

    automatic = host._dispatch(
        "configuration.update",
        {"schedulerUrl": None},
    )

    assert automatic["configuration"]["schedulerUrl"] is None
    persisted = json.loads(paths.environment.read_text(encoding="utf-8"))
    assert persisted["schedulerUrl"] is None
    host.close()


@pytest.mark.parametrize(
    "scheduler_url",
    [
        "scheduler.example.test:8081",
        "ftp://scheduler.example.test:8081",
        "http://user:secret@scheduler.example.test:8081",
        "http://scheduler.example.test:8081/path",
        "http://scheduler.example.test:8081?debug=1",
        "http://scheduler.example.test:8081#debug",
    ],
)
def test_scheduler_configuration_rejects_invalid_origins(
    workspace: Path,
    scheduler_url: str,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)

    with pytest.raises(WorkspaceHostError) as raised:
        host._dispatch("configuration.update", {"schedulerUrl": scheduler_url})

    assert raised.value.code == "scheduler_url_invalid"
    assert host.snapshot()["configuration"]["schedulerUrl"] is None
    host.close()


@pytest.mark.parametrize(
    ("backend_address", "scheduler_address"),
    [
        ("http://127.0.0.1:8080", "http://127.0.0.1:8081"),
        ("http://127.0.0.1:8080/api/v1/", "http://127.0.0.1:8081"),
        ("https://backend.example.test", "https://backend.example.test"),
        ("https://backend.example.test/api/v1", "https://backend.example.test"),
        ("http://[::1]:8080", "http://[::1]:8081"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]"),
    ],
)
def test_scheduler_address_derivation_preserves_ingress_and_ipv6(
    backend_address: str,
    scheduler_address: str,
) -> None:
    assert derive_scheduler_address(backend_address) == scheduler_address


def test_dry_run_edge_disables_device_auto_connect_in_detached_runtime_graph(
    workspace: Path,
) -> None:
    graph_path = workspace / "deployment" / "graphs" / "graph.json"
    graph_path.write_text(json.dumps({
        "nodes": [{
            "id": "plc",
            "config": {
                "url": "opc.tcp://plc-sim:4855/xuse_sim",
                "auto_connect": True,
            },
        }],
    }))
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    ensure_local_token(paths)
    paths.environment.write_text(json.dumps({
        "schemaVersion": 1,
        "graphPath": "deployment/graphs/graph.json",
        "runtimeMode": "dry-run",
        "domainMode": "local",
    }))

    backend = resolve_backend_launch(
        paths, backend_port=48_131
    )
    edge = resolve_edge_launch(
        paths, {"address": backend.address, "metadata": backend.metadata}
    )
    edge_graph = Path(_argument_value(edge.command, "--graph"))

    assert edge_graph != Path(str(backend.metadata["validatedGraphPath"]))
    assert json.loads(edge_graph.read_text())["nodes"][0]["config"] == {
        "url": "opc.tcp://plc-sim:4855/xuse_sim",
        "auto_connect": False,
    }


def test_authority_switch_is_removed(
    workspace: Path,
) -> None:
    """Workspace Host 不再暴露远端 Backend 控制面切换。"""
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    try:
        with pytest.raises(WorkspaceHostError, match="不能切换到 backend") as raised:
            host._dispatch(
                "authority.switch",
                {"mode": "backend", "backendUrl": "http://127.0.0.1:8080/"},
            )
        assert raised.value.code == "backend_mode_removed"
    finally:
        host.close()


def test_authority_preflight_rejects_backend_without_edge_contract(
    workspace: Path,
) -> None:
    backend, scheduler = _adjacent_http_servers(
        _ReadyHandler,
        _BackendWithoutEdgeHandler,
    )
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (backend, scheduler)
    ]
    for thread in threads:
        thread.start()
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)

    try:
        with pytest.raises(WorkspaceHostError) as raised:
            host._preflight_backend_authority(
                f"http://127.0.0.1:{backend.server_port}"
            )
        assert raised.value.code == "backend_authority_incompatible"
        assert "Edge 调度连接" in str(raised.value)
    finally:
        host.close()
        for server in (backend, scheduler):
            server.shutdown()
            server.server_close()


def test_authority_preflight_uses_split_scheduler_port(workspace: Path) -> None:
    backend, scheduler = _adjacent_http_servers(
        _BackendWithoutEdgeHandler,
        _SchedulerWithEdgeHandler,
    )
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (backend, scheduler)
    ]
    for thread in threads:
        thread.start()
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)

    try:
        host._preflight_backend_authority(
            f"http://127.0.0.1:{backend.server_port}"
        )
    finally:
        host.close()
        for server in (backend, scheduler):
            server.shutdown()
            server.server_close()


def test_backend_url_normalization_accepts_api_v1_suffix() -> None:
    assert WorkspaceHost._normalize_backend_url(
        "http://127.0.0.1:8080/api/v1/"
    ) == "http://127.0.0.1:8080"


def test_authority_preflight_explains_unavailable_scheduler(
    workspace: Path,
) -> None:
    backend, scheduler = _adjacent_http_servers(
        _ReadyHandler,
        _BackendWithoutSchedulerHandler,
    )
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (backend, scheduler)
    ]
    for thread in threads:
        thread.start()
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)

    try:
        with pytest.raises(WorkspaceHostError) as raised:
            host._preflight_backend_authority(
                f"http://127.0.0.1:{backend.server_port}"
            )
        assert raised.value.code == "backend_authority_unavailable"
        assert str(raised.value) == (
            "Backend 的 Scheduler 未就绪，Edge 暂时无法连接。"
            "请启动 Scheduler 服务（默认端口 8081）后重试；"
            "当前 OS 尚未切换。"
        )
    finally:
        host.close()
        for server in (backend, scheduler):
            server.shutdown()
            server.server_close()


def test_authority_switch_to_existing_backend_is_removed(
    workspace: Path,
) -> None:
    """即使不要求 bootstrap，也不能重新连接旧 Backend。"""
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    try:
        with pytest.raises(WorkspaceHostError, match="不能切换到 backend") as raised:
            host._dispatch(
                "authority.switch",
                {
                    "mode": "backend",
                    "backendUrl": "http://127.0.0.1:8080/",
                    "bootstrap": False,
                },
            )
        assert raised.value.code == "backend_mode_removed"
    finally:
        host.close()


def test_legacy_backend_configuration_is_rejected(
    workspace: Path,
) -> None:
    """旧环境文件中的 Backend 控制面必须显式迁移。"""
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    paths.environment.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "domainMode": "backend",
                "backendUrl": "http://127.0.0.1:8080",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceHostError, match="请删除旧配置中的 backend 模式") as raised:
        WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    assert raised.value.code == "backend_mode_removed"


@pytest.mark.parametrize("profile", ["szlab", "community_pump_station"])
def test_plc_launch_preserves_explicit_handshake_workflow(workspace: Path, profile: str) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    plc_project = workspace / "plc-sim"
    (plc_project / "OpcUaSim" / "gui").mkdir(parents=True)
    (plc_project / "OpcUaSim" / "gui" / "backend.py").write_text("# fixture\n")
    variable_table = workspace / "plc.csv"
    variable_table.write_text("变量名,数据类型\n")
    paths.environment.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "plcSimulatorProjectPath": str(plc_project),
                "plcVariableTablePath": str(variable_table),
                "plcHandshakeProfile": profile,
                "plcHandshakeWorkflow": "s_z_lab_单样品全流程_物料感知",
            }
        )
    )

    plan = resolve_plc_launch(paths)

    assert plan.metadata["handshakeProfile"] == profile
    assert plan.metadata["handshakeWorkflow"] == "s_z_lab_单样品全流程_物料感知"
    configuration = json.loads(paths.environment.read_text())
    configuration.pop("plcHandshakeProfile")
    paths.environment.write_text(json.dumps(configuration))
    with pytest.raises(WorkspaceHostError, match="握手器名称"):
        resolve_plc_launch(paths)


def test_operation_is_recoverable_idempotent_and_audited(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    threading.Thread(target=ready_server.serve_forever, daemon=True).start()
    ready_port = int(ready_server.server_address[1])
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)

    def fake_backend(*_args: object, **_kwargs: object) -> LaunchPlan:
        generation = "test-backend-generation"
        return LaunchPlan(
            component="backend",
            command=(sys.executable, "-c", "import time; time.sleep(120)"),
            cwd=workspace,
            environment=dict(os.environ),
            generation=generation,
            log_path=paths.logs / "backend.log",
            address=f"http://127.0.0.1:{ready_port}",
            ready_url=f"http://127.0.0.1:{ready_port}/api/v1/readiness",
            metadata={"runtimeMode": "normal"},
        )

    monkeypatch.setattr(
        "unilabos.workspace_host.host.resolve_backend_launch",
        fake_backend,
    )
    host = WorkspaceHost(paths, token, readiness_timeout=2.0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_type(host))
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    host.publish_endpoint(endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = WorkspaceHostClient(paths, endpoint, token)
    try:
        submitted = client.submit("backend.start", operation_id="start-once")
        duplicate = client.submit("backend.start", operation_id="start-once")
        assert duplicate["requestHash"] == submitted["requestHash"]
        completed = client.wait("start-once", timeout=5)
        assert completed["phase"] == "succeeded"
        snapshot = client.snapshot()
        assert snapshot["components"]["backend"]["phase"] == "ready"
        assert snapshot["components"]["backend"]["pid"] > 0

        recovered = client.operation("start-once")
        assert recovered == completed
        with pytest.raises(WorkspaceHostError) as caught:
            client.submit(
                "backend.stop",
                operation_id="start-once",
            )
        assert caught.value.code == "operation_conflict"

        stopped = client.submit("backend.stop", operation_id="stop-once")
        stopped = client.wait(str(stopped["operationId"]), timeout=5)
        assert stopped["phase"] == "succeeded"
        assert client.snapshot()["components"]["backend"]["phase"] == "idle"
        events = paths.audit.read_text(encoding="utf-8")
        assert "operation.submitted" in events
        assert "backend.ready" in events
    finally:
        try:
            operation = client.submit("backend.stop", operation_id="cleanup")
            client.wait(str(operation["operationId"]), timeout=5)
        except WorkspaceHostError:
            pass
        server.shutdown()
        server.server_close()
        host.close()
        ready_server.shutdown()
        ready_server.server_close()


def test_backend_readiness_falls_back_only_when_endpoint_is_absent(
    workspace: Path,
) -> None:
    """旧 Backend 明确没有 readiness 时仍用完整旧探针证明可用。

    参数：``workspace`` 提供隔离 Host 状态。返回：无；断言启动等待和重启接管
    都只在 readiness 返回 404 后回退 health。异常：兼容路径退化时测试失败。
    """

    ready_server = ThreadingHTTPServer(("127.0.0.1", 0), _LegacyReadyHandler)
    threading.Thread(target=ready_server.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{int(ready_server.server_address[1])}"
    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=2.0)
    plan = LaunchPlan(
        component="backend",
        command=(),
        cwd=workspace,
        environment={},
        generation="legacy-backend",
        log_path=paths.logs / "legacy-backend.log",
        address=address,
        ready_url=f"{address}/api/v1/health",
        metadata={"runtimeMode": "normal", "domainMode": "local"},
    )
    host._processes["backend"] = _RunningProcess()
    try:
        package_mounts = host._wait_backend_ready(plan)
        assert package_mounts["schemaVersion"] == "workspace-package-mounts/v1"
        assert WorkspaceHost._component_is_ready(
            "backend",
            {"address": address},
        )
    finally:
        host._processes.pop("backend", None)
        host.close()
        ready_server.shutdown()
        ready_server.server_close()


@pytest.mark.parametrize("foreign_desktop_backend", [False, True])
def test_backend_failed_or_foreign_readiness_stops_without_timeout(
    workspace: Path, foreign_desktop_backend: bool,
) -> None:
    """Backend 明确报告 failed 时 Host 必须立即失败而不是耗尽总超时。

    参数：``workspace`` 提供隔离 Host 状态。返回：无；断言稳定错误和进程所有权
    被撤销。异常：失败状态仍被当作 starting 轮询时测试超时或断言失败。
    """

    handler = _ReadyHandler if foreign_desktop_backend else _FailedReadyHandler
    ready_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=ready_server.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{int(ready_server.server_address[1])}"
    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=10.0)
    plan = LaunchPlan(
        component="backend",
        command=(),
        cwd=workspace,
        environment={"UNILABOS_DESKTOP_WEB_HOST": "127.0.0.1"} if foreign_desktop_backend else {},
        generation="failed-backend",
        log_path=paths.logs / "failed-backend.log",
        address=address,
        ready_url=f"{address}/api/v1/readiness",
        metadata={"runtimeMode": "normal", "domainMode": "local"},
    )
    host._processes["backend"] = _RunningProcess()
    try:
        with pytest.raises(WorkspaceHostError) as caught:
            host._wait_backend_ready(plan)
        assert caught.value.code == ("backend_port_occupied" if foreign_desktop_backend else "backend_readiness_failed")
        assert "backend" not in host._processes
    finally:
        host._processes.pop("backend", None)
        host.close()
        ready_server.shutdown()
        ready_server.server_close()


def test_os_restart_and_local_reset_state_are_distinct_commands(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)
    host = WorkspaceHost(paths, token, readiness_timeout=0.1)
    calls: list[tuple[str, dict[str, object]]] = []

    def dispatch(command: str, parameters: dict[str, object]) -> object:
        calls.append((command, parameters))
        return {"command": command}

    host._dispatch = dispatch  # type: ignore[method-assign]
    first = host.submit(
        {"operationId": "os", "command": "os.restart", "parameters": {}}
    )
    second = host.submit(
        {
            "operationId": "reset",
            "command": "local.reset-state",
            "parameters": {},
        }
    )
    _wait_operation(host, str(first["operationId"]))
    _wait_operation(host, str(second["operationId"]))
    assert calls == [("os.restart", {}), ("local.reset-state", {})]
    audit = paths.audit.read_text(encoding="utf-8")
    assert '"command":"os.restart"' in audit
    assert '"command":"local.reset-state"' in audit
    host.close()


def test_backend_restart_reconnects_an_existing_edge_to_the_new_dynamic_port(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    calls: list[str] = []
    host._components["backend"]["phase"] = "ready"
    host._components["edge"]["phase"] = "ready"
    host._stop_component = lambda name: calls.append(f"stop:{name}") or {}  # type: ignore[method-assign]
    host._start_backend = lambda parameters: calls.append("start:backend") or {}  # type: ignore[method-assign]
    host._start_edge = lambda: calls.append("start:edge") or {"ready": True}  # type: ignore[method-assign]

    result = host._dispatch("backend.restart", {})

    assert result == {"ready": True}
    assert calls == ["stop:edge", "stop:backend", "start:backend", "start:edge"]
    host.close()


def test_local_reset_state_clears_edge_work_but_preserves_identity(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证本地重置清除动作账本但保留稳定 Edge 实例身份。

    参数：``workspace`` 是隔离工作区，``monkeypatch`` 模拟文件操作边界。返回无；
    身份被删除或旧作业残留时断言失败。
    """

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    state_path = paths.runtime / "edge" / "edge_control.db"
    backend_state_path = (
        paths.runtime / "edge" / "edge_control-backend-test-authority.db"
    )
    store = EdgeControlStore(str(state_path))
    instance_uuid = store.get_or_create_instance_uuid()
    command_uuid = "50000000-0000-4000-8000-000000000201"
    job_uuid = "50000000-0000-4000-8000-000000000202"
    store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 1,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": "50000000-0000-4000-8000-000000000203",
            "node_uuid": "50000000-0000-4000-8000-000000000204",
            "claim_uuid": "50000000-0000-4000-8000-000000000205",
            "attempt": 1,
            "fences": [
                {
                    "lock_key": "/devices/robot-01",
                    "fencing_token": 1,
                }
            ],
            "job_access_token": "workspace-reset-token",
        },
        command_uuid,
    )
    store.close()
    backend_store = EdgeControlStore(str(backend_state_path))
    backend_instance_uuid = backend_store.get_or_create_instance_uuid()
    backend_store.record_command(
        {
            "message_uuid": "50000000-0000-4000-8000-000000000211",
            "sequence": 9,
            "type": "job.start",
            "payload": {"job_uuid": "50000000-0000-4000-8000-000000000212"},
        }
    )
    backend_store.close()
    local_domain = paths.runtime / "backend" / "local-domain"
    local_domain.mkdir(parents=True)
    local_domain_databases = [
        local_domain / "inventory.db",
        local_domain / "device_state.db",
        local_domain / "workflow_history.db",
        local_domain / "edge_authority.db",
    ]
    for database in local_domain_databases:
        database.write_bytes(b"stale")
        database.with_name(f"{database.name}-wal").write_bytes(b"stale-wal")

    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    monkeypatch.setattr(
        "unilabos.workspace_host.host.inspect_local_reset_blockers",
        lambda _paths: [],
    )
    stopped: list[str] = []
    host._stop_component = lambda name: stopped.append(name) or {}  # type: ignore[method-assign]
    host._start_backend = lambda parameters: {"parameters": parameters}  # type: ignore[method-assign]

    result = host._dispatch("local.reset-state", {"runtimeMode": "normal"})

    assert result == {"parameters": {"runtimeMode": "normal"}}
    assert stopped == ["edge", "backend"]
    reopened = EdgeControlStore(str(state_path))
    assert reopened.get_or_create_instance_uuid() == instance_uuid
    assert reopened.get_job(job_uuid) is None
    reopened.close()
    reopened_backend = EdgeControlStore(str(backend_state_path))
    assert reopened_backend.get_or_create_instance_uuid() == backend_instance_uuid
    assert reopened_backend.last_ack_command_sequence() == 0
    reopened_backend.close()
    assert all(not database.exists() for database in local_domain_databases)
    assert all(
        not database.with_name(f"{database.name}-wal").exists()
        for database in local_domain_databases
    )
    host.close()


def test_host_restart_adopts_a_ready_backend_without_stopping_it(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    threading.Thread(target=ready_server.serve_forever, daemon=True).start()
    ready_port = int(ready_server.server_address[1])
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)

    def fake_backend(*_args: object, **_kwargs: object) -> LaunchPlan:
        return LaunchPlan(
            component="backend",
            command=(sys.executable, "-c", "import time; time.sleep(120)"),
            cwd=workspace,
            environment=dict(os.environ),
            generation="adopted-generation",
            log_path=paths.logs / "adopted.log",
            address=f"http://127.0.0.1:{ready_port}",
            ready_url=f"http://127.0.0.1:{ready_port}/api/v1/readiness",
            metadata={"runtimeMode": "normal"},
        )

    monkeypatch.setattr(
        "unilabos.workspace_host.host.resolve_backend_launch",
        fake_backend,
    )
    original = WorkspaceHost(paths, token, readiness_timeout=2.0)
    original.publish_endpoint("http://127.0.0.1:1")
    original._start_backend({})
    pid = int(original.snapshot()["components"]["backend"]["pid"])
    original.close()
    try:
        assert _pid_running(pid)
        recovered = WorkspaceHost(paths, token, readiness_timeout=2.0)
        assert recovered.snapshot()["components"]["backend"]["phase"] == "ready"
        assert recovered.snapshot()["components"]["backend"]["pid"] == pid
        recovered.close()
        original._stop_component("backend")
        assert not _pid_running(pid)
    finally:
        if _pid_running(pid):
            os.kill(pid, 9)
        ready_server.shutdown()
        ready_server.server_close()


def test_backend_crash_is_supervised_into_a_new_process_generation(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready Local Backend is a supervised service, not a UI child process."""

    ready_server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    threading.Thread(target=ready_server.serve_forever, daemon=True).start()
    ready_port = int(ready_server.server_address[1])
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)
    launches = 0

    def fake_backend(*_args: object, **_kwargs: object) -> LaunchPlan:
        nonlocal launches
        launches += 1
        return LaunchPlan(
            component="backend",
            command=(sys.executable, "-c", "import time; time.sleep(120)"),
            cwd=workspace,
            environment=dict(os.environ),
            generation=f"backend-generation-{launches}",
            log_path=paths.logs / f"backend-{launches}.log",
            address=f"http://127.0.0.1:{ready_port}",
            ready_url=f"http://127.0.0.1:{ready_port}/api/v1/readiness",
            metadata={
                "runtimeMode": "normal",
                "domainMode": "local",
                "stateDirectory": str(paths.runtime / "backend" / "local-domain"),
            },
        )

    monkeypatch.setattr(
        "unilabos.workspace_host.host.resolve_backend_launch",
        fake_backend,
    )
    host = WorkspaceHost(paths, token, readiness_timeout=2.0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_type(host))
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    host.publish_endpoint(endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = WorkspaceHostClient(paths, endpoint, token)
    recovered_pid: int | None = None
    try:
        started = client.submit("backend.start", operation_id="crash-start")
        client.wait(str(started["operationId"]), timeout=5)
        before = client.snapshot()["components"]["backend"]
        first_pid = int(before["pid"])
        os.kill(first_pid, signal.SIGKILL)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            backend = client.snapshot()["components"]["backend"]
            if backend["phase"] == "ready" and backend["pid"] != first_pid:
                recovered_pid = int(backend["pid"])
                assert backend["generation"] == "backend-generation-2"
                assert backend["metadata"]["stateDirectory"] == str(
                    paths.runtime / "backend" / "local-domain"
                )
                if "backend.recovery.succeeded" in paths.audit.read_text(
                    encoding="utf-8"
                ):
                    break
            time.sleep(0.05)
        assert recovered_pid is not None
        assert "backend.recovery.succeeded" in paths.audit.read_text(encoding="utf-8")
    finally:
        try:
            stopped = client.submit("backend.stop", operation_id="crash-cleanup")
            client.wait(str(stopped["operationId"]), timeout=5)
        except WorkspaceHostError:
            if recovered_pid and _pid_running(recovered_pid):
                os.kill(recovered_pid, signal.SIGKILL)
        server.shutdown()
        server.server_close()
        host.close()
        ready_server.shutdown()
        ready_server.server_close()


def test_renderer_registration_is_host_owned_and_detachable(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths))
    attached = host._dispatch(
        "renderer.attach",
        {"pid": os.getpid(), "address": "http://127.0.0.1:3100"},
    )
    assert attached["components"]["renderer"] == {
        "name": "renderer",
        "phase": "ready",
        "pid": os.getpid(),
        "address": "http://127.0.0.1:3100",
        "generation": str(os.getpid()),
        "logPath": None,
        "diagnostic": None,
        "capabilities": [
            "workbench-ui",
            "theia-rpc",
            "material-scene-inspect",
            "material-scene-capture",
            "material-scene-reload",
        ],
        "metadata": {
            "automationBaseUrl": "http://127.0.0.1:3100/__unilab_renderer/v1",
            "automationContract": "unilab-material-renderer/v1",
        },
    }
    detached = host._dispatch("renderer.detach", {"pid": os.getpid()})
    assert detached["components"]["renderer"]["phase"] == "idle"
    host.close()


def _wait_operation(host: WorkspaceHost, operation_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        operation = host.operation(operation_id)
        if operation["phase"] in {"succeeded", "failed"}:
            return operation
        time.sleep(0.01)
    raise AssertionError(f"operation did not finish: {operation_id}")


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _argument_value(command: tuple[str, ...], name: str) -> str:
    return command[command.index(name) + 1]


class _ReadyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/api/v1/readiness":
            payload: object = {
                "status": "ready",
                "phase": "ready",
                "workflowProgress": {"loaded": 1, "total": 1},
            }
        elif self.path == "/api/v1/health":
            payload: object = {"status": "ok"}
        elif self.path.startswith("/api/v1/workflow-node-templates?"):
            payload = {
                "code": 0,
                "data": {
                    "items": [
                        {"uuid": "material-source", "node_type": "material_source"}
                    ]
                },
            }
        elif self.path == "/api/v1/resource-templates?page=1&page_size=1":
            body = json.dumps({"detail": "Not Found"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path == "/api/v1/workspace/package-mounts":
            payload = {
                "code": 0,
                "data": {
                    "schemaVersion": "workspace-package-mounts/v1",
                    "items": [{"packageId": "fixture"}],
                },
            }
        else:
            payload = {"code": 0, "data": {"items": []}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _LegacyReadyHandler(_ReadyHandler):
    def do_GET(self) -> None:
        if self.path == "/api/v1/readiness":
            self.send_response(404)
            self.end_headers()
            return
        super().do_GET()


class _FailedReadyHandler(_ReadyHandler):
    def do_GET(self) -> None:
        if self.path == "/api/v1/readiness":
            body = json.dumps(
                {
                    "status": "failed",
                    "phase": "failed",
                    "workflowProgress": {"loaded": 1, "total": 2},
                    "error": {"code": "workflow_runtime_start_failed"},
                }
            ).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class _RunningProcess:
    def poll(self) -> None:
        return None


class _BackendWithoutEdgeHandler(_ReadyHandler):
    def do_GET(self) -> None:
        if self.path == "/api/v1/edge/sessions":
            body = json.dumps({"error": {"code": "not_found"}}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class _BackendWithoutSchedulerHandler(_ReadyHandler):
    def do_GET(self) -> None:
        if self.path == "/api/v1/edge/sessions":
            self.send_response(502)
            self.end_headers()
            return
        super().do_GET()


class _SchedulerWithEdgeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/api/v1/edge/sessions":
            self.send_response(405)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _adjacent_http_servers(
    backend_handler: type[BaseHTTPRequestHandler],
    scheduler_handler: type[BaseHTTPRequestHandler],
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    for _ in range(100):
        backend = ThreadingHTTPServer(("127.0.0.1", 0), backend_handler)
        try:
            scheduler = ThreadingHTTPServer(
                ("127.0.0.1", backend.server_port + 1),
                scheduler_handler,
            )
        except OSError:
            backend.server_close()
            continue
        return backend, scheduler
    raise AssertionError("unable to reserve adjacent Backend and Scheduler ports")


class _RendererHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            body = b"workbench"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return
