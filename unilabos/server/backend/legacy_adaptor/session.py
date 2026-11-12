#!/usr/bin/env python
# coding=utf-8
"""Backend 连接会话抽象与生命周期工厂。

Edge 只通过这里的会话对象接触 Backend。Backend（微后端 + 调度权威）与 Host 分进程——Host
要能安全重启而不影响调度——所以 Edge → Backend 永远是同一个地址上的两条网络链路：

- **HTTP 数据面**：runtime.v1 / materials / telemetry / history 等 API；
- **控制面**：同地址同端口的 runtime.v1 控制 WebSocket（``/api/v1/ws/schedule``：命令通知、
  心跳、ping / pong）。

地址只有一条规则：``--address`` 给了用给的，没给就是本机自己的 Backend 端口
（``HTTPConfig.backend_port``）。``describe_links()`` 把两条链路描述成可探测对象（目标、
可用性、ping），调用方（如 host_node 的 test_latency）不自己判断拓扑；ping / pong 的簿记
也收在会话里。
"""

import json
import threading
import time
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from unilabos.utils import logger

APP_BRIDGES = ("websocket",)
COMMUNICATION_PROTOCOL = "websocket"

#: 控制面 ping-pong 单次等待上限（秒），monotonic 计时
CONTROL_PING_TIMEOUT_SECONDS = 10.0
#: HTTP ping 单次超时（秒）
HTTP_PING_TIMEOUT_SECONDS = 3.0


@dataclass
class BackendLink:
    """会话的一条可探测链路。``ping(timeout_s)`` 返回对端时钟（epoch 秒），失败返回 None。"""

    name: str
    transport: str
    target: str
    available: bool
    reason: str = ""
    ping: Optional[Callable[[float], Optional[float]]] = field(default=None, repr=False)

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "target": self.target,
            "available": self.available,
            "reason": self.reason,
        }


