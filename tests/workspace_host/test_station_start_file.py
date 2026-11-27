"""工站 YAML 与两种真实启动入口的回归。"""

import json
import os
import sys
from pathlib import Path

import pytest

from unilabos.app.main import main
from unilabos.workspace_host.client import WorkspaceHostClient
from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths
from unilabos.workspace_host.station_config import StationFile


@pytest.fixture
def setup_station(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "deployment").mkdir(parents=True)
    (workspace / "deployment/local_config.py").write_text("")
    for name in ("selected", "override"):
        (workspace / f"deployment/{name}.json").write_text('{"nodes": []}')
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("CENTER_") or key == "ROS_DOMAIN_ID":
            monkeypatch.delenv(key)
    config = tmp_path / "station.yaml"
    config.write_text("""
schemaVersion: 1
workspace: workspace
graphPath: workspace/deployment/selected.json
runtimeMode: dry-run
backendPort: 18410
rosDomainId: 21
center:
  enabled: true
  inventoryEnabled: false
  endpoint: https://center.example.com
  instanceUuid: 123e4567-e89b-12d3-a456-426614174000
  stationUuid: 123e4567-e89b-12d3-a456-426614174001
  labUuid: 123e4567-e89b-12d3-a456-426614174002
  clientCert: station.crt
  clientKey: station.key
  serverCa: ca.crt
  machineSecretFile: machine-secret
  handoffPolicyFile: handoff.json
  transportRouteFile: routes.json
""")
    for name in ("station.crt", "station.key", "ca.crt", "handoff.json", "routes.json"):
        (tmp_path / name).write_text("fixture")
    (tmp_path / "machine-secret").write_text("a" * 64 + "\n")
    return config, WorkspacePaths.resolve(workspace)


def test_cli_launch_and_reload_in_existing_host(setup_station, monkeypatch):
    config, paths = setup_station
    host = WorkspaceHost(paths, ensure_local_token(paths))
    launched = []
    calls = []

    class LocalClient:
        def execute(self, command, **options):
            calls.append(options["parameters"])
            return host._dispatch(command, options["parameters"])

    def discover(workspace):
        assert Path(workspace) == paths.workspace
        return LocalClient()

    def spawn(plan):
        launched.append(plan)
        host._components["backend"].update(metadata=plan.metadata, address=plan.address)

    monkeypatch.setattr("unilabos.workspace_host.cli.ensure_workspace_host", discover)
    monkeypatch.setattr(host, "_spawn", spawn)
    monkeypatch.setattr(host, "_wait_backend_ready", lambda _: [])
    monkeypatch.setattr(host, "_start_edge", lambda: None)
    monkeypatch.setattr(host, "_resume_local_scheduler", lambda: None)
    args = [
        "unilab",
        "workspace",
        "start",
        "-f",
        str(config),
        "--runtime-mode",
        "normal",
        "--graph",
        "deployment/override.json",
    ]
    monkeypatch.setattr(sys, "argv", args)
    try:
        main()
        plan = launched[0]
        assert plan.address == "http://127.0.0.1:18410"
        assert "hostLinkPort" not in plan.metadata
        assert plan.metadata["runtimeMode"] == "normal"
        assert plan.metadata["graphPath"] == str(paths.workspace / "deployment/override.json")
        assert plan.environment["CENTER_HANDOFF_POLICY_FILE"] == str(config.parent / "handoff.json")
        assert plan.environment["CENTER_INVENTORY_ENABLED"] == "0"
        assert plan.environment["CENTER_MACHINE_SECRET"] == "a" * 64
        assert plan.environment["ROS_DOMAIN_ID"] == "21"
        assert "a" * 64 not in json.dumps([calls, plan.metadata])
        main()
        assert len(launched) == 1
        # 变更文件后重复 start 不得误报新配置已生效。
        config.write_text(config.read_text().replace("rosDomainId: 21", "rosDomainId: 22"))
        with pytest.raises(SystemExit):
            main()
        assert len(launched) == 1
        # 已提交的 restart 遇到文件变更必须在停止旧进程前失败。
        with pytest.raises(WorkspaceHostError, match="启动文件已修改"):
            host._dispatch("workspace.restart", calls[0])
        assert host._components["backend"]["phase"] == "ready"
    finally:
        host.close()


