"""进程寿命：本机拉起的子进程跟随父进程退出，停机信号限时、及时完成，不留孤儿占着数据库根目录。

前半部分是轻量用例（小 Python 子进程 / 桩），各平台都跑；``slow`` 标记的用例起真实 ``unilab``
（调度权威 + Host 子进程、``--role backend`` + ``--address`` 的远端 Host），每个要几十秒。
真实进程只结束本测试自己拉起的进程（按本用例临时目录里的数据库根目录锁找到 Host 子进程）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

import unilabos
from unilabos.app import lifetime, supervisor
from unilabos.server.database.root_lock import (
    claim_database_root,
    read_database_root_holder,
    release_database_root,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = Path(unilabos.__file__).resolve().parent / "config" / "example_config.py"
CONTROL_TOKEN = "process-lifetime-test-token"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="Windows 没有可从外部投递的 SIGTERM / SIGINT")


def _env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("UNILABOS_")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", NO_COLOR="1")
    env.update(extra)
    return env


def _pid_alive(pid: int) -> bool:
    """进程是否还在（僵尸算已退出：孤儿被收养后由 init 回收，容器里的 init 未必及时回收）。"""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT：仍在运行
        finally:
            kernel32.CloseHandle(handle)
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_gone(pid: int, timeout: float) -> Optional[float]:
    """等进程消失，返回用时；超时返回 None。"""

    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if not _pid_alive(pid):
            return time.monotonic() - started
        time.sleep(0.1)
    return None


def _kill_pid(pid: int) -> None:
    """兜底结束本测试拉起的进程。"""

    if not _pid_alive(pid):
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass


# ── 轻量：跟随父进程 ─────────────────────────────────────────────────

# 子进程：认领父进程后装一个会留痕的 SIGTERM 处理器（模拟 Host 的有序退出），然后一直跑
_FOLLOWER = r"""
import os, signal, sys, time
from pathlib import Path
from unilabos.app import lifetime

marker = Path(sys.argv[1])

def graceful(signum, frame):
    marker.write_text(f"graceful {signum}", encoding="utf-8")
    sys.exit(0)

signal.signal(signal.SIGTERM, graceful)
signal.signal(signal.SIGINT, graceful)
parent = lifetime.follow_parent_from_env()
print(f"FOLLOWING {parent} {os.environ.get(lifetime.PARENT_PID_ENV)}", flush=True)
while True:
    time.sleep(0.2)
"""

# 父进程：按 argv 决定是否写跟随环境，拉起 _FOLLOWER 后打印它的 pid，然后一直跑
_PARENT = r"""
import os, subprocess, sys, time
from unilabos.app import lifetime

follower, marker, mode = sys.argv[1], sys.argv[2], sys.argv[3]
env = dict(os.environ)
if mode != "none":
    lifetime.follow_parent_env(env, death_signal=(mode == "death-signal"))
child = subprocess.Popen([sys.executable, "-c", follower, marker], env=env, stdout=sys.stdout)
print(f"CHILD {child.pid}", flush=True)
while True:
    time.sleep(0.2)
