"""工作区本地工作流权威（Workflow Authority）的进程级组合根。"""

from __future__ import annotations

import os
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from unilabos.app.scheduler.inventory.resource_reference import (
    build_inventory_resource_reference_resolver,
)
from unilabos.app.scheduler.inventory.site_selection import (
    build_inventory_site_selection_resolver,
)
from unilabos.registry.local_template_identity import (
    synchronize_local_template_identities,
)
from unilabos.registry.template_identity import (
    TemplateIdentityError,
    action_handle_uuid,
    action_template_uuid,
)
from unilabos.registry.template_projection import (
    RegistryTemplateProjection,
    RegistryTemplateProjectionError,
)
from unilabos.registry.template_snapshot import (
    RegistryTemplateSnapshot,
    RegistryTemplateSnapshotError,
)
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.composite import CompositeAuthoring
from unilabos.workflow.domain_source_target import DomainWorkflowSourceTarget
from unilabos.workflow.publication_catalog import (
    WorkflowPublicationCatalog,
    WorkflowPublicationCatalogError,
)
from unilabos.workflow.published_workflow_runtime import (
    PublishedWorkflowGeneration,
    PublishedWorkflowGenerationError,
    build_published_workflow_generation,
)
from unilabos.workflow.service import AuthoringCompiler, WorkflowService
from unilabos.workflow.source_discovery import (
    EditableSourceDiscoveryPlan,
    discover_editable_sources,
)
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.source_workspace import (
    SourceWorkspaceError,
    read_registered_source,
)
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

_lock = threading.RLock()
_service: Optional[WorkflowService] = None
_database_path: Optional[Path] = None
_monitor: Optional[WorkflowSourceMonitor] = None
_center_runtime: Any = None
_template_projection: Optional[RegistryTemplateProjection] = None
_compiler: Optional[AuthoringCompiler] = None
_compiler_rebuilder: Optional[Callable[[], AuthoringCompiler]] = None
_editable_package_roots: tuple[Path, ...] = ()
_editable_source_discovery_plan: Optional[EditableSourceDiscoveryPlan] = None
_source_monitor_enabled = True
_fixed_point_activation_enabled = False
_allow_unpublished_composite_sources = False
_runtime_template_snapshot_provider: Any = None


class _MutableTemplateSnapshotProvider:
    """只为冷启动复合工作流固定点提供可回滚的目录快照接缝。"""

    def __init__(self) -> None:
        """建立尚未发布任何目录的提供者。"""

        self._snapshot: AuthoringCatalogSnapshot | None = None
        self._candidate = threading.local()
        self._build_lock = threading.RLock()

    def snapshot(self) -> AuthoringCatalogSnapshot:
        """返回当前候选或最终目录；未绑定时关闭式失败。"""

        snapshot = self.current()
        if snapshot is None:
            raise RuntimeError("工作流模板目录尚未发布")
        return snapshot

    def current(self) -> AuthoringCatalogSnapshot | None:
        """返回当前可选快照，供原子刷新失败时保存回滚点。"""

        return getattr(self._candidate, "snapshot", self._snapshot)

    def replace(
        self,
        snapshot: AuthoringCatalogSnapshot | None,
    ) -> AuthoringCatalogSnapshot | None:
        """替换当前快照并返回旧值，供失败路径原样回滚。"""

        previous = self.current()
        if hasattr(self._candidate, "snapshot"):
            self._candidate.snapshot = snapshot
        else:
            self._snapshot = snapshot
        return previous

    @contextmanager
    def candidate_scope(self):
        """仅串行化构建者；固定点中间目录只对当前构建线程可见。"""
        with self._build_lock:
            nested = hasattr(self._candidate, "snapshot")
            previous = self.current()
            self._candidate.snapshot = previous
            try:
                yield
            finally:
                if nested:
                    self._candidate.snapshot = previous
                else:
                    del self._candidate.snapshot

    def publish(self, snapshot: AuthoringCatalogSnapshot) -> None:
        """构建成功后一次发布完整目录；读取者不等待构建锁。"""
        self._snapshot = snapshot


@dataclass
class _RuntimeCleanupOwner:
    """持有部分启动失败后仍需重试清理的完整运行时资源。"""

    service: WorkflowService
    monitor: WorkflowSourceMonitor | None
    center_runtime: Any = None
    center_stopped: bool = False
    monitor_stopped: bool = False
    service_closed: bool = False

    def cleanup(self) -> None:
        """按监视器后服务的安全顺序幂等推进清理。

        参数：无。返回：全部资源关闭时无返回值；任一步失败原样抛出，并保留
        已完成步骤标记供下一次重置继续，而不是重复关闭或遗失所有权。
        """

        if not self.center_stopped:
            if self.center_runtime is not None:
                self.center_runtime.close()
            self.center_stopped = True
        if not self.monitor_stopped:
            if self.monitor is not None:
                self.monitor.stop()
            self.monitor_stopped = True
        if not self.service_closed:
            self.service.close()
            self.service_closed = True


_failed_runtime: Optional[_RuntimeCleanupOwner] = None


def _cleanup_partial_composition(
    *,
    original_error: BaseException,
    workflow_store: WorkflowStore,
    workflow_definition_store: WorkflowStore,
    task_scheduler_bridge: Any,
) -> None:
    """按反向所有权清理尚未发布的工作流运行时组合。

    参数：``original_error`` 是必须保留的启动异常；``workflow_store`` 是待关闭的
    持久运行事实库，``workflow_definition_store`` 是待关闭的进程内定义目录；
    ``task_scheduler_bridge`` 是可能已注册调度监听器的唯一公共桥。返回无；每个
    清理异常只附加为原异常说明，不掩盖首个构造或恢复故障。
    """

    # ``owned_resources`` 按创建顺序列出，关闭时反向遍历，先注销桥再关闭存储。
    owned_resources = (
        ("工作流存储", workflow_store),
        ("工作流定义目录", workflow_definition_store),
        ("工作流任务调度桥", task_scheduler_bridge),
    )
    closed_ids: set[int] = set()
    for resource_name, resource in reversed(owned_resources):
        if resource is None or id(resource) in closed_ids:
            continue
        try:
            resource.close()
            closed_ids.add(id(resource))
        except BaseException as cleanup_error:  # noqa: BLE001 - 清理不能掩盖原始异常
            original_error.add_note(f"{resource_name}清理失败: {cleanup_error}")


