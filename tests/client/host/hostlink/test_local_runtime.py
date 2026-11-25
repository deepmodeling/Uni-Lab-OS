from __future__ import annotations

import asyncio
from pathlib import Path
import time
import threading

import pytest
import yaml

from unilabos.client.host.hostlink.local_runtime import (
    HostLinkDeviceNode,
    HostLinkDriverSpec,
    HostLinkLocalRuntime,
    instantiate_driver,
)
from unilabos.client.host.runtime import BackendCapabilityError
from unilabos.client.host.runtime.action import ActionContext
from unilabos.registry.decorators import topic_config
from unilabos.registry.placeholder_type import DeviceSlot, ResourceSlot
from unilabos.resources.resource_tracker import DeviceNodeResourceTracker


class ConfigDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.config = config


class FlatDriver:
    def __init__(self, port, device_id=None):
        self.port = port
        self.device_id = device_id


class LiquidHandlerAbstract:
    def __init__(self, backend):
        self.backend = backend
        self.setup_called = False

    def post_init(self, node) -> None:
        self.node = node

    async def setup(self) -> None:
        await self.node.sleep(0)
        self.setup_called = True


class DeviceConfig:
    children = []


class AsyncDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.config = config
        self.node = None
        self.initialized = False
        self.cleaned = False
        self.ready = "idle"

    def post_init(self, node) -> None:
        self.node = node

    async def initialize(self) -> bool:
        await self.node.sleep(0)
        self.initialized = True
        return True

    async def add(self, left: int, right: int) -> int:
        return left + right

    async def call_peer(
        self,
        target_device: str,
        left: int,
        right: int,
    ) -> int:
        return self.node.call_device_action(
            target_device,
            "add",
            {"left": left, "right": right},
        )

    async def call_peer_async(
        self,
        target_device: str,
        left: int,
        right: int,
    ) -> int:
        return await self.node.call_device_action_async(
            target_device,
            "add",
            {"left": left, "right": right},
        )

    async def cleanup(self) -> bool:
        self.cleaned = True
        return True


class RosGuardConditionDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.node = None

    def post_init(self, node) -> None:
        self.node = node

    def create_guard(self) -> None:
        self.node.create_guard_condition(lambda: None)


class AddService:
    class Request:
        def __init__(self, left=0, right=0):
            self.left = left
            self.right = right

    class Response:
        def __init__(self):
            self.total = 0


class ServiceDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.provider = bool((config or {}).get("provider"))

    def post_init(self, node) -> None:
        self.node = node
        if self.provider:
            node.create_service(AddService, "add", self.add)

    @staticmethod
    def add(request, response):
        response.total = request.left + request.right
        return response

    async def call_add(self, left, right):
        client = self.node.create_client(AddService, "/devices/provider/add")
        response = await client.call_async(AddService.Request(left, right))
        return response.total


class DecoratedRuntimeDriver:
    #: concurrent_action 每次执行的 (开始, 结束) 单调时钟区间
    concurrent_spans: list[tuple[float, float]] = []

    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.node = None
        self.progress = 0
        self.status_reads = 0

    def post_init(self, node) -> None:
        self.node = node

    @property
    @topic_config(period=0.02, name="renamed_status")
    def status(self) -> int:
        self.status_reads += 1
        return self.status_reads

    def mapped_action(self, driver_value: int) -> dict[str, int]:
        return {"value": driver_value * 2}

    def nested_action(self, driver_values: list[int]) -> list[int]:
        return [value * 2 for value in driver_values]

    def feedback_action(self, duration: float) -> int:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.progress += 1
            time.sleep(0.005)
        return self.progress

    def resource_action(self, resource: ResourceSlot) -> str:
        return resource.name

    async def concurrent_action(self, duration: float) -> float:
        started = time.monotonic()
        await self.node.sleep(duration)
        type(self).concurrent_spans.append((started, time.monotonic()))
        return duration


def test_driver_instantiation_supports_config_and_flat_styles() -> None:
    config_driver = instantiate_driver(ConfigDriver, "config-1", {"port": "A"})
    assert config_driver.device_id == "config-1"
    assert config_driver.config == {"port": "A"}

    flat_driver = instantiate_driver(FlatDriver, "flat-1", {"port": "B"})
    assert flat_driver.device_id == "flat-1"
    assert flat_driver.port == "B"


