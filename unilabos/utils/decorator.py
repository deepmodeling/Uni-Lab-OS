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


def topic_config(
    period: Optional[float] = None,
    print_publish: Optional[bool] = None,
    qos: Optional[int] = None,
) -> Callable[[F], F]:
    """
    Topic发布配置装饰器

    用于装饰 get_{attr_name} 方法或 @property，控制对应属性的ROS topic发布行为。

    Args:
        period: 发布周期（秒）。None 表示使用默认值 5.0
        print_publish: 是否打印发布日志。None 表示使用节点默认配置
        qos: QoS深度配置。None 表示使用默认值 10

    Example:
        class MyDriver:
            # 方式1: 装饰 get_{attr_name} 方法
            @topic_config(period=1.0, print_publish=False, qos=5)
            def get_temperature(self):
                return self._temperature

            # 方式2: 与 @property 连用（topic_config 放在下面）
            @property
            @topic_config(period=0.1)
            def position(self):
                return self._position

    Note:
        与 @property 连用时，@topic_config 必须放在 @property 下面，
        这样装饰器执行顺序为：先 topic_config 添加配置，再 property 包装。
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        # 在函数上附加配置属性 (type: ignore 用于动态属性)
        wrapper._topic_period = period  # type: ignore[attr-defined]
        wrapper._topic_print_publish = print_publish  # type: ignore[attr-defined]
        wrapper._topic_qos = qos  # type: ignore[attr-defined]
        wrapper._has_topic_config = True  # type: ignore[attr-defined]

        return wrapper  # type: ignore[return-value]

    return decorator


def get_topic_config(func) -> dict:
    """
    获取函数上的topic配置

    Args:
        func: 被装饰的函数

    Returns:
        包含 period, print_publish, qos 的配置字典
    """
    if hasattr(func, "_has_topic_config") and getattr(func, "_has_topic_config", False):
        return {
            "period": getattr(func, "_topic_period", None),
            "print_publish": getattr(func, "_topic_print_publish", None),
            "qos": getattr(func, "_topic_qos", None),
        }
    return {}


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

    Example:
        from std_msgs.msg import String, Float64

        class MyDriver:
            @subscribe(topic="/devices/{device_id}/set_speed", msg_type=Float64)
            def on_speed_update(self, msg: Float64):
                self._speed = msg.data
                print(f"Speed updated to: {self._speed}")

            @subscribe(topic="{namespace}/command")
            def on_command(self, msg: String):
                # msg_type 可从类型注解推断
                self.execute_command(msg.data)

    Note:
        - 回调方法的第一个参数是 self，第二个参数是收到的 ROS 消息
        - topic 中的占位符会在创建订阅时被实际值替换
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        # 在函数上附加订阅配置
        wrapper._subscribe_topic = topic  # type: ignore[attr-defined]
        wrapper._subscribe_msg_type = msg_type  # type: ignore[attr-defined]
        wrapper._subscribe_qos = qos  # type: ignore[attr-defined]
        wrapper._has_subscribe = True  # type: ignore[attr-defined]

        return wrapper  # type: ignore[return-value]

    return decorator


def get_subscribe_config(func) -> dict:
    """
    获取函数上的订阅配置

    Args:
        func: 被装饰的函数

    Returns:
        包含 topic, msg_type, qos 的配置字典
    """
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

    Args:
        instance: 要扫描的实例

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


def not_action(func: F) -> F:
    """
    标记方法为非动作的装饰器

    用于装饰 driver 类中的方法，使其在 complete_registry 时不被识别为动作。
    适用于辅助方法、内部工具方法等不应暴露为设备动作的公共方法。

    Example:
        class MyDriver:
            @not_action
            def helper_method(self):
                # 这个方法不会被注册为动作
                pass

            def actual_action(self, param: str):
                # 这个方法会被注册为动作
                self.helper_method()

    Note:
        - 可以与其他装饰器组合使用，@not_action 应放在最外层
        - 仅影响 complete_registry 的动作识别，不影响方法的正常调用
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    # 在函数上附加标记
    wrapper._is_not_action = True  # type: ignore[attr-defined]

    return wrapper  # type: ignore[return-value]


def is_not_action(func) -> bool:
    """
    检查函数是否被标记为非动作

    Args:
        func: 被检查的函数

    Returns:
        如果函数被 @not_action 装饰则返回 True，否则返回 False
    """
    return getattr(func, "_is_not_action", False)
