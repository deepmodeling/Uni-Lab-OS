"""Slave-side HostLink client for discovery, state sync and bidirectional RPC."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid
from concurrent.futures import (
    Future,
    InvalidStateError,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from typing import Any, Callable, Dict, Iterable, List, Optional

from unilabos.hostlink.protocol import (
    ActionType,
    exception_error_info,
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
            # asyncio timeout/cancellation may win the race with the reader.
            pass

    def wait(self, timeout: Optional[float]) -> Dict[str, Any]:
        return self.future.result(timeout=timeout)

    async def wait_async(self, timeout: Optional[float]) -> Dict[str, Any]:
        wrapped = asyncio.wrap_future(self.future)
        if timeout is None:
            return await wrapped
        return await asyncio.wait_for(wrapped, timeout)


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
        device_descriptors: Optional[Iterable[Dict[str, Any]]] = None,
        heartbeat_payload_provider: Optional[Callable[[], Dict[str, Any]]] = None,
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
        self.heartbeat_payload_provider = heartbeat_payload_provider
        self.node_id = self.machine_name or f"slave-{uuid.uuid4().hex}"
        self.device_ids: List[str] = []
        self.device_descriptors: List[Dict[str, Any]] = []
        self.configure_device_ids(device_ids or [])
        self.configure_device_descriptors(device_descriptors or [])
        self.capabilities = [
            "device-discovery",
            "ros-assist",
            "device-rpc",
            "service-rpc",
            "topic-pubsub",
        ]
        self.hello_info: Dict[str, Any] = {}
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}

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
        self._rpc_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="hostlink-slave-rpc",
        )

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
        self._rpc_executor.shutdown(wait=False, cancel_futures=True)

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

    def configure_device_descriptors(
        self,
        descriptors: Iterable[Dict[str, Any]],
    ) -> None:
        normalized: List[Dict[str, Any]] = []
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                continue
            device_id = str(descriptor.get("id") or "").strip()
            if not device_id:
                continue
            item = dict(descriptor)
            item["id"] = device_id
            normalized.append(item)
        self.device_descriptors = sorted(normalized, key=lambda item: item["id"])
        if self.device_descriptors:
            self.configure_device_ids(item["id"] for item in self.device_descriptors)

    def register_handler(
        self,
        action_type: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        self.handlers[str(action_type)] = handler

    def request(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        return self._request(action_type, data, timeout, require_online=True)

    async def request_async(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send a request and await its response without blocking a worker."""

        return await self._request_async(
            action_type,
            data,
            timeout,
            require_online=True,
        )

    def ros_info(self, timeout: Optional[float] = None) -> RosNetworkInfo:
        data = self.request(ActionType.ROS_INFO, timeout=timeout)
        return RosNetworkInfo.from_dict((data or {}).get("ros") or data)

    def get_resource(
        self,
        uuid: Optional[str] = None,
        res_id: Optional[str] = None,
        with_children: bool = True,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Query the Host microbackend's read-only material snapshot."""

        data = self.request(
            ActionType.MATERIAL,
            {
                "uuid": uuid,
                "id": res_id,
                "with_children": bool(with_children),
            },
            timeout,
        )
        nodes = (data or {}).get("nodes") if isinstance(data, dict) else None
        return list(nodes or [])

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
            "devices": [dict(item) for item in self.device_descriptors],
        }

    def _request(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]],
        timeout: Optional[float],
        *,
        require_online: bool,
    ) -> Any:
        request_id, pending = self._begin_request(
            action_type,
            data,
            require_online=require_online,
        )
        wait_timeout = self.request_timeout if timeout is None else float(timeout)
        if wait_timeout < 0:
            wait_timeout = None
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

    async def _request_async(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]],
        timeout: Optional[float],
        *,
        require_online: bool,
    ) -> Any:
        request_id, pending = self._begin_request(
            action_type,
            data,
            require_online=require_online,
        )
        wait_timeout = self.request_timeout if timeout is None else float(timeout)
        if wait_timeout < 0:
            wait_timeout = None
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
        *,
        require_online: bool,
    ) -> tuple[str, _Pending]:
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
            raise RemoteError(
                str(response.get("error") or "remote error"),
                response.get("error_info"),
            )
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
            payload: Optional[Dict[str, Any]] = None
            if self.heartbeat_payload_provider is not None:
                try:
                    payload = self.heartbeat_payload_provider()
                except Exception:  # noqa: BLE001 - 状态采集不能中断重连
                    logger.exception("[HostLink] heartbeat payload collection failed")
            self.request(ActionType.PING, data=payload, timeout=self.request_timeout)

    def _read_loop(self, sock: socket.socket) -> None:
        reader = LineReader(sock)
        try:
            while not self._stop.is_set():
                message = read_message(reader)
                if message is None:
                    break
                if message.get("kind") == "req":
                    try:
                        self._rpc_executor.submit(
                            self._handle_incoming_request,
                            sock,
                            message,
                        )
                    except RuntimeError:
                        break
                    continue
                if message.get("kind") != "resp":
                    continue
                request_id = str(message.get("id") or "")
                with self._pending_lock:
                    pending = self._pending.get(request_id)
                if pending is not None:
                    pending.resolve(message)
        except (OSError, LinkError) as exc:
            logger.debug(f"[HostLink] reader stopped: {exc}")
        finally:
            reader.close()
            self._connection_lost.set()
            self._set_online(False)
            with self._pending_lock:
                pending_items = list(self._pending.values())
            for pending in pending_items:
                pending.resolve({"ok": False, "error": "connection closed"})

    def _handle_incoming_request(
        self,
        sock: socket.socket,
        message: Dict[str, Any],
    ) -> None:
        request_id = str(message.get("id") or "")
        if message.get("v") != PROTOCOL_VERSION:
            response = new_response(
                request_id,
                False,
                error=f"unsupported protocol version: {message.get('v')!r}",
            )
        else:
            action = str(message.get("action_type") or "")
            handler = self.handlers.get(action)
            if handler is None:
                response = new_response(
                    request_id,
                    False,
                    error=f"unknown action: {action}",
                )
            else:
                raw_data = message.get("data")
                data = raw_data if isinstance(raw_data, dict) else {}
                try:
                    response = new_response(request_id, True, handler(data))
                except Exception as exc:  # noqa: BLE001 - RPC 请求必须返回明确错误
                    logger.warning(f"[HostLink] incoming {action} failed: {exc}")
                    response = new_response(
                        request_id,
                        False,
                        error=str(exc),
                        error_info=exception_error_info(exc),
                    )
        try:
            with self._write_lock:
                send_message(sock, response)
        except OSError:
            self._connection_lost.set()

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
