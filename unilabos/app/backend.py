"""Backend 配置档与运行时分发。

公开名称直接说明使用的通信协议，不跟内部运行时包名绑定：

``hostlink``
    本地 Python 驱动 + HostLink TCP 的分布式运行时；不启动 rclpy/DDS，
    但可以加载 ROS message 包用于字段解析和 JSON 转换。
``ros2``
    完整 ROS 2 运行时。

HostLink 的本地驱动执行器也位于 ``unilabos.hostlink``，不是第三种 backend；Dora
代码保留用于实验，也不进入公开选择。CLI、运行时、测试和文档共享本模块中的
公开 backend 清单，并确保可选 backend 只在被选中时导入。
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

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
    supports_slave: bool
    supports_visualization: bool


@dataclass(frozen=True)
class BackendSelection:
    """完成规范化和校验的启动选择。"""

    profile: BackendProfile

    @property
    def name(self) -> str:
        return self.profile.name


BACKEND_PROFILES: dict[str, BackendProfile] = {
    "hostlink": BackendProfile(
        name="hostlink",
        display_name="HostLink",
        module="unilabos.hostlink.main_hostlink_run",
        description="HostLink TCP 分布式 Python 驱动运行时（不启动 rclpy/DDS）",
        supports_slave=True,
        supports_visualization=False,
    ),
    "ros2": BackendProfile(
        name="ros2",
        display_name="ROS 2",
        module="unilabos.ros.main_slave_run",
        description="ROS 2 分布式设备运行时",
        supports_slave=True,
        supports_visualization=True,
    ),
}

BACKEND_NAMES: tuple[str, ...] = tuple(BACKEND_PROFILES)

DEFAULT_PYTHON_DRIVER_BACKENDS = ("hostlink", "ros2")

_REMOVED_BACKENDS: dict[str, str] = {
    "automancer": "automancer 从未实现，现已移除",
    "basic": "basic backend 已移除，请使用 backend 'hostlink'",
    "simple": "simple/basic backend 已移除，请使用 backend 'hostlink'",
    "dora": "dora 是实验运行时，不是公开 backend",
    "ros": "ros 旧别名已移除，请使用 backend 'ros2'",
}


def normalize_backend_name(value: str) -> str:
    """校验并返回公开 backend 名称。"""

    name = str(value or "").strip().lower()
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
    *,
    is_slave: bool = False,
    visual: str = "disable",
) -> BackendSelection:
    """规范化 backend 选择，并拒绝不支持的组合。"""

    name = normalize_backend_name(backend)
    profile = BACKEND_PROFILES[name]
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
    return BackendSelection(profile=profile)


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
