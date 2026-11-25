"""``hostlink`` backend startup entrypoints."""

from __future__ import annotations

from typing import Any, Callable, Optional

from unilabos.client.host.hostlink.adapter_registry import clear_execution_adapter
from unilabos.client.host.runtime.definition import (
    is_host_node_config,
    iter_device_config_entries,
    resolve_device_definition,
)
from unilabos.client.host.runtime.exception import (
    DeviceBackendUnsupported,
    DeviceClassInvalid,
    DeviceMaintenanceError,
    DeviceStartupError,
)
from unilabos.client.host.hostlink.backend import HostLinkBackend
from unilabos.client.host.hostlink.local_runtime import (
    HostLinkDriverSpec,
    HostLinkLocalRuntime,
)
from unilabos.utils import logger


_runtime: Optional[HostLinkBackend] = None
_host_node: Optional[Any] = None


def validate_environment() -> None:
    """HostLink backend only depends on the Python driver runtime."""


def get_runtime() -> Optional[HostLinkBackend]:
    return _runtime


def build_runtime(devices_config: Any) -> HostLinkLocalRuntime:
    """从设备图构造 HostLink 本地驱动运行时。"""

    return populate_runtime(HostLinkLocalRuntime(), devices_config)


def populate_runtime(
    runtime: HostLinkLocalRuntime, devices_config: Any
) -> HostLinkLocalRuntime:
    """把设备图装配进尚未启动的运行时。

    每台设备经 ``resolve_device_definition`` 装配：物料权威可达时按权威优先取回
    Site 快照与持有的物料，所以 Slave 要在 HostLink 连上之后再调用。

    持有的物料建不成时这台设备进入维护模式；``runtime.isolate_device_failures``（Host 进程）
    时装配 / 构造失败同样只让这台设备进入维护模式，否则（Slave 进程）照旧抛出。
    """

    if devices_config is None:
        return runtime

    for entry in iter_device_config_entries(devices_config):
        device_id = entry.device_id
        node = entry.config
        if entry.parent_device_id in runtime.unavailable_devices:
            # 父设备没起来，子设备也没有宿主：一并跳过并说明原因
            _mark_unavailable(
                runtime,
                device_id,
                registry_name=str(node.res_content.template_name or ""),
                reason=f"父设备 {entry.parent_device_id!r} 在 backend hostlink 上不可用，子设备一并跳过",
                parent_device_id=entry.parent_device_id,
            )
            continue
        if is_host_node_config(node.res_content):
            # host node 按 template_name 判别且全图只能有一个；服务设备由
            # backend.start() 注册，这里只记录图中声明的身份（uuid），
            # 注册时复用，保证图导出与权威身份一致。
            if runtime.graph_host_resource_uuids:
                raise ValueError(
                    "图中只能声明一个 host node（template_name=host_node），当前有: "
                    + ", ".join(
                        [*runtime.graph_host_resource_uuids, device_id]
                    )
                )
            runtime.graph_host_resource_uuids[device_id] = node.res_content.uuid
            logger.debug(
                "[HostLink] 图中的 host node 由内置 host 服务承载，跳过驱动创建"
            )
            continue
        try:
            definition = resolve_device_definition(
                device_id,
                node,
                backend_name="hostlink",
            )
        except DeviceBackendUnsupported as exc:
            # 设备本身没问题，只是只能跑在别的 backend（如 MoveIt 设备只支持 ros2）：
            # 跳过它，其余设备照常启动；原因写日志并随能力快照 / 诊断接口上报
            _mark_unavailable(
                runtime,
                device_id,
                registry_name=exc.registry_name,
                reason=(
                    f"设备类只支持 backend {', '.join(exc.supported_backends)}，"
                    "当前 backend 为 hostlink，已跳过"
                ),
                supported_backends=exc.supported_backends,
                parent_device_id=entry.parent_device_id,
            )
            continue
        except DeviceMaintenanceError as exc:
            _enter_maintenance(runtime, entry, exc, registry_name=str(node.res_content.template_name or ""))
            continue
        except DeviceClassInvalid:
            # 图配置错误（模板名非法、注册表里没有这个设备）照旧让启动失败
            raise
        except Exception as exc:
            if not runtime.isolate_device_failures:
                raise
            # Host 进程：权威状态装配不起来、驱动导入失败，只让这台设备进入维护模式
            _enter_maintenance(
                runtime,
                entry,
                DeviceStartupError(stage="definition", error=exc),
                registry_name=str(node.res_content.template_name or ""),
            )
            continue
        try:
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
        except DeviceMaintenanceError as exc:
            # 维护模式：持有的物料建不成 PLR 实例，这台设备不启动（子设备随之跳过），其余照常
            _enter_maintenance(runtime, entry, exc, registry_name=definition.registry_name)
        except Exception as exc:
            if not runtime.isolate_device_failures:
                raise
            # Host 进程：驱动构造失败只让这台设备进入维护模式
            _enter_maintenance(
                runtime,
                entry,
                DeviceStartupError(stage="construct", error=exc),
                registry_name=definition.registry_name,
            )
    return runtime


