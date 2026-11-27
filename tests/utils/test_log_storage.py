from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from unilabos.utils.log_storage import (
    LogPolicy,
    RotatingByteWriter,
    SessionRotatingFileHandler,
    cleanup_logs,
    cleanup_root_for,
    read_log_tail,
    unique_log_path,
)


def finish(writer: RotatingByteWriter) -> None:
    service = writer._session.service
    writer.close()
    if service.stop:
        service.thread.join(timeout=3)
        assert not service.thread.is_alive()


def stored(path: Path, content: bytes, root: Path | None = None) -> None:
    writer = RotatingByteWriter(path, cleanup_root=root)
    writer.write(content)
    finish(writer)


@pytest.mark.parametrize("field,value", [
    ("max_bytes", 0), ("max_bytes", 1.5), ("backup_count", -1),
    ("backup_count", True), ("retention_days", 0), ("retention_days", float("nan")),
    ("total_max_bytes", -1), ("cleanup_interval_seconds", float("inf")),
])
def test_invalid_limits_cannot_disable_rotation(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        LogPolicy(**{field: value})


def test_config_and_environment_share_validated_policy() -> None:
    class Config:
        log_max_bytes = 4096
        log_backup_count = 3

    assert LogPolicy.from_config(Config).max_bytes == 4096
    assert LogPolicy.from_env({"UNILABOS_BASICCONFIG_LOG_BACKUP_COUNT": "3"}).backup_count == 3
    assert LogPolicy().total_max_bytes == 2 * 1024**3
    with pytest.raises(ValueError):
        LogPolicy.from_env({"UNILABOS_BASICCONFIG_LOG_MAX_BYTES": "unlimited"})


def test_same_second_files_are_unique_and_workspace_is_shared(tmp_path: Path) -> None:
    paths = {unique_log_path(tmp_path) for _ in range(50)}
    assert len(paths) == 50
    root = tmp_path / ".unilabos"
    assert cleanup_root_for(root / "runtime/workbench/edge/generation/logs") == root
    assert cleanup_root_for(tmp_path / "custom") == tmp_path / "custom"


def test_unbroken_binary_output_is_bounded_and_tail_spans_backups(tmp_path: Path) -> None:
    path = tmp_path / "device.log"
    policy = LogPolicy(max_bytes=64, backup_count=3)
    content = bytes(range(256)) * 3 + b"final"
    writer = RotatingByteWriter(path, policy)
    writer.write(content)
    finish(writer)
    files = list(tmp_path.glob("device.log*"))
    assert len(files) == 4
    assert all(item.stat().st_size <= 64 for item in files)
    assert read_log_tail(path, 150) == content[-150:]
    assert read_log_tail(path, 0) == b""


def test_python_handler_rotates_and_keeps_exception_and_unicode(tmp_path: Path) -> None:
    path = tmp_path / "main.log"
    handler = SessionRotatingFileHandler(path, LogPolicy(max_bytes=200, backup_count=2))
    logger = logging.getLogger("test.diagnostic.handler")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        for index in range(15):
            logger.info("状态 %s %s", index, "x" * 30)
        try:
            raise RuntimeError("设备断开")
        except RuntimeError:
            logger.exception("读取失败")
    finally:
        logger.removeHandler(handler)
        handler.close()
    text = read_log_tail(path, 2048).decode("utf-8")
    assert "Traceback" in text and "设备断开" in text and "读取失败" in text
    assert len(list(tmp_path.glob("main.log*"))) == 3


def test_same_stream_cannot_have_two_writers_and_close_releases_lock(tmp_path: Path) -> None:
    path = tmp_path / "device.log"
    first = RotatingByteWriter(path)
    with pytest.raises(OSError):
        RotatingByteWriter(path)
    first.write(b"first")
    finish(first)
    second = RotatingByteWriter(path)
    second.write(b"second")
    finish(second)
    assert path.read_bytes() == b"firstsecond"


def test_closed_handler_does_not_reopen_unlocked_file(tmp_path: Path) -> None:
    path = tmp_path / "closed.log"
    handler = SessionRotatingFileHandler(path)
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "error", (), None)
    handler.handle(record)
    handler.close()
    before = path.read_bytes()
    handler.handle(record)
    assert handler.stream is None
    assert path.read_bytes() == before