class BaseBackendClient(ABC):
    """
    通信客户端抽象基类

    定义了所有通信客户端（WebSocket等）需要实现的接口。
    """

    def __init__(self):
        self.is_disabled = True
        self.client_id = ""
        # 控制面 ping-pong 簿记：ping_id → 等待事件 / 规范化 pong
        self._ping_lock = threading.Lock()
        self._ping_waiters: Dict[str, threading.Event] = {}
        self._ping_responses: Dict[str, Dict[str, Any]] = {}

    @abstractmethod
    def start(self) -> None:
        """
        启动通信客户端连接
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        停止通信客户端连接
        """
        pass

    @abstractmethod
    def publish_device_status(self, device_status: dict, device_id: str, property_name: str) -> None:
        """
        发布设备状态信息

        Args:
            device_status: 设备状态字典
            device_id: 设备ID
            property_name: 属性名称
        """
        pass

    @abstractmethod
    def publish_job_status(
        self, feedback_data: dict, job_id: str, status: str, return_info: Optional[dict] = None
    ) -> None:
        """
        发布作业状态信息

        Args:
            feedback_data: 反馈数据
            job_id: 作业ID
            status: 作业状态
            return_info: 返回信息
        """
        pass

    @abstractmethod
    def send_ping(self, ping_id: str, timestamp: float) -> bool:
        """
        发送ping消息

        Args:
            ping_id: ping ID
            timestamp: 时间戳
        """
        pass

    def publish_action_lock(self, device_id: str, action_name: str, free: bool) -> None:
        """
        主动上报单个 device+action 的锁(可用性)状态(默认空实现)

        Args:
            device_id: 设备ID
            action_name: 动作名称
            free: 是否空闲(True 空闲, False 占用)
        """
        pass

    def publish_action_locks(self, locks: list) -> None:
        """
        批量主动上报 device+action 的锁(可用性)状态(默认空实现)

        Args:
            locks: [{"device_id": str, "action_name": str, "free": bool}, ...]
        """
        pass

    def setup_pong_subscription(self) -> None:
        """
        设置pong消息订阅（可选实现）
        """
        pass

    @property
    def is_connected(self) -> bool:
        """
        检查是否已连接

        Returns:
            是否已连接
        """
        return not self.is_disabled

    # ── 拓扑：Edge 实际连接的 Backend 在哪 ────────────────────────────

    def connected(self) -> bool:
        """``is_connected`` 的统一读法（子类有的是属性、有的是方法）。"""

        probe = getattr(self, "is_connected", None)
        try:
            value = probe() if callable(probe) else probe
        except Exception:  # noqa: BLE001 - 状态探测失败按未连接处理
            return False
        return bool(value)

    @staticmethod
    def configured_backend_url() -> str:
        """显式配置的 Backend 地址（``--address`` / ``HTTPConfig.remote_addr``）；未配置为空串。"""

        from unilabos.config.config import HTTPConfig

        return str(getattr(HTTPConfig, "remote_addr", "") or "").strip().rstrip("/")

    @staticmethod
    def default_backend_url() -> str:
        """未显式配置时的 Backend 地址：本机自己的 Backend 端口。"""

        from unilabos.config.config import HTTPConfig

        port = int(getattr(HTTPConfig, "backend_port", 8081) or 8081)
        return f"http://127.0.0.1:{port}"

    @classmethod
    def backend_url(cls) -> str:
        """Edge 连接的 Backend 地址：配置的，否则本机 Backend 端口。HTTP 与 WS 都从它派生。"""

        return cls.configured_backend_url() or cls.default_backend_url()

    @classmethod
    def address_source(cls) -> str:
        """``configured``（--address 显式给出）或 ``default``（本机 Backend 端口）。"""

        return "configured" if cls.configured_backend_url() else "default"

    @classmethod
    def http_base_url(cls) -> str:
        """HTTP 数据面地址 = Backend 地址。"""

        return cls.backend_url()

    def control_link_target(self) -> str:
        """控制面 WebSocket 地址：客户端已建的 websocket_url，否则按同一 Backend 地址派生。"""

        explicit = str(getattr(self, "websocket_url", "") or "")
        if explicit:
            return explicit
        from unilabos.utils.address import derive_websocket_address

        try:
            return derive_websocket_address(self.backend_url())
        except ValueError:
            return ""

    # ── 链路探测 ─────────────────────────────────────────────────────

    def http_ping(self, timeout_s: float = HTTP_PING_TIMEOUT_SECONDS) -> Optional[float]:
        """对数据面 ``GET /api/v1/ping`` 做一次往返，返回服务端时钟；失败返回 None。"""

        base = self.http_base_url()
        url = f"{base}/api/v1/ping?client_timestamp={time.time()!r}"
        try:
            from unilabos.client.utils.envelope import unwrap_envelope

            with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - 目标是配置的 Backend
                payload = unwrap_envelope(json.loads(response.read().decode("utf-8")))
            return float(payload["server_timestamp"])
        except Exception as exc:  # noqa: BLE001 - 诊断失败不能影响主流程
            logger.debug("[BackendSession] HTTP ping 失败 %s: %s", url, exc)
            return None

    def handle_pong(self, pong_data: Dict[str, Any]) -> bool:
        """接收线程送来的 pong：只唤醒当前登记过的等待者，迟到 / 伪造的丢弃。"""

        from unilabos.protocol.runtime.control import PongNotice

        try:
            pong = PongNotice.model_validate(pong_data)
        except Exception as exc:  # noqa: BLE001 - 坏包不能打断接收线程
            logger.debug("[BackendSession] 忽略无效 pong: %s", exc)
            return False
        with self._ping_lock:
            waiter = self._ping_waiters.get(pong.ping_id)
            if waiter is None:
                return False
            self._ping_responses[pong.ping_id] = pong.model_dump(mode="json")
            waiter.set()
        return True

    def ping_control_link(self, timeout_s: float = CONTROL_PING_TIMEOUT_SECONDS) -> Optional[float]:
        """控制面一次 ping-pong，返回对端时钟；发送失败 / 超时 / 断线返回 None。"""

        ping_id = str(uuid.uuid4())
        waiter = threading.Event()
        with self._ping_lock:
            self._ping_responses.pop(ping_id, None)
            self._ping_waiters[ping_id] = waiter
        try:
            try:
                sent = self.send_ping(ping_id, time.time())
            except Exception as exc:  # noqa: BLE001 - 单次发送失败按本轮失败记
                logger.debug("[BackendSession] 控制面 ping 发送失败: %s", exc)
                sent = False
            if sent is False:
                return None
            # 小粒度等待并顺带看连接态：已经断线就不再盲等到超时
            deadline = time.monotonic() + float(timeout_s)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if waiter.wait(timeout=min(0.25, remaining)):
                    break
                if not self.connected():
                    return None
            with self._ping_lock:
                pong = self._ping_responses.get(ping_id)
            if pong is None:
                return None
            return float(pong["server_timestamp"])
        finally:
            with self._ping_lock:
                self._ping_waiters.pop(ping_id, None)
                self._ping_responses.pop(ping_id, None)

    def describe_links(self) -> List[BackendLink]:
        """Edge → Backend 同一地址上的两条网络链路：HTTP 数据面 + 控制 WebSocket。

        控制面只有 WebSocket 一种传输；没连上就是 ``not_connected``（目标地址照样给出，
        便于看出 Backend 进程没起来 / 端口不对）。
        """

        connected = self.connected()
        return [
            BackendLink(
                name="http",
                transport="http",
                target=f"{self.http_base_url()}/api/v1/ping",
                available=True,
                ping=self.http_ping,
            ),
            BackendLink(
                name="control",
                transport="websocket",
                target=self.control_link_target(),
                available=connected,
                reason="" if connected else "not_connected",
                ping=self.ping_control_link,
            ),
        ]


