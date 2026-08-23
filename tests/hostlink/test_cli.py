import pytest

from unilabos.app.cli.parser import build_parser
from unilabos.config.config import HostLinkConfig
from unilabos.hostlink.startup import apply_hostlink_cli
from unilabos.hostlink.startup import (
    HEATING_DEMO_EDGE_URL,
    HEATING_DEMO_GRAPH,
    HEATING_DEMO_HTTP_PORT,
    configure_heating_demo_args,
)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--port-management", 8100),
        ("--port_management", 8200),
        ("--port", 8300),
    ],
)
def test_management_port_accepts_semantic_name_and_short_alias(option, value) -> None:
    args = build_parser().parse_args([option, str(value)])

    assert args.port_management == value


def test_disable_browser_can_be_combined_with_management_port() -> None:
    args = build_parser().parse_args(
        ["--port-management", "8100", "--disable-browser"]
    )

    assert args.port_management == 8100
    assert args.disable_browser is True


def test_heating_demo_mode_starts_local_host_microbackend() -> None:
    args = vars(build_parser().parse_args(["--demo-mode"]))

    configure_heating_demo_args(args)

    assert args["backend"] == "hostlink"
    assert args["is_slave"] is False
    assert args["slave_no_host"] is False
    assert args["test_mode"] is True
    assert args["external_devices_only"] is True
    assert args["graph"] == str(HEATING_DEMO_GRAPH)
    assert args["port_management"] == HEATING_DEMO_HTTP_PORT
    assert args["host_node_ip"] == ""
    assert args["hostlink_port"] is None
    assert HEATING_DEMO_HTTP_PORT == 6005
    assert HEATING_DEMO_EDGE_URL == "https://edge.whalent.com"


def test_heating_demo_mode_preserves_explicit_port_and_graph(tmp_path) -> None:
    graph = tmp_path / "custom.json"
    args = vars(
        build_parser().parse_args(
            [
                "--demo-mode",
                "--graph",
                str(graph),
                "--port",
                "29005",
            ]
        )
    )

    configure_heating_demo_args(args)

    assert args["graph"] == str(graph)
    assert args["port_management"] == 29005


def test_networking_cli_accepts_host_and_domain_aliases() -> None:
    args = build_parser().parse_args(
        [
            "--is_slave",
            "--host-node-ip",
            "10.0.0.9:7402",
            "--hostlink-port",
            "7502",
            "--ros-domain-id",
            "52",
            "--ros-discovery-range",
            "OFF",
            "--ros-static-peers",
            "10.0.0.9;10.0.0.10",
            "--ros-discovery-port",
            "7600",
        ]
    )
    assert args.is_slave is True
    assert args.host_node_ip == "10.0.0.9:7402"
    assert args.hostlink_port == 7502
    assert args.ros_domain_id == 52
    assert args.ros_discovery_range == "OFF"
    assert args.ros_static_peers == "10.0.0.9;10.0.0.10"
    assert args.ros_discovery_port == 7600


@pytest.mark.parametrize("option", ["--is_slave", "--is-slave"])
def test_slave_role_accepts_dash_and_underscore(option) -> None:
    args = build_parser().parse_args([option])
    assert args.is_slave is True


@pytest.mark.parametrize("option", ["--slave_no_host", "--slave-no-host"])
def test_slave_offline_mode_accepts_dash_and_underscore(option) -> None:
    args = build_parser().parse_args([option])
    assert args.slave_no_host is True


@pytest.mark.parametrize("domain_id", ["-1", "233"])
def test_networking_cli_domain_range_is_validated_at_startup(domain_id) -> None:
    args = build_parser().parse_args(["--ros-domain-id", domain_id])
    with pytest.raises(ValueError, match="between 0 and 232"):
        apply_hostlink_cli(vars(args), is_slave=False)


def test_networking_cli_is_applied_after_config(monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "host", "")
    monkeypatch.setattr(HostLinkConfig, "port", 7302)
    monkeypatch.setattr(HostLinkConfig, "ros_domain_id", "")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_range", "")
    monkeypatch.delenv("ROS_DOMAIN_ID", raising=False)

    apply_hostlink_cli(
        {
            "host_node_ip": "10.0.0.9:7402",
            "ros_domain_id": 52,
            "ros_discovery_range": "OFF",
            "ros_discovery_server": None,
            "ros_discovery_port": None,
        },
        is_slave=True,
    )

    assert HostLinkConfig.host == "10.0.0.9"
    assert HostLinkConfig.port == 7402
    assert HostLinkConfig.ros_domain_id == "52"
    assert HostLinkConfig.ros_discovery_range == "OFF"


def test_detailed_hostlink_cli_overrides(monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "host", "")
    monkeypatch.setattr(HostLinkConfig, "port", 7302)
    monkeypatch.setattr(HostLinkConfig, "bind", "0.0.0.0")
    monkeypatch.setattr(HostLinkConfig, "advertise_ip", "")
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 5.0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 15.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 5.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 10.0)
    monkeypatch.setattr(HostLinkConfig, "ros_static_peers", "")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_port", 0)
    monkeypatch.setattr(HostLinkConfig, "ros_assist_apply", True)

    apply_hostlink_cli(
        {
            "host_node_ip": "10.0.0.9:7402",
            "hostlink_port": 7502,
            "hostlink_bind": "127.0.0.1",
            "hostlink_advertise_ip": "10.0.0.8",
            "disable_hostlink": True,
            "hostlink_heartbeat_interval": 2.5,
            "hostlink_heartbeat_timeout": 8,
            "hostlink_connect_timeout": 3,
            "hostlink_request_timeout": 6,
            "ros_domain_id": None,
            "ros_discovery_range": None,
            "ros_static_peers": "10.0.0.8;10.0.0.9",
            "ros_discovery_server": None,
            "ros_discovery_port": 7600,
            "no_ros_assist": True,
        },
        is_slave=True,
    )

    assert HostLinkConfig.host == "10.0.0.9"
    assert HostLinkConfig.port == 7502  # 显式端口覆盖 host-node-ip 中的兼容端口
    assert HostLinkConfig.bind == "127.0.0.1"
    assert HostLinkConfig.advertise_ip == "10.0.0.8"
    assert HostLinkConfig.enable is False
    assert HostLinkConfig.heartbeat_interval == 2.5
    assert HostLinkConfig.heartbeat_timeout == 8.0
    assert HostLinkConfig.connect_timeout == 3.0
    assert HostLinkConfig.request_timeout == 6.0
    assert HostLinkConfig.ros_static_peers == "10.0.0.8;10.0.0.9"
    assert HostLinkConfig.ros_discovery_port == 7600
    assert HostLinkConfig.ros_assist_apply is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"hostlink_port": 0},
        {"hostlink_port": 65536},
        {"hostlink_connect_timeout": 0},
        {"ros_discovery_port": -1},
        {"ros_discovery_port": 65536},
    ],
)
def test_invalid_hostlink_cli_values_are_rejected(overrides) -> None:
    with pytest.raises(ValueError):
        apply_hostlink_cli(overrides, is_slave=False)
