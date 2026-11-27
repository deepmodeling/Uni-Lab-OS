"""验证原生 ROS 日志接管参数、接收器失败边界和真正的 fd 2 数据路径。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from unilabos.ros import logging as ros_logging


def test_ros_arguments_keep_remaps_and_explicit_levels() -> None:
    original = ["program", "--ros-args", "-r", "__node:=test", "--log-level", "debug", "--"]
    before = list(original)
    result = ros_logging.ros_logging_args(original)
    assert original == before
    assert result[:len(original)] == original
    assert result.count("--log-level") == 1
    assert result[-2:] == ["--enable-stdout-logs", "--disable-external-lib-logs"]
    assert "--disable-rosout-logs" not in result


def test_ros_node_specific_level_keeps_info_default() -> None:
    result = ros_logging.ros_logging_args(["--ros-args", "--log-level", "sensor:=debug"])
    assert result == [
        "--ros-args", "--log-level", "sensor:=debug", "--", "--ros-args",
        "--log-level", "info", "--enable-stdout-logs", "--disable-external-lib-logs",
    ]
    assert "fatal" in ros_logging.ros_logging_args(log_level="CRITICAL")
    assert "debug" in ros_logging.ros_logging_args(log_level="TRACE")


def test_prepared_workbench_capture_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ros_logging, "_prepared", False)
    monkeypatch.setattr(ros_logging, "is_process_output_captured", lambda: True)
    monkeypatch.setattr(ros_logging, "ProcessOutput", lambda *a, **k: pytest.fail("不能重复创建接收器"))
    monkeypatch.setenv("RCUTILS_LOGGING_USE_STDOUT", "1")
    result = ros_logging.prepare_ros_logging(config=SimpleNamespace(log_level="INFO"))
    assert "--disable-external-lib-logs" in result
    assert os.environ["RCUTILS_LOGGING_USE_STDOUT"] == "0"


def test_existing_external_ros_context_cannot_be_silently_reconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ros_logging, "_prepared", False)
    with pytest.raises(RuntimeError, match="ROS 已在日志容量策略之前初始化"):
        ros_logging.prepare_ros_logging(context_initialized=True)


def test_capture_start_failure_restores_environment_and_stderr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ros_logging, "_prepared", False)
    monkeypatch.setattr(ros_logging, "is_process_output_captured", lambda: False)
    monkeypatch.setenv("RCUTILS_LOGGING_USE_STDOUT", "1")
    stderr_before = os.fstat(2)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("接收器测试故障")

    monkeypatch.setattr(ros_logging, "ProcessOutput", fail)
    with pytest.raises(OSError, match="接收器测试故障"):
        ros_logging.prepare_ros_logging(working_dir=tmp_path)
    stderr_after = os.fstat(2)
    assert (stderr_before.st_dev, stderr_before.st_ino) == (stderr_after.st_dev, stderr_after.st_ino)
    assert os.environ["RCUTILS_LOGGING_USE_STDOUT"] == "1"
    assert not ros_logging._prepared


def test_native_fd_output_is_echoed_rotated_and_drained(tmp_path: Path) -> None:
    code = '''
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unilabos.ros.logging import prepare_ros_logging, close_ros_logging
from unilabos.utils.process_output import is_process_output_captured
root = Path(os.environ["TEST_LOG_ROOT"])
config = SimpleNamespace(log_level="INFO", working_dir=str(root), log_max_bytes=4096, log_backup_count=2)
args = prepare_ros_logging(config=config)
captured_identity = is_process_output_captured()
libc = ctypes.CDLL("ucrtbase") if os.name == "nt" else ctypes.CDLL(None)
native_write = libc._write if os.name == "nt" else libc.write
native_write.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint if os.name == "nt" else ctypes.c_size_t]
native_write.restype = ctypes.c_int if os.name == "nt" else ctypes.c_ssize_t
for index in range(250):
    message = ("[原生 C INFO] %03d " % index + "x" * 240 + "\\n").encode()
    assert native_write(2, message, len(message)) == len(message)
if captured_identity:
    subprocess.run([sys.executable, "-c", "import os; from unilabos.ros.logging import prepare_ros_logging; prepare_ros_logging(working_dir=os.environ['TEST_LOG_ROOT']); os.write(2, b'child-capture-marker' + bytes([10]))"], check=True)
last = b"[native ERROR] final-error-marker\\n"
assert native_write(2, last, len(last)) == len(last)
close_ros_logging()
assert not is_process_output_captured()
files = list((root / "logs").glob("ros_console_*.log*"))
files = [p for p in files if p.name.endswith(".log") or p.suffix[1:].isdigit()]
print(json.dumps({"sizes": [p.stat().st_size for p in files], "tail": any(b"final-error-marker" in p.read_bytes() for p in files), "args": args}))
'''
    env = dict(os.environ, TEST_LOG_ROOT=str(tmp_path))
    env.pop("UNILABOS_PROCESS_OUTPUT_CAPTURED", None)
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr[-4000:].decode(errors="replace")
    observed = json.loads(result.stdout)
    assert observed["sizes"] and len(observed["sizes"]) <= 3
    assert max(observed["sizes"]) <= 4096
    assert observed["tail"]
    assert result.stderr.count("[原生 C INFO]".encode()) == 250
    assert b"[native ERROR] final-error-marker" in result.stderr


@pytest.mark.skipif(importlib.util.find_spec("rclpy") is None, reason="目标 ROS 环境未安装 rclpy")
def test_target_ros_native_output_and_rosout(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "unilabos.ros.logging_acceptance", "--working-dir", str(tmp_path)],
        capture_output=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    observed = json.loads(result.stdout)
    assert observed["native_error_on_rosout"]
    assert observed["native_error_on_disk"]
    assert observed["native_unbounded_files_absent"]


@pytest.mark.parametrize("legacy", [False, True])
def test_doctor_prepares_capture_before_ros_init_and_preserves_args(monkeypatch: pytest.MonkeyPatch, legacy: bool) -> None:
    from unilabos.networking import ros_doctor as doctor
    from unilabos.networking.ros_discovery import RosNetworkInfo

    argv = ["doctor", "--ros-args", "--log-level", "error", "--"]
    events: list[str] = []
    calls: list[dict[str, object]] = []

    def prepare(args: list[str]) -> list[str]:
        events.append("prepare")
        assert args == argv
        return ros_logging.ros_logging_args(args)

    def initialize(**kwargs: object) -> None:
        events.append("init")
        calls.append(kwargs)
        if legacy and "domain_id" in kwargs:
            raise TypeError("旧版不支持 domain_id")

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setitem(sys.modules, "rclpy", SimpleNamespace(ok=lambda: False, init=initialize))
    monkeypatch.setattr(doctor, "apply_ros_network_env", lambda info: {})
    monkeypatch.setattr(ros_logging, "prepare_ros_logging", prepare)
    doctor._setup_ros(RosNetworkInfo(domain_id=72), "test")
    assert events[0] == "prepare" and events.count("prepare") == 1
    assert calls[0]["domain_id"] == 72
    assert "error" in calls[-1]["args"]
    assert "--disable-external-lib-logs" in calls[-1]["args"]
    assert len(calls) == (2 if legacy else 1)


def test_doctor_keeps_existing_ros_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from unilabos.networking import ros_doctor as doctor
    from unilabos.networking.ros_discovery import RosNetworkInfo

    monkeypatch.setitem(sys.modules, "rclpy", SimpleNamespace(ok=lambda: True))
    monkeypatch.setattr(doctor, "apply_ros_network_env", lambda info: {})
    monkeypatch.setattr(ros_logging, "prepare_ros_logging", lambda *a, **kw: pytest.fail("已有 context 应继续复用"))
    doctor._setup_ros(RosNetworkInfo(domain_id=72), "test")
