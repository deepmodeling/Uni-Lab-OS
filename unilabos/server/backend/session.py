#!/usr/bin/env python
# coding=utf-8
"""Backend 连接会话抽象与生命周期工厂。"""

from abc import ABC, abstractmethod
from typing import Optional

from unilabos.legacy_support import legacy_support_enabled
from unilabos.utils import logger

APP_BRIDGES = ("websocket",)
COMMUNICATION_PROTOCOL = "websocket"


class BaseBackendClient(ABC):
    """
    通信客户端抽象基类

    定义了所有通信客户端（WebSocket等）需要实现的接口。
    """

    def __init__(self):
        self.is_disabled = True
        self.client_id = ""

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
    def send_ping(self, ping_id: str, timestamp: float) -> None:
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


class BackendSessionFactory:
    """创建固定 WebSocket 传输的后端客户端；--legacy 只改变线协议。"""

    _client_cache: Optional[BaseBackendClient] = None

    @classmethod
    def create_client(cls) -> BaseBackendClient:
        """
        创建通信客户端实例

        Returns:
            通信客户端实例
        """
        if legacy_support_enabled():
            return cls._create_legacy_client()
        return cls._create_backend_client()

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
        """创建新微后端的通用 Backend WS 轻通知客户端。"""

        from unilabos.server.backend.websocket import BackendWebSocketClient

        return BackendWebSocketClient()

    @classmethod
    def _create_legacy_client(cls) -> BaseBackendClient:
        """创建旧后端完整 WebSocket payload 客户端。"""

        from unilabos.legacy_support.websocket import LegacyWebSocketClient

        return LegacyWebSocketClient()

    @classmethod
    def reset_client(cls):
        """重置客户端缓存（用于测试或重新配置）"""
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
