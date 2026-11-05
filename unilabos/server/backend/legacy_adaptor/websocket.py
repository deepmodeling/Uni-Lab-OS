"""微后端与 Backend 之间的 ``runtime.v1`` 控制面 WebSocket 轻通知客户端。"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import ssl as ssl_module
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from queue import Empty, Full, PriorityQueue
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from unilabos.backend.hostlink.adapter_registry import get_execution_adapter
from unilabos.config.config import BasicConfig, WSConfig
from unilabos.config.control_token import client_headers
from unilabos.server.backend.legacy_adaptor.session import BaseBackendClient
from unilabos.server.backend.legacy_adaptor.url import build_backend_websocket_url
from unilabos.protocol.runtime.data import RUNTIME_PROTOCOL_VERSION
from unilabos.protocol.runtime.control import (
    BackendHttpRequest,
    EdgeHttpResponse,
    PingNotice,
    PongNotice,
)
from unilabos.utils.log import get_comm_logger

logger = get_comm_logger()


class _PriorityMessageQueue(PriorityQueue):
    """控制面出站队列：应用层 ping/pong 优先于 durable 业务通知。

    对外仍返回原始 dict，保留旧测试和调用方对 ``get_nowait`` 的约定；
    内部用 priority + 单调序号保证同优先级消息 FIFO。
    """

    _FAST_ACTIONS = frozenset({"ping", "pong"})

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize=maxsize)
        self._sequence = itertools.count()

    def put(self, item: dict[str, Any], block: bool = True, timeout: float | None = None) -> None:
        priority = 0 if item.get("action") in self._FAST_ACTIONS else 1
        wrapped = (priority, next(self._sequence), item)
        if priority == 0 and self.maxsize > 0:
            # 心跳不能因为 durable 业务通知把有限出站队列填满而丢失。
            # 直接在 Queue 的条件锁下入队，仍维护 unfinished_tasks，使
            # ``join/task_done`` 语义与普通消息一致；业务消息继续遵守容量。
            with self.not_full:
                self._put(wrapped)
                self.unfinished_tasks += 1
                self.not_empty.notify()
            return
        super().put(wrapped, block=block, timeout=timeout)

    def get(self, block: bool = True, timeout: float | None = None) -> dict[str, Any]:
        _priority, _sequence, item = super().get(block=block, timeout=timeout)
        return item


def _get_business_coordinator() -> Any:
    """延迟解析进程内微后端，避免通信工厂与组合根循环导入。"""

    try:
        from unilabos.server.backend.composition import get_business_coordinator

        return get_business_coordinator()
    except ImportError:
        return None


class BackendWebSocketClient(BaseBackendClient):
    """只传输短通知，各业务域的完整正文固定走 HTTP 数据面。"""

    def __init__(
        self,
        websocket_url: Optional[str] = None,
        *,
        coordinator_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__()
        self.is_disabled = False
        self.client_id = str(uuid.uuid4())
        self.websocket_url = (
            websocket_url
            if websocket_url is not None
            else build_backend_websocket_url()
        ) or ""
        self._coordinator_getter = coordinator_getter or _get_business_coordinator
        self._send_queue: PriorityQueue = _PriorityMessageQueue(maxsize=1000)
        self._running = False
        self._connected = False
        self._session_bound_for_connection = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._websocket: Any = None
        self._reconnect_count = 0
        # 在事件循环线程内创建；stop() 经 call_soon_threadsafe 置位，打断重连等待
        self._stop_event: Optional[asyncio.Event] = None
        # 业务通知串行处理，避免 coordinator 的 HTTP 拉取阻塞 WS 接收器。
        # ping/pong 不进入此队列，始终在接收协程中快速处理。
        self._business_queue: Optional[
            asyncio.Queue[tuple[str, dict[str, Any]]]
        ] = None

    def start(self) -> None:
        if self.is_disabled or self._running:
            return
        if not self.websocket_url:
            # 没有显式 --address 时调度权威仍随本进程装配，控制通知不需要网络连接。
            logger.info(
                "[ControlProtocol] 未配置 --address，调度权威随本进程装配，不建立控制 WebSocket"
            )
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="BackendControlProtocol",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        websocket = self._websocket
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and loop.is_running():
            try:
                if stop_event is not None:
                    # 唤醒正在等重连间隔的循环，否则 join 只能等到超时
                    loop.call_soon_threadsafe(stop_event.set)
                if websocket is not None:
                    asyncio.run_coroutine_threadsafe(websocket.close(), loop)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and not self.is_disabled

    def publish_device_status(
        self, device_status: dict, device_id: str, property_name: str
    ) -> None:
        """设备正文由本地数据 API 提供，不通过控制 WebSocket 发送。"""

    def publish_job_status(
        self,
        feedback_data: dict,
        job_id: str,
        status: str,
        return_info: Optional[dict] = None,
    ) -> None:
        """Job 结果由业务协调器持久化并产生 ``edge_change``。"""

    def send_ping(self, ping_id: str, timestamp: float) -> bool:
        """保留网络诊断所需的短 ping，不携带业务正文。

        字段名与 ``HostAdapterBase.handle_pong_response`` 消费的 pong 一致
        （``ping_id`` / ``client_timestamp`` / ``server_timestamp``）。
        """

        try:
            ping = PingNotice(ping_id=ping_id, client_timestamp=timestamp)
        except Exception as exc:  # noqa: BLE001 - 诊断请求不能污染控制链路
            logger.warning("[ControlProtocol] 无效 ping 字段: %s", exc)
            return False
        return self._queue_message(
            {"action": "ping", "data": ping.model_dump(mode="json")}
        )

    def publish_host_ready(self) -> None:
        """Host 就绪由微后端执行 bridge 消费，无需发送完整设备快照。"""

    def publish_runtime_events(self) -> None:
        """领取 durable outbox 并只发送可供 Backend HTTP 拉取的索引。"""

        if not self.is_connected():
            return
        if not self._session_bound_for_connection:
            return
        coordinator = self._coordinator_getter()
        if coordinator is None:
            return
        for notice in coordinator.claim_edge_changes():
            self._queue_message(
                {
                    "action": "edge_change",
                    "data": notice.model_dump(mode="json", exclude_none=True),
                }
            )

    def _queue_message(self, message: dict[str, Any]) -> bool:
        if self.is_disabled or not self.is_connected():
            return False
        try:
            self._send_queue.put_nowait(message)
            return True
        except Full:
            logger.error("[ControlProtocol] Send queue is full; durable event will retry")
            return False

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._connection_handler())
        except Exception:  # noqa: BLE001 - reconnect loop owns reporting
            logger.error(traceback.format_exc())
        finally:
            self._loop.close()
            self._loop = None

    async def _connection_handler(self) -> None:
        self._stop_event = asyncio.Event()
        while self._running:
            closed_notice: Optional[str] = None
            try:
                ssl_context = None
                if self.websocket_url.startswith("wss://"):
                    ssl_context = ssl_module.create_default_context()
                ws_logger = logging.getLogger("websockets.client")
                ws_logger.setLevel(logging.INFO)
                async with websockets.connect(
                    self.websocket_url,
                    ssl=ssl_context,
                    open_timeout=20,
                    ping_interval=WSConfig.ws_ping_interval,
                    ping_timeout=WSConfig.ws_ping_timeout,
                    close_timeout=5,
                    additional_headers={
                        "Authorization": f"Lab {BasicConfig.auth_secret()}",
                        "EdgeSession": self.client_id,
                        "EdgeProtocol": RUNTIME_PROTOCOL_VERSION,
                        **client_headers(),
                    },
                    logger=ws_logger,
                ) as websocket:
                    self._websocket = websocket
                    self._connected = True
                    self._session_bound_for_connection = False
                    self._reconnect_count = 0
                    logger.info(
                        "[ControlProtocol] Connected to %s", self.websocket_url
                    )
                    sender = asyncio.create_task(
                        self._send_handler(), name="control-protocol-send"
                    )
                    outbox_pump = asyncio.create_task(
                        self._outbox_handler(), name="control-protocol-outbox"
                    )
                    session_watch = asyncio.create_task(
                        self._session_watchdog(), name="control-protocol-session"
                    )
                    # 只缓存短通知；不以有限容量阻塞接收协程，确保 pong
                    # 在业务通知高峰期仍能即时抵达 HostAdapter。
                    business_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = (
                        asyncio.Queue()
                    )
                    self._business_queue = business_queue
                    business_worker = asyncio.create_task(
                        self._business_message_handler(business_queue),
                        name="control-protocol-business",
                    )
                    try:
                        async for raw_message in websocket:
                            await self._handle_raw_message(raw_message)
                    finally:
                        self._connected = False
                        self._session_bound_for_connection = False
                        self._business_queue = None
                        for task in (
                            sender,
                            outbox_pump,
                            session_watch,
                            business_worker,
                        ):
                            task.cancel()
                        for task in (
                            sender,
                            outbox_pump,
                            session_watch,
                            business_worker,
                        ):
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                        self._discard_queued_notices()
            except ConnectionClosed:
                # 先不出声：权威停机时先关 WS、紧接着才请本进程退出，两者只差几毫秒；
                # 等过了重连间隔还在运行，才是真正需要报出来的断线。
                closed_notice = "Backend connection closed"
            except TimeoutError:
                self._report_connect_failure("Backend connection timed out")
            except Exception as exc:  # noqa: BLE001 - reconnect after reporting
                self._report_connect_failure(f"Connection error: {exc}", exc)
            finally:
                self._connected = False
                self._session_bound_for_connection = False
                self._websocket = None

            if not self._running:
                break
            if self._reconnect_count >= WSConfig.max_reconnect_attempts:
                logger.error("[ControlProtocol] Max reconnection attempts reached")
                break
            self._reconnect_count += 1
            try:
                await asyncio.wait_for(self._stop_event.wait(), WSConfig.reconnect_interval)
            except asyncio.TimeoutError:
                pass
            if closed_notice and self._running:
                logger.warning("[ControlProtocol] %s; reconnecting", closed_notice)

    #: 连续重连失败每隔这么多次升一次 ERROR（默认 5s 间隔 ≈ 每分钟一条），其余走 DEBUG
    _RECONNECT_ESCALATE_EVERY = 12

    def _report_connect_failure(self, message: str, exc: Optional[BaseException] = None) -> None:
        """连不上权威：首次 WARNING、之后 DEBUG、长时间不通定期 ERROR；停机中一律 DEBUG。

        权威重启 / 本进程被权威叫停时连接被拒是正常现象，逐次 ERROR + 堆栈只会淹掉真问题。
        网络类异常（拒绝连接、超时、握手失败）本身就是原因，不再附堆栈。
        """

        if not self._running:
            logger.debug("[ControlProtocol] %s (shutting down)", message)
            return
        # 连接成功会把计数归零：attempt 0 是进程刚启动的首连，1 是断线后的首次重连
        attempt = self._reconnect_count
        if attempt <= 1:
            logger.warning(
                "[ControlProtocol] %s; retrying every %ss", message, WSConfig.reconnect_interval
            )
        elif attempt % self._RECONNECT_ESCALATE_EVERY == 0:
            logger.error(
                "[ControlProtocol] %s; still unreachable after %d attempts", message, attempt
            )
        else:
            logger.debug("[ControlProtocol] %s (attempt %d)", message, attempt)
        network_error = isinstance(exc, (OSError, TimeoutError, WebSocketException))
        if exc is not None and not network_error:
            logger.debug(traceback.format_exc())

    async def _handle_raw_message(self, raw_message: str | bytes) -> None:
        try:
            envelope = json.loads(raw_message)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            logger.warning("[ControlProtocol] Ignore invalid JSON message")
            return
        if not isinstance(envelope, dict):
            logger.warning("[ControlProtocol] Ignore non-object message")
            return
        action = str(envelope.get("action") or "")
        data = envelope.get("data", {})
        if not isinstance(data, dict):
            logger.warning("[ControlProtocol] Ignore %s with non-object data", action)
            return
        # 心跳是控制协议的特殊字段，必须绕过可能阻塞的业务协调器。
        if action in {"ping", "pong"}:
            try:
                await self._process_message(action, data)
            except Exception:  # noqa: BLE001 - 坏心跳只丢弃当前消息
                logger.error(
                    "[ControlProtocol] 处理 %s 心跳失败，已丢弃该消息:\n%s",
                    action,
                    traceback.format_exc(),
                )
            return
        if action == "backend_http":
            # Backend 要本进程执行一条 HTTP 请求（本进程不监听端口）：并发执行，不能
            # 排在串行业务队列里挡住命令，也不能让慢请求（受管进程启动）挡住别的请求。
            asyncio.create_task(
                self._serve_backend_http(data), name="control-protocol-backend-http"
            )
            return

        business_queue = self._business_queue
        if business_queue is not None:
            # 队列 worker 保持 backend_session/backend_change/ack 的顺序；
            # 接收协程立即回到 async for，后续 pong 不再排队等待 HTTP。
            await business_queue.put((action, data))
            return

        try:
            await self._process_message(action, data)
        except Exception:  # noqa: BLE001 - 单条坏消息不能触发断线重连循环
            # 命令权威仍在 Backend；校验失败（身份/哈希不匹配、拉取失败）只
            # 记录并丢弃这条通知，Backend 重发或人工介入时再处理。
            logger.error(
                "[ControlProtocol] 处理 %s 失败，已丢弃该通知:\n%s",
                action,
                traceback.format_exc(),
            )

    async def _business_message_handler(
        self, business_queue: "asyncio.Queue[tuple[str, dict[str, Any]]]"
    ) -> None:
        """串行消费业务通知；单条通知失败不影响接收和心跳。"""

        while True:
            action, data = await business_queue.get()
            try:
                await self._process_message(action, data)
            except Exception:  # noqa: BLE001 - 业务通知失败可由 durable outbox 重放
                logger.error(
                    "[ControlProtocol] 处理 %s 失败，已丢弃该通知:\n%s",
                    action,
                    traceback.format_exc(),
                )
            finally:
                business_queue.task_done()

    async def _process_message(
        self, action: str, data: dict[str, Any]
    ) -> None:
        if action == "pong":
            # 同一份规范化 pong 同时唤醒两类调用方：
            # 1) BackendSession 的独立控制面诊断（ping_control_link）；
            # 2) HostAdapter.test_latency 的执行适配器等待者。
            # 两者各自只接受自己登记过的 ping_id，因此不会互相污染。
            pong = PongNotice.model_validate(data)
            normalized = pong.model_dump(mode="json")
            self.handle_pong(normalized)
            host_node = get_execution_adapter(0)
            if host_node is not None:
                host_node.handle_pong_response(normalized)
            return
        if action == "ping":
            ping = PingNotice.model_validate(data)
            # 反向探测同样使用完整 pong 契约。虽然当前延迟诊断由 Edge
            # 发起 ping，但 Backend 可能在连接保活/诊断时主动发起 ping；
            # 缺少 server_timestamp 会让对端把响应当成坏包丢弃。
            pong = PongNotice(
                ping_id=ping.ping_id,
                client_timestamp=ping.client_timestamp,
                server_timestamp=time.time(),
            )
            self._queue_message(
                {"action": "pong", "data": pong.model_dump(mode="json")}
            )
            return
        coordinator = self._coordinator_getter()
        if action not in {"backend_session", "backend_change", "edge_change_ack"}:
            logger.warning("[ControlProtocol] Ignore unsupported action: %s", action)
            return
        if coordinator is None:
            raise RuntimeError("workflow business coordinator is not available")
        if action == "backend_session":
            await asyncio.to_thread(coordinator.bind_backend_session, data)
            self._session_bound_for_connection = True
            await asyncio.to_thread(self.publish_runtime_events)
        elif action == "backend_change":
            await asyncio.to_thread(coordinator.handle_backend_notice, data)
        else:
            await asyncio.to_thread(coordinator.acknowledge_edge_changes, data)

    # ── Backend → 本进程 HTTP（本进程不监听端口） ──────────────────────

    _HOP_HEADERS = frozenset({"content-length", "transfer-encoding", "connection", "keep-alive"})

    async def _serve_backend_http(self, data: dict[str, Any]) -> None:
        """对本进程的 ASGI 应用执行 Backend 下发的请求，并把结果用 HTTP 送回 Backend。"""

        try:
            request = BackendHttpRequest.model_validate(data)
        except Exception:  # noqa: BLE001 - 坏请求丢弃，Backend 侧会超时
            logger.warning("[ControlProtocol] 忽略无效 backend_http 请求")
            return
        try:
            response = await self._execute_local_http(request)
        except Exception as exc:  # noqa: BLE001 - 执行失败也要回一个响应，别让 Backend 干等
            logger.exception("[ControlProtocol] backend_http %s %s 执行失败", request.method, request.path)
            body = json.dumps({"detail": f"host request failed: {exc}"}).encode("utf-8")
            response = EdgeHttpResponse(
                request_uuid=request.request_uuid,
                status_code=500,
                headers={"content-type": "application/json"},
                body_base64=base64.b64encode(body).decode("ascii"),
            )
        await self._post_http_response(response)

    async def _execute_local_http(self, request: BackendHttpRequest) -> EdgeHttpResponse:
        import httpx

        from unilabos.server.api.app import app, wait_routes_ready

        # 控制 WS 先于主线程的 setup_server 连上权威：极快的动作可能在路由挂好前就已上报终态，
        # 权威随即来拉结果 payload。路由未挂好时对 app 执行只会得到误导性的 404，这里先等。
        wait_seconds = min(30.0, float(request.timeout_seconds or 30.0))
        if not await asyncio.to_thread(wait_routes_ready, wait_seconds):
            body = json.dumps({"detail": "host management routes are not mounted yet"}).encode("utf-8")
            return EdgeHttpResponse(
                request_uuid=request.request_uuid,
                status_code=503,
                headers={"content-type": "application/json"},
                body_base64=base64.b64encode(body).decode("ascii"),
            )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://host.local") as client:
            upstream = await client.request(
                request.method,
                request.path,
                headers={k: v for k, v in request.headers.items() if k.lower() not in self._HOP_HEADERS},
                content=base64.b64decode(request.body_base64) if request.body_base64 else None,
                timeout=request.timeout_seconds,
            )
        return EdgeHttpResponse(
            request_uuid=request.request_uuid,
            status_code=upstream.status_code,
            headers={k: v for k, v in upstream.headers.items() if k.lower() not in self._HOP_HEADERS},
            body_base64=base64.b64encode(upstream.content).decode("ascii"),
        )

    async def _post_http_response(self, response: EdgeHttpResponse) -> None:
        import httpx

        url = f"{self.backend_url()}/api/v1/edge/http-responses/{response.request_uuid}"
        secret = BasicConfig.auth_secret()
        headers = {"Authorization": f"Lab {secret}"} if secret else {}
        headers.update(client_headers())
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                reply = await client.post(url, json=response.model_dump(mode="json"), headers=headers)
            if reply.status_code >= 300:
                logger.warning(
                    "[ControlProtocol] 回送 backend_http 结果失败: HTTP %s %s",
                    reply.status_code,
                    reply.text[:200],
                )
        except httpx.HTTPError as exc:
            logger.warning("[ControlProtocol] 回送 backend_http 结果失败: %s", exc)

    async def _send_handler(self) -> None:
        while self._connected and self._websocket is not None:
            try:
                message = self._send_queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.1)
                continue
            try:
                await self._websocket.send(json.dumps(message, ensure_ascii=False))
            except Exception:  # noqa: BLE001 - closing forces durable replay
                await self._websocket.close()
                raise

    async def _outbox_handler(self) -> None:
        """周期领取到期通知，覆盖断线、满队列和 ACK 超时后的重放。"""

        while self._connected:
            if self._session_bound_for_connection:
                await asyncio.to_thread(self.publish_runtime_events)
            await asyncio.sleep(1)

    _SESSION_BIND_WARN_SECONDS = 30.0

    async def _session_watchdog(self) -> None:
        """连上却始终收不到 ``backend_session``：对端多半不是 runtime.v1 后端。"""

        await asyncio.sleep(self._SESSION_BIND_WARN_SECONDS)
        if self._connected and not self._session_bound_for_connection:
            logger.warning(
                "[ControlProtocol] 已连接 %s 但 %.0fs 内未收到 backend_session；"
                "对端不是 runtime.v1 微后端，请检查 --address 指向的服务",
                self.websocket_url,
                self._SESSION_BIND_WARN_SECONDS,
            )

    def _discard_queued_notices(self) -> None:
        """断线后丢弃内存副本；未 ACK 事件由 durable outbox 重放。"""

        while True:
            try:
                self._send_queue.get_nowait()
            except Empty:
                return


__all__ = ["BackendWebSocketClient"]
