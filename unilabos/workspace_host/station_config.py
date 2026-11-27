"""工站启动文件。只在启动操作中读入，设备图、交接策略及证书仍由各自的模块解释。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from yaml.constructor import ConstructorError

from .model import WorkspaceHostError, WorkspacePaths

if TYPE_CHECKING:
    from .launch import LaunchPlan


class _UniqueLoader(yaml.SafeLoader):
    """避免重复字段静默覆盖真机模式、地址或凭据引用。"""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key, _ in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.value in seen:
                raise ConstructorError("启动配置的字段必须是唯一标量")
            seen.add(key.value)
        return super().construct_mapping(node, deep)


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_min_length=1)


class CenterSettings(_Config):
    enabled: bool | None = None
    inventoryEnabled: bool | None = None
    endpoint: str | None = None
    stationUuid: str | None = None
    instanceUuid: str | None = None
    labUuid: str | None = None
    httpBaseUrl: str | None = None
    publicHttpBaseUrl: str | None = None
    clientCert: str | None = None
    clientKey: str | None = None
    serverCa: str | None = None
    machineSecretFile: str | None = None
    handoffPolicyFile: str | None = None
    transportRouteFile: str | None = None


class StationSettings(_Config):
    schemaVersion: int = Field(ge=1, le=1)
    workspace: str = "."
    graphPath: str | None = None
    runtimeMode: Literal["normal", "dry-run"] | None = None
    backendPort: int | None = Field(default=None, ge=1, le=65535)
    rosDomainId: int | None = Field(default=None, ge=0, le=232)
    runtimeRosDomainId: int | None = Field(default=None, ge=0, le=232)
    center: CenterSettings = Field(default_factory=CenterSettings)


_CENTER_VALUES = {
    "endpoint": "CENTER_ENDPOINT",
    "stationUuid": "CENTER_STATION_UUID",
    "instanceUuid": "CENTER_INSTANCE_UUID",
    "labUuid": "CENTER_LAB_UUID",
    "httpBaseUrl": "CENTER_HTTP_BASE_URL",
    "publicHttpBaseUrl": "CENTER_PUBLIC_HTTP_BASE_URL",
}
_CENTER_PATHS = {
    "clientCert": "CENTER_CLIENT_CERT",
    "clientKey": "CENTER_CLIENT_KEY",
    "serverCa": "CENTER_SERVER_CA",
    "handoffPolicyFile": "CENTER_HANDOFF_POLICY_FILE",
    "transportRouteFile": "CENTER_TRANSPORT_ROUTE_FILE",
}


@dataclass(frozen=True)
class StationFile:
    path: Path
    digest: str
    settings: StationSettings

    @classmethod
    def load(cls, path: str) -> StationFile:
        # 保留投影挂载路径；ConfigMap/Secret 更新后符号链接指向新的文件。
        source = Path(path).expanduser().absolute()
        try:
            raw = source.read_bytes()
            settings = StationSettings.model_validate(yaml.load(raw, Loader=_UniqueLoader))
        except ValidationError as error:
            fields = ", ".join(".".join(map(str, item["loc"])) for item in error.errors())
            raise WorkspaceHostError("station_config_invalid", f"工站启动配置字段无效：{fields}") from None
        except (OSError, UnicodeError, yaml.YAMLError):
            # YAML 解析器的原始异常可能携带配置中的整行内容。
            raise WorkspaceHostError("station_config_invalid", f"无法读取工站 YAML：{source}") from None
        return cls(source, hashlib.sha256(raw).hexdigest(), settings)

    def relative(self, value: str) -> Path:
        return self.path.parent / Path(value).expanduser()

    def center_environment(self) -> dict[str, str]:
        """仅在工站调度进程启动时读取密钥，密钥不进入 Host 操作及状态文件。"""
        center = self.settings.center
        values = {env: value for key, env in _CENTER_VALUES.items() if (value := getattr(center, key)) is not None}
        if center.enabled is not None:
            values["CENTER_ENABLED"] = "1" if center.enabled else "0"
        if center.inventoryEnabled is not None:
            values["CENTER_INVENTORY_ENABLED"] = "1" if center.inventoryEnabled else "0"
        for key, env in _CENTER_PATHS.items():
            if (value := getattr(center, key)) is not None:
                values[env] = str(self.relative(value))
        # 服务环境（包括 Kubernetes 注入的 Secret）优先覆盖启动文件。
        values.update({key: os.environ[key] for key in values if key in os.environ})
        for env in _CENTER_PATHS.values():
            if env in values and not Path(values[env]).is_file():
                raise WorkspaceHostError("station_config_invalid", f"找不到 {env} 引用的文件")
        if center.machineSecretFile and "CENTER_MACHINE_SECRET" not in os.environ:
            try:
                secret = self.relative(center.machineSecretFile).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise WorkspaceHostError("station_config_invalid", "无法读取 center.machineSecretFile") from None
            if not secret:
                raise WorkspaceHostError("station_config_invalid", "center.machineSecretFile 为空")
            values["CENTER_MACHINE_SECRET"] = secret
        if (os.environ | values).get("CENTER_ENABLED") == "1":
            from unilabos.app.center_client.config import CenterClientConfig, CenterConfigError

            try:
                CenterClientConfig.from_env(os.environ | values)
            except (CenterConfigError, ValueError, OSError) as error:
                raise WorkspaceHostError("station_config_invalid", f"Center 客户端配置无效：{error}") from None
        return values


def resolve_ros_domain(default: object) -> int | None:
    """工站配置模式下，每个组件以其自身进程的显式 ROS 域为准。"""
    raw = os.environ.get("ROS_DOMAIN_ID")
    if raw is not None:
        digits = raw.lstrip("0") or "0"
        if not (raw.isascii() and raw.isdecimal() and len(digits) <= 3):
            raise WorkspaceHostError("station_config_invalid", "ROS_DOMAIN_ID 必须为 0 至 232")
        default = int(digits)
    if default is not None and (type(default) is not int or not 0 <= default <= 232):
        raise WorkspaceHostError("station_config_invalid", "ROS_DOMAIN_ID 必须为 0 至 232")
    return default


@dataclass(frozen=True)
class StationLaunch:
    file: StationFile
    graph: str
    mode: str | None
    domain: int | None
    center_env: dict[str, str] = field(repr=False)

    @classmethod
    def prepare(cls, station: StationFile, paths: WorkspacePaths, parameters: dict[str, object]) -> StationLaunch:
        """在停止旧进程前验证图、配置与凭据；仅进程环境中保留解析后的凭据。"""
        from .launch import resolve_workspace_graph

        graph_path = parameters.get("graphPath") or (
            str(station.relative(station.settings.graphPath)) if station.settings.graphPath else None
        )
        mode = parameters.get("runtimeMode") or station.settings.runtimeMode
        if mode is not None and mode not in ("normal", "dry-run"):
            raise WorkspaceHostError("station_config_invalid", "runtimeMode 取值无效")
        if graph_path is not None and not isinstance(graph_path, str):
            raise WorkspaceHostError("station_config_invalid", "graphPath 必须为路径")
        graph = resolve_workspace_graph(paths, graph_path=graph_path)
        return cls(
            station, str(graph), mode, resolve_ros_domain(station.settings.rosDomainId), station.center_environment()
        )

    def signature(self) -> str:
        """标记实际启动的文件及调用方覆写，用于拒绝运行中静默切换配置。"""
        return hashlib.sha256(repr((self.file.digest, self.graph, self.mode, self.domain)).encode()).hexdigest()

    def backend_plan(self, paths: WorkspacePaths, *, host_port: int | None = None) -> LaunchPlan:
        from .launch import resolve_backend_launch

        plan = resolve_backend_launch(
            paths,
            graph_path=self.graph,
            runtime_mode=self.mode,
            backend_port=host_port or self.file.settings.backendPort,
        )
        plan.environment.update(self.center_env)
        if self.domain is not None:
            plan.environment["ROS_DOMAIN_ID"] = str(self.domain)
            plan.metadata["stationRosDomainId"] = self.domain
        if self.file.settings.runtimeRosDomainId is not None:
            plan.metadata["runtimeRosDomainId"] = self.file.settings.runtimeRosDomainId
        plan.metadata["stationStartSignature"] = self.signature()
        return plan


def run_station_role(
    role: str,
    station: StationFile,
    workspace: str,
    plan_file: str,
    parameters: dict[str, object],
) -> None:
    """容器前台运行一个组件，调度进程发布就绪探针和 Edge 共用的启动计划。"""
    from .discovery import ensure_local_token
    from .launch import resolve_edge_launch
    from .model import atomic_write_json, read_json

    paths = WorkspacePaths.resolve(workspace)
    shared = Path(plan_file).absolute()
    if role == "scheduler":
        launch = StationLaunch.prepare(station, paths, parameters)
        ensure_local_token(paths)
        plan = launch.backend_plan(paths)
        atomic_write_json(
            shared,
            {
                "stationFileDigest": station.digest,
                "workspace": str(paths.workspace),
                "address": plan.address,
                "metadata": plan.metadata,
            },
        )
    else:
        backend = read_json(shared)
        if backend.get("stationFileDigest") != station.digest or backend.get("workspace") != str(paths.workspace):
            raise WorkspaceHostError("station_plan_mismatch", "共享启动计划与当前工站配置不匹配")
        plan = resolve_edge_launch(paths, backend)
    os.chdir(plan.cwd)
    os.execvpe(plan.command[0], plan.command, plan.environment)
