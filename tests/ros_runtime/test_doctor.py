"""doctor 诊断：TCP 探测、组网解析、探测统计（无 ROS）+ 可选 rclpy 回环全链路。"""

import threading
import time

import pytest

from unilabos.networking.ros_doctor import (
    ProbeStats,
    make_probe_message,
    parse_probe_message,
    resolve_ros_network,
    run_doctor,
)


class TestResolveRosNetwork:
    def test_manual_peers_default_to_unicast_off(self):
        info, source = resolve_ros_network(peers="10.0.0.1, 10.0.0.2", ros_domain_id=7)
        assert info.static_peers == ["10.0.0.1", "10.0.0.2"]
        assert info.domain_id == 7
        assert info.automatic_discovery_range == "OFF"  # 指定对端未指定档位 → 收紧单播
        assert "manual peers" in source






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


class TestRunDoctor:



    def test_unknown_role(self):
        assert run_doctor({"doctor_command": "nope"}) == 1


class TestRosLoopback:
    """真实 rclpy 回环：talker/listener 全链路（LOCALHOST 发现，域号隔离）。"""

    def test_talker_listener_end_to_end(self, monkeypatch):
        rclpy = pytest.importorskip("rclpy")
        from unilabos.networking.ros_discovery import RosNetworkInfo
        from unilabos.networking.ros_doctor import run_listener, run_talker

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
