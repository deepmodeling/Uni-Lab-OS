"""Host-side HostLink listener for Slave discovery and ROS2 settings."""

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
    PROTOCOL_VERSION,
    new_response,
    read_message,
    send_message,
)
from unilabos.utils import logger

Handler = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


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
                if message.get("kind") != "req":
                    continue
                response = link.dispatch(message, peer_key)
                try:
                    send_message(sock, response)
                except OSError:
                    break
        finally:
            reader.close()
            link.mark_disconnected(peer_key)


class HostLinkServer:
    """Track Slave/device presence and publish the Host ROS2 network policy."""

    def __init__(
        self,
        bind: str = "0.0.0.0",
        port: int = 7302,
        heartbeat_timeout: float = 15.0,
        socket_timeout: float = 1.0,
    ) -> None:
        self._bind = bind
        self._port = int(port)
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.socket_timeout = float(socket_timeout)
        self.handlers: Dict[str, Handler] = {}
        self.hello_payload: Dict[str, Any] = {}
        self.stopping = threading.Event()
        self._peers: Dict[str, Dict[str, Any]] = {}
        self._connection_nodes: Dict[str, str] = {}
        self._peers_lock = threading.Lock()
        self._tcp: Optional[_LinkTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.register_handler(ActionType.HELLO, self._handle_hello)
        self.register_handler(ActionType.PING, self._handle_ping)
        self.register_handler(ActionType.ROS_INFO, self._handle_ros_info)

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

    @property
    def port(self) -> int:
        return self._port

    def register_handler(self, action_type: str, handler: Handler) -> None:
        self.handlers[action_type] = handler

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
                device_ids = sorted(
                    {
                        str(device_id).strip()
                        for device_id in data.get("device_ids") or []
                        if str(device_id).strip()
                    }
                )
                machine_name = str(data.get("machine_name") or "").strip()
                node_id = str(data.get("node_id") or "").strip()
                if device_ids:
                    # A reconnect keeps the logical peer keyed by its first
                    # globally unique device id even though the TCP port changes.
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
                result[str(device_id)] = dict(peer)
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
