"""Workspace 的命令行选择、进程恢复和只读检查共用同一个数据库。"""
import json
import time

import pytest

from unilabos.persistence.config import DatabaseSettings
from unilabos.persistence.workspace import database_selection_path, workspace_settings
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.launch import resolve_backend_launch, resolve_edge_launch
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths


@pytest.fixture
def host(tmp_path, monkeypatch):
    (tmp_path / "deployment" / "graphs").mkdir(parents=True)
    (tmp_path / "deployment" / "graphs" / "graph.json").write_text("{}")
    (tmp_path / "deployment" / "local_config.py").write_text("# 未配置数据库")
    monkeypatch.delenv("UNILABOS_BASICCONFIG_DATABASE_TYPE", raising=False)
    monkeypatch.delenv("UNILABOS_BASICCONFIG_DATABASE_URL", raising=False)
    monkeypatch.setattr("unilabos.persistence.config.verify_database_connection", lambda _: None)
    paths = WorkspacePaths.resolve(tmp_path)
    paths.prepare()
    paths.token.write_text("token")
    instance = WorkspaceHost(paths, "token")
    yield instance
    instance.close()


PG = {"databaseType": "postgresql", "databaseUrl": "postgresql://user:secret@localhost/lab"}
SQLITE = {"databaseType": "sqlite", "databaseUrl": ""}


def test_both_launch_plans_and_host_recovery_use_cli_database(host):
    host._select_database("workspace.start", PG)
    backend = resolve_backend_launch(host.paths, graph_path="deployment/graphs/graph.json")
    edge = resolve_edge_launch(host.paths, {"address": backend.address, "metadata": backend.metadata})
    for plan in (backend, edge):
        assert plan.environment["UNILABOS_BASICCONFIG_DATABASE_TYPE"] == "postgresql"
        assert plan.environment["UNILABOS_BASICCONFIG_DATABASE_URL"] == PG["databaseUrl"]
        assert plan.metadata["databaseType"] == "postgresql"
        assert "secret" not in json.dumps(plan.metadata)
        assert "secret" not in str(plan.command)
    assert database_selection_path(host.paths.workspace).stat().st_mode & 0o777 == 0o600
    replacement = WorkspaceHost(host.paths, "token")
    try:
        assert workspace_settings(replacement.paths.workspace) == DatabaseSettings("postgresql", PG["databaseUrl"])
    finally:
        replacement.close()


@pytest.mark.parametrize("command", ["workspace.start", "backend.start", "os.start", "backend.restart", "os.restart"])
def test_cannot_change_only_one_running_component_database(host, command):
    host._select_database("workspace.start", SQLITE)
    host._components["backend"]["phase"] = "ready"
    with pytest.raises(WorkspaceHostError, match="workspace restart"):
        host._dispatch(command, PG)
    assert workspace_settings(host.paths.workspace).database_type == "sqlite"


def test_workspace_restart_switches_only_after_old_components_stop(host, monkeypatch):
    host._select_database("workspace.start", SQLITE)
    host._components["backend"]["phase"] = "ready"
    order = []

    def stop():
        assert workspace_settings(host.paths.workspace).database_type == "sqlite"
        order.append("stop")
        host._components["backend"]["phase"] = "idle"

    def start(parameters):
        assert workspace_settings(host.paths.workspace).database_type == "postgresql"
        order.append("start")

    monkeypatch.setattr(host, "_stop_workspace", stop)
    monkeypatch.setattr(host, "_start_workspace", start)
    host._dispatch("workspace.restart", PG)
    assert order == ["stop", "start"]


def test_unreachable_pg_does_not_stop_or_change_running_database(host, monkeypatch):
    host._select_database("workspace.start", SQLITE)
    host._components["backend"]["phase"] = "ready"

    def unavailable(_):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("unilabos.persistence.config.verify_database_connection", unavailable)
    monkeypatch.setattr(host, "_stop_workspace", lambda: pytest.fail("不能停止旧服务"))
    with pytest.raises(WorkspaceHostError, match="未回退到 SQLite"):
        host._dispatch("workspace.restart", PG)
    assert workspace_settings(host.paths.workspace).database_type == "sqlite"


