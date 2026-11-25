"""虚拟加热平台的测试装配：设备物料按注册表 available_sites 在物料权威里装配。

与启动时 ``runtime/definition.py`` 同一步（``apply_device_authority_state``）：权威没有该设备时
以图中 uuid 创建设备物料与三个工位；已有时采用权威的工位与下挂物料（重启时样品从这里挂回）。
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from unilabos.devices.virtual.heating_platform import VirtualHeatingPlatform
from unilabos.registry.decorators import get_device_meta
from unilabos.resources.adapters.device_site import apply_device_authority_state
from unilabos.resources.objects.resource import ResourceDict
from unilabos.resources.resource_tracker import ResourceDictInstance

HEATER_REGISTRY_NAME = "virtual_heating_platform"


def heating_device_config(
    device_id: str, gateway: Any, *, device_uuid: Optional[str] = None
) -> ResourceDictInstance:
    """加热平台的设备节点（图中形状），并在权威里装配好它的设备物料。"""

    config = ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": device_id,
                "uuid": device_uuid or str(uuid4()),
                "name": device_id,
                "type": "device",
                "class": HEATER_REGISTRY_NAME,
                "template_name": HEATER_REGISTRY_NAME,
                "config": {},
                "data": {},
                "extra": {},
                "sites": None,
                "sites_initialized": False,
            }
        )
    )
    meta = get_device_meta(VirtualHeatingPlatform, HEATER_REGISTRY_NAME)
    apply_device_authority_state(
        config,
        {"available_sites": meta["available_sites"]},
        HEATER_REGISTRY_NAME,
        gateway=gateway,
    )
    return config
