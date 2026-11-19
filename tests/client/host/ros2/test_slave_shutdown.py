"""ROS 2 Slave：等 Host 的 /node_info_update 服务时不再一声不吭。"""

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
