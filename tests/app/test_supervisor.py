"""安全重启监督进程：约定退出码再拉起，--no-safe-restart / slave / check_mode 不套监督。"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
# 模块级导入：本文件 from __future__ import annotations 下路由签名里的 Request 是字符串注解，
# FastAPI 要在模块 globals 里找到它
from fastapi import FastAPI, Request  # noqa: F401

from unilabos.app import supervisor
from unilabos.app.cli.parser import build_parser


class _FakeProcess:
    """按脚本依次给出 wait() 结果；条目为异常类则抛出（模拟 Ctrl+C / 超时）。"""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.terminated = False
        self.killed = False
        self._code: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        if self._code is not None:
            return self._code
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        self._code = int(item)
        return self._code

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self._code = 143

    def kill(self) -> None:
        self.killed = True
        self._code = 137


@pytest.fixture()
def no_signal_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(supervisor, "_POLL_S", 0.001)


def test_parser_accepts_no_safe_restart_aliases() -> None:
    parser = build_parser()
    assert parser.parse_args(["--no-safe-restart"]).no_safe_restart is True
    assert parser.parse_args(["--no_safe_restart"]).no_safe_restart is True
    assert parser.parse_args([]).no_safe_restart is False


def test_should_supervise_only_top_level_host_with_remote_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(supervisor.SUPERVISOR_INNER_ENV, raising=False)
    remote = {"no_safe_restart": False, "check_mode": False, "is_slave": False, "address": "http://cloud:8081"}
    assert supervisor.should_supervise(SimpleNamespace(**remote)) is True
    # 默认拓扑：unilab 进程自己是调度权威、看护 Host 子进程，不再套薄监督进程
    assert supervisor.should_supervise(SimpleNamespace(**{**remote, "address": None})) is False
    assert supervisor.should_supervise(SimpleNamespace(**{**remote, "no_safe_restart": True})) is False
    assert supervisor.should_supervise(SimpleNamespace(**{**remote, "check_mode": True})) is False
    # 受管设备进程按 pid 看护 slave，不能多一层监督
    assert supervisor.should_supervise(SimpleNamespace(**{**remote, "is_slave": True})) is False
    monkeypatch.setenv(supervisor.SUPERVISOR_INNER_ENV, "1")
    assert supervisor.should_supervise(SimpleNamespace(**remote)) is False


def test_maybe_supervise_takes_over_remote_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(supervisor.SUPERVISOR_INNER_ENV, raising=False)
    monkeypatch.setattr(supervisor, "run_supervisor", lambda: 7)
    remote = SimpleNamespace(no_safe_restart=False, check_mode=False, is_slave=False, address="http://cloud")
    with pytest.raises(SystemExit) as exc:
        supervisor.maybe_supervise(remote)
    assert exc.value.code == 7
    # 不该接管时静默返回
    supervisor.maybe_supervise(SimpleNamespace(no_safe_restart=False, check_mode=False, is_slave=False, address=None))


def test_supervisor_relaunches_on_restart_exit_code(
    monkeypatch: pytest.MonkeyPatch, no_signal_setup: None
) -> None:
    scripts = [[supervisor.RESTART_EXIT_CODE], [supervisor.RESTART_EXIT_CODE], [0]]
    launched: list[list[str]] = []

    def fake_popen(command, env=None, **_kwargs):
        launched.append(list(command))
        assert env[supervisor.SUPERVISOR_INNER_ENV] == "1"
        return _FakeProcess(scripts.pop(0))

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    assert supervisor.run_supervisor(["--port", "8002"]) == 0
    assert len(launched) == 3
    assert launched[0][-2:] == ["--port", "8002"]


def test_supervisor_returns_child_code_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch, no_signal_setup: None
) -> None:
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_a, **_k: _FakeProcess([1]))
    assert supervisor.run_supervisor([]) == 1


def test_supervisor_waits_for_child_after_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, no_signal_setup: None
) -> None:
    # Ctrl+C 同时送达子进程：监督进程不该立刻 terminate，而是等它自己退出
    process = _FakeProcess([subprocess.TimeoutExpired("x", 1), KeyboardInterrupt, 0])
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_a, **_k: process)
    assert supervisor.run_supervisor([]) == 0
    assert process.terminated is False


def test_supervisor_terminates_child_that_ignores_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, no_signal_setup: None
) -> None:
    process = _FakeProcess([KeyboardInterrupt, subprocess.TimeoutExpired("x", 1)])
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(supervisor, "CHILD_EXIT_GRACE_S", 0.001)
    assert supervisor.run_supervisor([]) == 143
    assert process.terminated is True


def test_inner_command_uses_unilabos_main_module() -> None:
    command = supervisor.inner_command(["-g", "lab.json"])
    assert command[1:3] == ["-m", "unilabos.app.main"]
    assert command[-2:] == ["-g", "lab.json"]


# ── 调度权威看护 Host 子进程 ─────────────────────────────────────────


def test_host_child_command_points_back_to_authority() -> None:
    command = supervisor.host_child_command(
        ["--port", "8002", "-g", "lab.json", "--server_database_root", "/root"],
        authority_port=8002,
        database_root="/root/edge",
    )
    assert command[1:3] == ["-m", "unilabos.app.main"]
    # 用户参数原样保留，追加的同名参数在后面覆盖（argparse 后者优先）
    assert command[3:8] == ["--port", "8002", "-g", "lab.json", "--server_database_root"]
    tail = command[9:]
    assert tail[tail.index("--address") + 1] == "http://127.0.0.1:8002"
    assert tail[tail.index("--material_microbackend_addr") + 1] == "http://127.0.0.1:8002"
    assert tail[tail.index("--server_database_root") + 1] == "/root/edge"
    assert "--skip_env_check" in tail and "--disable_browser" in tail
    # Host 子进程不监听端口：不再给它 --port
    assert "--port" not in tail


class _FakeChild:
    def __init__(self, code: int) -> None:
        self._code_after = code
        self._code: int | None = None
        self.pid = 4321
        self.signals: list = []
        self.terminated = False

    def wait(self, timeout=None):
        if self._code is None:
            self._code = self._code_after
        return self._code

    def poll(self):
        return self._code

    def send_signal(self, sig):
        self.signals.append(sig)
        self._code = 0

    def terminate(self):
        self.terminated = True
        self._code = 143

    def kill(self):
        self._code = 137


class _RunningChild(_FakeChild):
    """一直运行到收到信号：wait 按超时返回，而不是像 _FakeChild 那样立即退出。"""

    def __init__(self) -> None:
        super().__init__(0)
        self._exited = threading.Event()

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("host", timeout)
        return self._code

    def send_signal(self, sig):
        super().send_signal(sig)
        self._exited.set()


def test_host_child_supervisor_relaunches_on_restart_code(monkeypatch: pytest.MonkeyPatch) -> None:
    codes = [supervisor.RESTART_EXIT_CODE, supervisor.RESTART_EXIT_CODE, 0]
    launched: list[dict] = []

    def fake_popen(command, env=None, creationflags=0, **_kwargs):
        launched.append({"command": list(command), "env": env})
        return _FakeChild(codes.pop(0))

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor._KillOnCloseJob, "create", classmethod(lambda cls: None))
    monkeypatch.setattr(supervisor, "_POLL_S", 0.001)

    child = supervisor.HostChildSupervisor(["python", "-m", "unilabos.app.main"])
    child.start()
    child._thread.join(timeout=5)

    assert len(launched) == 3
    assert child.restart_count == 2
    assert launched[0]["env"][supervisor.HOST_CHILD_ENV] == "1"
    assert launched[0]["env"][supervisor.SUPERVISOR_INNER_ENV] == "1"


def test_host_child_supervisor_backs_off_after_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    codes = [1, 1, 0]
    launched = 0

    def fake_popen(command, env=None, creationflags=0, **_kwargs):
        nonlocal launched
        launched += 1
        return _FakeChild(codes.pop(0))

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor._KillOnCloseJob, "create", classmethod(lambda cls: None))
    monkeypatch.setattr(supervisor, "_POLL_S", 0.001)
    monkeypatch.setattr(supervisor, "_CRASH_BACKOFF_S", (0.01,))

    child = supervisor.HostChildSupervisor(["python"])
    child.start()
    child._thread.join(timeout=5)
    assert launched == 3
    assert child.crash_count == 2
    # V1 Workspace Host 快照的 edge 组件据此推导阶段：子进程已正常退出、不再看护
    assert child.state() == {
        "pid": None,
        "alive": False,
        "supervising": False,
        "stop_requested": False,
        "restart_count": 0,
        "crash_count": 2,
        "last_exit_code": 0,
    }


def test_host_child_supervisor_stop_requests_graceful_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeChild(supervisor.RESTART_EXIT_CODE)
    fake._code = None
    started = threading.Event()

    def fake_popen(command, env=None, creationflags=0, **_kwargs):
        started.set()
        return fake

    def slow_wait(timeout=None):
        # 子进程一直在跑，直到 stop() 发信号
        deadline = time.time() + (timeout or 0)
        while time.time() < deadline:
            if fake._code is not None:
                return fake._code
            time.sleep(0.001)
        if timeout is None:
            return fake._code or 0
        raise subprocess.TimeoutExpired("x", timeout)

    fake.wait = slow_wait  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor._KillOnCloseJob, "create", classmethod(lambda cls: None))
    monkeypatch.setattr(supervisor, "_POLL_S", 0.001)

    child = supervisor.HostChildSupervisor(["python"])
    child.start()
    assert started.wait(timeout=2)
    assert child.alive() is True
    child.stop(timeout=1)
    assert fake.signals or fake.terminated
    assert child.alive() is False


def test_request_stop_signals_child_once_and_stop_only_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """权威收到停机信号先非阻塞地请 Host 退出；随后的 stop() 不再补发第二个 Ctrl+Break。"""

    fake = _RunningChild()

    def fake_popen(command, env=None, creationflags=0, **_kwargs):
        return fake

    exit_requested = threading.Event()

    def graceful(child):
        exit_requested.set()
        child.send_signal("CTRL_BREAK")

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor._KillOnCloseJob, "create", classmethod(lambda cls: None))
    monkeypatch.setattr(supervisor, "_POLL_S", 0.001)
    monkeypatch.setattr(supervisor, "_request_graceful_exit", graceful)

    child = supervisor.HostChildSupervisor(["python"])
    child.start()
    # Popen 返回后 supervisor 才在锁内记下子进程；等它真正可见再请求停止。
    deadline = time.monotonic() + 2
    while not child.alive() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert child.alive()

    child.request_stop()
    assert exit_requested.is_set()
    assert fake.signals == ["CTRL_BREAK"]
    child.request_stop()  # 幂等
    child.stop(timeout=1)
    assert fake.signals == ["CTRL_BREAK"]  # 没有第二个信号打断子进程的清理
    assert child.alive() is False


def test_host_child_serves_backend_http_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host 子进程不监听端口：WS 收到 backend_http 后对自己的 ASGI 应用执行，再 POST 结果回权威。"""

    import asyncio
    import base64

    from unilabos.protocol.runtime.control import EdgeHttpResponse
    from unilabos.server.api import app as app_module
    from unilabos.server.backend.legacy_adaptor.websocket import BackendWebSocketClient

    host_app = FastAPI()

    @host_app.post("/api/v1/hostlink/material-sync")
    async def material_sync(request: Request):
        return {"echo": await request.json(), "probe": request.headers.get("x-probe")}

    monkeypatch.setattr(app_module, "app", host_app)
    # 路由已由 setup_server 挂好（真实进程里由 serve_over_control_plane 置位）
    monkeypatch.setattr(app_module, "wait_routes_ready", lambda timeout: True)
    posted: list[EdgeHttpResponse] = []
    client = BackendWebSocketClient(websocket_url="ws://authority/api/v1/ws/schedule")

    async def capture(response: EdgeHttpResponse) -> None:
        posted.append(response)

    monkeypatch.setattr(client, "_post_http_response", capture)

    body = base64.b64encode(b'{"device_id": "bench"}').decode()
    asyncio.run(
        client._serve_backend_http(
            {
                "request_uuid": "req-1",
                "method": "POST",
                "path": "/api/v1/hostlink/material-sync?x=1",
                "headers": {"content-type": "application/json", "x-probe": "yes"},
                "body_base64": body,
            }
        )
    )
    assert len(posted) == 1
    response = posted[0]
    assert response.request_uuid == "req-1" and response.status_code == 200
    assert json.loads(response.body_bytes()) == {"echo": {"device_id": "bench"}, "probe": "yes"}

    # 坏路径也要回一个响应（404），权威侧不会干等
    asyncio.run(client._serve_backend_http({"request_uuid": "req-2", "method": "GET", "path": "/nope"}))
    assert posted[-1].status_code == 404


