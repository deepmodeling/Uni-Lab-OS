import io
from array import array

import pytest

from unilabos.hostlink.protocol import (
    ActionType,
    LinkError,
    MAX_FRAME_BYTES,
    encode_frame,
    new_request,
    read_message,
)


def test_request_round_trip() -> None:
    request = new_request(ActionType.HELLO, {"device_ids": ["pump-1"]})
    assert read_message(io.BytesIO(encode_frame(request))) == request


class RosPoint:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z

    @staticmethod
    def get_fields_and_field_types() -> dict[str, str]:
        return {"x": "double", "y": "double", "z": "double"}


class RosPayload:
    def __init__(self) -> None:
        self.name = "中文设备"
        self.point = RosPoint(1.0, 2.0, 3.0)
        self.samples = array("f", [0.5, 1.5])

    @staticmethod
    def get_fields_and_field_types() -> dict[str, str]:
        return {
            "name": "string",
            "point": "geometry_msgs/Point",
            "samples": "sequence<float>",
        }


def test_ros_message_arguments_are_encoded_as_utf8_json() -> None:
    request = new_request(
        ActionType.DEVICE_CALL,
        {"arguments": {"payload": RosPayload()}},
    )

    decoded = read_message(io.BytesIO(encode_frame(request)))

    assert decoded["data"]["arguments"]["payload"] == {
        "name": "中文设备",
        "point": {"x": 1.0, "y": 2.0, "z": 3.0},
        "samples": pytest.approx([0.5, 1.5]),
    }


def test_truncated_frame_is_rejected() -> None:
    with pytest.raises(LinkError, match="truncated"):
        read_message(io.BytesIO(b'{"kind":"req"}'))


def test_oversized_frame_is_rejected() -> None:
    with pytest.raises(LinkError, match="too large"):
        encode_frame({"kind": "req", "payload": "x" * MAX_FRAME_BYTES})