class ConfigAndExtrasDriver:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.device_id = device_id
        self.config = config
        self.extras = kwargs


def test_nested_config_key_reaches_driver_config_like_ros2() -> None:
    """节点配置按构造签名写成 ``{"config": {...}}`` 时，驱动的 ``config`` 只收这一层。

    ROS 2 把节点配置平铺成构造参数（``config=`` 就是内层字典）；HostLink 过去把整份节点
    配置再包一层传进去，驱动读不到参数、静默退回默认值。
    """

    driver = instantiate_driver(
        ConfigAndExtrasDriver,
        "bench",
        {"config": {"arm_operation_time": 3.0}, "port": "COM3"},
    )

    assert driver.config == {"arm_operation_time": 3.0}
    assert driver.extras == {"port": "COM3"}
    assert driver.device_id == "bench"
    # 平铺写法不变：config 仍是整份节点配置
    flat = instantiate_driver(ConfigAndExtrasDriver, "bench", {"port": "COM3"})
    assert flat.config == {"port": "COM3"} and flat.extras == {}


class SampleDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id

    def tag(self, sample_uuids, label: str) -> dict:
        return {"label": label, "samples": dict(sample_uuids)}

    def peer_tag(self, target_device: str) -> dict:
        return self._node.call_device_action(target_device, "tag", {"label": "peer"})

    def post_init(self, node) -> None:
        self._node = node


def test_missing_sample_uuids_is_injected_as_empty_mapping_like_ros2() -> None:
    """作业没带样品映射（单动作、跨设备调用）时，必填的 ``sample_uuids`` 注入空映射。

    ROS 2 一律注入 ``sample_uuids or {}``；HostLink 过去只在作业带样品时注入，
    把它声明为必填参数的驱动（虚拟工作台的四个动作）直接因缺参失败。
    """

    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("caller", SampleDriver, {}))
    runtime.add_driver(HostLinkDriverSpec("target", SampleDriver, {}))
    runtime.start()
    try:
        assert runtime.call_action("target", "tag", label="direct") == {"label": "direct", "samples": {}}
        assert runtime.call_action("caller", "peer_tag", target_device="target") == {
            "label": "peer",
            "samples": {},
        }
        # 作业带了样品映射时照常透传
        assert runtime.call_action("target", "tag", label="x", sample_uuids={"s1": "m1"})["samples"] == {
            "s1": "m1"
        }
    finally:
        runtime.stop()


def test_virtual_device_reads_flat_and_nested_graph_config() -> None:
    """内置虚拟设备按类型拆开构造参数：平铺的节点配置按名传入，旧注册表形状
    ``{"config": {...}}``（编辑器按它生成）的内层同样生效。"""

    from unilabos.devices.virtual.virtual_heatchill import VirtualHeatChill

    flat = instantiate_driver(VirtualHeatChill, "heatchill_1", {"max_temp": 150.0, "port": "COM3"})
    nested = instantiate_driver(
        VirtualHeatChill, "heatchill_2", {"config": {"max_temp": 120.0, "min_temp": 0.0, "time_scale": 0.5}}
    )

    assert (flat.device_id, flat.max_temp, flat.port) == ("heatchill_1", 150.0, "COM3")
    assert (nested.device_id, nested.max_temp, nested.min_temp, nested.time_scale) == (
        "heatchill_2",
        120.0,
        0.0,
        0.5,
    )


def test_liquid_handlers_declare_public_backends_and_action_metadata() -> None:
    yaml_registry = yaml.safe_load(
        Path("unilabos/registry/devices/liquid_handler.yaml").read_text(
            encoding="utf-8"
        )
    )
    entries = {"liquid_handler": yaml_registry["liquid_handler"]}

    for name, entry in entries.items():
        class_config = entry["class"]
        assert class_config["supported_backends"] == ["hostlink", "ros2"], name
        first_action = next(iter(class_config["action_value_mappings"].values()))
        assert first_action["type"], name
        assert first_action["schema"]["type"] == "object", name


def test_hostlink_runtime_constructs_and_sets_up_pylabrobot_style_driver() -> None:
    tracker = DeviceNodeResourceTracker()
    driver = instantiate_driver(
        LiquidHandlerAbstract,
        "liquid-handler",
        {"backend": "simulator"},
        device_config=DeviceConfig(),
        resource_tracker=tracker,
    )
    node = HostLinkDeviceNode(
        driver,
        "liquid-handler",
        resource_tracker=tracker,
    )
    node.start()
    try:
        assert driver.backend == "simulator"
        assert driver.node is node
        assert driver.setup_called is True
        assert node.resource_tracker is tracker
    finally:
        node.stop()


