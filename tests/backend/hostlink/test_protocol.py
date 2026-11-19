import io
from array import array

import pytest

from unilabos.backend.hostlink import protocol
from unilabos.backend.hostlink.protocol import (
    ActionType,
    LinkError,
    MAX_FRAME_BYTES,
    encode_frame,
    encode_frames,
    new_request,
    new_response,
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


def test_small_message_stays_a_single_frame() -> None:
    request = new_request(ActionType.PING, {"n": 1})
    assert encode_frames(request) == [encode_frame(request)]


def test_large_message_is_chunked_and_reassembled(monkeypatch) -> None:
    """超过单帧上限的消息切成 chunk 帧，接收端拼回原消息，上层看不到分片。

    回归：Host 把整套物料模板定义（近 10MB）回给 Slave 时单帧编码失败。载荷混入
    引号、反斜杠、中文和 4 字节 emoji，覆盖按字符切片后重新转义的膨胀情形。
    """

    monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 1024)
    blob = '含 "引号" \\ 反斜杠 😀 ' * 400
    response = new_response("req-1", True, {"templates": [blob, blob]})

    frames = encode_frames(response)

    assert len(frames) > 1
    assert all(len(frame) <= 1024 for frame in frames)
    assert all(b'"kind":"chunk"' in frame for frame in frames)
    assert read_message(io.BytesIO(b"".join(frames))) == response


def test_chunked_message_followed_by_normal_frame(monkeypatch) -> None:
    monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 1024)
    big = new_request(ActionType.DEVICE_CALL, {"blob": "x" * 5000})
    small = new_request(ActionType.PING)
    stream = io.BytesIO(b"".join(encode_frames(big)) + encode_frame(small))

    assert read_message(stream) == big
    assert read_message(stream) == small
    assert read_message(stream) is None


def test_message_over_total_limit_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 1024)
    monkeypatch.setattr(protocol, "MAX_MESSAGE_BYTES", 4096)
    with pytest.raises(LinkError, match="message too large"):
        encode_frames(new_request(ActionType.PING, {"blob": "x" * 8192}))


def test_broken_chunk_run_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 1024)
    frames = encode_frames(new_request(ActionType.PING, {"blob": "x" * 3000}))
    truncated = io.BytesIO(b"".join(frames[:-1]))
    with pytest.raises(LinkError, match="connection closed after"):
        read_message(truncated)
    interleaved = io.BytesIO(frames[0] + encode_frame(new_request(ActionType.PING)))
    with pytest.raises(LinkError, match="chunk sequence broken"):
        read_message(interleaved)
