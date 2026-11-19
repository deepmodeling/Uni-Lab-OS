"""工作站负责构造子设备，Host 必须同步登记每层子设备的路由/动作/锁。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from unilabos.backend.ros2.presets.host_node import HostNode
from unilabos.server.backend.capabilities import build_endpoint_capabilities


class Host:
    _register_local_device = HostNode._register_local_device

    def __init__(self):
        self.devices_instances = {}
        self.devices_names = {}
        self.device_machine_names = {}
        self._action_value_mappings = {}
        self._action_clients = {}
        self._online_devices = set()
        self._report_action_locks_free = Mock()
        self.lab_logger = Mock()


def device(name, children=None):
    return SimpleNamespace(
        _ros_node=SimpleNamespace(
            namespace=f"/devices/{name}",
            registry_name=f"{name}_template",
            _action_value_mappings={"probe": {"type": "UniLabJsonCommand", "goal_default": {"coil": 0}}},
            sub_devices=children or {},
        )
    )


def test_nested_subdevices_publish_routes_actions_and_locks_without_dds_discovery():
    host = Host()
    sensor = device("sensor")
    station = device("station", {"inner": device("inner", {"sensor": sensor}), "bus": device("bus")})
    host._register_local_device("station", station)
    assert set(host.devices_instances) == {"station", "inner", "sensor", "bus"}
    routes, capabilities = build_endpoint_capabilities(host, observed_at_ms=1000)
    assert {route.device_uuid for route in routes} == set(host.devices_instances)
    assert {cap.device_uuid for cap in capabilities} == set(host.devices_instances)
    assert next(route for route in routes if route.device_uuid == "sensor").config["registry_name"] == "sensor_template"
    assert host.devices_instances["sensor"] is sensor
    assert host._report_action_locks_free.call_count == 4
    host._register_local_device("station", station)
    assert host._report_action_locks_free.call_count == 4


def test_subdevice_cannot_overwrite_another_local_instance():
    host = Host()
    original = device("sensor")
    host._register_local_device("sensor", original)
    with pytest.raises(ValueError, match="身份重复"):
        host._register_local_device("sensor", device("sensor"))
    assert host.devices_instances["sensor"] is original