def _configured_package_roots(
    roots: Iterable[str | Path],
) -> tuple[Path, ...]:
    """冻结本次进程组合使用的显式授权包目录。

    参数：``roots`` 是调用者明确配置的可编辑包（Editable Package）选择目录。
    返回：保留输入顺序且不跟随符号链接的绝对路径元组；安全性由发现模块复核。
    """

    if not isinstance(roots, tuple) or any(
        not isinstance(root, (str, Path)) for root in roots
    ):
        raise TypeError("工作流源码授权目录必须是 tuple[str | Path, ...]")
    return tuple(Path(root).absolute() for root in roots)


def compose_workflow_runtime(
    working_dir: str | Path,
    *,
    compiler: Optional[AuthoringCompiler] = None,
    compiler_rebuilder: Optional[Callable[[], AuthoringCompiler]] = None,
    editable_package_roots: Iterable[str | Path] = (),
    editable_source_discovery_plan: Optional[EditableSourceDiscoveryPlan] = None,
    material_resolver: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    resource_reference_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
    site_selection_resolver: Optional[
        Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ] = None,
    scheduler: Optional[Any] = None,
    start_source_monitor: bool = True,
    workflow_activation_progress: Callable[[int, int], None] | None = None,
    template_snapshot_provider: Any = None,
    workflow_definition_store: WorkflowStore | None = None,
    activate_sources_to_fixed_point: bool = False,
    before_publish: Callable[[], None] | None = None,
) -> WorkflowService:
    """装配工作区唯一的工作流权威、启动恢复和草稿监视。

    参数：``working_dir`` 决定持久运行事实 SQLite 路径；``compiler`` 是可信工作流
    创作编译器；``compiler_rebuilder`` 在应用后原子刷新完整模板代际；
    ``editable_package_roots`` 是唯一允许发现源码的显式授权目录；
    ``editable_source_discovery_plan`` 是包目录（PackageCatalog）
    编译代际产生的预编译工作流源码（Workflow Source）计划；
    ``material_resolver`` 按稳定 UUID 读取本地物料权威摘要；``scheduler`` 是仅在
    本地调度模式装配的现有调度器（EdgeScheduler）；``start_source_monitor``
    仅供非工作区遗留入口保留逐源码监视，工作区必须传 ``False`` 并由统一文件
    世代监视器拥有刷新；``workflow_activation_progress`` 报告已完成真实编译的
    工作流源码数量和总数，不参与运行时组合身份；
    ``workflow_definition_store`` 是组合根内部复用的进程内定义目录，普通调用方
    省略后由本函数创建；``activate_sources_to_fixed_point`` 要求领域包工作流在
    ready 前按组合依赖完整应用。
    ``before_publish`` 是完整来源激活、任务恢复均成功后的最后一道原子发布
    屏障；回调失败按启动失败清理服务，且不会设置进程级工作流权威。
    返回：完成来源注册与启动恢复后发布的进程唯一工作流服务（WorkflowService）。
    异常：同时提供授权目录与预编译计划，或运行期间
    切换数据库、编译器、授权目录或来源计划时关闭式失败。
    """

    global _compiler, _compiler_rebuilder, _database_path
    global _editable_package_roots, _editable_source_discovery_plan, _failed_runtime
    global _monitor, _runtime_template_snapshot_provider, _service, _center_runtime
    global _fixed_point_activation_enabled, _source_monitor_enabled
    # ``workflow_history.db`` 只保存 Task/Job 等运行事实；定义由本次进程目录持有。
    database_path = Path(working_dir).resolve() / "workflow_history.db"
    configured_roots = _configured_package_roots(editable_package_roots)
    if configured_roots and editable_source_discovery_plan is not None:
        raise TypeError("工作流源码授权目录与预编译发现计划不能同时提供")
    if editable_source_discovery_plan is not None and not isinstance(
        editable_source_discovery_plan,
        EditableSourceDiscoveryPlan,
    ):
        raise TypeError("预编译工作流源码计划类型无效")
    with _lock:
        if _failed_runtime is not None:
            raise RuntimeError("工作流运行时仍有部分启动资源等待显式重置清理")
        if _service is not None:
            if database_path != _database_path:
                raise RuntimeError(
                    "工作流权威（Workflow Authority）运行期间不能切换 working_dir"
                )
            if compiler is not _compiler:
                raise RuntimeError(
                    "工作流权威（Workflow Authority）运行期间不能切换 compiler"
                )
            if compiler_rebuilder is not _compiler_rebuilder:
                raise RuntimeError(
                    "工作流权威（Workflow Authority）运行期间不能切换目录重建器"
                )
            if template_snapshot_provider is not _runtime_template_snapshot_provider:
                raise RuntimeError("工作流权威运行期间不能切换模板目录提供者")
            if configured_roots != _editable_package_roots:
                raise RuntimeError(
                    "工作流权威（Workflow Authority）运行期间不能切换可编辑包"
                )
            if editable_source_discovery_plan != _editable_source_discovery_plan:
                raise RuntimeError(
                    "工作流权威（Workflow Authority）运行期间不能切换源码发现计划"
                )
            if bool(start_source_monitor) != _source_monitor_enabled:
                raise RuntimeError("工作流权威运行期间不能切换源码监视所有权")
            if bool(activate_sources_to_fixed_point) != _fixed_point_activation_enabled:
                raise RuntimeError("工作流权威运行期间不能切换源码激活策略")
            if workflow_activation_progress is not None:
                total = len(_service.list_registered_sources())
                workflow_activation_progress(total, total)
            return _service
        # 文件库只持久化 WorkflowTask/WorkflowNodeJob 等运行事实；工作流定义、
        # 图、来源注册和创作状态全部进入本次进程独占的内存目录。
        workflow_store = (
            WorkflowStore(
                database_path,
                template_snapshot_provider=template_snapshot_provider,
                persist_workflow_definitions=False,
            )
            if template_snapshot_provider is not None
            else WorkflowStore(
                database_path,
                persist_workflow_definitions=False,
            )
        )
        try:
            definition_store = (
                workflow_definition_store
                if workflow_definition_store is not None
                else (
                    WorkflowStore(
                        ":memory:",
                        template_snapshot_provider=template_snapshot_provider,
                    )
                    if template_snapshot_provider is not None
                    else WorkflowStore(":memory:")
                )
            )
        except BaseException as definition_store_error:
            try:
                workflow_store.close()
            except BaseException as close_error:
                definition_store_error.add_note(
                    f"持久运行事实库清理失败: {close_error}"
                )
            raise
        task_scheduler_bridge = None
        new_service: Optional[WorkflowService] = None
        new_monitor: Optional[WorkflowSourceMonitor] = None
        new_center: Any = None
        try:
            # 发现计划同时决定启动来源与用户导入的默认领域包写入目标。只有一个
            # 实际包根时才允许自动落盘；多包场景禁止按顺序猜测目标。
            discovery_plan = (
                editable_source_discovery_plan
                if editable_source_discovery_plan is not None
                else discover_editable_sources(configured_roots)
            )
            source_target = DomainWorkflowSourceTarget.from_discovery_plan(
                discovery_plan
            )
            if scheduler is not None:
                task_scheduler_bridge = TaskSchedulerBridge(
                    workflow_store,
                    scheduler=scheduler,
                )
            new_service = WorkflowService(
                workflow_store,
                definition_store=definition_store,
                compiler=compiler,
                compiler_rebuilder=compiler_rebuilder,
                source_target=source_target,
                material_resolver=material_resolver,
                resource_reference_resolver=resource_reference_resolver,
                site_selection_resolver=site_selection_resolver,
                task_scheduler_bridge=task_scheduler_bridge,
            )
            if scheduler is not None:
                # 工站调度组合根把动作执行端的异常决策端口接入持久工作流干预；
                # 纯创作组合不传 Scheduler，也不会意外建立运行权威。
                intervention_delivery = getattr(scheduler, "_dispatcher", None)
                if callable(
                    getattr(
                        intervention_delivery,
                        "add_error_decision_required_listener",
                        None,
                    )
                ) and callable(
                    getattr(intervention_delivery, "resolve_error_decision", None)
                ):
                    new_service.bind_intervention_delivery(intervention_delivery)
            # ``discovery_plan`` 是全量文件预校验结果；服务在单事务中注册后，
            # 才能恢复草稿并建立一致的监视基线。
            new_service.replace_discovered_source_authorizations(discovery_plan)
            # 发布合同文件是跨重启保留实验操作身份的权威。先恢复合同，再按子到父
            # 固定点应用源码；这样首个子来源完成应用后的目录重建即可看到合同，
            # 父来源不会在“合同尚未恢复”的瞬间被编译成不可恢复的缺失诊断。
            new_service.restore_published_workflow_contracts()
            if (
                editable_source_discovery_plan is not None
                or activate_sources_to_fixed_point
            ):
                # managed-local 工作区把同代 PackageCatalog 当作激活权威；OS
                # 只有在子到父的组合依赖全部推进到固定点后才能发布 ready。
                if workflow_activation_progress is None:
                    new_service.activate_registered_sources_to_fixed_point()
                else:
                    new_service.activate_registered_sources_to_fixed_point(
                        progress_callback=workflow_activation_progress,
                    )
            else:
                # 遗留显式目录入口继续保留候选/人工应用语义。
                new_service.recover_registered_sources()
            # 工作流定义由领域包 Python 恢复后，再把同一领域包中的不可变发布
            # 合同恢复到进程内目录。运行事实 SQLite 不持有工作流定义或发布权威。
            new_service.restore_published_workflow_contracts()
            if task_scheduler_bridge is not None:
                # 后端重建只恢复结果路由；设备进程重启由 Edge HELLO 身份变化确认。
                task_scheduler_bridge.recover_active_tasks(
                    execution_process_restarted=False
                )
            if start_source_monitor:
                new_monitor = WorkflowSourceMonitor(new_service)
            # 连接管理器随本地运行时启动，注册可以在 UI 中稍后触发。
            from unilabos.app.center_client.connection import connection_runtime
            new_center = connection_runtime(database_path, new_service, workflow_store)
            if before_publish is not None:
                before_publish()
        except BaseException as startup_error:
            if new_center is not None:
                try:
                    new_center.close()
                except BaseException as cleanup_error:
                    startup_error.add_note(f"中心运行时清理失败: {cleanup_error}")
            _cleanup_partial_composition(
                original_error=startup_error,
                workflow_store=workflow_store,
                workflow_definition_store=definition_store,
                task_scheduler_bridge=task_scheduler_bridge,
            )
            raise

        # 启动恢复已经完成，此处才一次发布进程内工作流权威及其完整组合身份。
        _service = new_service
        _database_path = database_path
        _compiler = compiler
        _compiler_rebuilder = compiler_rebuilder
        _runtime_template_snapshot_provider = template_snapshot_provider
        _editable_package_roots = configured_roots
        _editable_source_discovery_plan = editable_source_discovery_plan
        _monitor = new_monitor
        _center_runtime = new_center
        _source_monitor_enabled = bool(start_source_monitor)
        _fixed_point_activation_enabled = bool(activate_sources_to_fixed_point)
        try:
            if new_monitor is not None:
                new_monitor.start()
            if new_center is not None:
                new_center.start()
        except BaseException as start_error:
            # 监视线程未可靠启动时立即撤销公开权威，但完整资源所有权必须保留到
            # 停止和服务关闭都确认成功，不能让清理错误覆盖原始启动错误。
            _service = None
            _database_path = None
            _compiler = None
            _compiler_rebuilder = None
            _runtime_template_snapshot_provider = None
            _editable_package_roots = ()
            _editable_source_discovery_plan = None
            _monitor = None
            _center_runtime = None
            _source_monitor_enabled = True
            _fixed_point_activation_enabled = False
            cleanup_owner = _RuntimeCleanupOwner(new_service, new_monitor, new_center)
            _failed_runtime = cleanup_owner
            try:
                cleanup_owner.cleanup()
            except BaseException as cleanup_error:
                start_error.add_note(f"后续清理失败: {cleanup_error}")
            else:
                _failed_runtime = None
            raise start_error
        return new_service


