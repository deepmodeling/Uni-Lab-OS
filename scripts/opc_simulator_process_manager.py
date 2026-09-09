"""本地 OPC 模拟器子进程的单实例生命周期管理。"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from scripts.opc_simulator_profiles import (
    ProfileStorageError,
    read_profile,
    resolve_profile_path,
)
from scripts.szlab_task_opc_simulator import DEFAULT_URL

SIMULATOR_MODULE = "scripts.szlab_task_opc_simulator"


MAX_LOG_LINES = 200
MAX_LOG_LINE_BYTES = 4096
MAX_LOG_TOTAL_BYTES = 256 * 1024
DEFAULT_STOP_TIMEOUT = 15.0
PROCESS_POLL_INTERVAL = 0.05
STARTUP_CLEANUP_TIMEOUT = 1.0
DEFAULT_SHUTDOWN_TIMEOUT = 60.0
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]+")
logger = logging.getLogger(__name__)


class SimulatorInputError(ValueError):
    """启动或停止请求参数无效。"""


class InvalidSimulatorRevision(SimulatorInputError):
    """expected_revision 不是严格的小写 SHA-256。"""


class SimulatorRevisionConflict(RuntimeError):
    """请求 revision 与磁盘配置不一致。"""


class SimulatorRunIdentityConflict(RuntimeError):
    """停止请求不属于当前活动模拟器运行。"""


class InvalidSimulatorRunId(SimulatorInputError):
    """expected_run_id 不是严格 UUID hex。"""


class InvalidSimulatorConfig(ValueError):
    """配置不能交给模拟器运行。"""


class UnsafeSimulatorUrlConfirmationRequired(InvalidSimulatorConfig):
    """非默认 OPC URL 尚未获得本次启动的明确授权。"""


class SimulatorConfigNotFound(FileNotFoundError):
    """模拟器配置不存在。"""


class SimulatorStorageError(RuntimeError):
    """读取模拟器配置的存储操作失败。"""


class SimulatorStorageFull(SimulatorStorageError):
    """模拟器配置存储空间不足。"""


class SimulatorAlreadyRunning(RuntimeError):
    """已有模拟器处于活动生命周期。"""


class SimulatorSpawnError(RuntimeError):
    """创建模拟器子进程失败。"""


class StopTimeout(TimeoutError):
    """模拟器未能在安全恢复期限内退出。"""

    def __init__(self, status: dict[str, Any]) -> None:
        super().__init__("模拟器停止超时")
        self.status = status


class OpcSimulatorProcessManager:
    """只允许一个 OPC 模拟器子进程存活。"""

    @staticmethod
    def _simulator_subprocess_env(repo_root: Path) -> dict[str, str]:
        """确保子进程能 import unilabos 与 scripts 包。"""
        env = os.environ.copy()
        root = str(repo_root.resolve())
        prefix = env.get("PYTHONPATH", "")
        if prefix:
            parts = [part for part in prefix.split(os.pathsep) if part]
            if root not in parts:
                env["PYTHONPATH"] = os.pathsep.join([root, prefix])
        else:
            env["PYTHONPATH"] = root
        return env

    def __init__(
        self,
        *,
        config_dir: str | Path,
        script_path: str | Path | None = None,
        python_executable: str = sys.executable,
        process_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        thread_factory: Callable[..., Any] = threading.Thread,
        poll_wait: Callable[[float], Any] = time.sleep,
        shutdown_waiter: Callable[[Any, float], int] | None = None,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._script_path_explicit = script_path is not None
        self._script_path = Path(
            script_path
            if script_path is not None
            else Path(__file__).with_name("szlab_task_opc_simulator.py")
        ).resolve()
        self._repo_root = self._script_path.parents[1].resolve()
        self._python_executable = python_executable
        self._process_factory = process_factory
        self._clock = clock
        self._thread_factory = thread_factory
        self._poll_wait = poll_wait
        self._shutdown_waiter = shutdown_waiter or (
            lambda process, timeout: process.wait(timeout=timeout)
        )
        if (
            isinstance(shutdown_timeout, bool)
            or not isinstance(shutdown_timeout, (int, float))
            or not math.isfinite(float(shutdown_timeout))
            or not 0 < float(shutdown_timeout) <= DEFAULT_SHUTDOWN_TIMEOUT
        ):
            raise ValueError("shutdown_timeout 必须在 0 到 60 秒之间")
        self._shutdown_timeout = float(shutdown_timeout)
        self._operation_lock = threading.RLock()
        self._lock = threading.RLock()
        self._process: Any | None = None
        self._state = "idle"
        self._pid: int | None = None
        self._file_name: str | None = None
        self._revision: str | None = None
        self._run_id: str | None = None
        self._opc_url: str | None = None
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._ended_monotonic: float | None = None
        self._return_code: int | None = None
        self._restore_status = "not_started"
        self._last_error: str | None = None
        self._recent_logs: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._recent_log_bytes = 0

    def _finalize_locked(self, process: Any, return_code: int) -> None:
        if process is not self._process:
            return
        if self._return_code is not None and self._ended_monotonic is not None:
            return
        self._return_code = int(return_code)
        ended = float(self._clock())
        self._ended_at = ended
        self._ended_monotonic = ended
        if return_code == 0:
            self._state = "stopped"
            self._restore_status = "succeeded"
            self._last_error = None
        else:
            self._state = "failed"
            self._restore_status = "not_started"
            self._last_error = "模拟器异常退出"

    def _refresh_exit_locked(self) -> None:
        process = self._process
        if process is None:
            return
        return_code = process.poll()
        if return_code is not None:
            self._finalize_locked(process, return_code)

    def _status_locked(self) -> dict[str, Any]:
        effective_now = (
            self._ended_monotonic
            if self._ended_monotonic is not None
            else float(self._clock())
        )
        elapsed = (
            0.0
            if self._started_at is None
            else max(0.0, effective_now - self._started_at)
        )
        return {
            "state": self._state,
            "pid": self._pid,
            "file_name": self._file_name,
            "revision": self._revision,
            "run_id": self._run_id,
            "opc_url": self._opc_url,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "ended_monotonic": self._ended_monotonic,
            "elapsed_seconds": elapsed,
            "return_code": self._return_code,
            "restore_status": self._restore_status,
            "last_error": self._last_error,
            "recent_logs": list(self._recent_logs),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_exit_locked()
            return self._status_locked()

    @staticmethod
    def _sanitize_opc_url(raw_url: str) -> str:
        try:
            parsed = urlsplit(raw_url)
            hostname = parsed.hostname
            if not hostname:
                netloc = parsed.netloc.rsplit("@", 1)[-1]
            else:
                host = f"[{hostname}]" if ":" in hostname else hostname
                netloc = host
                if parsed.port is not None:
                    netloc = f"{netloc}:{parsed.port}"
            return urlunsplit(
                (parsed.scheme, netloc, parsed.path, "", "")
            )
        except (TypeError, ValueError):
            return raw_url.split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1]

    def _sanitize_log_line(self, raw_line: str) -> str:
        line = raw_line.replace(
            str(self._config_dir.resolve()),
            "<config_dir>",
        )
        line = line.replace(str(Path.home()), "<home>")
        return _URL_PATTERN.sub(
            lambda match: self._sanitize_opc_url(match.group(0)),
            line,
        )

    @staticmethod
    def _bounded_log_line(raw_line: str) -> str:
        encoded = raw_line.rstrip("\r\n").encode("utf-8", errors="replace")
        if len(encoded) <= MAX_LOG_LINE_BYTES:
            return encoded.decode("utf-8", errors="replace")
        return encoded[:MAX_LOG_LINE_BYTES].decode("utf-8", errors="ignore")

    def _append_log(self, process: Any, raw_line: str) -> None:
        line = self._bounded_log_line(self._sanitize_log_line(raw_line))
        line_bytes = len(line.encode("utf-8"))
        with self._lock:
            if process is not self._process:
                return
            if len(self._recent_logs) == MAX_LOG_LINES:
                removed = self._recent_logs.popleft()
                self._recent_log_bytes -= len(removed.encode("utf-8"))
            self._recent_logs.append(line)
            self._recent_log_bytes += line_bytes
            while (
                self._recent_logs
                and self._recent_log_bytes > MAX_LOG_TOTAL_BYTES
            ):
                removed = self._recent_logs.popleft()
                self._recent_log_bytes -= len(removed.encode("utf-8"))

    def _read_logs(self, process: Any) -> None:
        stream = getattr(process, "stdout", None)
        if stream is None:
            return
        try:
            while True:
                line = stream.readline(MAX_LOG_LINE_BYTES + 2)
                if not line:
                    break
                self._append_log(process, line)
                while line and not line.endswith(("\n", "\r")):
                    line = stream.readline(MAX_LOG_LINE_BYTES + 2)
        except Exception:
            with self._lock:
                if (
                    process is self._process
                    and self._state not in {"failed", "stopped"}
                ):
                    self._last_error = "读取模拟器日志失败"
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _wait_for_exit(self, process: Any) -> None:
        while True:
            with self._lock:
                if process is not self._process:
                    return
                try:
                    return_code = process.poll()
                except Exception:
                    self._state = "failed"
                    self._restore_status = "error"
                    self._last_error = "轮询模拟器进程失败"
                    return
                if return_code is not None:
                    self._finalize_locked(process, return_code)
                    return
            self._poll_wait(PROCESS_POLL_INTERVAL)

    def _start_thread(self, *, target: Callable[[], None], name: str) -> None:
        thread = self._thread_factory(target=target, name=name, daemon=True)
        thread.start()

    def _send_sigterm_locked(self, process: Any) -> bool:
        """锁内确认存活并发送 TERM；返回 False 表示进程已经终态。"""
        return_code = process.poll()
        if return_code is not None:
            self._finalize_locked(process, return_code)
            return False
        pid = getattr(process, "pid", None)
        try:
            if pid is None:
                raise ProcessLookupError
            os.killpg(pid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return_code = process.poll()
            if return_code is not None:
                self._finalize_locked(process, return_code)
                return False
        except (OSError, TypeError, ValueError):
            pass
        try:
            process.send_signal(signal.SIGTERM)
            return True
        except ProcessLookupError:
            return_code = process.poll()
            if return_code is not None:
                self._finalize_locked(process, return_code)
                return False
            raise

    def _cleanup_failed_thread_start_locked(
        self,
        process: Any,
        cause: BaseException,
    ) -> None:
        stream = getattr(process, "stdout", None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        try:
            signaled = self._send_sigterm_locked(process)
        except Exception:
            signaled = False
        return_code = process.poll()
        if return_code is not None:
            try:
                return_code = process.wait(timeout=STARTUP_CLEANUP_TIMEOUT)
            except Exception:
                pass
        elif signaled:
            try:
                return_code = process.wait(timeout=STARTUP_CLEANUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                return_code = process.poll()
            except Exception:
                return_code = process.poll()
        if return_code is not None:
            self._return_code = int(return_code)
            ended = float(self._clock())
            self._ended_at = ended
            self._ended_monotonic = ended
            self._restore_status = (
                "succeeded" if return_code == 0 else "error"
            )
        else:
            self._restore_status = "uncertain"
        self._state = "failed"
        self._last_error = "启动模拟器后台线程失败"
        raise SimulatorSpawnError("启动模拟器后台线程失败") from cause

    def start(
        self,
        file_name: str,
        expected_revision: str,
        *,
        allow_unsafe_url: bool = False,
    ) -> dict[str, Any]:
        with self._operation_lock:
            return self._start_serialized(
                file_name,
                expected_revision,
                allow_unsafe_url=allow_unsafe_url,
            )

    def _start_serialized(
        self,
        file_name: str,
        expected_revision: str,
        *,
        allow_unsafe_url: bool,
    ) -> dict[str, Any]:
        if not isinstance(file_name, str) or not file_name:
            raise SimulatorInputError("file_name 必须是非空字符串")
        if (
            not isinstance(expected_revision, str)
            or not _REVISION_PATTERN.fullmatch(expected_revision)
        ):
            raise InvalidSimulatorRevision("expected_revision 格式无效")

        with self._lock:
            self._refresh_exit_locked()
            if self._state in {"starting", "running", "stopping"} or (
                self._process is not None and self._process.poll() is None
            ):
                raise SimulatorAlreadyRunning("已有 OPC 模拟器正在运行")

            try:
                config_path = resolve_profile_path(file_name, self._config_dir)
            except (TypeError, ValueError) as exc:
                raise SimulatorInputError("file_name 无效") from exc
            try:
                loaded = read_profile(file_name, self._config_dir)
            except FileNotFoundError as exc:
                raise SimulatorConfigNotFound("模拟器配置不存在") from exc
            except OSError as exc:
                if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise SimulatorStorageFull("模拟器存储空间不足") from exc
                raise SimulatorStorageError("模拟器存储操作失败") from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise InvalidSimulatorConfig("模拟器配置无效") from exc
            except ProfileStorageError as exc:
                raise SimulatorStorageError("模拟器存储操作失败") from exc
            except ValueError as exc:
                raise InvalidSimulatorConfig("模拟器配置无效") from exc
            if loaded["revision"] != expected_revision:
                raise SimulatorRevisionConflict("profile revision 冲突")
            profile = loaded.get("profile")
            validation_errors = loaded.get("validation_errors")
            if (
                not isinstance(profile, dict)
                or profile.get("status") != "runnable"
                or validation_errors
            ):
                raise InvalidSimulatorConfig("模拟器配置不可运行")

            opc = profile.get("opc")
            opc_url = opc.get("url") if isinstance(opc, dict) else None
            if not isinstance(opc_url, str) or not opc_url.strip():
                raise InvalidSimulatorConfig("模拟器配置不可运行")
            if opc_url != DEFAULT_URL and not allow_unsafe_url:
                raise UnsafeSimulatorUrlConfirmationRequired(
                    "非默认 OPC URL 启动前需确认风险"
                )

            self._state = "starting"
            self._process = None
            self._pid = None
            self._file_name = file_name
            self._revision = loaded["revision"]
            self._run_id = None
            self._opc_url = self._sanitize_opc_url(opc_url)
            self._started_at = None
            self._ended_at = None
            self._ended_monotonic = None
            self._return_code = None
            self._restore_status = "not_started"
            self._last_error = None
            self._recent_logs.clear()
            self._recent_log_bytes = 0

            if self._script_path_explicit:
                # An explicitly supplied script is treated as the complete
                # subprocess entrypoint.  Keep this contract minimal and
                # deterministic: no inherited cwd/env overrides are needed,
                # and callers can safely assert the exact Popen options.
                command = [
                    self._python_executable,
                    "-u",
                    str(self._script_path),
                    "--config",
                    str(config_path.resolve()),
                    "--expected-revision",
                    expected_revision,
                ]
            else:
                command = [
                    self._python_executable,
                    "-u",
                    "-m",
                    SIMULATOR_MODULE,
                    "--config",
                    str(config_path.resolve()),
                    "--expected-revision",
                    expected_revision,
                ]
            if allow_unsafe_url:
                command.append("--allow-unsafe-url")
            try:
                popen_kwargs = {
                    "shell": False,
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "start_new_session": True,
                    "close_fds": True,
                }
                if not self._script_path_explicit:
                    popen_kwargs.update(
                        cwd=str(self._repo_root),
                        env=self._simulator_subprocess_env(self._repo_root),
                    )
                process = self._process_factory(command, **popen_kwargs)
            except Exception as exc:
                self._state = "failed"
                self._restore_status = "not_started"
                self._last_error = "启动模拟器进程失败"
                raise SimulatorSpawnError("启动模拟器进程失败") from exc

            self._process = process
            self._pid = getattr(process, "pid", None)
            self._started_at = float(self._clock())
            self._state = "running"
            self._run_id = uuid.uuid4().hex
            try:
                self._start_thread(
                    target=lambda: self._read_logs(process),
                    name="opc-simulator-log-reader",
                )
                self._start_thread(
                    target=lambda: self._wait_for_exit(process),
                    name="opc-simulator-waiter",
                )
            except Exception as exc:
                self._run_id = None
                self._cleanup_failed_thread_start_locked(process, exc)
            self._refresh_exit_locked()
            return self._status_locked()

    @staticmethod
    def _validate_timeout(timeout: float) -> float:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 1 <= float(timeout) <= 60
        ):
            raise ValueError("timeout 必须在 1 到 60 秒之间")
        return float(timeout)

    def stop(
        self,
        timeout: float = DEFAULT_STOP_TIMEOUT,
        *,
        expected_run_id: str | None = None,
    ) -> dict[str, Any]:
        with self._operation_lock:
            bounded_timeout = self._validate_timeout(timeout)
            if expected_run_id is not None and (
                not isinstance(expected_run_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", expected_run_id)
            ):
                raise InvalidSimulatorRunId("expected_run_id 格式无效")
            return self._stop_serialized(bounded_timeout, expected_run_id)

    def _stop_serialized(
        self,
        bounded_timeout: float,
        expected_run_id: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            self._refresh_exit_locked()
            process = self._process
            if process is None:
                return self._status_locked()
            return_code = process.poll()
            if return_code is not None:
                self._finalize_locked(process, return_code)
                return self._status_locked()
            if expected_run_id is not None and expected_run_id != self._run_id:
                raise SimulatorRunIdentityConflict(
                    "expected_run_id 与当前活动运行不一致"
                )
            already_stopping = self._state == "stopping"
            self._state = "stopping"
            self._restore_status = "pending"
            self._last_error = None
            if not already_stopping:
                try:
                    signaled = self._send_sigterm_locked(process)
                except Exception as exc:
                    self._state = "running"
                    self._restore_status = "error"
                    self._last_error = "发送停止信号失败"
                    raise RuntimeError("发送停止信号失败") from exc
                if not signaled:
                    return self._status_locked()

        try:
            return_code = process.wait(timeout=bounded_timeout)
        except subprocess.TimeoutExpired as exc:
            with self._lock:
                return_code = process.poll()
                if return_code is not None:
                    self._finalize_locked(process, return_code)
                    return self._status_locked()
                if process is self._process:
                    self._state = "stopping"
                    self._restore_status = "uncertain"
                    self._last_error = "停止超时，恢复结果不确定"
                status = self._status_locked()
            raise StopTimeout(status) from exc

        with self._lock:
            self._finalize_locked(process, return_code)
            return self._status_locked()

    def shutdown(self) -> dict[str, Any]:
        with self._operation_lock:
            return self._shutdown_serialized()

    def _shutdown_serialized(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_exit_locked()
            process = self._process
            if process is None:
                return self._status_locked()
            return_code = process.poll()
            if return_code is not None:
                self._finalize_locked(process, return_code)
                return self._status_locked()
            already_stopping = self._state == "stopping"
            self._state = "stopping"
            self._restore_status = "pending"
            self._last_error = None
            if not already_stopping:
                try:
                    signaled = self._send_sigterm_locked(process)
                except Exception as exc:
                    self._state = "running"
                    self._restore_status = "error"
                    self._last_error = "发送停止信号失败"
                    raise RuntimeError("发送停止信号失败") from exc
                if not signaled:
                    return self._status_locked()

        try:
            return_code = self._shutdown_waiter(
                process,
                self._shutdown_timeout,
            )
        except subprocess.TimeoutExpired:
            return self._force_shutdown_after_timeout(process)
        with self._lock:
            if return_code is None:
                return_code = process.poll()
            if return_code is None:
                self._state = "stopping"
                self._restore_status = "uncertain"
                self._last_error = "shutdown 等待策略未回收子进程"
                raise RuntimeError("shutdown 未能回收模拟器子进程")
            self._finalize_locked(process, return_code)
            return self._status_locked()

    def _force_shutdown_after_timeout(self, process: Any) -> dict[str, Any]:
        critical_message = (
            "CRITICAL: workflow UI shutdown 超时，执行 SIGKILL；"
            "恢复结果不确定"
        )
        logger.critical(critical_message)
        with self._lock:
            return_code = process.poll()
            if return_code is not None:
                self._finalize_locked(process, return_code)
                return self._status_locked()
            self._state = "failed"
            self._restore_status = "uncertain"
            self._last_error = critical_message
            pid = getattr(process, "pid", None)
            kill_sent = False
            try:
                if pid is None:
                    raise ProcessLookupError
                os.killpg(pid, signal.SIGKILL)
                kill_sent = True
            except ProcessLookupError:
                return_code = process.poll()
                if return_code is not None:
                    self._record_forced_shutdown_locked(process, return_code)
                    return self._status_locked()
            except (OSError, TypeError, ValueError):
                pass
            if not kill_sent:
                try:
                    process.kill()
                    kill_sent = True
                except Exception:
                    failure = (
                        "CRITICAL: workflow UI shutdown 超时且 SIGKILL 失败；"
                        "恢复结果不确定"
                    )
                    logger.critical(failure)
                    self._state = "failed"
                    self._restore_status = "uncertain"
                    self._last_error = failure
                    return self._status_locked()

        try:
            return_code = process.wait()
        except Exception:
            with self._lock:
                failure = (
                    "CRITICAL: SIGKILL 后回收模拟器子进程失败；"
                    "恢复结果不确定"
                )
                logger.critical(failure)
                self._state = "failed"
                self._restore_status = "uncertain"
                self._last_error = failure
                return self._status_locked()
        with self._lock:
            self._record_forced_shutdown_locked(process, return_code)
            return self._status_locked()

    def _record_forced_shutdown_locked(
        self,
        process: Any,
        return_code: int,
    ) -> None:
        if process is not self._process:
            return
        self._return_code = int(return_code)
        ended = float(self._clock())
        self._ended_at = ended
        self._ended_monotonic = ended
        self._state = "failed"
        self._restore_status = "uncertain"
        self._last_error = (
            "CRITICAL: workflow UI shutdown 超时后已强制终止；"
            "恢复结果不确定"
        )