def _enter_maintenance(
    runtime: HostLinkLocalRuntime,
    entry: Any,
    error: DeviceMaintenanceError,
    *,
    registry_name: str,
) -> None:
    """这台设备不启动（子设备随之跳过），原因登记为不可用设备；Host 与其余设备照常。"""

    cause = error.__cause__ or error
    logger.error(
        "[HostLink] 设备 %s 启动失败的原始异常",
        entry.device_id,
        exc_info=(type(cause), cause, cause.__traceback__),
    )
    error.device_id = entry.device_id
    record = error.unavailable_record()
    _mark_unavailable(
        runtime,
        entry.device_id,
        registry_name=registry_name,
        reason=record.pop("reason"),
        parent_device_id=entry.parent_device_id,
        extra=record,
    )


def _mark_unavailable(
    runtime: HostLinkLocalRuntime,
    device_id: str,
    *,
    registry_name: str,
    reason: str,
    supported_backends: Optional[list[str]] = None,
    parent_device_id: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> None:
    runtime.mark_unavailable(
        device_id,
        registry_name=registry_name,
        reason=reason,
        supported_backends=supported_backends,
        parent_device_id=parent_device_id,
        extra=extra,
    )


def unavailable_devices() -> list[dict[str, Any]]:
    """本进程 HostLink runtime 没有启动的设备（backend 不支持、维护模式；供诊断 / 健康接口）。"""

    runtime = _runtime
    local = getattr(runtime, "local", None)
    records = getattr(local, "unavailable_devices", None) or {}
    return [dict(records[device_id]) for device_id in sorted(records)]


def _align_slave_materials(resources_config: Any) -> None:
    """Slave 经 HostLink 调用 materials.ensure：采用图中的物料 UUID，权威缺失时以该 UUID 创建。"""

    from unilabos.config.config import BasicConfig

    if (
        resources_config is None
        or not getattr(resources_config, "trees", None)
        or BasicConfig.slave_no_host
    ):
        return
    from unilabos.protocol.materials import ACTOR_GRAPH
    from unilabos.resources import materials

    ensured = materials.ensure(
        resources_config,
        actor_type=ACTOR_GRAPH,
        actor_uuid=BasicConfig.machine_name or None,
    )
    logger.info(
        "[HostLink] Slave 物料权威对齐完成: %s 棵树（uuid 与图一致）",
        len(ensured.trees),
    )


def startup_populate(
    devices_config: Any, resources_config: Any, *, is_slave: bool
) -> Callable[[HostLinkLocalRuntime], None]:
    """``HostLinkBackend.start(populate=...)`` 用的设备图装配步骤。

    Slave 在此刻已连上 Host：先按图对齐物料权威，再装配设备，设备的 Site 与持有
    的物料按权威优先取回。Host 侧的对齐已在 main.py 启动流程里完成。
    """

    def populate(local: HostLinkLocalRuntime) -> None:
        if is_slave:
            _align_slave_materials(resources_config)
        populate_runtime(local, devices_config)

    return populate


def _run(
    devices_config: Any,
    resources_config: Any,
    *,
    is_slave: bool,
    bridges: Optional[list[Any]] = None,
) -> None:
    global _host_node, _runtime
    # 设备失败的隔离只在 Host 进程：Slave 失败即退出，由受管进程按重启策略处理
    runtime = HostLinkBackend(HostLinkLocalRuntime(isolate_device_failures=not is_slave), is_slave=is_slave)
    _runtime = runtime
    try:
        runtime.start(
            populate=startup_populate(devices_config, resources_config, is_slave=is_slave)
        )
        if not is_slave:
            from unilabos.client.host.hostlink.host_node import HostNode
            from unilabos.config.config import BasicConfig

            # HostNode 构造时注册执行适配器、刷新设备快照并启动监控；
            # finally 负责关闭其生命周期。
            _host_node = HostNode(
                BasicConfig.host_node_name,
                runtime,
                bridges=bridges,
            )
        while not runtime.local.wait(timeout=1.0):
            pass
    finally:
        host_node, _host_node = _host_node, None
        if host_node is not None:
            clear_execution_adapter(host_node)
            host_node.stop()
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
    "build_runtime",
    "get_runtime",
    "main",
    "populate_runtime",
    "slave",
    "startup_populate",
    "unavailable_devices",
    "validate_environment",
]
