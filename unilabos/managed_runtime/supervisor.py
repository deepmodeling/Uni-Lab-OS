"""通过仅监听 loopback 的 HTTP 接口管理一个 Uni-Lab Runtime Worker。"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from unilabos.utils.log_storage import LogPolicy
from unilabos.utils.process_output import ProcessOutput, session_log_path

_BACKENDS = frozenset({"ros", "dora", "simple", "automancer"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_STOP_TIMEOUT_SECONDS = 10.0
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "worker_pid",
        "simulator_status",
        "simulator_pid",
        "updated_at",
    }
)
_PROCESS_STATES = frozenset({"idle", "running", "interrupted"})


class SupervisorRequestError(ValueError):
    """调用方可以修正的受管 Runtime 请求错误。"""


@dataclass(frozen=True)
class WorkerLaunchRequest:
    workspace_path: Path
    graph_path: Path
    config_path: Path
    working_dir: Path
    backend: str

    @classmethod
    def parse(cls, payload: object) -> WorkerLaunchRequest:
        if not isinstance(payload, dict):
            raise SupervisorRequestError("请求体必须是 JSON object")

        def required_path(name: str) -> Path:
            value = payload.get(name)
            if not isinstance(value, str) or not value.strip():
                raise SupervisorRequestError(f"{name} 必须是非空路径")
            return Path(value).expanduser().resolve()

        workspace_path = required_path("workspace_path")
        graph_path = required_path("graph_path")
        config_path = required_path("config_path")
        working_dir = required_path("working_dir")
        backend = payload.get("backend", "ros")
        if not isinstance(backend, str) or backend not in _BACKENDS:
            raise SupervisorRequestError(
                f"backend 必须是以下值之一：{', '.join(sorted(_BACKENDS))}"
            )
        if not workspace_path.is_dir():
            raise SupervisorRequestError(f"workspace_path 不存在：{workspace_path}")
        if not graph_path.is_file():
            raise SupervisorRequestError(f"graph_path 不存在：{graph_path}")
        if graph_path.suffix.lower() != ".json":
            raise SupervisorRequestError("graph_path 必须是 JSON 文件")
        if not config_path.is_file():
            raise SupervisorRequestError(f"config_path 不存在：{config_path}")
        if config_path.suffix.lower() != ".py":
            raise SupervisorRequestError("config_path 必须是 Python 文件")
        working_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            workspace_path=workspace_path,
            graph_path=graph_path,
            config_path=config_path,
            working_dir=working_dir,
            backend=backend,
        )


@dataclass(frozen=True)
class SimulatorLaunchRequest:
    kind: str
    path: Path
    working_directory: Path

    @classmethod
    def parse(cls, payload: object) -> SimulatorLaunchRequest:
        if not isinstance(payload, dict):
            raise SupervisorRequestError("请求体必须是 JSON object")
        source_path = payload.get("source_path")
        executable_path = payload.get("executable_path")
        if bool(source_path) == bool(executable_path):
            raise SupervisorRequestError(
                "source_path 与 executable_path 必须且只能提供一个"
            )
        if source_path:
            if not isinstance(source_path, str):
                raise SupervisorRequestError("source_path 必须是路径")
            root = Path(source_path).expanduser().resolve()
            for candidate in (root / "OpcUaSim", root):
                if (candidate / "gui" / "backend.py").is_file():
                    return cls("source", root, candidate)
            raise SupervisorRequestError(
                f"PLC-Sim 源码缺少 OpcUaSim/gui/backend.py：{root}"
            )
        if not isinstance(executable_path, str):
            raise SupervisorRequestError("executable_path 必须是路径")
        executable = Path(executable_path).expanduser().resolve()
        if not executable.is_file():
            raise SupervisorRequestError(f"PLC-Sim 可执行文件不存在：{executable}")
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise SupervisorRequestError(f"PLC-Sim 文件不可执行：{executable}")
        return cls("executable", executable, executable.parent)


class ManagedRuntimeSupervisor:
    """拥有受管 Worker 生命周期，并向 Electron 暴露窄 HTTP Interface。"""

    def __init__(
        self,
        runtime_prefix: Path,
        state_directory: Path,
        token: str,
    ) -> None:
        if not token:
            raise ValueError("Supervisor token 不能为空")
        self._runtime_prefix = runtime_prefix.expanduser().resolve()
        self._state_directory = state_directory.expanduser().resolve()
        self._state_directory.mkdir(parents=True, exist_ok=True)
        self._state_path = self._state_directory / "supervisor-state.json"
        self._token = token
        self._lock = threading.RLock()
        self._worker: subprocess.Popen[bytes] | None = None
        self._worker_log: ProcessOutput | None = None
        self._worker_log_path: Path | None = None
        self._simulator: subprocess.Popen[bytes] | None = None
        self._simulator_log: ProcessOutput | None = None
        self._simulator_log_path: Path | None = None
        self._last_error: str | None = None
        self._simulator_error: str | None = None
        previous_state = self._load_state()
        self._interrupted = previous_state.get("status") in {
            "running",
            "interrupted",
        }
        self._simulator_interrupted = previous_state.get("simulator_status") in {
            "running",
            "interrupted",
        }
        if self._interrupted or self._simulator_interrupted:
            self._persist_state()

    def authorized(self, authorization: str | None) -> bool:
        expected = f"Bearer {self._token}"
        return authorization is not None and hmac.compare_digest(
            authorization,
            expected,
        )

    def status(self) -> dict[str, object]:
        with self._lock:
            self._refresh_worker_locked()
            self._refresh_simulator_locked()
            worker = self._worker
            status = (
                "running"
                if worker is not None
                else "interrupted"
                if self._interrupted
                else "idle"
            )
            return {
                "status": status,
                "worker": None if worker is None else {"pid": worker.pid},
                "error": self._last_error,
                "logPath": None if self._worker_log_path is None else str(self._worker_log_path),
                "simulator": self._simulator_status_locked(),
            }

    def start_worker(self, payload: object) -> dict[str, object]:
        request = WorkerLaunchRequest.parse(payload)
        with self._lock:
            self._refresh_worker_locked()
            if self._worker is not None:
                raise SupervisorRequestError("已有受管 Runtime Worker 正在运行")
            if self._interrupted:
                raise SupervisorRequestError(
                    "上次 Runtime 未正常结束；请先确认设备状态并清除中断标记"
                )

            executable = self._unilab_executable()
            # 与此 Worker 的应用日志共享 working_dir 清理池，状态文件仍留在 state 目录。
            log_path = session_log_path(request.working_dir / "logs" / "edge.log")
            self._worker_log_path = log_path
            self._interrupted = True
            self._last_error = "Runtime Worker 启动尚未完成"
            self._persist_state()
            command = [
                str(executable),
                "--workspace",
                str(request.workspace_path),
                "--graph",
                str(request.graph_path),
                "--config",
                str(request.config_path),
                "--working_dir",
                str(request.working_dir),
                "--backend",
                request.backend,
                "--app_bridges",
                "fastapi",
                "--edge_scheduler",
                "--port",
                "18003",
                "--disable_browser",
                "--skip_env_check",
                "--test_mode",
            ]
            try:
                environment = self._runtime_environment()
                self._worker_log = ProcessOutput(log_path, LogPolicy.from_env(environment))
                self._worker = subprocess.Popen(
                    command,
                    cwd=request.workspace_path,
                    env=self._worker_log.environment(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=self._worker_log.stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                )
            except BaseException as error:
                self._close_worker_log_locked()
                self._last_error = f"Runtime Worker 启动失败：{error}；日志：{log_path}"
                self._persist_state_best_effort()
                raise
            self._worker_log.close()
            self._last_error = None
            try:
                self._persist_state()
            except BaseException:
                worker = self._worker
                if worker is not None:
                    self._terminate_process_locked(worker)
                self._worker = None
                self._close_worker_log_locked()
                self._interrupted = True
                self._last_error = "Runtime Worker 运行状态持久化失败"
                self._persist_state_best_effort()
                raise
            return self.status()

    def stop_worker(self) -> dict[str, object]:
        with self._lock:
            self._refresh_worker_locked()
            worker = self._worker
            if worker is None:
                self._interrupted = False
                self._last_error = None
                self._persist_state()
                return self.status()
            self._terminate_process_locked(worker)
            self._worker = None
            self._close_worker_log_locked()
            self._interrupted = False
            self._last_error = None
            self._persist_state()
            return self.status()

    def start_simulator(self, payload: object) -> dict[str, object]:
        request = SimulatorLaunchRequest.parse(payload)
        with self._lock:
            self._refresh_simulator_locked()
            if self._simulator is not None:
                raise SupervisorRequestError("已有 PLC-Sim 正在运行")
            if self._simulator_interrupted:
                raise SupervisorRequestError(
                    "上次 PLC-Sim 未正常结束；请先确认端口和设备状态"
                )
            command = (
                [
                    str(self._python_executable()),
                    "-m",
                    "gui.backend",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "18765",
                ]
                if request.kind == "source"
                else [str(request.path)]
            )
            self._simulator_interrupted = True
            self._simulator_error = "PLC-Sim 启动尚未完成"
            self._persist_state()
            try:
                self._simulator_log_path = session_log_path(self._state_directory / "simulator.log")
                environment = self._runtime_environment()
                self._simulator_log = ProcessOutput(self._simulator_log_path, LogPolicy.from_env(environment))
                self._simulator = subprocess.Popen(
                    command,
                    cwd=request.working_directory,
                    env=self._simulator_log.environment(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=self._simulator_log.stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                )
            except BaseException as error:
                self._close_simulator_log_locked()
                self._simulator_error = f"PLC-Sim 启动失败：{error}；日志：{self._simulator_log_path}"
                self._persist_state_best_effort()
                raise
            self._simulator_log.close()
            self._simulator_error = None
            try:
                self._persist_state()
            except BaseException:
                simulator = self._simulator
                if simulator is not None:
                    self._terminate_process_locked(simulator)
                self._simulator = None
                self._close_simulator_log_locked()
                self._simulator_interrupted = True
                self._simulator_error = "PLC-Sim 运行状态持久化失败"
                self._persist_state_best_effort()
                raise
            return self.status()

    def stop_simulator(self) -> dict[str, object]:
        with self._lock:
            self._refresh_simulator_locked()
            simulator = self._simulator
            if simulator is not None:
                self._terminate_process_locked(simulator)
            self._simulator = None
            self._close_simulator_log_locked()
            self._simulator_interrupted = False
            self._simulator_error = None
            self._persist_state()
            return self.status()

    def close(self) -> None:
        with self._lock:
            self._refresh_worker_locked()
            self._refresh_simulator_locked()
            worker = self._worker
            if worker is not None:
                self._terminate_process_locked(worker)
                self._worker = None
                self._close_worker_log_locked()
                self._interrupted = False
                self._last_error = None
            simulator = self._simulator
            if simulator is not None:
                self._terminate_process_locked(simulator)
                self._simulator = None
                self._close_simulator_log_locked()
                self._simulator_interrupted = False
                self._simulator_error = None
            self._persist_state()

    def _unilab_executable(self) -> Path:
        relative = (
            Path("Scripts") / "unilab.exe"
            if os.name == "nt"
            else Path("bin") / "unilab"
        )
        executable = (self._runtime_prefix / relative).resolve()
        try:
            executable.relative_to(self._runtime_prefix)
        except ValueError as error:
            raise RuntimeError("受管 unilab 可执行文件越出 Runtime 前缀") from error
        if not executable.is_file():
            raise RuntimeError(f"受管 Runtime 缺少 unilab：{executable}")
        return executable

    def _python_executable(self) -> Path:
        relative = Path("python.exe") if os.name == "nt" else Path("bin/python")
        executable = (self._runtime_prefix / relative).resolve()
        try:
            executable.relative_to(self._runtime_prefix)
        except ValueError as error:
            raise RuntimeError("受管 Python 可执行文件越出 Runtime 前缀") from error
        if not executable.is_file():
            raise RuntimeError(f"受管 Runtime 缺少 Python：{executable}")
        return executable

    def _runtime_environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in {"PYTHONPATH", "PYTHONHOME"}
        }
        if os.name == "nt":
            paths = [
                self._runtime_prefix,
                self._runtime_prefix / "Library" / "bin",
                self._runtime_prefix / "Scripts",
                self._runtime_prefix / "bin",
            ]
        else:
            paths = [self._runtime_prefix / "bin"]
        inherited_path = environment.get("PATH")
        environment["PATH"] = os.pathsep.join(
            [
                *(str(path) for path in paths),
                *([inherited_path] if inherited_path else []),
            ]
        )
        environment["CONDA_PREFIX"] = str(self._runtime_prefix)
        environment["CONDA_DEFAULT_ENV"] = self._runtime_prefix.name
        environment["CONDA_SHLVL"] = "1"
        # conda-forge's Windows Python build uses this opt-in hook to restore
        # the environment's DLL search directory for native ROS extensions.
        # A detached Supervisor cannot rely on an activated shell, so export
        # it explicitly before spawning the Runtime Worker.
        environment["CONDA_DLL_SEARCH_MODIFICATION_ENABLE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        return environment

    def _refresh_worker_locked(self) -> None:
        worker = self._worker
        if worker is None:
            return
        return_code = worker.poll()
        if return_code is None:
            return
        self._last_error = f"Runtime Worker 已退出，exit_code={return_code}"
        self._worker = None
        self._interrupted = True
        self._close_worker_log_locked()
        self._persist_state()

    def _refresh_simulator_locked(self) -> None:
        simulator = self._simulator
        if simulator is None:
            return
        return_code = simulator.poll()
        if return_code is None:
            return
        self._simulator_error = f"PLC-Sim 已退出，exit_code={return_code}"
        self._simulator = None
        self._simulator_interrupted = True
        self._close_simulator_log_locked()
        self._persist_state()

    def _simulator_status_locked(self) -> dict[str, object]:
        simulator = self._simulator
        return {
            "status": (
                "running"
                if simulator is not None
                else "interrupted"
                if self._simulator_interrupted
                else "idle"
            ),
            "pid": None if simulator is None else simulator.pid,
            "error": self._simulator_error,
            "logPath": None if self._simulator_log_path is None else str(self._simulator_log_path),
        }

    def _load_state(self) -> dict[str, object]:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return self._corrupt_state()
        if not self._state_is_valid(payload):
            return self._corrupt_state()
        return payload

    def _corrupt_state(self) -> dict[str, object]:
        self._last_error = "Supervisor 状态文件损坏；需要人工确认设备状态"
        self._simulator_error = self._last_error
        return {"status": "interrupted", "simulator_status": "interrupted"}

    @staticmethod
    def _state_is_valid(payload: object) -> bool:
        if not isinstance(payload, dict) or set(payload) != _STATE_FIELDS:
            return False
        schema_version = payload.get("schema_version")
        if isinstance(schema_version, bool) or schema_version != 1:
            return False
        updated_at = payload.get("updated_at")
        if (
            isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
            or not math.isfinite(updated_at)
        ):
            return False
        for status_field, pid_field in (
            ("status", "worker_pid"),
            ("simulator_status", "simulator_pid"),
        ):
            status = payload.get(status_field)
            pid = payload.get(pid_field)
            if not isinstance(status, str) or status not in _PROCESS_STATES:
                return False
            if status == "running":
                if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
                    return False
            elif pid is not None:
                return False
        return True

    def _persist_state(self) -> None:
        worker_status = (
            "running"
            if self._worker is not None
            else "interrupted"
            if self._interrupted
            else "idle"
        )
        payload = {
            "schema_version": 1,
            "status": worker_status,
            "worker_pid": None if self._worker is None else self._worker.pid,
            "simulator_status": self._simulator_status_locked()["status"],
            "simulator_pid": (None if self._simulator is None else self._simulator.pid),
            "updated_at": time.time(),
        }
        temporary_path = self._state_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, self._state_path)

    def _persist_state_best_effort(self) -> None:
        try:
            self._persist_state()
        except OSError:
            # 启动前已持久化 interrupted；清理后的二次写失败不能覆盖原异常。
            pass

    def _terminate_process_locked(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            self._terminate_windows_process_tree_locked(process)
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)

    def _terminate_windows_process_tree_locked(
        self,
        process: subprocess.Popen[bytes],
    ) -> None:
        system_root = os.environ.get("SystemRoot")
        if not system_root:
            raise RuntimeError("Windows 缺少 SystemRoot，无法回收受管进程树")
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
        if not taskkill.is_file():
            raise RuntimeError(f"Windows 缺少 taskkill.exe：{taskkill}")
        completed = subprocess.run(
            [
                str(taskkill),
                "/PID",
                str(process.pid),
                "/T",
                "/F",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_STOP_TIMEOUT_SECONDS,
        )
        try:
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Windows 受管进程树未在超时前退出") from error
        if completed.returncode != 0:
            raise RuntimeError(
                f"taskkill 回收受管进程树失败：exit_code={completed.returncode}"
            )

    def _close_worker_log_locked(self) -> None:
        if self._worker_log is not None:
            try:
                self._worker_log.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                # 遗留子孙进程可能仍持有写端，接收器会独立继续排空。
                pass
            self._worker_log = None

    def _close_simulator_log_locked(self) -> None:
        if self._simulator_log is not None:
            try:
                self._simulator_log.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            self._simulator_log = None


def _handler_type(
    supervisor: ManagedRuntimeSupervisor,
) -> type[BaseHTTPRequestHandler]:
    class SupervisorHandler(BaseHTTPRequestHandler):
        server_version = "UniLabManagedRuntime/1"

        def do_GET(self) -> None:
            if not self._authorize():
                return
            if self.path != "/v1/status":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._json(HTTPStatus.OK, supervisor.status())

        def do_POST(self) -> None:
            if not self._authorize():
                return
            if self.path not in {"/v1/workers", "/v1/simulators"}:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            try:
                body = self._body()
                result = (
                    supervisor.start_worker(body)
                    if self.path == "/v1/workers"
                    else supervisor.start_simulator(body)
                )
                self._json(HTTPStatus.CREATED, result)
            except SupervisorRequestError as error:
                self._json(HTTPStatus.CONFLICT, {"error": str(error)})
            except Exception as error:  # noqa: BLE001 - HTTP seam normalizes failures.
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

        def do_DELETE(self) -> None:
            if not self._authorize():
                return
            if self.path not in {
                "/v1/workers/current",
                "/v1/simulators/current",
            }:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            result = (
                supervisor.stop_worker()
                if self.path == "/v1/workers/current"
                else supervisor.stop_simulator()
            )
            self._json(HTTPStatus.OK, result)

        def log_message(self, format: str, *args: object) -> None:
            print(
                f"[supervisor] {self.address_string()} {format % args}",
                file=sys.stderr,
                flush=True,
            )

        def _authorize(self) -> bool:
            if supervisor.authorized(self.headers.get("Authorization")):
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False

        def _body(self) -> object:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as error:
                raise SupervisorRequestError("Content-Length 无效") from error
            if length < 1 or length > 1024 * 1024:
                raise SupervisorRequestError("请求体大小无效")
            try:
                return json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise SupervisorRequestError("请求体不是有效 JSON") from error

        def _json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return SupervisorHandler


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the Uni-Lab managed Runtime Supervisor.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18004)
    parser.add_argument("--runtime-prefix", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    token_group = parser.add_mutually_exclusive_group(required=True)
    token_group.add_argument("--token")
    token_group.add_argument("--token-file", type=Path)
    return parser.parse_args(argv)


def _read_token(args: argparse.Namespace) -> str:
    if args.token is not None:
        return str(args.token)
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("Supervisor token 文件为空")
    return token


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.host not in _LOOPBACK_HOSTS:
        raise SystemExit("Managed Runtime Supervisor 只允许监听 loopback")
    if not 0 < args.port <= 65535:
        raise SystemExit("Supervisor 端口必须在 1..65535")
    state_directory = args.state_dir or (Path.cwd() / "managed-runtime")
    supervisor = ManagedRuntimeSupervisor(
        runtime_prefix=args.runtime_prefix,
        state_directory=state_directory,
        token=_read_token(args),
    )
    server = ThreadingHTTPServer(
        (args.host, args.port),
        _handler_type(supervisor),
    )

    stopping = threading.Event()

    def stop_server(_signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            signal.signal(signal_value, stop_server)

    print(
        f"Managed Runtime Supervisor listening on {args.host}:{args.port}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        supervisor.close()


if __name__ == "__main__":
    main()