def test_hostlink_readiness_waits_for_post_init() -> None:
    entered, release = threading.Event(), threading.Event()

    class InitializingDriver:
        def post_init(self, node):
            entered.set()
            assert release.wait(10), "测试没有释放初始化屏障"

    from concurrent.futures import ThreadPoolExecutor

    node = HostLinkDeviceNode(driver=InitializingDriver(), device_id="initializing")
    assert node.describe()["ready"] is False
    with ThreadPoolExecutor(max_workers=1) as executor:
        started = executor.submit(node.start)
        try:
            assert entered.wait(5)
            assert node.describe()["ready"] is False
        finally:
            release.set()
        try:
            started.result(timeout=10)
            assert node.describe()["ready"] is True
        finally:
            node.stop()
    assert node.describe()["ready"] is False


def test_hostlink_device_lifecycle_and_direct_action() -> None:
    driver = AsyncDriver("dev-1", {})
    node = HostLinkDeviceNode(driver, "dev-1")
    node.start()
    try:
        assert driver.node is node
        assert driver.initialized is True
        assert node.call_action("add", left=2, right=3) == 5
    finally:
        node.stop()
    assert driver.cleaned is True


def test_hostlink_runtime_owns_and_routes_devices() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("dev-1", AsyncDriver, {"answer": 42}))
    runtime.start()
    try:
        assert runtime.call_action("dev-1", "add", left=10, right=5) == 15
    finally:
        runtime.stop()
    assert runtime.wait(timeout=0) is True


def test_hostlink_runtime_routes_cross_device_actions() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("caller", AsyncDriver, {}))
    runtime.add_driver(HostLinkDriverSpec("target", AsyncDriver, {}))
    runtime.start()
    try:
        assert (
            runtime.call_action(
                "caller",
                "call_peer",
                target_device="target",
                left=4,
                right=6,
            )
            == 10
        )
    finally:
        runtime.stop()


def test_hostlink_runtime_awaits_cross_device_actions_natively() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("caller", AsyncDriver, {}))
    runtime.add_driver(HostLinkDriverSpec("target", AsyncDriver, {}))
    runtime.start()
    try:
        result = asyncio.run(
            runtime.call_action_async(
                "caller",
                "call_peer_async",
                target_device="target",
                left=7,
                right=8,
            )
        )
        assert result == 15
    finally:
        runtime.stop()


def test_hostlink_runtime_exposes_registered_actions_and_status() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "dev-1",
            AsyncDriver,
            {},
            registry_name="async_driver",
            display_name="Async Driver",
            action_names=("auto-add",),
            status_names=("ready",),
        )
    )
    runtime.start()
    try:
        assert runtime.descriptors() == [
            {
                "id": "dev-1",
                "ready": True,
                "registry_name": "async_driver",
                "display_name": "Async Driver",
                "actions": ["auto-add"],
                "status_fields": ["ready"],
            }
        ]
        assert runtime.snapshot_states() == {"dev-1": {"ready": "idle"}}
        assert runtime.call_action("dev-1", "auto-add", left=1, right=2) == 3
    finally:
        runtime.stop()


def test_hostlink_runtime_supports_ros_shaped_timer_clock_and_rate() -> None:
    driver = AsyncDriver("dev-1", {})
    node = HostLinkDeviceNode(driver, "dev-1")
    fired = []
    node.start()
    try:
        assert node.get_clock().now().nanoseconds > 0

        timer = node.create_timer(0.01, lambda: fired.append(True))
        for _ in range(100):
            if fired:
                break
            node.create_rate(1000).sleep()
        assert fired
        assert node.destroy_timer(timer) is True
    finally:
        node.stop()


def test_hostlink_runtime_routes_ros_shaped_services_between_devices() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("provider", ServiceDriver, {"provider": True}))
    runtime.add_driver(
        HostLinkDriverSpec(
            "caller",
            ServiceDriver,
            {},
            action_names=("call_add",),
        )
    )
    runtime.start()
    try:
        assert runtime.call_action("caller", "call_add", left=7, right=8) == 15
        assert runtime.descriptors()[0]["services"] == ["/devices/provider/add"]
    finally:
        runtime.stop()


