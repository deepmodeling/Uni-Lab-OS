from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import request


class IsaacWorkerHealthError(RuntimeError):
    pass


@dataclass
class ManagedIsaacWorkerConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8091
    scene: str | None = None
    camera: str = "/World/Camera"
    headless: bool = True
    joint_control_ui: bool = False
    warmup_steps: int = 2
    rpc_timeout_s: float = 600.0
    start_timeout_s: float = 120.0
    conda_env: str | None = None
    conda_executable: str = "conda"
    python_executable: str = "python"
    log_path: str | None = None
    repo_root: Path = field(default_factory=Path.cwd)

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{int(self.port)}"


class ManagedIsaacWorker:
    def __init__(self, config: ManagedIsaacWorkerConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self._log_file: Any = None

    @property
    def endpoint(self) -> str:
        return self.config.endpoint

    def build_command(self) -> list[str]:
        worker_args = [
            "-m",
            "unilabos.sim.backends.isaac.worker",
            "--host",
            self.config.host,
            "--port",
            str(int(self.config.port)),
            "--headless" if self.config.headless else "--no-headless",
        ]
        if self.config.scene:
            worker_args.extend(["--scene", str(self.config.scene)])
        if self.config.joint_control_ui:
            worker_args.append("--joint-control-ui")
        worker_args.extend(
            [
                "--camera",
                self.config.camera,
                "--warmup-steps",
                str(int(self.config.warmup_steps)),
                "--rpc-timeout-s",
                str(float(self.config.rpc_timeout_s)),
            ]
        )
        if self.config.conda_env:
            return [
                self.config.conda_executable,
                "run",
                "--no-capture-output",
                "-n",
                self.config.conda_env,
                self.config.python_executable,
                *worker_args,
            ]
        return [self.config.python_executable, *worker_args]

    def build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        repo_root = str(self.config.repo_root)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = repo_root if not existing else os.pathsep.join([repo_root, existing])
        return env

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
        if self.config.log_path:
            log_file = open(self.config.log_path, "a", encoding="utf-8")
            self._log_file = log_file
            stdout = log_file
            stderr = subprocess.STDOUT
        try:
            self.process = subprocess.Popen(
                self.build_command(),
                cwd=str(self.config.repo_root),
                env=self.build_env(),
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            self.wait_until_healthy(timeout_s=self.config.start_timeout_s)
        except Exception:
            self.stop()
            raise

    def wait_until_healthy(self, timeout_s: float | None = None, interval_s: float = 1.0) -> None:
        timeout = self.config.start_timeout_s if timeout_s is None else float(timeout_s)
        deadline = time.monotonic() + timeout
        last_error: BaseException | None = None
        while time.monotonic() <= deadline:
            if self.process is not None and self.process.poll() is not None:
                raise IsaacWorkerHealthError(
                    f"Isaac worker exited before becoming healthy with code {self.process.poll()}"
                )
            try:
                payload = self._read_health()
                if payload.get("ok") is True:
                    return
            except BaseException as exc:
                last_error = exc
            time.sleep(max(0.0, float(interval_s)))
        raise IsaacWorkerHealthError(f"Isaac worker did not become healthy at {self.endpoint}: {last_error}")

    def stop(self, timeout_s: float = 10.0) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            self._terminate_process(process)
            try:
                process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self._kill_process(process)
                process.wait(timeout=timeout_s)
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def _read_health(self) -> dict[str, Any]:
        with request.urlopen(f"{self.endpoint}/health", timeout=5.0) as response:
            import json

            return dict(json.loads(response.read().decode("utf-8")))

    def _terminate_process(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError):
            process.terminate()

    def _kill_process(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            process.kill()
