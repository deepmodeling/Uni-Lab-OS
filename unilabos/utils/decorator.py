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
    topic: Optional[str] = None,
    msg_type: Optional[type] = None,
    qos: int = 10,
    *,
    device_id: Optional[str] = None,
    status_name: Optional[str] = None,
    trigger_when_change: bool = False,
    retry_interval: Optional[float] = None,
) -> Callable[[F], F]:
    """
    Topic 订阅装饰器

    用于装饰 driver 类中的方法，使其成为 **跨设备** ROS topic 的订阅回调。
    当 ROS2DeviceNode 初始化时，会自动扫描并创建对应的订阅者。

    仅支持订阅 **其它设备** 的状态（订阅本设备自己的状态没有意义，直接用 getter 读取即可）。
    订阅目标支持两种等价写法：

    1. 绝对路径（必须以 ``/`` 开头）::

           @subscribe("/devices/pump_1/pressure")

    2. 拆分写法（可读性更好，``device_id`` 与 ``status_name`` 都必填）::

           @subscribe(device_id="pump_1", status_name="pressure")

    回调收到的值统一经 ``convert_from_ros_msg`` 转换：``std_msgs`` 这类基础消息直接得到原生值
    （如 ``Int32 -> int``），复合消息得到递归转换后的 dict（与 topic 发布、call_device_action
    结果解析等通道一致）。框架不再对消息做额外解包，拿到什么由消息类型决定。

    Args:
        topic: 目标 topic 的绝对路径（以 ``/`` 开头）。与 ``device_id`` + ``status_name``
            二选一即可。
        msg_type: ROS 消息类型。**通常无需填写**——框架会按以下优先级自动识别：
            1) 显式传入的 ``msg_type``；
            2) 运行时 ROS 图中该 topic 已有发布者声明的类型；
            3) 回调函数首个参数的类型注解。
        qos: QoS 深度配置，默认为 10。
        device_id: 拆分写法中的目标设备 ID（必填）。
        status_name: 拆分写法中的状态名（即 topic 末段，必填）。
        trigger_when_change: 为 True 时，仅当本次收到的值与上一次不同才触发回调
            （去抖 / 边沿触发）；默认 False，每条消息都触发。比较的是回调实际收到的值。
        retry_interval: 重试建立订阅的检查间隔（秒）。无需 ``msg_type``、不受 host / slave
            启动先后顺序影响，行为为：

            - 发布者尚未就绪时，按该间隔**循环重试解析类型，不设上限直到订上**；
            - **一旦订上即停止重试**，之后只管订阅、不再判活 / 轮询（断线重连交给 DDS
              自动完成，真出问题等报错暴露）。

            不设置时使用默认间隔 ``_SUBSCRIBE_RETRY_PERIOD``（10s），同样一直重试直到订上。
            注意：自动识别类型时，回调首参不要加 Python 内置类型注解（如 ``value: int``），
            否则会被当成 ``msg_type``。
    """
    if topic is None and status_name is None:
        raise ValueError("@subscribe 需要提供 topic 或 status_name 之一")

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper._subscribe_topic = topic  # type: ignore[attr-defined]
        wrapper._subscribe_msg_type = msg_type  # type: ignore[attr-defined]
        wrapper._subscribe_qos = qos  # type: ignore[attr-defined]
        wrapper._subscribe_device_id = device_id  # type: ignore[attr-defined]
        wrapper._subscribe_status_name = status_name  # type: ignore[attr-defined]
        wrapper._subscribe_trigger_when_change = trigger_when_change  # type: ignore[attr-defined]
        wrapper._subscribe_retry_interval = retry_interval  # type: ignore[attr-defined]
        wrapper._has_subscribe = True  # type: ignore[attr-defined]

        return wrapper  # type: ignore[return-value]

    return decorator


def get_subscribe_config(func) -> dict:
    """获取函数上的订阅配置 (topic, msg_type, qos, device_id, status_name, trigger_when_change, retry_interval)"""
    if hasattr(func, "_has_subscribe") and getattr(func, "_has_subscribe", False):
        return {
            "topic": getattr(func, "_subscribe_topic", None),
            "msg_type": getattr(func, "_subscribe_msg_type", None),
            "qos": getattr(func, "_subscribe_qos", 10),
            "device_id": getattr(func, "_subscribe_device_id", None),
            "status_name": getattr(func, "_subscribe_status_name", None),
            "trigger_when_change": getattr(func, "_subscribe_trigger_when_change", False),
            "retry_interval": getattr(func, "_subscribe_retry_interval", None),
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