@pytest.mark.parametrize(
    "scheduler_env,runtime_file,runtime_env,expected_scheduler,expected_runtime",
    [
        ("182", 185, "183", "182", "183"),  # 两个容器各自的环境优先。
        (None, 183, None, "21", "183"),  # 同一份 YAML 可为两组件指定不同的值。
        ("182", None, None, "182", "182"),  # 原配置可继续使用调度侧选定的共用域。
        (None, None, None, "21", "21"),
    ],
)
def test_split_role_handoff_pins_graph_and_domain_without_mounting_runtime_credentials(
    setup_station,
    monkeypatch,
    scheduler_env,
    runtime_file,
    runtime_env,
    expected_scheduler,
    expected_runtime,
):
    config, paths = setup_station
    if runtime_file is not None:
        config.write_text(config.read_text() + f"runtimeRosDomainId: {runtime_file}\n")
    if scheduler_env is not None:
        monkeypatch.setenv("ROS_DOMAIN_ID", scheduler_env)
    monkeypatch.setenv("CENTER_ENDPOINT", "https://override.example.com")
    observed = []

    class ExecReplacedProcess(Exception):
        pass

    def fake_exec(program, argv, env):
        observed.append((argv, env))
        raise ExecReplacedProcess

    monkeypatch.setattr(os, "execvpe", fake_exec)
    shared = config.parent / "shared.json"

    def run(role):
        monkeypatch.setattr(
            sys,
            "argv",
            ["unilab", "workspace", "start", "-f", str(config), "--foreground-role", role, "--plan-file", str(shared)],
        )
        with pytest.raises(ExecReplacedProcess):
            main()

    run("scheduler")
    assert observed[0][1]["ROS_DOMAIN_ID"] == expected_scheduler
    assert observed[0][1]["CENTER_ENDPOINT"] == "https://override.example.com"
    assert observed[0][1]["CENTER_TRANSPORT_ROUTE_FILE"] == str(config.parent / "routes.json")
    assert "a" * 64 not in shared.read_text()
    (config.parent / "machine-secret").unlink()
    monkeypatch.delenv("ROS_DOMAIN_ID", raising=False)
    if runtime_env is not None:
        monkeypatch.setenv("ROS_DOMAIN_ID", runtime_env)
    run("runtime")
    command, env = observed[1]
    assert command[command.index("--process_role") + 1] == "edge_runtime"
    assert json.loads(shared.read_text())["metadata"]["runtimeMode"] == "dry-run"
    assert env["ROS_DOMAIN_ID"] == expected_runtime and "CENTER_MACHINE_SECRET" not in env
    assert env["UNILABOS_EDGECONTROLCONFIG_SCHEDULER_ADDR"] == "http://127.0.0.1:18410"
    config.write_text(config.read_text().replace("rosDomainId: 21", "rosDomainId: 22"))
    with pytest.raises(SystemExit):
        run("runtime")


@pytest.mark.parametrize(
    "text",
    [
        "schemaVersion: 2",
        "schemaVersion: true",
        "schemaVersion: 1\nextra: true",
        "schemaVersion: 1\nbackendPort: 0",
        "schemaVersion: 1\nrosDomainId: 233",
        "schemaVersion: 1\nruntimeRosDomainId: -3",
        "schemaVersion: 1\nruntimeMode: real",
        "schemaVersion: 1\nruntimeMode: normal\nruntimeMode: dry-run",
        "schemaVersion: 1\ncenter:\n  machineSecret: HIDDEN_SECRET_VALUE",
        "schemaVersion: 1\ncenter: !custom HIDDEN_SECRET_VALUE",
    ],
)
def test_reject_invalid_file_without_echoing_values(tmp_path, text):
    config = tmp_path / "invalid.yaml"
    config.write_text(text)
    with pytest.raises(WorkspaceHostError) as error:
        StationFile.load(str(config))
    assert error.value.code == "station_config_invalid"
    assert "HIDDEN_SECRET_VALUE" not in str(error.value)


def test_old_host_refuses_new_file_before_submission(setup_station, monkeypatch):
    _, paths = setup_station
    client = WorkspaceHostClient(paths, "http://127.0.0.1", "token")
    monkeypatch.setattr(client, "snapshot", lambda **_: {"host": {"capabilities": ["database-selection-v1"]}})
    with pytest.raises(WorkspaceHostError, match="升级"):
        client.submit("workspace.start", parameters={"stationFile": "station.yaml"})


def test_workspace_host_keeps_legacy_domain_and_applies_profile_runtime_override(setup_station, monkeypatch):
    from unilabos.workspace_host.launch import resolve_backend_launch, resolve_edge_launch
    from unilabos.workspace_host.station_config import StationLaunch

    config, paths = setup_station
    graph = str(paths.workspace / "deployment/selected.json")
    ensure_local_token(paths)
    old = resolve_backend_launch(paths, graph_path=graph)
    backend = {"metadata": old.metadata, "address": old.address}
    standard = resolve_edge_launch(paths, backend).environment["ROS_DOMAIN_ID"]
    monkeypatch.setenv("ROS_DOMAIN_ID", "183")
    assert resolve_edge_launch(paths, backend).environment["ROS_DOMAIN_ID"] == standard
    monkeypatch.delenv("ROS_DOMAIN_ID")
    config.write_text(config.read_text() + "runtimeRosDomainId: 183\n")
    station_plan = StationLaunch.prepare(StationFile.load(str(config)), paths, {}).backend_plan(paths)
    assert station_plan.environment["ROS_DOMAIN_ID"] == "21"
    edge = resolve_edge_launch(paths, {"metadata": station_plan.metadata, "address": station_plan.address})
    assert edge.environment["ROS_DOMAIN_ID"] == "183"
    monkeypatch.setenv("ROS_DOMAIN_ID", "233")
    with pytest.raises(WorkspaceHostError, match="ROS_DOMAIN_ID"):
        resolve_edge_launch(paths, {"metadata": station_plan.metadata, "address": station_plan.address})
