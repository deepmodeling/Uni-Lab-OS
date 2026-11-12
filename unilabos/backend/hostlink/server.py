"""Host-side HostLink listener for discovery, policy sync and device RPC."""

from __future__ import annotations

import asyncio
import errno
import os
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
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from unilabos.backend.hostlink.protocol import (
    ActionType,
    exception_error_info,
    LineReader,
    LinkError,
    PROTOCOL_VERSION,
    new_not_ready_response,
    new_request,
    new_response,
    read_message,
    response_error,
    send_message,
    send_response_or_reject,
)
from unilabos.utils import logger

Handler = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


class HostStage:
    """Host 启动期间 HostLink 请求依赖的就绪阶段。"""

    #: 端口已开、设备 runtime 尚未接管：该请求的处理器还没装配
    STARTUP = "startup"
    #: 物料权威已接入，Host 开机物料对齐已完成
    MATERIALS = "materials"
    #: 本机设备已全部启动（含 Workstation post_init），内置 host 服务已注册
    DEVICES = "devices"


#: Host 启动期间要等对应阶段就绪才处理的请求。HELLO / PING / 状态与日志通知 /
#: Topic 订阅与发布 / 动作反馈与取消不依赖 Host 的设备与物料状态，端口一开即可处理。
HOST_STARTUP_GATES: Dict[str, tuple[str, ...]] = {
    HostStage.MATERIALS: (
        ActionType.MATERIAL_TEMPLATE_LIST,
        ActionType.MATERIAL_TEMPLATE_CREATE,
        ActionType.MATERIAL_CREATE,
        ActionType.MATERIAL_GET_TREE,
        ActionType.MATERIAL_GET_BY_RESOURCE_ID,
        ActionType.MATERIAL_SEARCH,
        ActionType.MATERIAL_DATA_PUT,
        ActionType.MATERIAL_MOVE,
        ActionType.MATERIAL_TRANSFER,
        ActionType.MATERIAL_DELETE,
        ActionType.MATERIAL_COMPARE_SNAPSHOT,
        ActionType.MATERIAL_APPLY_SNAPSHOT,
        ActionType.MATERIAL_APPLY_DELTA,
    ),
    HostStage.DEVICES: (
        ActionType.DEVICE_CALL,
        ActionType.SERVICE_CALL,
    ),
}