def test_hostlink_runtime_exposes_action_metadata() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "dev-1",
            AsyncDriver,
            {},
            action_names=("add",),
            action_value_mappings={
                "add": {
                    "type": AddService,
                    "goal": {"left": "left", "right": "right"},
                    "result": {"total": "total"},
                    "schema": {"type": "object"},
                }
            },
        )
    )
    descriptor = runtime.descriptors()[0]
    assert descriptor["action_value_mappings"]["add"] == {
        "type": f"{AddService.__module__}.{AddService.__qualname__}",
        "goal": {"left": "left", "right": "right"},
        "result": {"total": "total"},
        "schema": {"type": "object"},
    }


def _decorated_runtime() -> HostLinkLocalRuntime:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "decorated",
            DecoratedRuntimeDriver,
            {},
            action_names=(
                "mapped_action",
                "nested_action",
                "feedback_action",
                "concurrent_action",
                "resource_action",
            ),
            action_value_mappings={
                "mapped_action": {
                    "type": "MappedAction",
                    "goal": {"wire_value": "driver_value"},
                    "result": {"value": "value"},
                },
                "nested_action": {
                    "type": "NestedAction",
                    "goal": {"items[].wire_value": "driver_values[]"},
                },
                "feedback_action": {
                    "type": "FeedbackAction",
                    "goal": {"duration": "duration"},
                    "feedback": {"progress": "progress"},
                    "feedback_interval": 0.01,
                },
                "concurrent_action": {
                    "type": "ConcurrentAction",
                    "always_free": True,
                },
                "resource_action": {
                    "type": "ResourceAction",
                    "goal": {"resource": "resource"},
                },
            },
            status_names=("status",),
        )
    )
    return runtime


def test_hostlink_runtime_applies_action_goal_mapping_and_feedback_config() -> None:
    runtime = _decorated_runtime()
    runtime.start()
    feedback: list[dict[str, int]] = []
    try:
        assert runtime.call_action(
            "decorated",
            "mapped_action",
            wire_value=7,
        ) == {"value": 14}
        assert runtime.call_action(
            "decorated",
            "nested_action",
            items=[{"wire_value": 2}, {"wire_value": 5}],
        ) == [4, 10]
        context = ActionContext(
            feedback_callback=lambda _action_id, data: feedback.append(data)
        )
        # 反馈按 0.01s 间隔发布；Windows 计时精度约 15.6ms，50ms 的动作在繁忙 CI 上
        # 可能一次反馈都来不及发出。给足时长，只验证映射与发布，不卡调度时序。
        assert runtime.call_action(
            "decorated",
            "feedback_action",
            action_context=context,
            duration=0.5,
        ) > 0
        assert feedback
        assert feedback[-1]["progress"] > 0
    finally:
        runtime.stop()


def test_hostlink_runtime_resolves_resource_slot_before_driver_call() -> None:
    from pylabrobot.resources import Resource

    runtime = _decorated_runtime()
    resource = Resource("plate", size_x=1, size_y=1, size_z=1)
    resource.unilabos_uuid = "resource-uuid"
    runtime.devices["decorated"].resource_tracker.add_resource(resource)
    runtime.start()
    try:
        assert runtime.call_action(
            "decorated",
            "resource_action",
            resource={"uuid": "resource-uuid"},
        ) == "plate"
    finally:
        runtime.stop()


class TransferDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id

    def pick_material(self, material: ResourceSlot, target_owner: ResourceSlot) -> dict:
        return {"material": material.name, "target_owner": target_owner}

    def pick_into_gripper(self, material: ResourceSlot, target_owner: DeviceSlot) -> dict:
        return {"material": material.name, "target_owner": target_owner}


class _AuthorityTrees:
    """物料权威的最小替身：按 uuid 返回整棵子树（with_children）。"""

    def __init__(self, tree_set) -> None:
        self.trees = {node.res_content.uuid: node for node in tree_set.all_nodes}

    def get_resources_sync(self, uuids, with_children=True):
        from unilabos.resources.resource_tracker import ResourceTreeInstance, ResourceTreeSet

        return ResourceTreeSet([ResourceTreeInstance(self.trees[uuid]) for uuid in uuids if uuid in self.trees])

    def get_resource_by_id_sync(self, resource_id, with_children=True):
        raise KeyError(resource_id)


