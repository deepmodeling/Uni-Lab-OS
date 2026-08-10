"""Host-side HostLink listener for discovery, policy sync and device RPC."""

from __future__ import annotations

import asyncio
import socket
import socketserver
import threading
import time
from concurrent.futures import (
    Future,
    InvalidStateError,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from typing import Any, Callable, Dict, List, Optional

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
from unilabos.utils import logger

Handler = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


class _Pending:
    """One response shared by blocking and asyncio callers."""

    __slots__ = ("future",)

    def __init__(self) -> None:
        self.future: Future[Dict[str, Any]] = Future()

    def resolve(self, response: Dict[str, Any]) -> None:
        if self.future.done():
            return
        try:
            self.future.set_result(response)
        except InvalidStateError:
            pass

    def wait(self, timeout: Optional[float]) -> Dict[str, Any]:
        return self.future.result(timeout=timeout)

    async def wait_async(self, timeout: Optional[float]) -> Dict[str, Any]:
        wrapped = asyncio.wrap_future(self.future)
        if timeout is None:
            return await wrapped
        return await asyncio.wait_for(wrapped, timeout)


class _PeerSession:
    """A connected Slave socket that also accepts Host-initiated requests."""

    def __init__(self, sock: socket.socket, request_timeout: float) -> None:
        self.sock = sock
        self.request_timeout = float(request_timeout)
        self._write_lock = threading.Lock()
        self._pending: Dict[str, _Pending] = {}
        self._pending_lock = threading.Lock()

    def send(self, message: Dict[str, Any]) -> None:
        with self._write_lock:
            send_message(self.sock, message)

    def request(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        request_id, pending = self._begin_request(action_type, data)
        wait_timeout = self.request_timeout if timeout is None else float(timeout)
        try:
            try:
                response = pending.wait(wait_timeout)
            except FutureTimeoutError as exc:
                raise LinkError(
                    f"request timeout: {action_type} ({request_id[:8]})"
                ) from exc
        finally:
            self._remove_pending(request_id, pending)
        return self._response_data(response)

    async def request_async(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Await one response while the connection reader stays threaded."""

        request_id, pending = self._begin_request(action_type, data)
        wait_timeout = self.request_timeout if timeout is None else float(timeout)
        try:
            try:
                response = await pending.wait_async(wait_timeout)
            except TimeoutError as exc:
                raise LinkError(
                    f"request timeout: {action_type} ({request_id[:8]})"
                ) from exc
        finally:
            self._remove_pending(request_id, pending)
        return self._response_data(response)

    def _begin_request(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]],
    ) -> tuple[str, _Pending]:
        message = new_request(action_type, data=data)
        request_id = str(message["id"])
        pending = _Pending()
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self.send(message)
        except OSError as exc:
            self._remove_pending(request_id, pending)
            raise LinkError(f"request send failed: {exc}") from exc
        except Exception:
            self._remove_pending(request_id, pending)
            raise
        return request_id, pending

    def _remove_pending(self, request_id: str, pending: _Pending) -> None:
        with self._pending_lock:
            if self._pending.get(request_id) is pending:
                self._pending.pop(request_id, None)

    @staticmethod
    def _response_data(response: Dict[str, Any]) -> Any:
        if not response.get("ok"):
            raise RemoteError(str(response.get("error") or "remote error"))
        return response.get("data")

    def resolve_response(self, message: Dict[str, Any]) -> None:
        request_id = str(message.get("id") or "")
        with self._pending_lock:
            pending = self._pending.get(request_id)
        if pending is not None:
            pending.resolve(message)

    def close(self) -> None:
        with self._pending_lock:
            pending_items = list(self._pending.values())
        for pending in pending_items:
            pending.resolve({"ok": False, "error": "connection closed"})


class _LinkTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    link: "HostLinkServer"


class _LinkRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        link = self.server.link  # type: ignore[attr-defined]
        sock: socket.socket = self.request
        sock.settimeout(link.socket_timeout)
        reader = LineReader(sock)
        peer_key = f"{self.client_address[0]}:{self.client_address[1]}"
        session = link.register_session(peer_key, sock)
        try:
            while not link.stopping.is_set():
                try:
                    message = read_message(reader)
                except (socket.timeout, TimeoutError):
                    continue
                except (LinkError, OSError) as exc:
                    logger.debug(f"[HostLink] close {peer_key}: {exc}")
                    break
                if message is None:
                    break
                if message.get("kind") == "resp":
                    session.resolve_response(message)
                    continue
                if message.get("kind") != "req":
                    continue
                if not link.submit_request(session, message, peer_key):
                    break
        finally:
            reader.close()
            link.unregister_session(peer_key, session)
            link.mark_disconnected(peer_key)


class HostLinkServer:
    """Track Slave devices and make requests over their control connections."""

    def __init__(
        self,
        bind: str = "0.0.0.0",
        port: int = 7302,
        heartbeat_timeout: float = 15.0,
        socket_timeout: float = 1.0,
        request_timeout: float = 10.0,
    ) -> None:
        self._bind = bind
        self._port = int(port)
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.socket_timeout = float(socket_timeout)
        self.request_timeout = float(request_timeout)
        self.handlers: Dict[str, Handler] = {}
        self.hello_payload: Dict[str, Any] = {}
        self.stopping = threading.Event()
        self._peers: Dict[str, Dict[str, Any]] = {}
        self._connection_nodes: Dict[str, str] = {}
        self._peers_lock = threading.Lock()
        self._sessions: Dict[str, _PeerSession] = {}
        self._sessions_lock = threading.Lock()
        self._tcp: Optional[_LinkTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._rpc_executor = ThreadPoolExecutor(
            max_workers=16,
            thread_name_prefix="hostlink-host-rpc",
        )
        self.register_handler(ActionType.HELLO, self._handle_hello)
        self.register_handler(ActionType.PING, self._handle_ping)
        self.register_handler(ActionType.ROS_INFO, self._handle_ros_info)
        self.register_handler(ActionType.DEVICE_STATE, self._handle_device_state)

    def start(self) -> "HostLinkServer":
        if self._thread is not None and self._thread.is_alive():
            return self
        self.stopping.clear()
        self._tcp = _LinkTCPServer((self._bind, self._port), _LinkRequestHandler)
        self._tcp.link = self
        self._port = int(self._tcp.server_address[1])
        self._thread = threading.Thread(
            target=self._tcp.serve_forever,
            name="hostlink-server",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"[HostLink] server listening on {self._bind}:{self._port}")
        return self

    def stop(self) -> None:
        self.stopping.set()
        tcp, self._tcp = self._tcp, None
        if tcp is not None:
            tcp.shutdown()
            tcp.server_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
        self._rpc_executor.shutdown(wait=False, cancel_futures=True)

    @property
    def port(self) -> int:
        return self._port

    def register_handler(self, action_type: str, handler: Handler) -> None:
        self.handlers[action_type] = handler

    def register_session(
        self,
        peer_key: str,
        sock: socket.socket,
    ) -> _PeerSession:
        session = _PeerSession(sock, self.request_timeout)
        with self._sessions_lock:
            old_session = self._sessions.get(peer_key)
            self._sessions[peer_key] = session
        if old_session is not None:
            old_session.close()
        return session

    def unregister_session(
        self,
        peer_key: str,
        session: _PeerSession,
    ) -> None:
        with self._sessions_lock:
            if self._sessions.get(peer_key) is session:
                self._sessions.pop(peer_key, None)
        session.close()

    def request_peer(
        self,
        peer_key: str,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        with self._sessions_lock:
            session = self._sessions.get(str(peer_key))
        if session is None:
            raise LinkError(f"hostlink peer offline: {peer_key}")
        return session.request(action_type, data, timeout)

    async def request_peer_async(
        self,
        peer_key: str,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        with self._sessions_lock:
            session = self._sessions.get(str(peer_key))
        if session is None:
            raise LinkError(f"hostlink peer offline: {peer_key}")
        return await session.request_async(action_type, data, timeout)

    def request_device(
        self,
        device_id: str,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        device_id = str(device_id)
        peer = self.devices(online_only=True).get(device_id)
        if peer is None:
            raise LinkError(f"hostlink device offline: {device_id}")
        return self.request_peer(str(peer["addr"]), action_type, data, timeout)

    async def request_device_async(
        self,
        device_id: str,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        device_id = str(device_id)
        peer = self.devices(online_only=True).get(device_id)
        if peer is None:
            raise LinkError(f"hostlink device offline: {device_id}")
        return await self.request_peer_async(
            str(peer["addr"]),
            action_type,
            data,
            timeout,
        )

    def submit_request(
        self,
        session: _PeerSession,
        message: Dict[str, Any],
        peer_key: str,
    ) -> bool:
        """Dispatch one Slave request without blocking reads on that connection."""

        def serve() -> None:
            response = self.dispatch(message, peer_key)
            try:
                session.send(response)
            except OSError:
                pass

        try:
            self._rpc_executor.submit(serve)
        except RuntimeError:
            return False
        return True

    def call_device(
        self,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        action_id: str = "",
    ) -> Any:
        return self.request_device(
            device_id,
            ActionType.DEVICE_CALL,
            {
                "device_id": str(device_id),
                "action": str(action_name),
                "arguments": dict(arguments or {}),
                "action_id": str(action_id),
            },
            timeout,
        )

    async def call_device_async(
        self,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        action_id: str = "",
    ) -> Any:
        return await self.request_device_async(
            device_id,
            ActionType.DEVICE_CALL,
            {
                "device_id": str(device_id),
                "action": str(action_name),
                "arguments": dict(arguments or {}),
                "action_id": str(action_id),
            },
            timeout,
        )

    def cancel_device_action(
        self,
        device_id: str,
        action_id: str,
        timeout: Optional[float] = None,
    ) -> Any:
        return self.request_device(
            device_id,
            ActionType.ACTION_CANCEL,
            {"device_id": str(device_id), "action_id": str(action_id)},
            timeout,
        )

    async def cancel_device_action_async(
        self,
        device_id: str,
        action_id: str,
        timeout: Optional[float] = None,
    ) -> Any:
        return await self.request_device_async(
            device_id,
            ActionType.ACTION_CANCEL,
            {"device_id": str(device_id), "action_id": str(action_id)},
            timeout,
        )

    def dispatch(self, message: Dict[str, Any], peer_key: str) -> Dict[str, Any]:
        request_id = str(message.get("id") or "")
        if message.get("v") != PROTOCOL_VERSION:
            return new_response(
                request_id,
                False,
                error=f"unsupported protocol version: {message.get('v')!r}",
            )
        action = str(message.get("action_type") or "")
        handler = self.handlers.get(action)
        if handler is None:
            return new_response(request_id, False, error=f"unknown action: {action}")
        raw_data = message.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        try:
            peer = self._touch_peer(peer_key, action, data)
            return new_response(request_id, True, handler(data, peer))
        except Exception as exc:  # noqa: BLE001 - protocol boundary
            logger.warning(f"[HostLink] {action} from {peer_key} failed: {exc}")
            return new_response(request_id, False, error=str(exc))

    def _touch_peer(
        self,
        peer_key: str,
        action: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = time.time()
        with self._peers_lock:
            known_node = self._connection_nodes.get(peer_key)
            if action == ActionType.HELLO:
                devices: Dict[str, Dict[str, Any]] = {}
                for item in data.get("devices") or []:
                    if not isinstance(item, dict):
                        continue
                    device_id = str(item.get("id") or "").strip()
                    if device_id:
                        descriptor = dict(item)
                        descriptor["id"] = device_id
                        devices[device_id] = descriptor
                device_ids = sorted(
                    {
                        str(device_id).strip()
                        for device_id in data.get("device_ids") or []
                        if str(device_id).strip()
                    }
                    | set(devices)
                )
                machine_name = str(data.get("machine_name") or "").strip()
                node_id = str(data.get("node_id") or "").strip()
                if device_ids:
                    incoming_devices = set(device_ids)
                    node_id = ""
                    for known_node_id, known_peer in self._peers.items():
                        known_devices = set(known_peer.get("device_ids") or [])
                        if incoming_devices.intersection(known_devices):
                            node_id = known_node_id
                            break
                    if not node_id:
                        node_id = f"device:{device_ids[0]}"
                node_id = node_id or machine_name or peer_key
                if known_node and known_node != node_id:
                    temporary = self._peers.get(known_node)
                    if temporary and temporary.get("addr") == peer_key:
                        self._peers.pop(known_node, None)
                self._connection_nodes[peer_key] = node_id
                peer = self._peers.setdefault(node_id, {})
                if peer.get("addr") != peer_key:
                    peer["connected_at"] = now
                peer.update(
                    {
                        "addr": peer_key,
                        "node_id": node_id,
                        "machine_name": machine_name,
                        "role": str(data.get("role") or "slave"),
                        "device_ids": device_ids,
                        "devices": devices,
                        "protocol_version": data.get("protocol_version"),
                        "capabilities": [
                            str(item)
                            for item in data.get("capabilities") or []
                            if str(item)
                        ],
                    }
                )
            else:
                node_id = known_node or peer_key
                peer = self._peers.setdefault(
                    node_id,
                    {
                        "addr": peer_key,
                        "node_id": node_id,
                        "machine_name": "",
                        "role": "",
                        "device_ids": [],
                        "protocol_version": data.get("protocol_version"),
                        "capabilities": [],
                        "connected_at": now,
                    },
                )
                if known_node and peer.get("addr") != peer_key:
                    return dict(peer)
                if action in (ActionType.PING, ActionType.DEVICE_STATE):
                    states = peer.setdefault("states", {})
                    if isinstance(data.get("states"), dict):
                        states.update(data["states"])
                    device_id = str(data.get("device_id") or "").strip()
                    state = data.get("state")
                    if device_id and isinstance(state, dict):
                        states[device_id] = dict(state)
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
        """Return all known Slaves with a calculated ``online`` field."""

        now = time.time()
        with self._peers_lock:
            result: List[Dict[str, Any]] = []
            for peer in self._peers.values():
                snapshot = dict(peer)
                snapshot["online"] = bool(
                    snapshot.get("connected")
                    and now - float(snapshot.get("last_seen") or 0)
                    < self.heartbeat_timeout
                )
                result.append(snapshot)
            return result

    def devices(self, online_only: bool = True) -> Dict[str, Dict[str, Any]]:
        """Map discovered device IDs to the Slave that advertised each one."""

        result: Dict[str, Dict[str, Any]] = {}
        for peer in self.peers():
            if online_only and not peer.get("online"):
                continue
            for device_id in peer.get("device_ids") or []:
                device_id = str(device_id)
                snapshot = dict(peer)
                snapshot["device"] = dict(
                    (peer.get("devices") or {}).get(device_id) or {"id": device_id}
                )
                state = (peer.get("states") or {}).get(device_id)
                snapshot["state"] = dict(state) if isinstance(state, dict) else {}
                result[device_id] = snapshot
        return result

    def has_device(self, device_id: str) -> bool:
        return str(device_id) in self.devices(online_only=True)

    def _handle_hello(
        self,
        _data: Dict[str, Any],
        peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "server_time": time.time(),
            "heartbeat_timeout": self.heartbeat_timeout,
            **self.hello_payload,
            "assigned_node_id": peer.get("node_id"),
            "device_ids": list(peer.get("device_ids") or []),
        }

    def _handle_ping(
        self,
        _data: Dict[str, Any],
        _peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {"pong": True, "server_time": time.time()}

    def _handle_ros_info(
        self,
        _data: Dict[str, Any],
        _peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {"ros": dict(self.hello_payload.get("ros") or {})}

    def _handle_device_state(
        self,
        data: Dict[str, Any],
        _peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "accepted": True,
            "device_id": str(data.get("device_id") or ""),
        }


_server_lock = threading.Lock()
_server: Optional[HostLinkServer] = None


def set_hostlink_server(server: Optional[HostLinkServer]) -> None:
    global _server
    with _server_lock:
        _server = server


def get_hostlink_server() -> Optional[HostLinkServer]:
    with _server_lock:
        return _server


__all__ = [
    "Handler",
    "HostLinkServer",
    "get_hostlink_server",
    "set_hostlink_server",
]
