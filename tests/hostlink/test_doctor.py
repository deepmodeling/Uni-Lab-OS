"""doctor 诊断：TCP 探测、组网解析、探测统计（无 ROS）+ 可选 rclpy 回环全链路。"""

import threading
import time

import pytest

from unilabos.hostlink.doctor import (
    ProbeStats,
    make_probe_message,
    parse_probe_message,
    probe_network,
    resolve_ros_network,
    run_doctor,
)
from unilabos.hostlink.server import HostLinkServer


@pytest.fixture()
def server():
    srv = HostLinkServer(bind="127.0.0.1", port=0, heartbeat_timeout=2.0).start()
    srv.hello_payload = {
        "host_name": "host-t",
        "ros": {"domain_id": 42, "static_peers": ["192.168.1.10"],
                "automatic_discovery_range": "OFF", "discovery_server": ""},
    }
    yield srv
    srv.stop()


class TestProbeNetwork:
    def test_ok_report_with_hello_and_ping(self, server):
        report = probe_network("127.0.0.1", server.port, ping_count=3)
        assert report["verdict"] == "ok"
        assert report["tcp_connect"]["ok"] is True
        assert report["hello"]["ok"] is True
        assert report["hello"]["host_name"] == "host-t"
        assert report["hello"]["ros"]["domain_id"] == 42
        assert report["ping"]["ok"] == 3
        assert report["ping"]["avg_ms"] is not None

    def test_tcp_fail_on_dead_port(self):
        report = probe_network("127.0.0.1", 1, ping_count=1, timeout=0.5)
        assert report["verdict"] == "tcp_fail"
        assert report["tcp_connect"]["ok"] is False
        assert "error" in report


class TestResolveRosNetwork:
    def test_manual_peers_default_to_unicast_off(self):
        info, source = resolve_ros_network(peers="10.0.0.1, 10.0.0.2", ros_domain_id=7)
        assert info.static_peers == ["10.0.0.1", "10.0.0.2"]
        assert info.domain_id == 7
        assert info.automatic_discovery_range == "OFF"  # 指定对端未指定档位 → 收紧单播
        assert "manual peers" in source

    def test_hostlink_source_pulls_hello_ros(self, server):
        info, source = resolve_ros_network(hostlink_addr=f"127.0.0.1:{server.port}")
        assert info.domain_id == 42
        assert info.static_peers == ["192.168.1.10"]
        assert source.startswith("hostlink 127.0.0.1")

    def test_managed_discovery_uses_proven_hostlink_address(self, server):
        server.hello_payload["ros"].update(
            {
                "discovery_server": "192.168.99.9:7302",
                "discovery_server_managed": True,
            }
        )
        info, _ = resolve_ros_network(hostlink_addr=f"127.0.0.1:{server.port}")
        assert info.discovery_server == "127.0.0.1:7302"

    def test_manual_overrides_hostlink(self, server):
        info, _ = resolve_ros_network(
            hostlink_addr=f"127.0.0.1:{server.port}",
            peers="10.9.9.9",
            ros_domain_id=8,
            discovery="LOCALHOST",
        )
        assert info.static_peers == ["10.9.9.9"]
        assert info.domain_id == 8
        assert info.automatic_discovery_range == "LOCALHOST"

    def test_unreachable_hostlink_falls_back(self):
        info, source = resolve_ros_network(hostlink_addr="127.0.0.1:1", peers="10.0.0.3")
        assert info.static_peers == ["10.0.0.3"]
        assert "unreachable" in source


class TestProbeStats:
    def test_loss_duplicate_latency_tracking(self):
        stats = ProbeStats()
        now = time.time()
        for seq in (1, 2, 5, 5):  # 3/4 丢失，5 重复
            message = parse_probe_message(make_probe_message(seq, "t1"))
            stats.add(message, now=now)
        assert stats.received == 3
        assert stats.lost == 2
        assert stats.duplicates == 1
        assert stats.ok
        summary = stats.summary()
        assert summary["senders"] == ["t1"]
        assert summary["latency_ms"]["avg"] is not None

    def test_multi_sender_independent_sequences(self):
        stats = ProbeStats()
        for sender, seq in (("a", 1), ("b", 1), ("a", 2), ("b", 3)):
            stats.add({"seq": seq, "sender": sender, "sent_at": time.time()})
        assert stats.received == 4
        assert stats.lost == 1  # 仅 b 的 2 丢失

    def test_parse_rejects_garbage(self):
        assert parse_probe_message("not json") is None
        assert parse_probe_message('{"no_seq": 1}') is None


class TestRunDoctorNet:
    def test_net_ok_exit_zero(self, server, capsys):
        code = run_doctor({"doctor_command": "net",
                           "hostlink_addr": f"127.0.0.1:{server.port}", "count": 2})
        assert code == 0
        assert '"verdict": "ok"' in capsys.readouterr().out

    def test_net_requires_addr(self):
        assert run_doctor({"doctor_command": "net"}) == 1

    def test_net_fail_exit_two(self):
        assert run_doctor({"doctor_command": "net", "hostlink_addr": "127.0.0.1:1"}) == 2

    def test_unknown_role(self):
        assert run_doctor({"doctor_command": "nope"}) == 1


class TestRosLoopback:
    """真实 rclpy 回环：talker/listener 全链路（LOCALHOST 发现，域号隔离）。"""

    def test_talker_listener_end_to_end(self, monkeypatch):
        rclpy = pytest.importorskip("rclpy")
        from unilabos.hostlink.doctor import run_listener, run_talker
        from unilabos.hostlink.ros_assist import RosNetworkInfo

        monkeypatch.setenv("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")
        info = RosNetworkInfo(domain_id=77, automatic_discovery_range="LOCALHOST")
        results = {}

        def listen():
            results["listener"] = run_listener(
                info, "test", topic="/unilab_doctor_test", duration_s=6.0, quiet=True
            )

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        time.sleep(1.0)  # 等 listener 订阅建立
        results["talker"] = run_talker(
            info, "test", topic="/unilab_doctor_test", rate_hz=10.0, duration_s=3.0
        )
        thread.join(timeout=15)
        assert results.get("talker") == 0
        assert results.get("listener") == 0  # 0 = 收到探测消息

        if rclpy.ok():
            rclpy.shutdown()
