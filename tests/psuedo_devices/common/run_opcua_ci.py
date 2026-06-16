#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST_GLOB = "tests/psuedo_devices/**/ci.json"
SERVER_SCRIPT = REPO_ROOT / "tests" / "psuedo_devices" / "common" / "opcua_csv_server.py"
DAEMON_SCRIPT = REPO_ROOT / "tests" / "psuedo_devices" / "common" / "opcua_flow_daemon.py"


@dataclass(frozen=True)
class OpcUaCiCase:
    name: str
    manifest_path: Path
    port: int
    object_name: str
    csv: str
    daemon_flow: str
    pytest: str
    enabled: bool = True
    path: str = "/"
    namespace_uri: str = "http://unilabos.com/opcua/test/pseudo-device"
    server_name: str = "UniLabOS Test OPC UA Server"
    name_column: str = "变量名"
    data_type_column: str = "数据类型"
    initial_value_column: str = "初始值"
    node_id_column: str = ""
    initial_values: dict[str, Any] = field(default_factory=dict)
    action_flow: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    poll_interval: float = 0.02
    startup_timeout: float = 10.0
    log_level: str = "DEBUG"


def render_value(value: str, *, workspace: str, endpoint: str, port: int) -> str:
    return value.format(workspace=workspace, endpoint=endpoint, port=port)


def discover_manifests(workspace: Path, manifest_glob: str = DEFAULT_MANIFEST_GLOB) -> list[OpcUaCiCase]:
    cases = []
    for manifest_path in sorted(workspace.glob(manifest_glob)):
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if data.get("enabled", True) is False:
            continue
        server = data.get("server") or {}
        cases.append(
            OpcUaCiCase(
                name=data["name"],
                manifest_path=manifest_path,
                port=int(data["port"]),
                object_name=data["object_name"],
                csv=data["csv"],
                daemon_flow=data["daemon_flow"],
                pytest=data["pytest"],
                enabled=bool(data.get("enabled", True)),
                path=data.get("path", "/"),
                namespace_uri=server.get("namespace_uri", "http://unilabos.com/opcua/test/pseudo-device"),
                server_name=server.get("server_name", "UniLabOS Test OPC UA Server"),
                name_column=server.get("name_column", "变量名"),
                data_type_column=server.get("data_type_column", "数据类型"),
                initial_value_column=server.get("initial_value_column", "初始值"),
                node_id_column=server.get("node_id_column", ""),
                initial_values=dict(server.get("initial_values") or {}),
                action_flow=data.get("action_flow"),
                env={str(key): str(value) for key, value in (data.get("env") or {}).items()},
                poll_interval=float(data.get("poll_interval", 0.02)),
                startup_timeout=float(data.get("startup_timeout", 10.0)),
                log_level=data.get("log_level", "DEBUG"),
            )
        )
    return cases


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).resolve()
    cases = discover_manifests(workspace, args.manifest_glob)
    if not cases:
        print(f"No OPC UA CI manifests matched {args.manifest_glob}")
        return 0

    for case in cases:
        run_case(case, workspace, args.log_dir)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repository-managed OPC UA pseudo-device CI manifests.")
    parser.add_argument("--workspace", default=os.environ.get("GITHUB_WORKSPACE", str(REPO_ROOT)))
    parser.add_argument("--manifest-glob", default=DEFAULT_MANIFEST_GLOB)
    parser.add_argument("--log-dir", default=os.environ.get("RUNNER_TEMP", "/tmp"))
    return parser.parse_args()


def run_case(case: OpcUaCiCase, workspace: Path, log_dir: str) -> None:
    endpoint = f"opc.tcp://localhost:{case.port}{case.path}"
    server_log = Path(log_dir) / f"{case.name}_opcua_server.log"
    daemon_log = Path(log_dir) / f"{case.name}_opcua_daemon.log"
    server_log.unlink(missing_ok=True)
    daemon_log.unlink(missing_ok=True)

    server = None
    daemon = None
    try:
        print(f"========== OPC UA CI: {case.name} ==========")
        print(f"Manifest: {case.manifest_path}")
        print(f"Endpoint: {endpoint}")
        print(f"CSV: {workspace / case.csv}")
        describe_action_flow(case, workspace)
        describe_daemon_flow(case, workspace)

        server = start_server(case, workspace, endpoint, server_log)
        wait_for_process("OPC UA server", server, case.startup_timeout)
        daemon = start_daemon(case, workspace, endpoint, daemon_log)
        wait_for_process("OPC UA daemon", daemon, case.startup_timeout)
        run_pytest(case, workspace, endpoint)
    finally:
        stop_process(daemon)
        stop_process(server)
        dump_log("OPC UA server", server_log)
        dump_log("OPC UA daemon", daemon_log)


