import errno
import io
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

import scripts.opc_simulator_process_manager as process_manager_module
import scripts.szlab_task_opc_simulator as simulator
from scripts.opc_simulator_process_manager import (
    InvalidSimulatorConfig,
    InvalidSimulatorRevision,
    OpcSimulatorProcessManager,
    SIMULATOR_MODULE,
    SimulatorAlreadyRunning,
    SimulatorConfigNotFound,
    SimulatorRevisionConflict,
    SimulatorSpawnError,
    SimulatorStorageError,
    SimulatorStorageFull,
    StopTimeout,
)
from scripts.opc_simulator_profiles import save_profile
from scripts.szlab_task_opc_simulator import DEFAULT_URL


def _profile(
    status: str = "runnable",
    *,
    url: str = DEFAULT_URL,
) -> dict:
    return {
        "schema_version": 2,
        "status": status,
        "name": "process-manager-test",
        "opc": {
            "url": url,
            "poll_interval": 0.2,
            "io_timeout": 2.0,
        },
        "variables": [
            {
                "name": "command",
                "direction": "pc_to_plc",
                "data_type": "bool",
                "source": "task_input",
            },
            {
                "name": "done",
                "direction": "plc_to_pc",
                "data_type": "bool",
                "source": "task_output",
                "initial_value": False,
            },
        ],
        "nodes": [
            {
                "workflow_node_id": "node-1",
                "task_template_ids": ["task-1"],
                "device_id": "device-1",
                "method": "run",
                "params": {},
                "channel": "device-1",
                "trigger": {
                    "all": [
                        {
                            "variable": "command",
                            "operator": "eq",
                            "value": True,
                            "edge": "rising",
                        }
                    ]
                },
                "on_trigger": {"writes": [{"variable": "done", "value": False}]},
                "on_complete": {
                    "delay": 0.1,
                    "writes": [{"variable": "done", "value": True}],
                },
                "reset_when": None,
                "after_reset": None,
            }
        ],
    }


class FakeProcess:
    def __init__(
        self,
        *,
        pid: int = 321,
        logs: list[str] | None = None,
        return_code: int | None = None,
        complete_on_signal: bool = False,
        complete_on_kill: bool = True,
    ) -> None:
        self.pid = pid
        self.stdout = io.StringIO("".join(logs or []))
        self.returncode = return_code
        self.complete_on_signal = complete_on_signal
        self.complete_on_kill = complete_on_kill
        self.signals: list[int] = []
        self.kill_calls = 0
        self.poll_calls = 0
        self.wait_calls = []
        self._completed = threading.Event()
        if return_code is not None:
            self._completed.set()

    def poll(self):
        self.poll_calls += 1
        return self.returncode if self._completed.is_set() else None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if timeout is None:
            self._completed.wait()
        elif not self._completed.wait(timeout):
            raise subprocess.TimeoutExpired("simulator", timeout)
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)
        if self.complete_on_signal:
            self.finish(0)

    def kill(self):
        self.kill_calls += 1
        if self.complete_on_kill:
            self.finish(-signal.SIGKILL)

    def finish(self, return_code: int) -> None:
        self.returncode = return_code
        self._completed.set()


class BoundedReadlineStream:
    def __init__(self, value: str) -> None:
        self._stream = io.StringIO(value)
        self.sizes: list[int] = []

    def __iter__(self):
        raise AssertionError("日志读取不得使用无界行迭代")

    def readline(self, size: int = -1) -> str:
        self.sizes.append(size)
        assert 0 < size <= 4098
        return self._stream.readline(size)

    def close(self) -> None:
        self._stream.close()


def _saved_profile(tmp_path, *, status: str = "runnable"):
    return save_profile("demo.json", _profile(status), tmp_path)


class DeferredThread:
    def __init__(self, target, **_kwargs):
        self.target = target

    def start(self):
        return None


class GatedWaitProcess(FakeProcess):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.wait_entered = threading.Event()
        self.wait_release = threading.Event()
        self.after_wait_release = lambda: None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        self.wait_entered.set()
        if not self.wait_release.wait(1):
            raise AssertionError("测试未释放 wait 门闩")
        self.after_wait_release()
        return self.returncode


