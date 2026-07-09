from pathlib import Path

import pytest

from unilabos.sim.backends.isaac import managed
from unilabos.sim.backends.isaac.managed import (
    IsaacWorkerHealthError,
    ManagedIsaacWorker,
    ManagedIsaacWorkerConfig,
)


def test_config_derives_endpoint_from_host_and_port():
    config = ManagedIsaacWorkerConfig(enabled=True, host="127.0.0.1", port=8092)

    assert config.endpoint == "http://127.0.0.1:8092"


def test_plain_python_worker_command_includes_scene_and_camera():
    config = ManagedIsaacWorkerConfig(
        enabled=True,
        host="127.0.0.1",
        port=8092,
        scene="/tmp/lab.usd",
        camera="/World/DemoCamera",
        headless=True,
        python_executable="/opt/unilab/bin/python",
        repo_root=Path("/repo"),
        rpc_timeout_s=600.0,
    )

    command = ManagedIsaacWorker(config).build_command()

    assert command == [
        "/opt/unilab/bin/python",
        "-m",
        "unilabos.sim.backends.isaac.worker",
        "--host",
        "127.0.0.1",
        "--port",
        "8092",
        "--headless",
        "--scene",
        "/tmp/lab.usd",
        "--camera",
        "/World/DemoCamera",
        "--warmup-steps",
        "2",
        "--rpc-timeout-s",
        "600.0",
    ]


def test_conda_worker_command_matches_4090_matterix_shape():
    config = ManagedIsaacWorkerConfig(
        enabled=True,
        host="127.0.0.1",
        port=8092,
        scene="/home/ubuntu/labsim/LabUtopia_repro/assets/chemistry_lab/lab_001/lab_001.usd",
        conda_env="matterix",
        conda_executable="/home/ubuntu/miniforge3/bin/conda",
        python_executable="python",
        repo_root=Path("/tmp/Uni-Lab-OS-phase2-c3c5"),
    )

    command = ManagedIsaacWorker(config).build_command()

    assert command[:6] == [
        "/home/ubuntu/miniforge3/bin/conda",
        "run",
        "--no-capture-output",
        "-n",
        "matterix",
        "python",
    ]
    assert command[6:8] == ["-m", "unilabos.sim.backends.isaac.worker"]
    assert "--scene" in command


def test_worker_command_includes_joint_control_ui_when_enabled():
    config = ManagedIsaacWorkerConfig(
        enabled=True,
        joint_control_ui=True,
        python_executable="/opt/unilab/bin/python",
        repo_root=Path("/repo"),
    )

    command = ManagedIsaacWorker(config).build_command()

    assert "--joint-control-ui" in command


def test_worker_environment_prepends_repo_root_to_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/existing")
    config = ManagedIsaacWorkerConfig(enabled=True, repo_root=Path("/repo"))

    env = ManagedIsaacWorker(config).build_env()

    assert env["PYTHONPATH"] == "/repo:/existing"


def test_wait_until_healthy_retries_until_health_ok():
    class TestWorker(ManagedIsaacWorker):
        def __init__(self, config):
            super().__init__(config)
            self.calls = 0

        def _read_health(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("not ready")
            return {"ok": True}

    worker = TestWorker(ManagedIsaacWorkerConfig(enabled=True))
    worker.process = _FakeProcess()

    worker.wait_until_healthy(timeout_s=1.0, interval_s=0.0)

    assert worker.calls == 2


def test_wait_until_healthy_raises_when_process_exits():
    worker = ManagedIsaacWorker(ManagedIsaacWorkerConfig(enabled=True))
    worker.process = _FakeProcess(returncode=7)

    with pytest.raises(IsaacWorkerHealthError, match="exited before becoming healthy"):
        worker.wait_until_healthy(timeout_s=1.0, interval_s=0.0)


def test_stop_terminates_worker_process_group(monkeypatch):
    calls = []
    process = _FakeProcess()
    worker = ManagedIsaacWorker(ManagedIsaacWorkerConfig(enabled=True))
    worker.process = process
    monkeypatch.setattr(managed.os, "killpg", lambda pid, signal_number: calls.append((pid, signal_number)))

    worker.stop()

    assert calls == [(process.pid, managed.signal.SIGTERM)]
    assert process.waited is True


class _FakeProcess:
    def __init__(self, returncode=None):
        self.pid = 1234
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode
