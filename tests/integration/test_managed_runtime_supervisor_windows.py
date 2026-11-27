"""Windows 原生进程树回收 smoke test；可由 unittest 与 pytest 直接执行。"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path

from unilabos.managed_runtime.supervisor import ManagedRuntimeSupervisor

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


@unittest.skipUnless(os.name == "nt", "仅在 Windows 验证 taskkill 进程树语义")
class WindowsManagedProcessTreeTest(unittest.TestCase):
    def test_terminate_reaps_parent_and_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid_path = root / "child.pid"
            parent_script = root / "parent.py"
            parent_script.write_text(
                "\n".join(
                    [
                        "import subprocess",
                        "import sys",
                        "import time",
                        "from pathlib import Path",
                        "child = subprocess.Popen([",
                        "    sys.executable,",
                        "    '-c',",
                        "    'import time; time.sleep(120)',",
                        "])",
                        f"Path({str(child_pid_path)!r}).write_text(",
                        "    str(child.pid), encoding='utf-8'",
                        ")",
                        "time.sleep(120)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            parent = subprocess.Popen(
                [sys.executable, str(parent_script)],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid: int | None = None
            try:
                child_pid = _wait_for_pid_file(child_pid_path)
                supervisor = ManagedRuntimeSupervisor(
                    runtime_prefix=root / "runtime-prefix",
                    state_directory=root / "state",
                    token="windows-process-tree-test",
                )
                supervisor._terminate_process_locked(parent)
                self.assertFalse(_process_is_running(parent.pid))
                self.assertFalse(_process_is_running(child_pid))
            finally:
                _force_kill_tree(parent.pid)
                if child_pid is not None:
                    _force_kill_tree(child_pid)


def _wait_for_pid_file(path: Path) -> int:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if path.is_file():
            return int(path.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError("Windows child process did not start")


def _process_is_running(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _force_kill_tree(pid: int) -> None:
    if not _process_is_running(pid):
        return
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    subprocess.run(
        [
            str(Path(system_root) / "System32" / "taskkill.exe"),
            "/PID",
            str(pid),
            "/T",
            "/F",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


if __name__ == "__main__":
    unittest.main()