def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("等待后台线程状态更新超时")


def test_start_validates_then_spawns_with_fixed_safe_popen_contract(tmp_path):
    saved = _saved_profile(tmp_path)
    process = FakeProcess()
    calls = []
    thread_names = []

    def process_factory(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    def thread_factory(**kwargs):
        thread_names.append(kwargs["name"])
        return threading.Thread(**kwargs)

    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        script_path=tmp_path / "simulator.py",
        python_executable="/test/python",
        process_factory=process_factory,
        clock=lambda: 50.0,
        thread_factory=thread_factory,
    )

    assert manager.status()["state"] == "idle"
    status = manager.start("demo.json", saved["revision"])

    assert calls == [
        (
            [
                "/test/python",
                "-u",
                str(tmp_path / "simulator.py"),
                "--config",
                str((tmp_path / "demo.json").resolve()),
                "--expected-revision",
                saved["revision"],
            ],
            {
                "shell": False,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "start_new_session": True,
                "close_fds": True,
            },
        )
    ]
    assert thread_names == ["opc-simulator-log-reader", "opc-simulator-waiter"]
    assert re.fullmatch(r"[0-9a-f]{32}", status["run_id"])
    assert status == {
        "state": "running",
        "pid": 321,
        "file_name": "demo.json",
        "revision": saved["revision"],
        "run_id": status["run_id"],
        "opc_url": DEFAULT_URL,
        "started_at": 50.0,
        "ended_at": None,
        "ended_monotonic": None,
        "elapsed_seconds": 0.0,
        "return_code": None,
        "restore_status": "not_started",
        "last_error": None,
        "recent_logs": [],
    }


def test_nondefault_url_requires_explicit_confirmation_before_spawn(tmp_path):
    saved = save_profile(
        "custom.json",
        _profile(url="opc.tcp://remote.example:4840"),
        tmp_path,
    )
    calls = []
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(InvalidSimulatorConfig, match="需确认"):
        manager.start("custom.json", saved["revision"], allow_unsafe_url=False)

    assert calls == []


def test_confirmed_nondefault_url_appends_only_fixed_cli_flag(tmp_path):
    saved = save_profile(
        "custom.json",
        _profile(url="opc.tcp://remote.example:4840"),
        tmp_path,
    )
    process = FakeProcess()
    calls = []
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda argv, **kwargs: calls.append((argv, kwargs)) or process,
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
    )

    manager.start("custom.json", saved["revision"], allow_unsafe_url=True)

    argv, kwargs = calls[0]
    assert argv[2:4] == ["-m", SIMULATOR_MODULE]
    assert argv[-1] == "--allow-unsafe-url"
    assert argv.count("--allow-unsafe-url") == 1
    parsed = simulator.build_parser().parse_args(argv[argv.index("--config"):])
    assert parsed.config == str((tmp_path / "custom.json").resolve())
    assert parsed.expected_revision == saved["revision"]
    assert parsed.allow_unsafe_url is True
    assert kwargs["cwd"] == str(Path(__file__).resolve().parents[2])
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0] == kwargs["cwd"]


def test_spawn_uses_repo_root_module_and_pythonpath(tmp_path):
    saved = _saved_profile(tmp_path)
    calls = []
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda argv, **kwargs: calls.append((argv, kwargs)) or FakeProcess(),
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
    )

    manager.start("demo.json", saved["revision"])

    argv, kwargs = calls[0]
    assert "-m" in argv
    assert SIMULATOR_MODULE in argv
    repo_root = str(Path(__file__).resolve().parents[2])
    assert kwargs["cwd"] == repo_root
    assert repo_root in kwargs["env"]["PYTHONPATH"]


def test_revision_and_config_validation_happen_before_spawn(tmp_path):
    runnable = _saved_profile(tmp_path)
    calls = []
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(SimulatorRevisionConflict):
        manager.start("demo.json", "0" * 64)

    draft = save_profile(
        "draft.json",
        _profile("draft"),
        tmp_path,
        expected_revision=None,
    )
    with pytest.raises(InvalidSimulatorConfig):
        manager.start("draft.json", draft["revision"])

    assert runnable["revision"] != draft["revision"]
    assert calls == []


