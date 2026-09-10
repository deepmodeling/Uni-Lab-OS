"""Windows CLI 会话文件锁与 UTF-8 持久化合同。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_session_module():
    """绕过客户端可选 HTTP 依赖，直接加载当前被测会话模块。"""

    module_path = (
        Path(__file__).parents[2] / "unilabos" / "client" / "session.py"
    )
    spec = importlib.util.spec_from_file_location(
        "unilabos_client_session_windows_test",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


session = _load_session_module()


class RecordingMsvcrt:
    """记录 Windows CRT 字节锁调用。"""

    LK_LOCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, descriptor: int, mode: int, length: int) -> None:
        """记录锁定参数，不改变宿主 Linux 描述符。"""

        self.calls.append((descriptor, mode, length))


def test_session_manager_uses_windows_lock_and_utf8(
    tmp_path,
    monkeypatch,
) -> None:
    """无 ``fcntl`` 平台必须用 ``msvcrt`` 锁并保存任意 Unicode 会话。"""

    windows_lock = RecordingMsvcrt()
    monkeypatch.setattr(session, "_fcntl", None)
    monkeypatch.setattr(session, "_msvcrt", windows_lock)

    with session.SessionManager(working_dir=str(tmp_path)) as manager:
        manager.get_state().auth.user_name = "实验员 🧪"

    assert [call[1:] for call in windows_lock.calls] == [
        (windows_lock.LK_LOCK, 1),
        (windows_lock.LK_UNLCK, 1),
    ]
    assert json.loads(
        (tmp_path / "session.json").read_text(encoding="utf-8")
    )["auth"]["user_name"] == "实验员 🧪"
