"""工作区产品启动、稳定监视与关闭式刷新组合根。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..package_catalog import PackageCatalog
from ..package_catalog.sources import WorkspaceSource
from .activation import (
    WorkspaceRegistryRuntime,
    prepare_workspace_registry_runtime,
)
from .discovery import compile_package_source
from .generation import (
    WorkspaceGenerationPublisher,
    WorkspaceInputGeneration,
    WorkspacePackageRuntime,
    WorkspaceRuntimeStatus,
)
from .monitor import (
    StableWorkspaceFileMonitor,
    StableWorkspaceGenerationMonitor,
    WorkspaceRefreshCoordinator,
)

_ATOMIC_PUBLICATION_UNAVAILABLE = ("complete_generation_atomic_publish_unavailable",)
_PRODUCT_LIFECYCLE_LOCK = threading.RLock()
_product_lifecycle: WorkspaceProductLifecycle | None = None


def _unknown_execution_state() -> tuple[str, ...]:
    """在没有持久执行投影 Adapter 时关闭自动监督重启。

    参数：无。
    返回：固定 ``execution_unknown``，表示不能证明物理执行已经停止。
    异常：无。
    """

    return ("execution_unknown",)


def _ignore_restart_request(_reasons: tuple[str, ...]) -> None:
    """为未启用监督重启的组合提供无副作用回调。

    参数：``_reasons`` 是关闭集合内的待重启原因。
    返回：无。
    异常：无。
    """


class WorkspaceGenerationChangedError(RuntimeError):
    """表示首代编译期间工作区文件身份发生变化。"""


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceProductGeneration:
    """绑定同一稳定文件输入代、预编译候选和后续监视器。"""

    candidate: WorkspaceRegistryRuntime
    input_generation: WorkspaceInputGeneration
    monitor: StableWorkspaceFileMonitor


class _RestartOnlyGenerationPublisher(WorkspaceGenerationPublisher):
    """发布产品首代，并关闭尚无跨存储原子性的热替换。"""

    def __init__(self, registry: Any) -> None:
        """保存产品唯一实时注册表（Registry）。

        参数：``registry`` 是已完成内置定义构建的产品注册表。
        返回：无。
        异常：缺少设备或资源映射时抛出 ``TypeError``。
        """

        if not hasattr(registry, "device_type_registry") or not hasattr(
            registry,
            "resource_type_registry",
        ):
            raise TypeError("产品工作区发布器需要完整实时注册表")
        self._registry = registry

    def publish_initial(self, candidate: Any) -> None:
        """原子发布预编译首代注册表并开放工作区导入根。

        参数：``candidate`` 必须是完整工作区注册表运行时。
        返回：无；发布成功后只开放作者模块导入资格，不导入未选设备。
        异常：候选类型、注册表冲突或导入根激活失败时传播异常。
        """

        if not isinstance(candidate, WorkspaceRegistryRuntime):
            raise TypeError("产品首代必须是 WorkspaceRegistryRuntime")
        candidate.publish(self._registry)
        candidate.activate_import_path()

    def hot_replace(self, previous: Any, candidate: Any) -> None:
        """拒绝缺少跨注册表、模板和源码授权原子性的产品热替换。

        参数：``previous`` 与 ``candidate`` 是旧、新完整工作区候选代。
        返回：永不返回。
        异常：固定抛出 ``RuntimeError``；正常路径应先被热发布守卫转换为
        ``pending_restart``，本方法是最后一道关闭式保护。
        """

        del previous, candidate
        raise RuntimeError("产品完整工作区代尚不支持原子热替换")


class WorkspaceProductLifecycle:
    """持有产品工作区刷新协调器的单一生命周期。"""

    def __init__(self, coordinator: WorkspaceRefreshCoordinator) -> None:
        """建立尚未启动的产品生命周期。

        参数：``coordinator`` 串行拥有运行时和稳定文件监视器。
        返回：无。
        异常：类型无效时抛出 ``TypeError``。
        """

        if not isinstance(coordinator, WorkspaceRefreshCoordinator):
            raise TypeError("产品生命周期需要 WorkspaceRefreshCoordinator")
        self._coordinator = coordinator

    def start(self) -> WorkspaceRuntimeStatus:
        """幂等发布预编译首代并启动稳定文件监视。

        参数：无。
        返回：工作区包运行时（Workspace Package Runtime）当前状态。
        异常：初始发布或监视器启动失败时传播异常。
        """

        return self._coordinator.start()

    def status(self) -> WorkspaceRuntimeStatus:
        """读取当前产品工作区生命周期状态。

        参数：无。
        返回：运行时唯一只读状态投影。
        异常：无。
        """

        return self._coordinator.status()

    def close(self) -> None:
        """幂等停止文件监视并关闭刷新入口。

        参数：无。
        返回：无。
        异常：监视线程无法确认停止时传播异常。
        """

        self._coordinator.close()


def prepare_stable_workspace_product_generation(
    arguments: dict[str, Any],
    *,
    compile_catalog: Callable[[WorkspaceSource], PackageCatalog] = (
        compile_package_source
    ),
) -> PreparedWorkspaceProductGeneration | None:
    """用编译前后相同文件身份准备产品首代。

    参数：``arguments`` 是公共命令行（CLI）参数；``compile_catalog`` 是统一静态
    包目录（PackageCatalog）编译接缝。
    返回：未启用工作区时返回 ``None``；否则返回稳定输入、恰好一次编译的候选和
    已建立同一基线的文件监视器。
    异常：编译前后任一相关文件发生变化时抛出
    ``WorkspaceGenerationChangedError``；静态编译、路径或合同错误原样传播。
    """

    workspace_argument = arguments.get("workspace")
    if workspace_argument is None:
        return None
    # ``workspace_root`` 与 ``runtime_ignored_paths`` 在完整编译前即可确定；默认
    # ``.unilabos`` 或显式工作区内运行目录都不得进入源码输入代。
    workspace_root = WorkspaceSource(workspace_argument).root
    runtime_ignored_paths = _runtime_ignored_paths(
        workspace_root,
        arguments.get("working_dir"),
    )
    # ``before_monitor`` 只建立编译前字节身份；默认物理图稍后由同次清单解析确定。
    before_monitor = StableWorkspaceFileMonitor(
        workspace_root,
        graph_argument=arguments.get("graph") or "graph.json",
        ignored_paths=runtime_ignored_paths,
    )
    before = before_monitor.capture()
    candidate = prepare_workspace_registry_runtime(
        arguments,
        compile_catalog=compile_catalog,
    )
    if candidate is None:
        raise RuntimeError("工作区首代未能产生注册表候选")
    # ``monitor`` 使用候选代最终固定的物理图参数，供后续稳定刷新原样复编译。
    monitor = StableWorkspaceFileMonitor(
        workspace_root,
        graph_argument=_graph_argument(candidate),
        ignored_paths=runtime_ignored_paths,
    )
    after = monitor.capture()
    if before.identity != after.identity:
        raise WorkspaceGenerationChangedError(
            "工作区在首代静态编译期间发生变化，请稳定保存后重新启动"
        )
    return PreparedWorkspaceProductGeneration(
        candidate=candidate,
        input_generation=after,
        monitor=monitor,
    )


def compose_workspace_product_lifecycle(
    initial_candidate: WorkspaceRegistryRuntime,
    *,
    registry: Any,
    initial_input: WorkspaceInputGeneration | None = None,
    monitor: StableWorkspaceGenerationMonitor | None = None,
    prepare_generation: Callable[[WorkspaceInputGeneration], Any] | None = None,
    restart_mode: bool = False,
    execution_states: Callable[[], Iterable[str]] = _unknown_execution_state,
    request_restart: Callable[[tuple[str, ...]], None] = _ignore_restart_request,
) -> WorkspaceProductLifecycle:
    """组合复用预编译首代的产品工作区生命周期。

    参数：``initial_candidate`` 是主流程已经静态编译一次的完整候选；``registry``
    是产品实时注册表；``initial_input`` 与 ``monitor`` 可由测试替换；
    ``prepare_generation`` 只编译后续稳定代；``restart_mode``、
    ``execution_states`` 和 ``request_restart`` 控制安全监督重启。
    返回：尚未启动的单一产品生命周期。
    异常：候选、输入或 Adapter 形状无效时关闭式抛出异常。
    """

    if not isinstance(initial_candidate, WorkspaceRegistryRuntime):
        raise TypeError("initial_candidate 必须是 WorkspaceRegistryRuntime")
    # ``generation_monitor`` 只观察字节级完整文件代，不解释包或工作流语义。
    generation_monitor = monitor or StableWorkspaceFileMonitor(
        initial_candidate.source.root,
        graph_argument=_graph_argument(initial_candidate),
    )
    if initial_input is None:
        capture = getattr(generation_monitor, "capture", None)
        if not callable(capture):
            raise TypeError("未提供 initial_input 时监视器必须实现 capture")
        initial_input = capture()
    if prepare_generation is None:
        prepare_generation = _prepare_changed_generation

    # ``initial_pending`` 确保首个 start 复用已有候选，后续才调用编译接缝。
    initial_pending = True

    def prepare(generation: WorkspaceInputGeneration) -> Any:
        """复用首代候选，或编译后续稳定工作区输入代。

        参数：``generation`` 是协调器串行提交的完整输入代。
        返回：首代返回现有候选，后续返回 ``prepare_generation`` 编译结果。
        异常：后续编译失败时传播异常并保留旧活跃代。
        """

        nonlocal initial_pending
        if initial_pending and generation is initial_input:
            initial_pending = False
            return initial_candidate
        return prepare_generation(generation)

    def close_hot_publication(
        _previous: Any,
        _candidate: Any,
    ) -> tuple[str, ...]:
        """在完整代原子发布端口完成前把所有可热变化转为待重启。

        参数：``_previous`` 与 ``_candidate`` 是通用分类已判定可热的新旧代。
        返回：固定产品能力缺口原因。
        异常：无。
        """

        return _ATOMIC_PUBLICATION_UNAVAILABLE

    runtime = WorkspacePackageRuntime(
        initial_input,
        prepare_generation=prepare,
        publisher=_RestartOnlyGenerationPublisher(registry),
        restart_mode=restart_mode,
        execution_states=execution_states,
        request_restart=request_restart,
        hot_publish_guard=close_hot_publication,
    )
    return WorkspaceProductLifecycle(
        WorkspaceRefreshCoordinator(runtime, generation_monitor)
    )


def install_workspace_product_lifecycle(
    prepared: PreparedWorkspaceProductGeneration,
    *,
    registry: Any,
    restart_mode: bool = False,
    execution_states: Callable[[], Iterable[str]] = _unknown_execution_state,
    request_restart: Callable[[tuple[str, ...]], None] = _ignore_restart_request,
) -> WorkspaceProductLifecycle:
    """安装并启动进程唯一的工作区产品生命周期。

    参数：``prepared`` 绑定编译前后相同输入身份的首代；``registry`` 是产品实时
    注册表（Registry）；``restart_mode`` 表示是否受监督器管理；
    ``execution_states`` 读取当前持久执行状态；``request_restart`` 在确认安全后
    提交待重启原因。
    返回：已启动的进程唯一生命周期；同一对象重复安装保持幂等。
    异常：试图替换运行中生命周期或启动失败时传播异常，失败对象不会被公开。
    """

    if not isinstance(prepared, PreparedWorkspaceProductGeneration):
        raise TypeError("prepared 必须是 PreparedWorkspaceProductGeneration")
    global _product_lifecycle
    with _PRODUCT_LIFECYCLE_LOCK:
        if _product_lifecycle is not None:
            return _product_lifecycle
        # ``lifecycle`` 是进程唯一的工作区监视、刷新门禁与首代发布所有者。
        lifecycle = compose_workspace_product_lifecycle(
            prepared.candidate,
            registry=registry,
            initial_input=prepared.input_generation,
            monitor=prepared.monitor,
            restart_mode=restart_mode,
            execution_states=execution_states,
            request_restart=request_restart,
        )
        lifecycle.start()
        _product_lifecycle = lifecycle
        return lifecycle


def get_workspace_product_lifecycle() -> WorkspaceProductLifecycle | None:
    """读取已经安装的工作区产品生命周期。

    参数：无。
    返回：尚未启用工作区时为 ``None``，否则返回进程唯一实例。
    异常：无。
    """

    with _PRODUCT_LIFECYCLE_LOCK:
        return _product_lifecycle


def close_workspace_product_lifecycle() -> None:
    """幂等关闭并清除进程工作区产品生命周期。

    参数：无。
    返回：无；监视器停止后清除公开所有权。
    异常：停止失败时传播异常并保留实例，调用者可再次清理。
    """

    global _product_lifecycle
    with _PRODUCT_LIFECYCLE_LOCK:
        # ``lifecycle`` 保留到监视线程确认停止；失败时不得提前清除所有权。
        lifecycle = _product_lifecycle
        if lifecycle is None:
            return
        lifecycle.close()
        _product_lifecycle = None


def _prepare_changed_generation(
    generation: WorkspaceInputGeneration,
) -> WorkspaceRegistryRuntime:
    """用统一静态编译器准备后续稳定工作区代。

    参数：``generation`` 是文件监视器稳定提交的根、物理图和内容身份。
    返回：完整包目录（PackageCatalog）、注册表快照（Registry Snapshot）和
    工作流源码（Workflow Source）计划组成的候选运行时。
    异常：静态编译或身份校验失败时传播异常，旧产品代保持活跃。
    """

    candidate = prepare_workspace_registry_runtime(
        {
            "workspace": str(generation.workspace_root),
            "graph": generation.graph_argument,
            "devices": None,
            "workflow_editable_package_root": None,
        }
    )
    if candidate is None:
        raise RuntimeError("稳定工作区输入代未能产生注册表候选")
    return candidate


def _graph_argument(candidate: WorkspaceRegistryRuntime) -> str:
    """把固定物理图路径还原为工作区内稳定相对参数。

    参数：``candidate`` 是首代工作区注册表运行时。
    返回：相对工作区根的 POSIX 路径。
    异常：物理图不在工作区时抛出 ``ValueError``。
    """

    return candidate.graph_path.relative_to(candidate.source.root).as_posix()


def _runtime_ignored_paths(
    workspace_root: Path,
    configured_working_directory: Any,
) -> tuple[Path, ...]:
    """计算文件监视器必须排除的产品运行状态目录。

    参数：``workspace_root`` 是显式工作区；``configured_working_directory`` 是
    可选公共启动参数，省略时使用工作区内默认 ``.unilabos``。
    返回：恰好一个绝对运行目录；工作区外目录随后由监视器安全忽略。
    异常：显式值不是字符串或路径时抛出 ``TypeError``，禁止监视范围含义漂移。
    """

    if configured_working_directory is None:
        return (workspace_root / ".unilabos",)
    if not isinstance(configured_working_directory, (str, Path)):
        raise TypeError("working_dir 必须是字符串或路径")
    # ``runtime_directory`` 是数据库、日志和本地投影的唯一可写根。
    runtime_directory = Path(configured_working_directory).expanduser()
    if not runtime_directory.is_absolute():
        runtime_directory = workspace_root / runtime_directory
    return (runtime_directory.absolute(),)


__all__ = [
    "PreparedWorkspaceProductGeneration",
    "WorkspaceGenerationChangedError",
    "WorkspaceProductLifecycle",
    "close_workspace_product_lifecycle",
    "compose_workspace_product_lifecycle",
    "get_workspace_product_lifecycle",
    "install_workspace_product_lifecycle",
    "prepare_stable_workspace_product_generation",
]