def describe_action_flow(case: OpcUaCiCase, workspace: Path) -> None:
    if not case.action_flow:
        return
    flow = json.loads((workspace / case.action_flow).read_text(encoding="utf-8"))
    print("---- action flow summary ----")
    print(f"Flow: {flow.get('name')}")
    for rule in flow.get("rules", []):
        for item in rule.get("actions", []):
            action = item.get("action")
            if action:
                print(
                    "Action {index}: {node} | {device_id}.{method}({params})".format(
                        **action,
                    )
                )


def describe_daemon_flow(case: OpcUaCiCase, workspace: Path) -> None:
    print("---- daemon flow summary ----")
    subprocess.run(
        [sys.executable, str(DAEMON_SCRIPT), "--flow", str(workspace / case.daemon_flow), "--describe-only"],
        cwd=workspace,
        check=True,
    )


def start_server(case: OpcUaCiCase, workspace: Path, endpoint: str, log_path: Path) -> subprocess.Popen:
    del endpoint
    command = [
        sys.executable,
        "-u",
        str(SERVER_SCRIPT),
        "--host",
        "0.0.0.0",
        "--port",
        str(case.port),
        "--path",
        case.path,
        "--csv",
        str(workspace / case.csv),
        "--object-name",
        case.object_name,
        "--namespace-uri",
        case.namespace_uri,
        "--server-name",
        case.server_name,
        "--name-column",
        case.name_column,
        "--data-type-column",
        case.data_type_column,
        "--initial-value-column",
        case.initial_value_column,
        "--initial-values-json",
        json.dumps(case.initial_values, ensure_ascii=False),
        "--log-level",
        case.log_level,
    ]
    if case.node_id_column:
        command.extend(["--node-id-column", case.node_id_column])
    print(f"Starting OPC UA server: {' '.join(command)}")
    return start_logged_process(command, workspace, log_path)


def start_daemon(case: OpcUaCiCase, workspace: Path, endpoint: str, log_path: Path) -> subprocess.Popen:
    command = [
        sys.executable,
        "-u",
        str(DAEMON_SCRIPT),
        "--url",
        endpoint,
        "--object-name",
        case.object_name,
        "--flow",
        str(workspace / case.daemon_flow),
        "--poll-interval",
        str(case.poll_interval),
        "--log-level",
        case.log_level,
    ]
    print(f"Starting OPC UA daemon: {' '.join(command)}")
    return start_logged_process(command, workspace, log_path)


def start_logged_process(command: list[str], workspace: Path, log_path: Path) -> subprocess.Popen:
    log_handle = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(command, cwd=workspace, stdout=log_handle, stderr=subprocess.STDOUT)
    finally:
        log_handle.close()


def wait_for_process(name: str, process: subprocess.Popen, timeout: float) -> None:
    time.sleep(min(timeout, 1.0))
    if process.poll() is not None:
        raise RuntimeError(f"{name} exited before tests: exit_code={process.returncode}")


def run_pytest(case: OpcUaCiCase, workspace: Path, endpoint: str) -> None:
    env = os.environ.copy()
    for key, value in case.env.items():
        env[key] = render_value(value, workspace=str(workspace), endpoint=endpoint, port=case.port)
    command = [
        sys.executable,
        "-m",
        "pytest",
        case.pytest,
        "-q",
        "-s",
        "-o",
        "log_cli=true",
        "--log-cli-level=INFO",
    ]
    print(f"Running pytest: {' '.join(command)}")
    subprocess.run(command, cwd=workspace, env=env, check=True)


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def dump_log(title: str, log_path: Path) -> None:
    print(f"---- {title} log: {log_path} ----")
    if log_path.exists():
        print(log_path.read_text(encoding="utf-8", errors="replace"))
    else:
        print("Log file not found")


if __name__ == "__main__":
    raise SystemExit(main())
