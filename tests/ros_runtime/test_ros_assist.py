"""ROS 组网协助：信息构建与环境变量套用（含降级形态）。"""

from __future__ import annotations

from unilabos.networking.ros_discovery import (
    RosNetworkInfo,
    apply_ros_network_env,
)


class TestApplyEnv:
    def test_degraded_unicast_form(self):
        """降级形态：关闭组播自动发现，仅与 host 单播互发现。"""
        env = {}
        info = RosNetworkInfo(
            domain_id=42, automatic_discovery_range="OFF", static_peers=["192.168.1.10"]
        )
        applied = apply_ros_network_env(info, environ=env)
        assert env == applied == {
            "ROS_DOMAIN_ID": "42",
            "ROS_AUTOMATIC_DISCOVERY_RANGE": "OFF",
            "ROS_STATIC_PEERS": "192.168.1.10",
        }

    def test_discovery_server_form(self):
        env = {}
        info = RosNetworkInfo(discovery_server="192.168.1.10:11811")
        apply_ros_network_env(info, environ=env)
        assert env == {"ROS_DISCOVERY_SERVER": "192.168.1.10:11811"}

    def test_empty_info_touches_nothing(self):
        env = {"ROS_DOMAIN_ID": "1"}
        applied = apply_ros_network_env(RosNetworkInfo(), environ=env)
        assert applied == {}
        assert env == {"ROS_DOMAIN_ID": "1"}

    def test_round_trip_serialization(self):
        info = RosNetworkInfo(
            domain_id=3,
            automatic_discovery_range="LOCALHOST",
            static_peers=["a", "b"],
            discovery_server="s:1",
        )
        assert RosNetworkInfo.from_dict(info.to_dict()) == info

    def test_explicit_disable_removes_inherited_server(self):
        env = {"ROS_DISCOVERY_SERVER": "old-host:11811", "KEEP": "yes"}
        info = RosNetworkInfo(discovery_server_disabled=True)
        assert apply_ros_network_env(info, environ=env) == {}
        assert env == {"KEEP": "yes"}
