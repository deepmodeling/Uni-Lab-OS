from functools import wraps
from typing import Any, Callable, Optional, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def singleton(cls):
    """
    单例装饰器
    确保被装饰的类只有一个实例
    """
    instances = {}

    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return get_instance


def subscribe(
    topic: str,
    msg_type: Optional[type] = None,
    qos: int = 10,
) -> Callable[[F], F]:
    """
    Topic订阅装饰器

    用于装饰 driver 类中的方法，使其成为 ROS topic 的订阅回调。
    当 ROS2DeviceNode 初始化时，会自动扫描并创建对应的订阅者。

    Args:
        topic: Topic 名称模板，支持以下占位符：
            - {device_id}: 设备ID (如 "pump_1")
            - {namespace}: 完整命名空间 (如 "/devices/pump_1")
        msg_type: ROS 消息类型。如果为 None，需要在回调函数的类型注解中指定
        qos: QoS 深度配置，默认为 10
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper._subscribe_topic = topic  # type: ignore[attr-defined]
        wrapper._subscribe_msg_type = msg_type  # type: ignore[attr-defined]
        wrapper._subscribe_qos = qos  # type: ignore[attr-defined]
        wrapper._has_subscribe = True  # type: ignore[attr-defined]

        return wrapper  # type: ignore[return-value]

    return decorator


def get_subscribe_config(func) -> dict:
    """获取函数上的订阅配置 (topic, msg_type, qos)"""
    if hasattr(func, "_has_subscribe") and getattr(func, "_has_subscribe", False):
        return {
            "topic": getattr(func, "_subscribe_topic", None),
            "msg_type": getattr(func, "_subscribe_msg_type", None),
            "qos": getattr(func, "_subscribe_qos", 10),
        }
    return {}


def get_all_subscriptions(instance) -> list:
    """
    扫描实例的所有方法，获取带有 @subscribe 装饰器的方法及其配置

    Returns:
        包含 (method_name, method, config) 元组的列表
    """
    subscriptions = []
    for attr_name in dir(instance):
        if attr_name.startswith("_"):
            continue
        try:
            attr = getattr(instance, attr_name)
            if callable(attr):
                config = get_subscribe_config(attr)
                if config:
                    subscriptions.append((attr_name, attr, config))
        except Exception:
            pass
    return subscriptions


# ---------------------------------------------------------------------------
# 向后兼容重导出 -- 已迁移到 unilabos.registry.decorators
# ---------------------------------------------------------------------------
from unilabos.registry.decorators import (  # noqa: E402, F401
    topic_config,
    get_topic_config,
    always_free,
    is_always_free,
    not_action,
    is_not_action,
)
