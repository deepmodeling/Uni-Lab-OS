"""微后端与 Backend 之间的 ``control.v1`` WebSocket 轻通知客户端。"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl as ssl_module
import threading
import traceback
import uuid
from collections.abc import Callable
from queue import Empty, Full, Queue
from typing import Any, Optional

import websockets

from unilabos.app.execution_adapter import get_execution_adapter
from unilabos.config.config import BasicConfig, WSConfig
from unilabos.server.backend.session import BaseBackendClient
from unilabos.server.backend.url import build_backend_websocket_url
from unilabos.server.protocol.control import CONTROL_PROTOCOL_VERSION
from unilabos.utils.log import get_comm_logger

logger = get_comm_logger()


def _get_business_coordinator() -> Any:
    """延迟解析进程内微后端，避免通信工厂与组合根循环导入。"""

    try:
        from unilabos.server.scheduler.integration import get_business_coordinator

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
        self._send_queue: Queue[dict[str, Any]] = Queue(maxsize=1000)
        self._running = False
        self._connected = False
        self._session_bound_for_connection = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._websocket: Any = None
        self._reconnect_count = 0

    def start(self) -> None:
        if self.is_disabled or self._running:
            return
        if not self.websocket_url:
            logger.error("[ControlProtocol] Backend WebSocket URL not configured")
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
        if websocket is not None and loop is not None and loop.is_running():
            try:
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

    def send_ping(self, ping_id: str, timestamp: float) -> None:
        """保留网络诊断所需的短 ping，不携带业务正文。"""

        self._queue_message(
            {"action": "ping", "data": {"id": ping_id, "timestamp": timestamp}}
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
        while self._running:
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
                        "EdgeProtocol": CONTROL_PROTOCOL_VERSION,
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
                    try:
                        async for raw_message in websocket:
                            await self._handle_raw_message(raw_message)
                    finally:
                        self._connected = False
                        self._session_bound_for_connection = False
                        for task in (sender, outbox_pump):
                            task.cancel()
                        for task in (sender, outbox_pump):
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                        self._discard_queued_notices()
            except websockets.exceptions.ConnectionClosed:
                logger.warning("[ControlProtocol] Backend connection closed")
            except TimeoutError:
                logger.warning("[ControlProtocol] Backend connection timed out")
            except Exception as exc:  # noqa: BLE001 - reconnect after reporting
                logger.error("[ControlProtocol] Connection error: %s", exc)
                logger.debug(traceback.format_exc())
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
            await asyncio.sleep(WSConfig.reconnect_interval)

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
        await self._process_message(action, data)

    async def _process_message(
        self, action: str, data: dict[str, Any]
    ) -> None:
        coordinator = self._coordinator_getter()
        if action == "pong":
            host_node = get_execution_adapter(0)
            if host_node is not None:
                host_node.handle_pong_response(data)
            return
        if action == "ping":
            self._queue_message({"action": "pong", "data": data})
            return
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

    def _discard_queued_notices(self) -> None:
        """断线后丢弃内存副本；未 ACK 事件由 durable outbox 重放。"""

        while True:
            try:
                self._send_queue.get_nowait()
            except Empty:
                return


__all__ = ["BackendWebSocketClient"]