class BackendSessionFactory:
    """创建 Edge 到微后端的唯一 ``runtime.v1`` 客户端。

    Edge 的调度面只有 runtime.v1：完整的命令、工作流、物料和调度事实
    都由微后端承接。旧云端的字段转换保留在
    ``server.backend.legacy_adaptor.legacy``，由 Backend 侧显式装配，不能
    再通过启动时探测把另一套调度协议带回 Edge。
    """

    _client_cache: Optional[BaseBackendClient] = None

    @classmethod
    def create_client(cls) -> BaseBackendClient:
        """
        创建通信客户端实例

        Returns:
            通信客户端实例
        """
        return cls._create_backend_client()

    @classmethod
    def protocol(cls) -> str:
        """Edge 线协议固定为 ``runtime.v1``。"""

        return "runtime.v1"

    @classmethod
    def is_legacy(cls) -> bool:
        """兼容旧调用方的查询接口；Edge 不再自动切换 legacy。"""

        return False

    @classmethod
    def get_client(cls) -> BaseBackendClient:
        """
        获取通信客户端实例（单例模式）

        Returns:
            通信客户端实例
        """
        if cls._client_cache is None:
            cls._client_cache = cls.create_client()
            logger.trace(
                "[BackendSession] Created %s client",
                type(cls._client_cache).__name__,
            )

        return cls._client_cache

    @classmethod
    def _create_backend_client(cls) -> BaseBackendClient:
        """创建 runtime.v1 WebSocket 轻通知客户端。"""

        from unilabos.server.backend.legacy_adaptor.websocket import BackendWebSocketClient

        return BackendWebSocketClient()

    @staticmethod
    def create_legacy_client() -> BaseBackendClient:
        """供 Backend 侧显式接入旧云端时使用的兼容客户端。

        该入口刻意不参与 Edge 的默认工厂，避免旧协议探测或字段镜像影响
        当前微后端调度链路。
        """

        from unilabos.server.backend.legacy_adaptor.legacy.ws import (
            LegacyBackendWebSocketClient,
        )

        return LegacyBackendWebSocketClient()

    @classmethod
    def reset_client(cls):
        """重置客户端缓存（用于测试或重新配置）。"""
        if cls._client_cache:
            try:
                cls._client_cache.stop()
            except Exception as e:
                logger.warning(f"[CommunicationFactory] Error stopping client: {str(e)}")

        cls._client_cache = None
        logger.info("[BackendSession] Client cache reset")


def get_backend_client() -> BaseBackendClient:
    """返回当前 Backend 会话客户端。"""

    return BackendSessionFactory.get_client()


__all__ = [
    "APP_BRIDGES",
    "BackendSessionFactory",
    "BaseBackendClient",
    "COMMUNICATION_PROTOCOL",
    "get_backend_client",
]
