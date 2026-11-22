"""ROS 2 Slave：等 Host 服务时不再一声不吭；SIGTERM 与 SIGINT 一样有序退出（退出码 0）。"""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from unilabos.client.host.ros2 import main_slave_run


def test_waiting_for_host_service_warns_with_discovery_endpoint(monkeypatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(main_slave_run.logger, "warning", lambda message, *_a, **_kw: warnings.append(message))
    monkeypatch.setattr(main_slave_run.rclpy, "ok", lambda *_a, **_kw: True)
    monkeypatch.setenv("ROS_DOMAIN_ID", "42")
    monkeypatch.setenv("ROS_DISCOVERY_SERVER", "127.0.0.1:7302")
    answers = iter([False, False, True])
    timeouts: list[float] = []

    class Client:
        def wait_for_service(self, timeout_sec=None):
            timeouts.append(timeout_sec)
            return next(answers)

    assert main_slave_run.wait_for_host_node_info_service(Client(), interval=10.0) is True
    assert timeouts == [10.0, 10.0, 10.0]
    assert len(warnings) == 2
    assert "10s" in warnings[0] and "20s" in warnings[1]
    assert "ROS_DISCOVERY_SERVER=127.0.0.1:7302" in warnings[0]
    assert "ROS_DOMAIN_ID=42" in warnings[0]


def test_waiting_for_host_service_returns_when_rclpy_shuts_down(monkeypatch) -> None:
    state = {"ok": True}
    monkeypatch.setattr(main_slave_run.rclpy, "ok", lambda *_a, **_kw: state["ok"])

    class Client:
        def wait_for_service(self, timeout_sec=None):
            # 停机信号关闭 rclpy 上下文后，服务探测抛 "context is invalid"
            state["ok"] = False
            raise RuntimeError("rcl node's context is invalid")

    assert main_slave_run.wait_for_host_node_info_service(Client(), interval=0.01) is False


_SLAVE_PROCESS = textwrap.dedent(
    """
    import threading

    import rclpy

    from unilabos.app import lifetime
    from unilabos.app.runtime_startup import _join_backend_until_stopped
    from unilabos.client.host.ros2 import main_slave_run


    def backend():
        rclpy.init()
        node = rclpy.create_node("slave_shutdown_probe")
        print("READY", flush=True)
        main_slave_run.spin_until_shutdown(poll_s=0.05)
        main_slave_run.shutdown_ros_runtime([node])
        print("CLEAN", rclpy.ok(), flush=True)


    # 与 Slave 启动顺序一致：先装停机处理器，再在 runtime 线程里 rclpy.init
    lifetime.install_stop_signal_handlers("Slave")
    thread = threading.Thread(target=backend, daemon=True)
    thread.start()
    try:
        _join_backend_until_stopped(thread)
    except BaseException as exc:
        lifetime.exit_now(lifetime.exit_code_for(exc))
    lifetime.exit_now(0)
    """
)


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有可投递的 SIGTERM")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_stop_signal_exits_cleanly_with_code_zero(tmp_path, signum) -> None:
    """rclpy 接管 SIGTERM 后只转给原来的处理器；没装处理器时 Slave 既不退出也不处理回调。"""

    script = tmp_path / "slave_process.py"
    script.write_text(_SLAVE_PROCESS, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "ROS_DISCOVERY_SERVER"}
    env.update({"ROS_DOMAIN_ID": "117", "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST", "PYTHONUNBUFFERED": "1"})
    process = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, encoding="utf-8", errors="replace"
    )
    try:
        deadline = time.monotonic() + 30
        lines: list[str] = []
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line)
            if line.strip() == "READY":
                break
        assert lines and lines[-1].strip() == "READY", "".join(lines)
        process.send_signal(signum)
        output, _ = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, output
    assert "CLEAN False" in output, output
