"""无中间件 ``basic`` backend 的启动入口。"""

from __future__ import annotations

from typing import Any, Optional

from unilabos.app.backend import resolve_driver_backends
from unilabos.basic.runtime import BasicDriverSpec, BasicRuntime
from unilabos.device_runtime.resource import LocalResourceService, ResourceStore
from unilabos.registry.init_enforce import merge_init_param_enforce
from unilabos.utils import logger
from unilabos.utils.import_manager import default_manager


_runtime: Optional[BasicRuntime] = None


def get_runtime() -> Optional[BasicRuntime]:
    return _runtime


def build_runtime(devices_config: Any, backend_name: str = "basic") -> BasicRuntime:
    from unilabos.registry.registry import lab_registry

    runtime = BasicRuntime(backend_name=backend_name)
    if devices_config is None:
        return runtime

    for node in devices_config.all_nodes:
        resource = node.res_content
        if getattr(resource, "type", None) != "device":
            continue
        device_id = str(resource.id)
        registry_name = resource.klass
        if not isinstance(registry_name, str):
            raise ValueError(f"Basic 设备 {device_id!r} 的注册表 class 必须是字符串")
        if not registry_name.strip():
            logger.debug("[Basic] 跳过没有驱动 class 的子节点：%s", device_id)
            continue
        try:
            registry_entry = lab_registry.device_type_registry[registry_name]
        except KeyError as exc:
            raise ValueError(
                f"Basic 设备 {device_id!r} 的 class {registry_name!r} 未注册"
            ) from exc
        categories = registry_entry.get("category") or []
        if isinstance(categories, str):
            categories = [categories]
        if "work_station" in categories:
            logger.info("[Basic] 跳过工作站聚合节点：%s", device_id)
            continue
        class_config = registry_entry.get("class") or {}
        supported_backends = resolve_driver_backends(class_config)
        if backend_name not in supported_backends:
            raise ValueError(
                f"设备 {device_id!r} 不支持 backend '{backend_name}'；"
                f"该驱动支持：{', '.join(supported_backends)}"
            )
        module_spec = class_config.get("module")
        if not isinstance(module_spec, str) or ":" not in module_spec:
            raise ValueError(f"Basic 设备 {device_id!r} 缺少有效的 module:Class 配置")
        driver_class = default_manager.get_class(module_spec)
        config = merge_init_param_enforce(
            resource.config if isinstance(resource.config, dict) else {},
            registry_entry.get("init_param_enforce"),
        )
        runtime.add_driver(
            BasicDriverSpec(
                device_id=device_id,
                driver_class=driver_class,
                config=config,
                registry_name=registry_name,
                display_name=str(registry_entry.get("displayname") or registry_name),
                resource_uuid=str(resource.uuid or ""),
                action_names=tuple(
                    (class_config.get("action_value_mappings") or {}).keys()
                ),
                action_value_mappings=dict(
                    class_config.get("action_value_mappings") or {}
                ),
                status_names=tuple((class_config.get("status_types") or {}).keys()),
                device_config=node,
            )
        )
    return runtime


# 保留内部旧名称，避免嵌入方在过渡期失效。
_build_runtime = build_runtime


def main(
    devices_config: Any,
    resources_config: Any,
    resources_edge_config: Optional[list[dict[str, Any]]] = None,
    graph: Any = None,
    controllers_config: Optional[dict[str, Any]] = None,
    bridges: Optional[list[Any]] = None,
    visual: str = "disable",
    resources_mesh_config: Optional[dict[str, Any]] = None,
    *args: Any,
    **kwargs: Any,
) -> None:
    """在进程内加载 Python 驱动，并保持运行直到进程退出。"""

    global _runtime
    _runtime = build_runtime(devices_config)
    _runtime.set_resource_service(LocalResourceService(ResourceStore(resources_config)))
    _runtime.start()
    logger.info(
        "[Basic] 运行时已启动，共 %d 台设备：%s",
        len(_runtime.devices),
        sorted(_runtime.devices),
    )
    try:
        while not _runtime.wait(timeout=1.0):
            pass
    finally:
        _runtime.stop()


def slave(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("Basic backend 不支持 Slave 模式；请使用 ros2")
