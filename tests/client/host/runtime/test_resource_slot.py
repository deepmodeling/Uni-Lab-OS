"""ResourceSlot 参数识别：HostLink 与 ROS 2 共用，字符串注解与真实类型同样识别。"""

from __future__ import annotations

from typing import List, Optional

import pytest

from unilabos.client.host.runtime.resource_slot import is_unset_resource_slot, resource_slot_shape, resource_slot_shapes
from unilabos.registry.decorators import action
from unilabos.registry.placeholder_type import ResourceSlot


@pytest.mark.parametrize(
    ("annotation", "shape"),
    [
        (ResourceSlot, "single"),
        (Optional[ResourceSlot], "single"),
        (ResourceSlot | None, "single"),
        (list[ResourceSlot], "list"),
        (List[ResourceSlot], "list"),
        ("ResourceSlot", "single"),
        ("Optional[ResourceSlot]", "single"),
        ("list[ResourceSlot]", "list"),
        ("typing.List[ResourceSlot]", "list"),
        (str, None),
        ("str", None),
        (list[str], None),
        (dict, None),
        ("Annotated[ResourceSlot, AllowedResourceTemplates(Plate)]", "single"),
        ("Annotated[list[ResourceSlot], Field(min_length=1)]", "list"),
    ],
)
def test_resource_slot_shape(annotation, shape):
    assert resource_slot_shape(annotation) == shape


class _Driver:
    # 本模块启用了 from __future__ import annotations：这里的注解全是字符串，
    # 与驱动包里常见写法一致（ROS 2 曾因此把 ResourceSlot 原样以 dict 传入驱动）。
    @action(always_free=False)
    def pick(self, sample: ResourceSlot, owners: list[ResourceSlot], site: str, note: Optional[str] = None) -> dict:
        return {}


def test_resource_slot_shapes_with_string_annotations_through_action_decorator():
    assert resource_slot_shapes(_Driver().pick) == {"sample": "single", "owners": "list"}


def test_resource_slot_shapes_tolerates_uninspectable_callables():
    assert resource_slot_shapes(print) == {}


@pytest.mark.parametrize(
    ("value", "unset"),
    [
        (None, True),
        ("", True),
        ("  ", True),
        ({}, True),
        ([], True),
        ({"uuid": "u"}, False),
        ({"id": "flask"}, False),
        ("u", False),
        ([{"uuid": "u"}], False),
    ],
)
def test_unset_resource_slot_matches_the_scheduler_absent_rule(value, unset):
    from unilabos.server.scheduler.materials import _is_absent_material

    assert is_unset_resource_slot(value) is unset
    # 调度器同一口径：未指定的物料参数不解析 uuid、不加锁
    assert _is_absent_material(value) is unset


class _OptionalSlotDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id

    def heat(self, temp: float, vessel: Optional[ResourceSlot] = None) -> dict:
        return {"vessel": vessel, "temp": temp}


@pytest.mark.parametrize("empty", [None, "", {}])
def test_hostlink_hands_an_unset_optional_slot_to_the_driver_as_none(empty):
    from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime

    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("heater", _OptionalSlotDriver, {}))
    runtime.start()
    try:
        # goal 默认值给可选容器填的是 {}：与 None / "" 一样不解析（没有资源服务也不报错）
        assert runtime.call_action("heater", "heat", temp=40.0, vessel=empty) == {"vessel": None, "temp": 40.0}
    finally:
        runtime.stop()


def _device_node(device_id: str, uuid: str) -> dict:
    return {"id": device_id, "uuid": uuid, "type": "device", "class": "test_device",
            "template_name": "test_device", "parent": None, "config": {}}


def test_a_slot_tree_rooted_at_a_device_names_the_parameter_and_the_device():
    from unilabos.resources.resource_tracker import ResourceTreeSet
    from unilabos.server.services.materials.graph import graph_resource_tree
    from unilabos.client.host.runtime.resource_slot import (
        DeviceResourceSlotError,
        first_plr_resource,
        reject_device_slot,
    )
    from tests.plr_fixtures import plate

    robot = graph_resource_tree([_device_node("robot", "6f0d1f3e-0c7a-4a55-9b7e-1d2c3b4a5f61")])
    with pytest.raises(DeviceResourceSlotError) as caught:
        first_plr_resource(robot, "slot-ref", "target_owner")
    message = str(caught.value)
    assert "参数 target_owner" in message and "robot" in message and "6f0d1f3e-0c7a-4a55-9b7e-1d2c3b4a5f61" in message
    assert "设备不是物料" in message and "DeviceSlot" in message
    plates = ResourceTreeSet.from_plr_resources([plate("p")], known_random_uuid=True)
    reject_device_slot(plates, "sample")
    assert first_plr_resource(plates, "slot-ref").name == "p"


def test_a_slot_tree_without_plr_materials_fails_with_a_clear_error():
    """一个 PLR 物料都转不出（空树）时说清是哪个引用，不再 IndexError。"""

    from unilabos.resources.resource_tracker import ResourceTreeSet
    from unilabos.client.host.runtime.resource_slot import first_plr_resource

    with pytest.raises(ValueError, match="没有 PLR 物料") as caught:
        first_plr_resource(ResourceTreeSet([]), "slot-ref")
    assert "slot-ref" in str(caught.value) and "空树" in str(caught.value)