"""


def _start_parent(tmp_path: Path, mode: str) -> tuple[subprocess.Popen[str], int, Path, list[str]]:
    marker = tmp_path / f"graceful-{mode}.txt"
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, _FOLLOWER, str(marker), mode],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    lines: list[str] = []
    child_pid = 0
    assert parent.stdout is not None
    deadline = time.monotonic() + 60
    for line in parent.stdout:
        lines.append(line)
        if line.startswith("CHILD "):
            child_pid = int(line.split()[1])
        if line.startswith("FOLLOWING "):
            break
        if time.monotonic() > deadline:
            break
    assert child_pid, "".join(lines)
    return parent, child_pid, marker, lines


@pytest.mark.parametrize("mode", ["poll", "death-signal"])
def test_child_follows_a_killed_parent(tmp_path: Path, mode: str) -> None:
    """父进程被强制结束（SIGKILL / TerminateProcess），子进程察觉后走自己的有序退出路径。"""

    if mode == "death-signal" and not sys.platform.startswith("linux"):
        pytest.skip("PR_SET_PDEATHSIG 只在 Linux 上有")
    parent, child_pid, marker, lines = _start_parent(tmp_path, mode)
    try:
        # 子进程认领了父进程 pid，并且把环境变量删掉了（不会传给孙进程）
        following = next(line for line in lines if line.startswith("FOLLOWING "))
        assert following.split()[1:] == [str(parent.pid), "None"]
        assert _pid_alive(child_pid)
        parent.kill()
        parent.wait(timeout=30)
        elapsed = _wait_gone(child_pid, timeout=30)
        assert elapsed is not None, "父进程被结束后子进程没有退出"
        assert elapsed < 15
        # 走的是子进程自己的 SIGTERM 处理器（Host 在这里关库、停受管进程），而不是被硬杀
        assert marker.read_text(encoding="utf-8").startswith("graceful")
    finally:
        _kill_pid(child_pid)
        if parent.poll() is None:
            parent.kill()
        if parent.stdout is not None:
            parent.stdout.close()


def test_child_without_parent_env_outlives_its_parent(tmp_path: Path) -> None:
    """没被要求跟随的进程（用户直接起的 unilab、``--address`` 的薄监督进程）不受父进程影响。"""

    parent, child_pid, marker, lines = _start_parent(tmp_path, "none")
    try:
        assert next(line for line in lines if line.startswith("FOLLOWING ")).split()[1:] == ["None", "None"]
        parent.kill()
        parent.wait(timeout=30)
        time.sleep(3 * lifetime.PARENT_POLL_S + 1)
        assert _pid_alive(child_pid)
        assert not marker.exists()
    finally:
        _kill_pid(child_pid)
        if parent.stdout is not None:
            parent.stdout.close()


def test_child_whose_parent_is_already_gone_exits_at_once(tmp_path: Path) -> None:
    """认领时父进程已经不在（启动途中父进程就被结束）：立即退出，不进入运行。"""

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(timeout=30)
    script = (
        "import time\n"
        "from unilabos.app import lifetime\n"
        "lifetime.follow_parent_from_env()\n"
        "time.sleep(30)\n"
        "print('STILL RUNNING', flush=True)\n"
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=_env(**{lifetime.PARENT_PID_ENV: str(gone.pid)}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert time.monotonic() - started < 20
    assert result.returncode != 0
    assert "STILL RUNNING" not in result.stdout


def test_follow_parent_env_is_consumed_and_invalid_values_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    env: dict[str, str] = {lifetime.PARENT_DEATH_SIGNAL_ENV: "1"}
    lifetime.follow_parent_env(env)
    assert env == {lifetime.PARENT_PID_ENV: str(os.getpid())}
    lifetime.follow_parent_env(env, death_signal=True)
    assert env[lifetime.PARENT_DEATH_SIGNAL_ENV] == "1"

    followed: list[tuple[int, bool]] = []
    monkeypatch.setattr(lifetime, "follow_parent", lambda pid, death_signal=False: followed.append((pid, death_signal)))
    assert lifetime.follow_parent_from_env(env) == os.getpid()
    assert followed == [(os.getpid(), True)]
    assert env == {}
    assert lifetime.follow_parent_from_env(env) is None
    assert lifetime.follow_parent_from_env({lifetime.PARENT_PID_ENV: "not-a-pid"}) is None
    assert followed == [(os.getpid(), True)]


# ── 轻量：停机限时与收尾 ─────────────────────────────────────────────


def test_shutdown_deadline_forces_exit_and_repeated_signals_are_ignored(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import threading, time
        from unilabos.app import lifetime

        assert lifetime.begin_shutdown(7, reason="测试", deadline=0.5) is True
        assert lifetime.begin_shutdown(9, reason="重复信号") is False
        assert lifetime.shutdown_started()
        # 收尾卡住：非守护线程永远不结束，主线程也不返回
        threading.Thread(target=threading.Event().wait).start()
        time.sleep(60)
        """
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script], env=_env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 7, result.stdout + result.stderr
    assert time.monotonic() - started < 30
    assert "测试：" in result.stdout and "强制结束进程" in result.stdout


