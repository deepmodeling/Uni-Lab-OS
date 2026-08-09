import pytest

from unilabos.app.main import _apply_hostlink_cli, parse_args
from unilabos.config.config import HostLinkConfig


def test_networking_cli_accepts_host_and_domain_aliases() -> None:
    args = parse_args().parse_args(
        [
            "--is_slave",
            "--host-node-ip",
            "10.0.0.9:7402",
            "--ros-domain-id",
            "52",
            "--ros-discovery-range",
            "OFF",
        ]
    )
    assert args.is_slave is True
    assert args.host_node_ip == "10.0.0.9:7402"
    assert args.ros_domain_id == 52
    assert args.ros_discovery_range == "OFF"


@pytest.mark.parametrize("domain_id", ["-1", "233"])
def test_networking_cli_domain_range_is_validated_at_startup(domain_id) -> None:
    args = parse_args().parse_args(["--ros-domain-id", domain_id])
    with pytest.raises(ValueError, match="between 0 and 232"):
        _apply_hostlink_cli(vars(args), is_slave=False)


def test_networking_cli_is_applied_after_config(monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "host", "")
    monkeypatch.setattr(HostLinkConfig, "port", 7302)
    monkeypatch.setattr(HostLinkConfig, "ros_domain_id", "")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_range", "")
    monkeypatch.delenv("ROS_DOMAIN_ID", raising=False)

    _apply_hostlink_cli(
        {
            "host_node_ip": "10.0.0.9:7402",
            "ros_domain_id": 52,
            "ros_discovery_range": "OFF",
            "ros_discovery_server": None,
        },
        is_slave=True,
    )

    assert HostLinkConfig.host == "10.0.0.9"
    assert HostLinkConfig.port == 7402
    assert HostLinkConfig.ros_domain_id == "52"
    assert HostLinkConfig.ros_discovery_range == "OFF"
