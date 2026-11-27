"""子进程日志的真实生命周期、字节完整性和故障隔离验证。"""

from __future__ import annotations

import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from unilabos.utils import process_output
from unilabos.utils.log_storage import LogPolicy, RotatingByteWriter, read_log_tail
from unilabos.utils.process_output import ProcessOutput, session_log_path


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("等待子进程条件超时")
        time.sleep(0.01)


def _log_mtime_ns(path: Path) -> int | None:
    """轮转会把当前文件瞬间改名；这段空隙里 stat 会报文件不存在。"""

    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _rotated_segments_within(directory: Path, name: str, limit: int) -> bool:
    """等备份 .3 出现，并且每个还在的片段都不超过上限。"""

    try:
        segments = list(directory.glob(name + "*"))
        return any(segment.name == f"{name}.3" for segment in segments) and all(
            segment.stat().st_size <= limit for segment in segments
        )
    except FileNotFoundError:
        return False


def test_raw_bytes_rotate_live_and_drain_partial_final_chunk(tmp_path: Path) -> None:
    path = tmp_path / "worker.log"
    gate = tmp_path / "continue"
    payload = bytes(range(256)) * 2048
    code = (
        "import os,time;from pathlib import Path;"
        "os.write(1,b'START\\n');"
        f"gate=Path({str(gate)!r})\n"
        "while not gate.exists(): time.sleep(.01)\n"
        "os.write(1,bytes(range(256))*2048);os.write(2,b'FINAL\\xff')"
    )
    with ProcessOutput(path, LogPolicy(max_bytes=128 * 1024, backup_count=9)) as output:
        child = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=output.stream,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for(lambda: path.exists() and path.read_bytes() == b"START\n")
        gate.touch()
        assert child.wait(timeout=5.0) == 0
        assert output.wait() == 0
        assert read_log_tail(path, 1024 * 1024) == b"START\n" + payload + b"FINAL\xff"
        assert len(list(tmp_path.glob("worker.log.*"))) >= 4
        assert all(p.stat().st_size <= 128 * 1024 for p in tmp_path.glob("worker.log*"))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        output.wait()


def test_failed_spawn_releases_collector_without_a_pipe_leak(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        with ProcessOutput(tmp_path / "spawn.log") as output:
            subprocess.Popen([str(tmp_path / "missing-executable")], stdout=output.stream)
    assert output.wait() == 0


def test_bad_directory_or_duplicate_writer_fails_before_business_spawn(tmp_path: Path) -> None:
    path = tmp_path / "worker.log"
    with ProcessOutput(path) as first:
        with pytest.raises(OSError, match="日志接收器启动失败"):
            ProcessOutput(path)
    assert first.wait() == 0
    bad = tmp_path / "bad.log"
    bad.mkdir()
    with pytest.raises(OSError, match="日志接收器启动失败"):
        ProcessOutput(bad)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 软链接验证")
def test_collector_does_not_follow_final_log_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.log"
    target.write_bytes(b"untouched")
    link = tmp_path / "linked.log"
    link.symlink_to(target)
    with pytest.raises(OSError, match="日志接收器启动失败"):
        ProcessOutput(link)
    assert target.read_bytes() == b"untouched"


def test_disk_failure_keeps_real_child_output_draining(tmp_path: Path) -> None:
    directory = tmp_path / "disk"
    path = directory / "worker.log"
    ready = tmp_path / "drained"
    gate = tmp_path / "continue"
    code = (
        "import os,time;from pathlib import Path;"
        "[os.write(1,b'x'*65536) for _ in range(256)];"
        f"Path({str(ready)!r}).touch()\n"
        f"while not Path({str(gate)!r}).exists():time.sleep(.01)\n"
    )
    with ProcessOutput(path, LogPolicy(max_bytes=1024)) as output:
        directory.rename(tmp_path / "offline-disk")
        directory.write_text("模拟挂载点失效")
        child = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=output.stream,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for(ready.exists)
        with pytest.raises(OSError):
            # 写入失败期间仍持有原文件会话锁，清理器或第二写入者无法接管。
            RotatingByteWriter(tmp_path / "offline-disk" / "worker.log")
        gate.touch()
        assert child.wait(timeout=10.0) == 0
        assert output.wait() == 2
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_tee_preserves_native_console_when_disk_is_unavailable(tmp_path: Path) -> None:
    directory = tmp_path / "disk"
    path = directory / "worker.log"
    echo = tmp_path / "console.bin"
    with echo.open("wb", buffering=0) as original:
        with ProcessOutput(path, LogPolicy(max_bytes=1024), tee_stderr=original) as output:
            directory.rename(tmp_path / "offline-disk")
            directory.write_text("模拟挂载点失效")
            child = subprocess.Popen(
                [sys.executable, "-c", "import os;os.write(2,b'\\0'*262144)"],
                stdout=output.stream,
                stderr=subprocess.STDOUT,
            )
        assert child.wait(timeout=5.0) == 0
        assert output.wait() == 2
    assert echo.read_bytes().count(b"\0") == 262144
    assert "未落盘" in echo.read_bytes().decode("utf-8")


def test_capture_marker_is_bound_to_the_actual_stderr_pipe(tmp_path: Path) -> None:
    code = (
        "from unilabos.utils.process_output import is_process_output_captured;"
        "print(is_process_output_captured())"
    )
    path = tmp_path / "identity.log"
    with ProcessOutput(path) as output:
        environment = output.environment()
        child = subprocess.Popen(
            [sys.executable, "-c", code], env=environment,
            stdout=output.stream, stderr=subprocess.STDOUT,
        )
    assert child.wait(timeout=5.0) == 0
    assert output.wait() == 0
    # 不暴露可靠管道身份的平台必须拒绝复用，由 ROS 自行建立受限 tee。
    expected = "True" if output._pipe_marker is not None else "False"
    assert path.read_text().strip() == expected
    redirected = subprocess.run(
        [sys.executable, "-c", code], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5.0, check=True,
    )
    assert redirected.stdout.strip() == b"False"


def test_zero_inode_cannot_authorize_capture_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    # 只替换模块内的 os，不能改全局 os.fstat：pytest 的捕获栈在测试收尾时也会
    # 调用 os.fstat(...).st_size，返回缺少该字段的假对象会让收尾直接失败。
    fake_os = SimpleNamespace(
        fstat=lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO, st_dev=0, st_ino=0),
        environ={**os.environ, "UNILABOS_PROCESS_OUTPUT_CAPTURED": "0:0"},
    )
    monkeypatch.setattr(process_output, "os", fake_os)
    assert process_output._pipe_identity(2) is None
    assert not process_output.is_process_output_captured()