def test_spawn_failure_is_failed_and_does_not_reuse_previous_process(tmp_path):
    saved = _saved_profile(tmp_path)
    previous = FakeProcess(return_code=0)
    replacement = FakeProcess(pid=999)
    outcomes = [previous, OSError("secret command path"), replacement]

    def process_factory(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=process_factory,
    )
    manager.start("demo.json", saved["revision"])
    _wait_for(lambda: manager.status()["state"] == "stopped")

    with pytest.raises(SimulatorSpawnError):
        manager.start("demo.json", saved["revision"])

    failed = manager.status()
    assert failed["state"] == "failed"
    assert failed["pid"] is None
    assert failed["restore_status"] == "not_started"
    assert failed["last_error"] == "启动模拟器进程失败"
    assert "secret" not in str(failed)
    assert manager.start("demo.json", saved["revision"])["pid"] == 999


def test_concurrent_start_calls_create_only_one_process(tmp_path):
    saved = _saved_profile(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    process = FakeProcess()
    calls = 0

    def process_factory(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(1)
        return process

    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=process_factory,
    )
    outcomes = []

    def start():
        try:
            outcomes.append(manager.start("demo.json", saved["revision"]))
        except Exception as exc:
            outcomes.append(exc)

    first = threading.Thread(target=start)
    second = threading.Thread(target=start)
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(1)
    second.join(1)

    assert calls == 1
    assert sum(isinstance(value, SimulatorAlreadyRunning) for value in outcomes) == 1
    assert sum(isinstance(value, dict) for value in outcomes) == 1


def test_waiter_polls_under_lock_without_blocking_wait(tmp_path):
    saved = _saved_profile(tmp_path)
    process = FakeProcess()
    targets = []
    waits = []

    def thread_factory(*, target, **kwargs):
        targets.append((kwargs["name"], target))
        return DeferredThread(target, **kwargs)

    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        thread_factory=thread_factory,
        poll_wait=lambda interval: waits.append(interval),
    )
    manager.start("demo.json", saved["revision"])
    original_poll = process.poll

    def finish_on_second_waiter_poll():
        if waits:
            process.finish(0)
        return original_poll()

    process.poll = finish_on_second_waiter_poll
    waiter = dict(targets)["opc-simulator-waiter"]

    waiter()

    assert process.wait_calls == []
    assert waits
    assert manager.status()["state"] == "stopped"


def test_stop_processlookup_refreshes_exit_without_fallback_signal(
    tmp_path, monkeypatch
):
    saved = _saved_profile(tmp_path)
    process = FakeProcess()
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
    )
    manager.start("demo.json", saved["revision"])

    def exit_before_group_signal(pid, signum):
        assert (pid, signum) == (321, signal.SIGTERM)
        process.finish(0)
        raise ProcessLookupError

    monkeypatch.setattr(
        process_manager_module.os,
        "killpg",
        exit_before_group_signal,
    )

    status = manager.stop(timeout=1)

    assert status["state"] == "stopped"
    assert status["restore_status"] == "succeeded"
    assert process.signals == []


@pytest.mark.parametrize("failed_thread_number", [1, 2])
def test_thread_start_failure_closes_pipe_signals_and_reaps_child(
    tmp_path, monkeypatch, failed_thread_number
):
    saved = _saved_profile(tmp_path)
    process = FakeProcess(complete_on_signal=True)
    starts = 0

    class Thread:
        def start(self):
            nonlocal starts
            starts += 1
            if starts == failed_thread_number:
                raise RuntimeError("thread unavailable")

    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        thread_factory=lambda **_kwargs: Thread(),
    )
    monkeypatch.setattr(
        process_manager_module.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    with pytest.raises(SimulatorSpawnError):
        manager.start("demo.json", saved["revision"])

    status = manager.status()
    assert process.stdout.closed
    assert process.signals == [signal.SIGTERM]
    assert process.wait_calls
    assert status["state"] == "failed"
    assert status["pid"] == 321
    assert status["return_code"] == 0


def test_thread_start_failure_keeps_unreaped_live_child_visible(
    tmp_path, monkeypatch
):
    saved = _saved_profile(tmp_path)
    process = FakeProcess()

    class BrokenThread:
        def start(self):
            raise RuntimeError("thread unavailable")

    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        thread_factory=lambda **_kwargs: BrokenThread(),
    )
    monkeypatch.setattr(process_manager_module.os, "killpg", lambda *_args: None)

    with pytest.raises(SimulatorSpawnError):
        manager.start("demo.json", saved["revision"])

    status = manager.status()
    assert process.stdout.closed
    assert status["state"] == "failed"
    assert status["pid"] == 321
    assert status["return_code"] is None
    with pytest.raises(SimulatorAlreadyRunning):
        manager.start("demo.json", saved["revision"])


