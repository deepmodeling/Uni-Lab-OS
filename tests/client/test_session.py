"""客户端会话文件锁的跨平台回归。"""

from __future__ import annotations

import multiprocessing
import queue
from pathlib import Path
from typing import Any

from unilabos.client.session import SessionManager


def _acquire_session(working_dir: str, result: Any) -> None:
    with SessionManager(working_dir=working_dir):
        result.put("acquired")


def test_session_manager_roundtrip(tmp_path: Path) -> None:
    with SessionManager(working_dir=str(tmp_path)) as manager:
        state = manager.get_state()
        state.auth.ak = "test-ak"
        state.auth.sk = "test-sk"

    with SessionManager(working_dir=str(tmp_path)) as manager:
        assert manager.get_state().auth.ak == "test-ak"
        assert manager.get_state().auth.sk == "test-sk"


def test_session_lock_serializes_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    process = context.Process(
        target=_acquire_session,
        args=(str(tmp_path), result),
    )

    with SessionManager(working_dir=str(tmp_path)):
        process.start()
        try:
            result.get(timeout=0.3)
        except queue.Empty:
            pass
        else:
            raise AssertionError("子进程不应在父进程释放文件锁前进入会话")

    assert result.get(timeout=5) == "acquired"
    process.join(timeout=5)
    assert process.exitcode == 0
