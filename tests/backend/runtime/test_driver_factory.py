from __future__ import annotations

from types import SimpleNamespace

from unilabos.backend.hostlink.main_hostlink_run import build_runtime
from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.backend.runtime.definition import (
    is_host_node_config,
    iter_device_config_entries,
)
from unilabos.backend.runtime.driver_creator import select_driver_creator
from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceDictInstance,
)


class _Workstation(WorkstationBase):
    def __init__(self, deck=None, children=None, protocol_type=None, **_kwargs):
        super().__init__(deck)
        self.constructed_children = children
        self.protocol_type = protocol_type


class _PlainDriver:
    def __init__(self, device_id: str, **_kwargs):
        self.device_id = device_id


class _CommunicationDriver:
    def __init__(self, device_id: str, **_kwargs):
        self.device_id = device_id
        self.calls = []

    def read_data(self, **kwargs):
        self.calls.append(("read", kwargs))
        return "read-result"

    def send_command(self, value, **kwargs):
        self.calls.append(("write", value, kwargs))
        return "write-result"


class _ProxyDriver:
    def __init__(self, device_id: str, **_kwargs):
        self.device_id = device_id
        self.hardware_interface = "serial_bus"
        self.slave_id = 7

    def read_register(self):
        raise AssertionError("hardware proxy was not configured")

    def write_register(self, _value):
        raise AssertionError("hardware proxy was not configured")


def _node(node_id: str, *children, resource_type: str = "device"):
    return SimpleNamespace(
        res_content=SimpleNamespace(type=resource_type, id=node_id),
        children=list(children),
    )


def test_common_factory_selects_workstation_for_every_backend() -> None:
    children = [_node("child")]
    selection = select_driver_creator(
        _Workstation,
        children,
        DeviceNodeResourceTracker(),
    )

    instance = selection.creator.create_instance({"protocol_type": ["demo"]})

    assert selection.is_workstation is True
    assert instance.constructed_children == children
    assert instance.protocol_type == ["demo"]


def test_device_tree_iterator_initializes_subdevices_with_graph_identity() -> None:
    child = _node("pump")
    material = _node("material", child, resource_type="container")
    workstation = _node("station", material)
    standalone = _node("balance")
    tree_set = SimpleNamespace(root_nodes=[workstation, standalone])

    assert [
        (entry.device_id, entry.parent_device_id)
        for entry in iter_device_config_entries(tree_set)
    ] == [
        ("station", ""),
        ("pump", "station"),
        ("balance", ""),
    ]


def test_hostlink_does_not_instantiate_ros_host_node() -> None:
    host_node = _node("host_node")
    host_node.res_content.template_name = "host_node"
    host_node.res_content.uuid = "host-uuid"

    runtime = build_runtime(SimpleNamespace(root_nodes=[host_node]))

    assert runtime.devices == {}
    # 图中声明的 host 身份被记录，供 backend.start() 注册服务设备时复用。
    assert runtime.graph_host_resource_uuids == {"host_node": "host-uuid"}


def test_host_node_is_identified_by_template_name_only() -> None:
    """host 判别只看 template_name；class 是旧字段，仅在图读取边界回填。"""
    declared = SimpleNamespace(type="device", id="host_a", template_name="host_node", klass="")
    assert is_host_node_config(declared) is True

    legacy_class_only = SimpleNamespace(
        type="device", id="host_b", template_name="device", klass="host_node"
    )
    assert is_host_node_config(legacy_class_only) is False
    # 未声明模板的旧图节点：template_name 回退为 type，按配置实例 id 兼容判别。
    assert is_host_node_config(legacy_class_only, "host_b") is True
    assert is_host_node_config(legacy_class_only, "other") is False

    other_device = SimpleNamespace(type="device", id="pump", template_name="pump_demo", klass="")
    assert is_host_node_config(other_device, "pump") is False


def test_hostlink_runtime_uses_same_factory_for_dynamic_subdevice(monkeypatch) -> None:
    from unilabos.backend.runtime import definition as definition_module

    dynamic_config = ResourceDictInstance.get_resource_instance_from_dict(
        {
            "id": "dynamic",
            "uuid": "dynamic-uuid",
            "name": "dynamic",
            "type": "device",
            "class": "dynamic-driver",
        }
    )

    def resolve(device_id, device_config, *, backend_name=None):
        assert backend_name == "hostlink"
        return SimpleNamespace(
            driver_class=_PlainDriver,
            runtime_config={},
            registry_name="dynamic-driver",
            display_name="Dynamic Driver",
            action_value_mappings={},
            status_types={},
            resource_uuid=device_config.res_content.uuid,
        )

    monkeypatch.setattr(definition_module, "resolve_device_definition", resolve)
    runtime = HostLinkLocalRuntime()
    owner = runtime.add_driver(HostLinkDriverSpec("owner", _PlainDriver, {}))
    runtime.start()
    try:
        created = owner.create_device("dynamic", dynamic_config)
        assert created == {
            "success": True,
            "device_id": "dynamic",
            "registry_name": "dynamic-driver",
        }
        assert runtime.devices["dynamic"].driver.device_id == "dynamic"
        assert runtime.devices["dynamic"]._started is True
        assert owner.destroy_device("dynamic") == {
            "success": True,
            "device_id": "dynamic",
        }
        assert "dynamic" not in runtime.devices
    finally:
        runtime.stop()