@pytest.mark.parametrize(
    ("return_code", "state", "restore_status"),
    [(0, "stopped", "succeeded"), (7, "failed", "not_started")],
)
def test_waiter_records_exit_and_allows_restart(
    tmp_path, return_code, state, restore_status
):
    saved = _saved_profile(tmp_path)
    processes = [
        FakeProcess(return_code=return_code),
        FakeProcess(pid=322),
    ]
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: processes.pop(0),
    )

    manager.start("demo.json", saved["revision"])
    _wait_for(lambda: manager.status()["return_code"] is not None)

    status = manager.status()
    assert status["state"] == state
    assert status["return_code"] == return_code
    assert status["restore_status"] == restore_status
    assert manager.start("demo.json", saved["revision"])["pid"] == 322


def test_start_returns_unpredictable_run_identity_and_restart_rotates_it(tmp_path):
    saved = _saved_profile(tmp_path)
    processes = [FakeProcess(), FakeProcess(pid=322)]
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: processes.pop(0),
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
    )

    first = manager.start("demo.json", saved["revision"])
    assert re.fullmatch(r"[0-9a-f]{32}", first["run_id"])
    manager._process.finish(0)
    assert manager.status()["run_id"] == first["run_id"]

    second = manager.start("demo.json", saved["revision"])
    assert re.fullmatch(r"[0-9a-f]{32}", second["run_id"])
    assert second["run_id"] != first["run_id"]


def test_stop_rejects_mismatched_active_run_identity_without_signal(
    tmp_path, monkeypatch
):
    saved = _saved_profile(tmp_path)
    process = FakeProcess()
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
    )
    started = manager.start("demo.json", saved["revision"])
    signals = []
    monkeypatch.setattr(
        process_manager_module.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )

    with pytest.raises(process_manager_module.SimulatorRunIdentityConflict):
        manager.stop(expected_run_id="0" * 32, timeout=1)

    assert started["run_id"] != "0" * 32
    assert signals == []
    assert process.signals == []
    assert manager.status()["state"] == "running"


def test_stale_stop_waiting_behind_restart_cannot_signal_new_run(
    tmp_path, monkeypatch
):
    saved = _saved_profile(tmp_path)
    first_process = FakeProcess()
    second_process = FakeProcess(pid=322)
    restart_entered = threading.Event()
    restart_release = threading.Event()
    process_calls = 0

    def process_factory(*_args, **_kwargs):
        nonlocal process_calls
        process_calls += 1
        if process_calls == 1:
            return first_process
        restart_entered.set()
        assert restart_release.wait(1)
        return second_process

    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=process_factory,
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
    )
    stale_run_id = manager.start("demo.json", saved["revision"])["run_id"]
    first_process.finish(0)
    assert manager.status()["state"] == "stopped"
    signals = []
    monkeypatch.setattr(
        process_manager_module.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )
    outcomes = []
    restart = threading.Thread(
        target=lambda: outcomes.append(
            manager.start("demo.json", saved["revision"])
        )
    )

    def stale_stop():
        try:
            manager.stop(expected_run_id=stale_run_id, timeout=1)
        except Exception as exc:
            outcomes.append(exc)

    stop = threading.Thread(target=stale_stop)
    restart.start()
    assert restart_entered.wait(1)
    stop.start()
    restart_release.set()
    restart.join(1)
    stop.join(1)

    restarted = next(item for item in outcomes if isinstance(item, dict))
    assert restarted["run_id"] != stale_run_id
    assert any(
        isinstance(item, process_manager_module.SimulatorRunIdentityConflict)
        for item in outcomes
    )
    assert signals == []
    assert second_process.signals == []
    assert manager.status()["run_id"] == restarted["run_id"]


