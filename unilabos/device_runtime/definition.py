"""两种设备 backend 共用的注册表解析与设备树遍历。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from unilabos.registry.init_enforce import merge_init_param_enforce
from unilabos.registry.material_locks import normalize_material_parameter_names
from unilabos.resources.device_site_adapter import apply_device_available_sites
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.utils.exception import DeviceClassInvalid
from unilabos.utils.import_manager import default_manager


@dataclass(frozen=True)
class DeviceDefinition:
    device_id: str
    resource_uuid: str
    registry_name: str
    display_name: str
    driver_class: type[Any]
    driver_config: dict[str, Any]
    runtime_config: dict[str, Any]
    registry_entry: dict[str, Any]
    categories: tuple[str, ...]

    @property
    def action_value_mappings(self) -> dict[str, Any]:
        mappings: dict[str, Any] = {}
        for action_name, raw_definition in (
            self.driver_config.get("action_value_mappings") or {}
        ).items():
            if not isinstance(raw_definition, dict):
                mappings[action_name] = raw_definition
                continue
            definition = dict(raw_definition)
            definition["materials_need_lock"] = (
                normalize_material_parameter_names(
                    definition.get("materials_need_lock"),
                    action_name=str(action_name),
                )
            )
            mappings[action_name] = definition
        return mappings

    @property
    def status_types(self) -> dict[str, Any]:
        return dict(self.driver_config.get("status_types") or {})

    @property
    def hardware_interface(self) -> dict[str, Any]:
        return dict(
            self.driver_config.get("hardware_interface")
            or {
                "name": "hardware_interface",
                "write": "send_command",
                "read": "read_data",
                "extra_info": [],
            }
        )

    @property
    def is_native_ros(self) -> bool:
        return self.driver_config.get("type") == "ros2"


@dataclass(frozen=True)
class DeviceConfigEntry:
    """设备树中的一个运行节点及其最近设备父节点。"""

    device_id: str
    config: ResourceDictInstance
    parent_device_id: str = ""


def resolve_device_definition(
    device_id: str,
    device_config: ResourceDictInstance,
    *,
    backend_name: str | None = None,
) -> DeviceDefinition:
    """解析一次 registry；HostLink 与 ROS2 不再各自解释同一份 YAML。"""

    from unilabos.registry.registry import lab_registry

    registry_name = device_config.res_content.klass
    if not isinstance(registry_name, str):
        raise DeviceClassInvalid(
            f"Device [{device_id}] class must be a registry name string, "
            f"but {type(registry_name).__name__} got. {device_config}"
        )
    registry_name = registry_name.strip()
    if not registry_name:
        raise DeviceClassInvalid(
            f"Device [{device_id}] class cannot be empty. {device_config}"
        )
    registry_entry = lab_registry.device_type_registry.get(registry_name)
    if registry_entry is None:
        raise DeviceClassInvalid(
            f"Device [{device_id}] registry {registry_name!r} not found. {device_config}"
        )
    if not isinstance(registry_entry, dict):
        raise DeviceClassInvalid(
            f"Device [{device_id}] registry {registry_name!r} must be an object. {device_config}"
        )
    driver_config = registry_entry.get("class")
    if not isinstance(driver_config, dict):
        raise DeviceClassInvalid(
            f"Device [{device_id}] registry {registry_name!r}.class must be an object. {device_config}"
        )
    module = driver_config.get("module")
    if not isinstance(module, str) or not module.strip():
        raise DeviceClassInvalid(
            f"Device [{device_id}] registry {registry_name!r}.class.module must be a non-empty string. "
            f"{device_config}"
        )
    if backend_name is not None:
        from unilabos.app.backend import resolve_driver_backends

        supported = resolve_driver_backends(driver_config)
        if backend_name not in supported:
            raise DeviceClassInvalid(
                f"Device [{device_id}] does not support backend {backend_name!r}; "
                f"supported: {', '.join(supported)}"
            )

    apply_device_available_sites(device_config, registry_entry, registry_name)
    raw_config = device_config.res_content.config
    runtime_config = merge_init_param_enforce(
        raw_config if isinstance(raw_config, dict) else {},
        registry_entry.get("init_param_enforce"),
    )
    raw_categories = registry_entry.get("category") or []
    if isinstance(raw_categories, str):
        raw_categories = [raw_categories]
    return DeviceDefinition(
        device_id=str(device_id),
        resource_uuid=str(device_config.res_content.uuid or ""),
        registry_name=registry_name,
        display_name=str(registry_entry.get("displayname") or registry_name),
        driver_class=default_manager.get_class(module.strip()),
        driver_config=dict(driver_config),
        runtime_config=runtime_config,
        registry_entry=registry_entry,
        categories=tuple(str(value) for value in raw_categories),
    )


def iter_device_config_entries(devices_config: Any) -> Iterator[DeviceConfigEntry]:
    """按父节点优先顺序产出普通设备、工作站和子设备。"""

    if devices_config is None:
        return

    def walk(
        node: ResourceDictInstance,
        parent_device_id: str = "",
    ) -> Iterator[DeviceConfigEntry]:
        resource = node.res_content
        next_parent_id = parent_device_id
        if getattr(resource, "type", None) == "device":
            device_id = str(resource.id).strip().strip("/")
            if not device_id:
                raise DeviceClassInvalid("Device id cannot be empty")
            yield DeviceConfigEntry(
                device_id=device_id,
                config=node,
                parent_device_id=parent_device_id,
            )
            next_parent_id = device_id
        for child in node.children:
            yield from walk(child, next_parent_id)

    for root in devices_config.root_nodes:
        yield from walk(root)


__all__ = [
    "DeviceConfigEntry",
    "DeviceDefinition",
    "iter_device_config_entries",
    "resolve_device_definition",
]
