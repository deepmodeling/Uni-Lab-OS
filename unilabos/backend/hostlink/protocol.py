"""HostLink wire protocol: newline-delimited JSON over TCP.

ROS2 mode uses the control messages for assisted discovery. The standalone
HostLink backend additionally uses the same connection for device RPC/state.

一帧就是一行 JSON。单帧上限 ``MAX_FRAME_BYTES``；超过的消息（整套物料模板定义、
大物料树快照）由 ``encode_frames`` 切成若干 chunk 连续发送：每个 chunk 是一行
``kind="chunk"`` 头（带 ``len``）加紧随其后的 ``len`` 字节原始 JSON 正文，正文不再
二次转义。``read_message`` 重组后再交给上层，上层只见 ``req`` / ``resp``。整条消息
上限 ``MAX_MESSAGE_BYTES``。
"""

from __future__ import annotations

import json
import socket
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional

from unilabos.backend.hostlink.topic import message_to_value
from unilabos.utils import logger

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_BYTES = 256 * 1024 * 1024


class ActionType:
    """Built-in networking actions."""

    HELLO = "hello"
    PING = "ping"
    LOG_READ = "process.log.read"
    LOG_CHANGED = "process.log.changed"
    # Slave 只经 HostLink 提交物料请求；Host 再代理到配置的 materials authority。
    MATERIAL_TEMPLATE_LIST = "material.template.list"
    MATERIAL_TEMPLATE_CREATE = "material.template.create"
    MATERIAL_CREATE = "material.create"
    MATERIAL_GET_TREE = "material.tree.get"
    MATERIAL_GET_BY_RESOURCE_ID = "material.resource-id.get"
    MATERIAL_SEARCH = "material.search"
    MATERIAL_DATA_PUT = "material.data.put"
    MATERIAL_MOVE = "material.move"
    MATERIAL_TRANSFER = "material.transfer"
    MATERIAL_DELETE = "material.delete"
    MATERIAL_COMPARE_SNAPSHOT = "material.snapshot.compare"
    MATERIAL_APPLY_SNAPSHOT = "material.snapshot.apply"
    MATERIAL_APPLY_DELTA = "material.delta.apply"
    # Host → 设备的物料/管理下行链路（不建 ROS service，本进程直调 / 跨机走 HostLink）。
    RESOURCE_TREE_SYNC = "resource.tree.sync"
    RESOURCE_APPEND = "resource.append"
    MATERIAL_SYNC = "material.sync"
    DEVICE_MANAGE = "device.manage"
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


#: Host 启动中、请求所需的状态尚未就绪时应答里的 ``error_info.code``。
HOST_NOT_READY = "host_not_ready"


