"""帧与信封：NDJSON 编解码、超限与损坏帧防护。"""

import io

import pytest

from unilabos.hostlink import protocol
from unilabos.hostlink.protocol import (
    LinkError,
    encode_frame,
    new_request,
    new_response,
    read_message,
)


def _reader(*frames: bytes) -> io.BufferedReader:
    return io.BufferedReader(io.BytesIO(b"".join(frames)))


class TestFraming:
    def test_round_trip(self):
        req = new_request("material", data={"uuid": "u-1"}, query_key="uuid", key="u-1")
        got = read_message(_reader(encode_frame(req)))
        assert got == req
        assert got["v"] == protocol.PROTOCOL_VERSION
        assert got["kind"] == "req"

    def test_response_shapes(self):
        ok = new_response("rid", True, data={"nodes": []})
        err = new_response("rid", False, error="boom")
        assert ok["ok"] and ok["data"] == {"nodes": []}
        assert not err["ok"] and err["error"] == "boom"

    def test_eof_returns_none(self):
        assert read_message(_reader()) is None

    def test_multiple_frames_in_stream(self):
        a = new_request("ping")
        b = new_request("ping")
        reader = _reader(encode_frame(a), encode_frame(b))
        assert read_message(reader)["id"] == a["id"]
        assert read_message(reader)["id"] == b["id"]

    def test_bad_json_raises(self):
        with pytest.raises(LinkError):
            read_message(_reader(b"{not json}\n"))

    def test_missing_kind_raises(self):
        with pytest.raises(LinkError):
            read_message(_reader(b'{"v":1}\n'))

    def test_truncated_frame_raises(self):
        with pytest.raises(LinkError):
            read_message(_reader(b'{"kind":"req"'))  # 无换行 = 残帧

    def test_oversize_frame_rejected_on_send(self, monkeypatch):
        monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 64)
        with pytest.raises(LinkError):
            encode_frame(new_request("material", data={"blob": "x" * 128}))
