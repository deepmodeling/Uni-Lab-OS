"""一个数据库根目录只归一个实例：操作系统级独占锁、随进程消亡释放、Host 子进程重启可再取。

跨进程的用例都起真实子进程（锁是进程级的，同一进程内可重入，测不出互斥）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import unilabos
from unilabos.app import supervisor
from unilabos.server.database import root_lock
from unilabos.server.database.root_lock import (
    LOCK_FILENAME,
    DatabaseRootLocked,
    claim_database_root,
    held_database_roots,
    read_database_root_holder,
    release_database_root,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = Path(unilabos.__file__).resolve().parent / "config" / "example_config.py"

# 子进程：按启动入口的规则独占 argv[1]（与 unilab 主进程同一个函数），再按 argv[2] 行事。
_CLAIMER = r"""
import json, os, sys
from pathlib import Path

from unilabos.app.main import _claim_process_database_root
from unilabos.server.database import DatabaseRootLocked

root, action = sys.argv[1], sys.argv[2]
try:
    _claim_process_database_root({"server_database_root": root}, root)
except DatabaseRootLocked as exc:
    print("LOCKED " + json.dumps({"holder": exc.holder, "message": str(exc)}, ensure_ascii=False), flush=True)
    os._exit(73)
if action == "probe":
    print("ACQUIRED", flush=True)
    os._exit(0)
if action == "hold":
    print("READY", flush=True)
    sys.stdin.readline()  # 测试写入一行后正常退出（走 atexit）；kill 则模拟崩溃
    sys.exit(0)
if action == "restart-once":
    # Host 子进程的安静点重启：第一代持锁直接以 75 退出（os._exit 不跑 atexit，只靠操作系统释放），
    # 被再拉起的第二代必须能再取到锁
    record = Path(sys.argv[3])
    generation = len(record.read_text(encoding="utf-8").splitlines()) if record.exists() else 0
    with record.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"pid": os.getpid()}) + "\n")
    os._exit(75 if generation == 0 else 0)
