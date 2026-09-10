"""HostLink 服务端（Edge 微后端 Host 侧）：TCP 监听 + peer 注册 + 心跳在线监控。

线程模型与本仓库其余部分一致（多线程、无 asyncio）：
``ThreadingTCPServer`` 每连接一个处理线程，逐行读请求、同步调 handler、
写回响应。请求处理是无状态纯函数，天然支持多 slave 并发。

在线监控：
- slave 连接后先发 ``hello`` 注册身份（全局唯一 device_ids 优先，machine_name
  兼容回退），随后按
  ``heartbeat_interval`` 发 ``ping``；
- 服务端记录每个 peer 的 ``last_seen``；``peers()`` 按
  ``last_seen + heartbeat_timeout`` 现算 online（无独立清扫线程，无竞态）；
- 连接断开即离线（保留最后一次记录供排查）。
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from unilabos.hostlink.protocol import (
    ActionType,
    LineReader,
    LinkError,
    RemoteError,
    new_request,
    new_response,
    read_message,
    send_message,
)
from unilabos.utils import logger
from unilabos.utils.tracing import (
    extract_trace_context,
    inject_trace_context,
    record_exception,
    span,
)

#: handler 签名：(data, peer_info) -> 响应 data；抛异常 = ok=false
Handler = Callable[[Dict[str, Any], Dict[str, Any]], Any]


class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[Dict[str, Any]] = None


class _PeerConnection:
    """One live Slave socket, including Host -> Slave pending requests."""

    def __init__(
        self,
        peer_key: str,
        sock: socket.socket,
        write_lock: threading.Lock,
    ) -> None:
        self.peer_key = peer_key
        self.sock = sock
        self.write_lock = write_lock
        self.pending: Dict[str, _Pending] = {}
        self.pending_lock = threading.Lock()
        self.closed = threading.Event()

    def request(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]],
        timeout: Optional[float],
    ) -> Any:
        if self.closed.is_set():
            raise LinkError(f"slave connection closed: {self.peer_key}")
        message = new_request(action_type, data=data)
        inject_trace_context(message)
        pending = _Pending()
        request_id = str(message["id"])
        with self.pending_lock:
            self.pending[request_id] = pending
        try:
            with self.write_lock:
                send_message(self.sock, message)
            if not pending.event.wait(timeout):
                raise LinkError(
                    f"request timeout: {action_type} ({request_id[:8]})"
                )
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)
        response = pending.response or {}
        if not response.get("ok"):
            raise RemoteError(str(response.get("error") or "remote error"))
        return response.get("data")

    def resolve(self, message: Dict[str, Any]) -> None:
        request_id = str(message.get("id") or "")
        with self.pending_lock:
            pending = self.pending.get(request_id)
        if pending is not None:
            pending.response = message
            pending.event.set()

    def close(self) -> None:
        self.closed.set()
        with self.pending_lock:
            for pending in self.pending.values():
                if pending.response is None:
                    pending.response = {
                        "ok": False,
                        "error": "slave connection closed",
                    }
                pending.event.set()


class _LinkTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    link: "HostLinkServer"  # start() 时注入


class _LinkRequestHandler(socketserver.BaseRequestHandler):
    """每连接一个线程；self.server 是 _LinkTCPServer，业务状态经 .link 访问。"""

    def handle(self) -> None:  # noqa: D102 - socketserver 接口
        link: HostLinkServer = self.server.link  # type: ignore[attr-defined]
        sock: socket.socket = self.request
        sock.settimeout(link.socket_timeout)
        # 不能用 sock.makefile：带 timeout 的 socket 上一次超时后文件对象即损坏
        # （"cannot read from timed out object"）；LineReader 基于 recv，超时可安全重试
        reader = LineReader(sock)
        peer_key = f"{self.client_address[0]}:{self.client_address[1]}"
        # 单连接内每请求独立线程分发：慢 handler（大物料树查询）不得阻塞
        # 后续请求——尤其是心跳 ping，否则会被误判离线。写回共用一把锁防交错。
        write_lock = threading.Lock()
        connection = link.register_connection(peer_key, sock, write_lock)

        def _serve_one(message: Dict[str, Any]) -> None:
            response = self._dispatch(link, message, peer_key)
            link.bind_connection(peer_key)
            try:
                with write_lock:
                    send_message(sock, response)
            except OSError:
                pass  # 连接已断，读循环会退出并标记离线

        try:
            while not link.stopping.is_set():
                try:
                    message = read_message(reader)
                except LinkError as exc:
                    logger.warning(f"[HostLink] {peer_key} bad frame, closing: {exc}")
                    break
                except (socket.timeout, TimeoutError):
                    continue  # 空闲连接继续等；在线判定交给 last_seen
                except OSError:
                    break
                if message is None:
                    break  # 对端正常关闭
                kind = message.get("kind")
                if kind == "req":
                    threading.Thread(
                        target=_serve_one,
                        args=(message,),
                        daemon=True,
                        name=f"hostlink-req-{peer_key}",
                    ).start()
                elif kind == "resp":
                    connection.resolve(message)
        finally:
            reader.close()
            link.unregister_connection(peer_key)
            link.mark_disconnected(peer_key)

    @staticmethod
    def _dispatch(
        link: "HostLinkServer", message: Dict[str, Any], peer_key: str
    ) -> Dict[str, Any]:
        request_id = str(message.get("id") or "")
        action = str(message.get("action_type") or "")
        data = message.get("data") or {}
        peer = link.touch_peer(peer_key, action, message)
        handler = link.handlers.get(action)
        if handler is None:
            return new_response(
                request_id, False, error=f"unknown action_type: {action}"
            )
        if action in (ActionType.PING, ActionType.HELLO):
            try:
                result = handler(dict(data), peer)
            except Exception as exc:  # noqa: BLE001 - 业务异常统一转 ok=false
                logger.warning(
                    f"[HostLink] handler {action} failed for {peer_key}: {exc}"
                )
                return new_response(request_id, False, error=str(exc))
            return new_response(request_id, True, data=result)

        parent = extract_trace_context(message)
        with span(
            "hostlink.handle",
            kind="server",
            parent_context=parent,
            attributes={
                "rpc.system": "hostlink",
                "rpc.method": action,
            },
        ):
            try:
                result = handler(dict(data), peer)
            except Exception as exc:  # noqa: BLE001 - 业务异常统一转 ok=false
                record_exception(exc)
                logger.warning(
                    f"[HostLink] handler {action} failed for {peer_key}: {exc}"
                )
                response = new_response(request_id, False, error=str(exc))
            else:
                response = new_response(request_id, True, data=result)
            inject_trace_context(response)
            return response


class HostLinkServer:
    """微后端 Host 侧通路。``start()`` 后台监听；``stop()`` 幂等关闭。"""

    def __init__(
        self,
        bind: str = "0.0.0.0",
        port: int = 7302,
        heartbeat_timeout: float = 15.0,
        socket_timeout: float = 1.0,
    ):
        self._bind = bind
        self._port = port
        self.heartbeat_timeout = heartbeat_timeout
        self.socket_timeout = socket_timeout
        self.handlers: Dict[str, Handler] = {}
        self.stopping = threading.Event()
        # Logical peers are keyed by the Slave-provided stable ``node_id``.
        # Socket source ports change on every reconnect, so they are only a
        # connection index and must never create duplicate Slave rows.
        self._peers: Dict[str, Dict[str, Any]] = {}
        self._connection_nodes: Dict[str, str] = {}
        self._peers_lock = threading.Lock()
        self._connections: Dict[str, _PeerConnection] = {}
        self._node_connections: Dict[str, str] = {}
        self._connections_lock = threading.Lock()
        self._tcp: Optional[_LinkTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        # 内置动作：心跳与握手
        self.register_handler(ActionType.PING, self._handle_ping)
        self.register_handler(ActionType.HELLO, self._handle_hello)
        #: hello 响应附带的静态信息（ros 组网协助等），由装配方填充
        self.hello_payload: Dict[str, Any] = {}

    # ── 生命周期 ─────────────────────────────────────────────

    def start(self) -> "HostLinkServer":
        self._tcp = _LinkTCPServer((self._bind, self._port), _LinkRequestHandler)
        self._tcp.link = self
        # 端口 0 时回读实际端口（测试友好）
        self._port = self._tcp.server_address[1]
        self._thread = threading.Thread(
            target=self._tcp.serve_forever, name="hostlink-server", daemon=True
        )
        self._thread.start()
        logger.info(f"[HostLink] server listening on {self._bind}:{self._port}")
        return self

    def stop(self) -> None:
        self.stopping.set()
        with self._connections_lock:
            connections = list(self._connections.values())
        for connection in connections:
            connection.close()
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._tcp is not None:
            self._tcp.shutdown()
            self._tcp.server_close()
            self._tcp = None

    @property
    def port(self) -> int:
        return self._port

    # ── handler 与 peer 注册 ─────────────────────────────────

    def register_handler(self, action_type: str, handler: Handler) -> None:
        self.handlers[action_type] = handler

    def register_connection(
        self,
        peer_key: str,
        sock: socket.socket,
        write_lock: threading.Lock,
    ) -> _PeerConnection:
        connection = _PeerConnection(peer_key, sock, write_lock)
        with self._connections_lock:
            old = self._connections.get(peer_key)
            self._connections[peer_key] = connection
        if old is not None:
            old.close()
        return connection

    def bind_connection(self, peer_key: str) -> None:
        with self._peers_lock:
            node_id = self._connection_nodes.get(peer_key)
        if not node_id:
            return
        with self._connections_lock:
            if peer_key in self._connections:
                self._node_connections[node_id] = peer_key

    def unregister_connection(self, peer_key: str) -> None:
        with self._connections_lock:
            connection = self._connections.pop(peer_key, None)
            stale_nodes = [
                node_id
                for node_id, connection_key in self._node_connections.items()
                if connection_key == peer_key
            ]
            for node_id in stale_nodes:
                self._node_connections.pop(node_id, None)
        if connection is not None:
            connection.close()

    def has_device(self, device_id: str, capability: str = "") -> bool:
        now = time.time()
        with self._peers_lock:
            return any(
                peer.get("connected")
                and now - peer.get("last_seen", 0) < self.heartbeat_timeout
                and device_id in (peer.get("device_ids") or [])
                and (
                    not capability
                    or capability in (peer.get("capabilities") or [])
                )
                for peer in self._peers.values()
            )

    def request_device(
        self,
        device_id: str,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = 600.0,
    ) -> Any:
        """Route a Host request to the online Slave that reported ``device_id``."""

        now = time.time()
        node_id = ""
        with self._peers_lock:
            for candidate_node, peer in self._peers.items():
                if (
                    peer.get("connected")
                    and now - peer.get("last_seen", 0) < self.heartbeat_timeout
                    and device_id in (peer.get("device_ids") or [])
                ):
                    node_id = candidate_node
                    break
        if not node_id:
            raise LinkError(f"no online Slave owns device {device_id!r}")
        with self._connections_lock:
            peer_key = self._node_connections.get(node_id, "")
            connection = self._connections.get(peer_key)
        if connection is None:
            raise LinkError(
                f"Slave {node_id!r} has no bidirectional HostLink connection"
            )
        return connection.request(action_type, data, timeout)

    def touch_peer(
        self, peer_key: str, action: str, message: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Refresh a logical peer; hello binds this socket to its stable node ID."""
        now = time.time()
        with self._peers_lock:
            connection_node = self._connection_nodes.get(peer_key)
            if action == ActionType.HELLO:
                raw_data = message.get("data") or {}
                data = raw_data if isinstance(raw_data, dict) else {}
                machine_name = str(data.get("machine_name") or "")
                raw_device_ids = data.get("device_ids")
                device_ids = (
                    sorted(
                        {
                            str(item).strip()
                            for item in raw_device_ids
                            if isinstance(item, str) and item.strip()
                        }
                    )
                    if isinstance(raw_device_ids, list)
                    else []
                )

                # Startup device IDs are globally unique and therefore outrank
                # machine name / socket address for logical Slave identity.  An
                # overlap also keeps the same peer when a Slave adds or removes
                # one device between reconnects.
                node_id = ""
                if device_ids:
                    incoming_devices = set(device_ids)
                    for known_node_id, known_peer in self._peers.items():
                        known_devices = set(known_peer.get("device_ids") or [])
                        if incoming_devices.intersection(known_devices):
                            node_id = known_node_id
                            break
                    if not node_id:
                        node_id = f"device:{device_ids[0]}"
                else:
                    node_id = str(data.get("node_id") or machine_name or peer_key)

                # A pre-hello request may have created a temporary addr-keyed
                # record.  Migrate it once the logical identity is known.
                if connection_node and connection_node != node_id:
                    temporary = self._peers.get(connection_node)
                    if temporary and temporary.get("addr") == peer_key:
                        self._peers.pop(connection_node, None)

                self._connection_nodes[peer_key] = node_id
                peer = self._peers.get(node_id)
                if peer is None:
                    peer = {}
                    self._peers[node_id] = peer

                # A new socket with the same node_id supersedes the old one.
                # The old handler can still unwind, but its later ping/close is
                # ignored because ``addr`` no longer matches.
                if peer.get("addr") != peer_key:
                    peer["addr"] = peer_key
                    peer["connected_at"] = now
                peer["node_id"] = node_id
                peer["device_ids"] = device_ids
                peer["machine_name"] = machine_name
                peer["role"] = str(data.get("role") or "slave")
                peer["protocol_version"] = data.get(
                    "protocol_version", message.get("v")
                )
                capabilities = data.get("capabilities")
                peer["capabilities"] = (
                    [str(item) for item in capabilities if isinstance(item, str)]
                    if isinstance(capabilities, list)
                    else []
                )
            else:
                node_id = connection_node or peer_key
                if connection_node is None:
                    self._connection_nodes[peer_key] = node_id
                peer = self._peers.setdefault(
                    node_id,
                    {
                        "addr": peer_key,
                        "node_id": node_id,
                        "device_ids": [],
                        "machine_name": "",
                        "role": "",
                        "protocol_version": message.get("v"),
                        "capabilities": [],
                        "connected_at": now,
                    },
                )
                # This is an old socket superseded by a reconnect with the same
                # node_id.  Do not let it steal ownership or refresh liveness.
                if connection_node and peer.get("addr") != peer_key:
                    return dict(peer)

            peer["last_seen"] = now
            peer["connected"] = True
            return dict(peer)

    def mark_disconnected(self, peer_key: str) -> None:
        with self._peers_lock:
            node_id = self._connection_nodes.pop(peer_key, peer_key)
            peer = self._peers.get(node_id)
            if peer is not None and peer.get("addr") == peer_key:
                peer["connected"] = False

    def peers(self) -> List[Dict[str, Any]]:
        """当前已知 peer 及在线状态（online = 连接存活且心跳未超时）。"""
        now = time.time()
        with self._peers_lock:
            result = []
            for peer in self._peers.values():
                snapshot = dict(peer)
                snapshot["online"] = bool(
                    snapshot.get("connected")
                    and now - snapshot.get("last_seen", 0) < self.heartbeat_timeout
                )
                result.append(snapshot)
            return result

    # ── 内置动作 ─────────────────────────────────────────────

    def _handle_ping(
        self, data: Dict[str, Any], peer: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {"pong": True, "server_time": time.time()}

    def _handle_hello(
        self, data: Dict[str, Any], peer: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {
            "server_time": time.time(),
            "heartbeat_timeout": self.heartbeat_timeout,
            **self.hello_payload,
            "assigned_node_id": peer.get("node_id"),
            "device_ids": list(peer.get("device_ids") or []),
        }


# ── 进程级单例（host 主流程注册，REST 面取用做在线监控） ─────────

_server_lock = threading.Lock()
_server: Optional[HostLinkServer] = None


def set_hostlink_server(server: Optional[HostLinkServer]) -> None:
    global _server
    with _server_lock:
        _server = server


def get_hostlink_server() -> Optional[HostLinkServer]:
    with _server_lock:
        return _server


__all__ = ["Handler", "HostLinkServer", "get_hostlink_server", "set_hostlink_server"]
