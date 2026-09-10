"""HostLink 客户端（slave 侧）：组网、在线监控、请求通道。

组网：配置 Host 微后端的 ip:port（HostLinkConfig / --hostlink_addr），
``start()`` 后台线程维持连接：connect → hello 握手 → 周期 ping 心跳；
断线指数退避自动重连。``online`` 随时可查，状态变化回调 ``on_status_change``。

请求通道：``request(action_type, ...)`` 同步等响应（按消息 id 关联，支持并发
调用）；物料查询封装为 ``get_resource()``，返回与旧云端接口一致的
raw dict 列表，设备端零改动换源。

进程级单例：Slave 微后端 ``set_hostlink_client()`` 注册后，设备节点可用
``get_hostlink_client()`` 查询 Host 物料；设备 Action 始终由 HostNode 走 ROS2。
"""

from __future__ import annotations

import socket
import threading
import time
import uuid as uuid_mod
from typing import Any, Callable, Dict, Iterable, List, Optional

from unilabos.hostlink.protocol import (
    ActionType,
    LineReader,
    LinkError,
    PROTOCOL_VERSION,
    RemoteError,
    new_request,
    new_response,
    read_message,
    send_message,
)
from unilabos.hostlink.ros_assist import RosNetworkInfo
from unilabos.utils import logger
from unilabos.utils.tracing import (
    extract_trace_context,
    inject_trace_context,
    record_exception,
    span,
)

InboundHandler = Callable[[Dict[str, Any]], Any]


class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[Dict[str, Any]] = None


