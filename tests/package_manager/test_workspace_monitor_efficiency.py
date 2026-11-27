"""低空闲开销与内容级热重载必须同时成立。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from unilabos.package_manager.workspace_runtime import monitor as module
from unilabos.package_manager.workspace_runtime.monitor import StableWorkspaceFileMonitor


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "device.py").write_text("VALUE=1\n")
    return root


def test_capture_never_descends_ignored_runtime_or_explicit_state(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    for name in [".unilabos", ".git", "state"]:
        path = root / name / "deep" / "logs"
        path.mkdir(parents=True)
        (path / "runtime.log").write_text("ignored")
    visited = []
    real = os.scandir

    def record(path):
        visited.append(Path(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", record)
    monitor = StableWorkspaceFileMonitor(root, graph_argument="graph.json", ignored_paths=(root / "state",))
    monitor.capture()
    assert visited
    assert not any(p != root for p in visited), visited


def test_idle_and_ignored_writes_do_not_reread_source(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    logs = root / ".unilabos" / "logs"
    logs.mkdir(parents=True)
    calls = []
    real = Path.read_bytes

    def record(path):
        if path == root / "device.py":
            calls.append(time.monotonic())
        return real(path)

    monkeypatch.setattr(Path, "read_bytes", record)
    monitor = StableWorkspaceFileMonitor(root, graph_argument="graph.json", interval_seconds=0.02)
    monitor.capture()
    monitor.start(lambda _: pytest.fail("ignored writes must not refresh"))
    try:
        time.sleep(0.3)  # 原生事件订阅建立并完成启动后的再次核对。
        before = len(calls)
        for i in range(12):
            (logs / "runtime.log").write_text(str(i))
            time.sleep(0.02)
        assert len(calls) == before
    finally:
        monitor.close()


def test_content_change_with_same_size_and_mtime_still_reloads(tmp_path):
    root = workspace(tmp_path)
    path = root / "device.py"
    original = path.stat()
    monitor = StableWorkspaceFileMonitor(root, graph_argument="graph.json", interval_seconds=0.01, settle_seconds=0.03)
    before = monitor.capture().identity
    submitted = []
    ready = threading.Event()

    def submit(generation):
        submitted.append(generation.identity)
        ready.set()
        return SimpleNamespace(outcome="hot_published")

    monitor.start(submit)
    try:
        path.write_text("VALUE=2\n")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert ready.wait(2)
        assert submitted[-1] != before
    finally:
        monitor.close()


def test_new_directory_atomic_save_and_deletion_remain_observable(tmp_path):
    root = workspace(tmp_path)
    monitor = StableWorkspaceFileMonitor(root, graph_argument="graph.json", interval_seconds=0.01, settle_seconds=0.03)
    monitor.capture()
    received = threading.Event()
    identities = []

    def submit(generation):
        identities.append(generation.identity)
        received.set()
        return SimpleNamespace(outcome="hot_published")

    monitor.start(submit)
    try:
        directory = root / "new" / "nested"
        directory.mkdir(parents=True)
        target = directory / "driver.py"
        target.write_text("VALUE=1\n")
        assert received.wait(2)
        time.sleep(0.1)
        received.clear()
        temporary = directory / "driver.py.tmp"
        temporary.write_text("VALUE=2\n")
        temporary.replace(target)
        assert received.wait(2)
        time.sleep(0.1)
        received.clear()
        target.unlink()
        assert received.wait(2)
        assert len(set(identities)) >= 3
    finally:
        monitor.close()


def test_native_watcher_failure_preserves_hot_reload(tmp_path, monkeypatch):
    root = workspace(tmp_path)

    def unavailable(*paths, **kwargs):
        raise OSError("native observer unavailable")

    monkeypatch.setattr(module, "watch", unavailable, raising=False)
    monitor = StableWorkspaceFileMonitor(root, graph_argument="graph.json", interval_seconds=0.01, settle_seconds=0.02)
    monitor.capture()
    ready = threading.Event()

    def submit(_):
        ready.set()
        return SimpleNamespace(outcome="hot_published")

    monitor.start(submit)
    try:
        (root / "device.py").write_text("VALUE=2\n")
        assert ready.wait(2)
    finally:
        monitor.close()


def test_periodic_content_reconciliation_recovers_a_lost_event(tmp_path, monkeypatch):
    root = workspace(tmp_path)

    def silent(*paths, stop_event, **kwargs):
        while not stop_event.wait(0.01):
            yield set()

    monkeypatch.setattr(module, "watch", silent, raising=False)
    monitor = StableWorkspaceFileMonitor(root, graph_argument="graph.json", interval_seconds=0.01,
                                        settle_seconds=0.02, reconcile_seconds=0.08)
    monitor.capture()
    ready = threading.Event()
    monitor.start(lambda _: (ready.set(), SimpleNamespace(outcome="hot_published"))[1])
    try:
        time.sleep(0.05)
        (root / "device.py").write_text("VALUE=2\n")
        assert ready.wait(1)
    finally:
        monitor.close()


def test_transient_snapshot_read_error_does_not_lose_the_only_event(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    monitor = StableWorkspaceFileMonitor(root, graph_argument="graph.json", interval_seconds=0.01, settle_seconds=0.02)
    monitor.capture()
    (root / "device.py").write_text("VALUE=2\n")
    real = Path.read_bytes
    failed = []

    def read(path):
        if path == root / "device.py" and not failed:
            failed.append(True)
            raise OSError("editor temporarily replacing the file")
        return real(path)

    def observed(_stop):
        yield True
        while not _stop.wait(0.01):
            yield False

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(monitor, "_observe_changes", observed)
    ready = threading.Event()
    monitor.start(lambda _: (ready.set(), SimpleNamespace(outcome="hot_published"))[1])
    try:
        assert ready.wait(1)
    finally:
        monitor.close()


def test_transient_native_failure_retries_subscription_without_losing_reload(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    native_watch = module.watch
    recovered = threading.Event()
    refreshed = threading.Event()
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("temporary subscription failure")
        recovered.set()
        return native_watch(*args, **kwargs)

    monkeypatch.setattr(module, "watch", fail_once)
    monitor = StableWorkspaceFileMonitor(
        root, graph_argument="graph.json", interval_seconds=0.01,
        settle_seconds=0.03, reconcile_seconds=0.1,
    )
    monitor.capture()
    monitor.start(lambda _: (refreshed.set(), SimpleNamespace(outcome="hot_published"))[1])
    try:
        assert recovered.wait(1), "temporary failure must not leave permanent polling"
        (root / "device.py").write_text("VALUE=2\n")
        assert refreshed.wait(2)
    finally:
        monitor.close()
