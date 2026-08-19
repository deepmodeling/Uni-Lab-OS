"""HostLink wire protocol: newline-delimited JSON over TCP.

ROS2 mode uses the control messages for assisted discovery. The standalone
HostLink backend additionally uses the same connection for device RPC/state.
"""

from __future__ import annotations

import json
import socket
import traceback
import uuid
from typing import Any, Dict, Optional

from unilabos.device_runtime.topic import message_to_value

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024


class ActionType:
    """Built-in networking actions."""

    HELLO = "hello"
    PING = "ping"
    # Slave 只经 HostLink 提交物料请求；Host 再代理到配置的 materials authority。
    MATERIAL_CREATE = "material.create"
    MATERIAL_GET_TREE = "material.tree.get"
    MATERIAL_GET_BY_RESOURCE_ID = "material.resource-id.get"
    MATERIAL_DELETE = "material.delete"
    MATERIAL_COMPARE_SNAPSHOT = "material.snapshot.compare"
    MATERIAL_APPLY_SNAPSHOT = "material.snapshot.apply"
    ROS_INFO = "ros_info"
    DEVICE_CALL = "device.call"
    DEVICE_STATE = "device.state"
    SERVICE_CALL = "service.call"
    ACTION_FEEDBACK = "action.feedback"
    ACTION_CANCEL = "action.cancel"
    TOPIC_PUBLISH = "topic.publish"
    TOPIC_SUBSCRIBE = "topic.subscribe"
    TOPIC_UNSUBSCRIBE = "topic.unsubscribe"
    TOPIC_DELIVER = "topic.deliver"


class LinkError(Exception):
    """Transport or framing error."""


class RemoteError(LinkError):
    """The remote endpoint returned ``ok=false``."""

    def __init__(
        self,
        message: str,
        error_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.error_info = dict(error_info) if isinstance(error_info, dict) else {}


def exception_error_info(exc: BaseException) -> Dict[str, Any]:
    """Build or forward structured exception identity across HostLink."""

    if isinstance(exc, RemoteError) and exc.error_info:
        info = dict(exc.error_info)
        info.setdefault("error_message", str(exc))
        return info
    info: Dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "exception_mro": [kind.__name__ for kind in type(exc).__mro__],
        "error_message": str(exc),
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }
    for key in ("category", "severity"):
        value = getattr(exc, key, None)
        if value is not None:
            info[key] = str(getattr(value, "value", value))
    return info


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
    error_info: Optional[Dict[str, Any]] = None,
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
        if error_info:
            message["error_info"] = dict(error_info)
    return message


def encode_frame(message: Dict[str, Any]) -> bytes:
    raw = (
        json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            default=message_to_value,
        ).encode("utf-8")
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
    "exception_error_info",
    "new_request",
    "new_response",
    "read_message",
    "send_message",
]
