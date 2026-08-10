"""HostLink wire protocol: newline-delimited JSON over TCP.

The first slice deliberately contains only networking control messages.  Device
actions and material/resource queries continue to use the existing ROS2 and
HTTP paths.
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any, Dict, Optional

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024


class ActionType:
    """Built-in networking actions."""

    HELLO = "hello"
    PING = "ping"
    ROS_INFO = "ros_info"


class LinkError(Exception):
    """Transport or framing error."""


class RemoteError(LinkError):
    """The remote endpoint returned ``ok=false``."""


def new_request(
    action_type: str,
    data: Optional[Dict[str, Any]] = None,
    request_id: str = "",
) -> Dict[str, Any]:
    message: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "kind": "req",
        "id": request_id or uuid.uuid4().hex,
        "action_type": action_type,
    }
    if data is not None:
        message["data"] = data
    return message


def new_response(
    request_id: str,
    ok: bool,
    data: Any = None,
    error: str = "",
) -> Dict[str, Any]:
    message: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "kind": "resp",
        "id": request_id,
        "ok": ok,
    }
    if ok:
        message["data"] = data
    else:
        message["error"] = error or "unknown error"
    return message


def encode_frame(message: Dict[str, Any]) -> bytes:
    raw = (
        json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if len(raw) > MAX_FRAME_BYTES:
        raise LinkError(f"frame too large: {len(raw)} bytes > {MAX_FRAME_BYTES}")
    return raw


def send_message(sock: socket.socket, message: Dict[str, Any]) -> None:
    sock.sendall(encode_frame(message))


class LineReader:
    """Buffered ``recv``-based line reader safe for timed sockets."""

    def __init__(self, sock: socket.socket, max_bytes: int = 0) -> None:
        self._sock = sock
        self._max = max_bytes or MAX_FRAME_BYTES
        self._buffer = bytearray()
        self._eof = False

    def readline(self, limit: int = 0) -> bytes:
        del limit
        while True:
            newline_at = self._buffer.find(b"\n")
            if newline_at >= 0:
                line = bytes(self._buffer[: newline_at + 1])
                del self._buffer[: newline_at + 1]
                return line
            if len(self._buffer) > self._max:
                raise LinkError(f"frame too large: >{self._max} bytes")
            if self._eof:
                if self._buffer:
                    remainder = bytes(self._buffer)
                    self._buffer.clear()
                    return remainder
                return b""
            chunk = self._sock.recv(65536)
            if chunk:
                self._buffer.extend(chunk)
            else:
                self._eof = True

    def close(self) -> None:
        self._buffer.clear()


def read_message(reader: Any) -> Optional[Dict[str, Any]]:
    line = reader.readline(MAX_FRAME_BYTES + 2)
    if not line:
        return None
    if len(line) > MAX_FRAME_BYTES:
        raise LinkError(f"frame too large: >{MAX_FRAME_BYTES} bytes")
    if not line.endswith(b"\n"):
        raise LinkError("truncated frame (no trailing newline)")
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinkError(f"invalid json frame: {exc}") from exc
    if not isinstance(message, dict) or message.get("kind") not in {"req", "resp"}:
        raise LinkError("invalid envelope")
    return message


__all__ = [
    "ActionType",
    "LineReader",
    "LinkError",
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "RemoteError",
    "encode_frame",
    "new_request",
    "new_response",
    "read_message",
    "send_message",
]