def test_killed_collector_unblocks_writer_with_explicit_broken_pipe(tmp_path: Path) -> None:
    gate = tmp_path / "continue"
    code = (
        "import os,time;from pathlib import Path;"
        f"gate=Path({str(gate)!r})\n"
        "while not gate.exists(): time.sleep(.01)\n"
        "try:\n"
        " for _ in range(1024): os.write(1,b'x'*65536)\n"
        "except BrokenPipeError: os._exit(42)\n"
    )
    with ProcessOutput(tmp_path / "killed.log") as output:
        child = subprocess.Popen(
            [sys.executable, "-c", code], stdout=output.stream, stderr=subprocess.DEVNULL,
        )
    try:
        output._process.kill()
        output.wait()
        gate.touch()
        assert child.wait(timeout=5.0) == 42
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_stuck_disk_has_bounded_memory_and_bounded_eof_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class StuckWriter:
        def __init__(self, *_args, **_kwargs):
            pass

        def write(self, _chunk):
            blocked.set()
            release.wait(timeout=10.0)

        def close(self):
            closed.set()

    class LargeStream:
        count = 0

        def read(self, size):
            assert size <= 65536
            if self.count == 1:
                assert blocked.wait(timeout=2.0)
            if self.count == 512:
                return b""
            self.count += 1
            return bytes(bytearray(size))

    monkeypatch.setattr(process_output, "RotatingByteWriter", StuckWriter)
    source = LargeStream()
    tracemalloc.start()
    try:
        started = time.monotonic()
        assert process_output._collect(
            source, tmp_path / "blocked.log", LogPolicy(), drain_timeout=0.05,
        ) == 2
        _, peak = tracemalloc.get_traced_memory()
        assert time.monotonic() - started < 2.0
        assert source.count == 512
        assert peak < 8 * 1024 * 1024
    finally:
        tracemalloc.stop()
        release.set()
        assert closed.wait(timeout=3.0)


def test_recovered_disk_reports_dropped_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[bytes] = []
    failed = False

    class FlakyWriter:
        def __init__(self, *_args, **_kwargs):
            pass

        def write(self, chunk):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("磁盘已满")
            writes.append(chunk)

        def close(self):
            pass

    class SlowStream(io.BytesIO):
        def read1(self, size):
            time.sleep(0.02)
            return super().read1(128)

    monkeypatch.setattr(process_output, "RotatingByteWriter", FlakyWriter)
    monkeypatch.setattr(process_output, "_RETRY_SECONDS", 0.001)
    result = process_output._collect(SlowStream(b"x" * 512), tmp_path / "recover.log", LogPolicy())
    assert result == 0
    assert "已丢弃 128 字节".encode() in b"".join(writes)
    assert b"".join(writes).endswith(b"x" * 128)


def test_old_unmarked_log_is_not_claimed_by_new_collector(tmp_path: Path) -> None:
    old = tmp_path / "workspace-host.log"
    old.write_bytes(b"legacy")
    selected = session_log_path(old)
    assert selected != old
    with ProcessOutput(selected) as output:
        output.stream.write(b"new-session")
    assert output.wait() == 0
    assert old.read_bytes() == b"legacy"
    assert selected.read_bytes() == b"new-session"


