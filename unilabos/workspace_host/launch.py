"""Resolve reproducible launch plans for Workspace Host components."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from unilabos.app.center_client.console_address import local_browser_origin

from .model import WorkspaceHostError, WorkspacePaths, atomic_write_json


@dataclass(frozen=True)
class LaunchPlan:
    component: str
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    generation: str
    log_path: Path
    address: str | None
    ready_url: str | None
    metadata: dict[str, object]


def available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def load_environment_configuration(paths: WorkspacePaths) -> dict[str, object]:
    try:
        payload = json.loads(paths.environment.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceHostError(
            "environment_invalid",
            f"本地环境配置无效：{paths.environment}",
        ) from error
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise WorkspaceHostError(
            "environment_invalid",
            f"本地环境配置 schemaVersion 无效：{paths.environment}",
        )
    return payload


def resolve_backend_launch(
    paths: WorkspacePaths,
    *,
    graph_path: str | None = None,
    runtime_mode: str | None = None,
    backend_port: int | None = None,
) -> LaunchPlan:
    """解析本地 Scheduler/HTTP 进程的可复现启动计划。

    参数：``paths`` 是工作区路径；其余参数分别是图路径、执行模式和端口。
    返回：包含命令、环境、身份代次与配置元数据的本地进程启动计划。异常：工作区
    文件、端口、模式或设备包范围无效时抛出 ``WorkspaceHostError``；配置中的
    ``domainMode=backend`` 会立即失败，不会发出远端请求。状态不变量：该计划的
    控制面始终是本站 OS，``backendUrl`` 不参与地址选择。
    """

    config = load_environment_configuration(paths)
    graph = resolve_workspace_graph(paths, graph_path=graph_path, configuration=config)
    local_config = _workspace_file(
        paths,
        "deployment/local_config.py",
        code="config_not_found",
    )
    mode = runtime_mode or _optional_text(config.get("runtimeMode")) or "normal"
    if mode in {"simulation", "simulate"}:
        mode = "dry-run"
    if mode not in {"normal", "dry-run"}:
        raise WorkspaceHostError("runtime_mode_invalid", f"无效运行模式：{mode}")
    domain_mode = _optional_text(config.get("domainMode")) or "local"
    if domain_mode != "local":
        raise WorkspaceHostError(
            "backend_mode_removed",
            "当前 OS 只支持 local 控制面，不再支持 backend 模式",
        )
    external_devices_only = config.get("externalDevicesOnly", True)
    if not isinstance(external_devices_only, bool):
        raise WorkspaceHostError(
            "environment_invalid", "externalDevicesOnly 必须是布尔值"
        )
    generation = str(uuid.uuid4())
    runtime_directory = paths.runtime / "backend" / generation
    runtime_directory.mkdir(parents=True, exist_ok=False)
    # 工站调度状态固定由本站拥有，跨进程启动只复用同一份本地状态。
    state_directory = paths.runtime / "backend" / "local-domain"
    state_directory.mkdir(parents=True, exist_ok=True)
    legacy_state = (
        _migrate_legacy_backend_state(paths, state_directory)
        if domain_mode == "local"
        else None
    )
    # 资源图文件名是本地库存启动来源的稳定业务身份；运行代次只能改变目录，
    # 不能把它重命名为 selected-graph.json，否则既有库存会正确拒绝接管。
    validated_graph = runtime_directory / graph.name
    shutil.copyfile(graph, validated_graph)
    os.chmod(validated_graph, 0o600)
    backend_port = _configured_service_port(
        backend_port,
        field="backend_port",
    ) or available_loopback_port()
    environment = _runtime_environment(paths, generation)
    edge_token = _workspace_host_token(paths)
    edge_key = _workspace_edge_key(paths)
    backend_address = local_browser_origin(environment.get("UNILABOS_DESKTOP_WEB_HOST", "127.0.0.1"), backend_port)
    upstream_backend_address = backend_address
    upstream_backend_token = edge_token
    environment.update(
        {
            "UNILABOS_EDGECONTROLCONFIG_API_KEY": edge_token,
            "UNILABOS_EDGECONTROLCONFIG_BACKEND_API_KEY": upstream_backend_token,
            "UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR": upstream_backend_address,
            "UNILABOS_EDGECONTROLCONFIG_EDGE_KEY": edge_key,
            "UNILABOS_EDGECONTROLCONFIG_SCHEDULER_ADDR": backend_address,
            "UNILABOS_WORKBENCH_RUNTIME_MODE": mode,
            "UNILABOS_WORKBENCH_GRAPH_FINGERPRINT": _sha256(graph),
            "ROS_DOMAIN_ID": str(2 + (uuid.uuid4().int % 98)),
        }
    )
    command = (
        sys.executable,
        "-m",
        "unilabos.app.main",
        "--workspace",
        str(paths.workspace),
        "--graph",
        str(validated_graph),
        "--config",
        str(local_config),
        "--working_dir",
        str(state_directory),
        "--preserve_runtime_databases",
        "--process_role",
        "workspace_backend",
        "--control_plane",
        domain_mode,
        "--backend",
        "ros",
        "--app_bridges",
        "fastapi",
        "--port",
        str(backend_port),
        "--disable_browser",
        "--action_mode",
        "real" if mode == "normal" else "simulate",
        *(("--external_devices_only",) if external_devices_only else ()),
        "--ros_discovery_server",
        "off",
    )
    return LaunchPlan(
        component="backend",
        command=command,
        cwd=paths.workspace,
        environment=environment,
        generation=generation,
        log_path=paths.logs / f"{generation}-backend.log",
        address=backend_address,
        ready_url=f"{backend_address}/api/v1/readiness",
        metadata={
            "graphPath": str(graph),
            "graphFingerprint": _sha256(graph),
            "runtimeMode": mode,
            "databaseType": environment["UNILABOS_BASICCONFIG_DATABASE_TYPE"],
            "externalDevicesOnly": external_devices_only,
            "domainMode": domain_mode,
            "backendUrl": None,
            "schedulerUrl": _optional_text(config.get("schedulerUrl")),
            "runtimeDirectory": str(runtime_directory),
            "stateDirectory": str(state_directory),
            **(
                {"legacyStateMigratedFrom": legacy_state}
                if legacy_state
                else {}
            ),
            "validatedGraphPath": str(validated_graph),
            "localConfigPath": str(local_config),
        },
    )


def resolve_edge_launch(
    paths: WorkspacePaths, backend: dict[str, object]
) -> LaunchPlan:
    """解析连接本站 Scheduler 的 Edge 启动计划。

    参数：``paths`` 是工作区路径，``backend`` 是本站 Scheduler 的地址和元数据。
    返回：与本站控制面共享设备包范围的 Edge 启动计划。异常：元数据、地址或设备
    包范围无效时抛出 ``WorkspaceHostError``；遇到 ``domainMode=backend`` 立即
    失败。状态不变量：Edge 只连接同一工作区的本地 Scheduler。
    """

    metadata = backend.get("metadata")
    if not isinstance(metadata, dict):
        raise WorkspaceHostError("backend_not_ready", "Backend 缺少启动元数据")
    generation = str(uuid.uuid4())
    runtime_directory = paths.runtime / "edge" / generation
    runtime_directory.mkdir(parents=True, exist_ok=False)
    ready_file = runtime_directory / "ready.json"
    mode = str(metadata.get("runtimeMode") or "normal")
    external_devices_only = metadata.get("externalDevicesOnly", True)
    if not isinstance(external_devices_only, bool):
        raise WorkspaceHostError(
            "backend_not_ready", "Backend 外部设备包范围元数据无效"
        )
    local_backend_address = str(backend.get("address") or "").strip()
    if not local_backend_address:
        raise WorkspaceHostError("backend_not_ready", "Backend 缺少服务地址")
    domain_mode = str(metadata.get("domainMode") or "local")
    if domain_mode != "local":
        raise WorkspaceHostError(
            "backend_mode_removed",
            "当前 OS 只支持 local 控制面，不再支持 backend 模式",
        )
    authority_address = local_backend_address
    # 动作执行进程永远连接同工作区的工站调度进程，不连接远端服务。
    scheduler_address = local_backend_address
    authority_token = _workspace_host_token(paths)
    edge_state_directory = paths.runtime / "edge"
    edge_state_directory.mkdir(parents=True, exist_ok=True)
    # 命令序列和待提交结果始终属于本地工站调度权威，两个进程复用同一账本。
    state_db = edge_state_directory / "edge_control.db"
    local_scheduler_token = _workspace_host_token(paths)
    environment = _runtime_environment(paths, generation)
    environment.update(
        {
            "UNILABOS_EDGECONTROLCONFIG_API_KEY": local_scheduler_token,
            "UNILABOS_EDGECONTROLCONFIG_BACKEND_API_KEY": authority_token,
            "UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR": authority_address,
            "UNILABOS_EDGECONTROLCONFIG_EDGE_KEY": _workspace_edge_key(paths),
            "UNILABOS_EDGECONTROLCONFIG_SCHEDULER_ADDR": scheduler_address,
            "UNILABOS_EDGECONTROLCONFIG_STATE_DB": str(state_db),
            "UNILABOS_WORKBENCH_RUNTIME_MODE": mode,
            "UNILABOS_WORKBENCH_PROCESS_ROLE": "edge_runtime",
            "UNILABOS_EDGE_READY_FILE": str(ready_file),
            # Isolate DDS discovery per Workspace.  Leaving Edge on domain 0
            # makes HostNode discover devices from unrelated laboratories
            # running on the same machine and then attempt to register their
            # barcodes with this Backend.
            "ROS_DOMAIN_ID": str(
                2
                + int.from_bytes(
                    hashlib.sha256(str(paths.workspace).encode("utf-8")).digest()[:4],
                    "big",
                )
                % 98
            ),
        }
    )
    if "stationStartSignature" in metadata:
        from .station_config import resolve_ros_domain

        domain = resolve_ros_domain(metadata.get("runtimeRosDomainId", metadata.get("stationRosDomainId")))
        if domain is not None:
            environment["ROS_DOMAIN_ID"] = str(domain)
    edge_graph = Path(str(metadata["validatedGraphPath"]))
    if mode == "dry-run":
        # Edge 使用独立副本关闭自动连接，但保留与 Backend 相同的来源文件名。
        edge_graph = runtime_directory / edge_graph.name
        _write_dry_run_edge_graph(
            Path(str(metadata["validatedGraphPath"])), edge_graph
        )
    command = (
        sys.executable,
        "-m",
        "unilabos.app.main",
        "--workspace",
        str(paths.workspace),
        "--graph",
        str(edge_graph),
        "--config",
        str(metadata["localConfigPath"]),
        "--working_dir",
        str(runtime_directory),
        "--process_role",
        "edge_runtime",
        "--control_plane",
        domain_mode,
        "--backend",
        "ros",
        "--app_bridges",
        "edge_control",
        "--port",
        "0",
        "--disable_browser",
        "--action_mode",
        "real" if mode == "normal" else "simulate",
        *(("--external_devices_only",) if external_devices_only else ()),
        "--ros_discovery_server",
        "off",
    )
    return LaunchPlan(
        component="edge",
        command=command,
        cwd=paths.workspace,
        environment=environment,
        generation=generation,
        log_path=paths.logs / f"{generation}-edge.log",
        address=None,
        ready_url=None,
        metadata={
            "graphPath": metadata["graphPath"],
            "runtimeMode": mode,
            "databaseType": environment["UNILABOS_BASICCONFIG_DATABASE_TYPE"],
            "externalDevicesOnly": external_devices_only,
            "domainMode": domain_mode,
            "authorityAddress": authority_address,
            "schedulerAddress": scheduler_address,
            "protocolStatePath": str(state_db),
            "runtimeDirectory": str(runtime_directory),
            "readyFilePath": str(ready_file),
        },
    )


def _write_dry_run_edge_graph(source: Path, target: Path) -> None:
    """Detach dry-run Edge drivers from real endpoints without changing authority data."""

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceHostError(
            "graph_invalid", "无法生成 Dry-run Edge 设备图"
        ) from error
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            config = node.get("config")
            if isinstance(config, dict) and "auto_connect" in config:
                config["auto_connect"] = False
    atomic_write_json(target, payload)
    os.chmod(target, 0o600)


def resolve_plc_launch(paths: WorkspacePaths) -> LaunchPlan:
    """Resolve PLC-Sim plus its variable-table and handshake configuration."""

    config = load_environment_configuration(paths)
    project_value = _optional_text(config.get("plcSimulatorProjectPath"))
    table_value = _optional_text(config.get("plcVariableTablePath"))
    profile = _optional_text(config.get("plcHandshakeProfile"))
    workflow = _optional_text(config.get("plcHandshakeWorkflow")) or "all"
    if not project_value:
        raise WorkspaceHostError("plc_configuration_missing", "未配置 PLC-Sim 项目目录")
    if not table_value:
        raise WorkspaceHostError("plc_configuration_missing", "未配置 PLC-Sim 变量表")
    if not profile:
        raise WorkspaceHostError("plc_configuration_missing", "未配置领域包的 PLC 握手器名称")
    project = Path(project_value).expanduser().resolve()
    working_directory = next(
        (
            candidate
            for candidate in (project / "OpcUaSim", project)
            if (candidate / "gui" / "backend.py").is_file()
        ),
        None,
    )
    if working_directory is None:
        raise WorkspaceHostError("plc_project_invalid", f"PLC-Sim 项目无效：{project}")
    table = Path(table_value).expanduser()
    if not table.is_absolute():
        table = paths.workspace / table
    table = table.resolve()
    if not table.is_file():
        raise WorkspaceHostError("plc_table_invalid", f"PLC-Sim 变量表不存在：{table}")
    generation = str(uuid.uuid4())
    runtime_directory = paths.runtime / "plc" / generation
    runtime_directory.mkdir(parents=True, exist_ok=False)
    # The device graph addresses the PLC simulator by its laboratory contract,
    # so unlike the Host and Backend control ports these ports are stable.
    gui_port = _configured_port(config.get("plcSimulatorGuiPort"), 18_765)
    opcua_port = _configured_port(config.get("plcSimulatorOpcUaPort"), 4_855)
    return LaunchPlan(
        component="plc",
        command=(
            sys.executable,
            "-m",
            "gui.backend",
            "--host",
            "127.0.0.1",
            "--port",
            str(gui_port),
        ),
        cwd=working_directory,
        environment=_runtime_environment(paths, generation),
        generation=generation,
        log_path=paths.logs / f"{generation}-plc.log",
        address=f"http://127.0.0.1:{gui_port}",
        ready_url=f"http://127.0.0.1:{gui_port}/api/state",
        metadata={
            "projectPath": str(project),
            "variableTablePath": str(table),
            "handshakeProfile": profile,
            "handshakeWorkflow": workflow,
            "guiUrl": f"http://127.0.0.1:{gui_port}",
            "opcUaUrl": f"opc.tcp://127.0.0.1:{opcua_port}",
            "opcUaPort": opcua_port,
            "runtimeDirectory": str(runtime_directory),
        },
    )


def _runtime_environment(paths: WorkspacePaths, generation: str) -> dict[str, str]:
    from unilabos.persistence.workspace import database_environment

    environment = _with_conda_ros_environment(dict(os.environ))
    environment.update(database_environment(paths.workspace))
    checkout = Path(__file__).resolve().parents[2]
    imports = [str(checkout), str(paths.workspace)]
    inherited = environment.get("PYTHONPATH")
    if inherited:
        imports.append(inherited)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(imports),
            "PYTHONUNBUFFERED": "1",
            "UNILABOS_OBSERVABILITYCONFIG_ENABLED": "true",
            "UNILABOS_OBSERVABILITYCONFIG_PROJECT_NAME": "uni-lab-workbench",
            "UNILABOS_WORKBENCH_GENERATION": generation,
            "UNILABOS_WORKBENCH_WORKSPACE": str(paths.workspace),
        }
    )
    return environment


def _with_conda_ros_environment(
    environment: dict[str, str],
    *,
    platform: str = sys.platform,
    prefix: Path | None = None,
    executable: str | None = None,
) -> dict[str, str]:
    """Restore RoboStack activation variables for detached Windows children.

    Workbench starts its Workspace Host with the selected environment's Python
    executable, but a GUI process has no activated Conda shell.  RoboStack's
    ``rclpy`` then imports successfully while RMW initialization fails because
    ``AMENT_PREFIX_PATH`` was never populated.  Reconstruct the stable subset
    of the environment's activation contract directly from that interpreter.
    Explicit caller overrides remain authoritative.
    """

    if platform != "win32":
        return environment
    conda_prefix = (prefix or Path(sys.prefix)).resolve()
    library = conda_prefix / "Library"
    if not (conda_prefix / "conda-meta").is_dir():
        return environment
    if not (library / "local_setup.bat").is_file():
        return environment

    python_executable = executable or sys.executable
    environment.setdefault("CONDA_PREFIX", str(conda_prefix))
    environment.setdefault("CONDA_DEFAULT_ENV", conda_prefix.name)
    environment.setdefault("AMENT_PREFIX_PATH", str(library))
    environment.setdefault("AMENT_PYTHON_EXECUTABLE", python_executable)
    environment.setdefault("QT_PLUGIN_PATH", str(library / "plugins"))
    environment.setdefault("ROS_DISTRO", "humble")
    environment.setdefault("ROS_ETC_DIR", str(library / "etc" / "ros"))
    environment.setdefault("ROS_LOCALHOST_ONLY", "0")
    environment.setdefault("ROS_OS_OVERRIDE", "conda:win64")
    environment.setdefault("ROS_PYTHON_VERSION", str(sys.version_info.major))
    environment.setdefault("ROS_VERSION", "2")
    environment["PYTHONHOME"] = ""
    # Workspace Host is commonly launched from Electron rather than an
    # activated shell; enable conda-forge Python's Windows DLL search hook so
    # rclpy and rosidl native extensions resolve their Library/bin DLLs.
    environment["CONDA_DLL_SEARCH_MODIFICATION_ENABLE"] = "1"

    existing_path = environment.get("PATH", "")
    path_entries = [
        library / "bin",
        conda_prefix,
        library / "mingw-w64" / "bin",
        library / "usr" / "bin",
        conda_prefix / "Scripts",
        conda_prefix / "bin",
    ]
    merged = [str(entry) for entry in path_entries]
    merged.extend(part for part in existing_path.split(os.pathsep) if part)
    seen: set[str] = set()
    preserved: list[str] = []
    for part in merged:
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        preserved.append(part)
    environment["PATH"] = os.pathsep.join(preserved)
    return environment


def resolve_workspace_graph(
    paths: WorkspacePaths,
    *,
    graph_path: str | None = None,
    configuration: dict[str, object] | None = None,
) -> Path:
    """按显式参数、本地环境、设备包声明的优先级解析图，不猜测设备包文件名。"""

    config = (
        load_environment_configuration(paths) if configuration is None else configuration
    )
    selected = graph_path or _optional_text(config.get("graphPath"))
    if not selected:
        from unilabos.package_manager.package_catalog.project_metadata import (
            parse_project_metadata,
        )

        project_path = paths.workspace / "pyproject.toml"
        try:
            raw_project = project_path.read_bytes()
        except FileNotFoundError:
            raw_project = None
        except OSError as error:
            raise WorkspaceHostError(
                "workspace_project_invalid", f"无法读取设备包声明：{project_path}"
            ) from error
        if raw_project is not None:
            try:
                selected = parse_project_metadata(raw_project).startup_graph
            except (ValueError, TypeError) as error:
                raise WorkspaceHostError(
                    "workspace_project_invalid", f"设备包声明无效：{error}"
                ) from error
    if not selected:
        raise WorkspaceHostError(
            "graph_not_configured",
            "未指定启动图，请使用 --graph 或在 pyproject.toml 的 "
            "[tool.unilabos.startup] 中配置 graph",
        )
    return _workspace_file(paths, selected, code="graph_not_found")


def _workspace_file(paths: WorkspacePaths, value: str, *, code: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = paths.workspace / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(paths.workspace)
    except ValueError as error:
        raise WorkspaceHostError(code, f"路径越出 Workspace：{candidate}") from error
    if not candidate.is_file():
        raise WorkspaceHostError(code, f"文件不存在：{candidate}")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_host_token(paths: WorkspacePaths) -> str:
    try:
        token = paths.token.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise WorkspaceHostError(
            "host_token_invalid", "Workspace Host token 不可读"
        ) from error
    if not token:
        raise WorkspaceHostError(
            "host_token_invalid", "Workspace Host token 为空"
        )
    return token


def _migrate_legacy_backend_state(
    paths: WorkspacePaths,
    state_directory: Path,
) -> str | None:
    """Move closed generation-local SQLite facts into the stable Local Domain."""

    from unilabos.persistence.workspace import migrate_sqlite_backup, workspace_settings

    if workspace_settings(paths.workspace).database_type == "postgresql":
        return None
    try:
        session = json.loads(paths.session.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    components = session.get("components") if isinstance(session, dict) else None
    backend = components.get("backend") if isinstance(components, dict) else None
    metadata = backend.get("metadata") if isinstance(backend, dict) else None
    value = metadata.get("runtimeDirectory") if isinstance(metadata, dict) else None
    if not isinstance(value, str) or not value:
        return None
    legacy_directory = Path(value).expanduser().resolve()
    backend_root = (paths.runtime / "backend").resolve()
    try:
        legacy_directory.relative_to(backend_root)
    except ValueError:
        return None
    if legacy_directory == state_directory.resolve():
        return None
    migrated = False
    for database_name in (
        "inventory.db",
        "device_state.db",
        "workflow_history.db",
        "edge_authority.db",
    ):
        source = legacy_directory / database_name
        destination = state_directory / database_name
        if destination.exists() or not source.is_file():
            continue
        try:
            migrate_sqlite_backup(source, destination)
        except (OSError, sqlite3.Error) as error:
            raise WorkspaceHostError(
                "backend_state_migration_failed",
                f"迁移旧 Local Domain 数据失败：{source}：{error}",
            ) from error
        migrated = True
    return str(legacy_directory) if migrated else None


def _workspace_edge_key(paths: WorkspacePaths) -> str:
    return "managed-local-" + hashlib.sha256(
        str(paths.workspace).encode("utf-8")
    ).hexdigest()[:24]


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _configured_service_port(value: object, *, field: str) -> int | None:
    """校验容器部署显式指定的业务服务端口。

    参数：``value`` 是待校验端口或 ``None``，``field`` 是稳定错误信息中的配置
    字段名。返回：合法的 1 至 65535 整数；未配置时返回 ``None``，由调用方动态
    分配。异常：布尔值、非整数或超出端口范围时抛 ``WorkspaceHostError``。
    """

    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= 65_535
    ):
        raise WorkspaceHostError(
            "port_configuration_invalid",
            f"{field} 必须是 1 到 65535 的整数",
        )
    return value


def _configured_port(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 65_535:
        raise WorkspaceHostError(
            "plc_configuration_invalid", f"无效 PLC-Sim 端口：{value}"
        )
    return value
