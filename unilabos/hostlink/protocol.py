"""HostLink 帧与信封：NDJSON over TCP。

帧格式：每条消息一行 UTF-8 JSON，``\\n`` 结尾（NDJSON）。语言无关、可 telnet
调试；单条消息大小受 ``MAX_FRAME_BYTES`` 保护（资源树可能较大，上限放宽到 8MB）。

信封形状对齐《Cloud-Edge 通信与同步协议》里的通信准则（数据类消息
``{action_type, query_key: uuid|name, key}``），三端（前端/后端/edge）与
host-slave 共用一套语义：

请求::

    {"v": 1, "kind": "req", "id": "<uuid>", "action_type": "material",
     "query_key": "uuid", "key": "<标识>", "data": {...}}

响应::

    {"v": 1, "kind": "resp", "id": "<对应请求 id>", "ok": true, "data": {...}}
    {"v": 1, "kind": "resp", "id": "...", "ok": false, "error": "<原因>"}

心跳与握手也是普通请求（``ping`` / ``hello``），不额外引入帧类型。
``hello.data.device_ids`` 是 Slave 启动图内所有 ``type=device`` 节点的全局
唯一 ID 集合，Host 优先用它识别逻辑 Slave；旧客户端缺失时才使用 node_id / machine_name。
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any, Dict, Optional

PROTOCOL_VERSION = 1

#: 单帧上限（字节）。物料树整树查询可能较大；超限直接断开，防止内存放大攻击。
MAX_FRAME_BYTES = 8 * 1024 * 1024


class ActionType:
    """内置 action_type 常量（业务方可注册任意自定义 action）。"""

    HELLO = "hello"  # 握手：上报身份，取回 host 信息与 ROS 组网协助
    PING = "ping"  # 心跳：维持在线状态
    MATERIAL = "material"  # 物料/资源查询（query_key: uuid|id，with_children 在 data）
    DEVICE = "device"  # 设备信息查询（预留，形状同 material）
    ROS_INFO = "ros_info"  # 单独拉取 ROS 组网协助信息


class LinkError(Exception):
    """通路层错误（帧损坏 / 超限 / 连接断开 / 请求超时）。"""


class RemoteError(LinkError):
    """对端返回 ok=false 的业务错误。"""


def new_request(
    action_type: str,
    data: Optional[Dict[str, Any]] = None,
    query_key: str = "",
    key: str = "",
    request_id: str = "",
) -> Dict[str, Any]:
    msg: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "kind": "req",
        "id": request_id or uuid.uuid4().hex,
        "action_type": action_type,
    }
    if query_key:
        msg["query_key"] = query_key
    if key:
        msg["key"] = key
    if data is not None:
        msg["data"] = data
    return msg


def new_response(
    request_id: str, ok: bool, data: Any = None, error: str = ""
) -> Dict[str, Any]:
    msg: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "kind": "resp",
        "id": request_id,
        "ok": ok,
    }
    if ok:
        msg["data"] = data
    else:
        msg["error"] = error or "unknown error"
    return msg


def encode_frame(message: Dict[str, Any]) -> bytes:
    """信封 → NDJSON 帧；超限抛 LinkError（发送端自检）。"""
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
    """基于 ``recv()`` 的缓冲行读取器（``sock.makefile`` 的安全替代）。

    不能用 ``sock.makefile('rb')``：CPython 明确不支持「带 timeout 的 socket +
    makefile」组合——一次 ``socket.timeout`` 后文件对象进入不一致状态，下一次读
    抛 ``OSError: cannot read from timed out object``（实机联调 5s 心跳 >
    服务端 1s socket 超时时被击中，连接每 5s 被杀）。``recv`` 超时则无副作用：
    已收数据都在本对象的 buffer 里，超时异常向上传播，调用方可安全重试。
    """

    def __init__(self, sock: socket.socket, max_bytes: int = 0):
        self._sock = sock
        self._max = max_bytes or MAX_FRAME_BYTES
        self._buf = bytearray()
        self._eof = False

    def readline(
        self, limit: int = 0
    ) -> bytes:  # limit 兼容 file-like 签名，实际用 _max
        """读一行（含 ``\\n``）；EOF 返回 b""；空闲超时抛 socket.timeout（可重试）。"""
        while True:
            newline_at = self._buf.find(b"\n")
            if newline_at >= 0:
                line = bytes(self._buf[: newline_at + 1])
                del self._buf[: newline_at + 1]
                return line
            if len(self._buf) > self._max:
                raise LinkError(f"frame too large: >{self._max} bytes")
            if self._eof:
                if self._buf:
                    remainder = bytes(self._buf)
                    self._buf.clear()
                    return remainder  # 残帧交给 read_message 判定 truncated
                return b""
            chunk = self._sock.recv(65536)  # 超时/OSError 原样上抛，buffer 完好
            if not chunk:
                self._eof = True
                continue
            self._buf += chunk

    def close(self) -> None:  # 与 file-like 接口对齐；socket 生命周期归调用方
        self._buf.clear()


def read_message(reader) -> Optional[Dict[str, Any]]:
    """从行读取器（``LineReader`` 或任何带 ``readline`` 的对象）读一条消息。

    返回 None 表示对端正常关闭；帧超限/非 JSON 抛 LinkError（调用方应断开连接，
    不尝试在损坏的流上继续解析）。
    """
    line = reader.readline(MAX_FRAME_BYTES + 2)
    if not line:
        return None
    if len(line) > MAX_FRAME_BYTES:
        raise LinkError(f"frame too large: >{MAX_FRAME_BYTES} bytes")
    if not line.endswith(b"\n"):
        # 无换行 = 残帧（对端中途断开或超限截断）
        raise LinkError("truncated frame (no trailing newline)")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise LinkError(f"invalid json frame: {exc}") from exc
    if not isinstance(message, dict) or "kind" not in message:
        raise LinkError("invalid envelope: missing kind")
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