ROBOT_UUID = "6f0d1f3e-0c7a-4a55-9b7e-1d2c3b4a5f60"
WORKDECK_DEVICE_UUID = "0b9e8d7c-6a5f-4e3d-8c2b-1a0f9e8d7c6b"


def _transfer_runtime():
    from unilabos.resources.resource_tracker import ResourceTreeSet
    from unilabos.server.services.materials.graph import graph_resource_tree
    from tests.plr_fixtures import plate

    def device(device_id: str, uuid: str) -> dict:
        return {"id": device_id, "uuid": uuid, "type": "device", "class": "test_device",
                "template_name": "test_device", "parent": None, "config": {}}

    deck_nodes = ResourceTreeSet.from_plr_resources([plate("deck_plate")], known_random_uuid=True).dump()[0]
    deck_nodes[0]["parent_uuid"] = WORKDECK_DEVICE_UUID
    graph = graph_resource_tree([device("robot", ROBOT_UUID), device("workdeck", WORKDECK_DEVICE_UUID), *deck_nodes])

    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec("robot", TransferDriver, {}, action_names=("pick_material", "pick_into_gripper"))
    )
    runtime.start()
    node = runtime.devices["robot"]
    node.__dict__["_device_resource_service"] = _AuthorityTrees(graph)
    from pylabrobot.resources import Resource

    held = Resource("held_plate", size_x=1, size_y=1, size_z=1)
    held.unilabos_uuid = "held-plate-uuid"
    node.resource_tracker.add_resource(held)
    return runtime, graph


@pytest.mark.parametrize("owner_uuid, owner_id", [(ROBOT_UUID, "robot"), (WORKDECK_DEVICE_UUID, "workdeck")])
def test_resource_slot_pointing_at_a_device_is_rejected_before_the_driver(owner_uuid, owner_id) -> None:
    """ResourceSlot 只指物料：传进来的是设备（机械臂本身；台面设备下挂着 PLR 物料也一样）时明确报错。

    以前权威取回设备树后 ``to_plr_resources()[0]`` 越界，报 "list index out of range"；现在指出参数与设备，
    并说明设备参数用 DeviceSlot。
    """

    runtime, _graph = _transfer_runtime()
    try:
        with pytest.raises(ValueError) as caught:
            runtime.call_action(
                "robot", "pick_material", material={"uuid": "held-plate-uuid"}, target_owner={"uuid": owner_uuid}
            )
    finally:
        runtime.stop()

    message = str(caught.value)
    assert "target_owner" in message and owner_id in message and owner_uuid in message
    assert "DeviceSlot" in message and "设备不是物料" in message


def test_resource_slot_tree_rooted_at_a_device_is_rejected() -> None:
    """上游端口传来的扁平树以设备为根时同样报错，不转 PLR。"""

    runtime, graph = _transfer_runtime()
    robot_tree = next(tree for tree in graph.dump() if tree[0]["uuid"] == ROBOT_UUID)
    try:
        with pytest.raises(ValueError, match="DeviceSlot"):
            runtime.call_action("robot", "pick_material", material={"uuid": "held-plate-uuid"}, target_owner=robot_tree)
    finally:
        runtime.stop()


def test_device_slot_reaches_the_driver_as_the_device_id() -> None:
    """DeviceSlot 原样交给驱动（设备 id 字符串），不做物料转换、不回权威查询。"""

    runtime, _graph = _transfer_runtime()
    node = runtime.devices["robot"]
    authority = node.__dict__["_device_resource_service"]
    queried: list = []
    authority.get_resources_sync = lambda uuids, with_children=True: queried.append(uuids)
    try:
        result = runtime.call_action(
            "robot", "pick_into_gripper", material={"uuid": "held-plate-uuid"}, target_owner="robot"
        )
    finally:
        runtime.stop()

    assert result == {"material": "held_plate", "target_owner": "robot"}
    assert type(result["target_owner"]) is str and queried == []


def test_hostlink_runtime_honors_topic_config_period_and_name() -> None:
    runtime = _decorated_runtime()
    received: list[int] = []
    runtime.topic_bus.subscribe(
        "/devices/decorated/renamed_status",
        received.append,
    )
    runtime.start()
    try:
        deadline = time.monotonic() + 0.5
        while len(received) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(received) >= 2
        assert runtime.descriptors()[0]["status_fields"] == ["renamed_status"]
        assert set(runtime.snapshot_states()["decorated"]) == {"renamed_status"}
    finally:
        runtime.stop()