def test_shutdown_deadline_exits_even_when_reporting_blocks(tmp_path: Path) -> None:
    """限时不能依赖输出流：卡住的线程占着 stdout 的锁时，打印报告会永远阻塞，进程仍须按时退出。

    CI 曾见过权威被强杀后 Host 子进程在 20 s 限时之后还在重连、日志里没有"强制结束"——
    限时线程卡在打印上，os._exit 没执行到。
    """

    script = textwrap.dedent(
        """
        import threading, time
        from unilabos.app import lifetime

        lifetime.print_status = lambda *args, **kwargs: threading.Event().wait()  # 输出永远阻塞
        assert lifetime.begin_shutdown(7, reason="测试", deadline=0.3) is True
        threading.Thread(target=threading.Event().wait).start()
        time.sleep(60)
        """
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script], env=_env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 7, result.stdout + result.stderr
    assert time.monotonic() - started < 15
    assert "强制结束进程" in result.stderr and "绕过缓冲区" in result.stderr


def test_exit_now_runs_atexit_and_does_not_join_busy_worker_threads(tmp_path: Path) -> None:
    """有序收尾之后不再让解释器 join 线程池：等 Host 路由 / 跑设备动作的工作线程曾把退出拖上几分钟。"""

    marker = tmp_path / "atexit.txt"
    script = textwrap.dedent(
        f"""
        import atexit, threading
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from unilabos.app import lifetime

        atexit.register(lambda: Path({str(marker)!r}).write_text("ran", encoding="utf-8"))
        pool = ThreadPoolExecutor(max_workers=1)
        pool.submit(threading.Event().wait)  # 永远不结束的工作项
        lifetime.exit_now(5)
        """
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script], env=_env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 5, result.stdout + result.stderr
    assert time.monotonic() - started < 30
    assert marker.read_text(encoding="utf-8") == "ran"


def test_exit_code_for_main_exceptions(capsys: pytest.CaptureFixture[str]) -> None:
    assert lifetime.exit_code_for(SystemExit()) == 0
    assert lifetime.exit_code_for(SystemExit(75)) == 75
    assert lifetime.exit_code_for(SystemExit("坏参数")) == 1
    assert "坏参数" in capsys.readouterr().err
    assert lifetime.exit_code_for(KeyboardInterrupt()) == 130
    assert lifetime.exit_code_for(RuntimeError("boom")) == 1
    assert "RuntimeError: boom" in capsys.readouterr().err


