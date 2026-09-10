"""ROS 组网协助：信息构建与环境变量套用（含降级形态）。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from unilabos.hostlink import ros_assist
from unilabos.hostlink.ros_assist import (
    FastDDSDiscoveryServer,
    RosNetworkInfo,
    apply_ros_network_env,
    build_host_ros_info,
    format_host_port,
    parse_host_port,
    use_connected_host,
)


class TestBuildHostInfo:
    def test_from_explicit_args(self):
        info = build_host_ros_info(
            host_ip="192.168.1.10",
            domain_id=42,
            discovery_range="OFF",
            static_peers=["192.168.1.10", "192.168.1.11"],
            environ={},
        )
        assert info.domain_id == 42
        assert info.automatic_discovery_range == "OFF"
        assert info.static_peers == ["192.168.1.10", "192.168.1.11"]

    def test_fallback_to_host_environ(self):
        env = {
            "ROS_DOMAIN_ID": "7",
            "ROS_AUTOMATIC_DISCOVERY_RANGE": "localhost",
            "ROS_STATIC_PEERS": "10.0.0.1;10.0.0.2",
            "ROS_DISCOVERY_SERVER": "10.0.0.1:11811",
        }
        info = build_host_ros_info(environ=env)
        assert info.domain_id == 7
        assert info.automatic_discovery_range == "LOCALHOST"
        assert info.static_peers == ["10.0.0.1", "10.0.0.2"]
        assert info.discovery_server == "10.0.0.1:11811"

    def test_host_ip_becomes_default_static_peer(self):
        info = build_host_ros_info(host_ip="192.168.9.9", environ={})
        assert info.static_peers == ["192.168.9.9"]

    def test_invalid_range_rejected(self):
        with pytest.raises(ValueError):
            build_host_ros_info(discovery_range="ULTRA", environ={})


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
            discovery_server_managed=True,
        )
        assert RosNetworkInfo.from_dict(info.to_dict()) == info

    def test_explicit_disable_removes_inherited_server(self):
        env = {"ROS_DISCOVERY_SERVER": "old-host:11811", "KEEP": "yes"}
        info = RosNetworkInfo(discovery_server_disabled=True)
        assert apply_ros_network_env(info, environ=env) == {}
        assert env == {"KEEP": "yes"}


class TestDirectedEndpoint:
    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            ("host.local:7302", ("host.local", 7302)),
            ("127.0.0.1:11811", ("127.0.0.1", 11811)),
            ("[::1]:11811", ("::1", 11811)),
        ],
    )
    def test_parse_host_port(self, endpoint, expected):
        assert parse_host_port(endpoint) == expected

    def test_format_ipv6_and_replace_advertised_host(self):
        assert format_host_port("::1", 7302) == "[::1]:7302"
        assert use_connected_host("192.168.50.99:7302", "10.0.0.8") == "10.0.0.8:7302"

    @pytest.mark.parametrize(
        "endpoint",
        ["", "host", "host:", ":7302", "host:0", "host:65536", "[::1]7302"],
    )
    def test_invalid_endpoint_is_rejected(self, endpoint):
        with pytest.raises(ValueError):
            parse_host_port(endpoint)


class TestDiscoveryServerWindowsLifecycle:
    def test_windows_finds_sibling_executable_with_exe_suffix(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Conda 环境中的 Fast DDS Windows 可执行文件必须带 ``.exe`` 查找。"""

        python = tmp_path / "python.exe"
        fastdds = tmp_path / "fastdds.exe"
        python.touch()
        fastdds.touch()
        monkeypatch.setattr(ros_assist, "_PLATFORM", "win32")
        monkeypatch.setattr(ros_assist.sys, "executable", str(python))
        monkeypatch.setattr(ros_assist.shutil, "which", lambda _name: None)

        assert ros_assist._discovery_command() == [str(fastdds), "discovery"]

    def test_windows_uses_native_process_termination(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows 不得调用不存在的 POSIX 进程组 API。"""

        process = Mock()
        process.poll.side_effect = [None]
        process.wait.return_value = 0
        process.terminate = Mock()
        process.kill = Mock()
        server = FastDDSDiscoveryServer("127.0.0.1", 11811)
        server.process = process
        monkeypatch.setattr(ros_assist, "_PLATFORM", "win32")
        monkeypatch.setattr(
            ros_assist.os,
            "killpg",
            Mock(side_effect=AssertionError("Windows 不应调用 killpg")),
            raising=False,
        )

        server.stop()

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=3)
        process.kill.assert_not_called()

    def test_windows_start_uses_process_group_creation_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows 启动不传 POSIX ``start_new_session``。"""

        process = Mock()
        process.poll.return_value = None
        popen = Mock(return_value=process)
        monkeypatch.setattr(ros_assist, "_PLATFORM", "win32")
        monkeypatch.setattr(ros_assist, "_discovery_command", lambda: ["fastdds.exe"])
        monkeypatch.setattr(ros_assist.subprocess, "Popen", popen)
        monkeypatch.setattr(ros_assist.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(
            ros_assist.subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            512,
            raising=False,
        )

        FastDDSDiscoveryServer("127.0.0.1", 11811).start()

        options = popen.call_args.kwargs
        assert "start_new_session" not in options
        assert options["creationflags"] == 512