#: 「Host 启动中」应答建议的重试间隔（秒）
NOT_READY_RETRY_AFTER = 0.5


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

    async def request_async(
        self,
        action_type: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Await one response while the connection reader stays threaded."""

        request_id, pending = self._begin_request(action_type, data)
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
            raise response_error(response)
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


class HostLinkPortInUseError(OSError):
    """HostLink 监听端口已被其他进程占用。"""

    def __init__(self, bind: str, port: int) -> None:
        self.bind = bind
        self.port = port
        locate = (
            f"netstat -ano | findstr :{port}"
            if os.name == "nt"
            else f"lsof -i :{port}"
        )
        super().__init__(
            errno.EADDRINUSE,
            f"HostLink 端口 {bind}:{port} 已被占用（多半是上一个未退出的 Host 进程，"
            f"可用 `{locate}` 找到占用者）。请结束该进程后重启，"
            "或用 --hostlink_port <其他端口> 换端口（Slave 侧需同步改 --hostlink_port）",
        )


_ADDR_IN_USE_ERRNOS = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)}


def _is_addr_in_use(exc: OSError) -> bool:
    return exc.errno in _ADDR_IN_USE_ERRNOS or getattr(exc, "winerror", None) == 10048


class _LinkTCPServer(socketserver.ThreadingTCPServer):
    # POSIX：SO_REUSEADDR 只是让重启能立刻重绑 TIME_WAIT 中的端口。
    # Windows 上它的语义完全不同：允许与另一个仍在 LISTEN 的进程同时绑同一端口，
    # 新连接落到哪个进程不确定——上次没退干净的 Host 会把 Slave 全部"接走"，
    # 本进程既不报错也看不到任何设备。Windows 改用 SO_EXCLUSIVEADDRUSE 独占。
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    link: "HostLinkServer"

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if not self.allow_reuse_address and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


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
        # 启动期门控：阶段 -> 是否就绪（按声明顺序），请求 -> 所需阶段。
        # 未声明任何阶段时（ROS2 组网微后端、单测里的裸 server）行为与原来一致。
        self._gate_lock = threading.Lock()
        self._stage_ready: Dict[str, bool] = {}
        self._action_stages: Dict[str, str] = {}
        self.register_handler(ActionType.HELLO, self._handle_hello)
        self.register_handler(ActionType.PING, self._handle_ping)
        self.register_handler(ActionType.LOG_CHANGED, self._handle_log_changed)
        self.register_handler(ActionType.ROS_INFO, self._handle_ros_info)
        self.register_handler(ActionType.DEVICE_STATE, self._handle_device_state)

    def start(self) -> "HostLinkServer":
        if self._thread is not None and self._thread.is_alive():
            return self
        self.stopping.clear()
        try:
            self._tcp = _LinkTCPServer((self._bind, self._port), _LinkRequestHandler)
        except OSError as exc:
            if _is_addr_in_use(exc):
                raise HostLinkPortInUseError(self._bind, self._port) from exc
            raise
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

    # ------------------------------------------------------------------
    # 启动期门控：端口先开，依赖 Host 状态的请求等对应阶段就绪
    # ------------------------------------------------------------------

    def hold_until_ready(self, gates: Mapping[str, Iterable[str]]) -> None:
        """进入启动期：``gates`` 里的请求在对应阶段 :meth:`mark_ready` 之前一律回「Host 启动中」。

        启动期内还没有处理器的请求同样回「Host 启动中」（处理器稍后由 backend 装配），
        全部阶段就绪后才按未知请求拒绝。拒绝发生在处理器执行之前，对端可以原样重试。
        """

        with self._gate_lock:
            for stage, actions in gates.items():
                self._stage_ready.setdefault(str(stage), False)
                for action in actions:
                    self._action_stages[str(action)] = str(stage)

    def mark_ready(self, stage: str) -> None:
        """标记某个启动阶段就绪；之后依赖它的请求正常处理。"""

        with self._gate_lock:
            changed = self._stage_ready.get(stage) is not True
            self._stage_ready[stage] = True
            pending = [name for name, ready in self._stage_ready.items() if not ready]
        if changed:
            logger.info(
                "[HostLink] Host 启动阶段已就绪：%s%s",
                stage,
                f"（仍在等待：{', '.join(pending)}）" if pending else "，Host 已完全就绪",
            )

    def pending_stages(self) -> List[str]:
        """尚未就绪的启动阶段（按声明顺序）；空列表表示 Host 已完全就绪。"""

        with self._gate_lock:
            return [name for name, ready in self._stage_ready.items() if not ready]

    @property
    def starting(self) -> bool:
        return bool(self.pending_stages())

    def _not_ready_stage(self, action: str) -> Optional[str]:
        with self._gate_lock:
            stage = self._action_stages.get(action)
            if stage is not None and not self._stage_ready.get(stage, True):
                return stage
            if action not in self.handlers and not all(self._stage_ready.values()):
                return HostStage.STARTUP
        return None

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
                send_response_or_reject(session.send, message, response, peer_key)
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
        stage = self._not_ready_stage(action)
        if stage is not None:
            logger.debug(f"[HostLink] {action} from {peer_key}: Host 启动中，等待 {stage}")
            return new_not_ready_response(
                request_id, action, stage, NOT_READY_RETRY_AFTER
            )
        handler = self.handlers.get(action)
        if handler is None:
            return new_response(request_id, False, error=f"unknown action: {action}")
        raw_data = message.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        try:
            peer = self._touch_peer(peer_key, action, data)
            return new_response(request_id, True, handler(data, peer))
        except Exception as exc:  # noqa: BLE001 - protocol boundary
            # not_found 是客户端探测存在性（materials.ensure / 删除后复查）的正常结果，
            # 不是服务端故障。
            log = logger.debug if getattr(exc, "code", None) == "not_found" else logger.warning
            log(f"[HostLink] {action} from {peer_key} failed: {exc}")
            return new_response(
                request_id,
                False,
                error=str(exc),
                error_info=exception_error_info(exc),
            )

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
                if not machine_name:
                    raise ValueError("HostLink HELLO 缺少 machine_name，请显式指定节点身份")
                node_id = machine_name
                existing = self._peers.get(node_id)
                if existing and existing.get("connected") and existing.get("addr") != peer_key:
                    raise ValueError(f"machine_name={machine_name!r} 已被活跃连接占用，请指定不同的 --machine-name")
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
                    if action == ActionType.PING and isinstance(
                        data.get("devices"), list
                    ):
                        devices: Dict[str, Dict[str, Any]] = {}
                        for item in data["devices"]:
                            if not isinstance(item, dict):
                                continue
                            device_id = str(item.get("id") or "").strip()
                            if device_id:
                                devices[device_id] = {
                                    **item,
                                    "id": device_id,
                                }
                        peer["devices"] = devices
                        if peer.get("device_ids") != sorted(devices):
                            from unilabos.utils.log_notices import log_notices

                            log_notices.changed(f"slave:{node_id}", sources_changed=True)
                        peer["device_ids"] = sorted(devices)
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
        from unilabos.utils.log_notices import log_notices

        log_notices.changed(f"slave:{node_id}", sources_changed=True)

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
        from unilabos.utils.log_notices import log_notices

        log_notices.changed(f"slave:{peer['node_id']}", sources_changed=True)
        return {
            "server_time": time.time(),
            "heartbeat_timeout": self.heartbeat_timeout,
            **self.hello_payload,
            "assigned_node_id": peer.get("node_id"),
            "device_ids": list(peer.get("device_ids") or []),
            # 附加字段：Host 仍在启动时列出未就绪阶段，Slave 据此提示而不是当作故障
            "pending_stages": self.pending_stages(),
        }

    def _handle_log_changed(self, data: Dict[str, Any], peer: Dict[str, Any]) -> Dict[str, Any]:
        if data or not peer.get("machine_name"):
            raise ValueError("日志通知只接受已注册连接的空通知，不接受正文或其它节点身份")
        from unilabos.utils.log_notices import log_notices

        log_notices.changed(f"slave:{peer['node_id']}")
        return {"accepted": True}

    def _handle_ping(
        self,
        _data: Dict[str, Any],
        _peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "pong": True,
            "server_time": time.time(),
            "devices": list(self.hello_payload.get("devices") or []),
            "pending_stages": self.pending_stages(),
        }

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
    "HOST_STARTUP_GATES",
    "Handler",
    "HostLinkPortInUseError",
    "HostLinkServer",
    "HostStage",
    "NOT_READY_RETRY_AFTER",
    "get_hostlink_server",
    "set_hostlink_server",
]