def test_log_reader_bounds_lines_count_and_total_bytes(tmp_path):
    saved = _saved_profile(tmp_path)
    process = FakeProcess(
        logs=[f"{index}:" + ("界" * 5000) + "\n" for index in range(250)]
    )
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
    )

    manager.start("demo.json", saved["revision"])
    _wait_for(
        lambda: any(
            line.startswith("249:") for line in manager.status()["recent_logs"]
        )
    )
    logs = manager.status()["recent_logs"]

    assert len(logs) <= 200
    assert all(len(line.encode("utf-8")) <= 4096 for line in logs)
    assert sum(len(line.encode("utf-8")) for line in logs) <= 256 * 1024


def test_log_reader_never_requests_an_unbounded_line(tmp_path):
    saved = _saved_profile(tmp_path)
    process = FakeProcess()
    stream = BoundedReadlineStream(("x" * 100_000) + "\nlast\n")
    process.stdout = stream
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
    )

    manager.start("demo.json", saved["revision"])
    _wait_for(lambda: manager.status()["recent_logs"] == ["x" * 4096, "last"])

    assert stream.sizes
    assert all(0 < size <= 4098 for size in stream.sizes)


@pytest.mark.parametrize("second_operation", ["stop", "shutdown"])
def test_lifecycle_operations_serialize_signal_wait_finalize_without_pid_reuse(
    tmp_path, monkeypatch, second_operation
):
    saved = _saved_profile(tmp_path)
    process = GatedWaitProcess()
    signals = []
    pid_owner = ["old-child"]
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
        shutdown_waiter=lambda candidate, timeout=None: candidate.wait(
            timeout=timeout
        ),
        shutdown_timeout=1,
    )
    manager.start("demo.json", saved["revision"])

    def signal_group(pid, signum):
        signals.append((pid_owner[0], pid, signum))
        if signum == signal.SIGTERM:
            process.finish(0)

    process.after_wait_release = lambda: pid_owner.__setitem__(
        0, "reused-unrelated-process"
    )
    monkeypatch.setattr(process_manager_module.os, "killpg", signal_group)
    results = []
    first = threading.Thread(
        target=lambda: results.append(manager.stop(timeout=1))
    )
    second_target = (
        (lambda: results.append(manager.stop(timeout=1)))
        if second_operation == "stop"
        else (lambda: results.append(manager.shutdown()))
    )
    second = threading.Thread(target=second_target)

    first.start()
    assert process.wait_entered.wait(1)
    second.start()
    time.sleep(0.02)
    assert signals == [("old-child", 321, signal.SIGTERM)]
    process.wait_release.set()
    first.join(1)
    second.join(1)

    assert not first.is_alive() and not second.is_alive()
    assert signals == [("old-child", 321, signal.SIGTERM)]
    assert [result["state"] for result in results] == ["stopped", "stopped"]


def test_stop_uses_process_group_then_fake_compatible_fallback(tmp_path, monkeypatch):
    saved = _saved_profile(tmp_path)
    process = FakeProcess(complete_on_signal=True)
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
    )
    manager.start("demo.json", saved["revision"])

    killpg_calls = []

    def unavailable_killpg(pid, signum):
        killpg_calls.append((pid, signum))
        raise ProcessLookupError

    monkeypatch.setattr("scripts.opc_simulator_process_manager.os.killpg", unavailable_killpg)
    result = manager.stop(timeout=1)

    assert killpg_calls == [(321, signal.SIGTERM)]
    assert process.signals == [signal.SIGTERM]
    assert result["state"] == "stopped"
    assert result["restore_status"] == "succeeded"
    assert manager.stop()["state"] == "stopped"