def test_operation_and_audit_do_not_echo_database_password(host, monkeypatch):
    monkeypatch.setattr(host, "_start_workspace", lambda _: {})
    payload = {"operationId": "pg-start", "command": "workspace.start", "parameters": PG}
    submitted = host.submit(payload)
    assert submitted["parameters"]["databaseUrl"] == "***"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        operation = host.operation("pg-start")
        if operation["phase"] in {"failed", "succeeded"}:
            break
        time.sleep(.01)
    assert operation["phase"] == "succeeded"
    assert "secret" not in json.dumps(operation)
    assert "secret" not in json.dumps(host.submit(payload))
    assert "secret" not in host.paths.audit.read_text()
    assert "secret" not in json.dumps(host.snapshot())
    assert (host.paths.operations / "pg-start.json").stat().st_mode & 0o777 == 0o600


def test_workspace_host_module_can_start_as_separate_process(tmp_path):
    """实际执行模块入口，防止导入测试漏掉 main 之前的符号定义问题。"""
    import os
    import signal

    from unilabos.workspace_host.client import ensure_workspace_host

    client = ensure_workspace_host(tmp_path)
    pid = client.snapshot()["host"]["pid"]
    try:
        assert client.snapshot()["host"]["phase"] == "ready"
    finally:
        os.kill(pid, signal.SIGTERM)


def test_cli_can_override_invalid_file_before_first_start(host):
    (host.paths.workspace / "deployment/local_config.py").write_text(
        'class BasicConfig: database_type = "postgresql"; database_url = "invalid"'
    )
    host._select_database("workspace.start", PG)
    assert workspace_settings(host.paths.workspace).database_url == PG["databaseUrl"]


def test_adopted_legacy_process_without_selection_cannot_switch_one_component(host):
    host._components["backend"]["phase"] = "ready"
    with pytest.raises(WorkspaceHostError, match="workspace restart"):
        host._dispatch("os.start", PG)
    assert not database_selection_path(host.paths.workspace).exists()


@pytest.mark.parametrize("command", ["workspace.start", "workspace.restart", "backend.start", "os.restart"])
def test_old_host_cannot_silently_ignore_database_parameters(tmp_path, monkeypatch, command):
    from unilabos.workspace_host.client import WorkspaceHostClient

    client = WorkspaceHostClient(WorkspacePaths.resolve(tmp_path), "http://127.0.0.1:1", "token")
    posted = []

    def request(method, path, *args, **kwargs):
        if method == "GET":
            return {"host": {"phase": "ready"}, "components": {}}
        posted.append(path)
        return {"operationId": "ignored-by-old-host", "phase": "succeeded"}

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(WorkspaceHostError, match="Host.*升级"):
        client.submit(command, parameters=PG)
    assert posted == []


def test_new_host_advertises_database_parameter_support(host):
    assert "database-selection-v1" in host.snapshot()["host"]["capabilities"]


def test_new_host_accepts_database_parameters_at_client_boundary(tmp_path, monkeypatch):
    from unilabos.workspace_host.client import WorkspaceHostClient

    client = WorkspaceHostClient(WorkspacePaths.resolve(tmp_path), "http://127.0.0.1:1", "token")
    posted = []

    def request(method, path, *args, **kwargs):
        if method == "GET":
            return {"host": {"capabilities": ["database-selection-v1"]}}
        posted.append(args[0]["parameters"])
        return {"operationId": "accepted", "phase": "pending"}

    monkeypatch.setattr(client, "_request", request)
    assert client.submit("workspace.start", parameters=PG)["phase"] == "pending"
    assert posted == [PG]


def test_adopted_old_process_cannot_claim_new_config_was_applied(host):
    (host.paths.workspace / "deployment/local_config.py").write_text(
        'class BasicConfig: database_type = "postgresql"; database_url = ' + repr(PG["databaseUrl"])
    )
    host._components["backend"]["phase"] = "ready"
    with pytest.raises(WorkspaceHostError, match="旧组件未记录数据库选择"):
        host._dispatch("workspace.start", PG)
