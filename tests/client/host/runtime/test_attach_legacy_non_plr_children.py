"""旧数据里设备下遗留的非 PLR 物料：不挂到本地、点名报错，整台设备照常启动。

权威已在落位时拒绝新的非 PLR 物料进入设备子树；这里只处理升级前留下的旧数据（如旧版
虚拟加热平台经网关手写、没有 ``config.type`` 的样品）——以前它让设备在重启时直接起不来。
"""

from __future__ import annotations

import logging
from uuid import uuid4

from pylabrobot.resources import Resource

from unilabos.client.host.runtime.driver_creator import DeviceClassCreator
from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceDict,
    ResourceDictInstance,
    ResourceTreeSet,
)


class _Driver:
    def __init__(self, **_kwargs) -> None:
        pass


def _legacy_sample(device_uuid: str) -> ResourceDictInstance:
    return ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": "heater-sample-1",
                "uuid": str(uuid4()),
                "name": "heater-sample-1",
                "type": "resource",
                "class": "Resource",
                "template_name": "virtual_heating_sample",
                "parent_uuid": device_uuid,
                "config": {},
                "data": {"temperature_c": 25.0},
                "extra": {},
            }
        )
    )


def _plr_child(device_uuid: str) -> ResourceDictInstance:
    source = Resource("legacy_neighbour", size_x=10, size_y=10, size_z=10)
    tree = ResourceTreeSet.from_plr_resources([source], known_random_uuid=True).trees[0]
    tree.root_node.res_content.parent_uuid = device_uuid
    return tree.root_node


def test_legacy_non_plr_child_is_skipped_and_named(caplog) -> None:
    device_uuid = str(uuid4())
    legacy = _legacy_sample(device_uuid)
    neighbour = _plr_child(device_uuid)
    tracker = DeviceNodeResourceTracker()
    creator = DeviceClassCreator(_Driver, [legacy, neighbour], tracker)

    with caplog.at_level(logging.ERROR):
        creator.create_instance({})

    assert legacy.res_content.uuid not in tracker.uuid_to_resources
    # 同一设备下的 PLR 物料照常挂上
    assert neighbour.res_content.uuid in tracker.uuid_to_resources
    assert any("heater-sample-1" in record.getMessage() for record in caplog.records)
