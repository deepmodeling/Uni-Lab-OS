"""微后端服务的启动组合。

应用入口只声明运行环境；四库路径、物料权威和执行服务的装配都在这里完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.server.database import ServerDatabasePaths
from unilabos.utils.log import warning


@dataclass(frozen=True)
class HostServerStack:
    database_paths: ServerDatabasePaths
    materials_gateway: Any
    execution_backend: Any
    material_authority: str
    template_count: int
    host_network: Any = None


#: 工作目录下的数据库子目录：四个库、Host 子进程的 edge/ 与 ``--role backend`` 的 backend/ 都在这里。
DATABASE_DIRNAME = "database"
#: 0.12.x 的默认位置。工作目录下还没有数据库而这里已有时继续沿用，升级后数据不会“消失”。
LEGACY_DATABASE_ROOT = "~/.unilabos"
_DATABASE_FILES = ("runtime.db", "materials.db", "telemetry.db", "history.db")
_legacy_warned: set[str] = set()


def _has_databases(root: Path) -> bool:
    return any((root / name).exists() for name in _DATABASE_FILES)


def default_database_root(working_dir: str | Path) -> str:
    """未显式配置时的根目录：``<working_dir>/database``；旧位置 ``~/.unilabos`` 已有数据库且工作目录尚无时沿用旧位置。"""

    default = Path(working_dir).expanduser() / DATABASE_DIRNAME
    legacy = Path(LEGACY_DATABASE_ROOT).expanduser()
    if not _has_databases(default) and _has_databases(legacy):
        key = str(legacy)
        if key not in _legacy_warned:
            _legacy_warned.add(key)
            warning(
                f"[Database] 数据库仍在旧的默认位置 {legacy}，本次继续使用。新版本默认放在工作目录下的 {default}；"
                f"如需迁移，停止 unilab 后把 {', '.join(_DATABASE_FILES)}（连同 -wal / -shm 文件）以及 edge/、backend/ "
                f"目录移到 {default}，或在 local_config.py 设置 BasicConfig.server_database_root 固定位置。"
            )
        return str(legacy)
    return str(default)


def configured_database_root(args: Mapping[str, Any], *, working_dir: str | Path) -> str:
    """四库根目录：``--server_database_root`` > ``BasicConfig.server_database_root`` > ``<working_dir>/database``。

    ``BasicConfig.server_database_root`` 来自配置文件或环境变量
    ``UNILABOS_BASICCONFIG_SERVER_DATABASE_ROOT``。返回值未展开 ``~``，由路径解析统一处理。
    """

    for candidate in (args.get("server_database_root"), getattr(BasicConfig, "server_database_root", None)):
        text = str(candidate or "").strip()
        if text:
            return text
    return default_database_root(working_dir)


def resolve_database_paths(
    args: Mapping[str, Any], *, working_dir: str | Path
) -> ServerDatabasePaths:
    """解析四库路径并一次性绑定到进程配置；位置规则见 :func:`configured_database_root`。"""

    root = configured_database_root(args, working_dir=working_dir)
    overrides = {
        key: value
        for key, value in {
            "runtime": args.get("runtime_db"),
            "materials": args.get("materials_db"),
            "telemetry": args.get("telemetry_db"),
            "history": args.get("history_db"),
        }.items()
        if value is not None and str(value).strip()
    }
    paths = ServerDatabasePaths.resolve(root, overrides)
    BasicConfig.server_database_paths = paths
    return paths


def setup_host_server_stack(
    *,
    args: Mapping[str, Any],
    working_dir: str | Path,
    registry: Any,
    communication_client: Any,
) -> HostServerStack:
    """装配 Host 唯一的微后端权威及其控制链路。"""

    from unilabos.resources.adapters.registry_materials import sync_registry_resources
    from unilabos.client.materials import (
        HTTPMaterialsClient,
        LocalMaterialsClient,
    )
    from unilabos.server.backend.composition import (
        set_materials_gateway,
        shutdown_backend_services,
        setup_execution_backend,
        setup_materials_service,
    )

    paths = resolve_database_paths(args, working_dir=working_dir)
    try:
        address_arg = args.get("material_microbackend_addr")
        if address_arg is not None:
            HTTPConfig.material_microbackend_addr = str(address_arg).strip()
        external_address = str(HTTPConfig.material_microbackend_addr or "").strip()

        if external_address:
            materials_gateway = HTTPMaterialsClient(external_address)
            material_authority = external_address
        else:
            materials_service = setup_materials_service(
                database_paths=paths,
            )
            materials_gateway = LocalMaterialsClient(materials_service)
            material_authority = str(paths.materials_db)

        template_report = sync_registry_resources(registry, materials_gateway)
        set_materials_gateway(materials_gateway)

        execution_backend = setup_execution_backend(
            control_client=communication_client,
            database_paths=paths,
            materials_gateway=materials_gateway,
        )
        # 调度权威归属：没有显式 --address 时调度权威随本进程装配（Scheduler +
        # Workflow API）；配置了 --address 的 Host 只提供执行面，调度在 Backend 地址上。
        from unilabos.server.backend.legacy_adaptor.url import build_backend_websocket_url

        if not build_backend_websocket_url():
            from unilabos.server.backend.composition import (
                setup_local_scheduler,
            )

            setup_local_scheduler(backend=execution_backend)

        host_network = None
        if args.get("backend") == "ros2":
            from unilabos.backend.hostlink.network import setup_host_network_service

            host_network = setup_host_network_service(
                material_gateway=materials_gateway
            )

        return HostServerStack(
            database_paths=paths,
            materials_gateway=materials_gateway,
            execution_backend=execution_backend,
            material_authority=material_authority,
            template_count=template_report.resource_count,
            host_network=host_network,
        )
    except BaseException:
        shutdown_backend_services()
        raise


__all__ = [
    "HostServerStack",
    "resolve_database_paths",
    "setup_host_server_stack",
]
