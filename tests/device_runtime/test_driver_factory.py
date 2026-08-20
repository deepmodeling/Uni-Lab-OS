from __future__ import annotations

from types import SimpleNamespace

from unilabos.hostlink.main_hostlink_run import build_runtime
from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.device_runtime.definition import iter_device_configs
from unilabos.device_runtime.driver_creator import select_driver_creator
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
        device_id for device_id, _config in iter_device_configs(tree_set)
    ] == ["station", "pump", "balance"]


def test_hostlink_does_not_instantiate_ros_host_node() -> None:
    host_node = _node("host_node")
    host_node.res_content.klass = "host_node"

    runtime = build_runtime(SimpleNamespace(root_nodes=[host_node]))

    assert runtime.devices == {}


def test_hostlink_runtime_uses_same_factory_for_dynamic_subdevice(monkeypatch) -> None:
    from unilabos.device_runtime import definition as definition_module

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
