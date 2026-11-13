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

from unilabos.backend.hostlink.protocol import (
    ActionType,
    exception_error_info,
    HostNotReadyError,
    LineReader,
    LinkError,
    PROTOCOL_VERSION,
    new_request,
    new_response,
    read_message,
    response_error,
    send_message,
    send_response_or_reject,
)
from unilabos.backend.hostlink.ros_assist import RosNetworkInfo
from unilabos.utils import logger

#: 等 Host 的默认上限（秒）：连接阶段与 Host 启动中的就绪等待各自计时
DEFAULT_HOST_WAIT_TIMEOUT = 120.0
#: 重连退避封顶（秒）：Host 重启后端口很快重新打开，Slave 不应再空等十秒
DEFAULT_RECONNECT_MAX_BACKOFF = 2.0
#: 「Host 启动中」应答后的重试退避（秒）：起步值翻倍到封顶值
_NOT_READY_FIRST_DELAY = 0.2
_NOT_READY_MAX_DELAY = 2.0


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
        reconnect_max_backoff: float = DEFAULT_RECONNECT_MAX_BACKOFF,
        on_status_change: Optional[Callable[[bool], None]] = None,
        device_descriptors: Optional[Iterable[Dict[str, Any]]] = None,
        heartbeat_payload_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        host_wait_timeout: float = DEFAULT_HOST_WAIT_TIMEOUT,
    ) -> None:
        if not str(host or "").strip():
            raise ValueError("HostLink host cannot be empty")
        self.host = str(host).strip()
        self.port = int(port)
        self.machine_name = str(machine_name or "").strip() or f"slave-{uuid.uuid4().hex}"
        self.heartbeat_interval = float(heartbeat_interval)
        # 单次 TCP 连接 / HELLO 握手的超时；等 Host 起来的总时长由 host_wait_timeout 决定
        self.connect_timeout = float(connect_timeout)
        self.request_timeout = float(request_timeout)
        self.reconnect_max_backoff = float(reconnect_max_backoff)
        # Host 仍在启动时（「Host 启动中」应答）单个请求最多重试多久
        self.host_wait_timeout = float(host_wait_timeout)
        #: 最近一次连接失败的原因，供启动超时时给出可操作的报错
        self.last_error = ""
        self.on_status_change = on_status_change
        self.heartbeat_payload_provider = heartbeat_payload_provider
        # 节点身份由 machine_name 指定，不能由设备列表推导或随设备增删改变。
        self.node_id = self.machine_name
        self.device_ids: List[str] = []
        self.device_descriptors: List[Dict[str, Any]] = []
        self.configure_device_ids(device_ids or [])
        if device_descriptors is not None:
            self.configure_device_descriptors(device_descriptors)
        self.capabilities = [
            "device-discovery",
            "ros-assist",
            "device-rpc",
            "service-rpc",
            "topic-pubsub",
            "process-logs",
        ]
        self.hello_info: Dict[str, Any] = {}
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        # 组网控制层即可读取日志，HostLink 与 ROS2 Slave 共用；不要求设备已初始化。
        from unilabos.utils.runtime_logs import read_current_log

        self.register_handler(ActionType.LOG_READ, read_current_log)

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
        self._release_log_notices = None

    def start(self) -> "HostLinkClient":
        if self._manager_thread is not None and self._manager_thread.is_alive():
            return self
        self._stop.clear()
        if self._release_log_notices is None:
            from unilabos.utils.log_notices import log_notices

            self._release_log_notices = log_notices.subscribe(self._notify_log_append)
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
        if self._release_log_notices is not None:
            self._release_log_notices()
            self._release_log_notices = None
        with self._status_condition:
            self._status_condition.notify_all()
        self._teardown_socket()
        if self._manager_thread is not None and self._manager_thread.is_alive():
            self._manager_thread.join(timeout=3)
        self._manager_thread = None
        self._rpc_executor.shutdown(wait=False, cancel_futures=True)

    def _notify_log_append(self, notice) -> None:
        if self.online and (notice.all_sources or "host" in notice.source_ids):
            # 运行在日志通知线程；只发索引，Host 按该连接身份定位 Slave。
            self.request(ActionType.LOG_CHANGED, {}, timeout=1)

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
        self.device_ids = normalized

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
        """发请求；Host 启动中拒绝时按退避原样重试，直到就绪或超过 ``host_wait_timeout``。"""

        started: Optional[float] = None
        delay = _NOT_READY_FIRST_DELAY
        while True:
            try:
                return self._request_once(
                    action_type,
                    data,
                    timeout,
                    require_online=require_online,
                )
            except HostNotReadyError as exc:
                started, wait = self._not_ready_backoff(exc, action_type, started, delay)
            if self._stop.wait(wait):
                raise LinkError(f"HostLink client 已关闭，放弃等待 Host 就绪：{action_type}")
            delay = min(delay * 2, _NOT_READY_MAX_DELAY)

    async def _request_async(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]],
        timeout: Optional[float],
        *,
        require_online: bool,
    ) -> Any:
        started: Optional[float] = None
        delay = _NOT_READY_FIRST_DELAY
        while True:
            try:
                return await self._request_once_async(
                    action_type,
                    data,
                    timeout,
                    require_online=require_online,
                )
            except HostNotReadyError as exc:
                started, wait = self._not_ready_backoff(exc, action_type, started, delay)
            await asyncio.sleep(wait)
            if self._stop.is_set():
                raise LinkError(f"HostLink client 已关闭，放弃等待 Host 就绪：{action_type}")
            delay = min(delay * 2, _NOT_READY_MAX_DELAY)

    def _not_ready_backoff(
        self,
        exc: HostNotReadyError,
        action_type: str,
        started: Optional[float],
        delay: float,
    ) -> tuple[float, float]:
        """返回 (首次被拒时刻, 本次等待秒数)；超过等待上限时抛出可操作的 HostNotReadyError。"""

        now = time.monotonic()
        if started is None:
            started = now
            logger.info(
                "[HostLink] Host %s:%s 仍在启动（等待 %s 就绪），%s 按退避重试，最长 %gs",
                self.host,
                self.port,
                exc.stage,
                action_type,
                self.host_wait_timeout,
            )
        remaining = started + self.host_wait_timeout - now
        if remaining <= 0:
            raise HostNotReadyError(
                f"Host {self.host}:{self.port} 在 {self.host_wait_timeout:g}s 内仍未就绪"
                f"（等待 {exc.stage}），放弃 {action_type}；Host 设备启动较慢时可调大 "
                "HostLinkConfig.host_wait_timeout（--hostlink_host_wait_timeout）",
                exc.error_info,
                stage=exc.stage,
            ) from exc
        wait = max(delay, min(exc.retry_after, _NOT_READY_MAX_DELAY))
        return started, min(wait, remaining)

    def _request_once(
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

    async def _request_once_async(
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
            raise response_error(response)
        return response.get("data")

    def _run(self) -> None:
        backoff = 0.5
        failed_attempts = 0
        while not self._stop.is_set():
            connected = False
            try:
                self._connect_once()
                connected = True
                backoff = 0.5
                failed_attempts = 0
                self._heartbeat_loop()
            except (OSError, LinkError) as exc:
                if connected:
                    logger.debug(f"[HostLink] connection cycle ended: {exc}")
                else:
                    self.last_error = str(exc) or type(exc).__name__
                    failed_attempts += 1
                    # 同一轮连续失败只提示一次，其余降为 debug，避免 Host 未起时刷屏
                    log = logger.info if failed_attempts == 1 else logger.debug
                    log(
                        f"[HostLink] Host {self.host}:{self.port} 暂不可连（{self.last_error}），"
                        f"按退避重试（间隔封顶 {self.reconnect_max_backoff:g}s）"
                    )
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
        self.last_error = ""
        self._set_online(True)
        logger.info(
            f"[HostLink] connected to {self.host}:{self.port}; "
            f"machine_name={self.machine_name}; node_id={self.node_id}; devices={self.device_ids}"
        )
        pending = [str(item) for item in self.hello_info.get("pending_stages") or []]
        if pending:
            logger.info(
                "[HostLink] Host 仍在启动（未就绪：%s）；已登记本节点，"
                "依赖这些阶段的请求会在 Host 就绪前自动重试",
                ", ".join(pending),
            )

    def heartbeat_now(self) -> None:
        """立刻发一次心跳：把当前设备描述与状态推给 Host，不等下一个心跳周期。

        Slave 在建链之后才装配设备时用它让 Host 马上看到设备，而不是最多等
        ``heartbeat_interval`` 才出现在 Host 的在线设备表里。
        """

        payload: Optional[Dict[str, Any]] = None
        if self.heartbeat_payload_provider is not None:
            try:
                payload = self.heartbeat_payload_provider()
            except Exception:  # noqa: BLE001 - 状态采集不能中断重连
                logger.exception("[HostLink] heartbeat payload collection failed")
        response = self.request(
            ActionType.PING,
            data=payload,
            timeout=self.request_timeout,
        )
        if isinstance(response, dict) and isinstance(
            response.get("devices"), list
        ):
            self.hello_info["devices"] = [
                dict(item)
                for item in response["devices"]
                if isinstance(item, dict)
            ]

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            if self._connection_lost.is_set():
                raise LinkError("connection closed")
            self.heartbeat_now()

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
                send_response_or_reject(
                    lambda reply: send_message(sock, reply),
                    message,
                    response,
                    f"{self.host}:{self.port}",
                )
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
