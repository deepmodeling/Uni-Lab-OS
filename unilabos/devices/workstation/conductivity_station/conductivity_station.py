"""电导率自动化工站 TCP 客户端及 UniLab 动作。

合作方工站作为 TCP 服务端，UniLab 作为客户端。报文为 UTF-8 JSON，使用
CRLF 分帧。查询类动作允许在连接中断后重连重试一次；启动、停止和手动运行
属于有副作用的命令，发送结果不明确时不会自动重发，避免重复执行。
"""

from __future__ import annotations

import json
import socket
import threading
from itertools import count
from typing import Any

from unilabos.registry.decorators import action, device


class ConductivityStationTransportError(RuntimeError):
    """TCP 连接或报文传输失败。"""


class ConductivityStationProtocolError(RuntimeError):
    """服务端响应不符合电导工站协议。"""


@device(
    id="conductivity_station",
    category=["workstation", "conductivity"],
    displayname="电导率自动化测试工站",
    description="通过 TCP JSON/CRLF 协议控制电导率自动化测试工站",
    version="1.0.0",
)
class ConductivityStation:
    """电导工站长连接客户端。"""

    def __init__(
        self,
        ip: str = "127.0.0.1",
        port: int = 19091,
        connect_timeout: float = 5.0,
        response_timeout: float = 10.0,
        max_message_bytes: int = 4194304,
        encoding: str = "utf-8",
        frame_delimiter: str = "\\r\\n",
        station_action_names: dict[str, str] | None = None,
        **_: Any,
    ) -> None:
        self.ip = str(ip)
        self.port = int(port)
        self.connect_timeout = float(connect_timeout)
        self.response_timeout = float(response_timeout)
        self.max_message_bytes = int(max_message_bytes)
        self.encoding = str(encoding).strip() or "utf-8"
        delimiter_text = (
            str(frame_delimiter).replace("\\r", "\r").replace("\\n", "\n")
        )
        self.frame_delimiter = delimiter_text.encode(self.encoding)
        if not self.frame_delimiter:
            raise ValueError("frame_delimiter 不能为空")
        self.station_action_names = {
            str(key): str(value)
            for key, value in (station_action_names or {}).items()
            if str(key) and str(value)
        }
        self.status = "idle"
        self._sock: socket.socket | None = None
        self._recv_buffer = bytearray()
        self._request_ids = count(1)
        self._lock = threading.RLock()

    def close(self) -> None:
        """关闭当前 TCP 连接。"""
        with self._lock:
            self._disconnect()

    def _disconnect(self) -> None:
        sock, self._sock = self._sock, None
        self._recv_buffer.clear()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _connect(self) -> socket.socket:
        if self._sock is None:
            try:
                sock = socket.create_connection(
                    (self.ip, self.port), timeout=self.connect_timeout
                )
                sock.settimeout(self.response_timeout)
                self._sock = sock
            except OSError as exc:
                self._disconnect()
                raise ConductivityStationTransportError(
                    f"无法连接电导工站 {self.ip}:{self.port}: {exc}"
                ) from exc
        return self._sock

    def _read_frame(self, sock: socket.socket) -> bytes:
        delimiter = self.frame_delimiter
        while True:
            marker = self._recv_buffer.find(delimiter)
            if marker >= 0:
                frame = bytes(self._recv_buffer[:marker])
                del self._recv_buffer[: marker + len(delimiter)]
                return frame
            if len(self._recv_buffer) > self.max_message_bytes:
                raise ConductivityStationProtocolError(
                    f"响应超过最大长度 {self.max_message_bytes} 字节"
                )
            try:
                chunk = sock.recv(65536)
            except (OSError, socket.timeout) as exc:
                raise ConductivityStationTransportError(
                    f"等待电导工站响应失败: {exc}"
                ) from exc
            if not chunk:
                raise ConductivityStationTransportError("电导工站在响应前关闭连接")
            self._recv_buffer.extend(chunk)

    def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        sock = self._connect()
        encoded = json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        ).encode(self.encoding) + self.frame_delimiter
        if len(encoded) > self.max_message_bytes:
            raise ConductivityStationProtocolError(
                f"请求超过最大长度 {self.max_message_bytes} 字节"
            )
        try:
            sock.sendall(encoded)
            frame = self._read_frame(sock)
        except Exception:
            self._disconnect()
            raise
        try:
            response = json.loads(frame.decode(self.encoding))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._disconnect()
            raise ConductivityStationProtocolError(
                f"响应不是有效 {self.encoding} JSON: {exc}"
            ) from exc
        if not isinstance(response, dict):
            raise ConductivityStationProtocolError("响应必须是 JSON 对象")
        if response.get("request_id") != request["request_id"]:
            raise ConductivityStationProtocolError(
                "响应 request_id 不匹配: "
                f"期望 {request['request_id']}，实际 {response.get('request_id')}"
            )
        if "result" not in response:
            raise ConductivityStationProtocolError("响应缺少 result 字段")
        return response

    def _request(
        self,
        action_name: str,
        param: dict[str, Any] | None = None,
        *,
        retry_read: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            request: dict[str, Any] = {
                "request_id": next(self._request_ids),
                "action": self.station_action_names.get(action_name, action_name),
            }
            if param is not None:
                request["param"] = param
            attempts = 2 if retry_read else 1
            for attempt in range(attempts):
                try:
                    return self._exchange(request)
                except ConductivityStationTransportError:
                    if attempt + 1 >= attempts:
                        raise
                    self._disconnect()
            raise AssertionError("unreachable")

    @action(always_free=True, description="查询电导工站各机构在线状态")
    def station_status(self) -> dict[str, Any]:
        return self._request("station_status", retry_read=True)

    @action(always_free=True, description="查询料瓶、模具和漏斗占位情况")
    def material_status(self) -> dict[str, Any]:
        return self._request("material_status", retry_read=True)

    @action(always_free=True, description="查询当前电导测试批次和执行进度")
    def batch_status(self) -> dict[str, Any]:
        return self._request("batch_status", retry_read=True)

    @action(always_free=True, description="按批次号查询样品测试结果")
    def batch_result(self, batch_id: str) -> dict[str, Any]:
        if not str(batch_id).strip():
            raise ValueError("batch_id 不能为空")
        return self._request(
            "batch_result", {"batch_id": str(batch_id).strip()}, retry_read=True
        )

    @action(description="上料完成后启动整批自动测试")
    def start_batch(self) -> dict[str, Any]:
        return self._request("start_batch")

    @action(description="当前样品完成后停止整批任务")
    def stop_current_batch(self) -> dict[str, Any]:
        return self._request("stop_current_batch")

    @action(description="切换手动模式并单独执行指定步骤")
    def manual_run(self, step: int) -> dict[str, Any]:
        step_value = int(step)
        if not 1 <= step_value <= 13:
            raise ValueError("step 必须在 1 到 13 之间")
        return self._request("manual_run", {"step": step_value})

    @action(description="清理非运行状态下的当前批次（动作名待合作方最终确认）")
    def clear_current_batch(self) -> dict[str, Any]:
        return self._request("clear_current_batch")
