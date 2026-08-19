#!/usr/bin/env python
# coding=utf-8
"""后端通信协议抽象与客户端工厂。"""

from abc import ABC, abstractmethod
from typing import Optional
from unilabos.config.config import BasicConfig
from unilabos.utils import logger


PROTOCOL_ALIASES = {
    "control": "control",
    "control.v1": "control",
    "old": "old",
    "legacy": "old",
    "old-websocket": "old",
    "websocket": "old",
    "ws": "old",
}


def normalize_communication_protocol(protocol: str) -> str:
    """规范化后端线协议名称；旧 ``websocket`` 配置继续映射到 ``old``。"""

    value = str(protocol or "").strip().lower()
    try:
        return PROTOCOL_ALIASES[value]
    except KeyError as exc:
        supported = ", ".join(CommunicationClientFactory.get_supported_protocols())
        raise ValueError(
            f"Unsupported backend communication protocol {protocol!r}; "
            f"expected one of: {supported}"
        ) from exc


class BaseCommunicationClient(ABC):
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


class CommunicationClientFactory:
    """
    通信客户端工厂类

    根据配置文件中的通信协议设置创建相应的客户端实例。
    """

    _client_cache: Optional[BaseCommunicationClient] = None

    @classmethod
    def create_client(cls, protocol: Optional[str] = None) -> BaseCommunicationClient:
        """
        创建通信客户端实例

        Args:
            protocol: 指定的协议类型，如果为None则使用配置文件中的设置

        Returns:
            通信客户端实例

        Raises:
            ValueError: 当协议类型不支持时
        """
        if protocol is None:
            protocol = BasicConfig.communication_protocol

        normalized = normalize_communication_protocol(protocol)
        if normalized == "control":
            return cls._create_control_client()
        return cls._create_old_protocol_client()

    @classmethod
    def get_client(cls, protocol: Optional[str] = None) -> BaseCommunicationClient:
        """
        获取通信客户端实例（单例模式）

        Args:
            protocol: 指定的协议类型，如果为None则使用配置文件中的设置

        Returns:
            通信客户端实例
        """
        if cls._client_cache is None:
            cls._client_cache = cls.create_client(protocol)
            logger.trace(f"[CommunicationFactory] Created {type(cls._client_cache).__name__} client")

        return cls._client_cache

    @classmethod
    def _create_control_client(cls) -> BaseCommunicationClient:
        """创建新微后端的 WS 轻通知客户端。"""

        from unilabos.app.backend_protocol.control import ControlWebSocketClient

        return ControlWebSocketClient()

    @classmethod
    def _create_old_protocol_client(cls) -> BaseCommunicationClient:
        """创建旧后端完整 WebSocket payload 客户端。"""

        try:
            from unilabos.app.backend_protocol.old import OldBackendProtocolClient

            return OldBackendProtocolClient()
        except Exception as e:
            logger.error(
                "[CommunicationFactory] Failed to create old protocol client: "
                f"{str(e)}"
            )
            raise

    @classmethod
    def reset_client(cls):
        """重置客户端缓存（用于测试或重新配置）"""
        if cls._client_cache:
            try:
                cls._client_cache.stop()
            except Exception as e:
                logger.warning(f"[CommunicationFactory] Error stopping old client: {str(e)}")

        cls._client_cache = None
        logger.info("[CommunicationFactory] Client cache reset")

    @classmethod
    def get_supported_protocols(cls) -> list[str]:
        """
        获取支持的协议列表

        Returns:
            支持的协议列表
        """
        return ["control", "old"]


def get_communication_client(protocol: Optional[str] = None) -> BaseCommunicationClient:
    """
    获取通信客户端实例的便捷函数

    Args:
        protocol: 指定的协议类型，如果为None则使用配置文件中的设置

    Returns:
        通信客户端实例
    """
    return CommunicationClientFactory.get_client(protocol)


__all__ = [
    "BaseCommunicationClient",
    "CommunicationClientFactory",
    "get_communication_client",
    "normalize_communication_protocol",
]
