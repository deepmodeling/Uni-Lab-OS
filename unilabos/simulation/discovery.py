"""从已发现的 Registry 模板读取可选描述，不导入设备或创建模型。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from unilabos.runtime.observations import Descriptor, Selector, StateError

if TYPE_CHECKING:
    from unilabos.app.scheduler.device_observations import DeviceObservationService


def bind_descriptors(runtime: DeviceObservationService, templates: Mapping[str, dict[str, Any]]) -> list[str]:
    """用规范模板 UUID 绑定当前库存实例；不猜设备名或 CAD 对象名。

    templates 由 OS Registry 投影传入，值为现有模板（含 metadata/model）。
    没有 twin 元数据的普通设备保持原行为。model 仍只有注册表一个入口。
    """
    bound = []
    for node in runtime.snapshot()["nodes"]:
        template = templates.get(node["resource_template_uuid"])
        metadata = (template or {}).get("metadata", {})
        declaration = metadata.get("observations", metadata.get("twin"))
        if declaration is None:
            continue
        if not node["id"]:
            raise StateError("identity_missing")
        runtime.describe(Selector(value=node["id"]), Descriptor.model_validate(declaration))
        bound.append(node["uuid"])
    return bound
