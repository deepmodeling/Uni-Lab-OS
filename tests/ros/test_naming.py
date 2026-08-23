import pytest

from unilabos.ros.naming import (
    ros_device_namespace,
    ros_device_node_name,
    ros_device_path,
    ros_name_segment,
)


def test_hyphenated_logical_id_gets_ros_safe_wire_identity() -> None:
    assert ros_device_node_name("virtual-heater") == "virtual_heater"
    assert ros_device_namespace("virtual-heater") == "/devices/virtual_heater"


def test_nested_device_path_preserves_valid_segments() -> None:
    assert ros_device_path("/devices/workstation-1/pump_2") == ("workstation_1/pump_2")
    assert ros_device_node_name("workstation-1/pump_2") == "pump_2"


def test_ros_name_segment_handles_numeric_and_other_invalid_characters() -> None:
    assert ros_name_segment("3-way.valve") == "_3_way_valve"


@pytest.mark.parametrize("value", ["", "/", "/devices/", "parent//child"])
def test_blank_ros_device_path_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        ros_device_path(value)
