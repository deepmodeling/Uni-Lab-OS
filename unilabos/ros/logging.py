"""在 ROS 初始化前接管原生控制台输出，避免 spdlog 生成无界日志文件。"""

from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

from unilabos.config.config import BasicConfig
from unilabos.utils.log_storage import LogPolicy, cleanup_root_for, unique_log_path
from unilabos.utils.process_output import ProcessOutput, is_process_output_captured

_lock = threading.RLock()
_output: ProcessOutput | None = None
_original_stderr: int | None = None
_prepared = False
_previous_capture_marker: str | None = None
_previous_stdout: str | None = None
_CAPTURE_MARKER = "UNILABOS_PROCESS_OUTPUT_CAPTURED"
_logger = logging.getLogger(__name__)


def ros_logging_args(args: Sequence[str] | None = None, *, log_level: str = "INFO") -> list[str]:
    """补齐 ROS 参数段，保留显式级别和重映射，强制由受控控制台承接原生日志。"""

    result = list(args or ())
    # 兼容历史调用方直接传入 --log-level 的形式；ROS 原生解析器要求参数段标记。
    if result and "--ros-args" not in result and result[0].startswith("--"):
        result.insert(0, "--ros-args")
    active = False
    has_default_level = False
    for index, arg in enumerate(result):
        if arg == "--ros-args":
            active = True
        elif arg == "--":
            active = False
        elif active and arg == "--log-level" and index + 1 < len(result):
            has_default_level |= ":=" not in result[index + 1]
    level = {"TRACE": "debug", "WARNING": "warn", "CRITICAL": "fatal"}.get(
        log_level.upper(), log_level.lower()
    )
    if level not in {"debug", "info", "warn", "error", "fatal"}:
        raise ValueError(f"无效的 ROS 日志级别：{log_level}")
    if active:
        result.append("--")
    result.append("--ros-args")
    if not has_default_level:
        result.extend(("--log-level", level))
    # 只在受控接收器安装成功后使用此参数；console 与 /rosout 保持原生实现。
    result.extend(("--enable-stdout-logs", "--disable-external-lib-logs"))
    return result


def prepare_ros_logging(
    args: Sequence[str] | None = None,
    *,
    working_dir: str | os.PathLike[str] | None = None,
    config: Any = BasicConfig,
    context_initialized: bool = False,
) -> list[str]:
    """先安装独立输出接收器，再返回可以安全初始化 ROS 的参数。

    Workbench 已接管同一 stderr 管道时复用父接收器；独立启动则在 fd 2 加 tee，
    原生 C/C++ 消息无需经过 Python logging，且仍回显到原 stderr。首次接管已
    初始化的外部 context 无法保证原磁盘 sink 已关闭，因此拒绝该情况。
    """

    global _output, _original_stderr, _prepared, _previous_capture_marker, _previous_stdout
    result = ros_logging_args(args, log_level=str(config.log_level))
    with _lock:
        if context_initialized and not _prepared:
            raise RuntimeError("ROS 已在日志容量策略之前初始化；请先准备日志策略再调用 rclpy.init")
        if _prepared:
            return result
        # rcutils 在初始化时读取此环境变量；固定 fd 2 才能覆盖真正的原生输出。
        previous_stdout = os.environ.get("RCUTILS_LOGGING_USE_STDOUT")
        os.environ["RCUTILS_LOGGING_USE_STDOUT"] = "0"
        if is_process_output_captured():
            _previous_stdout = previous_stdout
            _prepared = True
            return result
        output: ProcessOutput | None = None
        original: int | None = None
        previous_marker = os.environ.get(_CAPTURE_MARKER)
        try:
            root = Path(working_dir or config.working_dir or "unilabos_data").resolve() / "logs"
            policy = LogPolicy.from_config(config)
            original = os.dup(2)
            output = ProcessOutput(
                unique_log_path(root, prefix="ros_console_"),
                policy,
                cleanup_root=cleanup_root_for(root),
                tee_stderr=original,
            )
            sys.stderr.flush()
            os.dup2(output.stream.fileno(), 2)
            marker = output.environment().get(_CAPTURE_MARKER)
            if marker is not None:
                os.environ[_CAPTURE_MARKER] = marker
            else:
                os.environ.pop(_CAPTURE_MARKER, None)
            output.close()
            _output, _original_stderr, _prepared = output, original, True
            _previous_capture_marker, _previous_stdout = previous_marker, previous_stdout
        except BaseException:
            if original is not None:
                os.dup2(original, 2)
                os.close(original)
            if output is not None:
                output.close()
            if previous_marker is None:
                os.environ.pop(_CAPTURE_MARKER, None)
            else:
                os.environ[_CAPTURE_MARKER] = previous_marker
            if previous_stdout is None:
                os.environ.pop("RCUTILS_LOGGING_USE_STDOUT", None)
            else:
                os.environ["RCUTILS_LOGGING_USE_STDOUT"] = previous_stdout
            raise
    return result


def close_ros_logging() -> None:
    """恢复原 stderr 后等待接收器排空；应在 ROS shutdown 之后调用。"""

    global _output, _original_stderr, _prepared, _previous_capture_marker, _previous_stdout
    with _lock:
        if not _prepared:
            return
        output, original = _output, _original_stderr
        _output = None
        _original_stderr = None
        _prepared = False
        if original is not None:
            try:
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
            finally:
                os.dup2(original, 2)
                os.close(original)
            if _previous_capture_marker is None:
                os.environ.pop(_CAPTURE_MARKER, None)
            else:
                os.environ[_CAPTURE_MARKER] = _previous_capture_marker
        if _previous_stdout is None:
            os.environ.pop("RCUTILS_LOGGING_USE_STDOUT", None)
        else:
            os.environ["RCUTILS_LOGGING_USE_STDOUT"] = _previous_stdout
        _previous_capture_marker = _previous_stdout = None
    if output is not None:
        try:
            code = output.wait()
            if code:
                _logger.warning("ROS 日志接收器未完整落盘，退出码=%s", code)
        except Exception as exc:
            _logger.warning("ROS 日志接收器排空失败：%s", exc)


atexit.register(close_ros_logging)