def test_stop_timeout_never_kills_and_waiter_can_later_complete(tmp_path, monkeypatch):
    saved = _saved_profile(tmp_path)
    process = FakeProcess()
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
    )
    manager.start("demo.json", saved["revision"])
    monkeypatch.setattr(
        "scripts.opc_simulator_process_manager.os.killpg",
        lambda *_args: None,
    )

    with pytest.raises(StopTimeout) as caught:
        manager.stop(timeout=1)

    assert caught.value.status["state"] in {"running", "stopping"}
    assert caught.value.status["restore_status"] in {"pending", "uncertain"}
    assert process.signals == []
    assert process.kill_calls == 0
    process.finish(0)
    _wait_for(lambda: manager.status()["state"] == "stopped")
    assert manager.status()["restore_status"] == "succeeded"


def test_shutdown_waits_for_natural_restore_without_force_kill(
    tmp_path, monkeypatch
):
    saved = _saved_profile(tmp_path)
    process = FakeProcess()
    waiter_calls = []

    def shutdown_waiter(candidate, timeout):
        waiter_calls.append((candidate, timeout))
        assert killpg_calls == [(321, signal.SIGTERM)]
        candidate.finish(0)
        return candidate.poll()

    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
        shutdown_waiter=shutdown_waiter,
        shutdown_timeout=12,
    )
    manager.start("demo.json", saved["revision"])
    killpg_calls = []
    monkeypatch.setattr(
        process_manager_module.os,
        "killpg",
        lambda pid, signum: killpg_calls.append((pid, signum)),
    )

    status = manager.shutdown()

    assert waiter_calls == [(process, 12.0)]
    assert status["state"] == "stopped"
    assert status["restore_status"] == "succeeded"
    assert process.signals == []


@pytest.mark.parametrize("fallback", [False, True])
def test_shutdown_timeout_uses_sigkill_then_reaps_as_uncertain(
    tmp_path, monkeypatch, fallback
):
    saved = _saved_profile(tmp_path)
    process = FakeProcess()
    signals = []
    critical_messages = []
    monkeypatch.setattr(
        process_manager_module.logger,
        "critical",
        lambda message: critical_messages.append(message),
    )

    def timeout_waiter(candidate, timeout):
        assert candidate is process
        assert timeout == 0.25
        raise subprocess.TimeoutExpired("simulator", timeout)

    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
        shutdown_waiter=timeout_waiter,
        shutdown_timeout=0.25,
    )
    manager.start("demo.json", saved["revision"])

    def signal_group(pid, signum):
        signals.append((pid, signum))
        if signum == signal.SIGKILL:
            if fallback:
                raise PermissionError("group kill unavailable")
            process.finish(-signal.SIGKILL)

    monkeypatch.setattr(process_manager_module.os, "killpg", signal_group)

    status = manager.shutdown()

    assert signals == [
        (321, signal.SIGTERM),
        (321, signal.SIGKILL),
    ]
    assert process.kill_calls == int(fallback)
    assert process.wait_calls[-1] is None
    assert status["state"] == "failed"
    assert status["restore_status"] == "uncertain"
    assert status["return_code"] == -signal.SIGKILL
    assert "CRITICAL" in status["last_error"]
    assert "恢复结果不确定" in status["last_error"]
    assert critical_messages == [
        "CRITICAL: workflow UI shutdown 超时，执行 SIGKILL；恢复结果不确定"
    ]


def test_shutdown_kill_failure_records_uncertain_without_claiming_restore(
    tmp_path, monkeypatch
):
    saved = _saved_profile(tmp_path)
    process = FakeProcess(complete_on_kill=False)
    process.kill = lambda: (_ for _ in ()).throw(PermissionError("denied"))
    critical_messages = []
    monkeypatch.setattr(
        process_manager_module.logger,
        "critical",
        lambda message: critical_messages.append(message),
    )
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
        shutdown_waiter=lambda _process, timeout: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("simulator", timeout)
        ),
        shutdown_timeout=0.1,
    )
    manager.start("demo.json", saved["revision"])

    def fail_group_kill(_pid, signum):
        if signum == signal.SIGKILL:
            raise PermissionError("denied")

    monkeypatch.setattr(
        process_manager_module.os,
        "killpg",
        fail_group_kill,
    )

    status = manager.shutdown()

    assert status["state"] == "failed"
    assert status["restore_status"] == "uncertain"
    assert status["return_code"] is None
    assert "SIGKILL 失败" in status["last_error"]
    assert "恢复成功" not in status["last_error"]
    assert critical_messages == [
        "CRITICAL: workflow UI shutdown 超时，执行 SIGKILL；恢复结果不确定",
        status["last_error"],
    ]