def test_workspace_collector_uses_launch_environment_policy(tmp_path: Path) -> None:
    from unilabos.workspace_host.host import WorkspaceHost
    from unilabos.workspace_host.launch import LaunchPlan
    from unilabos.workspace_host.model import WorkspacePaths

    paths = WorkspacePaths.resolve(tmp_path)
    paths.prepare()
    host = WorkspaceHost(paths, "policy-test")
    path = paths.logs / "policy-edge.log"
    environment = dict(os.environ)
    environment["UNILABOS_BASICCONFIG_LOG_MAX_BYTES"] = "1024"
    environment["UNILABOS_BASICCONFIG_LOG_BACKUP_COUNT"] = "3"
    plan = LaunchPlan(
        "edge", (sys.executable, "-c", "import os;os.write(1,b'x'*8192)"),
        tmp_path, environment, "policy", path, None, None, {},
    )
    try:
        host._spawn(plan)
        assert host._processes["edge"].wait(timeout=5.0) == 0
        _wait_for(lambda: _rotated_segments_within(paths.logs, path.name, 1024))
    finally:
        process = host._processes.get("edge")
        if process is not None and process.poll() is None:
            host._stop_component("edge")
        host.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX 脱离父进程与进程组收养验证")
@pytest.mark.parametrize("abrupt_exit", [False, True])
def test_host_exit_preserves_logging_and_adopted_pid_can_be_stopped(
    tmp_path: Path, abrupt_exit: bool,
) -> None:
    from unilabos.workspace_host.host import WorkspaceHost, _pid_exists, _terminate_process_tree
    from unilabos.workspace_host.model import WorkspacePaths

    paths = WorkspacePaths.resolve(tmp_path)
    log_path = paths.logs / "generation-edge.log"
    collector_info = tmp_path / "collector.json"
    child_code = (
        "import os,time\n"
        "while True:\n"
        " os.write(1,b'BEFORE-AND-AFTER-HOST-EXIT:' + b'x'*500 + b'\\n');time.sleep(.01)\n"
    )
    parent_code = f"""
import json, os, sys
from pathlib import Path
import unilabos.workspace_host.host as module
from unilabos.workspace_host.launch import LaunchPlan
from unilabos.workspace_host.model import WorkspacePaths
from unilabos.utils.process_output import ProcessOutput
class TrackedOutput(ProcessOutput):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Path({str(collector_info)!r}).write_text(json.dumps({{'pid': self.collector_pid}}))
module.ProcessOutput = TrackedOutput
paths = WorkspacePaths.resolve({str(tmp_path)!r})
paths.prepare()
host = module.WorkspaceHost(paths, 'lifecycle-test')
host._spawn(LaunchPlan('edge', (sys.executable, '-c', {child_code!r}), paths.workspace,
    dict(os.environ), 'generation', Path({str(log_path)!r}), None, None, {{}}))
host._components['edge']['phase'] = 'ready'
host._publish_locked('edge.ready', {{}})
if {abrupt_exit!r}:
    os._exit(0)
host.close()
"""
    environment = dict(os.environ)
    environment["UNILABOS_BASICCONFIG_LOG_MAX_BYTES"] = "1024"
    environment["UNILABOS_BASICCONFIG_LOG_BACKUP_COUNT"] = "3"
    parent = subprocess.Popen([sys.executable, "-c", parent_code], env=environment)
    recovered = None
    child_pid = None
    collector_pid = None
    try:
        assert parent.wait(timeout=10.0) == 0
        collector_pid = json.loads(collector_info.read_text())["pid"]
        child_pid = json.loads(paths.session.read_text())["components"]["edge"]["pid"]
        assert _pid_exists(child_pid)
        assert _pid_exists(collector_pid)
        _wait_for(lambda: (log_path.with_name(log_path.name + ".3")).exists())
        seen: list[int] = []

        def _remember_mtime() -> bool:
            current = _log_mtime_ns(log_path)
            if current is None:
                return False
            seen.append(current)
            return True

        _wait_for(_remember_mtime)
        _wait_for(
            lambda: (current := _log_mtime_ns(log_path)) is not None and current != seen[-1]
        )
        recovered = WorkspaceHost(paths, "lifecycle-test")
        assert recovered.snapshot()["components"]["edge"]["pid"] == child_pid
        assert "edge" not in recovered._processes
        assert "HOST-EXIT" in recovered.log_tail("edge", 4096)["content"]
        recovered._stop_component("edge")
        _wait_for(lambda: not _pid_exists(child_pid))
        _wait_for(lambda: not _pid_exists(collector_pid))
        assert len(list(paths.logs.glob("generation-edge.log*"))) <= 4
    finally:
        if recovered is not None:
            recovered.close()
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_pid is not None and _pid_exists(child_pid):
            _terminate_process_tree(child_pid, None)
        if collector_pid is not None and _pid_exists(collector_pid):
            os.kill(collector_pid, signal.SIGTERM)
