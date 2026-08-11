import asyncio
import inspect

from unilabos.device_runtime import DeviceNode
from unilabos.ros.nodes import base_device_node
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


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