def test_terminal_elapsed_freezes_and_restart_resets_end_fields(tmp_path):
    saved = _saved_profile(tmp_path)
    now = [10.0]
    processes = [FakeProcess(), FakeProcess(pid=322)]
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: processes.pop(0),
        clock=lambda: now[0],
        thread_factory=lambda target, **kwargs: DeferredThread(target, **kwargs),
    )

    manager.start("demo.json", saved["revision"])
    now[0] = 12.0
    assert manager.status()["elapsed_seconds"] == 2.0
    manager._process.finish(0)
    now[0] = 15.0
    stopped = manager.status()
    now[0] = 100.0

    assert stopped["ended_at"] == 15.0
    assert stopped["ended_monotonic"] == 15.0
    assert manager.status()["elapsed_seconds"] == 5.0

    now[0] = 200.0
    restarted = manager.start("demo.json", saved["revision"])
    assert restarted["ended_at"] is None
    assert restarted["ended_monotonic"] is None
    assert restarted["elapsed_seconds"] == 0.0


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (FileNotFoundError(), SimulatorConfigNotFound),
        (PermissionError(), SimulatorStorageError),
        (OSError(errno.ENOSPC, "full"), SimulatorStorageFull),
        (
            json.JSONDecodeError("bad", "x", 0),
            InvalidSimulatorConfig,
        ),
    ],
)
def test_start_classifies_profile_read_failures(
    tmp_path, monkeypatch, error, expected_type
):
    manager = OpcSimulatorProcessManager(config_dir=tmp_path)
    monkeypatch.setattr(
        process_manager_module,
        "read_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(expected_type):
        manager.start("demo.json", "0" * 64)


@pytest.mark.parametrize(
    "revision",
    ["", "0" * 63, "0" * 65, "A" * 64, "g" * 64],
)
def test_start_rejects_invalid_revision_format_before_profile_read(
    tmp_path, monkeypatch, revision
):
    read_calls = []
    monkeypatch.setattr(
        process_manager_module,
        "read_profile",
        lambda *_args, **_kwargs: read_calls.append(True),
    )
    manager = OpcSimulatorProcessManager(config_dir=tmp_path)

    with pytest.raises(InvalidSimulatorRevision):
        manager.start("demo.json", revision)

    assert read_calls == []


def test_status_redacts_url_credentials_paths_and_query_from_logs(tmp_path):
    profile = save_profile(
        "demo.json",
        _profile(),
        tmp_path,
    )
    raw_url = (
        "opc.tcp://alice:secret@localhost:4840/plant"
        "?token=private#fragment"
    )
    process = FakeProcess(
        logs=[
            f"加载 {tmp_path.resolve()}/demo.json\n",
            f"用户目录 {Path.home()}/secret.txt\n",
            f"连接 {raw_url}\n",
            "回调 https://bob:pass@example.com/hook?token=web#fragment\n",
            "变量 done=True，动作完成\n",
        ]
    )
    manager = OpcSimulatorProcessManager(
        config_dir=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
    )

    manager.start("demo.json", profile["revision"])
    _wait_for(lambda: len(manager.status()["recent_logs"]) == 5)
    status = manager.status()
    rendered = "\n".join(status["recent_logs"])

    assert status["opc_url"] == DEFAULT_URL
    assert "alice" not in str(status)
    assert "alice:secret" not in rendered
    assert "bob:pass" not in rendered
    assert "token" not in rendered
    assert "private" not in rendered
    assert str(tmp_path.resolve()) not in rendered
    assert str(Path.home()) not in rendered
    assert "变量 done=True，动作完成" in rendered


@pytest.mark.parametrize("timeout", [True, 0, 0.9, 61, float("nan")])
def test_stop_rejects_timeout_outside_fixed_bounds(tmp_path, timeout):
    manager = OpcSimulatorProcessManager(config_dir=tmp_path)

    with pytest.raises(ValueError, match="timeout"):
        manager.stop(timeout=timeout)
