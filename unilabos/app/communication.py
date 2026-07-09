#!/usr/bin/env python
# coding=utf-8
"""
通信模块

提供WebSocket的统一接口，支持通过配置选择通信协议。
包含通信抽象层基类和通信客户端工厂。
"""

from abc import ABC, abstractmethod
from typing import Optional
from unilabos.config.config import BasicConfig
from unilabos.utils import logger


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

    def publish_joint_state(self, node_uuid: str, joint_states: dict, resource_poses: dict = None) -> None:
        """
        发布高频关节状态数据（push_joint_state action，不写 DB）

        Args:
            node_uuid: 设备节点的云端 UUID
            joint_states: 关节名 → 角度/位置 的映射
            resource_poses: 物料附着映射（可选）
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

        protocol = protocol.lower()

        if protocol == "websocket":
            return cls._create_websocket_client()
        else:
            logger.error(f"[CommunicationFactory] Unsupported protocol: {protocol}")
            logger.warning(f"[CommunicationFactory] Falling back to WebSocket")
            return cls._create_websocket_client()

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
    def _create_websocket_client(cls) -> BaseCommunicationClient:
        """创建WebSocket客户端"""
        try:
            from unilabos.app.ws_client import WebSocketClient

            return WebSocketClient()
        except Exception as e:
            logger.error(f"[CommunicationFactory] Failed to create WebSocket client: {str(e)}")
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
        return ["websocket"]


def get_communication_client(protocol: Optional[str] = None) -> BaseCommunicationClient:
    """
    获取通信客户端实例的便捷函数

    Args:
        protocol: 指定的协议类型，如果为None则使用配置文件中的设置

    Returns:
        通信客户端实例
    """
    return CommunicationClientFactory.get_client(protocol)