def test_wait_routes_ready_gives_up_once_shutdown_begins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host 主线程在挂路由时就收到停机信号：线程池里等路由的 backend_http 不能等满超时。"""

    from unilabos.server import lifecycle
    from unilabos.server.api import app as app_module

    monkeypatch.setattr(app_module, "_routes_ready", threading.Event())  # 路由还没挂好
    monkeypatch.setattr(app_module, "_abort_serving", threading.Event())
    lifecycle.reset_shutdown_state()
    result: list[bool] = []
    waiter = threading.Thread(target=lambda: result.append(app_module.wait_routes_ready(30.0)))
    waiter.start()
    try:
        time.sleep(0.3)
        assert waiter.is_alive()
        lifecycle.begin_shutdown()
        waiter.join(timeout=5)
        assert not waiter.is_alive() and result == [False]
    finally:
        lifecycle.reset_shutdown_state()


def test_authority_stop_signal_uses_graceful_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """权威的 SIGTERM / SIGHUP：uvicorn 在服务就请它停；否则抛 KeyboardInterrupt 沿 finally 收尾。"""

    from unilabos.app import backend_main
    from unilabos.server.api import app as app_module

    monkeypatch.setattr(app_module, "request_server_shutdown", lambda: True)
    assert backend_main._stop_on_signal(signal.SIGTERM, None) is None
    monkeypatch.setattr(app_module, "request_server_shutdown", lambda: False)
    with pytest.raises(KeyboardInterrupt):
        backend_main._stop_on_signal(signal.SIGTERM, None)


# ── 轻量：HostChildSupervisor 停机竞态与升级 ─────────────────────────


class _Child:
    """一直运行到收到退出请求（或被 kill）的子进程桩。"""

    def __init__(self, *, obeys: bool = True) -> None:
        self.pid = 5150
        self.obeys = obeys
        self.signals: list[str] = []
        self.killed = False
        self._exited = threading.Event()
        self._code: Optional[int] = None

    def poll(self) -> Optional[int]:
        return self._code

    def wait(self, timeout: Optional[float] = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("host", timeout or 0)
        return int(self._code or 0)

    def _exit(self, code: int) -> None:
        self._code = code
        self._exited.set()

    def terminate(self) -> None:
        self.signals.append("terminate")
        if self.obeys:
            self._exit(0)

    def send_signal(self, sig: Any) -> None:
        self.signals.append(str(sig))
        if self.obeys:
            self._exit(0)

    def kill(self) -> None:
        self.killed = True
        self._exit(-9)


@pytest.fixture()
def fast_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor._KillOnCloseJob, "create", classmethod(lambda cls: None))
    monkeypatch.setattr(supervisor, "_POLL_S", 0.01)


def test_host_child_launched_while_stopping_is_asked_to_exit(
    monkeypatch: pytest.MonkeyPatch, fast_supervisor: None
) -> None:
    """权威开始停机时看护线程恰好在 Popen：request_stop 还看不到这一代，拉起后必须立刻补发退出请求。"""

    holder: dict[str, Any] = {}
    child = _Child()

    def popen_racing_with_stop(command, env=None, creationflags=0, **_kwargs):
        holder["supervisor"].request_stop()  # Popen 期间收到停机
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen_racing_with_stop)
    host = supervisor.HostChildSupervisor(["python"])
    holder["supervisor"] = host
    # 子进程跟随权威：Linux 上启用死亡信号（拉起它的是常驻看护线程）
    assert host._env[lifetime.PARENT_PID_ENV] == str(os.getpid())
    assert host._env[lifetime.PARENT_DEATH_SIGNAL_ENV] == "1"
    host.start()
    assert host._thread is not None
    host._thread.join(timeout=5)
    assert not host._thread.is_alive()
    assert len(child.signals) == 1  # 只请求一次，不会打断它的收尾
    host.stop(timeout=1)
    assert len(child.signals) == 1
    assert child.killed is False


def test_host_child_that_ignores_the_exit_request_is_killed_after_the_grace(
    monkeypatch: pytest.MonkeyPatch, fast_supervisor: None
) -> None:
    child = _Child(obeys=False)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_a, **_k: child)
    host = supervisor.HostChildSupervisor(["python"])
    host.start()
    deadline = time.monotonic() + 5
    while not host.alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    host.stop(timeout=0.5)
    assert time.monotonic() - started < 5
    assert child.signals and child.killed is True
    assert host.alive() is False


def test_thin_supervisor_bounds_the_wait_after_forwarding_sigterm(
    monkeypatch: pytest.MonkeyPatch, fast_supervisor: None
) -> None:
    """薄监督进程把 SIGTERM 转发给 inner 后限时等待；inner 跟随监督进程（远端权威掉线不影响它）。"""

    child = _Child(obeys=False)
    captured: dict[str, Any] = {}
    handlers: dict[int, Any] = {}

    def fake_popen(command, env=None, **_kwargs):
        captured["env"] = env
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))
    monkeypatch.setattr(supervisor, "CHILD_EXIT_GRACE_S", 0.2)

    original_wait = child.wait

    def wait(timeout: Optional[float] = None) -> int:
        if not captured.get("sent"):
            captured["sent"] = True
            handlers[signal.SIGTERM](signal.SIGTERM, None)  # 监督进程收到 SIGTERM
        return original_wait(timeout)

    child.wait = wait  # type: ignore[method-assign]
    started = time.monotonic()
    code = supervisor.run_supervisor(["--address", "http://cloud:8081"])
    assert time.monotonic() - started < 15
    assert child.signals[0] == "terminate" and child.killed is True
    assert code == -9
    assert captured["env"][lifetime.PARENT_PID_ENV] == str(os.getpid())
    assert captured["env"][lifetime.PARENT_DEATH_SIGNAL_ENV] == "1"


# ── 真实进程（较慢）──────────────────────────────────────────────────


def _free_port() -> int:
    # 与 README demo 端到端用例同一套选端口规则：Windows 上避开系统动态端口范围（HostLink 独占绑定）
    from tests.e2e.readme_demos import free_port

    return free_port()


class _Unilab:
    """本测试拉起的一个 ``unilab`` 进程（输出写到日志文件）。"""

    def __init__(self, tmp_path: Path, name: str, args: list[str], *, root: Path) -> None:
        self.root = root
        self.log = tmp_path / f"{name}.log"
        self.port = _free_port()
        package = tmp_path / "empty_devices"
        package.mkdir(exist_ok=True)
        command = [
            sys.executable, "-m", "unilabos.app.main", "--backend", "hostlink", "--skip_env_check",
            "--config", str(EXAMPLE_CONFIG), "--disable_browser", "--external_devices_only",
            "--devices", str(package), "--port", str(self.port), "--hostlink_port", str(_free_port()),
            "--working_dir", str(tmp_path / f"{name}-work"), "--server_database_root", str(root),
            *args,
        ]
        self._stream = self.log.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            env=_env(UNILABOS_BASICCONFIG_CONTROL_TOKEN=CONTROL_TOKEN),
            stdout=self._stream,
            stderr=subprocess.STDOUT,
            # POSIX 上独立会话：测试对它发的信号不会波及测试进程自己
            start_new_session=os.name != "nt",
        )
        self.extra_pids: list[int] = []

    def text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def wait_for(self, predicate: Callable[[], Any], what: str, timeout: float = 240.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail(f"unilab 提前退出（{what}，code={self.process.returncode}）:\n{self.text()[-6000:]}")
            try:
                value = predicate()
            except Exception:  # noqa: BLE001 - 尚未就绪
                value = None
            if value:
                return value
            time.sleep(0.3)
        pytest.fail(f"等待超时：{what}\n{self.text()[-6000:]}")

    def health(self) -> dict[str, Any]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/v1/health", timeout=10) as response:
            return json.load(response)

    def host_child_pid(self) -> int:
        """等默认拓扑的 Host 子进程接入控制面并持有 <根目录>/edge，返回它的 pid。"""

        def ready() -> Optional[int]:
            holder = read_database_root_holder(self.root / "edge")
            if self.health().get("execution") != "ready" or not holder or holder.get("role") != "Host 子进程":
                return None
            return int(holder["pid"]) if _pid_alive(int(holder["pid"])) else None

        pid = self.wait_for(ready, "Host 子进程就绪")
        self.extra_pids.append(pid)
        return pid

    def close(self) -> None:
        for pid in [self.process.pid, *self.extra_pids]:
            _kill_pid(pid)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        self._stream.close()


def _assert_root_free(root: Path, wait_s: float = 15.0) -> None:
    lock = claim_database_root(root, role="测试", wait_s=wait_s)
    assert lock.held
    release_database_root(root)


@pytest.mark.slow
@POSIX_ONLY
def test_authority_sigterm_stops_its_host_child_and_releases_both_roots(tmp_path: Path) -> None:
    root = tmp_path / "db"
    authority = _Unilab(tmp_path, "authority", [], root=root)
    try:
        host = authority.host_child_pid()
        started = time.monotonic()
        authority.process.send_signal(signal.SIGTERM)
        code = authority.process.wait(timeout=60)
        authority_s = time.monotonic() - started
        host_s = _wait_gone(host, timeout=30)
        assert host_s is not None, "权威 SIGTERM 后 Host 子进程没有退出:\n" + authority.text()[-4000:]
        # 有序退出：权威沿 finally 等 Host 退出、关库后以 0 结束，而不是被重新抛出的 SIGTERM 杀掉
        assert code == 0, authority.text()[-4000:]
        assert authority_s < 20 and host_s < 20, (authority_s, host_s)
        assert "正在停止 Host 子进程" in authority.text()
        _assert_root_free(root)
        _assert_root_free(root / "edge")
    finally:
        authority.close()


@pytest.mark.slow
def test_host_child_follows_a_killed_authority_and_the_root_starts_cleanly(tmp_path: Path) -> None:
    """权威被强制结束（SIGKILL / TerminateProcess）：Host 子进程限时自行退出，同一根目录的下一次
    ``unilab`` 正常拉起 Host 子进程（不会因 <根目录>/edge 被遗留进程占着而 73）。"""

    root = tmp_path / "db"
    first = _Unilab(tmp_path, "first", [], root=root)
    try:
        host = first.host_child_pid()
        first.process.kill()
        first.process.wait(timeout=30)
        elapsed = _wait_gone(host, timeout=lifetime.SHUTDOWN_DEADLINE_S + 20)
        assert elapsed is not None, "权威被强制结束后 Host 子进程成了孤儿:\n" + first.text()[-4000:]
        _assert_root_free(root / "edge")
    finally:
        first.close()

    second = _Unilab(tmp_path, "second", [], root=root)
    try:
        new_host = second.host_child_pid()
        assert new_host != host
        assert read_database_root_holder(root)["pid"] == second.process.pid
        assert "数据库根目录被其他进程占用" not in second.text()
    finally:
        second.close()
        _wait_gone(second.process.pid, timeout=30)


@pytest.mark.slow
@POSIX_ONLY
def test_host_child_sigterm_exits_promptly(tmp_path: Path) -> None:
    root = tmp_path / "db"
    authority = _Unilab(tmp_path, "authority", [], root=root)
    try:
        host = authority.host_child_pid()
        os.kill(host, signal.SIGTERM)
        elapsed = _wait_gone(host, timeout=30)
        assert elapsed is not None and elapsed < 10, (elapsed, authority.text()[-4000:])
        _assert_root_free(root / "edge")
        assert authority.process.poll() is None  # 权威常驻
    finally:
        authority.close()


@pytest.mark.slow
def test_remote_address_host_survives_a_link_drop_but_follows_its_local_supervisor(tmp_path: Path) -> None:
    """``--address`` 连远端权威的 Host：权威（另一棵进程树，模拟远端）断开不会让它退出，权威回来后
    重连；它只跟随本机的薄监督进程。"""

    backend = _Unilab(tmp_path, "backend", ["--role", "backend"], root=tmp_path / "backend-db")
    backend.wait_for(lambda: backend.health().get("status") == "ok", "远端权威就绪")
    address = f"http://127.0.0.1:{backend.port}"
    host_root = tmp_path / "host-db"
    host = _Unilab(
        tmp_path, "remote-host", ["--address", address, "--material_microbackend_addr", address], root=host_root
    )
    restarted: Optional[subprocess.Popen[bytes]] = None
    try:
        host.wait_for(lambda: host.text().count("[ControlProtocol] Connected to") >= 1, "远端 Host 接入权威")
        holder = read_database_root_holder(host_root)
        assert holder is not None and holder["role"] == "Host"
        inner = int(holder["pid"])
        host.extra_pids.append(inner)
        assert inner != host.process.pid  # 外面一层是薄监督进程

        # 链路断开：远端权威没了，Host 只重连，不退出
        backend.process.kill()
        backend.process.wait(timeout=30)
        time.sleep(4 * lifetime.PARENT_POLL_S + 5)
        assert _pid_alive(inner) and host.process.poll() is None, host.text()[-4000:]

        # 权威回到同一地址：Host 重新接入
        restarted = subprocess.Popen(
            backend.process.args,
            env=_env(UNILABOS_BASICCONFIG_CONTROL_TOKEN=CONTROL_TOKEN),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        host.wait_for(lambda: host.text().count("[ControlProtocol] Connected to") >= 2, "远端 Host 重新接入")

        # 本机薄监督进程被强制结束：inner 跟随它退出，释放自己的根目录
        host.process.kill()
        host.process.wait(timeout=30)
        assert _wait_gone(inner, timeout=lifetime.SHUTDOWN_DEADLINE_S + 20) is not None, host.text()[-4000:]
        _assert_root_free(host_root)
    finally:
        host.close()
        backend.close()
        if restarted is not None:
            _kill_pid(restarted.pid)
            restarted.wait(timeout=30)