def test_host_child_backend_http_waits_for_routes_before_executing(monkeypatch: pytest.MonkeyPatch) -> None:
    """控制 WS 先于 setup_server 连上权威：路由没挂好之前不能对 app 执行（否则把"还没挂 /history"
    误报成 404，权威拉结果 payload 会拿到空）；等不到就回 503 而不是 404。"""

    import asyncio

    from unilabos.protocol.runtime.control import EdgeHttpResponse
    from unilabos.server.api import app as app_module
    from unilabos.server.backend.legacy_adaptor.websocket import BackendWebSocketClient

    host_app = FastAPI()

    @host_app.get("/api/v1/edge/history/payloads/p1")
    async def payload():
        return {"inline_payload": "e30="}

    monkeypatch.setattr(app_module, "app", host_app)
    waited: list[float] = []

    def not_ready(timeout: float) -> bool:
        waited.append(timeout)
        return False

    monkeypatch.setattr(app_module, "wait_routes_ready", not_ready)
    posted: list[EdgeHttpResponse] = []
    client = BackendWebSocketClient(websocket_url="ws://authority/api/v1/ws/schedule")

    async def capture(response: EdgeHttpResponse) -> None:
        posted.append(response)

    monkeypatch.setattr(client, "_post_http_response", capture)
    asyncio.run(
        client._serve_backend_http(
            {"request_uuid": "req-3", "method": "GET", "path": "/api/v1/edge/history/payloads/p1", "timeout_seconds": 5}
        )
    )
    assert waited == [5.0]
    # 路由未挂好：与「Host 不在线」同形，HTTP 200 + 业务码 5001
    reply = json.loads(posted[-1].body_bytes())
    assert posted[-1].status_code == 200 and reply["code"] == 5001
    assert "尚未挂载" in reply["error"]["msg"]

    # 路由就绪后同一请求正常执行
    monkeypatch.setattr(app_module, "wait_routes_ready", lambda timeout: True)
    asyncio.run(
        client._serve_backend_http({"request_uuid": "req-4", "method": "GET", "path": "/api/v1/edge/history/payloads/p1"})
    )
    assert posted[-1].status_code == 200


def test_runs_local_authority_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    from unilabos.app.main import _runs_local_authority
    from unilabos.config.config import HTTPConfig

    monkeypatch.delenv(supervisor.HOST_CHILD_ENV, raising=False)
    monkeypatch.setattr(HTTPConfig, "remote_addr", "")
    assert _runs_local_authority({}) is True
    assert _runs_local_authority({"no_safe_restart": True}) is False
    assert _runs_local_authority({"is_slave": True}) is False
    monkeypatch.setattr(HTTPConfig, "remote_addr", "http://cloud:8081")
    assert _runs_local_authority({}) is False
    monkeypatch.setattr(HTTPConfig, "remote_addr", "")
    monkeypatch.setenv(supervisor.HOST_CHILD_ENV, "1")
    assert _runs_local_authority({}) is False