raise SystemExit(f"unknown action {action}")
"""


def _env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("UNILABOS_")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", NO_COLOR="1")
    env.update(extra)
    return env


def _claimer(root: Path, action: str, *extra: str) -> list[str]:
    return [sys.executable, "-c", _CLAIMER, str(root), action, *extra]


def _probe(root: Path, **env: str) -> tuple[int, str]:
    result = subprocess.run(
        _claimer(root, "probe"),
        env=_env(**env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    return result.returncode, result.stdout + result.stderr


def _locked_report(output: str) -> dict[str, Any]:
    line = next(line for line in output.splitlines() if line.startswith("LOCKED "))
    return json.loads(line[len("LOCKED "):])


class _Holder:
    """在子进程里持有一个根目录，直到 quit() / kill()。"""

    def __init__(self, root: Path, **env: str) -> None:
        self.process = subprocess.Popen(
            _claimer(root, "hold"),
            env=_env(**env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        lines: list[str] = []
        assert self.process.stdout is not None
        for line in self.process.stdout:
            lines.append(line)
            if line.startswith("READY"):
                break
        else:
            self.process.wait(timeout=10)
            pytest.fail("持锁子进程没有就绪:\n" + "".join(lines))

    @property
    def pid(self) -> int:
        return self.process.pid

    def quit(self) -> int:
        assert self.process.stdin is not None
        self.process.stdin.write("quit\n")
        self.process.stdin.flush()
        return self.process.wait(timeout=30)

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(timeout=30)

    def close(self) -> None:
        if self.process.poll() is None:
            self.kill()
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()


# ── 进程内 ──────────────────────────────────────────────────────────


def test_claim_is_reentrant_within_one_process(tmp_path: Path) -> None:
    root = tmp_path / "db"
    first = claim_database_root(root, role="调度权威")
    # 组合根与启动入口在同一进程里多处打开同一根目录：拿到的是同一把锁
    assert claim_database_root(str(root) + os.sep) is first
    assert first.held and first.path == root.resolve() / LOCK_FILENAME
    assert root.resolve() in held_database_roots()
    holder = read_database_root_holder(root)
    assert holder is not None and holder["pid"] == os.getpid() and holder["role"] == "调度权威"
    # 持锁期间读持有者不能另开描述符（POSIX 记录锁会随之释放）：锁仍在
    assert _probe(root)[0] == 73

    assert release_database_root(root) is True
    assert release_database_root(root) is False
    assert root.resolve() not in held_database_roots()
    assert (root / LOCK_FILENAME).exists()  # 锁文件留着，不代表被占用
    assert _probe(root)[0] == 0
    assert root_lock.describe_holder(None).startswith("占用进程未知")


def test_check_mode_neither_takes_nor_needs_the_lock(tmp_path: Path) -> None:
    from unilabos.app.main import _claim_process_database_root

    root = tmp_path / "db"
    _claim_process_database_root({"check_mode": True, "server_database_root": str(root)}, str(tmp_path))
    assert root.resolve() not in held_database_roots()
    assert not root.exists()


# ── 跨进程 ──────────────────────────────────────────────────────────


def test_second_process_on_the_same_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "db"
    claim_database_root(root, role="调度权威")

    code, output = _probe(root)

    assert code == 73, output
    report = _locked_report(output)
    assert report["holder"]["pid"] == os.getpid()
    assert report["holder"]["role"] == "调度权威"
    assert report["holder"]["started_at"]
    message = report["message"]
    assert str(root.resolve()) in message
    assert f"pid {os.getpid()}" in message and "调度权威" in message
    assert "--server_database_root" in message
    assert sorted(path.name for path in root.iterdir()) == [LOCK_FILENAME]


@pytest.mark.parametrize("ending", ["kill", "exit"])
def test_lock_is_released_when_the_holder_ends(tmp_path: Path, ending: str) -> None:
    root = tmp_path / "db"
    holder = _Holder(root)
    try:
        with pytest.raises(DatabaseRootLocked) as caught:
            claim_database_root(root)
        assert caught.value.holder is not None and caught.value.holder["pid"] == holder.pid
        assert f"pid {holder.pid}" in str(caught.value)

        if ending == "kill":
            holder.kill()  # SIGKILL / TerminateProcess：不跑任何清理
        else:
            assert holder.quit() == 0
    finally:
        holder.close()

    # 不需要任何陈旧锁清理；Windows 进程结束后释放字节锁可能有毫秒级延迟
    lock = claim_database_root(root, wait_s=5.0)
    assert lock.holder["pid"] == os.getpid()


def test_host_child_restart_reacquires_its_root(tmp_path: Path) -> None:
    edge = tmp_path / "db" / "edge"
    record = tmp_path / "generations.jsonl"
    child = supervisor.HostChildSupervisor(
        _claimer(edge, "restart-once", str(record)),
        env=_env(),
    )
    child.start()
    assert child._thread is not None
    child._thread.join(timeout=120)
    try:
        assert not child._thread.is_alive()
    finally:
        child.stop(timeout=5)

    generations = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert len(generations) == 2, generations
    assert generations[0]["pid"] != generations[1]["pid"]
    assert child.restart_count == 1 and child.crash_count == 0
    assert child.last_exit_code == 0
    # 最后一代退出后同样释放
    assert claim_database_root(edge, wait_s=5.0).held


def test_relaunched_host_child_waits_for_the_previous_generation(tmp_path: Path) -> None:
    """被看护的进程再拉起时上一代可能还没释放：在 RELAUNCH_ROOT_LOCK_WAIT_S 内等到就取得。"""

    edge = tmp_path / "db" / "edge"
    record = tmp_path / "generations.jsonl"
    record.write_text(json.dumps({"pid": 0}) + "\n", encoding="utf-8")  # 直接作为第二代：取到即 0 退出
    claim_database_root(edge, role="上一代 Host 子进程")
    release_timer = threading.Timer(1.5, release_database_root, args=(edge,))
    child = supervisor.HostChildSupervisor(_claimer(edge, "restart-once", str(record)), env=_env())
    release_timer.start()
    child.start()
    try:
        assert child._thread is not None
        child._thread.join(timeout=60)
        assert not child._thread.is_alive()
    finally:
        release_timer.cancel()
        child.stop(timeout=5)
    assert child.last_exit_code == 0
    assert len(record.read_text(encoding="utf-8").splitlines()) == 2


def test_authority_and_edge_roots_are_locked_independently(tmp_path: Path) -> None:
    """默认拓扑：权威持有 <根目录>，Host 子进程持有 <根目录>/edge，互不等待。"""

    root = tmp_path / "db"
    edge = root / "edge"
    claim_database_root(root, role="调度权威")  # 本测试进程扮演权威
    host_child = _Holder(
        edge,
        **{supervisor.HOST_CHILD_ENV: "1", supervisor.SUPERVISOR_INNER_ENV: "1"},
    )
    try:
        edge_holder = read_database_root_holder(edge)
        assert edge_holder is not None
        assert edge_holder["pid"] == host_child.pid and edge_holder["role"] == "Host 子进程"
        assert read_database_root_holder(root)["pid"] == os.getpid()
        if os.name != "nt":
            # 锁文件布局：第 0 字节是被锁的占位字节，持有者记录从第 1 字节起（Windows 强制字节锁下
            # 别的进程读不到第 0 字节，但读得到记录）。本进程不持有 edge，另开描述符读没有副作用。
            raw = (edge / LOCK_FILENAME).read_bytes()
            assert raw[:1] == b"\n" and json.loads(raw[1:].decode("utf-8"))["pid"] == host_child.pid

        # 两个根目录都各自拒绝第三个进程，并说出各自的持有者
        code, output = _probe(root)
        assert code == 73 and _locked_report(output)["holder"]["pid"] == os.getpid()
        code, output = _probe(edge)
        assert code == 73 and _locked_report(output)["holder"]["pid"] == host_child.pid

        assert host_child.quit() == 0
    finally:
        host_child.close()
    # Host 子进程退出后 <根目录>/edge 可被下一代取得；权威仍持有 <根目录>
    assert _probe(edge)[0] == 0
    assert read_database_root_holder(root)["pid"] == os.getpid()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.parametrize(
    ("mode", "extra_args", "owned"),
    [
        ("default", [], ""),
        ("role-backend", ["--role", "backend"], "backend"),
        ("slave", ["--is_slave", "--host_node_ip", "127.0.0.1"], ""),
    ],
)
def test_unilab_refuses_a_held_root_before_touching_any_database(
    tmp_path: Path, mode: str, extra_args: list[str], owned: str
) -> None:
    root = tmp_path / "db"
    held = root / owned if owned else root
    claim_database_root(held, role="调度权威")
    command = [
        sys.executable,
        "-m",
        "unilabos.app.main",
        "--backend",
        "hostlink",
        "--skip_env_check",
        "--disable_browser",
        "--config",
        str(EXAMPLE_CONFIG),
        "--working_dir",
        str(tmp_path / "work"),
        "--server_database_root",
        str(root),
        "--port",
        str(_free_port()),
        *extra_args,
    ]
    try:
        result = subprocess.run(
            command,
            env=_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:  # 没被拒绝而是真的起来了
        pytest.fail(f"{mode}: unilab 没有拒绝已被占用的根目录:\n{exc.stdout}")
    output = result.stdout + result.stderr
    assert result.returncode == supervisor.DATABASE_ROOT_LOCKED_EXIT_CODE, output
    assert f"数据库根目录 {held.resolve()} 已被另一个 Uni-Lab-OS 进程占用" in output
    assert f"pid {os.getpid()}" in output
    assert "Traceback" not in output
    # 在打开任何库之前就失败
    assert sorted(path.name for path in held.iterdir()) == [LOCK_FILENAME]
    assert not any(root.rglob("*.db"))


# ── 真实默认拓扑（较慢，按需开启）──────────────────────────────────


def test_real_split_runtime_locks_both_roots_and_survives_host_restart(tmp_path: Path) -> None:
    """真实 ``unilab``：权威与 Host 子进程各持一把锁，Host 安静点重启后新一代再取到，第二个实例被拒绝。"""

    if os.environ.get("UNILABOS_TEST_ROOT_LOCK_RUNTIME") != "1":
        pytest.skip("设置 UNILABOS_TEST_ROOT_LOCK_RUNTIME=1 执行真实进程验证")
    import urllib.request

    management, hostlink = _free_port(), _free_port()
    root, work, package = tmp_path / "db", tmp_path / "work", tmp_path / "empty_devices"
    package.mkdir()

    def command(port: int, link: int) -> list[str]:
        return [
            sys.executable, "-m", "unilabos.app.main", "--backend", "hostlink", "--skip_env_check",
            "--config", str(EXAMPLE_CONFIG), "--disable_browser", "--external_devices_only",
            "--devices", str(package), "--port", str(port), "--hostlink_port", str(link),
            "--working_dir", str(work), "--server_database_root", str(root),
        ]

    def request(path: str, payload: Any = None) -> Any:
        headers = {"Content-Type": "application/json"}
        token = work / "control_token"
        if token.exists():
            headers["X-UniLab-Token"] = token.read_text(encoding="utf-8").strip()
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{management}/api/v1{path}", data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            body = json.load(response)
        return body.get("data", body) if isinstance(body, dict) and "code" in body else body

    def wait_for(predicate, what: str, timeout: float = 90.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"unilab 提前退出（{what}）:\n" + log.read_text(encoding="utf-8", errors="replace"))
            try:
                value = predicate()
            except Exception:  # noqa: BLE001 - 尚未就绪
                value = None
            if value:
                return value
            time.sleep(0.3)
        pytest.fail(f"等待超时：{what}\n" + log.read_text(encoding="utf-8", errors="replace"))

    log = tmp_path / "runtime.log"
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            command(management, hostlink), env=_env(), stdout=stream, stderr=subprocess.STDOUT
        )
        try:
            wait_for(lambda: request("/health").get("execution") == "ready", "Host 就绪")
            authority = read_database_root_holder(root)
            first_edge = read_database_root_holder(root / "edge")
            assert authority["pid"] == process.pid and authority["role"] == "调度权威"
            assert first_edge["role"] == "Host 子进程" and first_edge["pid"] != process.pid

            request("/restart", {})
            second_edge = wait_for(
                lambda: (holder := read_database_root_holder(root / "edge"))
                and holder["pid"] != first_edge["pid"]
                and request("/health").get("execution") == "ready"
                and holder,
                "Host 子进程重启并再取到 edge 锁",
            )
            assert second_edge["role"] == "Host 子进程"
            assert read_database_root_holder(root)["pid"] == process.pid  # 权威常驻

            second = subprocess.run(
                command(_free_port(), _free_port()), env=_env(), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            assert second.returncode == supervisor.DATABASE_ROOT_LOCKED_EXIT_CODE, second.stdout + second.stderr
            assert f"pid {process.pid}" in second.stdout + second.stderr
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=60)
    # 整棵进程树退出后两把锁都已释放
    assert claim_database_root(root, wait_s=5.0).held
    assert claim_database_root(root / "edge", wait_s=15.0).held

