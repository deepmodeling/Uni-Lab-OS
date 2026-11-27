"""进程存活探测不能为稳态检查不断启动子进程，也不能漏掉僵尸进程。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from unilabos.workspace_host import host as module
from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.launch import LaunchPlan
from unilabos.workspace_host.model import WorkspacePaths


@pytest.mark.parametrize("pid", [None, False, True, "1", 0, -1])
def test_invalid_pid_is_not_alive(pid: object) -> None:
    assert module._pid_exists(pid) is False


@pytest.mark.skipif(os.name == "nt", reason="仅模拟 POSIX 的 Linux procfs 探测路径")
@pytest.mark.parametrize("state,alive", [("R", True), ("S", True), ("T", True), ("Z", False)])
def test_linux_probe_reads_state_without_starting_a_process(
    monkeypatch: pytest.MonkeyPatch, state: str, alive: bool,
) -> None:
    pid = os.getpid()
    original_read = Path.read_text

    def read_stat(path: Path, *args: object, **kwargs: object) -> str:
        if path == Path(f"/proc/{pid}/stat"):
            # comm 可以含空格、换行和右括号，状态字段在最后一个右括号之后。
            return f"{pid} (worker ) with\nspaces) {state} 1 2 3\n"
        return original_read(path, *args, **kwargs)

    def forbidden_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("Linux 稳态存活检查不应启动 ps 子进程")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", read_stat)
    monkeypatch.setattr(subprocess, "run", forbidden_spawn)

    assert module._pid_exists(pid) is alive


@pytest.mark.skipif(os.name == "nt", reason="仅模拟 POSIX 的 procfs 不可用场景")
@pytest.mark.parametrize("problem", [FileNotFoundError(), PermissionError(), ValueError()])
def test_unavailable_procfs_keeps_legacy_zombie_check(
    monkeypatch: pytest.MonkeyPatch, problem: Exception,
) -> None:
    pid = os.getpid()
    original_read = Path.read_text

    def unavailable(path: Path, *args: object, **kwargs: object) -> str:
        if path == Path(f"/proc/{pid}/stat"):
            if isinstance(problem, ValueError):
                return "无效的 stat 内容"
            raise problem
        return original_read(path, *args, **kwargs)

    def zombie_status(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if command != ["ps", "-o", "stat=", "-p", str(pid)]:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"Z\n")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", unavailable)
    monkeypatch.setattr(subprocess, "run", zombie_status)

    assert module._pid_exists(pid) is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="需要真实 Linux procfs")
def test_real_unreaped_child_is_not_alive() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        os.kill(child.pid, 0)  # 僵尸仍有 PID，单用 kill(0) 会误判。
        assert module._pid_exists(child.pid) is False
    finally:
        child.wait(timeout=5)
    assert module._pid_exists(child.pid) is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="需要真实 Linux procfs")
def test_adopted_backend_zombie_is_automatically_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api/v1/readiness":
                data = {"status": "ready"}
            else:
                data = {
                    "code": 0,
                    "data": {
                        "schemaVersion": "workspace-package-mounts/v1",
                        "items": [{"uuid": "fixture", "node_type": "material_source"}],
                    },
                }
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ReadyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)
    launches = 0

    def launch(*args: object, **kwargs: object) -> LaunchPlan:
        nonlocal launches
        launches += 1
        address = f"http://127.0.0.1:{server.server_port}"
        return LaunchPlan(
            component="backend",
            command=(sys.executable, "-c", "import time; time.sleep(120)"),
            cwd=workspace,
            environment=dict(os.environ),
            generation=f"generation-{launches}",
            log_path=paths.logs / f"backend-{launches}.log",
            address=address,
            ready_url=f"{address}/api/v1/readiness",
            metadata={"runtimeMode": "normal"},
        )

    monkeypatch.setattr(module, "resolve_backend_launch", launch)
    original = module.WorkspaceHost(paths, token, readiness_timeout=2)
    recovered = None
    child = None
    try:
        original._start_backend({})
        child = original._processes["backend"]
        original.close()
        recovered = module.WorkspaceHost(paths, token, readiness_timeout=2)
        assert recovered.snapshot()["components"]["backend"]["pid"] == child.pid
        assert "backend" not in recovered._processes
        recovered.publish_endpoint("http://127.0.0.1:1")
        os.kill(child.pid, signal.SIGKILL)
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            backend = recovered.snapshot()["components"]["backend"]
            audit = paths.audit.read_text()
            if backend["phase"] == "ready" and backend["pid"] != child.pid:
                if "backend.recovery.succeeded" in audit:
                    break
            time.sleep(0.05)
        assert backend["phase"] == "ready"
        assert backend["generation"] == "generation-2"
        assert backend["pid"] != child.pid
        assert '"adopted":true' in audit
        assert "backend.recovery.succeeded" in audit
    finally:
        with ExitStack() as cleanup:
            cleanup.callback(server.server_close)
            cleanup.callback(server.shutdown)
            cleanup.callback(original.close)
            processes = set(original._processes.values())
            if child is not None:
                processes.add(child)
            if recovered is not None:
                processes.update(recovered._processes.values())
            for process in processes:
                cleanup.callback(process.wait, timeout=5)
                cleanup.callback(process.kill)
            if recovered is not None:
                cleanup.callback(recovered._stop_component, "backend")
                cleanup.callback(recovered.close)
