"""Slave-side HostLink client for discovery and ROS2 configuration sync."""

from __future__ import annotations

import socket
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

from unilabos.hostlink.protocol import (
    ActionType,
    LineReader,
    LinkError,
    PROTOCOL_VERSION,
    RemoteError,
    new_request,
    read_message,
    send_message,
)
from unilabos.hostlink.ros_assist import RosNetworkInfo
from unilabos.utils import logger


class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[Dict[str, Any]] = None


class HostLinkClient:
    """Maintain one reconnecting TCP control connection to the Host."""

    def __init__(
        self,
        host: str,
        port: int = 7302,
        machine_name: str = "",
        device_ids: Optional[Iterable[str]] = None,
        heartbeat_interval: float = 5.0,
        connect_timeout: float = 5.0,
        request_timeout: float = 10.0,
        reconnect_max_backoff: float = 10.0,
        on_status_change: Optional[Callable[[bool], None]] = None,
    ) -> None:
        if not str(host or "").strip():
            raise ValueError("HostLink host cannot be empty")
        self.host = str(host).strip()
        self.port = int(port)
        self.machine_name = str(machine_name or "").strip()
        self.heartbeat_interval = float(heartbeat_interval)
        self.connect_timeout = float(connect_timeout)
        self.request_timeout = float(request_timeout)
        self.reconnect_max_backoff = float(reconnect_max_backoff)
        self.on_status_change = on_status_change
        self.node_id = self.machine_name or f"slave-{uuid.uuid4().hex}"
        self.device_ids: List[str] = []
        self.configure_device_ids(device_ids or [])
        self.capabilities = ["device-discovery", "ros-assist"]
        self.hello_info: Dict[str, Any] = {}

        self._sock: Optional[socket.socket] = None
        self._manager_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._pending: Dict[str, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._connection_lost = threading.Event()
        self._online = threading.Event()
        self._status_condition = threading.Condition()

    def start(self) -> "HostLinkClient":
        if self._manager_thread is not None and self._manager_thread.is_alive():
            return self
        self._stop.clear()
        self._manager_thread = threading.Thread(
            target=self._run,
            name="hostlink-client",
            daemon=True,
        )
        self._manager_thread.start()
        return self

    def connect_blocking(self, timeout: Optional[float] = 10.0) -> bool:
        """Start the reconnect manager and wait for a successful hello."""

        self.start()
        deadline = None if timeout is None else time.monotonic() + float(timeout)
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
        if self._manager_thread is not None and self._manager_thread.is_alive():
            self._manager_thread.join(timeout=3)
        self._manager_thread = None

    @property
    def online(self) -> bool:
        return self._online.is_set()

    def configure_device_ids(self, device_ids: Iterable[str]) -> None:
        normalized = sorted(
            {
                str(device_id).strip()
                for device_id in device_ids
                if str(device_id).strip()
            }
        )
        if normalized:
            self.device_ids = normalized
            self.node_id = f"device:{normalized[0]}"

    def request(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        return self._request(action_type, data, timeout, require_online=True)

    def ros_info(self, timeout: Optional[float] = None) -> RosNetworkInfo:
        data = self.request(ActionType.ROS_INFO, timeout=timeout)
        return RosNetworkInfo.from_dict((data or {}).get("ros") or data)

    def hello_ros_info(self) -> RosNetworkInfo:
        return RosNetworkInfo.from_dict(self.hello_info.get("ros"))

    def _identity_payload(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "device_ids": list(self.device_ids),
            "machine_name": self.machine_name,
            "role": "slave",
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": list(self.capabilities),
        }

    def _request(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]],
        timeout: Optional[float],
        *,
        require_online: bool,
    ) -> Any:
        sock = self._sock
        if sock is None or (require_online and not self.online):
            raise LinkError(f"hostlink offline ({self.host}:{self.port})")
        message = new_request(action_type, data=data)
        pending = _Pending()
        request_id = str(message["id"])
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            with self._write_lock:
                send_message(sock, message)
            if not pending.event.wait(timeout or self.request_timeout):
                raise LinkError(f"request timeout: {action_type} ({request_id[:8]})")
        except OSError as exc:
            raise LinkError(f"request send failed: {exc}") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        response = pending.response or {}
        if not response.get("ok"):
            raise RemoteError(str(response.get("error") or "remote error"))
        return response.get("data")

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                self._connect_once()
                backoff = 0.5
                self._heartbeat_loop()
            except (OSError, LinkError) as exc:
                logger.debug(f"[HostLink] connection cycle ended: {exc}")
            self._set_online(False)
            self._teardown_socket()
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.reconnect_max_backoff)

    def _connect_once(self) -> None:
        sock = socket.create_connection(
            (self.host, self.port),
            timeout=self.connect_timeout,
        )
        sock.settimeout(None)
        self._sock = sock
        self._connection_lost.clear()
        self._reader_thread = threading.Thread(
            target=self._read_loop,
            args=(sock,),
            name="hostlink-reader",
            daemon=True,
        )
        self._reader_thread.start()
        data = self._request(
            ActionType.HELLO,
            self._identity_payload(),
            self.connect_timeout,
            require_online=False,
        )
        self.hello_info = dict(data or {})
        self._set_online(True)
        logger.info(
            f"[HostLink] connected to {self.host}:{self.port}; "
            f"devices={self.device_ids}"
        )

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            if self._connection_lost.is_set():
                raise LinkError("connection closed")
            self.request(ActionType.PING, timeout=self.request_timeout)

    def _read_loop(self, sock: socket.socket) -> None:
        reader = LineReader(sock)
        try:
            while not self._stop.is_set():
                message = read_message(reader)
                if message is None:
                    break
                if message.get("kind") != "resp":
                    continue
                request_id = str(message.get("id") or "")
                with self._pending_lock:
                    pending = self._pending.get(request_id)
                if pending is not None:
                    pending.response = message
                    pending.event.set()
        except (OSError, LinkError) as exc:
            logger.debug(f"[HostLink] reader stopped: {exc}")
        finally:
            reader.close()
            self._connection_lost.set()
            self._set_online(False)
            with self._pending_lock:
                pending_items = list(self._pending.values())
            for pending in pending_items:
                if pending.response is None:
                    pending.response = {"ok": False, "error": "connection closed"}
                    pending.event.set()

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
            except Exception:  # noqa: BLE001 - observer must not kill reconnect
                logger.exception("[HostLink] status callback failed")


_client_lock = threading.Lock()
_client: Optional[HostLinkClient] = None


def set_hostlink_client(client: Optional[HostLinkClient]) -> None:
    global _client
    with _client_lock:
        _client = client


def get_hostlink_client() -> Optional[HostLinkClient]:
    with _client_lock:
        return _client


__all__ = [
    "HostLinkClient",
    "get_hostlink_client",
    "set_hostlink_client",
]