def setup_workflow_service(
    working_dir: str | Path,
    *,
    compiler: Optional[AuthoringCompiler] = None,
    editable_package_roots: Iterable[str | Path] = (),
) -> WorkflowService:
    """兼容旧装配调用并进入完整工作流运行时组合。

    参数：``working_dir`` 决定数据库路径；``compiler`` 是可信创作编译器；
    ``editable_package_roots`` 是显式授权源码目录。
    返回：完成注册、恢复与发布的工作流服务（WorkflowService）。
    """

    return compose_workflow_runtime(
        working_dir,
        compiler=compiler,
        editable_package_roots=editable_package_roots,
    )


def compose_local_workflow_template_runtime(
    working_dir: str | Path,
    *,
    inventory_store: Any,
    registry: Any,
    scheduler: Optional[Any] = None,
    editable_package_roots: Iterable[str | Path] = (),
    editable_source_discovery_plan: Optional[EditableSourceDiscoveryPlan] = None,
    start_source_monitor: bool = True,
    workflow_activation_progress: Callable[[int, int], None] | None = None,
    allow_unpublished_composite_sources: bool = False,
) -> tuple[WorkflowService, RegistryTemplateProjection]:
    """装配本地模板权威、F02 创作编译器与工作流服务。

    参数说明：``working_dir`` 决定现有 ``workflow_history.db`` 路径；
    ``inventory_store`` 是同步并持有资源模板身份的 ``inventory.db`` 权威；
    ``registry`` 是原始注册表（Registry）或不可变注册表快照（Registry Snapshot）；
    ``scheduler`` 是仅在本地调度模式装配的既有调度器（EdgeScheduler）；
    ``editable_package_roots`` 是本次进程唯一授权的工作流源码（Workflow
    Source）目录 tuple；``editable_source_discovery_plan`` 是与注册表快照
    （Registry Snapshot）同代的预编译来源计划，存在时禁止再读
    ``package.yaml``；``start_source_monitor`` 仅允许遗留入口启动逐源码监视；
    ``workflow_activation_progress`` 报告工作流源码真实编译进度。
    ``allow_unpublished_composite_sources`` 仅用于允许未发布源码的 Runtime：为当前
    活动源码建立本地组合解析身份，即使来源尚未写入工作流发布目录也可被父工作流
    加载。该选项不改变发布文件或 HTTP 可见性；禁止运行未发布定义时应保持为
    ``False``。
    返回：共享同一已发布目录代际的工作流服务
    （WorkflowService）与模板投影（Template Projection）。异常：注册表快照构造、
    本地模板身份同步或模板投影失败时统一抛出
    ``RegistryTemplateProjectionError``，不发布工作流权威（Workflow Authority）；
    若失败前已经创建模板投影，则先关闭其持有的工作流存储连接再传播异常。
    """

    global _template_projection, _allow_unpublished_composite_sources
    with _lock:
        if _template_projection is not None:
            if bool(allow_unpublished_composite_sources) != (
                _allow_unpublished_composite_sources
            ):
                raise RuntimeError(
                    "工作流权威运行期间不能切换未发布组合来源策略"
                )
            # 已发布的模板投影必须复用原编译器和授权目录组合身份。
            service = compose_workflow_runtime(
                working_dir,
                compiler=_compiler,
                compiler_rebuilder=_compiler_rebuilder,
                editable_package_roots=editable_package_roots,
                editable_source_discovery_plan=editable_source_discovery_plan,
                start_source_monitor=start_source_monitor,
                workflow_activation_progress=workflow_activation_progress,
                template_snapshot_provider=_template_projection,
                activate_sources_to_fixed_point=True,
            )
            return service, _template_projection
        if _service is not None:
            raise RuntimeError("本地工作流服务已在没有模板投影的情况下完成装配")

        try:
            # ``registry_snapshot`` 是库存同步与模板投影共同消费的唯一注册表代际。
            registry_snapshot = (
                registry
                if isinstance(registry, RegistryTemplateSnapshot)
                else RegistryTemplateSnapshot.from_registry(registry)
            )
        except RegistryTemplateSnapshotError as error:
            raise RegistryTemplateProjectionError(str(error)) from error
        # ``resolve_resource_template_identity`` 是当前注册表（Registry）代际唯一的
        # 资源模板（ResourceTemplate）业务 ID/源码别名到活动 UUID 解析器。
        identity_synchronization = synchronize_local_template_identities(
            inventory_store=inventory_store,
            registry_snapshot=registry_snapshot,
        )
        resolve_resource_template_identity = identity_synchronization

        def resolve_material_identity(
            material_uuid: str,
        ) -> Optional[dict[str, Any]]:
            """按稳定 UUID 读取活动物料（Material）身份。

            参数：``material_uuid`` 是设备动作最终参数或执行器分配引用的实际物料
            UUID。返回 UUID 与资源模板 UUID 摘要；不存在或已删除时返回 ``None``。
            """

            # ``material_row`` 来自本地库存权威，只提供合同校验所需的稳定身份，
            # 不把可变物料快照复制进工作流写模型。
            material_row = inventory_store.query_one(
                """
                SELECT uuid, resource_template_uuid, meta_data
                FROM material
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (material_uuid,),
            )
            return dict(material_row) if material_row is not None else None

        configured_roots = _configured_package_roots(editable_package_roots)
        if configured_roots and editable_source_discovery_plan is not None:
            raise TypeError("工作流源码授权目录与预编译发现计划不能同时提供")
        publication_plan = (
            editable_source_discovery_plan
            if editable_source_discovery_plan is not None
            else discover_editable_sources(configured_roots)
        )
        # 每个可编辑包根各自拥有一份发布目录；模板资格必须从当前文件、当前
        # 源码字节和当前合同修订三项事实交集计算。``seen`` 用来区分“从未有过
        # 发布目录”的旧包兼容路径与已经撤空/删除发布文件的严格路径：一旦目录
        # 曾经出现，后续缺失只能表示当前没有可引用合同，不能重新把全部源码当
        # 成模板暴露。
        publication_catalogs_by_root = {
            str(Path(package_root)): WorkflowPublicationCatalog(
                package_root=Path(package_root),
                package_root_identity=package_root_identity,
            )
            for package_root, package_root_identity in publication_plan.root_identities
        }
        publication_roots_seen: set[str] = set()
        for root_key, catalog in publication_catalogs_by_root.items():
            try:
                _entries, present = catalog.list_entries_with_presence()
            except WorkflowPublicationCatalogError as error:
                raise RegistryTemplateProjectionError(str(error)) from error
            if present:
                publication_roots_seen.add(root_key)

        def _publication_eligibility(
            registrations: tuple[dict[str, Any], ...],
        ) -> tuple[set[str], dict[str, int], dict[str, str], set[str]]:
            """按包根计算当前可引用的发布合同 pin。

            参数：``registrations`` 是进程内当前活动来源注册；返回依次为可引用
            UUID、合同修订、源码哈希和仅适用于从未拥有发布文件的旧包来源集合。
            异常：发布目录损坏或源码读取失败转换为模板投影错误；单项合同缺失、
            撤权或源码哈希漂移均安全地从可引用集合排除。
            """

            by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for registration in registrations:
                by_root[str(registration["package_root"])].append(registration)
            eligible: set[str] = set()
            revisions: dict[str, int] = {}
            source_hashes: dict[str, str] = {}
            legacy: set[str] = set()
            for root_key, root_registrations in by_root.items():
                catalog = publication_catalogs_by_root.get(root_key)
                if catalog is None:
                    # 遗留的无包目录组合入口没有可核验发布文件；保持其旧
                    # 适配器语义，但不把这个分支用于已知工作区根。
                    legacy.update(
                        str(item["workflow_uuid"]) for item in root_registrations
                    )
                    continue
                try:
                    entries, present = catalog.list_entries_with_presence()
                except WorkflowPublicationCatalogError as error:
                    raise RegistryTemplateProjectionError(str(error)) from error
                if present:
                    publication_roots_seen.add(root_key)
                if root_key not in publication_roots_seen:
                    # 只有从未产生过发布目录的旧包才允许启动固定点临时引用
                    # 源码；一旦文件落盘，下一次重建立即切换严格资格。
                    legacy.update(
                        str(item["workflow_uuid"]) for item in root_registrations
                    )
                    continue
                latest_by_uuid: dict[str, dict[str, Any]] = {}
                for entry in entries:
                    contract = entry.get("contract")
                    if not isinstance(contract, Mapping):
                        continue
                    workflow_uuid = str(contract.get("workflow_uuid") or "")
                    if not workflow_uuid:
                        continue
                    previous = latest_by_uuid.get(workflow_uuid)
                    if previous is None or int(contract["version"]) > int(
                        previous["contract"]["version"]
                    ):
                        latest_by_uuid[workflow_uuid] = entry
                for registration in root_registrations:
                    workflow_uuid = str(registration["workflow_uuid"])
                    entry = latest_by_uuid.get(workflow_uuid)
                    if entry is None:
                        continue
                    contract = entry.get("contract")
                    if not isinstance(contract, Mapping):
                        continue
                    revision = contract.get("workflow_revision")
                    if (
                        isinstance(revision, bool)
                        or not isinstance(revision, int)
                        or revision < 1
                    ):
                        continue
                    raw_hash = entry.get("source_draft_hash")
                    if not isinstance(raw_hash, str) or not raw_hash:
                        continue
                    expected_hash = (
                        raw_hash
                        if raw_hash.startswith("sha256:")
                        else f"sha256:{raw_hash}"
                    )
                    try:
                        source = read_registered_source(registration)
                    except SourceWorkspaceError as error:
                        raise RegistryTemplateProjectionError(str(error)) from error
                    if source is None or source.draft_hash != expected_hash:
                        # 源码已被编辑、文件缺失或合同已撤权时，旧模板不得
                        # 继续留在目录中；父工作流会得到可定位的缺失诊断。
                        continue
                    eligible.add(workflow_uuid)
                    revisions[workflow_uuid] = revision
                    source_hashes[workflow_uuid] = expected_hash
            return eligible, revisions, source_hashes, legacy

        active_registrations = tuple(
            {
                "workflow_uuid": item.workflow_uuid,
                "package_id": item.package_id,
                "package_root": str(item.package_root),
                "relative_path": item.relative_path,
                "source_uri": item.source_uri,
                "module": item.module,
                "symbol": item.symbol,
                "definition_content_hash": item.definition_content_hash,
            }
            for item in publication_plan.registrations
        )
        publication_template_provider = _MutableTemplateSnapshotProvider()
        publication_store = WorkflowStore(
            ":memory:",
            template_snapshot_provider=publication_template_provider,
        )
        # 先把启动发现计划的五项来源身份安装到进程内目录。首次注册表投影发生在
        # ``compose_workflow_runtime`` 安装服务之前；提前安装让首轮编译与后续重建
        # 使用同一个可撤销来源集合，避免既看不到静态来源又在运行期撤权后复活。
        if active_registrations:
            publication_store.install_discovered_sources(
                tuple(
                    {
                        field: registration[field]
                        for field in (
                            "workflow_uuid",
                            "package_id",
                            "package_root",
                            "relative_path",
                            "source_uri",
                        )
                    }
                    for registration in active_registrations
                )
            )
        published_generation: PublishedWorkflowGeneration | None = None
        composed_service: WorkflowService | None = None

        def extend_template_generation(
            base_nodes: Any,
            base_handles: Any,
        ) -> tuple[Any, Any]:
            """独立刷新调用也只能在构建线程内观测未完成目录。"""
            with publication_template_provider.candidate_scope():
                return build_template_generation(base_nodes, base_handles)

        def build_template_generation(
            base_nodes: Any,
            base_handles: Any,
        ) -> tuple[Any, Any]:
            """在同一模板替换事务候选中追加全部已发布工作流合同。

            参数：``base_nodes``/``base_handles`` 是本次注册表编译出的设备与框架
            模板全集。返回：只含已发布工作流的节点和连接点候选。异常：来源或
            应用快照不一致时转换为 ``RegistryTemplateProjectionError``。
            """

            nonlocal published_generation
            # 发布运行中新增的实验操作后，来源注册会先写入进程内定义目录。
            # 不能只复用启动时冻结的 ``active_registrations``，否则父工作流
            # 随后引用新发布的子工作流时，编译目录仍看不到它的模板。
            initial_registration_by_uuid = {
                str(registration["workflow_uuid"]): registration
                for registration in active_registrations
            }
            # 启动阶段定义库只有本次计划；服务就绪后由服务的活动授权集合过滤
            # 历史注册行，避免授权替换撤销的来源被模板重建重新并入。
            registered_by_uuid: dict[str, dict[str, Any]] = {}
            registrations = (
                composed_service.list_registered_sources()
                if composed_service is not None
                else publication_store.list_source_registrations()
            )
            for registration in registrations:
                current = dict(registration)
                workflow_uuid = str(current["workflow_uuid"])
                initial = initial_registration_by_uuid.get(workflow_uuid)
                if initial is not None:
                    # 启动发现计划中的静态模块/符号/哈希更可信；动态导入
                    # 来源没有这些字段，build 函数会从已应用源码快照补齐。
                    for field in ("module", "symbol", "definition_content_hash"):
                        value = initial.get(field)
                        if value is not None:
                            current[field] = value
                registered_by_uuid[workflow_uuid] = current
            current_registrations = tuple(
                registered_by_uuid[workflow_uuid]
                for workflow_uuid in sorted(registered_by_uuid)
            )
            base_node_uuid_by_key = {
                (str(node["resource_template_uuid"]), str(node["name"])): str(
                    node["uuid"]
                )
                for node in base_nodes
            }
            resolved_base_handles: list[dict[str, Any]] = []
            for raw_handle in base_handles:
                handle = dict(raw_handle)
                parent_key = handle.pop("node_business_key", None)
                if not isinstance(parent_key, (list, tuple)) or len(parent_key) != 2:
                    raise RegistryTemplateProjectionError(
                        "候选 Handle 缺少父动作名称身份"
                    )
                try:
                    handle["workflow_node_template_uuid"] = base_node_uuid_by_key[
                        (str(parent_key[0]), str(parent_key[1]))
                    ]
                except KeyError:
                    raise RegistryTemplateProjectionError(
                        "候选 Handle 引用了未知父动作"
                    ) from None
                resolved_base_handles.append(handle)
            base_snapshot = AuthoringCatalogSnapshot.from_entities(
                base_nodes,
                resolved_base_handles,
            )
            previous_snapshot = publication_template_provider.replace(base_snapshot)
            try:
                (
                    published_workflow_uuids,
                    published_workflow_revisions,
                    published_workflow_source_hashes,
                    legacy_workflow_uuids,
                ) = _publication_eligibility(current_registrations)
                strict_publication_catalog = bool(publication_catalogs_by_root)
                # 开发工作区需要“源码可组合”而不是“已发布才可组合”：未发布的
                # 子工作流仍然可以参与本地父图编译，但已发布来源继续沿用当前
                # 合同的 revision/hash pin。生产入口不传该开关，保持严格发布边界。
                workspace_composite_uuids = set(legacy_workflow_uuids)
                if allow_unpublished_composite_sources:
                    workspace_composite_uuids.update(
                        str(registration["workflow_uuid"])
                        for registration in current_registrations
                        if str(registration["workflow_uuid"])
                        not in published_workflow_revisions
                    )
                # 读取嵌套已应用图需要其子模板。每次从本轮设备目录出发，
                # 只把当前授权和 pin 校验通过的投影加入下一轮读取闭包；不借用
                # 上一代模板，避免撤权来源或旧设备 schema 掩盖本轮错误。
                read_snapshot = base_snapshot
                for _pass in range(len(current_registrations) + 1):
                    generation = build_published_workflow_generation(
                        registrations=tuple(current_registrations),
                        snapshot_provider=publication_store,
                        base_node_templates=base_nodes,
                        # 已出现发布目录的根严格只接受当前源码哈希与合同修订；
                        # 从未拥有发布文件的旧包才保留有限的启动兼容集合。
                        published_workflow_revisions=(
                            published_workflow_revisions
                            if strict_publication_catalog
                            else None
                        ),
                        published_workflow_source_hashes=(
                            published_workflow_source_hashes
                            if strict_publication_catalog
                            else None
                        ),
                        workspace_workflow_uuids=(
                            workspace_composite_uuids
                            if strict_publication_catalog
                            else None
                        ),
                    )
                    overlaid_nodes, overlaid_handles = (
                        _overlay_published_template_identities(
                            generation.node_templates,
                            generation.handle_templates,
                            publication_store.list_published_template_projections(),
                        )
                    )
                    named_nodes = tuple(
                        _assign_published_template_identity(node)
                        for node in overlaid_nodes
                    )
                    node_uuid_by_key = {
                        (str(node["resource_template_uuid"]), str(node["name"])): str(
                            node["uuid"]
                        )
                        for node in named_nodes
                    }
                    resolved_handles: list[dict[str, Any]] = []
                    for raw_handle in overlaid_handles:
                        handle = dict(raw_handle)
                        parent_key = handle.pop("node_business_key")
                        parent_uuid = node_uuid_by_key[tuple(map(str, parent_key))]
                        handle["workflow_node_template_uuid"] = parent_uuid
                        handle.setdefault(
                            "uuid",
                            action_handle_uuid(
                                parent_uuid,
                                io_type=handle["io_type"],
                                handle_key=handle["handle_key"],
                            ),
                        )
                        resolved_handles.append(handle)
                    next_snapshot = AuthoringCatalogSnapshot.from_entities(
                        (*base_nodes, *named_nodes),
                        (*resolved_base_handles, *resolved_handles),
                    )
                    if next_snapshot.fingerprint == read_snapshot.fingerprint:
                        break
                    publication_template_provider.replace(next_snapshot)
                    read_snapshot = next_snapshot
                else:
                    raise PublishedWorkflowGenerationError("工作流模板依赖闭包未收敛")
            except (PublishedWorkflowGenerationError, TemplateIdentityError) as error:
                publication_template_provider.replace(previous_snapshot)
                raise RegistryTemplateProjectionError(str(error)) from error
            except BaseException:
                publication_template_provider.replace(previous_snapshot)
                raise
            published_generation = PublishedWorkflowGeneration(
                source_catalog=generation.source_catalog,
                node_templates=named_nodes,
                handle_templates=overlaid_handles,
            )
            return named_nodes, overlaid_handles

        projection = RegistryTemplateProjection.in_memory(
            authority_id="local",
            resource_template_identity_resolver=resolve_resource_template_identity,
            generation_extension=extend_template_generation,
        )
        try:
            # ``resource_reference_resolver`` 只读取 C3 已提交的本地资源图物料事实，
            # 让物料来源（MaterialSource）和普通动作共享同一业务 ID→UUID 规则。
            resource_reference_resolver = build_inventory_resource_reference_resolver(
                inventory_store
            )
            site_selection_resolver = build_inventory_site_selection_resolver(
                inventory_store
            )

            def rebuild_compiler() -> AuthoringCompiler:
                """刷新完整模板代际并返回与该代际绑定的新创作编译器。

                参数：无。返回：共享同一不可变目录快照的编译器与组合展开端口。
                异常：注册表、发布来源或模板替换失败时原样传播，调用服务据此
                关闭陈旧编译入口。
                """

                with publication_template_provider.candidate_scope():
                    snapshot = projection.refresh(registry_snapshot)
                    if published_generation is None:
                        raise RegistryTemplateProjectionError("已发布工作流目录扩展未执行")
                    compiler = WorkflowAuthoringEngine(
                        catalog=snapshot,
                        resource_reference_resolver=resource_reference_resolver,
                        composite_authoring=CompositeAuthoring(
                            snapshot_provider=publication_store,
                            catalog=snapshot,
                            resolver=published_generation.source_catalog,
                        ),
                    )
                    publication_template_provider.publish(snapshot)
                    return compiler

            compiler = rebuild_compiler()
            service = compose_workflow_runtime(
                working_dir,
                compiler=compiler,
                compiler_rebuilder=rebuild_compiler,
                editable_package_roots=editable_package_roots,
                editable_source_discovery_plan=editable_source_discovery_plan,
                material_resolver=resolve_material_identity,
                resource_reference_resolver=resource_reference_resolver,
                site_selection_resolver=site_selection_resolver,
                scheduler=scheduler,
                start_source_monitor=start_source_monitor,
                workflow_activation_progress=workflow_activation_progress,
                template_snapshot_provider=projection,
                workflow_definition_store=publication_store,
                activate_sources_to_fixed_point=True,
                # 领域源码完成子到父固定点激活、运行任务恢复后，才把设备模板
                # 候选代际装入库存服务；任一更早失败都不会迁移或发布设备目录。
                before_publish=(
                    identity_synchronization.activate_runtime_device_catalog
                ),
            )
            composed_service = service
        except BaseException:
            projection.close()
            publication_store.close()
            raise
        _template_projection = projection
        _allow_unpublished_composite_sources = bool(
            allow_unpublished_composite_sources
        )
        return service, projection


def _assign_published_template_identity(
    raw_template: dict[str, Any],
) -> dict[str, Any]:
    """为领域包工作流模板生成与设备动作一致的确定性外部 UUID。

    参数：``raw_template`` 是已校验发布合同节点。返回：保留原合同并补充
    ``uuid`` 的分离字典；稳定身份为 ``宿主设备模板名.工作流动作名``。
    """

    template = dict(raw_template)
    meta_data = template.get("meta_data")
    resource_template = (
        meta_data.get("resource_template") if isinstance(meta_data, dict) else None
    )
    device_name = (
        resource_template.get("name") if isinstance(resource_template, dict) else None
    )
    action_name = template.get("name")
    if not isinstance(device_name, str) or not device_name:
        raise TemplateIdentityError("发布工作流模板缺少宿主设备模板名")
    if not isinstance(action_name, str) or not action_name:
        raise TemplateIdentityError("发布工作流模板缺少动作模板名")
    # 恢复的发布合同已经携带发布时固定的节点模板 UUID。它可能与本次
    # Registry 代际首次生成的确定性 UUID 不同，但仍是父图和边界映射使用的
    # 权威身份；保留它才能让跨重启的合同继续命中同一模板。
    contract_meta = meta_data.get("unilab") if isinstance(meta_data, dict) else None
    if isinstance(contract_meta, dict):
        contract = contract_meta.get("workflow_contract")
        if isinstance(contract, dict) and isinstance(contract.get("contract_uuid"), str):
            return template
    template["uuid"] = action_template_uuid(device_name, action_name)
    return template


def _overlay_published_template_identities(
    generated_nodes: tuple[dict[str, Any], ...],
    generated_handles: tuple[dict[str, Any], ...],
    persisted: list[dict[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """把恢复合同的节点/Handle 身份覆盖到本次目录生成结果。

    参数：``generated_nodes``/``generated_handles`` 是当前源码目录生成的发布
    模板；``persisted`` 是定义库中恢复的不可变合同投影。返回：模板语义仍以
    当前源码目录为准，但 UUID 和 Handle 身份沿用发布合同；没有对应合同的模板
    保持原样。异常：投影字段不完整时跳过该项，让上层目录校验给出稳定诊断。
    """

    by_workflow = {
        str(item.get("workflow_uuid")): item
        for item in persisted
        if isinstance(item, dict)
        and isinstance(item.get("workflow_uuid"), str)
        and isinstance(item.get("template"), dict)
    }
    overlaid_parent_keys: set[tuple[str, str]] = set()
    output_nodes: list[dict[str, Any]] = []
    handles_by_parent: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for handle in generated_handles:
        parent = handle.get("node_business_key")
        if isinstance(parent, (list, tuple)) and len(parent) == 2:
            handles_by_parent[(str(parent[0]), str(parent[1]))].append(dict(handle))

    output_handles: list[dict[str, Any]] = []
    for node in generated_nodes:
        name = node.get("name")
        workflow_uuid = (
            str(name)[len("workflow:") :]
            if isinstance(name, str) and name.startswith("workflow:")
            else None
        )
        contract = by_workflow.get(workflow_uuid or "")
        template = contract.get("template") if isinstance(contract, dict) else None
        persisted_uuid = contract.get("node_template_uuid") if isinstance(contract, dict) else None
        if not isinstance(template, dict) or not isinstance(persisted_uuid, str):
            output_nodes.append(dict(node))
            continue
        cloned = dict(node)
        cloned["uuid"] = persisted_uuid
        metadata = dict(node.get("meta_data") or {})
        persisted_metadata = template.get("meta_data")
        if isinstance(persisted_metadata, dict):
            merged_unilab = dict(metadata.get("unilab") or {})
            merged_unilab.update(dict(persisted_metadata.get("unilab") or {}))
            metadata.update(persisted_metadata)
            metadata["unilab"] = merged_unilab
            cloned["meta_data"] = metadata
        output_nodes.append(cloned)

        persisted_handles = contract.get("handles") if isinstance(contract, dict) else None
        if not isinstance(persisted_handles, list):
            continue
        node_business_key = (
            str(node.get("resource_template_uuid")),
            str(name),
        )
        generated_for_node = sorted(
            handles_by_parent.get(node_business_key, []),
            key=lambda item: (str(item.get("handle_key")), str(item.get("io_type")), str(item.get("uuid"))),
        )
        persisted_for_node = sorted(
            (dict(item) for item in persisted_handles if isinstance(item, dict)),
            key=lambda item: (str(item.get("handle_key")), str(item.get("io_type")), str(item.get("uuid"))),
        )
        if len(generated_for_node) != len(persisted_for_node):
            continue
        overlaid_parent_keys.add(node_business_key)
        for generated_handle, persisted_handle in zip(
            generated_for_node,
            persisted_for_node,
            strict=True,
        ):
            # RegistryTemplateProjectionStore resolves the parent from
            # ``node_business_key``. Preserve that generated identity while
            # carrying over the immutable UUID and persisted handle semantics.
            output_handles.append(
                {
                    **generated_handle,
                    **persisted_handle,
                    "node_business_key": generated_handle["node_business_key"],
                }
            )

    output_handles.extend(
        dict(handle)
        for handle in generated_handles
        if not (
            isinstance(handle.get("node_business_key"), (list, tuple))
            and len(handle["node_business_key"]) == 2
            and (
                str(handle["node_business_key"][0]),
                str(handle["node_business_key"][1]),
            ) in overlaid_parent_keys
        )
    )
    output_nodes.sort(key=lambda item: str(item.get("uuid")))
    output_handles.sort(key=lambda item: str(item.get("uuid")))
    return tuple(output_nodes), tuple(output_handles)


def get_center_runtime() -> Any:
    """返回已发布的中心运行时；启动中和关闭后返回空值。"""
    return getattr(_center_runtime, "runtime", _center_runtime)


def get_center_connection() -> Any:
    """返回连接生命周期所有者，尚未注册也可以读取配置状态。"""
    return _center_runtime if hasattr(_center_runtime, "status") else None


def get_workflow_service() -> Optional[WorkflowService]:
    """返回进程内已经装配的本地工作流权威。

    返回值：工作流运行时尚未装配时为 ``None``；本函数只读取现有单例，绝不
    隐式创建工作流任务权威（WorkflowTask Authority）。
    """

    return _service


def get_registry_template_projection() -> Optional[RegistryTemplateProjection]:
    """返回工站进程最近装配的设备注册表模板投影。

    返回值：尚未建立模板目录时为 ``None``；本函数只读取现有投影，不因上游
    Backend 模式改变工站内注册表所有权，也不隐式创建第二写权威。
    """

    return _template_projection


def shutdown_workflow_runtime() -> None:
    """停止并清除本进程拥有的完整工作流（Workflow）运行时组合。

    参数：无。
    返回：无；监视器、服务、模板投影和组合身份全部恢复为空。
    异常：清理任一运行时所有者失败时原样传播，并保留失败运行时供重试。
    """

    global _compiler, _compiler_rebuilder, _database_path
    global _editable_package_roots, _editable_source_discovery_plan, _failed_runtime
    global _monitor, _runtime_template_snapshot_provider, _service, _center_runtime
    global \
        _fixed_point_activation_enabled, \
        _source_monitor_enabled, \
        _template_projection, \
        _allow_unpublished_composite_sources
    with _lock:
        from unilabos.workflow.station_event_http import (
            shutdown_station_event_projection,
        )

        shutdown_station_event_projection()
        if _center_runtime is not None:
            _center_runtime.close()
            _center_runtime = None
        if _failed_runtime is not None:
            # 失败运行时是一个整体清理所有者；任一步再次失败都保留原对象和已完成
            # 标记，调用者可在外部条件修复后再次执行重置。
            _failed_runtime.cleanup()
            _failed_runtime = None
        if _monitor is not None:
            _monitor.stop()
        if _service is not None:
            _service.close()
        if _template_projection is not None:
            _template_projection.close()
        _monitor = None
        _service = None
        _database_path = None
        _compiler = None
        _compiler_rebuilder = None
        _runtime_template_snapshot_provider = None
        _editable_package_roots = ()
        _editable_source_discovery_plan = None
        _template_projection = None
        _allow_unpublished_composite_sources = False
        _source_monitor_enabled = True
        _fixed_point_activation_enabled = False


# 保留既有测试接缝名称；生产重启与退出统一调用上面的生命周期入口。
reset_workflow_service_for_test = shutdown_workflow_runtime


__all__ = [
    "compose_local_workflow_template_runtime",
    "compose_workflow_runtime",
    "get_registry_template_projection",
    "get_workflow_service",
    "reset_workflow_service_for_test",
    "setup_workflow_service",
    "shutdown_workflow_runtime",
]
