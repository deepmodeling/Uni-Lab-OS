"""``hostlink`` backend startup entrypoints."""

from __future__ import annotations

from typing import Any, Optional

from unilabos.hostlink.adapter_registry import (
    clear_execution_adapter,
    set_execution_adapter,
)
from unilabos.hostlink.execution_adapter import HostLinkExecutionAdapter
from unilabos.device_runtime.definition import (
    iter_device_config_entries,
    resolve_device_definition,
)
from unilabos.hostlink.backend import HostLinkBackend
from unilabos.hostlink.local_runtime import (
    HostLinkDriverSpec,
    HostLinkLocalRuntime,
)
from unilabos.utils import logger


_runtime: Optional[HostLinkBackend] = None
_execution_adapter: Optional[HostLinkExecutionAdapter] = None


def validate_environment() -> None:
    """HostLink backend only depends on the Python driver runtime."""


def get_runtime() -> Optional[HostLinkBackend]:
    return _runtime


def get_execution_adapter() -> Optional[HostLinkExecutionAdapter]:
    return _execution_adapter


def build_runtime(devices_config: Any) -> HostLinkLocalRuntime:
    """从设备图构造 HostLink 本地驱动运行时。"""

    runtime = HostLinkLocalRuntime()
    if devices_config is None:
        return runtime

    for entry in iter_device_config_entries(devices_config):
        device_id = entry.device_id
        node = entry.config
        if node.res_content.klass == "host_node":
            logger.debug(
                "[HostLink] 跳过图中的 host_node；Host 生命周期由微后端管理"
            )
            continue
        definition = resolve_device_definition(
            device_id,
            node,
            backend_name="hostlink",
        )
        runtime.add_driver(
            HostLinkDriverSpec(
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
                parent_device_id=entry.parent_device_id,
                hardware_interface=definition.hardware_interface,
            )
        )
    return runtime


def _run(
    devices_config: Any,
    resources_config: Any,
    *,
    is_slave: bool,
    bridges: Optional[list[Any]] = None,
) -> None:
    global _execution_adapter, _runtime
    runtime = HostLinkBackend(
        build_runtime(devices_config),
        is_slave=is_slave,
    )
    _runtime = runtime
    try:
        runtime.start()
        if not is_slave:
            _execution_adapter = HostLinkExecutionAdapter(
                runtime,
                devices_config,
                resources_config,
                bridges=bridges,
            )
            _execution_adapter.start()
            set_execution_adapter(_execution_adapter)
            _execution_adapter.notify_ready()
        while not runtime.local.wait(timeout=1.0):
            pass
    finally:
        adapter, _execution_adapter = _execution_adapter, None
        if adapter is not None:
            clear_execution_adapter(adapter)
            adapter.stop()
        runtime.stop()
        if _runtime is runtime:
            _runtime = None


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
    del (
        resources_edge_config,
        graph,
        controllers_config,
        visual,
        resources_mesh_config,
        args,
        kwargs,
    )
    _run(
        devices_config,
        resources_config,
        is_slave=False,
        bridges=bridges,
    )


def slave(
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
    del (
        resources_edge_config,
        graph,
        controllers_config,
        visual,
        resources_mesh_config,
        args,
        kwargs,
    )
    _run(devices_config, resources_config, is_slave=True, bridges=None)


__all__ = [
    "get_execution_adapter",
    "get_runtime",
    "main",
    "slave",
    "validate_environment",
]