def test_writer_rejects_symlinked_log_directory(tmp_path: Path) -> None:
    root = tmp_path / ".unilabos"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / "logs").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="软链接"):
        RotatingByteWriter(root / "logs/device.log")
    assert list(external.iterdir()) == []


def test_old_unmarked_files_are_not_adopted_or_deleted(tmp_path: Path) -> None:
    path = tmp_path / "legacy.log"
    path.write_text("仍可能有旧版进程写入")
    with pytest.raises(ValueError, match="旧版日志"):
        RotatingByteWriter(path)
    assert cleanup_logs(tmp_path, LogPolicy(total_max_bytes=1), now=time.time() + 999999) == []
    assert path.exists()


def test_cleanup_counts_nested_live_streams_but_only_deletes_finished(tmp_path: Path) -> None:
    root = tmp_path / ".unilabos"
    old = root / "logs/workbench/old-backend.log"
    stored(old, b"o" * 200, root)
    active_path = root / "runtime/workbench/edge/run/logs/native.log"
    active = RotatingByteWriter(active_path, LogPolicy(max_bytes=64, backup_count=3), root)
    active.write(b"a" * 190)
    try:
        removed = cleanup_logs(root, LogPolicy(total_max_bytes=250), blocking=True)
        assert old in removed
        assert active_path.exists()
        assert len(list(active_path.parent.glob("native.log*"))) == 3
        assert read_log_tail(active_path, 190) == b"a" * 190
    finally:
        finish(active)


def test_retention_and_size_remove_oldest_files_first(tmp_path: Path) -> None:
    paths = [tmp_path / f"run{index}.log" for index in range(3)]
    now = time.time()
    for index, path in enumerate(paths):
        stored(path, b"x" * 80)
        os.utime(path, (now - 300 + index * 100, now - 300 + index * 100))
    removed = cleanup_logs(tmp_path, LogPolicy(total_max_bytes=160), now=now)
    assert removed == [paths[0]]
    assert paths[1].exists() and paths[2].exists()
    removed = cleanup_logs(tmp_path, LogPolicy(retention_days=1), now=now + 2 * 86400)
    assert removed == paths[1:]
    assert not list(tmp_path.glob("*.session.lock"))


def test_cleanup_leaves_business_files_symlinks_and_invalid_markers(tmp_path: Path) -> None:
    old = tmp_path / "old.log"
    stored(old, b"old")
    business = [tmp_path / "audit.jsonl", tmp_path / "experiment.db", tmp_path / "operation.json", tmp_path / "legacy.log"]
    for path in business:
        path.write_bytes(b"preserve")
    outside = tmp_path.parent / f"outside-{tmp_path.name}.log"
    outside.write_bytes(b"outside")
    link = tmp_path / "linked.log"
    link.symlink_to(outside)
    (tmp_path / ".linked.log.session.lock").write_text(json.dumps({"format": "unilab-diagnostic-v1", "file": "linked.log"}))
    nested = tmp_path / "elsewhere"
    nested.symlink_to(tmp_path.parent, target_is_directory=True)
    (tmp_path / ".legacy.log.session.lock").write_text('{"format":"other","file":"legacy.log"}')
    try:
        with pytest.raises(ValueError, match="标记无效"):
            RotatingByteWriter(business[-1])
        assert cleanup_logs(tmp_path, LogPolicy(total_max_bytes=1), now=time.time() + 999999) == [old]
        assert all(path.read_bytes() == b"preserve" for path in business)
        assert outside.read_bytes() == b"outside" and link.is_symlink()
        assert nested.is_symlink()
    finally:
        outside.unlink()


def test_process_crash_releases_lock_for_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "crashed.log"
    script = """
import os, sys
from unilabos.utils.log_storage import RotatingByteWriter
w = RotatingByteWriter(sys.argv[1])
w.write(b'last output')
os._exit(0)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert path.exists()
    assert cleanup_logs(tmp_path, LogPolicy(total_max_bytes=1)) == [path]


def test_idle_live_writer_triggers_periodic_history_cleanup(tmp_path: Path) -> None:
    old = tmp_path / "old.log"
    stored(old, b"old")
    active = RotatingByteWriter(tmp_path / "active.log", LogPolicy(cleanup_interval_seconds=0.05))
    try:
        # 启动之后才让旧文件过期，验证无需新日志或轮转也会定期清理。
        os.utime(old, (1, 1))
        deadline = time.monotonic() + 3
        while old.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not old.exists()
        assert active.path.exists()
    finally:
        finish(active)
