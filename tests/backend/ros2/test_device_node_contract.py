import asyncio
import inspect

from unilabos.backend.runtime import DeviceNode
from unilabos.backend.ros2 import base_device_node
from unilabos.backend.ros2.base_device_node import BaseROS2DeviceNode
from unilabos.utils.decorator import subscribe


def test_ros2_node_implements_backend_neutral_contract() -> None:
    assert issubclass(BaseROS2DeviceNode, DeviceNode)
    assert BaseROS2DeviceNode.backend_name == "ros2"
    assert not inspect.iscoroutinefunction(BaseROS2DeviceNode.create_task)


def test_ros2_create_task_accepts_common_coroutine_contract(monkeypatch) -> None:
    captured = {}

    class Executor:
        def create_task(self, coroutine):
            captured["coroutine"] = coroutine
            return "scheduled"

    monkeypatch.setattr(
        base_device_node.rclpy,
        "get_global_executor",
        lambda: Executor(),
    )

    async def operation() -> int:
        return 42

    result = BaseROS2DeviceNode.create_task(object(), operation())

    assert result == "scheduled"
    assert asyncio.run(captured["coroutine"]) == 42


def test_ros2_callable_create_task_uses_generic_run_async_func() -> None:
    captured = {}

    class NodeAdapter:
        def run_async_func(self, func, trace_error=True, **kwargs):
            captured.update(func=func, trace_error=trace_error, kwargs=kwargs)
            return "scheduled"

    async def operation() -> int:
        return 42

    result = BaseROS2DeviceNode.create_task(NodeAdapter(), operation)

    assert result == "scheduled"
    assert captured["trace_error"] is True
    assert captured["kwargs"] == {}
    assert asyncio.run(captured["func"]()) == 42


def test_ros2_node_registers_both_subscribe_decorator_forms() -> None:
    class MessageType:
        pass

    class Driver:
        @subscribe(
            "/devices/source/absolute",
            msg_type=MessageType,
            qos=4,
        )
        def absolute(self, value):
            return value

        @subscribe(
            device_id="source",
            status_name="split",
            msg_type=MessageType,
            qos=7,
            trigger_when_change=True,
        )
        def split(self, value):
            return value

    class Logger:
        def trace(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

    class NodeAdapter:
        _setup_one_subscriber = BaseROS2DeviceNode._setup_one_subscriber
        _ensure_subscription = BaseROS2DeviceNode._ensure_subscription
        _resolve_subscription_target = (
            BaseROS2DeviceNode._resolve_subscription_target
        )
        _resolve_subscription_msg_type = (
            BaseROS2DeviceNode._resolve_subscription_msg_type
        )
        _namespace_prefix = BaseROS2DeviceNode._namespace_prefix

        def __init__(self):
            self.driver_instance = Driver()
            self.namespace = "/devices/local"
            self._topic_subscriber_types = {}
            self._subscriber_monitors = {}
            self.created = []

        def lab_logger(self):
            return Logger()

        def create_ros_subscriber(
            self,
            topic,
            msg_type,
            callback,
            qos,
            trigger_when_change=False,
        ):
            self.created.append(
                {
                    "topic": topic,
                    "msg_type": msg_type,
                    "callback": callback,
                    "qos": qos,
                    "trigger_when_change": trigger_when_change,
                }
            )
            return object()

        def create_timer(self, *_args, **_kwargs):
            raise AssertionError("显式 msg_type 不应进入延迟重试")

    node = NodeAdapter()
    BaseROS2DeviceNode._setup_decorated_subscribers(node)

    assert [item["topic"] for item in node.created] == [
        "/devices/source/absolute",
        "/devices/source/split",
    ]
    assert [item["qos"] for item in node.created] == [4, 7]
    assert node.created[1]["trigger_when_change"] is True
    assert set(node._topic_subscriber_types) == {
        "/devices/source/absolute",
        "/devices/source/split",
    }
