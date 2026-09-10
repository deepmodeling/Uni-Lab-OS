"""工作区本地工作流权威（Workflow Authority）的进程级组合根。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from unilabos.app.scheduler.inventory.resource_reference import (
    build_inventory_resource_reference_resolver,
)
from unilabos.registry.local_template_identity import (
    synchronize_local_template_identities,
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
from unilabos.workflow.composite import CompositeAuthoring
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
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

_lock = threading.RLock()
_service: Optional[WorkflowService] = None
_database_path: Optional[Path] = None
_monitor: Optional[WorkflowSourceMonitor] = None
_template_projection: Optional[RegistryTemplateProjection] = None
_compiler: Optional[AuthoringCompiler] = None
_compiler_rebuilder: Optional[Callable[[], AuthoringCompiler]] = None
_editable_package_roots: tuple[Path, ...] = ()
_editable_source_discovery_plan: Optional[EditableSourceDiscoveryPlan] = None
_source_monitor_enabled = True


@dataclass
class _RuntimeCleanupOwner:
    """持有部分启动失败后仍需重试清理的完整运行时资源。"""

    service: WorkflowService
    monitor: WorkflowSourceMonitor
    monitor_stopped: bool = False
    service_closed: bool = False

    def cleanup(self) -> None:
        """按监视器后服务的安全顺序幂等推进清理。

        参数：无。返回：全部资源关闭时无返回值；任一步失败原样抛出，并保留
        已完成步骤标记供下一次重置继续，而不是重复关闭或遗失所有权。
        """

        if not self.monitor_stopped:
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
    task_scheduler_bridge: Any,
) -> None:
    """按反向所有权清理尚未发布的工作流运行时组合。

    参数：``original_error`` 是必须保留的启动异常；``workflow_store`` 是待关闭的
    工作流存储（WorkflowStore）；``task_scheduler_bridge`` 是可能已注册调度监听器
    的唯一公共桥。返回无；每个清理异常只附加为原异常说明，不掩盖首个构造或
    恢复故障。
    """

    # ``owned_resources`` 按创建顺序列出，关闭时反向遍历，先注销桥再关闭存储。
    owned_resources = (
        ("工作流存储", workflow_store),
        ("工作流任务调度桥", task_scheduler_bridge),
    )
    for resource_name, resource in reversed(owned_resources):
        if resource is None:
            continue
        try:
            resource.close()
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
    scheduler: Optional[Any] = None,
    start_source_monitor: bool = True,
) -> WorkflowService:
    """装配工作区唯一的工作流权威、启动恢复和草稿监视。

    参数：``working_dir`` 决定现有工作流 SQLite 路径；``compiler`` 是可信工作流
    创作编译器；``compiler_rebuilder`` 在应用后原子刷新完整模板代际；
    ``editable_package_roots`` 是唯一允许发现源码的显式授权目录；
    ``editable_source_discovery_plan`` 是包目录（PackageCatalog）
    编译代际产生的预编译工作流源码（Workflow Source）计划；
    ``material_resolver`` 按稳定 UUID 读取本地物料权威摘要；``scheduler`` 是仅在
    本地调度模式装配的现有调度器（EdgeScheduler）；``start_source_monitor``
    仅供非工作区遗留入口保留逐源码监视，工作区必须传 ``False`` 并由统一文件
    世代监视器拥有刷新。
    返回：完成来源注册与启动恢复后发布的进程唯一工作流服务（WorkflowService）。
    异常：同时提供授权目录与预编译计划，或运行期间
    切换数据库、编译器、授权目录或来源计划时关闭式失败。
    """

    global _compiler, _compiler_rebuilder, _database_path
    global _editable_package_roots, _editable_source_discovery_plan, _failed_runtime
    global _monitor, _service, _source_monitor_enabled
    # 后端形态合同（Backend-shaped Contract）的定义/任务与遗留执行历史共享
    # ``workflow_history.db``，但继续使用相互独立的表。
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
            return _service
        # ``workflow_store`` 是本地标准工作流任务（WorkflowTask）/工作流节点作业
        # （WorkflowNodeJob）写模型；执行桥与应用服务必须共享同一实例。
        workflow_store = WorkflowStore(database_path)
        task_scheduler_bridge = None
        new_service: Optional[WorkflowService] = None
        new_monitor: Optional[WorkflowSourceMonitor] = None
        try:
            if scheduler is not None:
                task_scheduler_bridge = TaskSchedulerBridge(
                    workflow_store,
                    scheduler=scheduler,
                )
            new_service = WorkflowService(
                workflow_store,
                compiler=compiler,
                compiler_rebuilder=compiler_rebuilder,
                material_resolver=material_resolver,
                task_scheduler_bridge=task_scheduler_bridge,
            )
            # ``discovery_plan`` 是全量文件预校验结果；服务在单事务中注册后，
            # 才能恢复草稿并建立一致的监视基线。
            discovery_plan = (
                editable_source_discovery_plan
                if editable_source_discovery_plan is not None
                else discover_editable_sources(configured_roots)
            )
            new_service.replace_discovered_source_authorizations(discovery_plan)
            if editable_source_discovery_plan is not None:
                # managed-local 工作区把同代 PackageCatalog 当作激活权威；OS
                # 只有在子到父的组合依赖全部推进到固定点后才能发布 ready。
                new_service.activate_registered_sources_to_fixed_point()
            else:
                # 遗留显式目录入口继续保留候选/人工应用语义。
                new_service.recover_registered_sources()
            if task_scheduler_bridge is not None:
                # 只恢复没有结果不明作业的运行中任务；已成功动作
                # 仅重建 DAG 状态，不越过物理派发边界重放。
                task_scheduler_bridge.recover_active_tasks()
            if start_source_monitor:
                new_monitor = WorkflowSourceMonitor(new_service)
        except BaseException as startup_error:
            _cleanup_partial_composition(
                original_error=startup_error,
                workflow_store=workflow_store,
                task_scheduler_bridge=task_scheduler_bridge,
            )
            raise

        # 启动恢复已经完成，此处才一次发布进程内工作流权威及其完整组合身份。
        _service = new_service
        _database_path = database_path
        _compiler = compiler
        _compiler_rebuilder = compiler_rebuilder
        _editable_package_roots = configured_roots
        _editable_source_discovery_plan = editable_source_discovery_plan
        _monitor = new_monitor
        _source_monitor_enabled = bool(start_source_monitor)
        if new_monitor is None:
            return new_service
        try:
            new_monitor.start()
        except BaseException as start_error:
            # 监视线程未可靠启动时立即撤销公开权威，但完整资源所有权必须保留到
            # 停止和服务关闭都确认成功，不能让清理错误覆盖原始启动错误。
            _service = None
            _database_path = None
            _compiler = None
            _compiler_rebuilder = None
            _editable_package_roots = ()
            _editable_source_discovery_plan = None
            _monitor = None
            _source_monitor_enabled = True
            cleanup_owner = _RuntimeCleanupOwner(new_service, new_monitor)
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
) -> tuple[WorkflowService, RegistryTemplateProjection]:
    """装配本地模板权威、F02 创作编译器与工作流服务。

    参数说明：``working_dir`` 决定现有 ``workflow_history.db`` 路径；
    ``inventory_store`` 是同步并持有资源模板身份的 ``inventory.db`` 权威；
    ``registry`` 是原始注册表（Registry）或不可变注册表快照（Registry Snapshot）；
    ``scheduler`` 是仅在本地调度模式装配的既有调度器（EdgeScheduler）；
    ``editable_package_roots`` 是本次进程唯一授权的工作流源码（Workflow
    Source）目录 tuple；``editable_source_discovery_plan`` 是与注册表快照
    （Registry Snapshot）同代的预编译来源计划，存在时禁止再读
    ``package.yaml``；``start_source_monitor`` 仅允许遗留入口启动逐源码监视。
    返回：共享同一已发布目录代际的工作流服务
    （WorkflowService）与模板投影（Template Projection）。异常：注册表快照构造、
    本地模板身份同步或模板投影失败时统一抛出
    ``RegistryTemplateProjectionError``，不发布工作流权威（Workflow Authority）；
    若失败前已经创建模板投影，则先关闭其持有的工作流存储连接再传播异常。
    """

    global _template_projection
    database_path = Path(working_dir).resolve() / "workflow_history.db"
    with _lock:
        if _template_projection is not None:
            # 已发布的模板投影必须复用原编译器和授权目录组合身份。
            service = compose_workflow_runtime(
                working_dir,
                compiler=_compiler,
                compiler_rebuilder=_compiler_rebuilder,
                editable_package_roots=editable_package_roots,
                editable_source_discovery_plan=editable_source_discovery_plan,
                start_source_monitor=start_source_monitor,
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
        resolve_resource_template_identity = synchronize_local_template_identities(
            inventory_store=inventory_store,
            registry_snapshot=registry_snapshot,
        )

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
            raise TypeError(
                "工作流源码授权目录与预编译发现计划不能同时提供"
            )
        publication_plan = (
            editable_source_discovery_plan
            if editable_source_discovery_plan is not None
            else discover_editable_sources(configured_roots)
        )
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
        publication_store = WorkflowStore(database_path)
        published_generation: PublishedWorkflowGeneration | None = None

        def extend_template_generation(
            base_nodes: Any,
            base_handles: Any,
        ) -> tuple[Any, Any]:
            """在同一模板替换事务候选中追加全部已发布工作流合同。

            参数：``base_nodes``/``base_handles`` 是本次注册表编译出的设备与框架
            模板全集。返回：只含已发布工作流的节点和连接点候选。异常：来源或
            应用快照不一致时转换为 ``RegistryTemplateProjectionError``。
            """

            del base_handles
            nonlocal published_generation
            try:
                generation = build_published_workflow_generation(
                    registrations=active_registrations,
                    snapshot_provider=publication_store,
                    base_node_templates=base_nodes,
                )
            except PublishedWorkflowGenerationError as error:
                raise RegistryTemplateProjectionError(str(error)) from error
            published_generation = generation
            return generation.node_templates, generation.handle_templates

        projection = RegistryTemplateProjection(
            publication_store,
            authority_id="local",
            resource_template_identity_resolver=resolve_resource_template_identity,
            generation_extension=extend_template_generation,
        )
        try:
            # ``resource_reference_resolver`` 只读取 C3 已提交的本地资源图物料事实，
            # 让物料来源（MaterialSource）和普通动作共享同一业务 ID→UUID 规则。
            resource_reference_resolver = (
                build_inventory_resource_reference_resolver(inventory_store)
            )

            def rebuild_compiler() -> AuthoringCompiler:
                """刷新完整模板代际并返回与该代际绑定的新创作编译器。

                参数：无。返回：共享同一不可变目录快照的编译器与组合展开端口。
                异常：注册表、发布来源或模板替换失败时原样传播，调用服务据此
                关闭陈旧编译入口。
                """

                snapshot = projection.refresh(registry_snapshot)
                if published_generation is None:
                    raise RegistryTemplateProjectionError(
                        "已发布工作流目录扩展未执行"
                    )
                return WorkflowAuthoringEngine(
                    catalog=snapshot,
                    resource_reference_resolver=resource_reference_resolver,
                    composite_authoring=CompositeAuthoring(
                        snapshot_provider=publication_store,
                        catalog=snapshot,
                        resolver=published_generation.source_catalog,
                    ),
                )

            compiler = rebuild_compiler()
            service = compose_workflow_runtime(
                working_dir,
                compiler=compiler,
                compiler_rebuilder=rebuild_compiler,
                editable_package_roots=editable_package_roots,
                editable_source_discovery_plan=editable_source_discovery_plan,
                material_resolver=resolve_material_identity,
                scheduler=scheduler,
                start_source_monitor=start_source_monitor,
            )
        except BaseException:
            projection.close()
            raise
        _template_projection = projection
        return service, projection


def get_workflow_service() -> Optional[WorkflowService]:
    """返回进程内已经装配的本地工作流权威。

    返回值：工作流运行时尚未装配时为 ``None``；本函数只读取现有单例，绝不
    隐式创建工作流任务权威（WorkflowTask Authority）。
    """

    return _service


def get_registry_template_projection() -> Optional[RegistryTemplateProjection]:
    """返回本地模式最近装配的设备注册表模板投影。

    返回值：尚未建立本地模板权威时为 ``None``；后端控制（Backend-controlled）模式不得用
    此函数隐式创建第二写权威。
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
    global _monitor, _service, _source_monitor_enabled, _template_projection
    with _lock:
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
        _editable_package_roots = ()
        _editable_source_discovery_plan = None
        _template_projection = None
        _source_monitor_enabled = True


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
