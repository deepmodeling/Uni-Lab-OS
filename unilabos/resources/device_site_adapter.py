"""设备注册表 ``available_sites`` 与实例根字段 ``sites`` 的适配。"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from unilabos.resources.resource_tracker import ResourceDictInstance, ResourceTreeSet
from unilabos.resources.objects.site import (
    normalize_available_sites,
    validate_instantiated_sites,
)


def apply_device_available_sites(
    device_config: ResourceDictInstance,
    registry_entry: Dict[str, Any],
    registry_name: str,
) -> None:
    """核验微后端设备 Site 快照与 Registry 模板定义一致。

    Registry ``available_sites`` 不写入实例；Edge 不生成或修改 Site 身份。
    """

    resource = device_config.res_content
    if resource.type != "device":
        raise ValueError(
            f"available_sites 只能应用于设备，{resource.id} 的 type={resource.type!r}"
        )

    definitions = normalize_available_sites(registry_entry.get("available_sites"))
    registry_entry["available_sites"] = definitions

    if resource.template_name != registry_name:
        raise ValueError(
            f"设备 {resource.id} 的 template_name={resource.template_name!r} "
            f"与注册表 {registry_name!r} 冲突"
        )

    current_sites = (
        [site.model_dump() for site in resource.sites]
        if resource.sites is not None
        else None
    )
    validate_instantiated_sites(
        definitions,
        owner_uuid=resource.uuid,
        template_name=registry_name,
        current_sites=current_sites,
        sites_initialized=resource.sites_initialized,
    )


def prepare_devices_for_report(
    resources: ResourceTreeSet,
    device_registry: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> int:
    """在设备启动/上报前校验微后端返回的 Site 权威快照。

    该入口不会生成 UUID、复制 available_sites 或修改实例。
    """

    if device_registry is None:
        from unilabos.registry.registry import lab_registry

        device_registry = lab_registry.device_type_registry

    prepared = 0
    for node in resources.all_nodes:
        resource = node.res_content
        if resource.type != "device":
            continue
        registry_name = resource.klass
        if not isinstance(registry_name, str) or not registry_name:
            raise ValueError(f"设备 {resource.id} 的 class 不能为空")
        registry_entry = device_registry.get(registry_name)
        if registry_entry is None:
            raise ValueError(f"设备 {resource.id} 的 class={registry_name!r} 不在注册表中")
        apply_device_available_sites(node, registry_entry, registry_name)
        prepared += 1
    return prepared


__all__ = ["apply_device_available_sites", "prepare_devices_for_report"]