class HostLinkClient:
    """与 host 的长连接客户端；线程安全，可并发 request。"""

    def __init__(
        self,
        host: str,
        port: int,
        machine_name: str = "",
        heartbeat_interval: float = 5.0,
        connect_timeout: float = 5.0,
        request_timeout: float = 10.0,
        reconnect_max_backoff: float = 10.0,
        on_status_change: Optional[Callable[[bool], None]] = None,
        node_id: str = "",
        capabilities: Optional[List[str]] = None,
        device_ids: Optional[Iterable[str]] = None,
    ):
        self.host = host
        self.port = port
        self.machine_name = machine_name
        self.heartbeat_interval = heartbeat_interval
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.reconnect_max_backoff = reconnect_max_backoff
        self.on_status_change = on_status_change
        self._identity_lock = threading.Lock()
        self._fallback_node_id = (
            str(node_id or "").strip()
            or str(machine_name or "").strip()
            or f"slave-{uuid_mod.uuid4().hex}"
        )
        self.device_ids: List[str] = []
        self.node_id = self._fallback_node_id
        self.configure_device_ids(device_ids or [])
        self.capabilities = list(
            ("material-query", "ros-assist") if capabilities is None else capabilities
        )

        self._sock: Optional[socket.socket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._manager_thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._pending: Dict[str, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._handlers: Dict[str, InboundHandler] = {}
        self._handlers_lock = threading.Lock()
        self._stop = threading.Event()
        self._online = threading.Event()
        self._status_condition = threading.Condition()
        #: hello 响应缓存（含 ros 组网协助）
        self.hello_info: Dict[str, Any] = {}

    # ── 生命周期 ─────────────────────────────────────────────

    def start(self) -> "HostLinkClient":
        """启动后台连接管理线程（非阻塞）。"""
        if self._manager_thread is not None and self._manager_thread.is_alive():
            return self
        self._stop.clear()
        self._manager_thread = threading.Thread(
            target=self._run, name="hostlink-client", daemon=True
        )
        self._manager_thread.start()
        return self

    def connect_blocking(self, timeout: Optional[float] = 10.0) -> bool:
        """启动并等待首次上线；``None`` 表示一直等到 Host 可用。"""
        self.start()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._status_condition:
            while not self._online.is_set() and not self._stop.is_set():
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._status_condition.wait(remaining)
        return self._online.is_set()

    def close(self) -> None:
        self._stop.set()
        with self._status_condition:
            self._status_condition.notify_all()
        self._teardown_socket()
        if self._manager_thread is not None:
            self._manager_thread.join(timeout=3)

    @property
    def online(self) -> bool:
        return self._online.is_set()

    def configure_device_ids(self, device_ids: Iterable[str]) -> None:
        """Use the unique startup device IDs as this Slave's logical identity.

        This is called before the connection manager starts.  Keeping the full
        sorted set in hello lets Host distinguish multiple Slave processes on
        one machine and recognize the same Slave after its TCP source port
        changes.  Empty-device utility clients retain the legacy node fallback.
        """

        normalized = sorted(
            {
                str(device_id).strip()
                for device_id in device_ids
                if str(device_id).strip()
            }
        )
        if not normalized:
            return
        with self._identity_lock:
            self.device_ids = normalized
            self.node_id = f"device:{normalized[0]}"

    def register_handler(
        self,
        action_type: str,
        handler: InboundHandler,
        *,
        capability: str = "",
    ) -> None:
        """Register a Host -> Slave RPC handler on the existing long connection.

        The reverse direction is reserved for control-plane extensions.  Device
        Action traffic deliberately remains on the HostNode/ROS2 path.
        """

        with self._handlers_lock:
            self._handlers[action_type] = handler
        if capability and capability not in self.capabilities:
            self.capabilities.append(capability)
        if self.online:
            threading.Thread(
                target=self._refresh_hello,
                daemon=True,
                name="hostlink-refresh-hello",
            ).start()

    # ── 请求通道 ─────────────────────────────────────────────

    def request(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        query_key: str = "",
        key: str = "",
        timeout: Optional[float] = None,
    ) -> Any:
        if action_type in (ActionType.PING, ActionType.HELLO):
            return self._request(action_type, data, query_key, key, timeout)
        with span(
            "hostlink.request",
            kind="client",
            attributes={
                "rpc.system": "hostlink",
                "rpc.method": action_type,
                "server.address": self.host,
                "server.port": self.port,
            },
        ):
            return self._request(action_type, data, query_key, key, timeout)

    def _request(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        query_key: str = "",
        key: str = "",
        timeout: Optional[float] = None,
    ) -> Any:
        """发送请求并同步等待响应 data；离线/超时抛 LinkError，业务失败抛 RemoteError。"""
        sock = self._sock
        if sock is None or not self._online.is_set():
            raise LinkError(f"hostlink offline (host={self.host}:{self.port})")
        message = new_request(action_type, data=data, query_key=query_key, key=key)
        inject_trace_context(message)
        pending = _Pending()
        request_id = message["id"]
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            with self._write_lock:
                send_message(sock, message)
            if not pending.event.wait(timeout or self.request_timeout):
                raise LinkError(f"request timeout: {action_type} ({request_id[:8]})")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        response = pending.response or {}
        if not response.get("ok"):
            raise RemoteError(str(response.get("error") or "remote error"))
        return response.get("data")

    def get_resource(
        self,
        uuid: Optional[str] = None,
        res_id: Optional[str] = None,
        with_children: bool = True,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """物料/资源查询：返回扁平 raw dict 列表（与旧云端接口同形状）。"""
        data = self.request(
            ActionType.MATERIAL,
            data={"uuid": uuid, "id": res_id, "with_children": with_children},
            query_key="uuid" if uuid else "id",
            key=uuid or res_id or "",
            timeout=timeout,
        )
        nodes = (data or {}).get("nodes")
        return list(nodes or [])

    def ros_info(self, timeout: Optional[float] = None) -> RosNetworkInfo:
        """拉取 host 的 ROS 组网协助信息（hello 后也可单独刷新）。"""
        data = self.request(ActionType.ROS_INFO, timeout=timeout)
        return RosNetworkInfo.from_dict((data or {}).get("ros") or data)

    def hello_ros_info(self) -> RosNetworkInfo:
        """从 hello 缓存读 ROS 组网信息（connect_blocking 成功后可用）。"""
        return RosNetworkInfo.from_dict(self.hello_info.get("ros"))

    def _identity_payload(self) -> Dict[str, Any]:
        with self._identity_lock:
            node_id = self.node_id
            device_ids = list(self.device_ids)
        return {
            "node_id": node_id,
            "device_ids": device_ids,
            "machine_name": self.machine_name,
            "role": "slave",
            "pid": None,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": list(self.capabilities),
        }

    def _refresh_hello(self) -> None:
        try:
            data = self.request(ActionType.HELLO, data=self._identity_payload())
            self.hello_info = dict(data or {})
        except (LinkError, RemoteError):
            # The connection manager will send the latest capabilities on the
            # next reconnect.  A transient refresh failure must not kill it.
            logger.debug("[HostLink] capability hello refresh failed")

    # ── 内部：连接管理 ────────────────────────────────────────

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                self._connect_once()
                backoff = 0.5  # 连上即重置退避
                self._heartbeat_loop()
            except (OSError, LinkError) as exc:
                logger.debug(f"[HostLink] connection cycle ended: {exc}")
            self._set_online(False)
            self._teardown_socket()
            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self.reconnect_max_backoff)

    def _connect_once(self) -> None:
        sock = socket.create_connection(
            (self.host, self.port), timeout=self.connect_timeout
        )
        sock.settimeout(None)  # 读超时交给 reader 线程阻塞读
        self._sock = sock
        self._reader_thread = threading.Thread(
            target=self._read_loop, args=(sock,), name="hostlink-reader", daemon=True
        )
        self._reader_thread.start()
        # 握手（直接走 pending 机制之前，需要 online 未置位也能发）
        message = new_request(
            ActionType.HELLO,
            data=self._identity_payload(),
        )
        pending = _Pending()
        with self._pending_lock:
            self._pending[message["id"]] = pending
        try:
            with self._write_lock:
                send_message(sock, message)
            if not pending.event.wait(self.connect_timeout):
                raise LinkError("hello timeout")
            response = pending.response or {}
        finally:
            with self._pending_lock:
                self._pending.pop(message["id"], None)
        if not response.get("ok"):
            raise LinkError(f"hello rejected: {response.get('error')}")
        self.hello_info = dict(response.get("data") or {})
        assigned_node_id = str(self.hello_info.get("assigned_node_id") or "").strip()
        if assigned_node_id:
            with self._identity_lock:
                self.node_id = assigned_node_id
        self._set_online(True)
        logger.info(f"[HostLink] connected to {self.host}:{self.port}")

    def _heartbeat_loop(self) -> None:
        """周期 ping；失败/超时视为断线，交回 _run 重连。

        ping 超时用 request_timeout（而非发送周期）：服务端连接内已并发分发，
        正常负载下 ping 秒回；宽超时只兜底半开 TCP（对端悄然消失）的检测。
        """
        while not self._stop.is_set():
            if self._stop.wait(self.heartbeat_interval):
                return
            self.request(ActionType.PING, timeout=self.request_timeout)

    def _read_loop(self, sock: socket.socket) -> None:
        reader = LineReader(sock)  # 见 protocol.LineReader：makefile 与 timeout 不兼容
        try:
            while True:
                message = read_message(reader)
                if message is None:
                    break
                kind = message.get("kind")
                if kind == "resp":
                    request_id = str(message.get("id") or "")
                    with self._pending_lock:
                        pending = self._pending.get(request_id)
                    if pending is not None:
                        pending.response = message
                        pending.event.set()
                elif kind == "req":
                    threading.Thread(
                        target=self._serve_incoming,
                        args=(sock, message),
                        daemon=True,
                        name="hostlink-inbound-request",
                    ).start()
        except (LinkError, OSError):
            pass
        finally:
            reader.close()
            self._set_online(False)
            # 唤醒所有等待者（以离线错误收场，避免卡满超时）
            with self._pending_lock:
                for pending in self._pending.values():
                    if pending.response is None:
                        pending.response = {"ok": False, "error": "connection closed"}
                    pending.event.set()

    def _serve_incoming(
        self, sock: socket.socket, message: Dict[str, Any]
    ) -> None:
        request_id = str(message.get("id") or "")
        action_type = str(message.get("action_type") or "")
        raw_data = message.get("data") or {}
        data = raw_data if isinstance(raw_data, dict) else {}
        with self._handlers_lock:
            handler = self._handlers.get(action_type)
        if handler is None:
            response = new_response(
                request_id,
                False,
                error=f"unknown action_type on slave: {action_type}",
            )
        else:
            parent = extract_trace_context(message)
            with span(
                "hostlink.handle",
                kind="server",
                parent_context=parent,
                attributes={
                    "rpc.system": "hostlink",
                    "rpc.method": action_type,
                },
            ):
                try:
                    result = handler(dict(data))
                except Exception as exc:  # noqa: BLE001 - wire errors use ok=false
                    record_exception(exc)
                    logger.exception(
                        "[HostLink] Slave handler %s failed", action_type
                    )
                    response = new_response(request_id, False, error=str(exc))
                else:
                    response = new_response(request_id, True, data=result)
                inject_trace_context(response)
        try:
            with self._write_lock:
                if sock is self._sock:
                    send_message(sock, response)
        except OSError:
            pass

    def _teardown_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _set_online(self, value: bool) -> None:
        changed = value != self._online.is_set()
        if value:
            self._online.set()
        else:
            self._online.clear()
        with self._status_condition:
            self._status_condition.notify_all()
        if changed and self.on_status_change is not None:
            try:
                self.on_status_change(value)
            except Exception:  # noqa: BLE001 - 回调故障不影响通路
                logger.exception("[HostLink] on_status_change callback failed")


# ── 进程级单例（slave 主流程注册，设备节点取用） ───────────────

_client_lock = threading.Lock()
_client: Optional[HostLinkClient] = None


def set_hostlink_client(client: Optional[HostLinkClient]) -> None:
    global _client
    with _client_lock:
        _client = client


def get_hostlink_client() -> Optional[HostLinkClient]:
    with _client_lock:
        return _client


__all__ = ["HostLinkClient", "get_hostlink_client", "set_hostlink_client"]