def test_hostlink_workstation_initializes_child_nodes_with_ros_shape() -> None:
    runtime = HostLinkLocalRuntime()
    workstation = runtime.add_driver(
        HostLinkDriverSpec(
            "station",
            _Workstation,
            {"protocol_type": []},
        )
    )
    child = runtime.add_driver(
        HostLinkDriverSpec(
            "pump",
            _PlainDriver,
            {},
            parent_device_id="station",
        )
    )

    assert workstation.sub_devices == {"pump": child}
    assert child.parent_device_id == "station"
    assert child.driver_instance is child.driver
    assert child.ros_node_instance is child
    assert child.resource_tracker is workstation.resource_tracker

    runtime.start()
    try:
        assert workstation.driver._ros_node is workstation
        assert child._started is True
    finally:
        runtime.stop()


def test_hostlink_workstation_configures_shared_hardware_proxy() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec("station", _Workstation, {"protocol_type": []})
    )
    proxy = runtime.add_driver(
        HostLinkDriverSpec(
            "pump",
            _ProxyDriver,
            {},
            parent_device_id="station",
            hardware_interface={
                "name": "hardware_interface",
                "read": "read_register",
                "write": "write_register",
                "extra_info": ["slave_id"],
            },
        )
    )
    communication = runtime.add_driver(
        HostLinkDriverSpec(
            "serial_bus",
            _CommunicationDriver,
            {},
            parent_device_id="station",
            hardware_interface={
                "name": "hardware_interface",
                "read": "read_data",
                "write": "send_command",
                "extra_info": [],
            },
        )
    )

    assert proxy.driver.read_register() == "read-result"
    assert proxy.driver.write_register("hello") == "write-result"
    assert communication.driver.calls == [
        ("read", {"slave_id": 7}),
        ("write", "hello", {"slave_id": 7}),
    ]


class _DeckStation(WorkstationBase):
    def __init__(self, deck=None, **_kwargs):
        super().__init__(deck)


def _station_with_deck_child():
    from unilabos.server.services.materials.graph import graph_resource_tree

    tree = graph_resource_tree(
        [
            {"id": "station", "uuid": "00000000-0000-4000-8000-0000000000a1", "type": "device",
             "class": "station", "template_name": "station", "parent": None, "config": {}},
            {"id": "deck", "uuid": "00000000-0000-4000-8000-0000000000a2", "type": "deck", "class": "Deck",
             "template_name": "Deck", "parent": "station",
             "config": {"type": "Deck", "size_x": 100.0, "size_y": 100.0, "size_z": 10.0}},
            {"id": "spare", "uuid": "00000000-0000-4000-8000-0000000000a3", "type": "deck", "class": "Deck",
             "template_name": "Deck", "parent": "station",
             "config": {"type": "Deck", "size_x": 50.0, "size_y": 50.0, "size_z": 5.0}},
        ]
    )
    return tree.trees[0].root_node


def test_workstation_creator_registers_the_graph_deck_once() -> None:
    """FRAMEWORK_GAPS #25：工作站构造器建好的台面不能被 attach_resource 再实例化一份。"""

    from pylabrobot.resources import Deck

    station_node = _station_with_deck_child()
    tracker = DeviceNodeResourceTracker()
    creator = select_driver_creator(
        _DeckStation, children=list(station_node.children), resource_tracker=tracker
    ).creator

    driver = creator.create_instance(
        {"deck": {"data": {"_resource_child_name": "deck", "_resource_type": "pylabrobot.resources:Deck"}}}
    )

    assert isinstance(driver.deck, Deck)
    assert [item for item in tracker.resources if item.name == "deck"] == [driver.deck]
    assert tracker.uuid_to_resources["00000000-0000-4000-8000-0000000000a2"] is driver.deck
    # 驱动没有引用的非设备子节点照常登记一次
    assert [item.name for item in tracker.resources if item.name == "spare"] == ["spare"]


class _ConfigAndFlatDriver:
    def __init__(self, config=None, debug_mode=False, address="10.0.0.1", deck=None, device_id=None):
        self.config = config
        self.debug_mode = debug_mode
        self.address = address
        self.deck = deck
        self.device_id = device_id


def test_hostlink_config_parameter_does_not_swallow_other_declared_parameters() -> None:
    """FRAMEWORK_GAPS #24：声明了 ``config`` 的构造函数，其他具名参数仍按名取节点配置。"""

    from unilabos.backend.hostlink.local_runtime import instantiate_driver

    node_config = {"debug_mode": True, "address": "127.0.0.1", "vendor_option": 3}
    driver = instantiate_driver(_ConfigAndFlatDriver, "cell", node_config)

    assert driver.debug_mode is True
    assert driver.address == "127.0.0.1"
    assert driver.device_id == "cell"
    # config 仍是整份节点配置（旧驱动从 config 里读参数的写法不变）
    assert driver.config == node_config
