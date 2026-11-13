"""两种设备 backend 共用的注册表解析与设备树遍历。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from unilabos.config.config import HOST_NODE_REGISTRY_NAME
from unilabos.registry.init_enforce import merge_init_param_enforce
from unilabos.registry.material_locks import normalize_material_parameter_names
from unilabos.resources.adapters.device_site import apply_device_authority_state
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.backend.runtime.exception import DeviceBackendUnsupported, DeviceClassInvalid
from unilabos.utils.import_manager import default_manager


def is_host_node_config(content: Any, device_id: str = "") -> bool:
    """按注册表模板名判断是否 host 服务设备（实例 id 可重命名）。

    ``template_name == "host_node"`` 是唯一判据（旧图的 ``class`` 已在图读取
    边界回填为 template_name）；``device_id`` 仅兼容未声明模板的旧图节点
    （template_name 回退为 type，id 与配置的实例名一致时视为 host node）。
    """

    template_name = str(content.template_name or "")
    if template_name == HOST_NODE_REGISTRY_NAME:
        return True
    undeclared = template_name in ("", str(content.type or ""))
    return bool(device_id) and content.id == device_id and undeclared


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
    """将 Registry YAML 解析为 HostLink 与 ROS2 共用的设备定义。"""

    from unilabos.registry.registry import lab_registry

    registry_name = device_config.res_content.template_name
    if not isinstance(registry_name, str):
        raise DeviceClassInvalid(
            f"Device [{device_id}] template_name must be a registry name string, "
            f"but {type(registry_name).__name__} got. {device_config}"
        )
    registry_name = registry_name.strip()
    if not registry_name:
        raise DeviceClassInvalid(
            f"Device [{device_id}] template_name cannot be empty. {device_config}"
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
        from unilabos.backend import resolve_driver_backends

        supported = resolve_driver_backends(driver_config)
        if backend_name not in supported:
            raise DeviceBackendUnsupported(
                device_id,
                backend_name=backend_name,
                supported_backends=supported,
                registry_name=registry_name,
            )

    # 权威优先：设备的 Site 快照与它持有的物料（图中子节点）都以物料权威为准
    apply_device_authority_state(device_config, registry_entry, registry_name)
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
        display_name=str(registry_entry.get("display_name") or registry_name),
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
    "is_host_node_config",
    "iter_device_config_entries",
    "resolve_device_definition",
]