class HostNotReadyError(RemoteError):
    """Host 仍在启动，请求所需的状态（物料权威 / 本机设备）尚未就绪。

    Host 在执行处理器之前就回这个应答，请求没有产生任何副作用，所以客户端可以原样重试；
    客户端重试到等待上限仍未就绪时抛出本异常。
    """

    def __init__(
        self,
        message: str,
        error_info: Optional[Dict[str, Any]] = None,
        *,
        stage: str = "",
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(message, error_info)
        self.stage = str(stage or self.error_info.get("stage") or "")
        self.retry_after = max(0.0, float(retry_after or 0.0))


def exception_error_info(exc: BaseException) -> Dict[str, Any]:
    """Build or forward structured exception identity across HostLink."""

    if isinstance(exc, RemoteError) and exc.error_info:
        info = dict(exc.error_info)
        info.setdefault("error_message", str(exc))
        return info
    to_error_info = getattr(exc, "to_error_info", None)
    structured = to_error_info() if callable(to_error_info) else {}
    info: Dict[str, Any] = {
        # 超时异常自带结构化身份（timeout_seconds、driver_still_running 等）
        **(structured if isinstance(structured, dict) else {}),
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


def new_not_ready_response(
    request_id: str,
    action_type: str,
    stage: str,
    retry_after: float,
) -> Dict[str, Any]:
    """Host 启动中的拒绝应答：处理器尚未执行，对端可以原样重试。

    重试标记 ``not_ready`` 放在应答顶层而不是 ``error_info`` 里：``error_info`` 会随
    嵌套调用的失败原样转发（``exception_error_info``），顶层字段不会，所以客户端只会
    重试 Host 门控本身拒绝的请求，不会把已经执行过一半的嵌套动作再跑一遍。
    """

    message = new_response(
        request_id,
        False,
        error=f"Host 正在启动，{action_type} 需等待 {stage} 就绪，请稍后重试",
        error_info={
            "code": HOST_NOT_READY,
            "stage": stage,
            "exception_type": "HostNotReadyError",
        },
    )
    message["not_ready"] = stage
    message["retry_after"] = float(retry_after)
    return message


def response_error(response: Dict[str, Any]) -> RemoteError:
    """把 ``ok=false`` 应答转成异常；Host 启动中的拒绝转成 :class:`HostNotReadyError`。"""

    message = str(response.get("error") or "remote error")
    error_info = response.get("error_info")
    stage = response.get("not_ready")
    if stage:
        return HostNotReadyError(
            message,
            error_info,
            stage=str(stage),
            retry_after=float(response.get("retry_after") or 0.0),
        )
    return RemoteError(message, error_info)


def _dumps(message: Dict[str, Any]) -> str:
    return json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
        default=message_to_value,
    )


def encode_frame(message: Dict[str, Any]) -> bytes:
    """Encode one message as exactly one frame; raises when it does not fit."""

    raw = _dumps(message).encode("utf-8") + b"\n"
    if len(raw) > MAX_FRAME_BYTES:
        raise LinkError(f"frame too large: {len(raw)} bytes > {MAX_FRAME_BYTES}")
    return raw


_CHUNK_HEADER_RESERVE = 256


def encode_frames(message: Dict[str, Any]) -> List[bytes]:
    """Encode a message as one frame, or as a run of chunks when it is too big.

    每个返回项都是一次 ``sendall`` 的内容：普通帧就是一行；chunk 则是头行加正文，
    头行 ``len`` 告诉接收端正文有多少字节。正文是原始 JSON 字节切片，按字节切开也
    没关系——接收端先把所有正文拼回来再解码。
    """

    raw = _dumps(message).encode("utf-8")
    if len(raw) + 1 <= MAX_FRAME_BYTES:
        return [raw + b"\n"]
    if len(raw) > MAX_MESSAGE_BYTES:
        raise LinkError(f"message too large: {len(raw)} bytes > {MAX_MESSAGE_BYTES}")
    step = max(1, MAX_FRAME_BYTES - _CHUNK_HEADER_RESERVE)
    bodies = [raw[offset : offset + step] for offset in range(0, len(raw), step)]
    transfer_id = uuid.uuid4().hex
    frames: List[bytes] = []
    for index, body in enumerate(bodies):
        header = encode_frame(
            {
                "v": PROTOCOL_VERSION,
                "kind": "chunk",
                "id": transfer_id,
                "seq": index,
                "n": len(bodies),
                "len": len(body),
            }
        )
        frames.append(header + body)
    return frames


def send_message(sock: socket.socket, message: Dict[str, Any]) -> None:
    """Send one message; callers hold the socket's write lock so chunk runs stay contiguous."""

    for frame in encode_frames(message):
        sock.sendall(frame)


def send_response_or_reject(
    send: Callable[[Dict[str, Any]], None],
    request: Dict[str, Any],
    response: Dict[str, Any],
    peer: str,
) -> None:
    """发送一条应答；应答连分片都装不下（超过 MAX_MESSAGE_BYTES）时改发明确的错误应答。

    否则 LinkError 只会在执行线程里被吞掉，对端等到超时都不知道原因——分片之前整套
    物料模板定义（近 10MB > 8MB 单帧）就是这样把 Slave 的 ``material.template.list``
    拖到 request timeout 的。
    """

    try:
        send(response)
    except LinkError as exc:
        action = str(request.get("action_type") or "")
        logger.warning("[HostLink] %s 的应答无法发给 %s: %s", action, peer, exc)
        send(
            new_response(
                str(request.get("id") or ""),
                False,
                error=f"response to {action} exceeds the HostLink message limit: {exc}",
            )
        )


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
            self._fill()

    def read(self, size: int) -> bytes:
        """Return exactly ``size`` bytes, or fewer only when the peer closed the socket."""

        while len(self._buffer) < size and not self._eof:
            self._fill()
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def _fill(self) -> None:
        chunk = self._sock.recv(65536)
        if chunk:
            self._buffer.extend(chunk)
        else:
            self._eof = True

    def close(self) -> None:
        self._buffer.clear()


def _read_frame(reader: Any) -> Optional[Dict[str, Any]]:
    line = reader.readline(MAX_FRAME_BYTES + 2)
    if not line:
        return None
    if len(line) > MAX_FRAME_BYTES:
        raise LinkError(f"frame too large: >{MAX_FRAME_BYTES} bytes")
    if not line.endswith(b"\n"):
        raise LinkError("truncated frame (no trailing newline)")
    try:
        frame = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinkError(f"invalid json frame: {exc}") from exc
    if not isinstance(frame, dict):
        raise LinkError("invalid envelope")
    return frame


def _validate_envelope(message: Any) -> Dict[str, Any]:
    if not isinstance(message, dict) or message.get("kind") not in {"req", "resp"}:
        raise LinkError("invalid envelope")
    return message


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _read_chunked(reader: Any, first: Dict[str, Any]) -> Dict[str, Any]:
    """Reassemble a contiguous run of chunks started by header ``first``."""

    transfer_id = first.get("id")
    total = first.get("n")
    if not _positive_int(total) or total < 1:
        raise LinkError("invalid chunk header")
    parts: List[bytes] = []
    received = 0
    header: Optional[Dict[str, Any]] = first
    for expected in range(total):
        if header is None:
            raise LinkError(f"connection closed after {expected}/{total} chunks")
        length = header.get("len")
        if (
            header.get("kind") != "chunk"
            or header.get("id") != transfer_id
            or header.get("seq") != expected
            or not _positive_int(length)
            or length > MAX_FRAME_BYTES
        ):
            raise LinkError(f"chunk sequence broken at {expected}/{total}")
        received += length
        if received > MAX_MESSAGE_BYTES:
            raise LinkError(f"message too large: >{MAX_MESSAGE_BYTES} bytes")
        body = reader.read(length)
        if len(body) != length:
            raise LinkError(f"truncated chunk body at {expected}/{total}")
        parts.append(body)
        if expected + 1 < total:
            header = _read_frame(reader)
    try:
        return _validate_envelope(json.loads(b"".join(parts)))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinkError(f"invalid json in chunked message: {exc}") from exc


def read_message(reader: Any) -> Optional[Dict[str, Any]]:
    """Return the next ``req``/``resp`` message, transparently reassembling chunk runs."""

    frame = _read_frame(reader)
    if frame is None:
        return None
    if frame.get("kind") == "chunk":
        return _read_chunked(reader, frame)
    return _validate_envelope(frame)


__all__ = [
    "ActionType",
    "HOST_NOT_READY",
    "HostNotReadyError",
    "LineReader",
    "LinkError",
    "MAX_FRAME_BYTES",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "RemoteError",
    "encode_frame",
    "encode_frames",
    "exception_error_info",
    "new_not_ready_response",
    "new_request",
    "new_response",
    "read_message",
    "response_error",
    "send_message",
    "send_response_or_reject",
]