def test_hostlink_runtime_does_not_queue_scheduler_dispatched_actions() -> None:
    runtime = _decorated_runtime()
    runtime.start()
    DecoratedRuntimeDriver.concurrent_spans.clear()
    try:
        async def run_both() -> list[float]:
            return await asyncio.gather(
                runtime.call_action_async(
                    "decorated", "concurrent_action", duration=0.2
                ),
                runtime.call_action_async(
                    "decorated", "concurrent_action", duration=0.2
                ),
            )

        assert asyncio.run(run_both()) == [0.2, 0.2]
        # 两次执行区间重叠，说明第二个没有排在第一个之后。不用墙钟上限：慢机器上调度开销会误报
        spans = DecoratedRuntimeDriver.concurrent_spans
        assert len(spans) == 2
        assert max(start for start, _ in spans) < min(end for _, end in spans)
    finally:
        runtime.stop()


def test_hostlink_runtime_reports_direct_ros_node_calls_clearly() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "ros-guard",
            RosGuardConditionDriver,
            {},
            action_names=("create_guard",),
        )
    )
    runtime.start()
    try:
        with pytest.raises(
            BackendCapabilityError,
            match="设备 'ros-guard'.*create_guard_condition.*DeviceNode",
        ):
            runtime.call_action("ros-guard", "create_guard")
    finally:
        runtime.stop()


def test_hostlink_turns_on_plr_volume_tracking_after_liquid_handler_setup() -> None:
    """FRAMEWORK_GAPS #23：HostLink 自己调用 setup，之后要与 ROS 2 一样打开 PLR 体积跟踪。"""

    from pylabrobot.resources import does_volume_tracking, set_volume_tracking

    set_volume_tracking(enabled=False)
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "liquid-handler",
            LiquidHandlerAbstract,
            {"backend": "simulator"},
            device_config=DeviceConfig(),
        )
    )
    try:
        assert does_volume_tracking() is False
        runtime.start()
        assert runtime.devices["liquid-handler"].driver.setup_called is True
        assert does_volume_tracking() is True
    finally:
        runtime.stop()
        set_volume_tracking(enabled=False)


def test_hostlink_leaves_volume_tracking_alone_for_plain_drivers() -> None:
    from pylabrobot.resources import does_volume_tracking, set_volume_tracking

    set_volume_tracking(enabled=False)
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("dev-1", AsyncDriver, {}))
    try:
        runtime.start()
        assert does_volume_tracking() is False
    finally:
        runtime.stop()


_status_getter_entered = threading.Event()
_status_getter_release = threading.Event()


class BlockingStatusDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.getter_threads: list[str] = []

    def post_init(self, node) -> None:
        self.node = node

    @property
    @topic_config(period=0.02)
    def status(self) -> str:
        self.getter_threads.append(threading.current_thread().name)
        _status_getter_entered.set()
        _status_getter_release.wait(10)  # 模拟一次慢的 TCP 查询
        return "idle"

    async def ping(self) -> str:
        return "pong"


def test_hostlink_blocking_status_getter_does_not_stall_device_actions() -> None:
    """FRAMEWORK_GAPS #21：阻塞的状态 getter 不能在设备事件循环上执行，拖住该设备的动作。"""

    import concurrent.futures

    _status_getter_entered.clear()
    _status_getter_release.clear()
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "slow-status",
            BlockingStatusDriver,
            {},
            action_names=("ping",),
            status_names=("status",),
        )
    )
    runtime.start()
    try:
        assert _status_getter_entered.wait(5), "状态定时发布没有调用 getter"
        node = runtime.devices["slow-status"]
        future = asyncio.run_coroutine_threadsafe(node.call_action_async("ping"), node._loop)
        try:
            assert future.result(timeout=2) == "pong"
        except concurrent.futures.TimeoutError:
            pytest.fail("状态 getter 阻塞了设备事件循环，动作无法执行")
        # 状态 getter 只在状态线程上求值：启动扫描 @subscribe 时不求值 property，也不上事件循环
        assert set(node.driver.getter_threads) == {"hostlink-status-slow-status"}
    finally:
        _status_getter_release.set()
        runtime.stop()
