"""Backend 配置档与运行时分发。

公开名称直接说明使用的通信方式和运行模式，不跟内部包名绑定：

``basic``
    不使用通信中间件的进程内 Python 驱动运行时。
``hostlink``
    Basic 驱动 + HostLink TCP 的 Python 分布式运行时；不启动 rclpy/DDS，
    但可以加载 ROS message 包用于字段解析和 JSON 转换。
``ros2``
    完整 ROS 2 运行时。
``dora``
    dora-rs 数据流运行时。

旧 CLI 值 ``simple`` 和 ``ros`` 作为兼容别名继续接受。所有映射集中在本模块，
使 CLI、运行时、测试和文档共享同一份事实，并确保可选 backend 只在被选中时导入。
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional

from unilabos.utils import logger

if TYPE_CHECKING:
    from unilabos.resources.resource_tracker import ResourceTreeSet


class BackendConfigurationError(ValueError):
    """Backend 名称或参数组合不受支持。"""


@dataclass(frozen=True)
class BackendProfile:
    """一个可选 backend 的静态元数据。"""

    name: str
    display_name: str
    module: str
    description: str
    default_app_bridges: tuple[str, ...]
    supported_app_bridges: tuple[str, ...]
    supports_slave: bool
    supports_visualization: bool


@dataclass(frozen=True)
class BackendSelection:
    """完成规范化和校验的启动选择。"""

    profile: BackendProfile
    app_bridges: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.profile.name


BACKEND_PROFILES: dict[str, BackendProfile] = {
    "basic": BackendProfile(
        name="basic",
        display_name="Basic",
        module="unilabos.basic.main_basic_run",
        description="无中间件的单进程 Python 驱动运行时",
        default_app_bridges=(),
        supported_app_bridges=(),
        supports_slave=False,
        supports_visualization=False,
    ),
    "hostlink": BackendProfile(
        name="hostlink",
        display_name="HostLink",
        module="unilabos.hostlink.main_hostlink_run",
        description="HostLink TCP 分布式 Python 驱动运行时（不启动 rclpy/DDS）",
        default_app_bridges=(),
        supported_app_bridges=(),
        supports_slave=True,
        supports_visualization=False,
    ),
    "ros2": BackendProfile(
        name="ros2",
        display_name="ROS 2",
        module="unilabos.ros.main_slave_run",
        description="ROS 2 分布式设备运行时",
        default_app_bridges=("websocket", "fastapi"),
        supported_app_bridges=("websocket", "fastapi"),
        supports_slave=True,
        supports_visualization=True,
    ),
    "dora": BackendProfile(
        name="dora",
        display_name="Dora",
        module="unilabos.dora.main_dora_run",
        description="dora-rs 数据流运行时",
        default_app_bridges=(),
        supported_app_bridges=(),
        supports_slave=False,
        supports_visualization=False,
    ),
}

BACKEND_NAMES: tuple[str, ...] = tuple(BACKEND_PROFILES)
BACKEND_ALIASES: dict[str, str] = {
    "simple": "basic",
    "ros": "ros2",
}

DEFAULT_PYTHON_DRIVER_BACKENDS = ("basic", "hostlink", "ros2")

_REMOVED_BACKENDS: dict[str, str] = {
    "automancer": "automancer 从未实现，现已移除",
}


def normalize_backend_name(value: str) -> str:
    """返回规范 backend 名称，并接受已登记的旧别名。"""

    name = str(value or "").strip().lower()
    if name in BACKEND_ALIASES:
        canonical = BACKEND_ALIASES[name]
        logger.warning(
            "Backend 名称 '%s' 已弃用，请改用 '%s'。",
            name,
            canonical,
        )
        return canonical
    if name in _REMOVED_BACKENDS:
        raise BackendConfigurationError(_REMOVED_BACKENDS[name])
    if name not in BACKEND_PROFILES:
        supported = ", ".join(BACKEND_NAMES)
        raise BackendConfigurationError(
            f"不支持 backend {value!r}；请选择：{supported}"
        )
    return name


def backend_cli_value(value: str) -> str:
    """供公开 CLI 使用的 ``argparse`` 类型适配器。"""

    return normalize_backend_name(value)


def resolve_driver_backends(class_config: dict[str, Any]) -> tuple[str, ...]:
    """Return backends a registry driver declares it can run on."""

    configured = class_config.get("supported_backends")
    if configured is None:
        if class_config.get("type") == "ros2":
            return ("ros2",)
        return DEFAULT_PYTHON_DRIVER_BACKENDS
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, (list, tuple)):
        raise BackendConfigurationError(
            "class.supported_backends 必须是 backend 名称列表"
        )
    result = tuple(
        dict.fromkeys(str(item).strip().lower() for item in configured if str(item).strip())
    )
    invalid = sorted(set(result) - set(BACKEND_NAMES))
    if invalid:
        raise BackendConfigurationError(
            f"class.supported_backends 包含未知 backend：{', '.join(invalid)}"
        )
    if not result:
        raise BackendConfigurationError("class.supported_backends 不能为空")
    return result


def resolve_backend_selection(
    backend: str,
    app_bridges: Optional[Iterable[str]] = None,
    *,
    is_slave: bool = False,
    visual: str = "disable",
) -> BackendSelection:
    """规范化 backend 选择，并拒绝不支持的组合。

    ``None`` 表示用户未指定 ``--app_bridges``，使用该 backend 的默认值；
    显式空序列表示关闭全部应用桥。
    """

    name = normalize_backend_name(backend)
    profile = BACKEND_PROFILES[name]
    bridges = (
        profile.default_app_bridges
        if app_bridges is None
        else tuple(dict.fromkeys(str(item).strip().lower() for item in app_bridges))
    )
    bridges = tuple(item for item in bridges if item)
    unsupported_bridges = sorted(set(bridges) - set(profile.supported_app_bridges))
    if unsupported_bridges:
        unsupported = ", ".join(unsupported_bridges)
        supported = ", ".join(profile.supported_app_bridges) or "无"
        raise BackendConfigurationError(
            f"backend '{name}' 不支持应用桥：{unsupported}；支持项：{supported}"
        )
    if is_slave and not profile.supports_slave:
        raise BackendConfigurationError(
            f"backend '{name}' 不支持 --is_slave；"
            "请使用 backend 'hostlink' 或 'ros2'"
        )
    if visual != "disable" and not profile.supports_visualization:
        raise BackendConfigurationError(
            f"backend '{name}' 不支持 --visual {visual}；"
            "请使用 --visual disable 或 backend 'ros2'"
        )
    return BackendSelection(profile=profile, app_bridges=bridges)


def _load_entrypoint(profile: BackendProfile, is_slave: bool) -> Callable[..., None]:
    """只导入选中的 backend，并返回已校验的入口。"""

    try:
        module = importlib.import_module(profile.module)
    except Exception as exc:
        raise RuntimeError(
            f"backend '{profile.name}' 不可用：导入 {profile.module} 失败：{exc}"
        ) from exc

    validate_environment = getattr(module, "validate_environment", None)
    if callable(validate_environment):
        validate_environment()

    entrypoint_name = "slave" if is_slave else "main"
    entrypoint = getattr(module, entrypoint_name, None)
    if not callable(entrypoint):
        raise RuntimeError(
            f"backend '{profile.name}' 没有可调用的 {entrypoint_name} 入口"
        )
    return entrypoint


def start_backend(
    backend: str,
    devices_config: "ResourceTreeSet",
    resources_config: "ResourceTreeSet",
    resources_edge_config: Optional[list[dict[str, Any]]] = None,
    graph: Any = None,
    controllers_config: Optional[dict[str, Any]] = None,
    bridges: Optional[list[Any]] = None,
    is_slave: bool = False,
    visual: str = "disable",
    resources_mesh_config: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> threading.Thread:
    """在守护线程中启动选中的 backend，并返回该线程。"""

    name = normalize_backend_name(backend)
    profile = BACKEND_PROFILES[name]
    if is_slave and not profile.supports_slave:
        raise BackendConfigurationError(
            f"backend '{name}' 不支持 --is_slave；"
            "请使用 backend 'hostlink' 或 'ros2'"
        )
    entrypoint = _load_entrypoint(profile, is_slave)

    backend_thread = threading.Thread(
        target=entrypoint,
        args=(
            devices_config,
            resources_config,
            resources_edge_config or [],
            graph,
            controllers_config or {},
            bridges or [],
            visual,
            resources_mesh_config or {},
        ),
        name=f"backend-{name}",
        daemon=True,
    )
    backend_thread.start()
    logger.info("Backend %s（%s）已启动。", name, profile.display_name)
    return backend_thread
