"""HostLink 内部使用的本地 Python 驱动运行时构建入口。

``BasicRuntime`` 名称保留给内部实现与测试；它不属于公开部署 backend。
"""

from __future__ import annotations

from typing import Any, Optional

from unilabos.basic.runtime import BasicDriverSpec, BasicRuntime
from unilabos.device_runtime.definition import (
    iter_device_configs,
    resolve_device_definition,
)
from unilabos.utils import logger


_runtime: Optional[BasicRuntime] = None


def get_runtime() -> Optional[BasicRuntime]:
    return _runtime


def build_runtime(devices_config: Any, backend_name: str = "basic") -> BasicRuntime:
    runtime = BasicRuntime(backend_name=backend_name)
    if devices_config is None:
        return runtime

    for device_id, node in iter_device_configs(devices_config):
        if node.res_content.klass == "host_node":
            logger.debug(
                "[HostLink] 跳过图中的 host_node；Host 生命周期由微后端管理"
            )
            continue
        definition = resolve_device_definition(
            device_id,
            node,
            backend_name=backend_name,
        )
        runtime.add_driver(
            BasicDriverSpec(
                device_id=device_id,
                driver_class=definition.driver_class,
                config=definition.runtime_config,
                registry_name=definition.registry_name,
                display_name=definition.display_name,
                resource_uuid=definition.resource_uuid,
                action_names=tuple(definition.action_value_mappings),
                action_value_mappings=definition.action_value_mappings,
                status_names=tuple(definition.status_types),
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
    """内部兼容入口：在进程内加载 Python 驱动并保持运行。"""

    global _runtime
    del resources_config
    _runtime = build_runtime(devices_config)
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
    raise RuntimeError("BasicRuntime 是内部本地执行引擎；请使用 hostlink 或 ros2")
