import io

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


def test_truncated_frame_is_rejected() -> None:
    with pytest.raises(LinkError, match="truncated"):
        read_message(io.BytesIO(b'{"kind":"req"}'))


def test_oversized_frame_is_rejected() -> None:
    with pytest.raises(LinkError, match="too large"):
        encode_frame({"kind": "req", "payload": "x" * MAX_FRAME_BYTES})
