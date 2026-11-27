"""The public `unilab workspace` command bypasses product runtime composition."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from unilabos.app.main import main


def test_workspace_status_returns_stable_offline_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "workspace",
            "status",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == "unilab-workspace-host/v1"
    assert payload["workspacePath"] == str(workspace)
    assert payload["host"]["phase"] == "offline"
    assert set(payload["components"]) == {"backend", "edge", "plc", "renderer"}


def test_workspace_restart_uses_the_shared_client_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, object]] = []

    class Client:
        def execute(self, command: str, **options: object) -> dict[str, object]:
            calls.append((command, options))
            return {"operationId": "cli-restart", "phase": "succeeded"}

    monkeypatch.setattr(
        "unilabos.workspace_host.cli.ensure_workspace_host",
        lambda _workspace: Client(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "workspace",
            "restart",
            "--workspace",
            str(workspace),
            "--component",
            "os",
            "--runtime-mode",
            "normal",
            "--operation-id",
            "cli-restart",
            "--wait",
            "9",
            "--json",
        ],
    )

    main()

    assert json.loads(capsys.readouterr().out)["phase"] == "succeeded"
    assert calls == [
        (
            "os.restart",
            {
                "parameters": {"runtimeMode": "normal", "databaseType": "sqlite", "databaseUrl": ""},
                "operation_id": "cli-restart",
                "timeout": 9.0,
            },
        )
    ]


def test_workspace_start_passes_runtime_mode_to_scheduler_and_edge_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """一条 start 命令把运行配置交给 Scheduler 与 Edge 两个进程。

    参数：``tmp_path`` 隔离工作区，``monkeypatch`` 替换外部 Host 客户端，
    ``capsys`` 捕获 CLI 输出。返回：无。异常：默认命令或参数发生回归时失败。
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, object]] = []

    class Client:
        def execute(self, command: str, **options: object) -> dict[str, object]:
            """记录 CLI 通过公开客户端提交的 Host 操作。"""

            calls.append((command, options))
            return {"operationId": "stack-start", "phase": "succeeded"}

    monkeypatch.setattr(
        "unilabos.workspace_host.cli.ensure_workspace_host",
        lambda _workspace: Client(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "workspace",
            "start",
            "--workspace",
            str(workspace),
            "--runtime-mode",
            "normal",
            "--operation-id",
            "stack-start",
            "--wait",
            "30",
            "--json",
        ],
    )

    main()

    assert json.loads(capsys.readouterr().out)["phase"] == "succeeded"
    assert calls == [
        (
            "workspace.start",
            {
                "parameters": {
                    "runtimeMode": "normal",
                    "databaseType": "sqlite",
                    "databaseUrl": "",
                },
                "operation_id": "stack-start",
                "timeout": 30.0,
            },
        )
    ]


def test_workspace_reset_local_requires_confirmation_and_uses_shared_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, object]] = []

    class Client:
        def execute(self, command: str, **options: object) -> dict[str, object]:
            calls.append((command, options))
            return {"operationId": "reset-local", "phase": "succeeded"}

    monkeypatch.setattr(
        "unilabos.workspace_host.cli.ensure_workspace_host",
        lambda _workspace: Client(),
    )
    base_arguments = [
        "unilab",
        "workspace",
        "reset-local",
        "--workspace",
        str(workspace),
        "--json",
    ]
    monkeypatch.setattr(sys, "argv", base_arguments)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == (
        "confirmation_required"
    )
    assert calls == []

    monkeypatch.setattr(
        sys,
        "argv",
        [
            *base_arguments,
            "--yes",
            "--graph",
            "deployment/graphs/lab.json",
            "--runtime-mode",
            "normal",
            "--operation-id",
            "reset-local",
            "--wait",
            "15",
        ],
    )
    main()

    assert json.loads(capsys.readouterr().out)["phase"] == "succeeded"
    assert calls == [
        (
            "local.reset-state",
            {
                "parameters": {
                    "graphPath": "deployment/graphs/lab.json",
                    "runtimeMode": "normal",
                },
                "operation_id": "reset-local",
                "timeout": 15.0,
            },
        )
    ]


@pytest.mark.parametrize("action", ["start", "restart"])
@pytest.mark.parametrize("flag", ["--database-type", "--database_type"])
def test_workspace_database_cli_overrides_config_and_environment(
    tmp_path, monkeypatch, capsys, action, flag,
):
    """CLI 覆盖文件和环境，参数通过 API 传递给已经存在的 Host。"""
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "local_config.py").write_text('class BasicConfig: database_type = "sqlite"')
    monkeypatch.setenv("UNILABOS_BASICCONFIG_DATABASE_TYPE", "sqlite")
    monkeypatch.setenv("UNILABOS_BASICCONFIG_DATABASE_URL", "postgresql://env@localhost/env")
    calls = []

    class Client:
        def execute(self, command, **options):
            calls.append((command, options["parameters"]))
            return {"phase": "succeeded"}

    monkeypatch.setattr("unilabos.workspace_host.cli.ensure_workspace_host", lambda _: Client())
    monkeypatch.setattr(sys, "argv", [
        "unilab", "workspace", action, "--workspace", str(tmp_path),
        flag, "postgresql", "--database-url", "postgresql://cli@localhost/cli", "--json",
    ])
    main()
    capsys.readouterr()
    assert calls == [("workspace." + action, {
        "databaseType": "postgresql", "databaseUrl": "postgresql://cli@localhost/cli",
    })]


def test_workspace_cli_uses_current_environment_even_for_existing_host(tmp_path, monkeypatch):
    from unilabos.persistence.config import DatabaseSettings
    from unilabos.persistence.workspace import save_workspace_database
    from unilabos.workspace_host.cli import _database_parameters

    save_workspace_database(tmp_path, DatabaseSettings())
    monkeypatch.setenv("UNILABOS_BASICCONFIG_DATABASE_TYPE", "postgresql")
    monkeypatch.setenv("UNILABOS_BASICCONFIG_DATABASE_URL", "postgresql://env@localhost/env")
    assert _database_parameters({}, str(tmp_path), "start") == {
        "databaseType": "postgresql", "databaseUrl": "postgresql://env@localhost/env",
    }


def test_workspace_restart_reuses_selection_but_new_start_resolves_config(tmp_path, monkeypatch):
    from unilabos.persistence.config import DatabaseSettings
    from unilabos.persistence.workspace import save_workspace_database
    from unilabos.workspace_host.cli import _database_parameters

    monkeypatch.delenv("UNILABOS_BASICCONFIG_DATABASE_TYPE", raising=False)
    monkeypatch.delenv("UNILABOS_BASICCONFIG_DATABASE_URL", raising=False)
    save_workspace_database(tmp_path, DatabaseSettings("postgresql", "postgresql://local/db"))
    assert _database_parameters({}, str(tmp_path), "restart")["databaseType"] == "postgresql"
    assert _database_parameters({}, str(tmp_path), "start")["databaseType"] == "sqlite"


def test_workspace_cli_invalid_pg_does_not_contact_host(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UNILABOS_BASICCONFIG_DATABASE_URL", raising=False)
    monkeypatch.setattr("unilabos.workspace_host.cli.ensure_workspace_host", lambda _: pytest.fail("不应启动 Host"))
    monkeypatch.setattr(sys, "argv", [
        "unilab", "workspace", "start", "--workspace", str(tmp_path),
        "--database-type", "postgresql", "--json",
    ])
    with pytest.raises(SystemExit):
        main()
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "database_configuration_invalid"
