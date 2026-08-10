import pytest

from unilabos.hostlink.ros_assist import (
    RosNetworkInfo,
    apply_ros_network_env,
    build_host_ros_info,
    parse_host_target,
    validate_domain_id,
)


def test_host_policy_uses_domain_and_advertised_ip() -> None:
    info = build_host_ros_info(
        host_ip="10.20.0.5",
        domain_id=37,
        discovery_range="OFF",
        environ={},
    )
    assert info.domain_id == 37
    assert info.static_peers == ["10.20.0.5"]
    assert info.automatic_discovery_range == "OFF"


def test_slave_applies_host_policy_before_ros_init() -> None:
    environ = {"ROS_DISCOVERY_SERVER": "stale:11811"}
    applied = apply_ros_network_env(
        RosNetworkInfo(
            domain_id=12,
            automatic_discovery_range="LOCALHOST",
            static_peers=["10.0.0.8"],
            discovery_server_disabled=True,
        ),
        environ,
    )
    assert applied == {
        "ROS_DOMAIN_ID": "12",
        "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
        "ROS_STATIC_PEERS": "10.0.0.8",
    }
    assert "ROS_DISCOVERY_SERVER" not in environ


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.0.0.8", ("10.0.0.8", 7302)),
        ("host.local:7400", ("host.local", 7400)),
        ("[fe80::1]:7500", ("fe80::1", 7500)),
        ("fe80::1", ("fe80::1", 7302)),
    ],
)
def test_parse_host_target(value, expected) -> None:
    assert parse_host_target(value, 7302) == expected


def test_parse_host_target_rejects_non_numeric_port() -> None:
    with pytest.raises(ValueError, match="invalid HostNode address"):
        parse_host_target("host.local:not-a-port", 7302)


@pytest.mark.parametrize("value", [-1, 233])
def test_domain_id_portable_range(value: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 232"):
        validate_domain_id(value)
