import asyncio
import inspect
import json
from typing import Any, Optional
from unittest.mock import Mock

from rclpy.task import Task

from unilabos.client.host.runtime import DeviceNode
from unilabos.client.host.ros2 import base_device_node
from unilabos.client.host.ros2.base_device_node import BaseROS2DeviceNode
from unilabos.client.host.ros2.msgs.message_converter import Resource, convert_to_ros_msg
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


class _StirDriver:
    def __init__(self) -> None:
        self.vessels: list = []

    async def stir(
        self,
        stir_time: float,
        stir_speed: float,
        settling_time: str = "",
        vessel: Optional[Any] = None,
        time: str = "",
        time_spec: str = "",
        event: str = "",
    ) -> dict:
        self.vessels.append(vessel)
        return {"success": True, "vessel": "" if vessel is None else str(vessel)}


class _GoalHandle:
    def __init__(self, request: Any) -> None:
        self.request = request

    def succeed(self) -> None:
        pass

    def publish_feedback(self, _message: Any) -> None:
        pass


class _ResourceGoalNode:
    """原生 action 执行回调所需的最小节点（不起 rclpy）：物料查询只记录、不命中。"""

    get_real_function = BaseROS2DeviceNode.get_real_function
    _resolve_report_action_name = BaseROS2DeviceNode._resolve_report_action_name

    def __init__(self) -> None:
        self.driver_instance = _StirDriver()
        self.queries: list = []
        self._print_publish = False
        self.lab_logger = Mock()

    def run_async_func(self, func, trace_error=True, **kwargs):
        task = Task(func, kwargs=kwargs)
        task()  # 驱动协程不挂起，一次就跑完
        return task

    async def get_resource_with_dir(self, resource_id: str, with_children: bool = True):
        self.queries.append(resource_id)
        raise LookupError(f"未找到物料 {resource_id}")

    async def get_resource(self, uuids: list):
        self.queries.extend(uuids)
        raise LookupError(f"未找到物料 {uuids}")


def _run_stir(goal: Any) -> tuple[_ResourceGoalNode, Any]:
    from unilabos_msgs.action import Stir

    node = _ResourceGoalNode()
    mapping = {
        "type": Stir,
        "goal": {name: name for name in Stir.Goal.get_fields_and_field_types()},
        "feedback": {},
        "result": {},
    }
    execute = BaseROS2DeviceNode._create_execute_callback(node, "stir", mapping)
    return node, asyncio.run(execute(_GoalHandle(goal)))


def test_native_action_without_optional_vessel_runs_with_none() -> None:
    """可选容器留空（Host 发空 Resource）：与 HostLink 一致不查物料，驱动拿到 None（修复前 KeyError 'id'）。"""
    from unilabos_msgs.action import Stir

    for arguments in ({"stir_time": 1.0, "stir_speed": 300.0}, {"stir_time": 1.0, "stir_speed": 300.0, "vessel": None}):
        node, result = _run_stir(convert_to_ros_msg(Stir.Goal(), arguments))

        assert result.success is True, json.loads(result.return_info)
        assert node.driver_instance.vessels == [None]
        assert node.queries == []


def test_native_action_with_vessel_still_resolves_it() -> None:
    """指定了容器：引用（含 uuid）随 goal 到达，照常向物料权威查询，查不到时动作失败而不是当成未指定。"""
    from unilabos_msgs.action import Stir

    vessel_uuid = "6c8e2f7a-9a51-4c43-8f0e-2d4b1a7f5e10"
    goal = convert_to_ros_msg(
        Stir.Goal(), {"stir_time": 1.0, "stir_speed": 300.0, "vessel": {"id": "beaker", "uuid": vessel_uuid}}
    )
    node, result = _run_stir(goal)

    assert result.success is False
    assert node.queries == [vessel_uuid]
    assert node.driver_instance.vessels == []

    # 旧发送端只填 id、不填 data：不算未指定，照常失败可见
    node, result = _run_stir(Stir.Goal(stir_time=1.0, stir_speed=300.0, vessel=Resource(id="beaker")))
    assert result.success is False
    assert node.driver_instance.vessels == []


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


def test_ros2_resource_slot_pointing_at_a_device_is_rejected() -> None:
    """与 HostLink 同一规则：ResourceSlot 只指物料，指向设备时明确报错（设备参数用 DeviceSlot）。"""

    import logging

    import pytest

    from unilabos.client.host.runtime.resource_slot import DeviceResourceSlotError
    from unilabos.resources.resource_tracker import (
        DeviceNodeResourceTracker,
        ResourceTreeInstance,
        ResourceTreeSet,
    )
    from unilabos.server.services.materials.graph import graph_resource_tree
    from tests.plr_fixtures import plate

    robot_uuid = "6f0d1f3e-0c7a-4a55-9b7e-1d2c3b4a5f64"
    plate_nodes = ResourceTreeSet.from_plr_resources([plate("loose_plate")], known_random_uuid=True).dump()[0]
    graph = graph_resource_tree([
        {"id": "robot", "uuid": robot_uuid, "type": "device", "class": "test_device",
         "template_name": "test_device", "parent": None, "config": {}},
        *plate_nodes,
    ])
    nodes = {node.res_content.uuid: node for node in graph.all_nodes}

    class Service:
        def get_resources_sync(self, uuids, with_children=True):
            return ResourceTreeSet([ResourceTreeInstance(nodes[uuid]) for uuid in uuids])

    class Node:
        _convert_resources_sync = BaseROS2DeviceNode._convert_resources_sync
        _assemble_single_resource = BaseROS2DeviceNode._assemble_single_resource
        _convert_resource_async = BaseROS2DeviceNode._convert_resource_async
        _resolve_slot_value_sync = BaseROS2DeviceNode._resolve_slot_value_sync
        resource_tracker = DeviceNodeResourceTracker()

        def _require_resource_service(self):
            return Service()

        async def get_resource(self, uuids, with_children=True):
            return Service().get_resources_sync(uuids, with_children)

        def lab_logger(self):
            return logging.getLogger(__name__)

    node = Node()
    robot_tree = next(tree for tree in graph.dump() if tree[0]["uuid"] == robot_uuid)
    for resolve in (
        lambda: node._resolve_slot_value_sync({"uuid": robot_uuid}),
        lambda: asyncio.run(node._convert_resource_async({"uuid": robot_uuid})),
        lambda: node._assemble_single_resource(ResourceTreeSet.load([robot_tree])),
        lambda: node._convert_resources_sync(robot_uuid, plate_nodes[0]["uuid"]),
    ):
        with pytest.raises(DeviceResourceSlotError, match="DeviceSlot") as caught:
            resolve()
        assert "robot" in str(caught.value) and robot_uuid in str(caught.value)
    # 物料照常装配
    (loose,) = node._convert_resources_sync(plate_nodes[0]["uuid"])
    assert loose.name == "loose_plate"
