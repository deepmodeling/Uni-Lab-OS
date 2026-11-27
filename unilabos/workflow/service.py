"""本地后端形态工作流权威（Backend-shaped Workflow Authority）的应用服务。"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError

from unilabos.app.runtime_capabilities import (
    get_runtime_capabilities,
    is_workflow_visible,
)
from unilabos.persistence.recovery_mode import EvidenceOnlyError
from unilabos.workflow.authoring_ast import parse_authoring_source
from unilabos.workflow.authoring_candidate_hash import (
    AuthoringCandidateHashError,
    compute_authoring_candidate_hash,
)
from unilabos.workflow.authoring_graph_semantics import candidate_changeset
from unilabos.workflow.authoring_identity import declared_workflow_uuid
from unilabos.workflow.authoring_python import _safe_identifier
from unilabos.workflow.candidate_validation import (
    CandidateBundleError,
    validate_candidate_bundle,
)
from unilabos.workflow.catalog_dependent_authoring_refresh import (
    CatalogAuthoringGenerationTracker,
)
from unilabos.workflow.catalog_withdrawal import CatalogWithdrawals
from unilabos.workflow.composite_contract_refresh import (
    CompositeContractRefreshPending,
    graph_references_composite_child,
    refresh_published_composite_invocations,
)
from unilabos.workflow.composite_invocation import (
    CompositeInvocationInvalid,
    _remap_control_references,
    _remap_nested_composite_metadata,
    compile_composite_invocation,
)
from unilabos.workflow.definition_edit import (
    WorkflowDefinitionInvalid,
    duplicate_graph,
)
from unilabos.workflow.definition_edit import (
    create_edge as build_workflow_edge,
)
from unilabos.workflow.definition_edit import (
    create_node as build_workflow_node,
)
from unilabos.workflow.definition_edit import (
    duplicate_node as build_duplicated_node,
)
from unilabos.workflow.definition_edit import (
    patch_node as build_patched_node,
)
from unilabos.workflow.device_action_run import (
    DeviceActionRunConflict,
    DeviceActionRunInputError,
    DeviceActionRunService,
    DeviceActionRunUnavailable,
)
from unilabos.workflow.domain_source_target import (
    DomainWorkflowSourceError,
    DomainWorkflowSourceTarget,
)
from unilabos.workflow.event_reader import DurableEventReader
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.graph_validation import GraphValidationError
from unilabos.workflow.intervention import WorkflowInterventionStore
from unilabos.workflow.job_evidence import JobEvidenceStore
from unilabos.workflow.manual_confirmation import ManualConfirmationStore
from unilabos.workflow.models import (
    CandidateChangeset,
    CandidateCompilation,
    CandidateDiagnostic,
    CandidateSourceMapEntry,
    WorkflowEdgeWrite,
    WorkflowInventoryRequirementWrite,
    WorkflowNodeWrite,
    WorkflowTaskPriority,
    normalize_json_array,
    normalize_json_object,
    validate_uuid,
)
from unilabos.workflow.operation_category import (
    OperationCategoryCatalog,
    OperationCategoryError,
    default_operation_categories,
    legacy_operation_category_uuid,
)
from unilabos.workflow.publication_catalog import (
    WorkflowPublicationCatalog,
    WorkflowPublicationCatalogError,
)
from unilabos.workflow.published_contract import (
    PublishedContractConflict,
    PublishedContractInvalid,
    PublishedWorkflowContractStore,
    published_contract_semantic_hash,
    published_graph_semantic_hash,
)
from unilabos.workflow.python_workflow_import import (
    PythonWorkflowImportError,
    validate_python_workflow_import,
)
from unilabos.workflow.run_preflight import (
    build_batch_run_preflight_report,
    build_run_preflight_report,
    unavailable_material_source_checks,
)
from unilabos.workflow.source_coordinates import source_ranges_fit
from unilabos.workflow.source_discovery import (
    EditableSourceDiscoveryPlan,
    EditableSourceRegistration,
)
from unilabos.workflow.source_workspace import (
    NO_EXPECTED_HASH as _NO_EXPECTED_HASH,
)
from unilabos.workflow.source_workspace import (
    SourceWorkspaceConflict,
    SourceWorkspaceError,
    pin_package_roots,
    read_registered_source,
    registered_source_signature,
    validate_source_registration,
    write_registered_source,
)
from unilabos.workflow.station_workflow_submission import (
    StationWorkflowSubmission,
    StationWorkflowSubmissionInvalid,
    prepare_station_workflow_submission,
)
from unilabos.workflow.store import (
    StoreAdmissionRejected,
    StoreAuthoringConflict,
    StoreConflict,
    StoreNotFound,
    StoreRevisionConflict,
    WorkflowStore,
    utc_now,
)
from unilabos.workflow.task_input import (
    PreparedTaskInput,
    SiteSelectionResolver,
    TaskInputError,
    prepare_task_input,
)
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridgeError
from unilabos.workflow.workflow_type import (
    WORKFLOW_TYPE_EXPERIMENT_OPERATION,
    normalize_workflow_type,
)

logger = logging.getLogger(__name__)

_WORKFLOW_STATUS_SOURCE = "source"
_WORKFLOW_STATUS_PUBLISHED = "published"
_OPERATION_CATEGORY_META_KEY = "operation_category_uuid"
_OPERATION_CATEGORY_UNSET = object()

_ERRORS = {
    "transport_workflow_invalid": (409, "搬运许可或工作流车辆占用合同无效，工站未创建任务"),
    "inventory_admission_failed": (409, "库存条件不满足，工站未创建任务"),
    "admission_retry_exhausted": (409, "工站未创建任务，本地恢复已达到总尝试次数上限"),
    "invalid_input": (
        400,
        "请求参数不符合接口要求，请检查必填字段、字段类型和 JSON 数据格式后重试",
    ),
    "read_only_mode": (
        403,
        "当前 Runtime 不允许修改工作流定义",
    ),
    "debug_not_allowed": (403, "当前 Runtime 不允许单步调试"),
    "debug_task_conflict": (409, "单步运行需要当前 Runtime 没有其他活动任务"),
    "device_action_not_allowed": (403, "当前 Runtime 不允许直接执行设备动作"),
    "preflight_failed": (
        409,
        "任务尚未创建：执行前检查未通过，请根据返回的检查项补齐前置条件后重试",
    ),
    "not_found": (
        404,
        "请求的工作流、任务、节点或其他资源不存在，可能已被删除或尚未创建",
    ),
    "conflict": (409, "请求与当前数据状态冲突，请刷新最新数据后再重试"),
    "workflow_not_found": (404, "工作流不存在或已被删除"),
    "draft_hash_conflict": (
        409,
        "草稿已被其他程序修改，请查看差异后再保存",
    ),
    "workflow_revision_conflict": (
        409,
        "工作流已在其他位置更新，请刷新并重新确认本次修改",
    ),
    "workflow_identity_mismatch": (
        409,
        "导入的 Python workflow_uuid 与当前工作流不一致",
    ),
    "candidate_hash_conflict": (
        409,
        "预览结果已变化，请重新检查 DAG 和源码差异",
    ),
    "template_catalog_conflict": (
        409,
        "设备动作模板已更新，请重新编译并检查工作流",
    ),
    "candidate_not_ready": (
        409,
        "当前草稿还没有生成可以应用的工作流，请先完成编译并修复编译错误",
    ),
    "draft_invalid": (422, "草稿存在错误，修复后才能应用"),
    "candidate_invalid": (422, "工作流校验失败，请检查节点、连线和输入输出"),
    "candidate_identity_conflict": (
        409,
        "节点或连线 UUID 已被其他工作流占用，请更新源码中的节点身份",
    ),
    "invalid_material_source": (400, "物料来源选择器不符合规范"),
    "material_flow_fan_out": (409, "同一个物料输出不能连接多个物理消费者"),
    "invalid_control_region": (400, "条件分支配置无效，请检查每条分支的节点"),
    "graph_not_executable": (400, "工作流图不能生成执行计划，请按提示修改后再保存"),
    "material_shortage_unhandled": (
        409,
        "启用支持补料的物料来源必须连接一个读取 sufficient 的条件节点，并配置有料和缺料两条非空分支",
    ),
    "material_template_mismatch": (409, "物料资源模板与消费者约束不兼容"),
    "template_catalog_unavailable": (
        503,
        (
            "设备动作目录尚未就绪或加载失败，暂时无法编译或运行工作流；"
            "请检查设备动作目录和 backend.log，待工作流运行时就绪后重试"
        ),
    ),
    "source_target_unavailable": (
        503,
        "当前没有唯一可写的领域包，无法保存工作流源码",
    ),
    "source_publication_failed": (
        500,
        "工作流源码写入领域包失败，请检查目录权限后重试",
    ),
    "source_identity_conflict": (
        409,
        "工作流 UUID 或 Python 文件名已被领域包中的其他工作流占用",
    ),
    "source_function_conflict": (
        409,
        "工作流函数名已被领域包中的其他工作流占用，请修改工作流名称",
    ),
    "invalid_composite_child_type": (
        422,
        "组合节点只能引用实验操作类型的工作流",
    ),
    "invalid_composite_child_status": (
        409,
        "只能引用当前修订已发布的实验操作",
    ),
    "internal_error": (
        500,
        "本地工作流服务处理失败，请查看 backend.log 中的具体错误后重试",
    ),
}
_HASH_TOKEN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ISOLATED_WORKSPACE_ACTIVATION_ERRORS = frozenset(
    {"candidate_invalid", "draft_invalid"}
)
_WORKFLOW_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "name",
    "tags",
    "workflow_type",
    "revision",
    "description",
}
_NODE_TEMPLATE_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
    "description",
    "class",
    "schema",
    "icon",
    "header",
    "footer",
}
_HANDLE_TEMPLATE_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
    "description",
    "data_source",
    "data_key",
}
_WORKFLOW_REQUIRED_READ_FIELDS = _WORKFLOW_READ_FIELDS - {
    "description",
    "workflow_type",
}
_NODE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_uuid",
    "name",
    "status",
    "type",
    "pose",
    "param",
    "execution_policy",
    "disabled",
    "minimized",
}
_EDGE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "source_node_uuid",
    "target_node_uuid",
    "source_handle_uuid",
    "target_handle_uuid",
}
_NODE_TEMPLATE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
}
_HANDLE_TEMPLATE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
}
_INVENTORY_REQUIREMENT_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_uuid",
    "consume_node_uuid",
    "requirement_key",
    "target_type",
    "required_quantity",
    "quantity_unit",
    "allow_split",
    "sort_order",
}


class WorkflowError(RuntimeError):
    """面向前端的稳定 Workflow 错误。"""

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ):
        """创建稳定业务错误并允许安全的可行动消息覆盖。

        参数：``code`` 是公共错误码，``message`` 可提供不含源码内容的具体提示。
        返回：无。异常：未知错误码抛出 ``KeyError``，保持开发期失败关闭。
        """

        status, default_message = _ERRORS[code]
        message = message or default_message
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details or {})


class WorkflowConflict(WorkflowError):
    pass


class AuthoringCompiler(Protocol):
    compiler_version: str
    template_catalog_fingerprint: str

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation: ...


class WorkflowTaskSchedulerBridge(Protocol):
    """普通任务与设备单动作共享的工作流任务（WorkflowTask）调度端口。"""

    def submit(self, task: dict[str, Any]) -> dict[str, Any]:
        """提交已持久任务。

        参数：``task`` 是标准工作流任务（WorkflowTask）投影。返回：同步推进后的
        任务/作业聚合。异常：编译、准入或派发前投影失败时由实现抛稳定桥接错误。
        """

        ...

    def update_pending_job_param(
        self,
        task_uuid: str,
        job_uuid: str,
        param: Mapping[str, Any],
    ) -> dict[str, Any]:
        """修改已暂停任务中尚未派发作业的参数。"""

        ...

    def close(self) -> None:
        """幂等释放调度生命周期监听器；参数无，返回无。"""

        ...

    def step(
        self,
        task_uuid: str,
        *,
        target_node_uuid: str | None = None,
    ) -> dict[str, Any]:
        """让暂停的单步任务放行一个节点并返回调度摘要。"""

        ...

    def cancel(
        self,
        task_uuid: str,
        *,
        command_uuid: str | None = None,
    ) -> dict[str, Any]:
        """持久化取消命令并返回当前任务聚合；物理终态可随后异步收敛。"""

        ...

    def decide_manual_confirmation(
        self,
        job_uuid: str,
        *,
        action: str,
    ) -> dict[str, Any]:
        """批准人工确认，或通过既有 Task Cancel 流程拒绝。"""

        ...

    def request_uncertain_resolution(
        self,
        job_uuid: str,
        *,
        reason: str,
        device_command_id: str,
    ) -> dict[str, Any]:
        """请求 Edge 证明未知设备作业已经取消。"""

        ...

    def reconcile_job_quantity(
        self, job_uuid: str, *, consumptions: Sequence[Mapping[str, Any]], reason: str
    ) -> dict[str, Any]:
        """更正已执行作业的实际数量并只重放结算结果。"""

        ...

    def settle_failed_material_transfer(
        self,
        job_uuid: str,
        *,
        actual_change_set: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """提交失败转运的实际物料位置并完成物理结算。"""

        ...

    def reschedule(self) -> None:
        """在外部持久事实释放后唤醒本地调度循环。"""

        ...

    def unlock_resources(
        self,
        task_uuid: str,
        *,
        command_uuid: str,
        reason: str,
    ) -> dict[str, Any]:
        """整组释放操作员已确认安全的异常终态 Task 资源。"""

        ...


class WorkflowInterventionDelivery(Protocol):
    """本地设备异常决定的投递端口。"""

    def add_error_decision_required_listener(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> None: ...

    def remove_error_decision_required_listener(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> None: ...

    def resolve_error_decision(
        self,
        decision_id: str,
        decision: dict[str, Any],
    ) -> bool: ...


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _mtime_rfc3339(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class WorkflowService:
    """协调进程内工作流定义、持久运行事实与 package 源码。"""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        definition_store: WorkflowStore | None = None,
        compiler: AuthoringCompiler | None = None,
        compiler_rebuilder: Callable[[], AuthoringCompiler] | None = None,
        source_target: DomainWorkflowSourceTarget | None = None,
        material_resolver: Callable[[str], dict[str, Any] | None] | None = None,
        resource_reference_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
        site_selection_resolver: SiteSelectionResolver | None = None,
        device_preflight: Callable[[Mapping[str, Any]], Mapping[str, Any]]
        | None = None,
        task_scheduler_bridge: WorkflowTaskSchedulerBridge | None = None,
    ) -> None:
        """装配本地工作流应用服务。

        参数：``store`` 是持久 Task/Job 运行事实库；``definition_store`` 是进程内
        工作流定义目录，省略时仅为兼容隔离测试而复用 ``store``；``compiler``
        负责编译可信工作流源码；
        ``compiler_rebuilder`` 在成功应用后重建包含已发布工作流的完整目录代际；
        ``source_target`` 是接口创建及 JSON/Python 导入默认写入的唯一领域包，
        后续图、元数据和实验操作类别编辑也回写该目标；省略时仅保留无领域
        工作区的遗留内存行为和三个只读默认类别；
        ``material_resolver`` 按物料 UUID 读取活动物料身份，供设备单动作运行
        （DeviceActionRun）关闭式校验；``task_scheduler_bridge`` 把普通工作流任务
        （WorkflowTask）与首次创建的设备单动作聚合交给同一本地调度器。返回无。
        异常：编译器重建器不可调用时抛出 ``TypeError``。
        """

        self._store = store
        self._center_material_input_lock = threading.Lock()
        self._center_material_input_authority: Any = None
        self._definition_store = (
            definition_store if definition_store is not None else store
        )
        # 新增能力仓储按需初始化，避免“只创建任务”的调用方被迫实现发布、证据和
        # 人工处置所需的 SQLite 事务接口；第一次使用相应能力时才补齐其物理表。
        self._published_contracts: PublishedWorkflowContractStore | None = None
        self._job_evidence: JobEvidenceStore | None = None
        self._manual_confirmations: ManualConfirmationStore | None = None
        self._interventions: WorkflowInterventionStore | None = None
        self._intervention_delivery: WorkflowInterventionDelivery | None = None
        self._event_reader = DurableEventReader(store)
        self._definition_event_lock = threading.Lock()
        self._definition_event_cursor = 0
        self.compiler = compiler
        if compiler_rebuilder is not None and not callable(compiler_rebuilder):
            raise TypeError("compiler_rebuilder 必须是可调用对象")
        self._compiler_rebuilder = compiler_rebuilder
        self._source_target = source_target
        self._operation_category_catalog = (
            None
            if source_target is None
            else OperationCategoryCatalog(
                package_root=source_target.package_root,
                package_root_identity=source_target.package_root_identity,
            )
        )
        self._publication_catalog = (
            None
            if source_target is None
            else WorkflowPublicationCatalog(
                package_root=source_target.package_root,
                package_root_identity=source_target.package_root_identity,
            )
        )
        # ``_operation_category_lock`` 线性化类别删除与工作流类别引用写入。类别
        # 文件和工作流定义位于不同存储，不能依赖单个 SQLite/文件事务维护引用；
        # 同一服务进程必须保证“先检查引用再删除”和“先验证类别再写入”互斥。
        self._operation_category_lock = threading.RLock()
        self._authoring_function_name_lock = threading.RLock()
        self._device_action_runs = DeviceActionRunService(
            store,
            definition_store=self._definition_store,
            material_resolver=material_resolver,
            site_selection_resolver=site_selection_resolver,
        )
        self._material_resolver = material_resolver
        self._resource_reference_resolver = resource_reference_resolver
        self._site_selection_resolver = site_selection_resolver
        self._device_preflight = device_preflight
        # ``_task_scheduler_bridge`` 是普通任务与设备单动作共享的唯一监听器所有者；
        # 工站调度进程必须装配，纯工作流创作或隔离读取场景才允许保持空。
        self._task_scheduler_bridge = task_scheduler_bridge
        self._attention_notifier: Callable[[], None] | None = None
        from unilabos.workflow.error_handling import ErrorHandlingService

        self.error_handling = (
            ErrorHandlingService(store, task_scheduler_bridge, self._device_action_runs)
            if task_scheduler_bridge is not None else None
        )
        self._locks_guard = threading.Lock()
        self._capability_store_lock = threading.Lock()
        self._authoring_locks: dict[str, threading.RLock] = {}
        # ``_source_authorization_replacement_lock`` 串行化完整授权集合替换，使“当前
        # 集合 ∪ 新集合”的锁快照在取得所有创作锁前不会被另一替换命令改变。
        self._source_authorization_replacement_lock = threading.RLock()
        # ``_active_source_workflow_uuids`` 只表达本次进程启动配置授权的工作流
        # 源码（Workflow Source）；注册行和创作状态都只存在于本次进程目录。
        self._active_sources_lock = threading.RLock()
        self._active_source_workflow_uuids: frozenset[str] = frozenset()
        # 依赖关系来自与 Registry Snapshot 同代的静态 Package Catalog，只用于
        # managed-local 冷启动按子到父分层激活；交互 Apply 仍保持原有单项语义。
        self._active_source_dependencies: dict[str, frozenset[str]] = {}
        # 冷启动单线程批次提交期间暂缓逐项目录重建；这是服务内部生命周期状态，
        # 不暴露到公共 Apply API，避免调用方绕开每次交互应用后的目录一致性刷新。
        self._workspace_activation_batch = False
        # 自动激活候选由当前线程刚刚编译并安装进内存目录；线程本地授权让
        # Apply 复用这个编译事实，同时保持交互 Apply 的独立重编译复核。
        self._workspace_activation_context = threading.local()
        # 冷启动发布合同只把“空骨架应采用的修订基线”保存在内存。映射值为
        # ``(workflow_revision, source_draft_hash, semantic_graph_hash)``；仅固定点
        # 激活且源码字节与发布时一致、编译出的冻结图也一致时允许 Apply 保持该
        # 修订，成功后立即消费。普通交互 Apply 永远不读取此映射，避免绕过修订
        # 推进语义。
        self._bootstrap_published_revisions_lock = threading.RLock()
        self._bootstrap_published_revisions: dict[str, tuple[int, str, str]] = {}
        # ``_catalog_generation_tracker`` 隐藏本进程目录编译基线、变化判定和源码
        # 观测签名组合；工作流服务只在编译事务接缝提交已验证指纹。
        self._catalog_generation_tracker = CatalogAuthoringGenerationTracker()
        # 发布、删除意图和全目录导出共用锁，目录读取不跨越一次下架提交。
        self._publication_snapshot_lock = threading.RLock()
        self._catalog_withdrawals: CatalogWithdrawals | None = None

    def set_attention_notifier(self, notifier: Callable[[], None] | None) -> None:
        """Attach the center WSS callback without making workflow execution depend on WSS."""

        self._attention_notifier = notifier
        if self._task_scheduler_bridge is not None:
            setter = getattr(self._task_scheduler_bridge, "set_attention_notifier", None)
            if callable(setter):
                setter(notifier)

    def _notify_attention_changed(self) -> None:
        if self._attention_notifier is not None:
            try:
                self._attention_notifier()
            except Exception:
                # 通知失败不能把已提交事实伪装成业务失败。
                logger.exception("中心事项快照唤醒失败，保留已提交事实")

    def center_attention_state(self) -> tuple[Any, ...]:
        """读取原生中心事项的小状态；不读取交接或物料完整诊断。"""
        return self._store.center_attention_state()

    def center_attention_snapshot(self) -> dict[str, Any]:
        """Return the current local facts needed for the center attention snapshot."""

        return self._store.center_attention_snapshot()

    def _withdrawals(self) -> CatalogWithdrawals:
        if self._catalog_withdrawals is None:
            self._catalog_withdrawals = CatalogWithdrawals(self._store)
        return self._catalog_withdrawals

    def export_published_workflow_catalog(self) -> dict[str, Any]:
        """原子导出完整公开目录和显式下架墓碑，不从分页或源码缺失推断删除。"""

        with self._publication_snapshot_lock:
            tombstones = self._withdrawals().snapshot()
            withdrawn = {item["workflow_uuid"] for item in tombstones}
            contracts = [item for item in self._published_contract_store().snapshot_latest()
                         if item["workflow_uuid"] not in withdrawn]
            return {"contracts": contracts, "tombstones": tombstones}

    def _published_contract_store(self) -> PublishedWorkflowContractStore:
        with self._capability_store_lock:
            if self._published_contracts is None:
                self._published_contracts = PublishedWorkflowContractStore(
                    self._definition_store
                )
        return self._published_contracts

    @property
    def runtime_store(self) -> WorkflowStore:
        """返回工站 Task/Job 运行事实库，供同进程基础设施装配发件箱消费者。"""

        return self._store

    def _public_workflow_with_status(
        self,
        workflow: Mapping[str, Any],
    ) -> dict[str, Any]:
        """给工作流公共读模型补充稳定的源码/已发布状态。

        参数：``workflow`` 是定义仓储返回的工作流行投影。返回：不修改输入的
        工作流副本，提取顶层 ``operation_category_uuid`` 并增加 ``status``；
        只有最新发布合同的
        ``workflow_revision`` 与当前工作流 ``revision`` 相同时才返回
        ``published``，其余情况统一返回 ``source``。同时返回
        ``published_revision``（中心目录当前可见合同的工作流修订，没有则为
        ``None``）和 ``has_unpublished_changes``（有可见合同但当前修订未发布）。
        异常：合同仓储的数据库读取错误原样传播；没有发布合同不视为异常。
        """

        projected = dict(workflow)
        meta_data = dict(projected.get("meta_data") or {})
        category_was_explicit = _OPERATION_CATEGORY_META_KEY in meta_data
        category_uuid = meta_data.pop(_OPERATION_CATEGORY_META_KEY, None)
        if (
            not category_was_explicit
            and projected.get("workflow_type") == WORKFLOW_TYPE_EXPERIMENT_OPERATION
        ):
            category_uuid = legacy_operation_category_uuid(projected.get("tags"))
        projected["meta_data"] = meta_data
        projected["operation_category_uuid"] = category_uuid
        identity = str(projected["uuid"])
        latest_contract = self._published_contract_store().latest_for_workflow(identity)
        if self._publication_catalog is None:
            is_currently_published = latest_contract is not None and int(
                latest_contract["workflow_revision"]
            ) == int(projected["revision"])
        elif not self._has_active_source(identity):
            # 领域包发布目录是当前来源授权的权威边界。内存合同表会保留不可变
            # 历史，不能因为撤权后工作流修订仍相同，就把孤儿定义继续标成当前
            # published；否则父工作流和控制台都会重新暴露已撤销来源。
            is_currently_published = False
        else:
            publication = self._publication_catalog.latest_for_workflow(identity)
            source = self._read_source(self._registration(identity))
            authoring = self._definition_store.get_authoring_record(identity)
            applied_source = authoring.get("applied_source")
            diagnostics = authoring.get("diagnostics")
            # 发布目录中的 ``source_draft_hash`` 只是持久化的发布事实，不能
            # 单独把任意后来改写的源码标成 published。当前文件还必须已经被
            # 本进程的作者权威成功应用到同一修订；否则即使有人同步篡改了目录
            # 中的源码摘要，draft_invalid/unapplied 工作流也只能显示 source。
            authoring_matches_source = (
                source is not None
                and authoring.get("observed_draft_hash") == source["draft_hash"]
                and isinstance(applied_source, Mapping)
                and applied_source.get("workflow_revision")
                == int(projected["revision"])
                and authoring.get("candidate") is None
                and authoring.get("writeback_status") == "settled"
                and isinstance(diagnostics, list)
                and not any(
                    isinstance(item, Mapping)
                    and str(item.get("severity", "")).lower() == "error"
                    for item in diagnostics
                )
            )
            is_currently_published = (
                latest_contract is not None
                and int(latest_contract["workflow_revision"]) == int(projected["revision"])
                and publication is not None
                and source is not None
                and publication["contract"]["uuid"] == latest_contract["uuid"]
                and publication["source_draft_hash"] == source["draft_hash"]
                and authoring_matches_source
            )
        projected["status"] = (
            _WORKFLOW_STATUS_PUBLISHED
            if is_currently_published
            else _WORKFLOW_STATUS_SOURCE
        )
        # 与 ``export_published_workflow_catalog`` 同口径：下架的工作流不再向中心
        # 提供合同，因此也不返回发布修订。
        published_revision = (
            None
            if latest_contract is None or self._withdrawals().contains(identity)
            else int(latest_contract["workflow_revision"])
        )
        projected["published_revision"] = published_revision
        projected["has_unpublished_changes"] = (
            published_revision is not None and not is_currently_published
        )
        return projected

    def _job_evidence_store(self) -> JobEvidenceStore:
        with self._capability_store_lock:
            if self._job_evidence is None:
                self._job_evidence = JobEvidenceStore(self._store)
        return self._job_evidence

    def _manual_confirmation_store(self) -> ManualConfirmationStore:
        with self._capability_store_lock:
            if self._manual_confirmations is None:
                self._manual_confirmations = ManualConfirmationStore(self._store)
        return self._manual_confirmations

    def _intervention_store(self) -> WorkflowInterventionStore:
        with self._capability_store_lock:
            if self._interventions is None:
                self._interventions = WorkflowInterventionStore(self._store)
        return self._interventions

    def bind_intervention_delivery(
        self,
        delivery: WorkflowInterventionDelivery,
    ) -> None:
        """把设备异常报告与决定投递接到持久工作流干预。

        参数：``delivery`` 是当前本地设备执行端口。返回无。绑定后立即重投此前
        已选但未明确接受的决定；投递仍不可达时保留 ``unknown``，不会阻止服务
        启动，也不会伪造设备已恢复执行。
        """

        self._intervention_delivery = delivery
        delivery.add_error_decision_required_listener(
            self.open_workflow_intervention_from_report
        )
        for intervention in self._intervention_store().list_replayable_selected():
            if not self._deliver_workflow_intervention(intervention):
                logger.warning(
                    "工作流干预恢复投递仍不可达 intervention_uuid=%s job_uuid=%s",
                    intervention["uuid"],
                    intervention["workflow_node_job_uuid"],
                )

    # 工作流（Workflow）与图（Graph） -------------------------------------

    def list_operation_categories(self) -> list[dict[str, Any]]:
        """列出领域包中的全部实验操作类别。

        参数：无。返回：按展示顺序排列的类别列表；未配置领域包或类别文件时返回
        三个产品默认类别。异常：领域包类别文件损坏或目录不安全时关闭式失败。
        """

        if self._operation_category_catalog is None:
            return default_operation_categories()
        try:
            return self._operation_category_catalog.list_categories()
        except OperationCategoryError as error:
            self._raise_operation_category_error(error)

    def get_operation_category(self, category_uuid: str) -> dict[str, Any]:
        """按 UUID 读取一个实验操作类别。

        参数：``category_uuid`` 是稳定类别身份。返回：类别读模型。异常：身份
        非法、类别不存在或领域包配置不可用时映射为公共工作流错误。
        """

        if self._operation_category_catalog is None:
            try:
                identity = validate_uuid(category_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
            category = next(
                (
                    item
                    for item in default_operation_categories()
                    if item["uuid"] == identity
                ),
                None,
            )
            if category is None:
                raise WorkflowError("not_found")
            return category
        try:
            return self._operation_category_catalog.get_category(category_uuid)
        except OperationCategoryError as error:
            self._raise_operation_category_error(error)

    def create_operation_category(
        self,
        *,
        name: str,
        sort_order: int,
    ) -> dict[str, Any]:
        """在唯一领域包中新增实验操作类别。

        参数：``name`` 是显示名称，``sort_order`` 是升序展示权重。返回：新类别。
        异常：没有唯一领域包、名称重复、输入非法或 CAS 冲突时返回稳定业务错误。
        """

        catalog = self._writable_operation_category_catalog()
        try:
            return catalog.create_category(name=name, sort_order=sort_order)
        except OperationCategoryError as error:
            self._raise_operation_category_error(error)

    def update_operation_category(
        self,
        category_uuid: str,
        *,
        name: str | None = None,
        sort_order: int | None = None,
    ) -> dict[str, Any]:
        """修改类别名称或展示顺序，保持类别 UUID 不变。

        参数：``category_uuid`` 定位类别；名称和顺序至少提供一项。返回：更新后
        类别。异常：无唯一领域包、类别不存在、名称冲突或持久化失败时关闭写入。
        """

        catalog = self._writable_operation_category_catalog()
        try:
            return catalog.update_category(
                category_uuid,
                name=name,
                sort_order=sort_order,
            )
        except OperationCategoryError as error:
            self._raise_operation_category_error(error)

    def delete_operation_category(self, category_uuid: str) -> None:
        """删除没有被任何实验操作引用的类别。

        参数：``category_uuid`` 是稳定类别身份。返回：无。异常：仍被显式类别或
        兼容标签引用时返回冲突；身份、目录或并发错误按公共合同返回。
        """

        with self._operation_category_lock:
            category = self.get_operation_category(category_uuid)
            if self._operation_category_is_referenced(category["uuid"]):
                raise WorkflowConflict(
                    "conflict",
                    message="该类别仍被实验操作引用，请先调整对应实验操作",
                )
            catalog = self._writable_operation_category_catalog()
            try:
                catalog.delete_category(category["uuid"])
            except OperationCategoryError as error:
                self._raise_operation_category_error(error)

    def _writable_operation_category_catalog(self) -> OperationCategoryCatalog:
        """返回唯一领域包的可写类别目录。

        参数：无。返回：已在服务构造时绑定目录身份的深模块。异常：没有唯一领域
        包时抛 ``source_target_unavailable``，不把分类落到临时 SQLite 或内存。
        """

        if self._operation_category_catalog is None:
            raise WorkflowError("source_target_unavailable")
        return self._operation_category_catalog

    @staticmethod
    def _raise_operation_category_error(error: OperationCategoryError) -> None:
        """把类别目录内部错误映射为稳定工作流 HTTP 错误。

        参数：``error`` 是类别目录错误。返回：不返回。异常：总是抛出对应的
        ``WorkflowError`` 或 ``WorkflowConflict``，隐藏文件系统实现细节。
        """

        if error.code == "invalid_input":
            raise WorkflowError("invalid_input") from None
        if error.code == "not_found":
            raise WorkflowError("not_found") from None
        if error.code == "conflict":
            raise WorkflowConflict("conflict") from None
        raise WorkflowError(
            "source_publication_failed",
            message="实验操作类别配置不可用，请检查领域包目录",
        ) from None

    def _validate_workflow_operation_category(
        self,
        *,
        workflow_type: str,
        category_uuid: Any,
    ) -> str | None:
        """校验工作流类型与实验操作类别的组合。

        参数：``workflow_type`` 是规范工作流类型；``category_uuid`` 是可空类别
        身份。返回：规范 UUID 或 ``None``。异常：普通工作流携带类别、类型错误或
        类别不存在时关闭当前写入。
        """

        if category_uuid is None:
            return None
        if workflow_type != WORKFLOW_TYPE_EXPERIMENT_OPERATION or not isinstance(
            category_uuid, str
        ):
            raise WorkflowError("invalid_input")
        return self.get_operation_category(category_uuid)["uuid"]

    def _validated_operation_category_meta_data(
        self,
        *,
        workflow_type: str,
        meta_data: Mapping[str, Any],
        tags: Any,
    ) -> dict[str, Any]:
        """校验领域源码或导入图中的实验操作类别引用。

        参数：``workflow_type`` 是已规范化的工作流类型，``meta_data`` 是候选
        工作流根元数据，``tags`` 用于兼容旧前端的类别标签。返回：保留其他字段
        并规范类别 UUID 的新字典；显式 ``null`` 会作为“已清空”标记保留，避免
        旧标签再次回填；未显式提供类别的实验操作会把仍存在的旧标签类别固化为
        UUID。异常：普通工作流挂类别、类别已删除或身份非法时关闭候选写入。
        """

        normalized = dict(meta_data)
        if _OPERATION_CATEGORY_META_KEY not in normalized:
            if workflow_type != WORKFLOW_TYPE_EXPERIMENT_OPERATION:
                return normalized
            legacy_uuid = legacy_operation_category_uuid(tags)
            if legacy_uuid is None:
                return normalized
            normalized[_OPERATION_CATEGORY_META_KEY] = legacy_uuid
        category_uuid = self._validate_workflow_operation_category(
            workflow_type=workflow_type,
            category_uuid=normalized[_OPERATION_CATEGORY_META_KEY],
        )
        if workflow_type == "normal":
            normalized.pop(_OPERATION_CATEGORY_META_KEY, None)
        else:
            normalized[_OPERATION_CATEGORY_META_KEY] = category_uuid
        return normalized

    def _operation_category_is_referenced(self, category_uuid: str) -> bool:
        """检查类别是否仍被当前内存工作流目录引用。

        参数：``category_uuid`` 是已存在类别身份。返回：显式元数据或旧类别标签
        命中时为 ``True``。异常：定义目录读取错误原样传播。
        """

        page = 1
        while True:
            batch = self._definition_store.list_workflows(
                page=page,
                page_size=100,
                workflow_type=WORKFLOW_TYPE_EXPERIMENT_OPERATION,
            )
            if any(
                self._public_workflow_with_status(item).get("operation_category_uuid")
                == category_uuid
                for item in batch["items"]
            ):
                return True
            if page * 100 >= int(batch["total"]):
                return False
            page += 1

    def _list_workflows_by_operation_category(
        self,
        *,
        page: int,
        page_size: int,
        name: str,
        workflow_type: str | None,
        status: str | None,
        category_uuid: str,
    ) -> dict[str, Any]:
        """在既有工作流筛选结果上按实验操作类别分页。

        参数：页码、页长、名称、类型和状态与公开列表一致；``category_uuid`` 已
        验证存在。返回：类别过滤后的标准分页结构。异常：定义或发布目录读取错误
        原样传播。省略类别的旧请求不经过此兼容扫描路径。
        """

        matched: list[dict[str, Any]] = []
        source_page = 1
        while True:
            batch = self._definition_store.list_workflows(
                page=source_page,
                page_size=100,
                name=name,
                workflow_type=workflow_type,
                publication_status=status,
            )
            for item in batch["items"]:
                projected = self._public_workflow_with_status(item)
                if projected.get("operation_category_uuid") == category_uuid:
                    matched.append(projected)
            if source_page * 100 >= int(batch["total"]):
                break
            source_page += 1
        start = (page - 1) * page_size
        return {
            "items": matched[start : start + page_size],
            "total": len(matched),
            "page": page,
            "page_size": page_size,
        }

    def create_workflow(
        self,
        *,
        name: str,
        tags: list[Any],
        description: str | None,
        meta_data: dict[str, Any],
        workflow_type: str = "normal",
        operation_category_uuid: str | None | object = _OPERATION_CATEGORY_UNSET,
        workflow_uuid: str | None = None,
    ) -> dict[str, Any]:
        """创建普通工作流或实验操作定义。

        参数：名称、标签、描述和公开元数据沿用既有合同；``workflow_type`` 省略
        时为普通工作流；实验操作可通过 ``operation_category_uuid`` 归类，省略
        类别时兼容解析旧类别标签，显式 ``null`` 表示不分类；可选工作流 UUID
        主要供内部确定性创建。返回：含类型、类别和派生发布状态的工作流。异常：
        字段无效或旧标签对应类别已删除时映射为 ``invalid_input``，身份冲突映射
        为 ``conflict``。
        """

        try:
            name = name.strip()
            if not name:
                raise ValueError("workflow name must not be blank")
            identity = validate_uuid(workflow_uuid or str(uuid4()))
            tags = normalize_json_array(tags)
            meta_data = normalize_json_object(meta_data)
            # The managed-local source is authoritative, but the initial public
            # contract still has to reach the source generator.  Keep only the
            # two editable contract sections from the request; all other
            # ``unilab`` metadata is server-owned and must not cross this seam.
            requested_unilab = meta_data.get("unilab")
            requested_input_contract = (
                deepcopy(requested_unilab.get("input_contract"))
                if isinstance(requested_unilab, Mapping)
                and isinstance(requested_unilab.get("input_contract"), Mapping)
                else {"version": 1, "parameters": []}
            )
            requested_output_contract = (
                deepcopy(requested_unilab.get("output_contract"))
                if isinstance(requested_unilab, Mapping)
                and isinstance(requested_unilab.get("output_contract"), Mapping)
                else {"version": 1, "outputs": []}
            )
            if "parameters" not in requested_input_contract:
                requested_input_contract["parameters"] = []
            if "version" not in requested_input_contract:
                requested_input_contract["version"] = 1
            if "outputs" not in requested_output_contract:
                requested_output_contract["outputs"] = []
            if "version" not in requested_output_contract:
                requested_output_contract["version"] = 1
            workflow_type = normalize_workflow_type(workflow_type)
            public_meta_data = dict(meta_data)
            public_meta_data.pop("unilab", None)
            public_meta_data.pop(_OPERATION_CATEGORY_META_KEY, None)
            category_was_provided = (
                operation_category_uuid is not _OPERATION_CATEGORY_UNSET
            )
            if category_was_provided:
                public_meta_data[_OPERATION_CATEGORY_META_KEY] = operation_category_uuid
            with self._operation_category_lock:
                public_meta_data = self._validated_operation_category_meta_data(
                    workflow_type=workflow_type,
                    meta_data=public_meta_data,
                    tags=tags,
                )
                if self._source_target is not None:
                    if self.compiler is None:
                        raise WorkflowError("template_catalog_unavailable")
                    # ``registration`` 由请求中的工作流类型选择普通工作流或实验操作
                    # 源码目录；UUID 仍是唯一身份，目录不参与身份计算。
                    registration = self._source_target.registration(
                        workflow_uuid=identity,
                        file_name=DomainWorkflowSourceTarget.default_file_name(
                            identity
                        ),
                        workflow_type=workflow_type,
                    )
                    created_graph = self._commit_domain_workflow_creation(
                        registration=registration,
                        name=name,
                        tags=tags,
                        description=self._optional_text(description),
                        meta_data=public_meta_data,
                        nodes=[],
                        edges=[],
                        workflow_type=workflow_type,
                        input_contract=requested_input_contract,
                        output_contract=requested_output_contract,
                    )
                    return self._public_workflow_with_status(created_graph["workflow"])
                workflow = self._definition_store.create_workflow(
                    workflow_uuid=identity,
                    name=name,
                    tags=tags,
                    description=self._optional_text(description),
                    meta_data=public_meta_data,
                    workflow_type=workflow_type,
                )
            return self._public_workflow_with_status(workflow)
        except (ValueError, ValidationError):
            raise WorkflowError("invalid_input") from None
        except StoreConflict:
            raise WorkflowConflict("conflict") from None

    def get_workflow(self, workflow_uuid: str) -> dict[str, Any]:
        try:
            identity = validate_uuid(workflow_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self._public_workflow_with_status(
                self._definition_store.get_workflow(identity)
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def list_workflows(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        name: str = "",
        workflow_type: str | None = None,
        status: str | None = None,
        operation_category_uuid: str | None = None,
    ) -> dict[str, Any]:
        """分页查询工作流并支持类型、当前发布状态组合筛选。

        参数：页码、页长和名称沿用旧合同；可选类型为普通工作流或实验操作，可选状态为
        源码或已发布；类别筛选同时兼容旧标签分类。返回：筛选后当前页及总数，
        调用方省略新增条件时仍读取全部。异常：未知类型、状态或类别映射为稳定
        公共错误。
        """

        page, page_size = self._normalize_page(page, page_size)
        try:
            normalized_workflow_type = (
                None
                if workflow_type is None
                else normalize_workflow_type(workflow_type)
            )
        except ValueError:
            raise WorkflowError("invalid_input") from None
        if status not in {None, _WORKFLOW_STATUS_SOURCE, _WORKFLOW_STATUS_PUBLISHED}:
            raise WorkflowError("invalid_input")
        authoritative_status_filter = (
            status is not None and self._publication_catalog is not None
        )
        if status is not None:
            # 状态筛选依赖不可变发布合同；先建立其表结构，再让目录仓储在数据库
            # 内完成筛选和分页，避免先分页后过滤导致 ``has_more`` 错误。
            self._published_contract_store()
        if operation_category_uuid is not None:
            category_uuid = self.get_operation_category(operation_category_uuid)["uuid"]
            if authoritative_status_filter:
                return self._list_workflows_by_authoritative_status(
                    page=page,
                    page_size=page_size,
                    name=name,
                    workflow_type=normalized_workflow_type,
                    status=status,
                    category_uuid=category_uuid,
                )
            return self._list_workflows_by_operation_category(
                page=page,
                page_size=page_size,
                name=name,
                workflow_type=normalized_workflow_type,
                status=status,
                category_uuid=category_uuid,
            )
        if authoritative_status_filter:
            return self._list_workflows_by_authoritative_status(
                page=page,
                page_size=page_size,
                name=name,
                workflow_type=normalized_workflow_type,
                status=status,
            )
        result = self._definition_store.list_workflows(
            page=page,
            page_size=page_size,
            name=name,
            workflow_type=normalized_workflow_type,
            publication_status=status,
        )
        result["items"] = [
            self._public_workflow_with_status(item) for item in result["items"]
        ]
        return result

    def _list_workflows_by_authoritative_status(
        self,
        *,
        page: int,
        page_size: int,
        name: str,
        workflow_type: str | None,
        status: str,
        category_uuid: str | None = None,
    ) -> dict[str, Any]:
        """按源码/发布目录权威过滤工作流并在过滤后分页。

        参数：筛选条件与 ``list_workflows`` 相同，``category_uuid`` 是已校验的
        实验操作类别。返回：当前源码摘要、发布合同身份和工作流修订共同确认的
        精确分页结果。异常：定义仓储、发布目录或源码读取失败原样传播；不把
        数据库中仅按修订命中的陈旧合同当作当前已发布工作流。

        组合工作区的发布目录位于文件系统，SQLite 只能证明“曾经存在同修订合同”，
        不能证明当前源码仍与该合同绑定。因而不能先用 ``publication_status`` 在
        SQL 层分页再做状态投影，否则源码哈希漂移会同时污染 source/published 两个
        分页结果。这里先读取定义候选全集，再以同一公开状态投影过滤并重新切页。
        """

        matched: list[dict[str, Any]] = []
        source_page = 1
        while True:
            batch = self._definition_store.list_workflows(
                page=source_page,
                page_size=100,
                name=name,
                workflow_type=workflow_type,
            )
            for item in batch["items"]:
                projected = self._public_workflow_with_status(item)
                if projected.get("status") != status:
                    continue
                if (
                    category_uuid is not None
                    and projected.get("operation_category_uuid") != category_uuid
                ):
                    continue
                matched.append(projected)
            if source_page * 100 >= int(batch["total"]):
                break
            source_page += 1
        start = (page - 1) * page_size
        return {
            "items": matched[start : start + page_size],
            "total": len(matched),
            "page": page,
            "page_size": page_size,
        }

    def list_referencing_workflows(
        self,
        workflow_uuid: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分页返回当前已应用图中引用指定工作流的父工作流。

        参数：``workflow_uuid`` 是被引用工作流（通常为实验操作）的稳定身份；
        ``page`` 和 ``page_size`` 遵循工作流列表分页规则。返回：按名称和 UUID
        稳定排序的父工作流公开读模型、总数及分页信息。异常：被引用工作流身份
        非法或不存在时沿用工作流详情接口错误；并发删除的父工作流不会形成幽灵
        引用。引用事实只来自当前已应用图中的组合工作流调用元数据，不建立第二份
        持久索引。
        """

        child_uuid = self.get_workflow(workflow_uuid)["uuid"]
        page, page_size = self._normalize_page(page, page_size)
        referencing_workflows: list[dict[str, Any]] = []
        source_page = 1
        while True:
            batch = self._definition_store.list_workflows(
                page=source_page,
                page_size=100,
            )
            for workflow in batch["items"]:
                # ``parent_uuid`` 是候选父工作流身份；引用由该父图中组合调用节点
                # 的 child_workflow_uuid 证明，名称和标签不参与关系判断。
                parent_uuid = str(workflow["uuid"])
                if parent_uuid == child_uuid:
                    continue
                try:
                    parent_graph = self._definition_store.get_graph(parent_uuid)
                except StoreNotFound:
                    continue
                if graph_references_composite_child(
                    parent_graph,
                    child_workflow_uuid=child_uuid,
                ):
                    referencing_workflows.append(
                        self._public_workflow_with_status(workflow)
                    )
            if source_page * 100 >= int(batch["total"]):
                break
            source_page += 1

        referencing_workflows.sort(
            key=lambda item: (
                str(item.get("name") or "").casefold(),
                str(item["uuid"]),
            )
        )
        start = (page - 1) * page_size
        return {
            "items": referencing_workflows[start : start + page_size],
            "total": len(referencing_workflows),
            "page": page,
            "page_size": page_size,
        }

    def update_workflow(
        self,
        workflow_uuid: str,
        *,
        name: str,
        tags: list[Any],
        description: str | None,
        meta_data: dict[str, Any],
        workflow_type: str | None = None,
        operation_category_uuid: str | None | object = _OPERATION_CATEGORY_UNSET,
    ) -> dict[str, Any]:
        """更新工作流根字段并保持旧调用的类型兼容。

        参数：名称、标签、描述和元数据是既有完整更新值；``workflow_type`` 省略
        时沿用当前分类，显式提供时只能重复当前值，避免源码跨目录迁移；类别字段
        省略时保持原值，显式 ``null`` 时清空。返回：含类型、类别和派生状态的
        最新定义。异常：字段、类型转换、源码写回、修订或身份错误映射为稳定
        工作流错误；领域包来源的修改只有在 Python 固定点验证及 CAS 写回成功后
        才推进内存定义。
        """

        current = self.get_workflow(workflow_uuid)
        identity = current["uuid"]
        with self._authoring_lock(identity):
            current = self.get_workflow(identity)
            raw_current = self._definition_store.get_workflow(identity)
            raw_current_meta_data = dict(raw_current.get("meta_data") or {})
            current_category_was_explicit = (
                _OPERATION_CATEGORY_META_KEY in raw_current_meta_data
            )
            try:
                name = name.strip()
                if not name:
                    raise ValueError("workflow name must not be blank")
                tags = normalize_json_array(tags)
                public_meta_data = dict(normalize_json_object(meta_data))
                requested_unilab = public_meta_data.get("unilab")
                normalized_workflow_type = normalize_workflow_type(
                    workflow_type,
                    default=current["workflow_type"],
                )
                if normalized_workflow_type != current["workflow_type"]:
                    raise ValueError("workflow_type is immutable")
            except (AttributeError, TypeError, ValueError):
                raise WorkflowError("invalid_input") from None
            public_meta_data.pop("unilab", None)
            public_meta_data.pop(_OPERATION_CATEGORY_META_KEY, None)
            category_was_provided = (
                operation_category_uuid is not _OPERATION_CATEGORY_UNSET
            )
            requested_category_uuid = (
                operation_category_uuid
                if category_was_provided
                else current.get("operation_category_uuid")
            )
            with self._operation_category_lock:
                if normalized_workflow_type == "normal":
                    if category_was_provided and requested_category_uuid is not None:
                        raise WorkflowError("invalid_input")
                    normalized_category_uuid = None
                else:
                    if (
                        not category_was_provided
                        and not current_category_was_explicit
                        and requested_category_uuid is None
                    ):
                        requested_category_uuid = legacy_operation_category_uuid(tags)
                    normalized_category_uuid = (
                        self._validate_workflow_operation_category(
                            workflow_type=normalized_workflow_type,
                            category_uuid=requested_category_uuid,
                        )
                    )
                if normalized_category_uuid is not None or (
                    (category_was_provided or current_category_was_explicit)
                    and normalized_workflow_type == WORKFLOW_TYPE_EXPERIMENT_OPERATION
                ):
                    # 显式 null 是持久的“已清空”事实；否则旧分类标签会在读路径
                    # 再次映射成类别，使前端无法真正解除分类。
                    public_meta_data[_OPERATION_CATEGORY_META_KEY] = (
                        normalized_category_uuid
                    )
                if "unilab" in current["meta_data"]:
                    # Preserve server-owned authoring metadata while accepting
                    # the explicitly editable workflow I/O contracts from the
                    # update request.  Previously the whole incoming section
                    # was discarded, so node input bindings were validated
                    # against an empty contract after every save.
                    current_unilab = dict(current["meta_data"]["unilab"] or {})
                    if isinstance(requested_unilab, Mapping):
                        for contract_key in (
                            "input_contract",
                            "output_contract",
                            "output_bindings",
                        ):
                            contract = requested_unilab.get(contract_key)
                            if isinstance(contract, Mapping):
                                current_unilab[contract_key] = deepcopy(contract)
                    public_meta_data["unilab"] = current_unilab
                if self._has_active_source(identity):
                    unilab_meta = dict(public_meta_data.get("unilab") or {})
                    root_fields = set(unilab_meta.get("authoring_root_fields") or [])
                    root_fields.update({"tags", "meta_data"})
                    if workflow_type is not None:
                        root_fields.add("workflow_type")
                    unilab_meta["authoring_root_fields"] = sorted(root_fields)
                    public_meta_data["unilab"] = unilab_meta
                    graph = self.get_graph(identity)
                    graph["workflow"] = {
                        **graph["workflow"],
                        "name": name,
                        "tags": tags,
                        "description": self._optional_text(description),
                        "meta_data": public_meta_data,
                        "workflow_type": normalized_workflow_type,
                    }
                    return self._public_workflow_with_status(
                        self._commit_domain_graph_candidate(
                            identity,
                            revision=int(current["revision"]),
                            graph=graph,
                        )["workflow"]
                    )
                return self._public_workflow_with_status(
                    self._definition_store.update_workflow(
                        identity,
                        name=name,
                        tags=tags,
                        description=self._optional_text(description),
                        meta_data=public_meta_data,
                        workflow_type=normalized_workflow_type,
                    )
                )

    def delete_workflow(self, workflow_uuid: str) -> None:
        """下架工作流并注销领域包来源，保留已发布历史合同。

        参数：``workflow_uuid`` 是待删除工作流稳定身份。返回：无；未知身份抛
        ``not_found``。异常：manifest 注销或内存定义删除失败时返回稳定工作流
        错误。持久下架意图先于跨文件删除提交；后续失败时中心仍停止新准入，
        用户可重试删除或显式重新发布。原发布文件及其冻结图不会物理回收。
        """

        try:
            identity = validate_uuid(workflow_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        with self._publication_snapshot_lock, self._authoring_lock(identity):
            try:
                self._definition_store.get_workflow(identity)
            except StoreNotFound:
                raise WorkflowError("not_found") from None
            # 删除是一条显式下架指令。后续源码撤销失败也不恢复中心准入；用户可
            # 重试删除或显式重新发布。不可变合同文件保留给已冻结的中心调用。
            self._withdrawals().withdraw(identity)
            # 来源登记可能已从 active 授权集合移除（例如重启/导入回滚后），
            # 但仍然存在于内存定义目录。删除时必须以登记表为准清理，不能只
            # 依赖 active 集合，否则页面删除后再次导入仍会命中 UUID/文件名冲突。
            try:
                registration = self._registered_domain_source(identity)
            except WorkflowError as error:
                if error.code != "source_target_unavailable":
                    raise
            else:
                self._unregister_domain_source(registration)
            # 领域工作流定义只存在进程内目录（SQLite :memory: 仅作事务实现），
            # 删除时必须清掉 UUID/节点/连线墓碑，否则同一源码再次导入会冲突。
            # 运行事实库仍走默认软删除，保留历史任务与事件。
            self._definition_store.delete_workflow(
                identity,
                purge=self._definition_store.path == ":memory:",
            )
            self._remove_active_source_authorization(identity)
            contract_store = self._published_contract_store()
            contract_store.discard_workflow(identity)
            # v1 不回收任何已发布历史文件；下架只改变新任务准入。

    def get_graph(self, workflow_uuid: str) -> dict[str, Any]:
        try:
            identity = validate_uuid(workflow_uuid)
            graph = self._definition_store.get_graph(identity)
        except (StoreNotFound, ValueError):
            raise WorkflowError("not_found") from None
        return self._validated_applied_backend_graph(graph)

    def publish_workflow_contract(
        self,
        workflow_uuid: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """把当前工作流修订冻结为不可变已发布工作流合同。

        参数：``workflow_uuid`` 是来源工作流稳定身份，``revision`` 是调用方确认
        的当前修订。返回 Backend 公共发布投影；存在父工作流引用时，额外返回
        ``dependent_refresh``，分别列出已自动更新的父工作流和待处理诊断。
        修订变化或同修订内容漂移抛 ``WorkflowConflict``，空图和非法边界抛
        ``WorkflowError``。子合同提交后，任一父工作流刷新失败只进入诊断，不能
        回滚已发布合同或把本次发布伪装成失败。
        """

        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise WorkflowError("invalid_input")
        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._publication_snapshot_lock, self._authoring_lock(identity):
            graph = self.get_graph(identity)
            contract_store = self._published_contract_store()
            previous = contract_store.latest_for_workflow(identity)
            try:
                contract = contract_store.publish(
                    graph=graph,
                    expected_revision=revision,
                )
            except PublishedContractInvalid as error:
                raise WorkflowError("invalid_input", message=str(error)) from None
            except PublishedContractConflict:
                raise WorkflowConflict("workflow_revision_conflict") from None
            if self._publication_catalog is not None and self._has_active_source(
                identity
            ):
                source = self._read_source(self._registration(identity))
                if source is None:
                    if previous is None or previous["uuid"] != contract["uuid"]:
                        contract_store.discard(contract["uuid"])
                    raise WorkflowError("source_publication_failed")
                try:
                    self._publication_catalog.save(
                        source_draft_hash=source["draft_hash"],
                        contract=contract,
                    )
                except WorkflowPublicationCatalogError as error:
                    if previous is None or previous["uuid"] != contract["uuid"]:
                        contract_store.discard(contract["uuid"])
                    raise WorkflowError("source_publication_failed") from error
            # 发布本身新增了一个组合节点模板；立即刷新共享编译目录，让同一进程
            # 内随后创建父图时即可引用它。跨重启场景由
            # ``restore_published_workflow_contracts`` 执行同一刷新。
            if self._compiler_rebuilder is not None:
                self._rebuild_workspace_activation_catalog()
            self._withdrawals().republish(identity)
        public_contract = contract_store.public(contract)
        dependent_refresh = self._refresh_published_contract_dependents(contract)
        if dependent_refresh["updated_workflow_uuids"] or dependent_refresh["pending"]:
            public_contract["dependent_refresh"] = dependent_refresh
        return public_contract

    def restore_published_workflow_contracts(self) -> None:
        """从领域包文件恢复全部不可变发布合同到内存目录。

        参数：无。返回：无。只有当前 manifest 仍授权且已完成激活的工作流合同
        才恢复；文件损坏、合同摘要不一致或稳定身份冲突时关闭启动，不能把已发布
        实验操作静默降级为普通源码。
        """

        if self._publication_catalog is None:
            return
        # ``contract_store`` 是本次进程内发布合同投影；领域包 JSON 才负责跨
        # 重启持久化，恢复过程不会触碰运行事实 SQLite。
        contract_store = self._published_contract_store()
        restored_any = False
        latest_entries: dict[str, Mapping[str, Any]] = {}
        for entry in self._publication_catalog.list_entries():
            # ``workflow_uuid`` 是合同来源定义稳定身份；只有同代 manifest 已授权
            # 且 Python 定义已激活时才允许恢复，防止孤儿合同重新暴露已撤权定义。
            contract = entry["contract"]
            workflow_uuid = str(contract["workflow_uuid"])
            if not self._has_active_source(workflow_uuid):
                continue
            # 先记录当前来源的最新合同，再尝试恢复具体定义。冷启动时来源清单
            # 可能刚安装了空骨架，旧实现因 ``get_workflow`` 尚未可见而直接丢掉
            # 这条记录，后续就无法把发布合同的修订号预置到首次源码 Apply。
            prior = latest_entries.get(workflow_uuid)
            if prior is None or (
                int(contract["version"]),
                int(contract["workflow_revision"]),
                str(contract["uuid"]),
            ) > (
                int(prior["contract"]["version"]),
                int(prior["contract"]["workflow_revision"]),
                str(prior["contract"]["uuid"]),
            ):
                latest_entries[workflow_uuid] = entry
            try:
                self._definition_store.get_workflow(workflow_uuid)
                contract_store.restore(contract)
                restored_any = True
            except StoreNotFound:
                continue
            except (PublishedContractConflict, PublishedContractInvalid) as error:
                raise WorkflowError("source_publication_failed") from error
        # 来源清单安装出来的定义是空骨架，初始 revision 固定为 1；若该工作流已有
        # 发布合同，先把合同 revision 作为本轮 AST 编译的基线。随后固定点 Apply
        # 会按源码哈希决定是否保持该 revision，从而避免冷重启把同一源码/图误报
        # 为一次新的图编辑。该映射只存在于当前服务实例，且每项成功 Apply 后消费。
        bootstrap_revisions: dict[str, tuple[int, str, str]] = {}
        for workflow_uuid, entry in latest_entries.items():
            contract = entry["contract"]
            try:
                revision = int(contract["workflow_revision"])
                source_hash = str(entry["source_draft_hash"])
                if not source_hash.startswith("sha256:"):
                    source_hash = "sha256:" + source_hash
                if _HASH_TOKEN.fullmatch(source_hash) is None:
                    raise ValueError("发布源码摘要格式无效")
                contract_source_hash = str(contract["source_hash"])
                if _HASH_TOKEN.fullmatch(contract_source_hash) is None:
                    raise ValueError("发布合同图摘要格式无效")
                semantic_graph_hash = published_contract_semantic_hash(contract)
                # 与随后 candidate Apply 共用工作流锁，避免监视线程恰好在空骨架
                # 检查后写入图，导致修订基线和候选基线分裂。
                with self._authoring_lock(workflow_uuid):
                    can_bootstrap = (
                        self._definition_store.bootstrap_workflow_revision(
                            workflow_uuid,
                            revision=revision,
                        )
                    )
                    if not can_bootstrap:
                        # Earlier dependency layers may have observed this source
                        # without applying it yet. Keep its already-authorized
                        # bootstrap pin; observation alone is not a graph edit.
                        with self._bootstrap_published_revisions_lock:
                            pending_bootstrap = self._bootstrap_published_revisions.get(
                                workflow_uuid
                            )
                        can_bootstrap = (
                            pending_bootstrap
                            == (revision, source_hash, semantic_graph_hash)
                            and self._definition_store.get_workflow(workflow_uuid)[
                                "revision"
                            ] == revision
                            and self._definition_store.get_authoring_record(
                                workflow_uuid
                            ).get("applied_source") is None
                        )
            except StoreNotFound:
                # 兼容该方法在来源清单安装之前被调用的入口；下一次恢复会在
                # 空骨架创建后重新建立同一发布修订基线。
                continue
            except (StoreConflict, TypeError, ValueError):
                raise WorkflowError("source_publication_failed") from None
            if can_bootstrap:
                bootstrap_revisions[workflow_uuid] = (
                    revision,
                    source_hash,
                    semantic_graph_hash,
                )
        with self._bootstrap_published_revisions_lock:
            self._bootstrap_published_revisions = bootstrap_revisions
        # 模板投影在工作流源码激活之前构造，而发布合同在此方法中才从领域包
        # 恢复。恢复后立即重建一次编译目录，确保已发布组合模板（包括其合同
        # UUID/Handle UUID）进入后续 graph save 的同一目录代际；否则父图插入
        # 会被编译器误判为“当前目录之外的模板”。
        if restored_any and self._compiler_rebuilder is not None:
            self._rebuild_workspace_activation_catalog()

    def list_published_workflow_contracts(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str = "",
    ) -> dict[str, Any]:
        """分页返回每个来源工作流最新的已发布合同。

        参数：``page``/``page_size`` 是 Backend 页码，``keyword`` 按名称模糊
        过滤。返回空集合而不是 ``null``；非法页码按 Backend 默认值规范化。
        """

        page, page_size = self._normalize_page(page, page_size)
        return self._published_contract_store().list_latest(
            page=page,
            page_size=page_size,
            keyword=keyword,
        )

    def _refresh_published_contract_dependents(
        self,
        contract: Mapping[str, Any],
    ) -> dict[str, Any]:
        """在实验操作发布后自动更新仍兼容的引用方定义。

        参数：``contract`` 是已经成功持久化的新发布合同。返回：已更新父工作流
        UUID 和待人工处理诊断；没有引用者时两个集合均为空。异常：单个父工作流
        的刷新失败只记入 ``pending``，不能把已经提交的实验操作发布伪装成失败。
        已经创建的工作流任务使用各自冻结快照，不在此处读取或修改。
        """

        contract = self._contract_for_composite_expansion(contract)
        updated: list[str] = []
        pending: list[dict[str, str]] = []
        source_dependents = set(
            self._composite_dependent_workflow_uuids(
                str(contract["workflow_uuid"]),
            )
        )
        # 旧版领域包声明不一定记录 ``dependency_workflow_uuids``。这类父源码在
        # 子操作首次发布前只能留下稳定的 ``composite_child_not_found`` 诊断；
        # 目录新增合同后统一重试这些受阻来源，由编译器判断它实际依赖哪个子操作。
        for registration in self.list_registered_sources():
            workflow_uuid = str(registration["workflow_uuid"])
            try:
                authoring = self.get_authoring(workflow_uuid)
            except WorkflowError:
                continue
            draft = authoring.get("draft")
            diagnostics = draft.get("diagnostics") if isinstance(draft, dict) else []
            if {
                str(item.get("code"))
                for item in diagnostics or []
                if isinstance(item, dict) and item.get("code")
            } == {"composite_child_not_found"}:
                source_dependents.add(workflow_uuid)
        page = 1
        while True:
            listed = self._definition_store.list_workflows(
                page=page,
                page_size=100,
            )
            for workflow in listed["items"]:
                # ``parent_uuid`` 是可能包含该子合同的父定义稳定身份；同一身份锁
                # 同时串行化前端保存、领域源码回写和本次自动替换。
                parent_uuid = str(workflow["uuid"])
                if parent_uuid == contract.get("workflow_uuid"):
                    continue
                try:
                    if parent_uuid in source_dependents:
                        recovered = self._recover_first_published_source_dependent(
                            parent_uuid=parent_uuid,
                        )
                        if recovered:
                            updated.append(parent_uuid)
                    graph = self._definition_store.get_graph(parent_uuid)
                except CompositeContractRefreshPending as error:
                    pending.append(
                        {
                            "workflow_uuid": parent_uuid,
                            "code": error.code,
                            "message": str(error),
                        }
                    )
                    continue
                except StoreNotFound:
                    continue
                if not graph_references_composite_child(
                    graph,
                    child_workflow_uuid=str(contract["workflow_uuid"]),
                    except_contract_uuid=str(contract["uuid"]),
                ):
                    continue
                try:
                    with self._authoring_lock(parent_uuid):
                        if (
                            self._has_active_source(parent_uuid)
                            and self.get_authoring(parent_uuid)["state"] != "applied"
                        ):
                            raise CompositeContractRefreshPending(
                                "composite_parent_dirty",
                                "引用方存在尚未应用的编辑，本次未自动替换实验操作",
                            )
                        graph = self.get_graph(parent_uuid)
                        refreshed = refresh_published_composite_invocations(
                            parent_graph=graph,
                            current_contract=contract,
                            load_contract=self._published_contract_store().frozen_dependency,
                            validate_bindings=(
                                self._published_executor_bindings_are_valid
                            ),
                        )
                        if not refreshed.invocation_uuids:
                            continue
                        self._save_server_generated_graph(
                            parent_uuid,
                            revision=int(graph["workflow"]["revision"]),
                            nodes=[
                                WorkflowNodeWrite.model_validate(node)
                                for node in refreshed.graph["nodes"]
                            ],
                            edges=[
                                WorkflowEdgeWrite.model_validate(edge)
                                for edge in refreshed.graph["edges"]
                            ],
                            workflow_meta_data=refreshed.graph["workflow"][
                                "meta_data"
                            ],
                            inventory_requirements=refreshed.graph.get(
                                "inventory_requirements"
                            ),
                        )
                    updated.append(parent_uuid)
                except CompositeContractRefreshPending as error:
                    pending.append(
                        {
                            "workflow_uuid": parent_uuid,
                            "code": error.code,
                            "message": str(error),
                        }
                    )
                except Exception:
                    logger.exception(
                        "published child refresh failed for parent %s",
                        parent_uuid,
                    )
                    pending.append(
                        {
                            "workflow_uuid": parent_uuid,
                            "code": "composite_refresh_failed",
                            "message": "父工作流自动更新失败，请重试发布或检查工作流",
                        }
                    )
            if page * int(listed["page_size"]) >= int(listed["total"]):
                break
            page += 1
        return {
            "updated_workflow_uuids": sorted(set(updated)),
            "pending": pending,
        }

    def _recover_first_published_source_dependent(
        self,
        *,
        parent_uuid: str,
    ) -> bool:
        """恢复只因子实验操作尚未发布而无法应用的父源码。

        参数：``parent_uuid`` 是领域包 AST 已确认引用本次子工作流的父工作流
        身份。返回：本方法重新编译并应用父源码时返回 ``True``；父源码本来已经
        应用时返回 ``False``。异常：存在其他诊断、未应用编辑或候选无法提交时抛
        ``CompositeContractRefreshPending``，发布结果会把它公开为待处理项，绝不
        覆盖用户编辑。状态不变量：仅有 ``composite_child_not_found`` 诊断的登记
        源码才允许自动恢复，且始终保留领域包中的原始作者源码字节。
        """

        before = self.get_authoring(parent_uuid)
        if before.get("state") == "applied":
            return False
        draft = before.get("draft")
        diagnostics = draft.get("diagnostics") if isinstance(draft, dict) else None
        diagnostic_codes = {
            str(item.get("code"))
            for item in diagnostics or []
            if isinstance(item, dict) and item.get("code")
        }
        if diagnostic_codes != {"composite_child_not_found"}:
            raise CompositeContractRefreshPending(
                "composite_parent_dirty",
                "引用方存在其他诊断或尚未应用的编辑，本次未自动应用",
            )
        refreshed = self.reconcile_registered_source(
            parent_uuid,
            force_compile=True,
            preserve_author_source=True,
        )
        candidate = refreshed.get("candidate")
        if not isinstance(candidate, dict):
            refreshed_draft = refreshed.get("draft")
            refreshed_diagnostics = (
                refreshed_draft.get("diagnostics")
                if isinstance(refreshed_draft, dict)
                else []
            )
            if {
                str(item.get("code"))
                for item in refreshed_diagnostics or []
                if isinstance(item, dict) and item.get("code")
            } == {"composite_child_not_found"}:
                return False
            raise CompositeContractRefreshPending(
                "composite_parent_invalid",
                "引用方在子工作流发布后仍未生成可应用版本",
            )
        candidate_hash = candidate.get("candidate_hash")
        if not isinstance(candidate_hash, str) or not candidate_hash:
            raise CompositeContractRefreshPending(
                "composite_parent_invalid",
                "引用方候选缺少稳定身份",
            )
        self.apply_authoring(
            parent_uuid,
            candidate_hash=candidate_hash,
            preserve_author_source=True,
        )
        return True

    def _published_executor_bindings_are_valid(
        self,
        requirements: Sequence[Mapping[str, Any]],
        bindings: Mapping[str, str],
    ) -> bool:
        """判断父调用保存的设备绑定是否仍满足新发布合同。

        参数：``requirements`` 是新合同的设备模板要求，``bindings`` 是父调用
        原有的要求键到设备物料 UUID 映射。返回：键集合完全一致且每个活动物料
        仍属于指定设备模板时为 ``True``；目录未装配或物料失效时关闭返回
        ``False``。异常：物料读取异常由调用方收敛为父工作流待处理诊断。
        """

        required_keys = {
            str(requirement.get("key"))
            for requirement in requirements
            if isinstance(requirement.get("key"), str)
        }
        if len(required_keys) != len(requirements) or set(bindings) != required_keys:
            return False
        if not requirements:
            return True
        if self._material_resolver is None:
            return False
        for requirement in requirements:
            material = self._material_resolver(bindings[str(requirement["key"])])
            if not isinstance(material, Mapping) or material.get(
                "resource_template_uuid"
            ) != requirement.get("resource_template_uuid"):
                return False
        return True

    def _contract_for_composite_expansion(
        self,
        contract: Mapping[str, Any],
    ) -> dict[str, Any]:
        """为本次展开补充同修订的源码摘要，不修改不可变发布合同。

        发布合同的 ``source_hash`` 是图摘要；Python 往返编译核对的是已应用
        源码摘要。新增引用和发布后刷新必须使用相同补充规则，且不能把当前
        新修订的源码摘要写到旧合同上。
        """
        result = deepcopy(dict(contract))
        try:
            snapshot = self._definition_store.get_published_workflow_snapshot(
                str(contract["workflow_uuid"])
            )
        except (StoreNotFound, KeyError, TypeError, ValueError):
            return result
        applied = snapshot.get("applied_source")
        if (
            isinstance(applied, Mapping)
            and applied.get("workflow_revision") == contract.get("workflow_revision")
            and isinstance(applied.get("source_hash"), str)
        ):
            result["applied_source_hash"] = applied["source_hash"]
        return result

    def insert_composite_workflow(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        contract_uuid: str,
        invocation_uuid: str | None,
        device_bindings: Mapping[str, str],
        pose: dict[str, Any],
        param: dict[str, Any],
    ) -> dict[str, Any]:
        """把一个不可变发布合同原子展开到父工作流图。

        参数：``workflow_uuid``/``revision`` 固定父图，``contract_uuid`` 固定子
        合同，``invocation_uuid`` 固定本次调用身份；设备绑定、位置和参数成为冻结
        调用事实。返回修订推进后的完整父图；递归、碰撞、合同损坏或设备不兼容时
        关闭失败，任何节点都不会部分写入。
        """

        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise WorkflowError("invalid_input")
        try:
            parent_uuid = validate_uuid(workflow_uuid)
            contract_identity = validate_uuid(contract_uuid)
            invocation_identity = validate_uuid(invocation_uuid or str(uuid4()))
            pose = normalize_json_object(pose)
            param = normalize_json_object(param)
            if not isinstance(device_bindings, Mapping):
                raise ValueError
            normalized_bindings = {
                str(key): validate_uuid(value)
                for key, value in device_bindings.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            if len(normalized_bindings) != len(device_bindings):
                raise ValueError
        except (TypeError, ValueError):
            raise WorkflowError("invalid_input") from None

        parent_uuid = self.get_workflow(parent_uuid)["uuid"]
        with self._authoring_lock(parent_uuid):
            parent_graph = self.get_graph(parent_uuid)
            if parent_graph["workflow"]["revision"] != revision:
                raise WorkflowConflict("workflow_revision_conflict")
            try:
                contract = deepcopy(
                    self._published_contract_store().get(contract_identity)
                )
            except KeyError:
                raise WorkflowError("not_found") from None
            # 复合节点只能引用已发布的实验操作合同；普通工作流即使存在发布
            # 合同，也不能作为实验操作子工作流插入。
            child_uuid = validate_uuid(str(contract.get("workflow_uuid", "")))
            child = self.get_workflow(child_uuid)
            if child.get("workflow_type") != WORKFLOW_TYPE_EXPERIMENT_OPERATION:
                raise WorkflowError("invalid_composite_child_type")
            if child.get("status") != "published":
                raise WorkflowError("invalid_composite_child_status")
            contract = self._contract_for_composite_expansion(contract)
            requirements = contract["executor_requirements"]
            required_keys = {str(item["key"]) for item in requirements}
            if set(normalized_bindings) != required_keys:
                raise WorkflowError("invalid_input")
            for requirement in requirements:
                material_uuid = normalized_bindings[str(requirement["key"])]
                material = (
                    self._material_resolver(material_uuid)
                    if self._material_resolver is not None
                    else None
                )
                if (
                    not isinstance(material, Mapping)
                    or material.get("resource_template_uuid")
                    != requirement["resource_template_uuid"]
                ):
                    raise WorkflowError("invalid_input")
            try:
                expansion = compile_composite_invocation(
                    parent_graph=parent_graph,
                    contract=contract,
                    invocation_uuid=invocation_identity,
                    pose=pose,
                    param=param,
                    device_bindings=normalized_bindings,
                )
                node_values = [
                    WorkflowNodeWrite.model_validate(item)
                    for item in [*parent_graph["nodes"], *expansion.nodes]
                ]
                edge_values = [
                    WorkflowEdgeWrite.model_validate(item)
                    for item in [*parent_graph["edges"], *expansion.edges]
                ]
                return self._save_server_generated_graph(
                    parent_uuid,
                    revision=revision,
                    nodes=node_values,
                    edges=edge_values,
                    workflow_meta_data=expansion.workflow_meta_data,
                    inventory_requirements=[
                        *[dict(item) for item in parent_graph.get("inventory_requirements") or []],
                        *[dict(item) for item in expansion.inventory_requirements],
                    ],
                )
            except (CompositeInvocationInvalid, ValidationError):
                raise WorkflowError("invalid_input") from None
            except StoreRevisionConflict:
                raise WorkflowConflict("workflow_revision_conflict") from None
            except StoreNotFound:
                raise WorkflowError("not_found") from None
            except StoreAuthoringConflict as error:
                raise WorkflowError(error.code, message=error.message) from None
            except StoreConflict:
                raise WorkflowError("invalid_input") from None

    def _empty_graph_source_error(self, graph: Mapping[str, Any]) -> str | None:
        """首次源码尚未有效应用时，不能把空白初始图作为任务执行。"""
        if graph.get("nodes"):
            return None
        record = self._definition_store.get_authoring_record(graph["workflow"]["uuid"])
        for diagnostic in record.get("diagnostics", []):
            if str(diagnostic.get("severity", "")).lower() == "error":
                return str(diagnostic.get("message") or "工作流源码草稿未通过编译")
        candidate = record.get("candidate")
        if (
            record.get("applied_source") is None
            and isinstance(candidate, Mapping)
            and candidate.get("graph", {}).get("nodes")
        ):
            return "工作流源码已编译但尚未应用；当前仅可预览，不能运行空白初始图"
        return None

    def _ensure_workflow_execution_allowed(self, workflow_uuid: str) -> dict[str, Any]:
        """校验工作流在当前 Runtime 能力下可以进入执行路径。"""

        workflow = self.get_workflow(workflow_uuid)
        capabilities = get_runtime_capabilities()
        if not is_workflow_visible(workflow):
            raise WorkflowError("not_found")
        if (
            not capabilities.can_run_unpublished
            and workflow.get("status") != "published"
        ):
            raise WorkflowError("not_found")
        return workflow

    def get_workflow_run_preflight(
        self,
        workflow_uuid: str,
        *,
        run_mode: str = "normal",
        target_node_uuid: str | None = None,
        input_value: Mapping[str, Any] | None = None,
        inventory_bindings: list[dict[str, Any]] | None = None,
        evaluate_inventory: bool = False,
    ) -> dict[str, Any]:
        """返回不产生 Task、预留或执行占用的候选运行报告。

        参数：``workflow_uuid`` 定位当前图，``run_mode`` 与可选目标节点固定执行
        范围；POST 预检通过 ``input_value/inventory_bindings`` 提供候选提交载荷并
        启用 ``evaluate_inventory``。返回 Backend 形状的只读报告；库存预检不写
        Task/Job/Reservation，正式提交仍在原子准入中复核，不能作为执行承诺。
        """

        return self._workflow_run_preflight(
            workflow_uuid, run_mode=run_mode, target_node_uuid=target_node_uuid,
            input_value=input_value, inventory_bindings=inventory_bindings,
            evaluate_inventory=evaluate_inventory,
        )

    def get_workflows_run_preflight(
        self, items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """按请求顺序检查候选，并合计同一工站共享库存需求。

        同一工作流可出现多次，每次输入独立计量。单项报告只反映该候选的条件；
        整批总量另在同一库存事务快照中复核。不创建任务、不预留库存，不展开
        中心组合，也不推测尚未产生的上游输出。任一项受阻时整批不可运行。
        """

        if not 1 <= len(items) <= 100:
            raise WorkflowError("invalid_input")
        allocation_groups: list[list[Mapping[str, Any]]] = []
        source_plans: list[Mapping[str, Any]] = []
        reports = [self._workflow_run_preflight(
            item["workflow_uuid"], run_mode=item.get("run_mode", "normal"),
            target_node_uuid=item.get("target_node_uuid"),
            input_value=item.get("input", {}),
            inventory_bindings=item.get("inventory_bindings", []),
            evaluate_inventory=True, collected_allocations=allocation_groups,
            input_ready=item.get("input_ready", True),
            revision_fingerprint=item.get("revision_fingerprint"),
            collected_source_plans=source_plans,
        ) for item in items]
        allocations = [allocation for group in allocation_groups for allocation in group]
        check: dict[str, Any] = {
            "status": "passed", "message": "共享数量库存当前满足整批候选需求",
            "allocation_count": len(allocations),
        }
        try:
            validate = getattr(self._task_scheduler_bridge, "check_preflight_inventory_allocations", None)
            if callable(validate):
                validate(allocations)
            elif allocations:
                raise StoreConflict("工作流调度桥未提供整批库存检查")
        except StoreConflict as error:
            check.update(status="blocked", message=str(error))
            shortages = getattr(error, "shortages", None)
            if shortages:
                check["shortages"] = shortages
        if len(allocation_groups) != sum(item.get("input_ready", True) for item in items):
            message = "部分候选输入或库存绑定未通过检查，无法确认整批库存"
            if check["status"] == "blocked":
                message += "；已知候选检查：" + check["message"]
            check.update(status="blocked", message=message)
        pending_count = sum(not item.get("input_ready", True) for item in items)
        if pending_count and check["status"] == "passed":
            check.update(status="deferred", message="已知输入的库存已检查，部分下游输入尚未产生，整批库存仍待确认")
        source_checker = getattr(self._task_scheduler_bridge, "preflight_batch_material_sources", None)
        source_checks = source_checker(source_plans) if callable(source_checker) else []
        return build_batch_run_preflight_report(reports, check, source_checks)

    def _workflow_run_preflight(
        self, workflow_uuid: str, *, run_mode: str = "normal",
        target_node_uuid: str | None = None,
        input_value: Mapping[str, Any] | None = None,
        inventory_bindings: list[dict[str, Any]] | None = None,
        evaluate_inventory: bool = False,
        collected_allocations: list[list[Mapping[str, Any]]] | None = None,
        input_ready: bool = True,
        revision_fingerprint: str | None = None,
        collected_source_plans: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """从同一次读取的定义与候选输入生成报告，供单份和批量预检复用。"""

        normalized_mode = run_mode or "normal"
        if normalized_mode not in {"normal", "step", "single_node"}:
            raise WorkflowError("invalid_input")
        self._ensure_workflow_execution_allowed(workflow_uuid)
        if normalized_mode == "step" and not get_runtime_capabilities().can_debug:
            raise WorkflowError("debug_not_allowed")
        if normalized_mode == "single_node" and target_node_uuid is None:
            raise WorkflowError("invalid_input")
        if normalized_mode != "single_node" and target_node_uuid is not None:
            raise WorkflowError("invalid_input")
        if target_node_uuid is not None:
            try:
                target_node_uuid = validate_uuid(target_node_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        if revision_fingerprint is None:
            graph = self.get_graph(workflow_uuid)
        else:
            contracts = self._published_contract_store()
            try:
                contract = contracts.get_by_revision_fingerprint(
                    workflow_uuid=workflow_uuid, revision_fingerprint=revision_fingerprint,
                )
            except KeyError:
                raise WorkflowConflict(
                    "workflow_revision_conflict",
                    message="请求的工作流发布修订不存在或指纹不匹配",
                ) from None
            from unilabos.workflow.station_contract import bind_station_contract

            try:
                graph = bind_station_contract(
                    contract, self._resource_reference_resolver, contracts.frozen_dependency,
                )
            except ValueError as error:
                raise WorkflowConflict("preflight_failed", message=str(error)) from error
        execution_admission = self._store.find_execution_admission_blocker(
            requested_run_mode=normalized_mode,
        )
        quantity_inventory_check: dict[str, Any] | None = None
        prepared = None
        if evaluate_inventory and input_ready:
            try:
                normalized_input = normalize_json_object(input_value or {})
                normalized_bindings = [
                    normalize_json_object(binding)
                    for binding in (inventory_bindings or [])
                ]
                prepared = self._prepare_task_input(
                    graph,
                    input_value=normalized_input,
                    run_mode=normalized_mode,
                    target_node_uuid=target_node_uuid,
                )
                preflight = getattr(
                    self._task_scheduler_bridge,
                    "preflight_inventory_allocations" if collected_allocations is None
                    else "prepare_preflight_inventory_allocations",
                    None,
                )
                active_requirements = [
                    requirement
                    for requirement in prepared.workflow_snapshot.get(
                        "inventory_requirements",
                        [],
                    )
                    if isinstance(requirement, Mapping)
                    and str(requirement.get("consume_node_uuid"))
                    in prepared.planned_node_uuids
                ]
                if not callable(preflight):
                    if active_requirements or normalized_bindings:
                        raise StoreConflict("工作流数量型库存未装配本地库存权威")
                    allocations: list[dict[str, Any]] = []
                else:
                    allocations = preflight(
                        graph=graph,
                        prepared=prepared,
                        bindings=normalized_bindings,
                    )
                if collected_allocations is not None:
                    collected_allocations.append(allocations)
                    validate = getattr(self._task_scheduler_bridge, "check_preflight_inventory_allocations", None)
                    if callable(validate):
                        validate(allocations)
                    elif allocations:
                        raise StoreConflict("工作流调度桥未提供整批库存检查")
                quantity_inventory_check = {
                    "status": "passed",
                    "message": (
                        "共享数量库存当前可完成整任务准入"
                        if allocations
                        else "工作流本次运行没有活动数量库存需求"
                    ),
                    "allocation_count": len(allocations),
                }
            except (StoreConflict, TaskInputError, TypeError, ValueError) as error:
                quantity_inventory_check = {
                    "status": "blocked",
                    "message": str(error),
                    "allocation_count": 0,
                }
                shortages = getattr(error, "shortages", None)
                if isinstance(shortages, list) and shortages:
                    quantity_inventory_check["shortages"] = shortages
        if prepared is None and not evaluate_inventory:
            try:
                prepared = self._prepare_task_input(graph, input_value={}, run_mode=normalized_mode, target_node_uuid=target_node_uuid)
            except (StoreConflict, TaskInputError, TypeError, ValueError):
                pass  # GET 没有提供的必填入口仍待用户输入，结构错误由原报告处理。
        if collected_source_plans is not None:
            collected_source_plans.append(prepared.execution_plan if prepared is not None else {"nodes": []})
        source_checker = getattr(self._task_scheduler_bridge, "preflight_material_sources", None)
        material_source_checks = None if prepared is None else (
            source_checker(prepared) if callable(source_checker) else unavailable_material_source_checks(prepared.execution_plan))
        material_output_checks = None
        if revision_fingerprint is not None:
            from unilabos.workflow.material_output_preflight import material_output_checks as check_material_outputs

            try:
                checker = getattr(self._task_scheduler_bridge, "preflight_material_outputs", check_material_outputs)
                material_output_checks = checker(graph, prepared=prepared,
                    run_mode=normalized_mode, target_node_uuid=target_node_uuid)
            except (KeyError, TypeError, ValueError, StoreConflict) as error:
                material_output_checks = [{"type": "material_output", "status": "blocked",
                    "code": "invalid_material_output", "message": str(error), "blocking": True, "details": {}}]
        with self._resource_order_context():
            report = build_run_preflight_report(
                graph=graph,
                run_mode=normalized_mode,
                target_node_uuid=target_node_uuid,
                material_resolver=self._material_resolver,
                device_preflight=self._device_preflight,
                quantity_inventory_check=quantity_inventory_check,
                execution_admission=execution_admission,
                source_error=self._empty_graph_source_error(graph),
                material_output_checks=material_output_checks,
                execution_plan=prepared.execution_plan if prepared is not None else None,
                material_source_checks=material_source_checks,
            )
        if not input_ready:
            from unilabos.workflow.run_preflight import mark_upstream_input_pending

            mark_upstream_input_pending(report)
        return report


    def save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: list[WorkflowNodeWrite | dict[str, Any]],
        edges: list[WorkflowEdgeWrite | dict[str, Any]],
    ) -> dict[str, Any]:
        """以严格工作流输入/输出（Workflow I/O）合同保存完整图。

        参数说明：``workflow_uuid`` 是工作流（Workflow）稳定身份，``revision``
        是乐观并发预期修订，``nodes`` 与 ``edges`` 是完整替换集合。返回：提交后
        的后端（Backend）形状工作流图投影。异常：输入 DTO 或图语义非法抛出
        ``WorkflowError``；修订冲突抛出 ``WorkflowConflict``；任何失败都由存储
        适配器（Store Adapter）回滚，公共服务入口不会留下部分节点或修订写入。
        公共入口始终保护系统保留元数据，并使用同一个严格校验深模块。
        """

        return self._save_graph(
            workflow_uuid,
            revision=revision,
            nodes=nodes,
            edges=edges,
            protect_reserved_metadata=True,
        )

    def _save_server_generated_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: list[WorkflowNodeWrite | dict[str, Any]],
        edges: list[WorkflowEdgeWrite | dict[str, Any]],
        workflow_meta_data: dict[str, Any] | None = None,
        inventory_requirements: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """保存由 OS 生成、并已完成组合合同校验的完整工作流图。

        参数：``workflow_uuid`` 是父工作流稳定身份，``revision`` 是乐观并发基线，
        ``nodes``/``edges`` 是包含可信组合元数据的完整替换集合。返回：提交后的
        Backend 形状图。异常：校验、修订冲突与源码回写失败沿用 ``save_graph``
        的公共语义；本入口仅允许服务内部调用，HTTP 载荷不能选择信任策略。
        """

        return self._save_graph(
            workflow_uuid,
            revision=revision,
            nodes=nodes,
            edges=edges,
            protect_reserved_metadata=False,
            workflow_meta_data=workflow_meta_data,
            inventory_requirements=inventory_requirements,
        )

    def _save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: list[WorkflowNodeWrite | dict[str, Any]],
        edges: list[WorkflowEdgeWrite | dict[str, Any]],
        protect_reserved_metadata: bool,
        workflow_meta_data: dict[str, Any] | None = None,
        inventory_requirements: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """在线性化锁内执行公共或系统生成图的统一保存事务。

        ``inventory_requirements`` 仅由 OS 组合展开传入：为 ``None`` 时沿用已保存需求；
        有源码的工作流由重新编译推导需求，无需传入。


        参数：工作流身份、预期修订和完整节点/连线集合固定写入内容；
        ``protect_reserved_metadata`` 为真表示来源是公共请求，必须拒绝修改系统
        保留元数据，为假仅供已校验的 OS 组合展开结果。返回：提交后的完整图。
        异常：DTO、图、源码或修订错误映射为稳定服务异常；失败不留下部分写入。
        """

        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            self.get_workflow(identity)
            try:
                node_values = [
                    item
                    if isinstance(item, WorkflowNodeWrite)
                    else WorkflowNodeWrite.model_validate(item)
                    for item in nodes
                ]
                edge_values = [
                    item
                    if isinstance(item, WorkflowEdgeWrite)
                    else WorkflowEdgeWrite.model_validate(item)
                    for item in edges
                ]
                from unilabos.workflow.definition_hierarchy import restore_private_subgraphs

                restored_nodes, restored_edges = restore_private_subgraphs(
                    applied=self.get_graph(identity),
                    nodes=[item.model_dump() for item in node_values],
                    edges=[item.model_dump() for item in edge_values],
                )
                node_values = [WorkflowNodeWrite.model_validate(item) for item in restored_nodes]
                edge_values = [WorkflowEdgeWrite.model_validate(item) for item in restored_edges]
                if self._has_active_source(identity):
                    candidate = self._definition_store.preview_graph_replacement(
                        identity,
                        revision=revision,
                        nodes=node_values,
                        edges=edge_values,
                        protect_reserved_metadata=protect_reserved_metadata,
                        workflow_meta_data=workflow_meta_data,
                        validate_workflow_io_contract=True,
                    )
                    return self._commit_domain_graph_candidate(
                        identity,
                        revision=revision,
                        graph=candidate,
                    )
                extra = {} if inventory_requirements is None else {
                    "inventory_requirements": [
                        item
                        if isinstance(item, WorkflowInventoryRequirementWrite)
                        else WorkflowInventoryRequirementWrite.model_validate(item)
                        for item in inventory_requirements
                    ]
                }
                return self._definition_store.save_graph(
                    identity,
                    revision=revision,
                    nodes=node_values,
                    edges=edge_values,
                    protect_reserved_metadata=protect_reserved_metadata,
                    workflow_meta_data=workflow_meta_data,
                    validate_workflow_io_contract=True,
                    **extra,
                )
            except ValidationError:
                raise WorkflowError("invalid_input") from None
            except StoreRevisionConflict:
                raise WorkflowConflict("conflict") from None
            except StoreNotFound:
                raise WorkflowError("not_found") from None
            except StoreAuthoringConflict as error:
                raise WorkflowError(error.code, message=error.message) from None
            except StoreConflict:
                raise WorkflowError("invalid_input") from None

    def _has_active_source(self, workflow_uuid: str) -> bool:
        """返回当前定义是否由本次启动的领域包 Python 目标拥有。

        仅手工挂载创作文件、但没有配置领域包写入目标的遗留入口继续保持原有
        Graph API 语义；只有 managed Local 领域包启用双向同步。
        """

        if self._source_target is None:
            return False
        with self._active_sources_lock:
            return workflow_uuid in self._active_source_workflow_uuids

    def _ensure_authoring_function_name_available(
        self,
        *,
        workflow_uuid: str,
        function_name: str,
    ) -> None:
        """拒绝同一领域包中重复的工作流作者函数名。"""

        if self._source_target is None:
            return
        normalized_name = _safe_identifier(function_name, fallback="workflow")
        with self._authoring_function_name_lock:
            page = 1
            while True:
                listed = self._definition_store.list_workflows(
                    page=page,
                    page_size=100,
                )
                for workflow in listed["items"]:
                    if str(workflow.get("uuid")) == workflow_uuid:
                        continue
                    meta_data = workflow.get("meta_data")
                    unilab = (
                        meta_data.get("unilab")
                        if isinstance(meta_data, Mapping)
                        else None
                    )
                    existing_name = (
                        unilab.get("authoring_function_name")
                        if isinstance(unilab, Mapping)
                        else None
                    )
                    if not isinstance(existing_name, str) or not existing_name:
                        existing_name = _safe_identifier(
                            str(workflow.get("name") or "workflow"), fallback="workflow"
                        )
                    if existing_name == normalized_name:
                        raise WorkflowConflict("source_function_conflict")
                if page * 100 >= int(listed["total"]):
                    break
                page += 1

    @staticmethod
    def _authoring_function_name_from_source(
        python_source: str,
        workflow_uuid: str,
    ) -> str:
        """从已通过编译的源码读取唯一作者函数名。"""

        try:
            return parse_authoring_source(
                python_source=python_source,
                expected_workflow_uuid=workflow_uuid,
            ).function_name
        except Exception:
            raise WorkflowError("candidate_invalid") from None

    def _commit_domain_graph_candidate(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        graph: dict[str, Any],
    ) -> dict[str, Any]:
        """把 API 图修改转换为 Python，并经现有创作事务写回同一领域文件。

        参数：``workflow_uuid``/``revision`` 固定当前定义代际，``graph`` 是已用
        Store 同合同预演通过的候选完整图。返回应用后的完整图。源码生成、AST
        固定点、目录指纹、文件 CAS 或候选应用任一步失败时，不直接改内存图。
        """

        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        registration = self._registration(workflow_uuid)
        source = self._read_source(registration)
        if source is None:
            raise WorkflowConflict("draft_hash_conflict")
        try:
            compilation = CandidateCompilation.model_validate(
                self.compiler.generate_python(
                    workflow_uuid=workflow_uuid,
                    workflow_revision=revision,
                    graph=self._authoring_graph_projection(graph),
                    source_uri=str(registration["source_uri"]),
                )
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise WorkflowError(
                "candidate_invalid",
                message=str(error) or "工作流图不能转换为规范 Python 源码",
            ) from None
        except Exception:
            raise WorkflowError("internal_error") from None
        if not compilation.valid or compilation.normalized_python_source is None:
            diagnostic = next(
                (
                    item
                    for item in compilation.diagnostics
                    if str(item.get("severity", "")).lower() == "error"
                ),
                None,
            )
            message = (
                str(diagnostic.get("message"))
                if isinstance(diagnostic, Mapping) and diagnostic.get("message")
                else "工作流图不能转换为规范 Python 源码"
            )
            raise WorkflowError("candidate_invalid", message=message)
        from unilabos.workflow.authoring_graph_semantics import semantic_graph_equal

        current = self.get_graph(workflow_uuid)
        if (
            compilation.normalized_python_source == source["python_source"]
            and semantic_graph_equal(graph, current)
        ):
            # 原样回存：源码与图都没有变化，不产生新候选，直接返回当前定义。
            return current
        authoring = self.save_draft(
            workflow_uuid,
            python_source=compilation.normalized_python_source,
            expected_draft_hash=source["draft_hash"],
            expected_workflow_revision=revision,
            compilation_base_graph=graph,
        )
        candidate = authoring.get("candidate")
        if candidate is None:
            # ``get_authoring`` 将编译诊断放在 ``draft.diagnostics``；不能只查
            # 聚合根，否则组合节点生成失败时会丢掉真正的合同诊断，前端只能看到
            # 无法定位的通用错误。
            draft = authoring.get("draft")
            diagnostics = (
                draft.get("diagnostics")
                if isinstance(draft, Mapping)
                else authoring.get("diagnostics")
            )
            diagnostic = next(
                (
                    item
                    for item in diagnostics
                    if isinstance(item, Mapping)
                    and str(item.get("severity", "")).lower() == "error"
                ),
                None,
            ) if isinstance(diagnostics, list) else None
            message = (
                str(diagnostic.get("message"))
                if isinstance(diagnostic, Mapping) and diagnostic.get("message")
                else "工作流图不能转换为规范 Python 源码"
            )
            # ``save_draft`` 的候选为空表示编译/合同校验失败。不能把当前旧图
            # 当成成功返回，否则前端会误以为组合节点已经保存，随后发布或执行
            # 才暴露更难定位的错误。
            raise WorkflowError("candidate_invalid", message=message)
        candidate_hash = candidate.get("candidate_hash")
        if not isinstance(candidate_hash, str) or not candidate_hash:
            raise WorkflowError("candidate_invalid")
        applied = self.apply_authoring(
            workflow_uuid,
            candidate_hash=candidate_hash,
        )
        return self._validated_applied_backend_graph(
            applied["authoring"]["applied_graph"]
        )

    def create_workflow_node(
        self,
        workflow_uuid: str,
        *,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """通过完整图原子保存向工作流增加一个节点。"""

        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            graph = self.get_graph(identity)
            template = None
            template_uuid = payload.get("workflow_node_template_uuid")
            if template_uuid is not None:
                try:
                    template = self._definition_store.get_node_template(
                        validate_uuid(str(template_uuid))
                    )
                except ValueError:
                    raise WorkflowError("invalid_input") from None
                except StoreNotFound:
                    raise WorkflowError("not_found") from None
            try:
                node = build_workflow_node(payload=payload, template=template)
            except (TypeError, ValueError, WorkflowDefinitionInvalid):
                raise WorkflowError("invalid_input") from None

            # 公共 Graph 写接口不能直接改写 ``unilab`` 保留元数据；但固定设备
            # 选择又必须同时投影为顶层 material_uuid 和可信 executor_binding，
            # 否则源码回写会退化成动态 ``device()`` 并丢失用户选择。这里只接受
            # 用户可编辑的 input_bindings，由服务端根据已校验物料身份生成绑定。
            submitted_unilab = node.get("meta_data", {}).get("unilab", {})
            editable_unilab: dict[str, Any] = {}
            if isinstance(submitted_unilab, Mapping):
                input_bindings = submitted_unilab.get("input_bindings")
                if isinstance(input_bindings, Mapping):
                    editable_unilab["input_bindings"] = dict(input_bindings)
            material_uuid = node.get("material_uuid")
            if material_uuid is not None:
                material = (
                    self._material_resolver(str(material_uuid))
                    if self._material_resolver is not None
                    else None
                )
                if material is None:
                    raise WorkflowError(
                        "invalid_input", message="固定执行器物料不存在或当前不可用"
                    )
                expected_template_uuid = str(
                    (template or {}).get("resource_template_uuid") or ""
                )
                actual_template_uuid = str(material.get("resource_template_uuid") or "")
                if (
                    not expected_template_uuid
                    or actual_template_uuid != expected_template_uuid
                ):
                    raise WorkflowError(
                        "invalid_input",
                        message="固定执行器资源模板与动作模板不一致",
                    )
                editable_unilab["executor_binding"] = {
                    "mode": "fixed",
                    "device_id": str(material_uuid),
                }
            # New nodes must append to the author's existing sequence.  If the
            # field is omitted, the deterministic compiler falls back to UUID
            # order; that can put a newly added node before the existing one
            # and emit a reverse ready edge before the UI adds its intended
            # dependency.
            existing_orders = [
                int(
                    ((candidate.get("meta_data") or {}).get("unilab") or {})[
                        "authoring_source_order"
                    ]
                )
                for candidate in graph["nodes"]
                if isinstance((candidate.get("meta_data") or {}).get("unilab"), Mapping)
                and isinstance(
                    ((candidate.get("meta_data") or {}).get("unilab") or {}).get(
                        "authoring_source_order"
                    ),
                    int,
                )
                and not isinstance(
                    ((candidate.get("meta_data") or {}).get("unilab") or {}).get(
                        "authoring_source_order"
                    ),
                    bool,
                )
            ]
            editable_unilab["authoring_source_order"] = (
                max(existing_orders) + 1 if existing_orders else len(graph["nodes"])
            )
            node_meta_data = {
                key: value
                for key, value in node.get("meta_data", {}).items()
                if key != "unilab"
            }
            if editable_unilab:
                node_meta_data["unilab"] = editable_unilab
            node["meta_data"] = node_meta_data
            updated = self._save_server_generated_graph(
                identity,
                revision=graph["workflow"]["revision"],
                nodes=[*graph["nodes"], node],
                edges=graph["edges"],
            )
            return self._graph_entity(updated, "nodes", node["uuid"])

    def list_workflow_nodes(
        self,
        workflow_uuid: str,
        *,
        page: int,
        page_size: int,
        workflow_node_template_uuid: str | None = None,
        material_uuid: str | None = None,
    ) -> dict[str, Any]:
        """分页返回指定工作流节点，并支持模板与物料身份筛选。"""

        from unilabos.workflow.definition_hierarchy import definition_graph

        graph = definition_graph(self.get_graph(workflow_uuid))
        page, page_size = self._normalize_page(page, page_size)
        try:
            template_uuid = (
                validate_uuid(workflow_node_template_uuid)
                if workflow_node_template_uuid
                else None
            )
            resolved_material_uuid = (
                validate_uuid(material_uuid) if material_uuid else None
            )
        except ValueError:
            raise WorkflowError("invalid_input") from None
        items = [
            node
            for node in graph["nodes"]
            if (
                template_uuid is None
                or node.get("workflow_node_template_uuid") == template_uuid
            )
            and (
                resolved_material_uuid is None
                or node.get("material_uuid") == resolved_material_uuid
            )
        ]
        offset = (page - 1) * page_size
        return {
            "items": items[offset : offset + page_size],
            "total": len(items),
            "page": page,
            "page_size": page_size,
        }

    def get_workflow_node(self, node_uuid: str) -> dict[str, Any]:
        """按全局稳定身份读取一个活动工作流节点。"""

        _workflow_uuid, _graph, node = self._locate_graph_entity("nodes", node_uuid)
        return node

    def get_workflow_node_owner(self, node_uuid: str) -> str:
        """返回节点所属工作流 UUID，供接口层执行可见性校验。

        参数：``node_uuid`` 是节点的全局稳定身份。返回：节点当前所属工作流
        UUID。异常：节点不存在或身份非法时沿用工作流存储错误；只读定位，不修改
        工作流图或修订。状态不变量：返回的工作流 UUID 与
        :meth:`get_workflow_node` 使用同一活动图定位规则。
        """

        workflow_uuid, _graph, _node = self._locate_graph_entity("nodes", node_uuid)
        return workflow_uuid

    def patch_workflow_node(
        self,
        node_uuid: str,
        *,
        patch: Mapping[str, Any],
    ) -> dict[str, Any]:
        """局部修改节点，但最终只通过完整图 CAS 写入一次。"""

        workflow_uuid, _graph, _node = self._locate_graph_entity("nodes", node_uuid)
        with self._authoring_lock(workflow_uuid):
            graph = self.get_graph(workflow_uuid)
            current = self._graph_entity(graph, "nodes", validate_uuid(node_uuid))
            try:
                updated_node = build_patched_node(current, patch)
            except (TypeError, ValueError, WorkflowDefinitionInvalid):
                raise WorkflowError("invalid_input") from None
            nodes = [
                updated_node if node["uuid"] == current["uuid"] else node
                for node in graph["nodes"]
            ]
            updated = self.save_graph(
                workflow_uuid,
                revision=graph["workflow"]["revision"],
                nodes=nodes,
                edges=graph["edges"],
            )
            return self._graph_entity(updated, "nodes", current["uuid"])

    def duplicate_workflow_node(
        self,
        node_uuid: str,
        *,
        name: str | None,
    ) -> dict[str, Any]:
        """复制单个节点定义；原有连线保持不变。"""

        workflow_uuid, _graph, _node = self._locate_graph_entity("nodes", node_uuid)
        with self._authoring_lock(workflow_uuid):
            graph = self.get_graph(workflow_uuid)
            current = self._graph_entity(graph, "nodes", validate_uuid(node_uuid))
            try:
                duplicate = build_duplicated_node(current, name=name)
            except (TypeError, ValueError, WorkflowDefinitionInvalid):
                raise WorkflowError("invalid_input") from None
            # 复制会带上原节点的作者源码顺序和结果变量。不改成新值时，保存会因重复被拒绝。
            unilab = (duplicate.get("meta_data") or {}).get("unilab")
            if isinstance(unilab, Mapping):
                updated_unilab = dict(unilab)
                if "authoring_source_order" in updated_unilab:
                    existing_orders = [
                        int(order)
                        for node in graph["nodes"]
                        if isinstance(
                            order := ((node.get("meta_data") or {}).get("unilab") or {}).get(
                                "authoring_source_order"
                            ),
                            int,
                        )
                        and not isinstance(order, bool)
                    ]
                    updated_unilab["authoring_source_order"] = (
                        max(existing_orders) + 1 if existing_orders else len(graph["nodes"])
                    )
                taken_names = {
                    str(name)
                    for node in (*graph["nodes"],)
                    if isinstance(
                        name := ((node.get("meta_data") or {}).get("unilab") or {}).get(
                            "authoring_result_name"
                        ),
                        str,
                    )
                    and name
                }
                current_name = str(updated_unilab.get("authoring_result_name") or duplicate.get("name") or "result")
                candidate_name = f"{current_name}_copy"
                suffix = 2
                while candidate_name in taken_names:
                    candidate_name = f"{current_name}_copy_{suffix}"
                    suffix += 1
                updated_unilab["authoring_result_name"] = candidate_name
                meta_data = dict(duplicate.get("meta_data") or {})
                meta_data["unilab"] = updated_unilab
                duplicate["meta_data"] = meta_data
            updated = self.save_graph(
                workflow_uuid,
                revision=graph["workflow"]["revision"],
                nodes=[*graph["nodes"], duplicate],
                edges=graph["edges"],
            )
            return self._graph_entity(updated, "nodes", duplicate["uuid"])

    def delete_workflow_node(self, node_uuid: str) -> None:
        """删除节点及其关联连线；存在未删除子节点时拒绝。"""

        workflow_uuid, _graph, _node = self._locate_graph_entity("nodes", node_uuid)
        with self._authoring_lock(workflow_uuid):
            graph = self.get_graph(workflow_uuid)
            identity = validate_uuid(node_uuid)
            self._graph_entity(graph, "nodes", identity)
            if any(node.get("parent_uuid") == identity for node in graph["nodes"]):
                raise WorkflowConflict("conflict")
            self.save_graph(
                workflow_uuid,
                revision=graph["workflow"]["revision"],
                nodes=[node for node in graph["nodes"] if node["uuid"] != identity],
                edges=[
                    edge
                    for edge in graph["edges"]
                    if edge["source_node_uuid"] != identity
                    and edge["target_node_uuid"] != identity
                ],
            )

    def create_workflow_edge(
        self,
        workflow_uuid: str,
        *,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """通过完整图验证和 CAS 增加一条连线。

        参数：``workflow_uuid`` 是工作流稳定身份，``payload`` 包含源/目标节点
        和句柄 UUID。返回：保存后图中的实际连线投影；托管领域包模式下，源码
        编译器可能按规范源码重建连线 UUID，因此按节点与句柄的稳定组合回读。
        异常：工作流、节点或句柄不存在时返回 ``not_found``，图合同或修订冲突
        沿用统一工作流错误；失败不会留下半条连线。
        """

        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            graph = self.get_graph(identity)
            try:
                edge = build_workflow_edge(payload)
                edge_value = WorkflowEdgeWrite.model_validate(edge)
            except (TypeError, ValueError, ValidationError, WorkflowDefinitionInvalid):
                raise WorkflowError("invalid_input") from None
            updated = self.save_graph(
                identity,
                revision=graph["workflow"]["revision"],
                nodes=graph["nodes"],
                edges=[*graph["edges"], edge_value],
            )
            try:
                return self._graph_entity(updated, "edges", edge_value.uuid)
            except WorkflowError as error:
                if error.code != "not_found":
                    raise
                # Managed domain sources are reconstructed from canonical Python
                # after every graph write.  The compiler intentionally owns the
                # persisted edge UUID, while the endpoint caller only knows the
                # transient UUID generated by ``create_edge``.  Return the edge
                # by its semantic endpoints instead of reporting a false 404
                # after a successful write.
                for candidate in reversed(updated.get("edges", [])):
                    if (
                        candidate.get("source_node_uuid") == edge_value.source_node_uuid
                        and candidate.get("target_node_uuid") == edge_value.target_node_uuid
                        and candidate.get("source_handle_uuid") == edge_value.source_handle_uuid
                        and candidate.get("target_handle_uuid") == edge_value.target_handle_uuid
                    ):
                        return candidate
                raise

    def delete_workflow_edge(self, edge_uuid: str) -> None:
        """按稳定身份删除一条工作流连线。"""

        workflow_uuid, _graph, _edge = self._locate_graph_entity("edges", edge_uuid)
        with self._authoring_lock(workflow_uuid):
            graph = self.get_graph(workflow_uuid)
            identity = validate_uuid(edge_uuid)
            self._graph_entity(graph, "edges", identity)
            self.save_graph(
                workflow_uuid,
                revision=graph["workflow"]["revision"],
                nodes=graph["nodes"],
                edges=[edge for edge in graph["edges"] if edge["uuid"] != identity],
            )

    def batch_delete_workflow_graph(
        self,
        workflow_uuid: str,
        *,
        node_uuids: list[str],
        edge_uuids: list[str],
    ) -> dict[str, Any]:
        """一次原子删除指定节点、关联连线和显式指定连线。"""

        identity = self.get_workflow(workflow_uuid)["uuid"]
        try:
            node_set = {validate_uuid(value) for value in node_uuids}
            edge_set = {validate_uuid(value) for value in edge_uuids}
        except (TypeError, ValueError):
            raise WorkflowError("invalid_input") from None
        if not node_set and not edge_set:
            raise WorkflowError("invalid_input")
        with self._authoring_lock(identity):
            graph = self.get_graph(identity)
            known_nodes = {node["uuid"] for node in graph["nodes"]}
            known_edges = {edge["uuid"] for edge in graph["edges"]}
            if not node_set <= known_nodes or not edge_set <= known_edges:
                raise WorkflowError("invalid_input")
            if any(
                node.get("parent_uuid") in node_set and node["uuid"] not in node_set
                for node in graph["nodes"]
            ):
                raise WorkflowConflict("conflict")
            retained_edges = [
                edge
                for edge in graph["edges"]
                if edge["uuid"] not in edge_set
                and edge["source_node_uuid"] not in node_set
                and edge["target_node_uuid"] not in node_set
            ]
            return self.save_graph(
                identity,
                revision=graph["workflow"]["revision"],
                nodes=[node for node in graph["nodes"] if node["uuid"] not in node_set],
                edges=retained_edges,
            )

    def duplicate_workflow(
        self,
        workflow_uuid: str,
        *,
        name: str | None,
    ) -> dict[str, Any]:
        """在一个定义事务中复制工作流主记录和完整图。

        参数：``workflow_uuid`` 是来源工作流身份；``name`` 是可选新名称，省略
        时追加 ``copy``。返回：新身份、首版修订及完整图。异常：来源不存在、
        来源没有节点、名称非法、节点或连线身份冲突时转换为稳定工作流错误；
        失败不保留不完整副本。
        """

        source = self.get_graph(workflow_uuid)
        if not source["nodes"]:
            raise WorkflowError("invalid_input")
        copied_name = (
            name.strip()
            if isinstance(name, str)
            else f"{source['workflow']['name']} copy"
        )
        if not copied_name:
            raise WorkflowError("invalid_input")
        nodes, edges, inventory_requirements = duplicate_graph(source)
        identity = str(uuid4())
        try:
            return self._definition_store.create_workflow_with_graph(
                workflow_uuid=identity,
                name=copied_name,
                tags=list(source["workflow"].get("tags", [])),
                description=source["workflow"].get("description"),
                meta_data=dict(source["workflow"].get("meta_data", {})),
                nodes=[WorkflowNodeWrite.model_validate(node) for node in nodes],
                edges=[WorkflowEdgeWrite.model_validate(edge) for edge in edges],
                inventory_requirements=[
                    WorkflowInventoryRequirementWrite.model_validate(item)
                    for item in inventory_requirements
                ],
                workflow_type=str(source["workflow"].get("workflow_type", "normal")),
            )
        except ValidationError:
            raise WorkflowError("invalid_input") from None
        except StoreAuthoringConflict as error:
            raise WorkflowError(error.code, message=error.message) from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreConflict:
            raise WorkflowError("invalid_input") from None

    def import_legacy_workflow(
        self,
        *,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """原子导入旧版 JSON 工作流，并转成领域包 Python 定义。

        参数：``payload`` 是旧版工作流根对象，或用 ``data`` 包裹的同形对象。
        返回：重建节点与连线身份后的首版完整图。异常：名称、图、模板、工作流
        类型或实验操作类别非法时关闭导入；领域包或编译器不可用时不留下定义、
        清单或源码半状态。
        """

        definition = (
            payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        )
        if not isinstance(definition, Mapping):
            raise WorkflowError(
                "invalid_input",
                message=(
                    "JSON 工作流导入失败：请求体必须是 JSON 对象，不能是数组、"
                    "字符串或空值"
                ),
            )
        name_value = definition.get("name") or definition.get("workflow_name")
        if not isinstance(name_value, str) or not name_value.strip():
            raise WorkflowError(
                "invalid_input",
                message=(
                    "JSON 工作流导入失败：缺少工作流名称，请填写 name 或"
                    " workflow_name，"
                    "且名称不能为空"
                ),
            )
        source_nodes = definition.get("nodes")
        source_edges = definition.get("edges", [])
        if not isinstance(source_nodes, list) or not source_nodes:
            raise WorkflowError(
                "invalid_input",
                message="JSON 工作流导入失败：nodes 必须是至少包含一个节点的数组",
            )
        if not isinstance(source_edges, list):
            raise WorkflowError(
                "invalid_input",
                message=(
                    "JSON 工作流导入失败：edges 必须是数组；没有连线时请传 []"
                    " 或省略"
                ),
            )
        if definition.get("inventory_requirements") not in (None, []):
            # Local 的数量型库存需求必须走已对齐的任务准入合同，不能静默丢弃。
            raise WorkflowError(
                "invalid_input",
                message=(
                    "JSON 工作流导入失败：暂不支持在导入图中携带"
                    " inventory_requirements；"
                    "请先导入工作流，再通过任务输入接口配置物料需求"
                ),
            )
        if self._source_target is None:
            raise WorkflowError("source_target_unavailable")
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")

        old_to_new: dict[str, str] = {}
        nodes: list[dict[str, Any]] = []
        try:
            for index, source in enumerate(source_nodes):
                if not isinstance(source, Mapping):
                    raise WorkflowDefinitionInvalid(f"nodes[{index}] 必须是对象")
                try:
                    old_uuid = validate_uuid(str(source.get("uuid")))
                except (TypeError, ValueError):
                    raise WorkflowDefinitionInvalid(
                        f"nodes[{index}].uuid 必须是有效且非空的 UUID"
                    ) from None
                if old_uuid in old_to_new:
                    raise WorkflowDefinitionInvalid(
                        f"nodes[{index}].uuid 与前面的节点重复（UUID：{old_uuid}）"
                    )
                node_payload = dict(source)
                node_payload["workflow_node_template_uuid"] = source.get(
                    "workflow_node_template_uuid"
                ) or source.get("template_uuid")
                template = None
                template_uuid = node_payload.get("workflow_node_template_uuid")
                if template_uuid is not None:
                    try:
                        template_uuid = validate_uuid(str(template_uuid))
                    except (TypeError, ValueError):
                        raise WorkflowDefinitionInvalid(
                            f"nodes[{index}].workflow_node_template_uuid 必须是有效"
                            " UUID"
                        ) from None
                    try:
                        template = self._definition_store.get_node_template(
                            template_uuid
                        )
                    except StoreNotFound:
                        raise WorkflowError(
                            "not_found",
                            message=(
                                f"nodes[{index}] 引用的工作流节点模板不存在"
                                f"（模板 UUID：{template_uuid}）"
                            ),
                        ) from None
                try:
                    node = build_workflow_node(
                        payload=node_payload,
                        template=template,
                    )
                except WorkflowDefinitionInvalid as error:
                    raise WorkflowDefinitionInvalid(
                        f"nodes[{index}] 校验失败：{error}"
                    ) from None
                old_to_new[old_uuid] = node["uuid"]
                nodes.append(node)
            # JSON 导入会为每个节点重建身份；控制区域参数和嵌套组合元数据中
            # 的节点引用也必须同步重映射，否则候选图校验会看到已不存在的旧 UUID。
            for node in nodes:
                node["param"] = _remap_control_references(
                    node.get("param") or {},
                    old_to_new,
                )
                node["meta_data"] = _remap_nested_composite_metadata(
                    node.get("meta_data") or {},
                    old_to_new,
                )
            for index, source in enumerate(source_nodes):
                parent_uuid = source.get("parent_uuid")
                if parent_uuid is not None:
                    try:
                        parent_identity = validate_uuid(str(parent_uuid))
                    except (TypeError, ValueError):
                        raise WorkflowDefinitionInvalid(
                            f"nodes[{index}].parent_uuid 必须是有效 UUID"
                        ) from None
                    if parent_identity not in old_to_new:
                        raise WorkflowDefinitionInvalid(
                            f"nodes[{index}].parent_uuid 引用了不存在的节点"
                            f"（UUID：{parent_identity}）"
                        )
                    nodes[index]["parent_uuid"] = old_to_new[parent_identity]
            edges: list[dict[str, Any]] = []
            for index, source in enumerate(source_edges):
                if not isinstance(source, Mapping):
                    raise WorkflowDefinitionInvalid(f"edges[{index}] 必须是对象")
                edge_payload = dict(source)
                for field in ("source_node_uuid", "target_node_uuid"):
                    try:
                        node_identity = validate_uuid(str(source.get(field)))
                    except (TypeError, ValueError):
                        raise WorkflowDefinitionInvalid(
                            f"edges[{index}].{field} 必须是有效 UUID"
                        ) from None
                    if node_identity not in old_to_new:
                        raise WorkflowDefinitionInvalid(
                            f"edges[{index}].{field} 引用了不存在的节点"
                            f"（UUID：{node_identity}）"
                        )
                    edge_payload[field] = old_to_new[node_identity]
                try:
                    edges.append(build_workflow_edge(edge_payload))
                except WorkflowDefinitionInvalid as error:
                    raise WorkflowDefinitionInvalid(
                        f"edges[{index}] 校验失败：{error}"
                    ) from None
            try:
                tags = normalize_json_array(definition.get("tags"))
            except (TypeError, ValueError):
                raise WorkflowDefinitionInvalid("tags 必须是 JSON 数组") from None
            try:
                meta_data = normalize_json_object(definition.get("meta_data"))
            except (TypeError, ValueError):
                raise WorkflowDefinitionInvalid("meta_data 必须是 JSON 对象") from None
            # ``unilab`` 下的输入/输出合同是可编辑的工作流语义，不能和其余
            # 服务端私有元数据一起丢弃；导入后交给统一提交路径重新固化。
            unilab_meta_data = meta_data.get("unilab")
            input_contract = (
                deepcopy(unilab_meta_data.get("input_contract"))
                if isinstance(unilab_meta_data, Mapping)
                and isinstance(unilab_meta_data.get("input_contract"), Mapping)
                else {"version": 1, "parameters": []}
            )
            output_contract = (
                deepcopy(unilab_meta_data.get("output_contract"))
                if isinstance(unilab_meta_data, Mapping)
                and isinstance(unilab_meta_data.get("output_contract"), Mapping)
                else {"version": 1, "outputs": []}
            )
            output_bindings = (
                deepcopy(unilab_meta_data.get("output_bindings"))
                if isinstance(unilab_meta_data, Mapping)
                and isinstance(unilab_meta_data.get("output_bindings"), Mapping)
                else {}
            )
            if "version" not in input_contract:
                input_contract["version"] = 1
            if "parameters" not in input_contract:
                input_contract["parameters"] = []
            if "version" not in output_contract:
                output_contract["version"] = 1
            if "outputs" not in output_contract:
                output_contract["outputs"] = []
            output_bindings = _remap_control_references(
                output_bindings,
                old_to_new,
            )
            try:
                workflow_type = normalize_workflow_type(definition.get("workflow_type"))
            except (TypeError, ValueError):
                raise WorkflowDefinitionInvalid(
                    "workflow_type 只能是 normal 或 experiment_operation"
                ) from None
            public_meta_data = dict(meta_data)
            public_meta_data.pop("unilab", None)
        except WorkflowDefinitionInvalid as error:
            raise WorkflowError(
                "invalid_input",
                message=f"JSON 工作流导入失败：{error}",
            ) from None
        except ValidationError:
            raise WorkflowError(
                "invalid_input",
                message=(
                    "JSON 工作流导入失败：节点或连线字段类型不正确，请检查每个对象的"
                    " UUID、名称、参数和连接点字段"
                ),
            ) from None
        except (KeyError, TypeError, ValueError):
            raise WorkflowError(
                "invalid_input",
                message=(
                    "JSON 工作流导入失败：节点、连线或工作流类型不符合导入要求，"
                    "请检查字段名称、字段类型和引用关系"
                ),
            ) from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreAuthoringConflict as error:
            raise WorkflowError(error.code, message=error.message) from None
        except StoreConflict:
            raise WorkflowError("invalid_input") from None
        identity = str(uuid4())
        registration = self._source_target.registration(
            workflow_uuid=identity,
            file_name=DomainWorkflowSourceTarget.default_file_name(identity),
            workflow_type=workflow_type,
        )
        return self._commit_domain_workflow_creation(
            registration=registration,
            name=name_value.strip(),
            tags=tags,
            description=self._optional_text(definition.get("description")),
            meta_data=public_meta_data,
            nodes=nodes,
            edges=edges,
            workflow_type=workflow_type,
            input_contract=input_contract,
            output_contract=output_contract,
            output_bindings=output_bindings,
            inline_expanded_composites=True,
        )

    def _commit_domain_workflow_creation(
        self,
        *,
        registration: EditableSourceRegistration,
        name: str,
        tags: list[Any],
        description: str | None,
        meta_data: Mapping[str, Any],
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        workflow_type: str,
        input_contract: Mapping[str, Any] | None = None,
        output_contract: Mapping[str, Any] | None = None,
        output_bindings: Mapping[str, Any] | None = None,
        inline_expanded_composites: bool = False,
    ) -> dict[str, Any]:
        """在工作流锁内把新定义规范化为首版领域 Python 源码。

        参数：``registration`` 固定目标领域包身份和类型目录；其余字段是已规范
        的根字段、可为空的节点、连线与工作流类型；``inline_expanded_composites``
        只给 JSON 导入使用：先按已发布实验操作保留 ``workflow()`` 调用，只有
        编译器无法展开时才把已展开子图写成内部控制流。返回：已发布到领域包并
        应用的首版完整图。异常：类别引用、模板、编译、来源发布或内存定义提交
        失败时回滚本次定义、源码和清单，既有领域包内容不受影响。
        """

        identity = registration.workflow_uuid
        source_registered = False
        workflow_created = False
        with self._authoring_lock(identity):
            try:
                with self._operation_category_lock:
                    validated_meta_data = self._validated_operation_category_meta_data(
                        workflow_type=workflow_type,
                        meta_data=meta_data,
                        tags=tags,
                    )
                    # 首次写入完整图时也要让图校验看到输入/输出合同；否则带
                    # workflow_input 或输出绑定的控制图会在尚未生成源码前被
                    # 当成“合同缺失”拒绝。其余 ``unilab`` 字段仍由后续源码
                    # 固化步骤统一生成，避免把调用方私有元数据直接写入权威。
                    creation_meta_data = dict(validated_meta_data)
                    creation_meta_data["unilab"] = {
                        "input_contract": deepcopy(dict(input_contract or {})),
                        "output_contract": deepcopy(dict(output_contract or {})),
                        "output_bindings": deepcopy(dict(output_bindings or {})),
                    }
                    created = self._definition_store.create_workflow_with_graph(
                        workflow_uuid=identity,
                        name=name,
                        tags=tags,
                        description=description,
                        meta_data=creation_meta_data,
                        nodes=[
                            WorkflowNodeWrite.model_validate(node) for node in nodes
                        ],
                        edges=[
                            WorkflowEdgeWrite.model_validate(edge) for edge in edges
                        ],
                        workflow_type=workflow_type,
                    )
                workflow_created = True
            except (KeyError, TypeError, ValueError, ValidationError) as error:
                detail = str(error).strip()
                raise WorkflowError(
                    "invalid_input",
                    message=detail or None,
                ) from None
            except StoreNotFound:
                raise WorkflowError("not_found") from None
            except StoreAuthoringConflict as error:
                raise WorkflowError(error.code, message=error.message) from None
            except StoreConflict as error:
                detail = str(error).strip()
                raise WorkflowError(
                    "invalid_input",
                    message=detail or None,
                ) from None

            try:
                source_meta_data = dict(created["workflow"].get("meta_data") or {})
                source_meta_data["unilab"] = {
                    "source_bootstrap": self._source_bootstrap_metadata(registration),
                    "authoring_root_fields": [
                        "meta_data",
                        "tags",
                        "workflow_type",
                    ],
                }
                if isinstance(input_contract, Mapping):
                    source_meta_data["unilab"]["input_contract"] = deepcopy(
                        dict(input_contract)
                    )
                if isinstance(output_contract, Mapping):
                    source_meta_data["unilab"]["output_contract"] = deepcopy(
                        dict(output_contract)
                    )
                if isinstance(output_bindings, Mapping):
                    source_meta_data["unilab"]["output_bindings"] = deepcopy(
                        dict(output_bindings)
                    )
                source_graph = self._authoring_graph_projection(created)
                source_graph["workflow"]["meta_data"] = source_meta_data
                # 公共创建会丢掉客户端提交的执行器绑定；导入图里的设备物料
                # UUID 必须在生成 Python 前写回固定绑定，否则人工确认和设备
                # 动作无法建成可运行的执行计划。
                self._bind_imported_fixed_executors(source_graph)
                # JSON 导入优先保留已展开实验操作为 ``workflow()`` 调用，这样
                # 测试环境里已发布的子流程可以重新展开；没有展开端口时再摊平。
                compilation, canonical = self._compile_imported_graph(
                    workflow_uuid=identity,
                    workflow_revision=int(created["workflow"]["revision"]),
                    source_uri=registration.source_uri,
                    source_graph=source_graph,
                    inline_expanded_composites=False,
                )
                if (
                    inline_expanded_composites
                    and (
                        canonical is None
                        or not canonical.valid
                        or canonical.graph is None
                    )
                ):
                    compilation, canonical = self._compile_imported_graph(
                        workflow_uuid=identity,
                        workflow_revision=int(created["workflow"]["revision"]),
                        source_uri=registration.source_uri,
                        source_graph=source_graph,
                        inline_expanded_composites=True,
                    )
                if (
                    compilation is None
                    or not compilation.valid
                    or compilation.normalized_python_source is None
                ):
                    raise WorkflowError(
                        "candidate_invalid",
                        message=self._candidate_error_message(
                            compilation,
                            fallback="工作流图不能转换为规范 Python 源码",
                        ),
                    )
                if canonical is None or not canonical.valid or canonical.graph is None:
                    raise WorkflowError(
                        "candidate_invalid",
                        message=self._candidate_error_message(
                            canonical,
                            fallback="生成的候选结果不能通过公共工作流校验",
                        ),
                    )
                function_name = self._authoring_function_name_from_source(
                    compilation.normalized_python_source,
                    identity,
                )
                self._ensure_authoring_function_name_available(
                    workflow_uuid=identity,
                    function_name=function_name,
                )
                canonical_workflow = canonical.graph["workflow"]
                canonical_workflow_type = normalize_workflow_type(
                    canonical_workflow.get("workflow_type")
                )
                # 首次空投影会被规范 Python 重新编译出的完整图替换。类别锁必须
                # 覆盖“校验、移除空投影、创建最终图”，避免类别删除恰好落在两次
                # 定义写入之间，留下指向已删除类别的工作流。
                with self._operation_category_lock:
                    canonical_meta_data = self._validated_operation_category_meta_data(
                        workflow_type=canonical_workflow_type,
                        meta_data=dict(canonical_workflow.get("meta_data") or {}),
                        tags=canonical_workflow.get("tags"),
                    )
                    self._definition_store.discard_uncommitted_workflow(identity)
                    workflow_created = False
                    created = self._definition_store.create_workflow_with_graph(
                        workflow_uuid=identity,
                        name=canonical_workflow["name"],
                        tags=list(canonical_workflow.get("tags") or []),
                        description=canonical_workflow.get("description"),
                        meta_data=canonical_meta_data,
                        nodes=[
                            WorkflowNodeWrite.model_validate(node)
                            for node in canonical.graph["nodes"]
                        ],
                        edges=[
                            WorkflowEdgeWrite.model_validate(edge)
                            for edge in canonical.graph["edges"]
                        ],
                        inventory_requirements=[
                            WorkflowInventoryRequirementWrite.model_validate(item)
                            for item in canonical.graph.get(
                                "inventory_requirements",
                                [],
                            )
                        ],
                        node_templates=list(
                            canonical.graph.get("node_templates") or []
                        ),
                        handle_templates=list(
                            canonical.graph.get("handle_templates") or []
                        ),
                        template_catalog_fingerprint=(
                            canonical.template_catalog_fingerprint
                        ),
                        trusted_authoring_graph=True,
                        workflow_type=canonical_workflow_type,
                    )
                workflow_created = True
                self._provision_domain_source(
                    registration=registration,
                    python_source=compilation.normalized_python_source,
                )
                source_registered = True
                return self._publish_imported_domain_workflow(
                    registration=registration,
                    python_source=compilation.normalized_python_source,
                    created_graph=created,
                )
            except Exception as error:
                rollback_failed = self._rollback_domain_import(
                    registration,
                    workflow_uuid=identity,
                    source_was_registered=source_registered,
                    workflow_was_created=workflow_created,
                )
                if rollback_failed:
                    raise WorkflowError("source_publication_failed") from error
                raise

    def import_python_workflow(
        self,
        *,
        file_name: str,
        python_source: str,
    ) -> dict[str, Any]:
        """通过现有 AST 编译器原子导入一个 Python 工作流文件。

        参数：``file_name`` 是上传时的单个 ``.py`` 文件名，``python_source`` 是
        已按 UTF-8 解码的完整文件内容。返回：新建工作流的 Backend 形状完整图。
        异常：文件边界、AST 声明、动作模板、节点或连线不合法时返回稳定工作流
        错误；工作流、节点或连线身份已存在时返回冲突。

        安全不变量：源码只进入静态 AST 编译器，绝不 import 或执行；只有完整候选
        通过现有目录、图和身份校验后，才在进程内定义事务中创建定义和完整图。
        """

        try:
            # ``imported`` 冻结上传文件身份、工作流稳定 UUID 与源码摘要；此阶段
            # 尚未创建任何工作流定义事实。
            encoded = python_source.encode("utf-8")
            imported = validate_python_workflow_import(
                file_name=file_name,
                python_source=python_source,
                source_hash=_sha256(encoded),
            )
        except PythonWorkflowImportError as error:
            raise WorkflowError(
                "invalid_input",
                message=str(error),
            ) from None
        except UnicodeEncodeError:
            raise WorkflowError(
                "invalid_input",
                message="Python 工作流源码无法按 UTF-8 编码，请检查文件内容后重新上传",
            ) from None
        except AttributeError:
            raise WorkflowError(
                "invalid_input",
                message="源码必须是文本内容，不能是空值或其他类型",
            ) from None
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        if self._source_target is None:
            raise WorkflowError("source_target_unavailable")
        registration = self._source_target.registration(
            workflow_uuid=imported.workflow_uuid,
            file_name=imported.file_name,
            workflow_type=imported.workflow_type,
        )

        # ``initial_graph`` 是修订 1 的空基线，``compilation`` 只代表同一内存模板
        # 代际产生的静态候选，二者都不是已提交的工作流定义。
        initial_graph = imported.initial_graph()
        # 跨重启只保留可由领域包清单重建的来源事实；upload:// 追踪信息不能
        # 成为内存图与冷启动图之间的隐藏差异。
        initial_graph["workflow"]["meta_data"] = {
            "unilab": {
                "source_bootstrap": self._source_bootstrap_metadata(registration)
            }
        }
        try:
            compilation = CandidateCompilation.model_validate(
                self.compiler.compile(
                    workflow_uuid=imported.workflow_uuid,
                    workflow_revision=1,
                    python_source=imported.python_source,
                    source_uri=registration.source_uri,
                    applied_graph=initial_graph,
                )
            )
        except Exception:
            raise WorkflowError(
                "internal_error",
                message=(
                    "源码编译器处理异常，请查看 backend.log 中的具体错误"
                ),
            ) from None
        if compilation.valid and compilation.normalized_python_source is not None:
            function_name = self._authoring_function_name_from_source(
                compilation.normalized_python_source,
                imported.workflow_uuid,
            )
            self._ensure_authoring_function_name_available(
                workflow_uuid=imported.workflow_uuid,
                function_name=function_name,
            )
        # ``candidate`` 经过图、源码范围、模板目录和全局节点/连线身份复核；只有
        # 签发成功后才能进入下面唯一的进程内定义事务。
        candidate = self._issue_candidate(
            workflow_revision=1,
            draft_hash=imported.source_hash,
            compilation=compilation,
            applied_graph=initial_graph,
            draft_python_source=imported.python_source,
        )
        if candidate is None:
            diagnostic = next(
                (
                    item
                    for item in compilation.diagnostics
                    if str(item.get("severity", "")).lower() == "error"
                ),
                None,
            )
            message = (
                str(diagnostic.get("message"))
                if isinstance(diagnostic, Mapping) and diagnostic.get("message")
                else "源码编译未能生成可信候选图，请检查工作流声明、节点和设备动作参数"
            )
            raise WorkflowError(
                "draft_invalid",
                message=f"源码编译未通过：{message}",
            )

        graph = candidate["graph"]
        workflow = graph["workflow"]
        # 领域包来源证据属于系统保留元数据；编译器只追加创作合同，两者在首次
        # 内存事务前合并，避免生成文件、当前投影和冷启动投影出现隐藏差异。
        graph_meta_data = dict(workflow.get("meta_data") or {})
        initial_unilab = dict(
            initial_graph["workflow"].get("meta_data", {}).get("unilab", {})
        )
        candidate_unilab = dict(graph_meta_data.get("unilab") or {})
        graph_meta_data["unilab"] = {**initial_unilab, **candidate_unilab}
        return self._commit_python_domain_import(
            registration=registration,
            workflow=workflow,
            graph=graph,
            graph_meta_data=graph_meta_data,
            candidate=candidate,
        )

    def _commit_python_domain_import(
        self,
        *,
        registration: EditableSourceRegistration,
        workflow: Mapping[str, Any],
        graph: Mapping[str, Any],
        graph_meta_data: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        """在工作流锁内原子提交 Python 定义、领域来源及创作状态。

        参数：``registration`` 固定领域包来源身份；``workflow``/``graph`` 是
        静态 AST 编译后的可信候选；``graph_meta_data`` 合并来源追踪元数据；
        ``candidate`` 固定目录指纹与规范源码。返回：可跨重启恢复的首版完整图。
        异常：类型、类别、模板、身份或来源发布失败时撤销本次内存定义、源码和
        清单，不执行上传的 Python 文件。
        """

        source_registered = False
        workflow_created = False
        with self._authoring_lock(registration.workflow_uuid):
            try:
                try:
                    workflow_type = normalize_workflow_type(
                        workflow.get("workflow_type")
                    )
                    with self._operation_category_lock:
                        validated_meta_data = (
                            self._validated_operation_category_meta_data(
                                workflow_type=workflow_type,
                                meta_data=graph_meta_data,
                                tags=workflow.get("tags"),
                            )
                        )
                        created = self._definition_store.create_workflow_with_graph(
                            workflow_uuid=registration.workflow_uuid,
                            name=str(workflow["name"]),
                            tags=list(workflow.get("tags") or []),
                            description=workflow.get("description"),
                            meta_data=validated_meta_data,
                            nodes=[
                                WorkflowNodeWrite.model_validate(node)
                                for node in graph["nodes"]
                            ],
                            edges=[
                                WorkflowEdgeWrite.model_validate(edge)
                                for edge in graph["edges"]
                            ],
                            inventory_requirements=[
                                WorkflowInventoryRequirementWrite.model_validate(item)
                                for item in graph.get("inventory_requirements", [])
                            ],
                            node_templates=list(graph.get("node_templates") or []),
                            handle_templates=list(graph.get("handle_templates") or []),
                            template_catalog_fingerprint=str(
                                candidate["template_catalog_fingerprint"]
                            ),
                            trusted_authoring_graph=True,
                            workflow_type=workflow_type,
                        )
                    workflow_created = True
                except StoreAuthoringConflict as error:
                    raise WorkflowConflict(error.code, message=error.message) from None
                except StoreNotFound:
                    raise WorkflowConflict("template_catalog_conflict") from None
                except StoreConflict:
                    raise WorkflowConflict("conflict") from None
                except (KeyError, TypeError, ValidationError, ValueError):
                    raise WorkflowError("candidate_invalid") from None
                normalized_source = candidate.get("normalized_python_source")
                if not isinstance(normalized_source, str) or not normalized_source:
                    raise WorkflowError("candidate_invalid")
                self._provision_domain_source(
                    registration=registration,
                    python_source=normalized_source,
                )
                source_registered = True
                return self._publish_imported_domain_workflow(
                    registration=registration,
                    python_source=normalized_source,
                    created_graph=created,
                )
            except Exception as error:
                rollback_failed = self._rollback_domain_import(
                    registration,
                    workflow_uuid=registration.workflow_uuid,
                    source_was_registered=source_registered,
                    workflow_was_created=workflow_created,
                )
                if rollback_failed:
                    raise WorkflowError("source_publication_failed") from error
                raise

    @staticmethod
    def _source_bootstrap_metadata(
        registration: EditableSourceRegistration,
    ) -> dict[str, str]:
        """生成与冷启动骨架完全相同的领域来源追踪元数据。"""

        return {
            "kind": "editable_package_manifest",
            "package_id": registration.package_id,
            "relative_path": registration.relative_path,
            "source_uri": registration.source_uri,
        }

    @staticmethod
    def _candidate_error_message(
        compilation: CandidateCompilation | None,
        *,
        fallback: str,
    ) -> str:
        """取出编译诊断中的第一条错误说明，供 JSON 导入返回可行动消息。"""

        diagnostics = (
            compilation.diagnostics
            if compilation is not None and isinstance(compilation.diagnostics, list)
            else []
        )
        diagnostic = next(
            (
                item
                for item in diagnostics
                if isinstance(item, Mapping)
                and str(item.get("severity", "")).lower() == "error"
                and item.get("message")
            ),
            None,
        )
        if isinstance(diagnostic, Mapping):
            return str(diagnostic["message"])
        return fallback

    def _compile_imported_graph(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        source_uri: str,
        source_graph: Mapping[str, Any],
        inline_expanded_composites: bool,
    ) -> tuple[CandidateCompilation | None, CandidateCompilation | None]:
        """把导入图画成 Python 并重编译。内联失败时由调用方改走另一条路径。

        参数：``inline_expanded_composites`` 为假时保留 ``workflow()`` 调用；为
        真时摊平已展开组合并去掉组合父节点后再编译。返回：源码生成结果与重编译
        结果；源码无效时第二项为 ``None``。异常：编译器抛出未声明错误时转为
        ``internal_error``。
        """

        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        try:
            compilation = CandidateCompilation.model_validate(
                self.compiler.generate_python(
                    workflow_uuid=workflow_uuid,
                    workflow_revision=workflow_revision,
                    graph=source_graph,
                    source_uri=source_uri,
                    inline_expanded_composites=inline_expanded_composites,
                )
            )
        except Exception:
            raise WorkflowError("internal_error") from None
        if not compilation.valid or compilation.normalized_python_source is None:
            return compilation, None
        applied_graph: Mapping[str, Any] = source_graph
        if inline_expanded_composites:
            applied_graph = self._applied_graph_without_inlined_composite_parents(
                source_graph
            )
        try:
            canonical = CandidateCompilation.model_validate(
                self.compiler.compile(
                    workflow_uuid=workflow_uuid,
                    workflow_revision=1,
                    python_source=compilation.normalized_python_source,
                    source_uri=source_uri,
                    applied_graph=dict(applied_graph),
                )
            )
        except Exception:
            raise WorkflowError("internal_error") from None
        return compilation, canonical

    @staticmethod
    def _applied_graph_without_inlined_composite_parents(
        graph: Mapping[str, Any],
    ) -> dict[str, Any]:
        """去掉已内联组合调用节点，避免重编译后留下悬挂的 ``parent_uuid``。

        参数：``graph`` 是 JSON 导入后、生成内联 Python 前的完整候选图。返回：
        删除已展开的 ``type=workflow`` 组合调用及其相关连线，并把其子节点提升
        为顶层；未展开的 ``workflow()`` 调用节点会保留。供编译器按源码结构重
        建父子关系。异常：无。
        """

        result = deepcopy(dict(graph))
        nodes = list(result.get("nodes") or [])
        parent_ids = {
            str(node.get("uuid"))
            for node in nodes
            if isinstance(node, Mapping) and str(node.get("type") or "") == "workflow"
        }
        child_parents = {
            str(node.get("parent_uuid"))
            for node in nodes
            if isinstance(node, Mapping) and isinstance(node.get("parent_uuid"), str)
        }
        invocation_uuids = parent_ids & child_parents
        lifted: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            node_uuid = str(node.get("uuid"))
            if node_uuid in invocation_uuids:
                continue
            lifted_node = dict(node)
            if lifted_node.get("parent_uuid") in invocation_uuids:
                lifted_node["parent_uuid"] = None
            lifted.append(lifted_node)
        result["nodes"] = lifted
        result["edges"] = [
            dict(edge)
            for edge in result.get("edges") or []
            if isinstance(edge, Mapping)
            and str(edge.get("source_node_uuid")) not in invocation_uuids
            and str(edge.get("target_node_uuid")) not in invocation_uuids
        ]
        return result

    @staticmethod
    def _bind_imported_fixed_executors(graph: dict[str, Any]) -> None:
        """按导入节点上的设备物料 UUID 写回固定执行器绑定。

        参数：``graph`` 是即将生成 Python 的创作图，会原地补齐
        ``meta_data.unilab.executor_binding``。返回：无。异常：物料身份不是规范
        UUID 时跳过该节点，避免把部署业务 ID 写进 ``device()`` 后编译失败。
        """

        nodes = graph.get("nodes")
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            try:
                device_id = validate_uuid(str(node.get("material_uuid")))
            except (TypeError, ValueError):
                continue
            meta_data = node.get("meta_data")
            if not isinstance(meta_data, dict):
                meta_data = {}
                node["meta_data"] = meta_data
            unilab = meta_data.get("unilab")
            if not isinstance(unilab, dict):
                unilab = {}
                meta_data["unilab"] = unilab
            unilab["executor_binding"] = {
                "mode": "fixed",
                "device_id": device_id,
            }

    @staticmethod
    def _authoring_graph_projection(graph: Mapping[str, Any]) -> dict[str, Any]:
        """把公共读图收敛为创作编译器要求的完整集合。"""

        fields = ("workflow", "nodes", "edges", "node_templates", "handle_templates")
        try:
            projection = {
                **{field: deepcopy(graph[field]) for field in fields},
                "inventory_requirements": deepcopy(
                    graph.get("inventory_requirements", [])
                ),
            }
            return projection
        except (KeyError, TypeError):
            raise WorkflowError("candidate_invalid") from None

    def _provision_domain_source(
        self,
        *,
        registration: EditableSourceRegistration,
        python_source: str,
    ) -> None:
        """发布已验证 Python 与 manifest 登记，供导入事务随后激活。"""

        if self._source_target is None:
            raise WorkflowError("source_target_unavailable")
        try:
            self._source_target.provision(
                registration=registration,
                python_source=python_source,
            )
        except DomainWorkflowSourceError as error:
            raise WorkflowError(error.code) from None

    def _registered_domain_source(
        self,
        workflow_uuid: str,
    ) -> EditableSourceRegistration:
        """读取当前内存目录中的领域来源身份并恢复为强类型对象。"""

        if self._source_target is None:
            raise WorkflowError("source_target_unavailable")
        try:
            row = self._definition_store.get_source_registration(workflow_uuid)
            return EditableSourceRegistration(
                workflow_uuid=str(row["workflow_uuid"]),
                package_id=str(row["package_id"]),
                package_root=Path(str(row["package_root"])),
                relative_path=str(row["relative_path"]),
                source_uri=str(row["source_uri"]),
            )
        except (KeyError, StoreNotFound):
            raise WorkflowError("source_target_unavailable") from None

    def _unregister_domain_source(
        self,
        registration: EditableSourceRegistration,
        *,
        remove_source: bool = True,
    ) -> None:
        """从唯一领域包 manifest 注销来源，并按场景删除源码文件。"""

        if self._source_target is None:
            raise WorkflowError("source_target_unavailable")
        try:
            self._source_target.unregister(
                registration=registration,
                remove_source=remove_source,
            )
        except DomainWorkflowSourceError as error:
            raise WorkflowError(error.code) from None

    def _remove_active_source_authorization(self, workflow_uuid: str) -> None:
        """在源码注销或导入回滚后移除本进程的来源授权。"""

        with self._active_sources_lock:
            self._active_source_workflow_uuids = frozenset(
                identity
                for identity in self._active_source_workflow_uuids
                if identity != workflow_uuid
            )
            self._active_source_dependencies.pop(workflow_uuid, None)
        # 撤销来源同时撤销尚未消费的冷启动修订闸门，避免同一进程随后重新
        # 导入/创建同 UUID（若调用方恢复墓碑失败也不会沿用旧合同基线）。
        with self._bootstrap_published_revisions_lock:
            self._bootstrap_published_revisions.pop(workflow_uuid, None)

    def _rollback_domain_import(
        self,
        registration: EditableSourceRegistration,
        *,
        workflow_uuid: str,
        source_was_registered: bool,
        workflow_was_created: bool,
    ) -> bool:
        """补偿失败导入；返回是否有任一步补偿失败。"""

        rollback_failed = False
        if workflow_was_created and self._store.has_workflow_tasks(workflow_uuid):
            logger.error("拒绝回滚已有运行 Task 的工作流定义 %s", workflow_uuid)
            return True
        if source_was_registered:
            try:
                self._unregister_domain_source(registration, remove_source=False)
            except WorkflowError:
                logger.exception("回滚工作流领域来源失败")
                # manifest 仍是重启权威；保留本进程定义和授权，避免当前进程与
                # 下次启动观察到两套相反事实。调用方返回可重试的发布失败。
                return True
        if workflow_was_created:
            self._remove_active_source_authorization(workflow_uuid)
            try:
                self._definition_store.discard_uncommitted_workflow(workflow_uuid)
            except StoreNotFound:
                pass
            except Exception:
                logger.exception("回滚进程内工作流定义失败")
                rollback_failed = True
        return rollback_failed

    def _publish_imported_domain_workflow(
        self,
        *,
        registration: EditableSourceRegistration,
        python_source: str,
        created_graph: Mapping[str, Any],
    ) -> dict[str, Any]:
        """登记已验证领域源码并发布刚创建的内存图。

        参数：来源已经写入领域包；``python_source`` 与 ``created_graph`` 已在发布
        前完成 AST、图和固定点校验。返回当前完整图。首次导入仍通过统一创作协调
        器记录已应用源码；source-only 候选不会推进 HTTP ``revision=1`` 合同。
        """

        del python_source, created_graph
        self._add_active_source_authorization(registration)
        self.reconcile_registered_source(
            registration.workflow_uuid,
            force_compile=True,
            preserve_author_source=True,
        )
        record = self._definition_store.get_authoring_record(registration.workflow_uuid)
        candidate = record.get("candidate")
        if isinstance(candidate, Mapping):
            candidate_hash = candidate.get("candidate_hash")
            if not isinstance(candidate_hash, str) or not candidate_hash:
                raise WorkflowError("candidate_invalid")
            self.apply_authoring(
                registration.workflow_uuid,
                candidate_hash=candidate_hash,
                preserve_author_source=True,
            )
        return self.get_graph(registration.workflow_uuid)

    def _locate_graph_entity(
        self,
        collection: str,
        entity_uuid: str,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """通过公共存储投影定位全局节点或连线身份。"""

        try:
            identity = validate_uuid(entity_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        page = 1
        while True:
            result = self._definition_store.list_workflows(page=page, page_size=100)
            for workflow in result["items"]:
                graph = self.get_graph(workflow["uuid"])
                for entity in graph[collection]:
                    if entity["uuid"] == identity:
                        return workflow["uuid"], graph, entity
            if page * result["page_size"] >= result["total"]:
                break
            page += 1
        raise WorkflowError("not_found")

    @staticmethod
    def _graph_entity(
        graph: Mapping[str, Any],
        collection: str,
        entity_uuid: str,
    ) -> dict[str, Any]:
        """从同一图快照读取一个稳定身份实体。"""

        for entity in graph[collection]:
            if entity["uuid"] == entity_uuid:
                return entity
        raise WorkflowError("not_found")

    # 工作流任务（WorkflowTask）与工作流节点作业（WorkflowNodeJob） --------

    def create_workflow_task(
        self,
        *,
        workflow_uuid: str,
        run_mode: str,
        target_node_uuid: str | None,
        input_value: dict[str, Any],
        description: str | None,
        meta_data: dict[str, Any],
        inventory_bindings: list[dict[str, Any]] | None = None,
        backend_task_uuid: str | None = None,
        invocation_key: str | None = None,
        priority: WorkflowTaskPriority | str | float = WorkflowTaskPriority.NORMAL,
        request_fingerprint: str = "",
        revision_fingerprint: str | None = None,
        deadline: str | None = None,
        frozen_graph: dict[str, Any] | None = None,
        center_invocation_uuid: str | None = None,
    ) -> dict[str, Any]:
        """从已应用工作流图创建一次工作流任务（WorkflowTask）及其作业。

        参数：``workflow_uuid`` 是工作流定义身份；``run_mode`` 是普通、单步或
        单节点运行模式；``target_node_uuid`` 是单节点运行目标；``input_value``
        是任务输入；``description`` 与 ``meta_data`` 是用户说明和公开元数据；
        ``inventory_bindings`` 把本次执行的逻辑数量需求绑定到具体试剂或当前
        内容物库存；``priority`` 接受 ``normal``/``high`` 字符串枚举并随任务
        持久化，调度顺序由后续调度器实现；可选 Backend Task、调用键和请求指纹
        仅用于工站调用幂等，不允许调用方提交任何中间节点参数。
        ``center_invocation_uuid`` 将已领取中心意图及边界事件纳入创建事务。
        返回：同一事务创建的工作流任务及工作流节点作业（WorkflowNodeJob）投影。
        异常：身份、运行模式、输入或执行计划不合法时抛出稳定工作流错误；输入
        合同解析、默认值填充与计划绑定全部在同一创建事务的首次写入前完成。
        """

        try:
            workflow_uuid = validate_uuid(workflow_uuid)
            if center_invocation_uuid is not None and frozen_graph is not None:
                from unilabos.workflow.center_boundary import validate_claimed_submission

                with self._store.transaction() as connection:
                    validate_claimed_submission(
                        connection, center_invocation_uuid, workflow_uuid=workflow_uuid,
                        revision_fingerprint=revision_fingerprint,
                        request_fingerprint=request_fingerprint,
                    )
            else:
                self._ensure_workflow_execution_allowed(workflow_uuid)
            if frozen_graph is None:
                self._definition_store.get_workflow(workflow_uuid)
            elif str(frozen_graph.get("workflow", {}).get("uuid")) != workflow_uuid:
                raise ValueError("冻结图与工作流身份不一致")
        except ValueError:
            raise WorkflowError("invalid_input") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except (StoreConflict, EvidenceOnlyError) as error:
            raise WorkflowConflict("conflict", message=str(error)) from error
        run_mode = "normal" if run_mode == "" else run_mode
        if run_mode not in {"normal", "step", "single_node"}:
            raise WorkflowError("invalid_input")
        if run_mode != "single_node" and target_node_uuid is not None:
            raise WorkflowError("invalid_input")
        if target_node_uuid is not None:
            try:
                target_node_uuid = validate_uuid(target_node_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        try:
            input_value = normalize_json_object(input_value)
            meta_data = normalize_json_object(meta_data)
            normalized_inventory_bindings = [
                normalize_json_object(binding) for binding in (inventory_bindings or [])
            ]
        except ValueError:
            raise WorkflowError("invalid_input") from None
        from unilabos.workflow.transport_workflow import submission_context, validate_vehicle_interval

        try:
            if center_invocation_uuid is None:
                transport_context = submission_context(None, None, input_value)
            else:
                with self._store.transaction() as connection:
                    transport_context = submission_context(connection, center_invocation_uuid, input_value)
        except (StoreConflict, ValueError) as error:
            raise WorkflowConflict("transport_workflow_invalid", message=str(error)) from error
        description = self._optional_text(description)
        task_uuid = str(uuid4())
        center_source_payload = None
        if center_invocation_uuid is not None:
            from unilabos.workflow.center_boundary import material_input_task_uuid

            with self._store.transaction() as connection:
                planned_task = material_input_task_uuid(connection, center_invocation_uuid)
                import json
                original = connection.execute("SELECT package_json FROM center_invocation_inbox WHERE invocation_uuid=?",
                    (center_invocation_uuid,)).fetchone()
                center_source_payload = json.loads(original["package_json"])["payload"]
                if planned_task is not None:
                    authority = getattr(self, "_center_material_input_authority", None)
                    if authority is None:
                        raise WorkflowConflict("conflict", message="物料调用尚未配置真实库存输入权威")
                    import json

                    row = connection.execute(
                        "SELECT i.package_json,h.receipt_json FROM center_invocation_inbox i "
                        "JOIN center_material_input h USING(invocation_uuid) WHERE invocation_uuid=?",
                        (center_invocation_uuid,),
                    ).fetchone()
                    body, receipt = json.loads(row["package_json"]), json.loads(row["receipt_json"])
                    authority.verify(body, receipt)
                    authority.prepare(body, input_materials=self.center_material_input_items(body, connection=connection))
                    task_uuid = planned_task

        if run_mode == "step":
            if not get_runtime_capabilities().can_debug:
                raise WorkflowError("debug_not_allowed")
            report = self.get_workflow_run_preflight(
                workflow_uuid,
                run_mode=run_mode,
                target_node_uuid=None,
                input_value=input_value,
                inventory_bindings=normalized_inventory_bindings,
                evaluate_inventory=True,
            )
            if not report.get("can_run"):
                admission = next(
                    (
                        check
                        for check in report.get("checks", [])
                        if check.get("code") == "debug_task_conflict"
                    ),
                    None,
                )
                if admission is not None:
                    raise WorkflowConflict(
                        "debug_task_conflict",
                        message=str(admission.get("message") or "单步运行准入被阻塞"),
                        details=admission.get("details"),
                    )
                raise WorkflowConflict(
                    "preflight_failed",
                    message=str(report.get("status") or "Preflight 未通过"),
                )

        def plan_builder(graph: dict[str, Any]) -> PreparedTaskInput:
            """在创建事务内冻结本次工作流任务（WorkflowTask）输入和计划。

            参数：``graph`` 是同一事务读取的已应用工作流图。返回：规范输入、
            工作流快照、执行计划（ExecutionPlan）和首次工作流节点作业
            （WorkflowNodeJob）的不可变创建载荷。异常：计划构建或输入绑定失败
            时保留原始领域错误，使外层映射为稳定公共错误且事务零写入。
            """

            source_error = self._empty_graph_source_error(graph)
            if source_error:
                raise WorkflowConflict("preflight_failed", message=source_error)
            prepared = self._prepare_task_input(
                graph,
                input_value=input_value,
                run_mode=run_mode,
                target_node_uuid=target_node_uuid,
                require_output_boundary=center_invocation_uuid is not None,
            )
            if center_invocation_uuid is not None:
                from unilabos.app.scheduler.inventory.source_bindings import validate_plan_bindings
                payload = center_source_payload
                try:
                    validate_plan_bindings(getattr(self, "_center_material_source_authority", None),
                        prepared.execution_plan, payload, task_uuid=task_uuid)
                except ValueError as error:
                    raise WorkflowConflict("preflight_failed", message=str(error)) from error
                from unilabos.workflow.material_source_context import source_context_from_payload

                prepared.execution_plan["center_material_source_context"] = source_context_from_payload(payload)
            # 明确缺料、在途或离站库存不可作为新任务来源；普通执行占用仍允许排队。
            source_checker = getattr(self._task_scheduler_bridge, "preflight_material_sources", None)
            source_checks = source_checker(prepared, task_uuid=task_uuid) if callable(source_checker) else unavailable_material_source_checks(prepared.execution_plan)
            blocked_source = next((check for check in source_checks if check["code"] in {
                "material_source_empty", "material_source_unavailable", "invalid_material_source", "material_source_batch_conflict"}), None)
            if blocked_source is not None:
                raise WorkflowConflict("preflight_failed", message=blocked_source["message"], details=blocked_source)
            if transport_context is not None:
                from unilabos.workflow.transport_pickup import pickup_binding

                try:
                    pickup_binding(graph)
                except ValueError as error:
                    raise WorkflowConflict("transport_workflow_invalid", message=str(error)) from error
                validate_vehicle_interval(
                    prepared.execution_plan, transport_context, self._resource_reference_resolver,
                )
            return prepared

        def inventory_allocation_builder(
            connection: Any,
            graph: dict[str, Any],
            prepared: PreparedTaskInput,
        ) -> list[dict[str, Any]]:
            """在 Task/Jobs 首次写入前校验并冻结数量型库存分配。

            参数：``connection`` 是工作流写事务；``graph/prepared`` 来自同一
            应用图。返回：现有分配表待插入行。异常：执行包含库存需求但没有
            可用调度桥/库存权威时关闭式失败，禁止创建无法执行的半任务。
            """

            prepare = getattr(
                self._task_scheduler_bridge,
                "prepare_inventory_allocations",
                None,
            )
            if callable(prepare):
                return prepare(
                    connection,
                    graph=graph,
                    prepared=prepared,
                    task_uuid=task_uuid,
                    bindings=normalized_inventory_bindings,
                )
            has_active_requirements = any(
                isinstance(requirement, Mapping)
                and str(requirement.get("consume_node_uuid"))
                in prepared.planned_node_uuids
                for requirement in prepared.workflow_snapshot.get(
                    "inventory_requirements",
                    [],
                )
            )
            if has_active_requirements or normalized_inventory_bindings:
                raise StoreConflict("工作流数量型库存未装配本地库存权威")
            return []

        task_created = False

        def discard_uncommitted_inventory() -> None:
            """补偿本次 Task 创建回滚前写入库存权威的数量预留。"""

            discard = getattr(
                self._task_scheduler_bridge,
                "discard_uncommitted_inventory",
                None,
            )
            if callable(discard):
                discard(task_uuid)

        try:
            with self._authoring_lock(workflow_uuid):
                applied_graph = (
                    frozen_graph
                    if frozen_graph is not None
                    else self.get_graph(workflow_uuid)
                )
                task = self._store.create_task_with_jobs(
                    workflow_uuid=workflow_uuid,
                    task_uuid=task_uuid,
                    run_mode=run_mode,
                    target_node_uuid=target_node_uuid,
                    description=description,
                    meta_data=meta_data,
                    plan_builder=plan_builder,
                    inventory_allocation_builder=inventory_allocation_builder,
                    applied_graph=applied_graph,
                    backend_task_uuid=backend_task_uuid,
                    invocation_key=invocation_key,
                    priority=priority,
                    request_fingerprint=request_fingerprint,
                    revision_fingerprint=revision_fingerprint,
                    deadline=deadline,
                    center_invocation_uuid=center_invocation_uuid,
                    validate_scheduler_compilation=(
                        self._task_scheduler_bridge is not None
                    ),
                )
            task_created = bool(task.pop("_station_submission_created", True))
            if not task_created:
                return task
            if task["status"] == "succeeded":
                # 纯数据边界工作流已经在创建事务内完成；不得再提交空物理 DAG。
                return task
            if self._task_scheduler_bridge is None:
                return task
            if center_invocation_uuid is not None and self._store.center_cancellation_requested(task["uuid"]):
                # 领取后崩溃再恢复时，仅补齐原 Task 事实；已知取消禁止首次派发。
                return task
            # ``aggregate`` 来自调度同步推进后的标准持久投影，不返回创建事务中的
            # 过期 ``pending`` 快照。
            aggregate = self._task_scheduler_bridge.submit(task)
            return aggregate["task"]
        except (TaskSchedulerBridgeError, TaskInputError, StoreConflict, EvidenceOnlyError) as error:
            if not task_created:
                try:
                    discard_uncommitted_inventory()
                except Exception:
                    raise WorkflowError("internal_error") from None
            if isinstance(error, StoreAdmissionRejected) and center_invocation_uuid is not None:
                raise WorkflowConflict("inventory_admission_failed", message=str(error)) from None
            if isinstance(error, EvidenceOnlyError):
                raise WorkflowConflict("conflict", message=str(error)) from None
            if isinstance(error, TaskSchedulerBridgeError):
                logger.exception(
                    "工作流任务已创建，但提交本地调度器失败 task=%s",
                    task_uuid,
                )
                raise WorkflowError("internal_error") from None
            if isinstance(error, StoreConflict) and backend_task_uuid is not None:
                raise WorkflowConflict("conflict", message=str(error)) from None
            if isinstance(error, StoreConflict) and str(error).startswith(
                "debug_task_conflict:"
            ):
                raise WorkflowConflict(
                    "debug_task_conflict", message=str(error)
                ) from None
            # 计划与编译错误带着可行动的具体原因，不能压成通用提示。
            code = str(getattr(error, "code", "") or "").lower()
            if code not in {"material_shortage_unhandled", "invalid_control_region"}:
                code = "invalid_input"
            raise WorkflowError(code, message=str(error) or None) from None
        except Exception:
            if not task_created:
                try:
                    discard_uncommitted_inventory()
                except Exception:
                    raise WorkflowError("internal_error") from None
            raise

    def update_workflow_task_job_param(
        self,
        task_uuid: str,
        job_uuid: str,
        *,
        param: Mapping[str, Any],
    ) -> dict[str, Any]:
        """修改暂停任务中尚未派发 Job 的参数。

        参数：``task_uuid``、``job_uuid`` 定位稳定运行事实；``param`` 是参数补丁。
        返回更新后的 Task/Job 投影。异常：任务未暂停、作业已下发或身份不一致时
        返回稳定的冲突错误；不触碰工作流拓扑和已下发参数。
        """

        if not isinstance(param, Mapping):
            raise WorkflowError("invalid_input")
        if self._task_scheduler_bridge is None:
            raise WorkflowConflict(
                "conflict",
                message="任务调度器未就绪，暂时不能修改任务参数",
            )
        try:
            return self._task_scheduler_bridge.update_pending_job_param(
                task_uuid,
                job_uuid,
                param,
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except (StoreConflict, TaskSchedulerBridgeError, ValueError) as error:
            raise WorkflowConflict("conflict", message=str(error)) from None

    def command_workflow_task(
        self,
        task_uuid: str,
        *,
        command_type: str,
        target_node_uuid: str | None,
        idempotency_key: str,
        description: str | None,
        meta_data: dict[str, Any],
    ) -> dict[str, Any]:
        """幂等执行本地工作流任务控制命令。

        暂停只阻止后续派发，不中断已在途设备动作；恢复、单步切换和取消都复用
        同一个 Task 身份。返回与 Backend 相同的 WorkflowTaskCommand 投影。
        """

        try:
            task_uuid = validate_uuid(task_uuid)
            if command_type not in {
                "step",
                "switch_to_step",
                "pause",
                "resume",
                "cancel",
                "unlock_resources",
            }:
                raise WorkflowError("invalid_input")
            if command_type in {"step", "switch_to_step"} and not get_runtime_capabilities().can_debug:
                raise WorkflowError("debug_not_allowed")
            if target_node_uuid is not None:
                target_node_uuid = validate_uuid(target_node_uuid)
            if command_type != "step" and target_node_uuid is not None:
                raise WorkflowError("invalid_input")
            normalized_key = str(idempotency_key).strip()
            if not normalized_key:
                raise WorkflowError("invalid_input")
            meta_data = normalize_json_object(meta_data)
            description = self._optional_text(description)
            if command_type == "unlock_resources" and (
                meta_data.get("confirmed_physical_safe") is not True
                or description is None
            ):
                raise WorkflowError("invalid_input")
            task = self._store.get_task(task_uuid)
            command, created = self._store.create_task_command(
                task_uuid=task_uuid,
                command_uuid=str(uuid4()),
                command_type=command_type,
                target_node_uuid=target_node_uuid,
                idempotency_key=normalized_key,
                description=description,
                meta_data=meta_data,
            )
            # 取消落盘后、调度器受理前退出时，重放同一控制身份继续取消。
            # 其他控制动作沿用原行为，不能因重放重复单步或恢复派发。
            if command["status"] != "pending" or (not created and command_type != "cancel"):
                return command
            if command_type == "unlock_resources":
                if task.get("status") not in {"failed", "canceled", "timeout"}:
                    return self._store.complete_task_command(
                        command["uuid"],
                        status="rejected",
                        result={"reason": "task_is_not_abnormal_terminal"},
                    )
                if self._task_scheduler_bridge is None:
                    return self._store.complete_task_command(
                        command["uuid"],
                        status="rejected",
                        result={"reason": "scheduler_unavailable"},
                    )
                try:
                    result = self._task_scheduler_bridge.unlock_resources(
                        task_uuid,
                        command_uuid=command["uuid"],
                        reason=description,
                    )
                except TaskSchedulerBridgeError as error:
                    return self._store.complete_task_command(
                        command["uuid"],
                        status="rejected",
                        result={"reason": str(error)},
                    )
                return self._store.complete_task_command(
                    command["uuid"],
                    status="succeeded",
                    result=result,
                )
            if task.get("status") in {
                "succeeded",
                "success",
                "failed",
                "canceled",
                "timeout",
            }:
                return self._store.complete_task_command(
                    command["uuid"],
                    status="rejected",
                    result={"reason": "task_is_terminal"},
                )
            execution_mode = str(
                task.get("execution_mode") or task.get("run_mode") or "normal"
            )
            dispatch_gate = str(task.get("dispatch_gate") or "active")
            if (
                command_type == "resume"
                and execution_mode == "step"
                and not get_runtime_capabilities().can_debug
            ):
                return self._store.complete_task_command(
                    command["uuid"],
                    status="rejected",
                    result={"reason": "debug_not_allowed"},
                )
            # 暂停中的自动运行也可以单步：先切回 step，再只派发一个节点。
            paused_for_step = (
                task.get("control_status") == "paused" and dispatch_gate == "paused"
            )
            if command_type == "step" and not (
                paused_for_step and execution_mode in {"step", "normal"}
            ):
                return self._store.complete_task_command(
                    command["uuid"],
                    status="rejected",
                    result={"reason": "task_is_not_paused_step"},
                )
            if command_type in {"pause", "switch_to_step"} and execution_mode != "normal":
                return self._store.complete_task_command(
                    command["uuid"],
                    status="rejected",
                    result={"reason": "task_is_not_in_normal_mode"},
                )
            normal_resume_invalid = (
                execution_mode == "normal" and dispatch_gate != "paused"
            )
            step_resume_invalid = execution_mode == "step" and (
                task.get("control_status") != "paused" or dispatch_gate != "paused"
            )
            if command_type == "resume" and (
                execution_mode not in {"normal", "step"}
                or normal_resume_invalid
                or step_resume_invalid
            ):
                return self._store.complete_task_command(
                    command["uuid"],
                    status="rejected",
                    result={
                        "reason": (
                            "task_is_not_paused_step"
                            if execution_mode == "step"
                            else "task_is_not_paused"
                            if execution_mode == "normal"
                            else "task_is_not_resumable"
                        )
                    },
                )
            if self._task_scheduler_bridge is None:
                return self._store.complete_task_command(
                    command["uuid"],
                    status="rejected",
                    result={"reason": "scheduler_unavailable"},
                )
            try:
                if command_type == "step":
                    result = self._task_scheduler_bridge.step(
                        task_uuid,
                        target_node_uuid=target_node_uuid,
                    )
                elif command_type == "switch_to_step":
                    result = self._task_scheduler_bridge.switch_to_step(task_uuid)
                elif command_type == "pause":
                    result = self._task_scheduler_bridge.pause(task_uuid)
                elif command_type == "resume":
                    result = self._task_scheduler_bridge.resume(task_uuid)
                else:
                    result = self._task_scheduler_bridge.cancel(
                        task_uuid,
                        command_uuid=command["uuid"],
                    )
            except TaskSchedulerBridgeError as error:
                return self._store.complete_task_command(
                    command["uuid"],
                    status="rejected",
                    result={"reason": str(error)},
                )
            return self._store.complete_task_command(
                command["uuid"],
                status="succeeded",
                result=result,
            )
        except WorkflowError:
            raise
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except (StoreConflict, ValueError):
            raise WorkflowError("invalid_input") from None

    def create_debug_workflow_task(
        self,
        *,
        workflow_uuid: str,
        start_node_uuids: list[str],
        breakpoint_node_uuids: list[str],
        priority: WorkflowTaskPriority | str | float = WorkflowTaskPriority.NORMAL,
        input_value: dict[str, Any],
        description: str | None,
        meta_data: dict[str, Any],
    ) -> dict[str, Any]:
        """创建带不可变起始点、断点和首个 Admission Hold 的调试任务。

        参数：``priority`` 接受 ``normal``/``high`` 字符串枚举并随任务持久化；
        旧的数值优先级仅为兼容已有内部调用，调度排序由后续调度器负责。其余
        参数分别定义工作流、起始节点、断点、输入和审计信息。返回：标准
        WorkflowTask 投影。异常：输入、工作流图或任务写入不合法时抛出稳定
        ``WorkflowError``，失败不留下半个调试任务。
        """

        if not get_runtime_capabilities().can_debug:
            raise WorkflowError("debug_not_allowed")
        workflow_uuid = self._ensure_workflow_execution_allowed(workflow_uuid)["uuid"]
        try:
            normalized_starts = [validate_uuid(value) for value in start_node_uuids]
            normalized_breakpoints = [
                validate_uuid(value) for value in breakpoint_node_uuids
            ]
            if len(normalized_starts) != 1 or len(set(normalized_starts)) != 1:
                raise ValueError
            if len(set(normalized_breakpoints)) != len(normalized_breakpoints):
                raise ValueError
            input_value = normalize_json_object(input_value)
            meta_data = normalize_json_object(meta_data)
        except (TypeError, ValueError):
            raise WorkflowError("invalid_input") from None
        description = self._optional_text(description)
        meta_data = {**meta_data, "debug": True}
        start_node_uuid = normalized_starts[0]

        def plan_builder(graph: dict[str, Any]) -> PreparedTaskInput:
            prepared = self._prepare_task_input(
                graph,
                input_value=input_value,
                run_mode="step",
                target_node_uuid=None,
            )
            return self._scope_debug_task_input(
                prepared,
                start_node_uuid=start_node_uuid,
                breakpoint_node_uuids=normalized_breakpoints,
            )

        try:
            with self._authoring_lock(workflow_uuid):
                applied_graph = self.get_graph(workflow_uuid)
                task = self._store.create_task_with_jobs(
                    workflow_uuid=workflow_uuid,
                    task_uuid=str(uuid4()),
                    run_mode="step",
                    target_node_uuid=None,
                    priority=priority,
                    description=description,
                    meta_data=meta_data,
                    plan_builder=plan_builder,
                    applied_graph=applied_graph,
                )
                self._store.create_debug_configuration(
                    task_uuid=task["uuid"],
                    start_node_uuids=normalized_starts,
                    breakpoint_node_uuids=normalized_breakpoints,
                )
            if self._task_scheduler_bridge is None:
                return task
            return self._task_scheduler_bridge.submit(task)["task"]
        except (TaskInputError, StoreConflict, ValueError, EvidenceOnlyError) as error:
            if isinstance(error, EvidenceOnlyError):
                raise WorkflowConflict("conflict", message=str(error)) from None
            if isinstance(error, StoreConflict) and str(error).startswith(
                "debug_task_conflict:"
            ):
                raise WorkflowConflict(
                    "debug_task_conflict", message=str(error)
                ) from None
            raise WorkflowError("invalid_input") from None
        except TaskSchedulerBridgeError:
            raise WorkflowError("internal_error") from None

    def install_center_material_source_authority(self, authority: Any, session_provider: Any) -> None:
        """中心启用时装配来源证明；普通本地执行不需要该能力。"""
        self._center_material_source_authority = authority
        self._center_material_source_session = session_provider

    def material_source_bindings(self, *, workflow_uuid: str, revision_fingerprint: str, input_value: dict[str, Any]) -> dict[str, Any]:
        """只读解析指定发布合同的实际独占来源，不创建 Task 或取得库存占用。"""
        from unilabos.app.scheduler.inventory.domain import InventoryError
        from unilabos.app.scheduler.inventory.source_bindings import bindings_for_plan, source_nodes
        from unilabos.center_protocol.digest import digest
        from unilabos.workflow.station_contract import bind_station_contract

        authority = getattr(self, "_center_material_source_authority", None)
        provider = getattr(self, "_center_material_source_session", None)
        session = provider() if provider else None
        if session is None or (authority is not None and (session.station_uuid != authority.station_uuid or session.instance_uuid != authority.instance_uuid)):
            raise WorkflowConflict("preflight_failed", message="中心静态来源证明缺少当前工站控制会话")
        contracts = self._published_contract_store()
        try:
            contract = contracts.get_by_revision_fingerprint(workflow_uuid=workflow_uuid, revision_fingerprint=revision_fingerprint)
            graph = bind_station_contract(contract, self._resource_reference_resolver, contracts.frozen_dependency)
            prepared = self._prepare_task_input(graph, input_value=input_value, run_mode="normal", target_node_uuid=None)
            if source_nodes(prepared.execution_plan) and authority is None:
                raise ValueError("中心静态来源证明缺少本站库存权威")
            bindings = bindings_for_plan(authority, prepared.execution_plan) if authority is not None else []
        except (KeyError, ValueError, InventoryError) as error:
            raise WorkflowConflict("preflight_failed", message=str(error)) from error
        return {"binding_version": 1, "workflow_uuid": workflow_uuid, "revision_fingerprint": revision_fingerprint,
            "input_digest": digest(input_value), "bindings": bindings,
            **{key: getattr(session, key) for key in ("station_uuid", "instance_uuid", "process_uuid", "session_uuid", "session_epoch")}}

    def install_center_material_input_authority(self, authority: Any) -> None:
        """由中心桥显式装配真实库存；公共提交服务同样必须通过这道准入。"""
        with self._center_material_input_lock:
            existing = getattr(self, "_center_material_input_authority", None)
            if existing is not None and existing.store is not authority.store:
                raise ValueError("不能在运行中替换中心物料输入的库存权威")
            self._center_material_input_authority = authority

    def _claimed_historical_contract(
        self, invocation_uuid: str, submission: StationWorkflowSubmission, *, connection: Any = None,
    ) -> dict[str, Any] | None:
        """已领取原调用可读取下架后的冻结历史；新准入不借此恢复可见性。"""
        from unilabos.workflow.center_boundary import validate_claimed_submission
        from unilabos.workflow.published_contract import validate_historical_contract

        # Task 创建前的库存核实已经持有工作流写事务，必须复用同一事务。
        with (nullcontext(connection) if connection is not None else self._store.transaction()) as connection:
            validate_claimed_submission(connection, invocation_uuid,
                workflow_uuid=submission.workflow_id, revision_fingerprint=submission.revision_fingerprint,
                request_fingerprint=submission.request_fingerprint)
        contract = self._published_contract_store().historical_by_revision_fingerprint(
            workflow_uuid=submission.workflow_id, revision_fingerprint=submission.revision_fingerprint or "")
        if contract is None and self._publication_catalog is not None:
            for entry in self._publication_catalog.list_entries():
                historical = entry["contract"]
                if (historical["workflow_uuid"] == submission.workflow_id
                        and historical["source_hash"] == submission.revision_fingerprint):
                    contract = historical
                    break
        if contract is not None:
            try:
                validate_historical_contract(contract)
            except PublishedContractInvalid as error:
                raise WorkflowConflict("workflow_revision_conflict", message=str(error)) from error
        return contract

    def center_material_input_items(self, package: dict[str, Any], *, connection: Any = None) -> list[dict[str, str]]:
        """领取前用本站原发布合同核对中心冻结包；不从当前可编辑定义猜输入。"""
        from unilabos.app.center_client.catalog import project_contract
        from unilabos.center_protocol.task_package import validate_task_package
        from unilabos.workflow.material_ports import input_material_items

        payload = validate_task_package(package)["payload"]
        contracts = self._published_contract_store()
        try:
            contract = contracts.get_by_revision_fingerprint(
                workflow_uuid=payload["workflow_uuid"], revision_fingerprint=payload["revision_fingerprint"],
            )
        except KeyError as error:
            prepared = prepare_station_workflow_submission(
                backend_task_uuid=payload["global_task_uuid"], invocation_key=payload["invocation_key"],
                workflow_name=None, workflow_id=payload["workflow_uuid"],
                revision_fingerprint=payload["revision_fingerprint"], input_value=payload["input"],
                priority=WorkflowTaskPriority.NORMAL, inventory_bindings=None, description=None, meta_data=None,
            )
            try:
                contract = self._claimed_historical_contract(payload["station_invocation_uuid"], prepared, connection=connection)
            except StoreConflict as conflict:
                from unilabos.center_protocol.material_input import MaterialClaimConfirmationRequired

                raise MaterialClaimConfirmationRequired("本地历史合同没有对应的原领取证明") from conflict
            if contract is None:
                raise ValueError("本地没有目标输入对应的冻结发布合同") from error
        projected = project_contract(contracts.public(contract))
        if any(projected[key] != payload[key] for key in (
            "contract_uuid", "workflow_uuid", "publication_version", "revision_fingerprint",
            "contract_digest", "projection_digest", "adapter_version",
        )):
            raise ValueError("目标物料输入与本地冻结合同身份或摘要不匹配")
        from unilabos.app.scheduler.inventory.source_bindings import validate_plan_bindings
        from unilabos.workflow.station_contract import bind_station_contract

        graph = bind_station_contract(contract, self._resource_reference_resolver, contracts.frozen_dependency)
        prepared = self._prepare_task_input(graph, input_value=payload["input"], run_mode="normal", target_node_uuid=None)
        validate_plan_bindings(getattr(self, "_center_material_source_authority", None), prepared.execution_plan, payload)
        return input_material_items(projected["material_ports"], payload["input"])

    def transport_preparation_candidates(self, request: dict[str, Any]) -> list[Any]:
        """只读发现同一已发布合同的兼容车辆，不创建 Task 或物理命令。"""
        from unilabos.workflow.transport_candidates import candidates
        if self._task_scheduler_bridge is None:
            raise ValueError("物流执行调度桥尚未就绪")
        contract = self._published_contract_store().get(request["contract_uuid"])
        return candidates(contract, request, self._resource_reference_resolver,
                          self._task_scheduler_bridge.preflight_transport_node)

    def submit_station_workflow(
        self,
        *,
        backend_task_uuid: str,
        invocation_key: str,
        workflow_name: str | None,
        workflow_id: str | None = None,
        revision_fingerprint: str | None = None,
        input_value: dict[str, Any],
        priority: WorkflowTaskPriority | str | float = WorkflowTaskPriority.NORMAL,
        deadline: str | None = None,
        inventory_bindings: list[dict[str, Any]] | None = None,
        description: str | None = None,
        meta_data: dict[str, Any] | None = None,
        center_invocation_uuid: str | None = None,
    ) -> dict[str, Any]:
        """按工作流名称和入口参数幂等创建一次工站工作流调用。

        参数：``backend_task_uuid`` 关联全局 Backend Task；``invocation_key`` 在
        同一全局计划内唯一标识一次工站调用；``workflow_name`` 只解析唯一的当前
        工作流；``input_value`` 是唯一允许的运行参数；其余字段提供优先级、数量
        库存绑定和审计信息；``center_invocation_uuid`` 要求同库已领取的冻结中心
        意图，受理与 Task/Jobs 同事务提交。返回本地 WorkflowTask 投影。异常：UUID、名称、调用
        键、优先级非法，名称不存在/不唯一，或同一调用键重放不同内容时抛稳定
        ``WorkflowError``，且不会创建第二组 Job。
        """

        try:
            if center_invocation_uuid is not None:
                if str(UUID(center_invocation_uuid)) != center_invocation_uuid:
                    raise ValueError("中心调用 UUID 必须规范化")
            submission = prepare_station_workflow_submission(
                backend_task_uuid=backend_task_uuid,
                invocation_key=invocation_key,
                workflow_name=workflow_name,
                workflow_id=workflow_id,
                revision_fingerprint=revision_fingerprint,
                input_value=input_value,
                priority=priority,
                deadline=deadline,
                inventory_bindings=inventory_bindings,
                description=description,
                meta_data=meta_data,
            )
        except (StationWorkflowSubmissionInvalid, ValueError, TypeError, AttributeError):
            raise WorkflowError("invalid_input") from None

        try:
            existing = self._store.get_station_task_by_invocation(
                backend_task_uuid=submission.backend_task_uuid,
                invocation_key=submission.invocation_key,
                request_fingerprint=submission.request_fingerprint,
            )
        except StoreConflict as error:
            raise WorkflowConflict("conflict", message=str(error)) from None
        if existing is not None:
            if center_invocation_uuid is not None:
                recovered = self.recover_center_workflow_invocation(center_invocation_uuid)
                if recovered is None or recovered["uuid"] != existing["uuid"]:
                    raise WorkflowConflict("conflict", message="中心调用与既有本地任务不匹配")
                return recovered
            return existing

        frozen_graph: dict[str, Any] | None = None
        if submission.workflow_id is not None:
            try:
                contract = self._published_contract_store().get_by_revision_fingerprint(
                    workflow_uuid=submission.workflow_id,
                    revision_fingerprint=submission.revision_fingerprint or "",
                )
            except KeyError:
                contract = None
                if center_invocation_uuid is not None:
                    contract = self._claimed_historical_contract(center_invocation_uuid, submission)
                if contract is None:
                    raise WorkflowConflict(
                        "workflow_revision_conflict",
                        message="请求的工作流发布修订不存在或指纹不匹配",
                    ) from None
            workflow = {"uuid": submission.workflow_id}
            from unilabos.workflow.station_contract import bind_station_contract

            binding = None
            if center_invocation_uuid is not None:
                from unilabos.workflow.center_boundary import claimed_transport_binding
                try:
                    with self._store.transaction() as c:
                        binding = claimed_transport_binding(c, center_invocation_uuid)
                except StoreConflict as error:
                    raise WorkflowConflict("conflict", message=str(error)) from error

            try:
                frozen_graph = bind_station_contract(
                    contract, self._resource_reference_resolver,
                    self._published_contract_store().frozen_dependency,
                    transport_binding=binding,
                )
            except ValueError as error:
                raise WorkflowConflict("workflow_executor_unavailable", message=str(error)) from error
        else:
            matches: list[dict[str, Any]] = []
            page = 1
            while True:
                result = self.list_workflows(
                    page=page,
                    page_size=100,
                    name=submission.workflow_name or "",
                )
                matches.extend(
                    workflow
                    for workflow in result["items"]
                    if workflow["name"] == submission.workflow_name
                )
                if page * result["page_size"] >= result["total"]:
                    break
                page += 1
            if not matches:
                raise WorkflowError("workflow_not_found")
            if len(matches) != 1:
                raise WorkflowConflict(
                    "conflict",
                    message="工站工作流名称不唯一，无法安全选择运行定义",
                )
            workflow = matches[0]
        return self.create_workflow_task(
            workflow_uuid=workflow["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value=submission.input_value,
            description=submission.description,
            meta_data=submission.meta_data,
            inventory_bindings=list(submission.inventory_bindings),
            backend_task_uuid=submission.backend_task_uuid,
            invocation_key=submission.invocation_key,
            priority=submission.priority,
            request_fingerprint=submission.request_fingerprint,
            revision_fingerprint=submission.revision_fingerprint,
            deadline=submission.deadline,
            frozen_graph=frozen_graph,
            center_invocation_uuid=center_invocation_uuid,
        )

    def inspect_existing_center_task(self, package: dict[str, Any], *, local_task_uuid: str | None = None,
                                     allow_terminal: bool = False) -> dict[str, Any]:
        """公开只读核实入口，只核对现存 Task，不提交、领取或调度任务。"""
        from unilabos.workflow.center_boundary import inspect_existing_task

        try:
            with self._store.read() as connection:
                task = inspect_existing_task(connection, package, local_task_uuid=local_task_uuid,
                                             allow_terminal=allow_terminal)
                return {"local_task_uuid": task["uuid"], "status": task["status"]}
        except StoreConflict as error:
            raise WorkflowConflict("conflict", message=str(error)) from error

    def apply_center_reconciliation(self, request_uuid: str) -> dict[str, Any]:
        """精确回执后再次核对现存 Task，只修复中心绑定和事实发件箱。"""
        from unilabos.workflow.center_boundary import apply_reconciliation

        try:
            with self._store.transaction() as connection:
                task_uuid = apply_reconciliation(connection, request_uuid)
                # 回执丢失期间原任务可能完成并软删除，核实仍返回保留的事实。
                task = connection.execute("SELECT status FROM workflow_task WHERE uuid=?", (task_uuid,)).fetchone()
                return {"local_task_uuid": task_uuid, "status": task["status"]}
        except StoreConflict as error:
            raise WorkflowConflict("conflict", message=str(error)) from error

    def recover_center_workflow_invocation(self, invocation_uuid: str) -> dict[str, Any] | None:
        """恢复已有中心调用的持久事实；不读取当前定义、不领取、不创建或调度任务。"""
        try:
            if str(UUID(invocation_uuid)) != invocation_uuid:
                raise ValueError("中心调用 UUID 必须规范化")
            return self._store.recover_center_workflow_invocation(invocation_uuid)
        except (ValueError, TypeError, AttributeError):
            raise WorkflowError("invalid_input") from None
        except StoreConflict as error:
            raise WorkflowConflict("conflict", message=str(error)) from None

    def find_station_workflow_submission(
        self, *, backend_task_uuid: str, invocation_key: str,
        workflow_name: str | None, workflow_id: str | None = None,
        revision_fingerprint: str | None = None, input_value: dict[str, Any],
    ) -> dict[str, Any] | None:
        """只读恢复中心提交窗口，包含已软删除任务；绝不创建或调度新执行。"""
        try:
            submission = prepare_station_workflow_submission(
                backend_task_uuid=backend_task_uuid, invocation_key=invocation_key,
                workflow_name=workflow_name, workflow_id=workflow_id,
                revision_fingerprint=revision_fingerprint, input_value=input_value,
                priority=WorkflowTaskPriority.NORMAL, deadline=None, inventory_bindings=None,
                description=None, meta_data=None,
            )
            return self._store.get_station_task_by_invocation(
                backend_task_uuid=submission.backend_task_uuid,
                invocation_key=submission.invocation_key,
                request_fingerprint=submission.request_fingerprint,
            )
        except StationWorkflowSubmissionInvalid:
            raise WorkflowError("invalid_input") from None
        except StoreConflict as error:
            raise WorkflowConflict("conflict", message=str(error)) from None

    def get_station_workflow_result(self, task_uuid: str) -> dict[str, Any] | None:
        """返回真实持久工作流结果；未产生输出边界时返回 None。"""
        try:
            return self._store.get_station_workflow_result(validate_uuid(task_uuid))
        except ValueError:
            raise WorkflowError("invalid_input") from None

    def get_debug_workflow_task(self, task_uuid: str) -> dict[str, Any]:
        """返回标准 Task/Jobs 与调试配置、范围和 Hold 的三源一致投影。"""

        if not get_runtime_capabilities().can_debug:
            raise WorkflowError("debug_not_allowed")
        try:
            task_uuid = validate_uuid(task_uuid)
            task = self._store.get_task(task_uuid)
            jobs = self._store.list_jobs(task_uuid)
            debug = self._store.get_debug_projection(task_uuid)
        except (StoreNotFound, ValueError):
            raise WorkflowError("not_found") from None
        snapshot_nodes = task.get("workflow_snapshot", {}).get("nodes", [])
        disabled = [
            str(node.get("uuid"))
            for node in snapshot_nodes
            if isinstance(node, Mapping) and node.get("disabled") is True
        ]
        enabled = [
            str(node.get("uuid"))
            for node in snapshot_nodes
            if isinstance(node, Mapping)
            and node.get("disabled") is not True
            and node.get("uuid")
        ]
        active = [
            str(node.get("uuid"))
            for node in task.get("execution_plan", {}).get("nodes", [])
            if isinstance(node, Mapping) and node.get("uuid")
        ]
        active_set = set(active)
        return {
            "task": task,
            "jobs": jobs,
            **debug,
            "active_node_uuids": active,
            "out_of_scope_node_uuids": [
                node_uuid for node_uuid in enabled if node_uuid not in active_set
            ],
            "disabled_node_uuids": disabled,
        }

    def command_debug_workflow_task(
        self,
        task_uuid: str,
        *,
        command_type: str,
        scope_type: str,
        hold_uuid: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """按精确 Hold 范围幂等执行调试单步或继续。"""

        if not get_runtime_capabilities().can_debug:
            raise WorkflowError("debug_not_allowed")
        try:
            task_uuid = validate_uuid(task_uuid)
            hold_uuid = validate_uuid(hold_uuid)
            if command_type not in {"step", "continue"} or scope_type != "hold":
                raise ValueError
            normalized_key = str(idempotency_key).strip()
            if not normalized_key:
                raise ValueError
            command, created, node_uuid = self._store.begin_debug_command(
                task_uuid=task_uuid,
                command_uuid=str(uuid4()),
                command_type=command_type,
                hold_uuid=hold_uuid,
                idempotency_key=normalized_key,
            )
            if not created or command["status"] != "pending":
                return command
            if self._task_scheduler_bridge is None:
                return self._store.complete_debug_command(
                    command["uuid"],
                    status="rejected",
                    result={"reason": "scheduler_unavailable"},
                )
            try:
                result = self._task_scheduler_bridge.step(
                    task_uuid,
                    target_node_uuid=node_uuid,
                )
            except TaskSchedulerBridgeError as error:
                return self._store.complete_debug_command(
                    command["uuid"],
                    status="rejected",
                    result={"reason": str(error)},
                )
            return self._store.complete_debug_command(
                command["uuid"], status="succeeded", result=result
            )
        except WorkflowError:
            raise
        except (StoreNotFound, StoreConflict, TypeError, ValueError):
            raise WorkflowError("invalid_input") from None

    def create_device_action_run(
        self,
        *,
        material_uuid: str,
        workflow_node_template_uuid: str,
        param: dict[str, Any] | None,
        execution_policy: dict[str, Any] | None,
        idempotency_key: str,
        description: str | None,
        meta_data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """创建或幂等复用设备单动作运行（DeviceActionRun）。

        参数与 Backend ``POST /device-action-runs`` DTO 同名；返回标准工作流任务
        （WorkflowTask）、唯一工作流节点作业（WorkflowNodeJob）和 ``created``。
        输入/引用错误、依赖未装配和幂等冲突分别映射为稳定 HTTP 业务错误。
        公共任务调度桥失败映射为 ``internal_error``，且不创建第二套执行身份。
        """

        if not get_runtime_capabilities().can_execute_device_actions:
            raise WorkflowError("device_action_not_allowed")
        try:
            # ``aggregate`` 是已原子持久化的标准工作流任务（WorkflowTask）和
            # 工作流节点作业（WorkflowNodeJob）；只有首次创建才允许物理派发。
            aggregate = self._device_action_runs.create(
                material_uuid=material_uuid,
                workflow_node_template_uuid=workflow_node_template_uuid,
                param=param,
                execution_policy=execution_policy,
                idempotency_key=idempotency_key,
                description=description,
                meta_data=meta_data,
            )
            if aggregate["created"] is True and self._task_scheduler_bridge is not None:
                scheduled = self._task_scheduler_bridge.submit(aggregate["task"])
                # ``scheduled_jobs`` 是公共桥返回的同一任务作业集合；设备单动作
                # 必须仍精确包含创建事务生成的唯一作业身份。
                scheduled_jobs = [
                    job
                    for job in scheduled["jobs"]
                    if job.get("uuid") == aggregate["job"]["uuid"]
                ]
                if len(scheduled_jobs) != 1:
                    raise TaskSchedulerBridgeError(
                        "设备单动作调度结果缺少唯一原始作业身份"
                    )
                # 公共桥可能同步推进首次派发，必须返回同一任务/作业身份的刷新状态，
                # 不能把创建事务中的 ``pending`` 快照误报给前端。
                aggregate = {
                    "task": scheduled["task"],
                    "job": scheduled_jobs[0],
                    "created": True,
                }
            # DeviceActionRun 的创建响应与标准任务详情保持同一删除资格投影，
            # 让调用方在首次创建后即可稳定渲染终态/资源门禁字段。
            task_projection = aggregate.get("task")
            if isinstance(task_projection, dict) and task_projection.get("uuid"):
                eligibility = self._store.task_delete_eligibility(
                    str(task_projection["uuid"])
                )
                aggregate["task"] = {
                    **task_projection,
                    "can_delete": bool(eligibility["can_delete"]),
                    "delete_blockers": list(eligibility["delete_blockers"]),
                }
            return aggregate
        except DeviceActionRunInputError:
            raise WorkflowError("invalid_input") from None
        except DeviceActionRunUnavailable:
            raise WorkflowError("template_catalog_unavailable") from None
        except DeviceActionRunConflict as error:
            if str(error).startswith("debug_task_conflict:"):
                raise WorkflowConflict(
                    "debug_task_conflict", message=str(error)
                ) from None
            raise WorkflowConflict("conflict") from None
        except TaskSchedulerBridgeError:
            raise WorkflowError("internal_error") from None

    def list_task_inventory_consumptions(self, task_uuid: str) -> list[dict[str, Any]]:
        """读取一个工作流任务的数量型库存消费事实。

        参数：``task_uuid`` 是任务身份。返回：统一库存台账还原的消费列表；未
        装配库存权威时为空。异常：非法任务身份映射为公共参数错误。
        """

        try:
            task_uuid = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            self._store.get_task(task_uuid)
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        reader = getattr(
            self._task_scheduler_bridge,
            "list_task_inventory_consumptions",
            None,
        )
        return reader(task_uuid) if callable(reader) else []

    def list_task_material_events(self, task_uuid: str) -> list[dict[str, Any]]:
        """读取一个工作流任务造成的物料变化历史，并标注对应的步骤节点。

        参数：``task_uuid`` 是任务身份。返回：按发生顺序排列的只读列表，每项含
        变化类型（搬运/消耗/标为已使用）、物料、搬运前后库位或用量，以及产生它的
        作业、节点名称和节点所在的上级步骤路径；未装配库存权威时为空。
        异常：非法任务身份映射为公共参数错误，任务不存在映射为 not_found。
        """

        try:
            task_uuid = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            task = self._store.get_task(task_uuid)
            jobs = self._store.list_jobs(task_uuid)
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        reader = getattr(self._task_scheduler_bridge, "list_task_material_events", None)
        if not callable(reader):
            return []
        created = str(task.get("create_time") or "")
        try:
            since_ms = int(
                datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            since_ms = 0
        node_of_job = {str(job["uuid"]): str(job.get("workflow_node_uuid") or "") for job in jobs}
        events = reader(task_uuid, set(node_of_job), since_ms=since_ms)
        snapshot = task.get("workflow_snapshot") or {}
        nodes = {
            str(node.get("uuid")): node
            for node in snapshot.get("nodes", [])
            if isinstance(node, Mapping)
        }

        def step_path(node_uuid: str) -> list[str]:
            path: list[str] = []
            seen: set[str] = set()
            parent = str((nodes.get(node_uuid) or {}).get("parent_uuid") or "")
            while parent and parent in nodes and parent not in seen:
                seen.add(parent)
                name = str(nodes[parent].get("name") or "")
                if name:
                    path.append(name)
                parent = str(nodes[parent].get("parent_uuid") or "")
            return list(reversed(path))

        result = []
        for event in events:
            node_uuid = node_of_job.get(str(event.get("workflow_node_job_uuid") or ""), "")
            node = nodes.get(node_uuid) or {}
            result.append({
                **event,
                "workflow_task_uuid": task_uuid,
                "workflow_node_uuid": node_uuid or None,
                "node_name": node.get("name"),
                "step_path": step_path(node_uuid) if node_uuid else [],
            })
        return result

    def list_job_inventory_consumptions(self, job_uuid: str) -> list[dict[str, Any]]:
        """读取一个工作流节点作业的数量型库存消费事实。

        参数：``job_uuid`` 是作业身份。返回：统一库存台账还原的消费列表；未
        装配库存权威时为空。异常：非法作业身份映射为公共参数错误。
        """

        try:
            job_uuid = validate_uuid(job_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            self._store.get_job(job_uuid)
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        reader = getattr(
            self._task_scheduler_bridge,
            "list_job_inventory_consumptions",
            None,
        )
        return reader(job_uuid) if callable(reader) else []

    def list_reagent_inventory_consumptions(
        self, reagent_uuid: str
    ) -> list[dict[str, Any]]:
        """读取一个试剂库存实例的工作流消费谱系。

        参数：``reagent_uuid`` 是库存实例身份。返回：统一库存台账还原的消费
        列表，允许已删除库存保留历史；未装配库存权威时为空。异常：非法身份映射
        为公共参数错误。
        """

        try:
            reagent_uuid = validate_uuid(reagent_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        reader = getattr(
            self._task_scheduler_bridge,
            "list_reagent_inventory_consumptions",
            None,
        )
        return reader(reagent_uuid) if callable(reader) else []

    def get_workflow_task(self, task_uuid: str, *, projection: str = "full") -> dict[str, Any]:
        if projection not in {"full", "state"}:
            raise WorkflowError("invalid_input")
        try:
            identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            task = (
                self._store.get_task_state(identity)
                if projection == "state" else self._store.get_task(identity)
            )
            task = self._project_manual_review_attention([task])[0]
            eligibility = self._store.task_delete_eligibility(identity)
            return {
                **task,
                "can_delete": bool(eligibility["can_delete"]),
                "delete_blockers": list(eligibility["delete_blockers"]),
            }
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def get_workflow_task_delete_eligibility(self, task_uuid: str) -> dict[str, Any]:
        """按任务按需读取软删除资格，供控制台在删除前核验。

        参数：``task_uuid`` 是任务稳定 UUID。返回：``can_delete``、
        ``delete_blockers`` 与状态事实的紧凑投影；任务列表保持摘要读契约，
        资格只在这里逐任务计算，避免列表接口放大成 N 次资源扫描。异常：非法
        身份映射为 ``invalid_input``；任务不存在映射为 ``not_found``。状态
        不变量：只读，且不能替代删除事务内的权威门禁。
        """

        try:
            identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            eligibility = self._store.task_delete_eligibility(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        return {
            "workflow_task_uuid": identity,
            "can_delete": bool(eligibility["can_delete"]),
            "already_deleted": bool(eligibility["already_deleted"]),
            "delete_blockers": list(eligibility["delete_blockers"]),
            "status": str(eligibility["status"] or ""),
            "cleanup_status": str(eligibility["cleanup_status"] or "none"),
            "deleted_at": eligibility["deleted_at"],
        }

    def delete_workflow_task(self, task_uuid: str) -> dict[str, Any]:
        """软删除一个已终态且全部资源已释放的 Edge WorkflowTask。

        参数：``task_uuid`` 是任务稳定 UUID。返回：``deleted`` 或幂等的
        ``already_deleted`` 结果。异常：不存在映射为 ``not_found``；终态、清理或
        资源门禁不满足映射为带阻塞原因的 ``conflict``。软删除只隐藏根任务，保留
        Job、运行事件、库存和设备历史。
        """

        try:
            identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self._store.delete_task(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreConflict as error:
            raise WorkflowConflict("conflict", message=str(error)) from None

    def _project_manual_review_attention(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """展示资格独立于持久控制状态，不清除任何未结算事实。"""
        resolved = self._store.manual_review_attention_resolved_for_tasks(task["uuid"] for task in tasks)
        return [{**task, "manual_review_attention_resolved": resolved.get(task["uuid"], False)} for task in tasks]

    def list_workflow_task_execution_locks(
        self,
        task_uuid: str,
    ) -> dict[str, Any]:
        """读取任务详情页的活动执行锁与人工释放资格。"""

        try:
            identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return TaskRuntimeProjection(self._store).list_task_execution_locks(
                identity
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def force_release_workflow_task_execution_lock(
        self,
        task_uuid: str,
        lease_uuid: str,
        *,
        expected_claim_uuid: str,
        expected_fencing_token: int,
        reason: str,
        physical_settlement_confirmed: bool,
    ) -> dict[str, Any]:
        """执行带物理确认与 CAS 校验的人工锁释放，并唤醒调度器。"""

        try:
            task_identity = validate_uuid(task_uuid)
            lease_identity = validate_uuid(lease_uuid)
            expected_claim_identity = validate_uuid(expected_claim_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        if (
            isinstance(expected_fencing_token, bool)
            or not isinstance(expected_fencing_token, int)
            or expected_fencing_token <= 0
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason.strip()) > 500
            or not isinstance(physical_settlement_confirmed, bool)
        ):
            raise WorkflowError("invalid_input")
        projection = TaskRuntimeProjection(self._store)
        try:
            result = projection.force_release_execution_lock(
                task_identity,
                lease_identity,
                expected_claim_uuid=expected_claim_identity,
                expected_fencing_token=expected_fencing_token,
                reason=reason,
                physical_settlement_confirmed=physical_settlement_confirmed,
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreConflict as error:
            raise WorkflowConflict("conflict", message=str(error)) from error
        reschedule = (
            getattr(self._task_scheduler_bridge, "reschedule", None)
            if self._task_scheduler_bridge is not None
            else None
        )
        if result.get("status") == "released" and callable(reschedule):
            try:
                reschedule()
            except Exception:
                # DB 事实已经安全提交；调度器恢复后会通过持久扫描重新发现释放。
                logger.warning("人工释放执行锁后调度器唤醒失败", exc_info=True)
        return result

    def get_workflow_task_step_state(self, task_uuid: str) -> dict[str, Any]:
        """返回 Task 详情页的权威单步候选和当前模式。"""

        if not get_runtime_capabilities().can_debug:
            raise WorkflowError("debug_not_allowed")

        task = self.get_workflow_task(task_uuid)
        execution_mode = str(
            task.get("execution_mode") or task.get("run_mode") or "normal"
        )
        terminal = task.get("status") in {
            "succeeded",
            "success",
            "failed",
            "canceled",
            "timeout",
        }
        if terminal or self._task_scheduler_bridge is None:
            return {
                "workflow_task_uuid": task_uuid,
                "execution_mode": execution_mode,
                "control_status": task.get("control_status"),
                "in_flight_job_count": 0,
                "requires_selection": False,
                "can_step": False,
                "candidates": [],
            }
        try:
            state = self._task_scheduler_bridge.step_state(task_uuid)
        except TaskSchedulerBridgeError:
            return {
                "workflow_task_uuid": task_uuid,
                "execution_mode": execution_mode,
                "control_status": task.get("control_status"),
                "in_flight_job_count": 0,
                "requires_selection": False,
                "can_step": False,
                "candidates": [],
            }
        plan = task.get("execution_plan")
        raw_nodes = plan.get("nodes") if isinstance(plan, Mapping) else []
        node_by_uuid = {
            str(node.get("uuid") or ""): node
            for node in raw_nodes
            if isinstance(node, Mapping)
        }
        candidates = []
        for candidate in state.get("candidates", []):
            node_uuid = str(candidate.get("node_id") or "")
            planned = node_by_uuid.get(node_uuid, {})
            candidates.append(
                {
                    "node_uuid": node_uuid,
                    "name": str(
                        planned.get("name")
                        or candidate.get("action_name")
                        or node_uuid
                    ),
                    "kind": str(
                        planned.get("kind")
                        or candidate.get("executor_kind")
                        or "device_action"
                    ),
                    "device_id": str(candidate.get("device_id") or ""),
                    "action_name": str(candidate.get("action_name") or ""),
                }
            )
        return {
            "workflow_task_uuid": task_uuid,
            "execution_mode": str(state.get("execution_mode") or execution_mode),
            "control_status": task.get("control_status"),
            "in_flight_job_count": int(state.get("in_flight_job_count") or 0),
            "requires_selection": bool(state.get("requires_selection")),
            "can_step": bool(state.get("can_step")),
            "candidates": candidates,
            "blocked_candidates": [
                {"node_uuid": item["node_id"], "reason": item["reason"], "message": item["message"]}
                for item in state.get("blocked_candidates", [])
            ],
        }

    def list_workflow_tasks(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        workflow_uuid: str | None = None,
        execution_kind: str = "",
        status: str = "",
        cleanup_status: str = "",
        projection: str = "full",
    ) -> dict[str, Any]:
        """按后端（Backend）查询合同分页读取工作流任务（WorkflowTask）。

        参数：分页字段限定结果窗口；``workflow_uuid`` 限定工作流定义；
        ``execution_kind`` 区分工作流与直接设备动作来源；状态字段限定业务和清理
        生命周期。返回分页投影，非法枚举或 UUID 映射为稳定输入错误。
        """

        if projection not in {"full", "summary"}:
            raise WorkflowError("invalid_input")
        (
            page,
            page_size,
            workflow_uuid,
            execution_kind,
            status,
            cleanup_status,
        ) = self._normalize_task_list_filters(
            page=page,
            page_size=page_size,
            workflow_uuid=workflow_uuid,
            execution_kind=execution_kind,
            status=status,
            cleanup_status=cleanup_status,
        )
        reader = self._store.list_task_summaries if projection == "summary" else self._store.list_tasks
        result = reader(
            page=page,
            page_size=page_size,
            workflow_uuid=workflow_uuid,
            execution_kind=execution_kind,
            status=status,
            cleanup_status=cleanup_status,
        )
        return {**result, "items": self._project_manual_review_attention(result["items"])}

    def count_workflow_tasks(
        self, *, workflow_uuid: str | None = None, execution_kind: str = "",
        status: str = "", cleanup_status: str = "",
    ) -> dict[str, int]:
        """复用任务列表的合法筛选，只返回总数。"""
        _, _, workflow_uuid, execution_kind, status, cleanup_status = self._normalize_task_list_filters(
            page=1, page_size=1, workflow_uuid=workflow_uuid,
            execution_kind=execution_kind, status=status, cleanup_status=cleanup_status,
        )
        return {"total": self._store.count_tasks(
            workflow_uuid=workflow_uuid, execution_kind=execution_kind,
            status=status, cleanup_status=cleanup_status,
        )}

    def _normalize_task_list_filters(
        self,
        *,
        page: int,
        page_size: int,
        workflow_uuid: str | None,
        execution_kind: str,
        status: str,
        cleanup_status: str,
    ) -> tuple[int, int, str | None, str, str, str]:
        """校验共享任务列表与 Edge 展示列表共用的查询条件。"""

        page, page_size = self._normalize_page(page, page_size)
        if workflow_uuid is not None:
            try:
                workflow_uuid = validate_uuid(workflow_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        status = status.strip().lower()
        execution_kind = execution_kind.strip().lower()
        cleanup_status = cleanup_status.strip().lower()
        if execution_kind and execution_kind not in {
            "workflow",
            "ad_hoc_device_action",
        }:
            raise WorkflowError("invalid_input")
        if status and status not in {
            "pending",
            "running",
            "canceling",
            "succeeded",
            "failed",
            "canceled",
            "timeout",
        }:
            raise WorkflowError("invalid_input")
        if cleanup_status and cleanup_status not in {
            "none",
            "pending",
            "canceling",
            "settled",
            "requires_attention",
        }:
            raise WorkflowError("invalid_input")
        return (
            page,
            page_size,
            workflow_uuid,
            execution_kind,
            status,
            cleanup_status,
        )

    def list_workflow_task_presentations(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        workflow_uuid: str | None = None,
        execution_kind: str = "",
        status: str = "",
        cleanup_status: str = "",
        view: str = "",
        terminal_limit: int = 20,
    ) -> dict[str, Any]:
        """分页或按矩阵窗口返回 Task/Jobs 紧凑只读投影。"""

        (
            page,
            page_size,
            workflow_uuid,
            execution_kind,
            status,
            cleanup_status,
        ) = self._normalize_task_list_filters(
            page=page,
            page_size=page_size,
            workflow_uuid=workflow_uuid,
            execution_kind=execution_kind,
            status=status,
            cleanup_status=cleanup_status,
        )
        view = view.strip().lower()
        if view not in {"", "matrix"}:
            raise WorkflowError("invalid_input")
        if view == "matrix":
            if status or cleanup_status or page != 1:
                raise WorkflowError("invalid_input")
            if isinstance(terminal_limit, bool) or not 0 <= terminal_limit <= 100:
                raise WorkflowError("invalid_input")
        result = self._store.list_task_presentations(
            page=page,
            page_size=page_size,
            workflow_uuid=workflow_uuid,
            execution_kind=execution_kind,
            status=status,
            cleanup_status=cleanup_status,
            view=view,
            terminal_limit=terminal_limit,
        )
        result["items"] = self._project_manual_review_attention(result["items"])
        jobs_by_task = self._store.list_jobs_for_tasks(
            str(task["uuid"]) for task in result["items"]
        )
        confirmations_by_task = self._manual_confirmation_store().list_by_tasks(
            str(task["uuid"]) for task in result["items"]
        )
        return {
            **result,
            "items": [
                {
                    **task,
                    "jobs": [
                        {
                            **job,
                            **(
                                {"manual_confirmation": confirmation}
                                if (
                                    confirmation := confirmations_by_task.get(
                                        str(task["uuid"]), {}
                                    ).get(str(job["uuid"]))
                                )
                                else {}
                            ),
                        }
                        for job in jobs_by_task.get(str(task["uuid"]), [])
                    ],
                }
                for task in result["items"]
            ],
        }

    def list_workflow_node_jobs(self, task_uuid: str) -> list[dict[str, Any]]:
        identity = self.get_workflow_task(task_uuid)["uuid"]
        confirmations = {
            item["workflow_node_job_uuid"]: item
            for item in self._manual_confirmation_store().list_by_task(identity)
        }
        return [
            {
                **job,
                **(
                    {"manual_confirmation": confirmations[job["uuid"]]}
                    if job["uuid"] in confirmations
                    else {}
                ),
            }
            for job in self._store.list_jobs(identity)
        ]

    def list_workflow_task_runtime_events(
        self,
        task_uuid: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """分页读取任务的持久运行事件与动作下发/结果载荷。"""

        try:
            identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
            or after_sequence > (1 << 63) - 1
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise WorkflowError("invalid_input")
        try:
            return self._store.list_task_runtime_events(
                identity,
                after_sequence=after_sequence,
                limit=limit,
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def get_workflow_node_job(self, job_uuid: str) -> dict[str, Any]:
        try:
            identity = validate_uuid(job_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            job = self._store.get_job(identity)
            try:
                confirmation = self._manual_confirmation_store().get_by_job(identity)
            except StoreNotFound:
                return job
            return {**job, "manual_confirmation": confirmation}
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def list_workflow_node_job_feedback(
        self,
        job_uuid: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分页读取单个作业已经提交的过程反馈证据。"""

        try:
            identity = validate_uuid(job_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        page, page_size = self._normalize_page(page, page_size)
        try:
            return self._job_evidence_store().list_feedback(
                job_uuid=identity,
                page=page,
                page_size=page_size,
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def get_execution_wait_graph(self) -> dict[str, Any]:
        """返回工站内全部持久资源等待边和循环诊断。"""

        return TaskRuntimeProjection(self._store).get_execution_wait_graph()

    def get_manual_confirmation(self, job_uuid: str) -> dict[str, Any]:
        """读取一条人工确认事实。"""

        try:
            identity = validate_uuid(job_uuid)
            return self._manual_confirmation_store().get(identity)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def resolve_uncertain_job(
        self,
        job_uuid: str,
        *,
        resolution: str,
        reason: str,
        device_command_id: str | None,
    ) -> dict[str, Any]:
        """安全请求取消等待物理对账的运行中作业，并等待 Edge 提交证明。

        参数：``job_uuid`` 是稳定工作流节点作业 UUID；``resolution`` 当前只接受
        ``canceled``；``reason`` 是操作员理由；``device_command_id`` 是原设备命令
        身份。返回人工处置请求及其确认状态。异常：输入、作业状态、占用事实或本地
        执行端口不满足要求时转换为稳定工作流错误，且不会提前释放任何资源。
        """

        try:
            identity = validate_uuid(job_uuid)
            normalized_resolution = resolution.strip().lower()
            normalized_reason = reason.strip()
            if normalized_resolution != "canceled" or not normalized_reason:
                raise ValueError
            job = self._store.get_job(identity)
            control = job.get("control_data")
            manual = (
                control.get("manual_resolution")
                if isinstance(control, Mapping)
                else None
            )
            if job.get("status") == "canceled" and isinstance(manual, Mapping):
                if (
                    manual.get("resolution") != "canceled"
                    or manual.get("reason") != normalized_reason
                ):
                    raise StoreConflict("作业已经按另一人工结论终结")
                return {
                    "job": job,
                    "resolution_command_uuid": manual.get("command_uuid"),
                    "pending_edge_confirmation": False,
                    "created": False,
                }
            if (
                job.get("status") not in {"running", "failed"}
                or not str(job.get("uncertainty_reason") or "").strip()
            ):
                raise StoreConflict("作业不是等待物理对账的运行中或失败作业")
            if self._task_scheduler_bridge is None:
                raise StoreConflict("当前模式没有本地 UNKNOWN 处置端口")
            command_id = (
                str(device_command_id).strip()
                if device_command_id is not None
                else f"workflow-node-job:{identity}"
            )
            if not command_id:
                raise ValueError
            result = self._task_scheduler_bridge.request_uncertain_resolution(
                identity,
                reason=normalized_reason,
                device_command_id=command_id,
            )
            self._notify_attention_changed()
            return result
        except ValueError:
            raise WorkflowError("invalid_input") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except (StoreConflict, TaskSchedulerBridgeError):
            raise WorkflowConflict("conflict") from None

    def reconcile_job_quantity(
        self, job_uuid: str, *, consumptions: Sequence[Mapping[str, Any]], reason: str
    ) -> dict[str, Any]:
        """在人工核对实际用量后完成待处理的库存结算。"""

        try:
            identity = validate_uuid(job_uuid)
            if not reason.strip() or not consumptions:
                raise ValueError
            if self._task_scheduler_bridge is None:
                raise StoreConflict("当前模式没有本地数量结算端口")
            return self._task_scheduler_bridge.reconcile_job_quantity(
                identity, consumptions=consumptions, reason=reason.strip()
            )
        except ValueError:
            raise WorkflowError("invalid_input") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except (StoreConflict, TaskSchedulerBridgeError):
            raise WorkflowConflict("conflict") from None

    def settle_failed_material_transfer(
        self,
        job_uuid: str,
        *,
        actual_change_set: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """提交失败转运的实际位置；拒绝时保留业务原因，不改变库存与占用。

        已有库存错误码放入 ``details.reason_code``，其余业务拒绝保留原说明；
        成功仍保持失败主状态，只释放已结算占用。
        """

        try:
            identity = validate_uuid(job_uuid)
            normalized_reason = reason.strip()
            if not normalized_reason or not isinstance(actual_change_set, Mapping):
                raise ValueError
            if self._task_scheduler_bridge is None:
                raise StoreConflict("当前模式没有本地物理结算端口")
            return self._task_scheduler_bridge.settle_failed_material_transfer(
                identity,
                actual_change_set=actual_change_set,
                reason=normalized_reason,
            )
        except ValueError:
            raise WorkflowError("invalid_input") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except (StoreConflict, TaskSchedulerBridgeError) as error:
            reason_code = getattr(error, "reason_code", None)
            logger.warning(
                "物料转运对账被拒绝 job_uuid=%s target_site_uuid=%s reason_code=%s reason=%s",
                job_uuid,
                actual_change_set.get("target_site_uuid"),
                reason_code,
                str(error),
            )
            raise WorkflowConflict(
                "conflict",
                message=str(error),
                details={"reason_code": reason_code} if reason_code else {},
            ) from error

    def list_task_manual_confirmations(self, task_uuid: str) -> list[dict[str, Any]]:
        """按开启时间倒序读取任务的全部人工确认。"""

        try:
            identity = validate_uuid(task_uuid)
            return self._manual_confirmation_store().list_by_task(identity)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def decide_manual_confirmation(
        self,
        job_uuid: str,
        *,
        action: str,
    ) -> dict[str, Any]:
        """按 Job UUID 幂等批准或拒绝，并返回最新 Task 聚合。"""

        try:
            identity = validate_uuid(job_uuid)
            if self._task_scheduler_bridge is None:
                raise WorkflowConflict("conflict")
            return self._task_scheduler_bridge.decide_manual_confirmation(
                identity,
                action=action,
            )
        except ValueError:
            raise WorkflowError("invalid_input") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except (StoreConflict, TaskSchedulerBridgeError):
            raise WorkflowConflict("conflict") from None

    def list_human_interactions(self, task_uuid: str) -> list[dict[str, Any]]:
        if self._task_scheduler_bridge is None:
            return []
        self._store.get_task(validate_uuid(task_uuid))
        return self._task_scheduler_bridge.human_interactions.list(task_uuid)

    def reply_human_interaction(self, identity: str, result: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._task_scheduler_bridge is None:
                raise RuntimeError("调度器未连接")
            return self._task_scheduler_bridge.reply_human_interaction(validate_uuid(identity), result)
        except ValueError as error:
            raise WorkflowError("invalid_input", message=str(error)) from error
        except KeyError as error:
            raise WorkflowError("not_found") from error
        except RuntimeError as error:
            raise WorkflowConflict("conflict", message=str(error)) from error

    def open_workflow_intervention_from_report(
        self,
        report: Mapping[str, Any],
    ) -> dict[str, Any]:
        """把本地设备异常报告持久化为公共工作流干预。"""

        try:
            if self.error_handling is not None and report.get("station_controlled"):
                result = self.error_handling.control.report_error(str(report.get("job_id") or ""),
                    phase="execution", message=str(report.get("error_message") or "设备动作异常"))
                self._notify_attention_changed()
                return result
            result = self._intervention_store().open_from_report(report)
            self._notify_attention_changed()
            return result
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreConflict:
            raise WorkflowConflict("conflict") from None

    def list_workflow_interventions(
        self,
        *,
        status: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """按状态读取当前工作流干预。"""

        try:
            return self._intervention_store().list(status=status, limit=limit)
        except StoreConflict:
            raise WorkflowError("invalid_input") from None

    def get_workflow_intervention(
        self,
        intervention_uuid: str,
    ) -> dict[str, Any]:
        """读取一条工作流干预事实。"""

        try:
            return self._intervention_store().get(validate_uuid(intervention_uuid))
        except ValueError:
            raise WorkflowError("invalid_input") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def select_workflow_intervention(
        self,
        intervention_uuid: str,
        *,
        revision: int,
        option_id: str,
        idempotency_key: str,
        result: Any = None,
    ) -> dict[str, Any]:
        """幂等选择一个设备已提供的干预方案并投递给原设备动作。"""

        selection_committed = False
        try:
            identity = validate_uuid(intervention_uuid)
            intervention, _created = self._intervention_store().select(
                identity,
                revision=revision,
                option_id=option_id,
                idempotency_key=idempotency_key,
                result=result,
            )
            selection_committed = True
            if intervention["delivery_status"] == "accepted":
                return {
                    "intervention": intervention,
                    "command_uuid": intervention["edge_command_uuid"],
                    "created": False,
                }
            if not self._deliver_workflow_intervention(intervention):
                raise WorkflowConflict("conflict")
            delivered = self._intervention_store().get(identity)
            return {
                "intervention": delivered,
                "command_uuid": delivered["edge_command_uuid"],
                "created": _created,
            }
        except ValueError:
            raise WorkflowError("invalid_input") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreConflict:
            raise WorkflowConflict("conflict") from None
        finally:
            # 选择提交即关闭 open 事项；投递拒绝或异常也必须刷新当前快照。
            if selection_committed:
                self._notify_attention_changed()

    def _deliver_workflow_intervention(
        self,
        intervention: Mapping[str, Any],
    ) -> bool:
        """用持久冻结载荷向本地设备幂等投递一次干预决定。

        参数：``intervention`` 是已选干预投影。返回：设备明确接受时为真；端口
        缺失或拒绝时为假并把状态记为 ``unknown``。异常：持久事实损坏或数据库
        写失败原样传播；同一 ``edge_command_uuid`` 始终作为稳定投递身份。
        """

        identity = str(intervention["uuid"])
        delivery = self._intervention_delivery
        if delivery is None:
            self._intervention_store().mark_delivery(identity, accepted=False)
            return False
        meta_data = intervention.get("meta_data")
        payload = (
            dict(meta_data["delivery_payload"])
            if isinstance(meta_data, Mapping)
            and isinstance(meta_data.get("delivery_payload"), Mapping)
            else {
                "option": dict(intervention["selected_option"]),
                "action": str(
                    intervention["selected_option"].get("action")
                    or intervention.get("selected_option_id")
                    or ""
                ),
                **(
                    {"result": intervention["selected_option"]["result"]}
                    if "result" in intervention["selected_option"]
                    else {}
                ),
            }
        )
        if isinstance(meta_data, Mapping):
            for field in ("job_id", "device_id"):
                if meta_data.get(field):
                    payload[field] = str(meta_data[field])
        accepted = delivery.resolve_error_decision(
            str(intervention["edge_command_uuid"]),
            payload,
        )
        self._intervention_store().mark_delivery(identity, accepted=accepted)
        return accepted

    def _build_execution_plan(
        self,
        graph: dict[str, Any],
        *,
        run_mode: str,
        target_node_uuid: str | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """委托深模块构造执行计划（ExecutionPlan）与首次作业集合。

        参数：``graph`` 是冻结应用图，``run_mode`` 是运行模式，
        ``target_node_uuid`` 是单节点目标。返回：唯一冻结计划和待持久化作业。
        异常：图、物料来源（MaterialSource）或目标非法时由构建器失败关闭。
        """

        return ExecutionPlanBuilder().build(
            graph,
            run_mode=run_mode,
            target_node_uuid=target_node_uuid,
        )

    def _prepare_task_input(
        self,
        graph: dict[str, Any],
        *,
        input_value: dict[str, Any],
        run_mode: str,
        target_node_uuid: str | None,
        require_output_boundary: bool = False,
    ) -> PreparedTaskInput:
        """从同一应用图构造计划并冻结工作流任务（WorkflowTask）输入。

        参数：``graph`` 是创建事务读取的应用图；``input_value`` 是规范 JSON
        请求对象；``run_mode`` 和 ``target_node_uuid`` 决定活动计划范围。返回：
        已解析输入、快照、执行计划（ExecutionPlan）和首次作业。异常：计划或
        输入绑定不合法时保留构建器/``TaskInputError`` 以映射稳定业务错误。
        """

        from unilabos.workflow.task_input import freeze_scope_device_bindings

        with self._resource_order_context():
            graph = freeze_scope_device_bindings(graph, self._resource_reference_resolver)
            # 库位地址（台面位号、堆栈料架层号）换成载架上的物理库位：只改这份任务自己的图副本，
            # 之后的计划、资源锁、派发和结算拿到的都是物理库位。
            from unilabos.workflow.site_address_binding import bind_graph_site_addresses
            bind_graph_site_addresses(graph, self._resource_reference_resolver)
            plan, jobs = self._build_execution_plan(
                graph,
                run_mode=run_mode,
                target_node_uuid=target_node_uuid,
            )
            prepared = prepare_task_input(
                graph=graph,
                raw_input=input_value,
                execution_plan=plan,
                jobs=jobs,
                resource_resolver=self._material_resolver,
                site_selection_resolver=self._site_selection_resolver,
                require_output_boundary=require_output_boundary,
            )
            # 组合调用展开、任务输入代入之后才出现的库位地址，在计划和作业上再翻译一遍。
            from unilabos.workflow.site_address_binding import bind_plan_site_addresses
            bind_plan_site_addresses(prepared.execution_plan, prepared.jobs, self._resource_reference_resolver)
            return prepared

    def _resource_order_context(self):
        """仅当当前工站具备实际派发顺序校验时才声明该运行要求。"""
        from unilabos.workflow.resource_lock_plan import scheduler_resource_order

        scheduler = getattr(self._task_scheduler_bridge, "_scheduler", None)
        return scheduler_resource_order(bool(getattr(scheduler, "time_scheduling_enabled", False)))

    @staticmethod
    def _scope_debug_task_input(
        prepared: PreparedTaskInput,
        *,
        start_node_uuid: str,
        breakpoint_node_uuids: list[str],
    ) -> PreparedTaskInput:
        """把已验证计划裁成从起始点可达的活动子图，快照保持完整。"""

        plan = dict(prepared.execution_plan)
        nodes = [dict(node) for node in plan.get("nodes", [])]
        node_ids = {str(node.get("uuid") or "") for node in nodes}
        if start_node_uuid not in node_ids:
            raise StoreConflict("debug start node is not enabled and executable")
        snapshot_nodes = prepared.workflow_snapshot.get("nodes", [])
        enabled_snapshot_ids = {
            str(node.get("uuid") or "")
            for node in snapshot_nodes
            if isinstance(node, Mapping) and node.get("disabled") is not True
        }
        if any(
            node_uuid not in enabled_snapshot_ids for node_uuid in breakpoint_node_uuids
        ):
            raise StoreConflict("debug breakpoint node is not enabled")
        outgoing: dict[str, list[str]] = {}
        for edge in plan.get("edges", []):
            if not isinstance(edge, Mapping):
                continue
            source = str(edge.get("source_node_uuid") or "")
            target = str(edge.get("target_node_uuid") or "")
            outgoing.setdefault(source, []).append(target)
        reachable: set[str] = set()
        pending = [start_node_uuid]
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(outgoing.get(current, []))
        # 调试起点只裁掉此前的物理动作；MaterialSource 是任务级物料准入，
        # 不是可以跳过的设备动作。资源可能先经过被跳过动作的 ResourceSlot
        # 透传链，再进入活动子图。此时把来源的冻结绑定目标重接到活动子图的
        # 第一个消费者，既不补跑起点前的物理动作，也不会丢失稳定物料身份。
        resource_outgoing: dict[str, list[Mapping[str, Any]]] = {}
        for edge in plan.get("edges", []):
            if not isinstance(edge, Mapping):
                continue
            if (
                edge.get("dependency_only") is True
                or edge.get("source_type") != "ResourceSlot"
                or edge.get("target_type") != "ResourceSlot"
            ):
                continue
            resource_outgoing.setdefault(
                str(edge.get("source_node_uuid") or ""),
                [],
            ).append(edge)
        supporting_material_sources: set[str] = set()
        for node in nodes:
            if str(node.get("kind") or "") != "material_source":
                continue
            source_uuid = str(node.get("uuid") or "")
            raw_targets = node.get("material_binding_targets", [])
            if not isinstance(raw_targets, list):
                continue
            rebound_targets: list[dict[str, str]] = []
            seen_targets: set[tuple[str, str]] = set()

            def append_target(target_uuid: str, param_key: str) -> None:
                identity = (target_uuid, param_key)
                if not target_uuid or not param_key or identity in seen_targets:
                    return
                seen_targets.add(identity)
                rebound_targets.append(
                    {
                        "workflow_node_uuid": target_uuid,
                        "param_key": param_key,
                    }
                )

            for target in raw_targets:
                if not isinstance(target, Mapping):
                    continue
                target_uuid = str(target.get("workflow_node_uuid") or "")
                if target_uuid in reachable:
                    append_target(target_uuid, str(target.get("param_key") or ""))

            visited_resource_nodes = {source_uuid}
            pending_resource_nodes = [source_uuid]
            while pending_resource_nodes:
                current = pending_resource_nodes.pop()
                for edge in resource_outgoing.get(current, []):
                    target_uuid = str(edge.get("target_node_uuid") or "")
                    if target_uuid in reachable:
                        append_target(
                            target_uuid,
                            str(edge.get("target_data_key") or ""),
                        )
                        continue
                    if target_uuid and target_uuid not in visited_resource_nodes:
                        visited_resource_nodes.add(target_uuid)
                        pending_resource_nodes.append(target_uuid)
            if rebound_targets:
                supporting_material_sources.add(source_uuid)
                node["material_binding_targets"] = rebound_targets
        scoped_node_ids = reachable | supporting_material_sources
        plan["nodes"] = [
            node for node in nodes if str(node.get("uuid")) in scoped_node_ids
        ]
        plan["edges"] = [
            edge
            for edge in plan.get("edges", [])
            if str(edge.get("source_node_uuid")) in scoped_node_ids
            and str(edge.get("target_node_uuid")) in scoped_node_ids
        ]
        plan["handles"] = [
            handle
            for handle in plan.get("handles", [])
            if str(handle.get("node_uuid")) in scoped_node_ids
        ]
        jobs = [
            job
            for job in prepared.jobs
            if str(job.get("workflow_node_uuid")) in scoped_node_ids
        ]
        if not jobs:
            raise StoreConflict("debug task has no reachable jobs")
        return PreparedTaskInput(
            workflow_snapshot=prepared.workflow_snapshot,
            resolved_input=prepared.resolved_input,
            execution_plan=plan,
            jobs=jobs,
        )

    # 工作流创作（Authoring） ---------------------------------------------

    def replace_discovered_source_authorizations(
        self,
        plan: EditableSourceDiscoveryPlan,
    ) -> list[dict[str, Any]]:
        """原子安装发现计划并替换当前进程活动源码授权集合。

        参数：``plan`` 是从全部显式授权目录完成预校验后生成的不可变计划。
        返回：按计划顺序排列的进程内来源记录；成功后活动授权恰好等于本计划。
        异常：软删除工作流、来源身份或目录安全冲突映射为稳定
        ``invalid_input``，且不提交任何部分定义、来源或创作事实。
        """

        if not isinstance(plan, EditableSourceDiscoveryPlan):
            raise WorkflowError("invalid_input")
        # ``root_paths`` 是计划声称已固定的全部包目录；每项注册必须且只能引用它们。
        root_paths = tuple(
            package_root for package_root, _identity in plan.root_identities
        )
        registered_roots = {
            registration.package_root for registration in plan.registrations
        }
        if (
            len(root_paths) != len(set(root_paths))
            or any(not package_root.is_absolute() for package_root in root_paths)
            # 一个新领域包可以先只有合法 package 身份、尚无工作流；它仍是本次
            # 启动明确授权的写入目标。已有注册必须来自这些根，但不再反向要求
            # 每个根至少声明一项工作流。
            or not registered_roots.issubset(set(root_paths))
            or any(
                registration.source_uri
                != (f"package://{registration.package_id}/{registration.relative_path}")
                for registration in plan.registrations
            )
        ):
            raise WorkflowError("invalid_input")
        # ``incoming_workflow_uuids`` 是计划将保留授权的完整新集合。
        incoming_workflow_uuids = frozenset(
            {registration.workflow_uuid for registration in plan.registrations}
        )
        source_dependencies: dict[str, frozenset[str]] = {}
        for registration in plan.registrations:
            dependencies = registration.dependency_workflow_uuids
            if (
                not isinstance(dependencies, tuple)
                or any(not isinstance(item, str) for item in dependencies)
                or not set(dependencies).issubset(incoming_workflow_uuids)
            ):
                raise WorkflowError("invalid_input")
            source_dependencies[registration.workflow_uuid] = frozenset(dependencies)
        # ``registration_rows`` 是交给进程内定义目录的完整、不可变批次。
        registration_rows = tuple(
            {
                "workflow_uuid": registration.workflow_uuid,
                "package_id": registration.package_id,
                "package_root": str(registration.package_root),
                "relative_path": registration.relative_path,
                "source_uri": registration.source_uri,
            }
            for registration in plan.registrations
        )
        with self._source_authorization_replacement_lock:
            with self._active_sources_lock:
                current_workflow_uuids = self._active_source_workflow_uuids
            # ``locked_workflow_uuids`` 同时覆盖将撤销和将激活的身份；稳定排序避免
            # 多工作流保存、读取与授权替换形成锁顺序反转。
            locked_workflow_uuids = sorted(
                current_workflow_uuids | incoming_workflow_uuids
            )
            with ExitStack() as locks:
                for workflow_uuid in locked_workflow_uuids:
                    locks.enter_context(self._authoring_lock(workflow_uuid))
                try:
                    with pin_package_roots(plan.root_identities) as pinned_roots:
                        registered = self._definition_store.install_discovered_sources(
                            registration_rows,
                            before_commit=pinned_roots.assert_current,
                        )
                except SourceWorkspaceError:
                    raise WorkflowError("invalid_input") from None
                except StoreConflict:
                    raise WorkflowConflict("invalid_input") from None
                # 定义目录事务与进程级文件访问授权不能共用一个锁，但必须
                # 在所有相关创作锁释放前一次发布，撤权返回后不得再有旧操作读写路径。
                with self._active_sources_lock:
                    self._active_source_workflow_uuids = incoming_workflow_uuids
                    self._active_source_dependencies = source_dependencies
                # 授权替换不仅更新文件访问白名单，也必须撤销已离开集合的
                # 冷启动修订闸门；否则同进程重新授权同一 UUID 时可能借用旧合同
                # 基线。不可变合同历史仍保留在定义库，但不会再被这次启动批次
                # 当作尚未消费的 bootstrap 事实。
                with self._bootstrap_published_revisions_lock:
                    for workflow_uuid in current_workflow_uuids - incoming_workflow_uuids:
                        self._bootstrap_published_revisions.pop(workflow_uuid, None)
            return registered

    def replace_active_editable_source_authorization(
        self,
        *,
        workflow_uuid: str,
        package_id: str,
        package_root: str | Path,
        relative_path: str,
    ) -> dict[str, Any]:
        """用一项可编辑来源替换当前进程的完整活动源码授权集合。

        参数：工作流（Workflow）UUID 是已有定义身份；包身份、包目录和相对路径
        共同形成工作流源码（Workflow Source）的稳定来源身份。
        返回：安装后的进程内来源记录；此前活动的其他来源和对应定义同时退出本次
        进程授权集合，下次启动只从新选择的领域包重新建立。
        异常：身份不存在、路径不安全或唯一性冲突时返回稳定工作流错误。
        """

        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        try:
            root, normalized_relative_path = validate_source_registration(
                package_root=package_root,
                relative_path=relative_path,
            )
            root_metadata = root.lstat()
        except (OSError, SourceWorkspaceError):
            raise WorkflowError("invalid_input") from None
        if not isinstance(package_id, str) or not package_id.strip():
            raise WorkflowError("invalid_input")
        normalized_package_id = package_id.strip()
        source_uri = f"package://{normalized_package_id}/{normalized_relative_path}"
        # 单项替换命令构造成与启动发现完全相同的不可变计划，避免绕过物理路径、
        # 来源 URI 和“既有身份不可重绑定”等批量授权不变量。
        plan = EditableSourceDiscoveryPlan(
            registrations=(
                EditableSourceRegistration(
                    workflow_uuid=workflow_uuid,
                    package_id=normalized_package_id,
                    package_root=root,
                    relative_path=normalized_relative_path,
                    source_uri=source_uri,
                ),
            ),
            root_identities=(((root, (root_metadata.st_dev, root_metadata.st_ino))),),
        )
        return self.replace_discovered_source_authorizations(plan)[0]

    def _add_active_source_authorization(
        self,
        registration: EditableSourceRegistration,
    ) -> dict[str, Any]:
        """把一项已发布领域源码增量加入当前进程授权集合。

        参数：``registration`` 来自当前唯一领域包源码目标。返回安装后的来源
        记录。异常：目录、身份或既有来源冲突时失败关闭；已有活动来源不会被
        替换或撤权。
        """

        try:
            root, relative_path = validate_source_registration(
                package_root=registration.package_root,
                relative_path=registration.relative_path,
            )
            metadata = root.lstat()
        except (OSError, SourceWorkspaceError):
            raise WorkflowError("source_target_unavailable") from None
        if (
            root != registration.package_root
            or registration.source_uri
            != f"package://{registration.package_id}/{relative_path}"
        ):
            raise WorkflowError("source_target_unavailable")
        row = {
            "workflow_uuid": registration.workflow_uuid,
            "package_id": registration.package_id,
            "package_root": str(root),
            "relative_path": relative_path,
            "source_uri": registration.source_uri,
        }
        with self._source_authorization_replacement_lock:
            with self._authoring_lock(registration.workflow_uuid):
                try:
                    with pin_package_roots(
                        ((root, (metadata.st_dev, metadata.st_ino)),)
                    ) as pinned:
                        installed = self._definition_store.install_discovered_sources(
                            (row,),
                            before_commit=pinned.assert_current,
                        )[0]
                except SourceWorkspaceError:
                    raise WorkflowError("source_target_unavailable") from None
                except StoreConflict:
                    raise WorkflowConflict("source_identity_conflict") from None
                with self._active_sources_lock:
                    self._active_source_workflow_uuids = frozenset(
                        (
                            *self._active_source_workflow_uuids,
                            registration.workflow_uuid,
                        )
                    )
                    self._active_source_dependencies[registration.workflow_uuid] = (
                        frozenset(registration.dependency_workflow_uuids)
                    )
                return installed

    def list_registered_sources(self) -> list[dict[str, Any]]:
        """返回本次进程配置仍授权的工作流源码（Workflow Source）。

        参数：无。返回：按稳定工作流 UUID 排序的本进程活动注册；文件库中的旧
        定义或来源记录不会自动获得当前路径访问权。
        """

        with self._active_sources_lock:
            active_workflow_uuids = self._active_source_workflow_uuids
        return [
            registration
            for registration in self._definition_store.list_source_registrations()
            if registration["workflow_uuid"] in active_workflow_uuids
        ]

    def _composite_dependent_workflow_uuids(
        self,
        child_workflow_uuid: str,
    ) -> tuple[str, ...]:
        """返回直接引用指定实验操作的活动工作流。

        参数：``child_workflow_uuid`` 是刚应用新修订的实验操作稳定身份。返回：
        按 UUID 稳定排序且去重的直接父工作流集合；优先使用领域包 AST 扫描得到的
        import 依赖，并以已应用图中的 ``child_workflow_uuid`` 补偿旧包缺失依赖
        元数据的情况。异常：单个父图暂不可读时仍把它列为待刷新对象，由提交后
        刷新器把真实失败收敛为 warning，不能让已提交的实验操作伪装回滚。
        """

        # ``child_workflow_uuid`` 来自刚提交的子定义，不是某一修订或调用节点；
        # 因此引用方源码依赖可跨修订稳定命中同一个实验操作。
        with self._active_sources_lock:
            active_workflow_uuids = tuple(self._active_source_workflow_uuids)
            dependencies = dict(self._active_source_dependencies)
        dependents: set[str] = set()
        for workflow_uuid in active_workflow_uuids:
            if workflow_uuid == child_workflow_uuid:
                continue
            if child_workflow_uuid in dependencies.get(
                workflow_uuid,
                frozenset(),
            ):
                dependents.add(workflow_uuid)
                continue
            try:
                graph = self.get_graph(workflow_uuid)
            except Exception:  # noqa: BLE001 - 交给提交后刷新器形成可观察警告。
                dependents.add(workflow_uuid)
                continue
            if graph_references_composite_child(
                graph,
                child_workflow_uuid=child_workflow_uuid,
            ):
                dependents.add(workflow_uuid)
        return tuple(sorted(dependents))

    def recover_registered_sources(
        self,
        *,
        preserve_author_source: bool = False,
    ) -> None:
        """启动时按当前模板目录逐一恢复全部授权源码。

        参数：``preserve_author_source`` 只供工作区自动激活使用，使成功候选绑定
        原始作者源码和对应映射。返回：无；只读取本轮活动注册并强制替换旧进程
        目录产生的候选或诊断，不把普通子源码编译误当成子合同发布。异常：文件、
        目录或持久化失败全部传播到组合根，禁止启动在不完整恢复后误报 ready。
        """

        for registration in self.list_registered_sources():
            self.reconcile_registered_source(
                registration["workflow_uuid"],
                force_compile=True,
                preserve_author_source=preserve_author_source,
            )

    def activate_registered_sources_to_fixed_point(
        self,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """恢复并应用工作区源码，直到组合工作流依赖达到固定点。

        参数：``progress_callback`` 在开始和每个源码完成真实编译后接收
        ``(loaded, total)``。返回：无；使用同代 Package Catalog 的静态组合依赖把来源按
        子到父分层，每层只编译一次、批量提交候选并只重建一次模板目录。循环层
        保留逐项刷新语义，让编译器发布稳定递归诊断。缺失或无效来源保留真实
        诊断；单个候选的稳定业务失败被隔离，未知基础设施异常继续失败关闭。对于
        旧包未显式标注的组合依赖，分层后再以有限轮次重试到固定点。
        """

        registrations = self.list_registered_sources()
        total = len(registrations)
        loaded = 0
        if progress_callback is not None:
            progress_callback(loaded, total)
        registrations_by_uuid = {
            str(registration["workflow_uuid"]): registration
            for registration in registrations
        }
        applied_during_layers = False
        for workflow_uuids, cyclic in self._workspace_activation_layers(
            tuple(registrations_by_uuid)
        ):
            for workflow_uuid in workflow_uuids:
                self.reconcile_registered_source(
                    workflow_uuid,
                    force_compile=True,
                    preserve_author_source=True,
                )
                loaded += 1
                if progress_callback is not None:
                    progress_callback(loaded, total)

            deferred_results: list[Mapping[str, Any]] = []
            previous_batch_state = self._workspace_activation_batch
            self._workspace_activation_batch = not cyclic
            try:
                for workflow_uuid in workflow_uuids:
                    record = self._definition_store.get_authoring_record(workflow_uuid)
                    candidate = record.get("candidate")
                    if not isinstance(candidate, dict):
                        continue
                    candidate_hash = candidate.get("candidate_hash")
                    if not isinstance(candidate_hash, str) or not candidate_hash:
                        raise WorkflowError("candidate_invalid")
                    try:
                        result = self._apply_workspace_activation_candidate(
                            workflow_uuid,
                            candidate_hash=candidate_hash,
                        )
                    except WorkflowError as error:
                        if error.code not in _ISOLATED_WORKSPACE_ACTIVATION_ERRORS:
                            raise
                        self._record_workspace_activation_failure(
                            workflow_uuid,
                            error=error,
                        )
                        continue
                    if cyclic:
                        self._require_workspace_activation_apply_complete(result)
                    else:
                        deferred_results.append(result)
                    applied_during_layers = True
            finally:
                self._workspace_activation_batch = previous_batch_state

            if not deferred_results:
                # 发布合同文件是跨重启保留实验操作身份的权威。子工作流刚刚
                # 应用完成后，必须先把它恢复到内存目录，再尝试下一层父工作流；
                # 否则父源码会在恢复合同之前被编译为
                # ``composite_child_not_found``，其嵌套图就无法重新生成。
                self.restore_published_workflow_contracts()
                continue
            # 同一层的候选已经提交。先恢复本层刚激活的实验操作发布合同，
            # 再重建模板目录；否则目录重建会看不到刚恢复的合同，下一轮父
            # 工作流只能继续使用缺少子模板的旧目录并反复得到
            # ``composite_catalog_mismatch``。
            self.restore_published_workflow_contracts()
            self._rebuild_workspace_activation_catalog()
            for result in deferred_results:
                self._require_workspace_activation_apply_complete(result)
        unresolved_sources = any(
            self.get_authoring(workflow_uuid).get("state") != "applied"
            for workflow_uuid in registrations_by_uuid
        )
        if applied_during_layers and unresolved_sources:
            self._retry_workspace_activation_to_fixed_point(
                tuple(registrations_by_uuid),
            )

    def _retry_workspace_activation_to_fixed_point(
        self,
        workflow_uuids: tuple[str, ...],
    ) -> None:
        """重试静态计划未标注出的组合依赖，直到不再产生新应用图。

        参数：``workflow_uuids`` 保留来源注册稳定顺序。返回无；每轮都使用上一轮
        已应用工作流生成的新目录重新编译尚未成功的来源，最多推进来源数量轮。
        稳定业务失败只隔离对应来源，基础设施或目录发布失败继续关闭式失败。

        领域包旧声明可能没有显式 ``dependency_workflow_uuids``，但 Python import
        已形成真实实验操作依赖。本补偿循环只解决这类缺失元数据，不替代有向依赖
        分层，也不会执行源码或猜测替代工作流。
        """

        blocked: set[str] = set()
        for _pass in range(len(workflow_uuids)):
            # 上一轮可能刚应用了一个同时作为子工作流的来源。它的发布合同在
            # 该来源真正存在之前不能恢复；每轮开始重新投影一次，才能让更深层
            # 的“孙工作流”继续被父工作流解析，而不是停在上一层诊断。
            self.restore_published_workflow_contracts()
            applied_any = False
            for workflow_uuid in workflow_uuids:
                if workflow_uuid in blocked:
                    continue
                # 补偿轮只负责推进尚未成功应用的来源。已经处于 applied 状态的
                # 来源无需再次 Apply；重复提交会重新触发其父工作流刷新，若父
                # 来源当前仍是未发布/不可解析的组合操作，反而会产生
                # ``dependent_authoring_refresh_pending``，把可用的工作区错误地
                # 判定为目录不可用。
                current_state = self.get_authoring(workflow_uuid).get("state")
                if current_state == "applied":
                    continue
                self.reconcile_registered_source(
                    workflow_uuid,
                    force_compile=True,
                    preserve_author_source=True,
                )
                record = self._definition_store.get_authoring_record(workflow_uuid)
                candidate = record.get("candidate")
                if not isinstance(candidate, dict):
                    continue
                candidate_hash = candidate.get("candidate_hash")
                if not isinstance(candidate_hash, str) or not candidate_hash:
                    raise WorkflowError("candidate_invalid")
                try:
                    result = self._apply_workspace_activation_candidate(
                        workflow_uuid,
                        candidate_hash=candidate_hash,
                    )
                except WorkflowError as error:
                    if error.code not in _ISOLATED_WORKSPACE_ACTIVATION_ERRORS:
                        raise
                    self._record_workspace_activation_failure(
                        workflow_uuid,
                        error=error,
                    )
                    blocked.add(workflow_uuid)
                    continue
                applied_any = True
                apply_result = result.get("apply_result")
                warnings = (
                    apply_result.get("warnings")
                    if isinstance(apply_result, Mapping)
                    else None
                )
                warning_codes = {
                    str(warning.get("code"))
                    for warning in warnings or []
                    if isinstance(warning, Mapping)
                }
                if (
                    self.compiler is not None
                    and warning_codes
                    and warning_codes <= {"dependent_authoring_refresh_pending"}
                ):
                    # 补偿轮本来就是为了把隐式组合依赖推进到固定点。某些未发布
                    # 或仍有用户编辑的引用方暂时不能刷新时，已成功提交的当前
                    # 来源仍可继续提供工作流能力；若把这个业务待办升级成目录
                    # 不可用，会让无关的有效工作流也无法启动。
                    logger.warning(
                        "工作区工作流已应用，但以下引用方未能自动更新；请打开引用方工作流，"
                        "检查组合节点参数和设备动作模板，重新编译并应用: %s",
                        ",".join(
                            str(warning.get("message", ""))
                            for warning in warnings or []
                            if isinstance(warning, Mapping)
                        ),
                    )
                else:
                    self._require_workspace_activation_apply_complete(result)
            if not applied_any:
                return

    def _apply_workspace_activation_candidate(
        self,
        workflow_uuid: str,
        *,
        candidate_hash: str,
    ) -> dict[str, Any]:
        """应用同一启动线程刚签发的候选并复用其编译结果。

        参数：``workflow_uuid`` 与 ``candidate_hash`` 精确标识待应用候选。
        返回：与公共 Apply 相同的结果。异常：公共 Apply 的所有权威冲突和
        定义目录写入错误均原样传播；授权只在当前线程的本次调用期间有效。
        """

        key = (workflow_uuid, candidate_hash)
        previous = getattr(
            self._workspace_activation_context,
            "prevalidated_candidate",
            None,
        )
        self._workspace_activation_context.prevalidated_candidate = key
        try:
            return self.apply_authoring(
                workflow_uuid,
                candidate_hash=candidate_hash,
                preserve_author_source=True,
            )
        finally:
            if previous is None:
                del self._workspace_activation_context.prevalidated_candidate
            else:
                self._workspace_activation_context.prevalidated_candidate = previous

    def _workspace_activation_layers(
        self,
        workflow_uuids: tuple[str, ...],
    ) -> tuple[tuple[tuple[str, ...], bool], ...]:
        """把活动来源稳定划分为子到父的启动层。

        参数：``workflow_uuids`` 保留注册表稳定顺序。返回：每项包含同层 UUID
        及是否为循环残量；只有循环残量允许退回逐项目录刷新。异常：活动依赖
        快照缺失时按无内部依赖处理，兼容不含工作流的旧计划。
        """

        with self._active_sources_lock:
            dependencies = dict(self._active_source_dependencies)
        remaining = set(workflow_uuids)
        layers: list[tuple[tuple[str, ...], bool]] = []
        while remaining:
            layer = tuple(
                workflow_uuid
                for workflow_uuid in workflow_uuids
                if workflow_uuid in remaining
                and not (dependencies.get(workflow_uuid, frozenset()) & remaining)
            )
            if not layer:
                layers.append(
                    (
                        tuple(
                            workflow_uuid
                            for workflow_uuid in workflow_uuids
                            if workflow_uuid in remaining
                        ),
                        True,
                    )
                )
                break
            layers.append((layer, False))
            remaining.difference_update(layer)
        return tuple(layers)

    def _rebuild_workspace_activation_catalog(self) -> None:
        """在一层候选全部提交后原子发布一次新模板目录。

        参数：无。返回：无；没有目录重建器时保留既有编译器。异常：重建失败时
        关闭陈旧编译入口并抛稳定目录不可用错误，禁止组合根误报 ready。
        """

        if self._compiler_rebuilder is None:
            return
        try:
            self.compiler = self._compiler_rebuilder()
        except Exception:
            logger.exception("工作流模板目录重建失败，关闭陈旧编译入口")
            self.compiler = None
            raise WorkflowError("template_catalog_unavailable") from None

    def _require_workspace_activation_apply_complete(
        self,
        result: Mapping[str, Any],
    ) -> None:
        """检查工作区自动激活的目录基础设施状态。

        参数：``result`` 是刚完成的 ``apply_authoring`` 结果。返回：没有提交后
        基础设施 warning 且当前目录编译器仍可用时无返回值。异常：目录重建
        未完成时抛 ``template_catalog_unavailable``；其他未知提交后 warning 抛
        ``internal_error``。``dependent_authoring_refresh_pending`` 只表示某个
        父工作流仍有业务诊断（例如引用未发布子工作流），不影响当前目录和已
        应用来源的可用性，不能阻断 Workspace 正常启动。
        """

        apply_result = result.get("apply_result")
        if not isinstance(apply_result, Mapping):
            raise WorkflowError("candidate_invalid")
        warnings = apply_result.get("warnings")
        if not isinstance(warnings, list):
            raise WorkflowError("candidate_invalid")
        warning_codes = {
            str(warning.get("code"))
            for warning in warnings
            if isinstance(warning, Mapping)
        }
        catalog_incomplete = {
            "template_catalog_rebuild_pending",
        }
        if self.compiler is None or warning_codes & catalog_incomplete:
            raise WorkflowError("template_catalog_unavailable")
        # 父工作流刷新失败是局部业务状态；其诊断已经由
        # ``_record_workspace_activation_failure``/依赖刷新器保存，不应把整个
        # Backend 的 ready 门禁升级为基础设施故障。
        unexpected_warning_codes = warning_codes - {
            "dependent_authoring_refresh_pending",
        }
        if unexpected_warning_codes:
            raise WorkflowError("internal_error")

    def _record_workspace_activation_failure(
        self,
        workflow_uuid: str,
        *,
        error: WorkflowError,
    ) -> None:
        """把一个自动应用业务失败收敛为该来源自己的进程内诊断。

        参数：``workflow_uuid`` 是失败来源身份；``error`` 是已经稳定映射的工作流
        业务错误。返回：无；撤销不可再次应用的旧候选，保留当前源码代，并发布
        一次可观察创作事件。异常：读取来源或写入诊断失败时原样传播，避免把存储
        故障误当成普通草稿错误。
        """

        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            record = self._definition_store.get_authoring_record(workflow_uuid)
            draft_hash = (
                source["draft_hash"]
                if source is not None
                else record["observed_draft_hash"]
            )
            draft_update_time = (
                source["update_time"]
                if source is not None
                else record["draft_update_time"]
            )
            self._definition_store.record_draft_compilation(
                workflow_uuid=workflow_uuid,
                draft_hash=draft_hash,
                draft_update_time=draft_update_time,
                diagnostics=[
                    {
                        "severity": "error",
                        "code": error.code,
                        "message": error.message,
                    }
                ],
                candidate_hash=None,
                candidate=None,
                event_data={
                    "workflow_uuid": workflow_uuid,
                    "cause": "workspace_activation_failed",
                    "workflow_revision": self._get_authoring_workflow(workflow_uuid)[
                        "revision"
                    ],
                    "draft_hash": draft_hash,
                    "candidate_hash": None,
                },
            )
        # ament/launch 在测试与部分本地启动路径会把 ``unilabos`` 父 logger
        # 替换为不向根 logger 传播的适配器；这里的启动失败诊断必须同时进入
        # 标准根日志（便于 caplog、集中式采集和现场排障），不能只落到
        # ``lastResort`` 的裸 stderr。
        logging.getLogger().warning(
            "工作区工作流自动激活失败 workflow_uuid=%s code=%s message=%s",
            workflow_uuid,
            error.code,
            error.message,
        )

    def close(self) -> None:
        """关闭共享本地调度桥、运行事实库和进程内定义目录。

        参数：无。返回：无；桥必须幂等注销监听器，随后关闭运行库与定义目录。异常：清理
        失败原样传播，调用方据此保留未完成资源所有权并可重试。
        """

        if self._task_scheduler_bridge is not None:
            self._task_scheduler_bridge.close()
        if self._intervention_delivery is not None:
            self._intervention_delivery.remove_error_decision_required_listener(
                self.open_workflow_intervention_from_report
            )
            self._intervention_delivery = None
        self._store.close()
        if self._definition_store is not self._store:
            self._definition_store.close()

    def get_authoring(self, workflow_uuid: str) -> dict[str, Any]:
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            graph = self.get_graph(workflow_uuid)
            record = self._definition_store.get_authoring_record(workflow_uuid)
            return self._authoring_aggregate(
                workflow=workflow,
                graph=graph,
                registration=registration,
                source=source,
                record=record,
            )

    def save_draft(
        self,
        workflow_uuid: str,
        *,
        python_source: str,
        expected_draft_hash: str | None,
        expected_workflow_revision: int,
        compilation_base_graph: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_hash(expected_draft_hash, nullable=True)
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            current = self._read_source(registration)
            current_hash = current["draft_hash"] if current is not None else None
            if current_hash != expected_draft_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if workflow["revision"] != expected_workflow_revision:
                raise WorkflowConflict("workflow_revision_conflict")

            try:
                encoded = python_source.encode("utf-8")
            except UnicodeEncodeError:
                raise WorkflowError("invalid_input") from None
            self._reject_cross_workflow_source(
                workflow_uuid=workflow_uuid,
                python_source=python_source,
            )
            encoded_hash = _sha256(encoded)
            # IDE 保存事件发生时，文件系统已经发布了作者字节；随后对同一哈希
            # 发起的 CAS 只用于静态编译和签发候选。再次原子替换会无意义地改变
            # 文件世代，还可能触发工作区监视器的第二轮刷新。
            if encoded_hash != current_hash:
                try:
                    self._atomic_write(
                        registration,
                        encoded,
                        expected_hash=current_hash,
                    )
                except OSError:
                    raise WorkflowError("internal_error") from None
            source = self._read_source(registration)
            assert source is not None
            if source["draft_hash"] != encoded_hash:
                raise WorkflowConflict("draft_hash_conflict")
            applied_graph = self.get_graph(workflow_uuid)
            # 系统生成的组合图可能刚把具体执行器绑定到新建的组合根；源码调用
            # 本身只表达子工作流边界参数，编译固定点需要同时看到这张候选图，
            # 才能从已有组合元数据恢复设备绑定。候选签发仍以真实已应用图为
            # 变更基线，避免跳过修订推进或把未提交图误当成事实。
            compilation_graph = (
                compilation_base_graph
                if compilation_base_graph is not None
                else applied_graph
            )
            compilation = self._compile(
                workflow=workflow,
                graph=compilation_graph,
                registration=registration,
                python_source=source["python_source"],
            )
            # API 生成的组合图作为编译基线时，本地变更集相对于该候选图，而非
            # 真正已应用的父图。签名前必须把变更集重建到真实已应用图上，否则
            # 候选会因变更集不精确而被拒绝。
            if compilation.graph is not None:
                compilation = compilation.model_copy(
                    update={
                        "changeset": candidate_changeset(
                            graph=compilation.graph,
                            applied_graph=applied_graph,
                        )
                    }
                )
            candidate = self._issue_candidate(
                workflow_revision=workflow["revision"],
                draft_hash=source["draft_hash"],
                compilation=compilation,
                applied_graph=applied_graph,
                draft_python_source=source["python_source"],
            )
            record = self._definition_store.get_authoring_record(workflow_uuid)
            applied_source = record.get("applied_source")
            if self._source_only_candidate_is_already_applied(
                candidate=candidate,
                applied_source=applied_source,
                workflow_revision=workflow["revision"],
                draft_hash=source["draft_hash"],
            ):
                # 恢复到已应用的精确作者字节且重新编译证明图未变时，没有待
                # Apply 的新事实；清空旧无效草稿派生状态即可回到 applied。
                candidate = None
            event_data = {
                "workflow_uuid": workflow_uuid,
                "cause": "draft_saved",
                "workflow_revision": workflow["revision"],
                "draft_hash": source["draft_hash"],
                "candidate_hash": (
                    candidate["candidate_hash"] if candidate is not None else None
                ),
            }
            self._definition_store.record_draft_compilation(
                workflow_uuid=workflow_uuid,
                draft_hash=source["draft_hash"],
                draft_update_time=source["update_time"],
                diagnostics=compilation.diagnostics,
                candidate_hash=(
                    candidate["candidate_hash"] if candidate is not None else None
                ),
                candidate=candidate,
                event_data=event_data,
            )
            self._catalog_generation_tracker.record_compilation(
                workflow_uuid,
                compilation.template_catalog_fingerprint,
            )
            return self.get_authoring(workflow_uuid)

    @staticmethod
    def _reject_cross_workflow_source(
        *, workflow_uuid: str, python_source: str
    ) -> None:
        """拒绝把明确属于其他工作流的源码写入当前登记路径。

        参数：``workflow_uuid`` 是当前登记路径的权威工作流 UUID；
        ``python_source`` 是待保存的完整 Python 文本。返回：身份一致或无法静态
        确认时无返回。异常：唯一声明另一个有效 UUID 时抛
        ``WorkflowConflict(workflow_identity_mismatch)``。

        安全不变量：语法错误、缺失/动态/歧义声明仍可作为无效草稿保存；这里只
        拦截能够静态证明属于另一工作流的源码，且拒绝发生在任何物理写入之前。
        """

        expected_uuid = validate_uuid(workflow_uuid)
        declared_uuid = declared_workflow_uuid(python_source)
        if declared_uuid is None or declared_uuid == expected_uuid:
            return
        raise WorkflowConflict(
            "workflow_identity_mismatch",
            message=(
                f"导入的 Python 声明工作流 {declared_uuid}，当前编辑的是 "
                f"{expected_uuid}；请选择匹配的工作流，或修改 "
                "@workflow.workflow_uuid 后再保存"
            ),
        )

    def reconcile_registered_source(
        self,
        workflow_uuid: str,
        *,
        force_compile: bool = False,
        preserve_author_source: bool = False,
    ) -> dict[str, Any]:
        """协调一个已注册工作流源码及其可重建创作派生状态。

        参数：``workflow_uuid`` 是工作流（Workflow）稳定身份；
        ``force_compile`` 用于启动恢复或模板目录换代后强制重编译目录相关诊断和
        候选稿（Candidate）；``preserve_author_source`` 让自动激活候选保留
        原始作者源码与相应映射。返回：最新创作聚合。异常：来源、编译或持久化
        失败时传播稳定工作流错误；本函数不发布模板目录。
        """

        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            record = self._definition_store.get_authoring_record(workflow_uuid)
            current_catalog_fingerprint = (
                self._catalog_fingerprint() if self.compiler is not None else None
            )
            catalog_changed = (
                current_catalog_fingerprint is not None
                and self._catalog_generation_tracker.changed_from_known_generation(
                    workflow_uuid,
                    current_catalog_fingerprint,
                )
            )
            applied_source = record.get("applied_source")
            writeback_marker_valid = (
                record.get("writeback_source") is not None
                and record.get("writeback_expected_hash") is not None
                and record.get("writeback_generation") is not None
            )
            if (
                record["writeback_status"] == "pending"
                and writeback_marker_valid
                and source is not None
                and applied_source is not None
                and source["draft_hash"] == applied_source["source_hash"]
            ):
                self._definition_store.settle_writeback(
                    workflow_uuid=workflow_uuid,
                    expected_writeback_source=record["writeback_source"],
                    expected_writeback_hash=record["writeback_expected_hash"],
                    expected_writeback_generation=record["writeback_generation"],
                    observed_draft_hash=source["draft_hash"],
                    draft_update_time=source["update_time"],
                    event_data={
                        "workflow_uuid": workflow_uuid,
                        "cause": "recovered",
                        "workflow_revision": workflow["revision"],
                        "draft_hash": source["draft_hash"],
                        "candidate_hash": None,
                    },
                )
                return self.get_authoring(workflow_uuid)
            if (
                record["writeback_status"] == "pending"
                and writeback_marker_valid
                and (
                    source is None
                    or source["draft_hash"] == record["writeback_expected_hash"]
                )
            ):
                recovery_source = record.get("writeback_source")
                if recovery_source is not None:
                    try:
                        recovery_bytes = recovery_source.encode("utf-8")
                        recovery_hash = _sha256(recovery_bytes)
                        self._atomic_write(
                            registration,
                            recovery_bytes,
                            expected_hash=(
                                source["draft_hash"] if source is not None else None
                            ),
                        )
                        source = self._read_source(registration)
                        if source is not None and source["draft_hash"] == recovery_hash:
                            self._definition_store.settle_writeback(
                                workflow_uuid=workflow_uuid,
                                expected_writeback_source=record["writeback_source"],
                                expected_writeback_hash=record[
                                    "writeback_expected_hash"
                                ],
                                expected_writeback_generation=record[
                                    "writeback_generation"
                                ],
                                observed_draft_hash=source["draft_hash"],
                                draft_update_time=source["update_time"],
                                event_data={
                                    "workflow_uuid": workflow_uuid,
                                    "cause": "recovered",
                                    "workflow_revision": workflow["revision"],
                                    "draft_hash": source["draft_hash"],
                                    "candidate_hash": None,
                                },
                            )
                            return self.get_authoring(workflow_uuid)
                    except (OSError, UnicodeError, WorkflowError):
                        return self.get_authoring(workflow_uuid)
            actual_hash = source["draft_hash"] if source is not None else None
            invalid_writeback_marker = (
                record["writeback_status"] == "pending" and not writeback_marker_valid
            )
            if (
                actual_hash == record["observed_draft_hash"]
                and not invalid_writeback_marker
                and not (actual_hash is None and record.get("candidate") is not None)
                and not force_compile
            ):
                return self.get_authoring(workflow_uuid)

            candidate: dict[str, Any] | None = None
            diagnostics: list[dict[str, Any]] = []
            if source is not None:
                applied_graph = self.get_graph(workflow_uuid)
                compilation = self._compile(
                    workflow=workflow,
                    graph=applied_graph,
                    registration=registration,
                    python_source=source["python_source"],
                )
                if preserve_author_source:
                    compilation = self._preserve_author_source_compilation(
                        compilation=compilation,
                        workflow=workflow,
                        graph=applied_graph,
                        python_source=source["python_source"],
                    )
                diagnostics = compilation.diagnostics
                candidate = self._issue_candidate(
                    workflow_revision=workflow["revision"],
                    draft_hash=source["draft_hash"],
                    compilation=compilation,
                    applied_graph=applied_graph,
                    draft_python_source=source["python_source"],
                )
                if self._source_only_candidate_is_already_applied(
                    candidate=candidate,
                    applied_source=applied_source,
                    workflow_revision=workflow["revision"],
                    draft_hash=source["draft_hash"],
                ):
                    # 当前目录已再次证明同一作者源码不改变应用图；若继续签发
                    # 候选，每次重启都会无意义提升修订并触发全目录级联编译。
                    candidate = None
            if force_compile and actual_hash == record["observed_draft_hash"]:
                # 同一源码代际只在进程内已知目录指纹变化时标记为目录变化；
                # 冷启动没有旧代际证据，只能记录为恢复编译。
                cause = "catalog_changed" if catalog_changed else "recovered"
            elif (
                source is not None
                and record["observed_draft_hash"] is None
                and record["update_time"] is not None
            ):
                cause = "recovered"
            else:
                cause = "external_draft_changed"
            self._definition_store.record_draft_compilation(
                workflow_uuid=workflow_uuid,
                draft_hash=actual_hash,
                draft_update_time=(
                    source["update_time"] if source is not None else None
                ),
                diagnostics=diagnostics,
                candidate_hash=(
                    candidate["candidate_hash"] if candidate is not None else None
                ),
                candidate=candidate,
                event_data={
                    "workflow_uuid": workflow_uuid,
                    "cause": cause,
                    "workflow_revision": workflow["revision"],
                    "draft_hash": actual_hash,
                    "candidate_hash": (
                        candidate["candidate_hash"] if candidate is not None else None
                    ),
                },
            )
            if source is not None:
                self._catalog_generation_tracker.record_compilation(
                    workflow_uuid,
                    compilation.template_catalog_fingerprint,
                )
            else:
                self._catalog_generation_tracker.record_compilation(
                    workflow_uuid,
                    None,
                )
            return self.get_authoring(workflow_uuid)

    def submit_source_change(
        self,
        workflow_uuid: str,
        *,
        observed_signature: tuple[Any, ...],
    ) -> bool:
        """提交一个稳定观测到的工作流源码（Workflow Source）变化命令。

        参数：``workflow_uuid`` 是已注册来源绑定的稳定工作流身份；
        ``observed_signature`` 是源码监视器（Source Monitor）去抖后的文件世代。
        返回：只有相同文件世代完成哈希去重、候选推进及待写回恢复时才为
        ``True``；文件并发变化或持久恢复仍待处理时返回 ``False``。读取、编译或
        持久化异常原样映射为稳定工作流错误，调用者不得把异常视为已确认。
        """

        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            # ``current_signature`` 是服务在取得创作锁后复核的文件世代，防止监视
            # 线程用过期观测授权编译更新中的文件。
            current_signature = self.source_signature(workflow_uuid)
            if current_signature != observed_signature:
                return False
            # ``catalog_fingerprint`` 是当前服务编译器的目录代际；它与文件签名
            # 独立变化，必须强制替换旧目录产生的候选或失败诊断。
            catalog_fingerprint = self._catalog_fingerprint()
            force_compile = self._catalog_generation_tracker.requires_compile(
                workflow_uuid,
                catalog_fingerprint,
            )
            self.reconcile_registered_source(
                workflow_uuid,
                force_compile=force_compile,
            )
            # ``latest_signature`` 证明整个状态推进期间规范源码没有再次变化。
            latest_signature = self.source_signature(workflow_uuid)
            if latest_signature != observed_signature:
                return False
            record = self._definition_store.get_authoring_record(workflow_uuid)
            return record["writeback_status"] != "pending"

    def apply_authoring(
        self,
        workflow_uuid: str,
        *,
        candidate_hash: str,
        preserve_author_source: bool = False,
    ) -> dict[str, Any]:
        """按服务端候选哈希线性化应用可信工作流创作结果。

        参数：``workflow_uuid`` 是工作流（Workflow）稳定身份；``candidate_hash``
        是服务端持久并签发的候选哈希（Candidate Hash），客户端不得重述草稿、
        工作流修订或候选包；``preserve_author_source`` 仅供启动固定点激活，
        禁止把自动扫描变成未确认的规范化编辑。返回：应用结果与最新创作聚合。
        异常：候选、源码权威（Source Authority）、工作流修订（Workflow
        Revision）或目录指纹已变化时抛出稳定 ``WorkflowConflict``；候选无效时
        抛出 ``WorkflowError``。
        """

        self._validate_hash(candidate_hash, nullable=False)
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            record = self._definition_store.get_authoring_record(workflow_uuid)
            candidate = record.get("candidate")
            if candidate is None:
                if any(
                    str(item.get("severity", "")).lower() == "error"
                    for item in record["diagnostics"]
                ):
                    raise WorkflowError("draft_invalid")
                raise WorkflowConflict("candidate_not_ready")
            if candidate.get("candidate_hash") != candidate_hash:
                raise WorkflowConflict("candidate_hash_conflict")
            try:
                # 这些前置事实只从持久候选推导，客户端无法混搭不同世代。
                expected_draft_hash = candidate["draft_hash"]
                expected_workflow_revision = candidate["base_workflow_revision"]
                expected_catalog_fingerprint = candidate["template_catalog_fingerprint"]
            except (KeyError, TypeError):
                raise WorkflowError("candidate_invalid") from None
            self._validate_hash(expected_draft_hash, nullable=False)
            if (
                type(expected_workflow_revision) is not int
                or expected_workflow_revision < 1
            ):
                raise WorkflowError("candidate_invalid")
            self._validate_hash(expected_catalog_fingerprint, nullable=False)

            source = self._read_source(registration)
            if source is None:
                raise WorkflowConflict("draft_hash_conflict")
            # D-079 的源码、修订、目录冲突顺序继续保持稳定。
            actual_hash = source["draft_hash"]
            if actual_hash != expected_draft_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if workflow["revision"] != expected_workflow_revision:
                raise WorkflowConflict("workflow_revision_conflict")
            if self._catalog_fingerprint() != expected_catalog_fingerprint:
                raise WorkflowConflict("template_catalog_conflict")

            prevalidated_candidate = getattr(
                self._workspace_activation_context,
                "prevalidated_candidate",
                None,
            )
            # 只有固定点启动线程刚签发的候选，且其源码仍是发布时字节，才可
            # 使用冷启动合同修订基线。公共/交互 Apply 没有该线程标记，始终
            # 走普通递增语义。
            bootstrap_entry: tuple[int, str, str] | None = None
            bootstrap_attempt = False
            if (
                preserve_author_source
                and prevalidated_candidate == (workflow_uuid, candidate_hash)
            ):
                with self._bootstrap_published_revisions_lock:
                    bootstrap_entry = self._bootstrap_published_revisions.get(
                        workflow_uuid
                    )
                bootstrap_attempt = (
                    bootstrap_entry is not None
                    and expected_workflow_revision == bootstrap_entry[0]
                )
            bootstrap_no_advance = False
            if (
                bootstrap_attempt
                and bootstrap_entry is not None
                and actual_hash == bootstrap_entry[1]
            ):
                # 原始源码字节哈希相同仍不足以证明发布合同不变：编译器/模板目录
                # 可能已换代并产生不同图。只有候选图重新计算出的合同图摘要也相同，
                # 才能在冷启动时保持不可变发布修订；否则按一次真实图编辑递增。
                try:
                    candidate_graph_hash = published_graph_semantic_hash(
                        candidate["graph"]
                    )
                except (KeyError, TypeError, ValueError, PublishedContractInvalid):
                    candidate_graph_hash = None
                bootstrap_no_advance = candidate_graph_hash == bootstrap_entry[2]
            if prevalidated_candidate != (workflow_uuid, candidate_hash):
                applied_graph = self.get_graph(workflow_uuid)
                # 必须针对签发哈希时使用的精确候选图再次校验。服务端生成组合调用
                # 时，API 已在候选图中固化执行器绑定，而创作源码自身无法表达这些
                # 运行时绑定。
                compilation_graph = candidate.get("graph")
                if not isinstance(compilation_graph, Mapping):
                    raise WorkflowError("candidate_invalid")
                compilation = self._compile(
                    workflow=workflow,
                    graph=compilation_graph,
                    registration=registration,
                    python_source=source["python_source"],
                )
                if preserve_author_source:
                    compilation = self._preserve_author_source_compilation(
                        compilation=compilation,
                        workflow=workflow,
                        graph=compilation_graph,
                        python_source=source["python_source"],
                    )
                if compilation.graph is not None:
                    compilation = compilation.model_copy(
                        update={
                            "changeset": candidate_changeset(
                                graph=compilation.graph,
                                applied_graph=applied_graph,
                            )
                        }
                    )
                if not self._normalize_candidate_diagnostics(
                    compilation,
                    python_source=source["python_source"],
                ):
                    raise WorkflowError("candidate_invalid")
                if not compilation.valid:
                    if any(
                        str(item.get("severity", "")).lower() == "error"
                        for item in compilation.diagnostics
                    ):
                        raise WorkflowError("draft_invalid")
                    raise WorkflowError("candidate_invalid")
                revalidated = self._issue_candidate(
                    workflow_revision=workflow["revision"],
                    draft_hash=source["draft_hash"],
                    compilation=compilation,
                    applied_graph=applied_graph,
                    draft_python_source=source["python_source"],
                )
                if revalidated is None:
                    raise WorkflowError("candidate_invalid")
                if (
                    revalidated["template_catalog_fingerprint"]
                    != expected_catalog_fingerprint
                ):
                    raise WorkflowConflict("template_catalog_conflict")
                if revalidated["candidate_hash"] != candidate_hash:
                    raise WorkflowConflict("candidate_hash_conflict")

            def validate_authoring_authorities(
                linearized_draft_hash: str,
                linearized_catalog_fingerprint: str,
            ) -> None:
                """在写事务内复核源码与目录两项创作权威。

                参数：``linearized_draft_hash`` 是存储从持久候选推导的草稿哈希。
                ``linearized_catalog_fingerprint`` 是同一候选的目录指纹
                （Catalog Fingerprint）。返回：无；源码或目录世代变化时抛出稳定
                冲突，使同一 SQLite 事务回滚。该回调不接受客户端事实。
                """

                latest_source = self._read_source(registration)
                if (
                    latest_source is None
                    or latest_source["draft_hash"] != linearized_draft_hash
                ):
                    raise WorkflowConflict("draft_hash_conflict")
                if self._catalog_fingerprint() != linearized_catalog_fingerprint:
                    raise WorkflowConflict("template_catalog_conflict")

            try:
                candidate_workflow = candidate["graph"]["workflow"]
                candidate_workflow_type = normalize_workflow_type(
                    candidate_workflow.get("workflow_type")
                )
                candidate_meta_data = dict(candidate_workflow.get("meta_data") or {})
            except (KeyError, TypeError, ValueError):
                raise WorkflowError("candidate_invalid") from None

            normalized_source = candidate["normalized_python_source"]
            normalized_bytes = normalized_source.encode("utf-8")
            normalized_hash = _sha256(normalized_bytes)
            applied_source = {
                "python_source": normalized_source,
                "source_hash": normalized_hash,
                "source_map": candidate["source_map"],
                "compiler_version": candidate["compiler_version"],
                "template_catalog_fingerprint": candidate[
                    "template_catalog_fingerprint"
                ],
            }
            # 在进入唯一写事务前最后复核目录权威（Catalog Authority）。
            if self._catalog_fingerprint() != expected_catalog_fingerprint:
                raise WorkflowConflict("template_catalog_conflict")
            previous_revision = expected_workflow_revision
            with self._operation_category_lock:
                # Python 文件可以直接修改根类型和类别。应用候选前重新读取类别
                # 权威，并把校验与定义事务线性化；这样类别删除不会与源码应用
                # 交错，普通工作流也不能经 AST 路径旁路类别组合约束。
                self._validated_operation_category_meta_data(
                    workflow_type=candidate_workflow_type,
                    meta_data=candidate_meta_data,
                    tags=candidate_workflow.get("tags"),
                )
                try:
                    (
                        resulting_revision,
                        writeback_generation,
                    ) = self._definition_store.apply_authoring_candidate(
                        workflow_uuid=workflow_uuid,
                        candidate_hash=candidate_hash,
                        authoring_authority_validator=(validate_authoring_authorities),
                        advance_revision=not bootstrap_no_advance,
                    )
                except StoreAuthoringConflict as error:
                    raise WorkflowConflict(error.code, message=error.message) from None
                except StoreRevisionConflict:
                    raise WorkflowConflict("workflow_revision_conflict") from None
                except (StoreConflict, ValidationError):
                    raise WorkflowError("candidate_invalid") from None
            if bootstrap_attempt:
                # 无论源码是否仍与合同一致，首次启动 Apply 成功后都消费闸门；
                # 变化源码已经按普通递增提交，后续监视/交互不能再次借用旧基线。
                with self._bootstrap_published_revisions_lock:
                    if self._bootstrap_published_revisions.get(workflow_uuid) == (
                        bootstrap_entry
                    ):
                        self._bootstrap_published_revisions.pop(workflow_uuid, None)

            warnings: list[dict[str, str]] = []
            if (
                self._compiler_rebuilder is not None
                and not self._workspace_activation_batch
            ):
                try:
                    rebuilt_compiler = self._compiler_rebuilder()
                except Exception:  # noqa: BLE001 - 主事务已提交，只能关闭目录
                    # 应用图已经提交，目录刷新失败时撤销编译入口，禁止继续用陈旧
                    # 指纹签发父候选；下次进程启动会从持久图重建完整代际。
                    self.compiler = None
                    warnings.append(
                        {
                            "code": "template_catalog_rebuild_pending",
                            "message": (
                                "工作流已应用，但模板目录重建失败；"
                                "创作编译已关闭，重启后将自动恢复。"
                            ),
                        }
                    )
                else:
                    self.compiler = rebuilt_compiler
            response_source = source

            def warn_writeback() -> None:
                if warnings:
                    return
                warnings.append(
                    {
                        "code": "draft_writeback_pending",
                        "message": (
                            "工作流已应用，但本地源码同步失败；"
                            "OS 已保留可恢复的源码记录。"
                        ),
                    }
                )

            def mark_pending_best_effort() -> None:
                for _attempt in range(2):
                    try:
                        marker_owned = self._definition_store.mark_writeback_pending(
                            workflow_uuid=workflow_uuid,
                            expected_writeback_source=normalized_source,
                            expected_writeback_hash=actual_hash,
                            expected_writeback_generation=writeback_generation,
                        )
                        if not marker_owned:
                            # 新 Apply/Draft 已接管 marker，旧 generation 不再重试。
                            return
                        return
                    except Exception:  # noqa: BLE001 - 提交后只能尽力恢复
                        continue

            try:
                latest = self._read_source(registration)
                if latest is None or latest["draft_hash"] != actual_hash:
                    raise WorkflowError("draft_hash_conflict")
                if normalized_hash == actual_hash:
                    # 自动激活以及已经规范的交互 Apply 都不替换相同作者字节，
                    # 避免制造虚假的 IDE 保存事件和文件世代。
                    written = latest
                else:
                    self._atomic_write(
                        registration,
                        normalized_bytes,
                        expected_hash=actual_hash,
                    )
                    written = self._read_source(registration)
                    assert written is not None
                response_source = written
                if written["draft_hash"] != normalized_hash:
                    raise WorkflowConflict("draft_hash_conflict")
            except Exception:  # noqa: BLE001 - 主事务已提交
                # 主事务已经提交。之后任何文件系统、数据库或聚合错误
                # 都只能降级为可恢复警告，不能把成功伪装成失败。
                warn_writeback()
                mark_pending_best_effort()
            else:
                settled = False
                for _attempt in range(2):
                    try:
                        marker_owned = self._definition_store.settle_writeback(
                            workflow_uuid=workflow_uuid,
                            expected_writeback_source=normalized_source,
                            expected_writeback_hash=actual_hash,
                            expected_writeback_generation=writeback_generation,
                            observed_draft_hash=written["draft_hash"],
                            draft_update_time=written["update_time"],
                        )
                        if not marker_owned:
                            # 新 generation 已接管；陈旧 settle 无需恢复。
                            settled = True
                            break
                        settled = True
                        break
                    except Exception:  # noqa: BLE001 - 主事务已提交
                        warn_writeback()
                if not settled:
                    mark_pending_best_effort()

            fallback_meta_data = dict(workflow["meta_data"])
            candidate_workflow = candidate["graph"].get("workflow") or {}
            candidate_meta_data = candidate_workflow.get("meta_data") or {}
            if (
                isinstance(candidate_meta_data, dict)
                and "unilab" in candidate_meta_data
            ):
                fallback_meta_data["unilab"] = candidate_meta_data["unilab"]
            fallback_workflow = {
                **workflow,
                "revision": resulting_revision,
                "meta_data": fallback_meta_data,
                "update_time": utc_now(),
            }
            fallback_applied_source = {
                **applied_source,
                "workflow_revision": resulting_revision,
                "update_time": utc_now(),
            }
            fallback_record = {
                "observed_draft_hash": (
                    response_source["draft_hash"]
                    if response_source is not None
                    else None
                ),
                "diagnostics": [],
                "candidate": None,
                "applied_source": fallback_applied_source,
            }
            try:
                authoring = self.get_authoring(workflow_uuid)
            except Exception:  # noqa: BLE001 - 主事务已提交
                try:
                    authoring = self.get_authoring(workflow_uuid)
                except Exception:  # noqa: BLE001 - 使用已知事实降级
                    try:
                        fallback_graph = self.get_graph(workflow_uuid)
                        fallback_workflow = fallback_graph["workflow"]
                        fallback_record = self._definition_store.get_authoring_record(
                            workflow_uuid
                        )
                    except Exception:  # noqa: BLE001 - 使用提交时事实降级
                        fallback_graph = self._post_commit_candidate_graph(
                            candidate["graph"],
                            workflow=fallback_workflow,
                        )
                    authoring = self._authoring_aggregate(
                        workflow=fallback_workflow,
                        graph=fallback_graph,
                        registration=registration,
                        source=response_source,
                        record=fallback_record,
                    )

            result = {
                "apply_result": {
                    "kind": candidate["changeset"]["kind"],
                    "previous_workflow_revision": previous_revision,
                    "workflow_revision": resulting_revision,
                    "applied_candidate_hash": candidate["candidate_hash"],
                    "applied_source_hash": normalized_hash,
                    "warnings": warnings,
                },
                "authoring": authoring,
            }
        # 引用方父工作流只能在当前子流程发布后刷新。Apply 仅提交子流程的
        # 编辑候选；如果此处提前重编译并应用父流程，父图会切换到尚未发布的
        # 子工作流修订，且父子调用节点的合同身份可能被清空。发布路径统一由
        # ``_refresh_published_contract_dependents`` 按不可变发布合同刷新，
        # 因此 Apply 阶段不得触发任何依赖方更新。
        return result

    def latest_event_sequence(self) -> int:
        """先转发定义通知，再固定新订阅的持久事件水位。"""
        self._forward_definition_events()
        return self._store.latest_event_sequence()

    def read_event_stream(
        self, *, after_sequence: int, limit: int = 100,
        replay_retained: bool = False,
    ) -> dict[str, Any]:
        """提供带保留窗口检查的 SSE 页，普通历史读取合同保持不变。"""
        self._forward_definition_events()
        return self._event_reader.read_stream(
            after_sequence=after_sequence, limit=limit,
            replay_retained=replay_retained,
        )

    def prune_frontend_events(
        self, *, before: str, max_count: int, batch_size: int,
    ) -> int:
        """清理持久通知前缀；内存定义通知须先完成转发，不能在此清理。"""
        return self._store.prune_frontend_events(
            before=before, max_count=max_count, batch_size=batch_size,
        )

    def list_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        after_id: int | None = None,
    ) -> dict[str, Any]:
        """读取服务器发送事件（SSE）使用的持久失效通知页。

        参数：``after_sequence`` 是规范排他游标，``limit`` 是公开页长；
        ``after_id`` 仅兼容现有进程内调用，不能与非零规范游标并用。返回：含事件、
        下一游标与是否还有后页的只读投影，并保留旧 ``after_id`` 回显。异常：
        参数非法时抛稳定 ``WorkflowError``；持久投影损坏时传播
        ``EventProjectionError`` 形成服务器失败，不误报为客户端输入错误。本方法
        不写任何运行状态。
        """

        if after_id is not None:
            if after_sequence != 0:
                raise WorkflowError("invalid_input")
            after_sequence = after_id
        try:
            self._forward_definition_events()
            page = self._event_reader.read(
                after_sequence=after_sequence,
                limit=limit,
            )
        except ValueError:
            raise WorkflowError("invalid_input")
        return {**page, "after_id": after_sequence}

    def _forward_definition_events(self) -> None:
        """把本进程定义变更投影到持久 SSE 失效通知流。

        参数：无。返回无；定义目录与运行库相同时无需转发。每批先完整写入运行
        事件事务，再推进局部游标；事件只用于提示客户端重读，不成为工作流定义。
        """

        if self._definition_store is self._store:
            return
        with self._definition_event_lock:
            while True:
                events = self._definition_store.list_events(
                    after_sequence=self._definition_event_cursor,
                    limit=200,
                )
                if not events:
                    return
                self._store.append_forwarded_events(events)
                self._definition_event_cursor = int(events[-1]["id"])
                if len(events) < 200:
                    return

    # 工作流创作（Authoring）内部实现 -------------------------------------

    def _get_authoring_workflow(
        self,
        workflow_uuid: str,
    ) -> dict[str, Any]:
        try:
            identity = validate_uuid(workflow_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self._definition_store.get_workflow(identity)
        except StoreNotFound:
            raise WorkflowError("workflow_not_found") from None

    def _registration(self, workflow_uuid: str) -> dict[str, Any]:
        """读取当前进程仍授权的规范源码注册。

        参数：``workflow_uuid`` 是已校验的工作流稳定身份。返回：当前活动来源
        注册。异常：未在本次启动 allowlist 中授权或目录行缺失时统一抛出
        ``workflow_not_found``，且在拒绝前不触碰持久路径。
        """

        with self._active_sources_lock:
            if workflow_uuid not in self._active_source_workflow_uuids:
                raise WorkflowError("workflow_not_found")
        try:
            return self._definition_store.get_source_registration(workflow_uuid)
        except StoreNotFound:
            raise WorkflowError("workflow_not_found") from None

    def _read_source(
        self,
        registration: dict[str, Any],
    ) -> dict[str, Any] | None:
        """通过源码工作区（SourceWorkspace）读取一项已注册草稿。

        参数：``registration`` 是本进程已授权的来源身份。
        返回：缺失时为 ``None``，否则返回源码、草稿哈希和修改时间字典。
        异常：不安全或超限文件映射为 ``invalid_input``。
        """

        try:
            source = read_registered_source(registration)
        except SourceWorkspaceError:
            raise WorkflowError("invalid_input") from None
        if source is None:
            return None
        return {
            "python_source": source.python_source,
            "draft_hash": source.draft_hash,
            "update_time": source.update_time,
        }

    def source_signature(
        self,
        workflow_uuid: str,
    ) -> tuple[Any, ...]:
        """按当前授权的工作流身份返回轻量源码签名。

        参数：``workflow_uuid`` 是工作流源码（Workflow Source）绑定的稳定身份；
        本方法不接受调用者缓存的注册路径。
        返回：缺失标记或普通文件的身份、大小和时间签名。
        异常：已撤权身份稳定映射为 ``workflow_not_found``；不安全路径或非普通
        文件映射为 ``invalid_input``。安全：每次读取都在工作流创作锁内重新取得
        当前注册，撤权返回后旧注册信息不能继续触碰文件系统；尚未装配编译器的
        来源管理用途只返回文件签名，不伪造模板目录代际。
        """

        with self._authoring_lock(workflow_uuid):
            registration = self._registration(workflow_uuid)
            try:
                file_signature = registered_source_signature(registration)
                if self.compiler is None:
                    return self._catalog_generation_tracker.source_signature(
                        file_signature,
                        None,
                    )
                # 保留既有文件签名首项，末尾追加模板目录代际；旧诊断调用者仍可
                # 识别 ``file``/``missing``，统一监视器则会在目录换代时得到新签名。
                return self._catalog_generation_tracker.source_signature(
                    file_signature,
                    self._catalog_fingerprint(),
                )
            except SourceWorkspaceError:
                raise WorkflowError("invalid_input") from None

    def _atomic_write(
        self,
        registration: dict[str, Any],
        content: bytes,
        *,
        expected_hash: Any = _NO_EXPECTED_HASH,
    ) -> None:
        """通过源码工作区（SourceWorkspace）执行原子 CAS 草稿写入。

        参数：``registration`` 是来源身份；``content`` 是 UTF-8 源码字节；
        ``expected_hash`` 是可选原稿哈希条件。
        返回：无；成功时规范源码完整替换。
        异常：CAS 变化映射为 ``draft_hash_conflict``，其他失败映射为稳定错误。
        """

        try:
            write_registered_source(
                registration,
                content,
                expected_hash=expected_hash,
            )
        except SourceWorkspaceConflict:
            raise WorkflowConflict("draft_hash_conflict") from None
        except SourceWorkspaceError as error:
            error_code = (
                "invalid_input" if error.code == "invalid_input" else "internal_error"
            )
            raise WorkflowError(error_code) from None

    def _compile(
        self,
        *,
        workflow: dict[str, Any],
        graph: dict[str, Any],
        registration: dict[str, Any],
        python_source: str,
    ) -> CandidateCompilation:
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        try:
            result = self.compiler.compile(
                workflow_uuid=workflow["uuid"],
                workflow_revision=workflow["revision"],
                python_source=python_source,
                source_uri=registration["source_uri"],
                applied_graph=graph,
            )
            return CandidateCompilation.model_validate(result)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("internal_error") from None

    def _preserve_author_source_compilation(
        self,
        *,
        compilation: CandidateCompilation,
        workflow: dict[str, Any],
        graph: dict[str, Any],
        python_source: str,
    ) -> CandidateCompilation:
        """让自动激活候选使用原始作者源码及其精确源码映射。

        参数：``compilation`` 是当前目录刚生成的结果；``workflow``、``graph``
        和 ``python_source`` 是同一编译事务的权威输入。返回可按普通候选合同
        签发的源码保留结果。异常：编译器不支持可信源码保留或投影失败时抛
        ``candidate_invalid``，固定点激活会隔离该来源且绝不回写规范化文本。
        """

        if (
            not compilation.valid
            or compilation.normalized_python_source == python_source
        ):
            return compilation
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        preserve = getattr(self.compiler, "preserve_author_source", None)
        if not callable(preserve):
            raise WorkflowError("candidate_invalid")
        try:
            result = preserve(
                compilation=compilation,
                workflow_uuid=workflow["uuid"],
                workflow_revision=workflow["revision"],
                python_source=python_source,
                applied_graph=graph,
            )
            preserved = CandidateCompilation.model_validate(result)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("candidate_invalid") from None
        if preserved.normalized_python_source != python_source:
            raise WorkflowError("candidate_invalid")
        return preserved

    def _catalog_fingerprint(self) -> str:
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        try:
            value = self.compiler.template_catalog_fingerprint
        except Exception:
            raise WorkflowError("template_catalog_unavailable") from None
        if not isinstance(value, str) or _HASH_TOKEN.fullmatch(value) is None:
            raise WorkflowError("template_catalog_unavailable")
        return value

    @staticmethod
    def _source_only_candidate_is_already_applied(
        *,
        candidate: dict[str, Any] | None,
        applied_source: Any,
        workflow_revision: int,
        draft_hash: str,
    ) -> bool:
        """判断当前目录重编译是否只重新证明了既有应用事实。

        参数：候选稿（Candidate）、已应用源码、当前工作流修订和作者源码哈希。
        返回：候选不改变图、同一作者字节和同一模板目录代际均已绑定当前修订
        时为 ``True``。异常：无；持久派生字段形状异常只按不匹配处理。
        """

        return (
            isinstance(candidate, dict)
            and candidate.get("changeset", {}).get("kind") == "source_only"
            and isinstance(applied_source, dict)
            and applied_source.get("workflow_revision") == workflow_revision
            and applied_source.get("source_hash") == draft_hash
            and isinstance(candidate.get("template_catalog_fingerprint"), str)
            and candidate.get("template_catalog_fingerprint")
            == applied_source.get("template_catalog_fingerprint")
        )

    def _issue_candidate(
        self,
        *,
        workflow_revision: int,
        draft_hash: str,
        compilation: CandidateCompilation,
        applied_graph: dict[str, Any],
        draft_python_source: str,
    ) -> dict[str, Any] | None:
        """校验编译结果并签发一个可信候选稿（Candidate）。

        参数：``workflow_revision`` 与 ``draft_hash`` 固定候选基线；``compilation``
        是编译结果；``applied_graph`` 是当前应用图；``draft_python_source`` 用于
        诊断和源码映射校验。返回：包含规范八字段、候选哈希（Candidate Hash）
        和更新时间的候选字典；编译结果不能证明时返回 ``None``。异常：目录不可
        用等非候选错误原样传播，其他候选结构或编码错误转为稳定诊断。
        """

        applied_graph = self._validated_applied_backend_graph(applied_graph)
        if not self._normalize_candidate_diagnostics(
            compilation,
            python_source=draft_python_source,
        ):
            return None
        if not compilation.valid:
            return None
        assert compilation.graph is not None
        try:
            graph = self._backend_candidate_graph(
                compilation.graph,
                applied_graph=applied_graph,
            )
            if not isinstance(compilation.source_map, list):
                raise ValueError
            source_map = [
                CandidateSourceMapEntry.model_validate(item).model_dump()
                for item in compilation.source_map
            ]
            if not source_ranges_fit(
                compilation.normalized_python_source,
                source_map,
            ):
                raise ValueError
            # 签发用的是补齐读取字段后的后端图。变更集必须按这张图重算，
            # 否则删除连线后编译图和读取图的边集合对不上，候选会被拒绝。
            changeset = candidate_changeset(
                graph=graph,
                applied_graph=applied_graph,
            )
            validate_candidate_bundle(
                graph=graph,
                base_graph=applied_graph,
                workflow_uuid=graph["workflow"]["uuid"],
                revision=workflow_revision,
                source_map=source_map,
                changeset=changeset,
            )
            self._definition_store.validate_candidate_identity_ownership(
                workflow_uuid=graph["workflow"]["uuid"],
                node_uuids=(item["uuid"] for item in graph["nodes"]),
                edge_uuids=(item["uuid"] for item in graph["edges"]),
            )
            compiler_version = compilation.compiler_version
            if not compiler_version.strip():
                raise ValueError
            template_catalog_fingerprint = compilation.template_catalog_fingerprint
            if _HASH_TOKEN.fullmatch(template_catalog_fingerprint) is None:
                raise ValueError
        except StoreAuthoringConflict as error:
            if error.code != "candidate_identity_conflict":
                raise
            self._set_candidate_identity_conflict_diagnostic(compilation)
            return None
        except (
            GraphValidationError,
            CandidateBundleError,
            KeyError,
            TypeError,
            ValidationError,
            ValueError,
            WorkflowError,
        ) as error:
            if isinstance(error, WorkflowError) and error.code != "candidate_invalid":
                raise
            logger.exception(
                "工作流候选签发失败 workflow_uuid=%s revision=%s draft_hash=%s "
                "error=%s",
                compilation.graph.get("workflow", {}).get("uuid")
                if isinstance(compilation.graph, dict)
                else None,
                workflow_revision,
                draft_hash,
                error,
            )
            self._set_candidate_invalid_diagnostic(compilation)
            return None
        bundle = {
            "base_workflow_revision": workflow_revision,
            "draft_hash": draft_hash,
            "graph": graph,
            "normalized_python_source": compilation.normalized_python_source,
            "source_map": source_map,
            "changeset": changeset,
            "compiler_version": compiler_version,
            "template_catalog_fingerprint": template_catalog_fingerprint,
        }
        try:
            # ``candidate_hash`` 是共享八字段规则对本次签发正文的唯一稳定摘要。
            candidate_hash = compute_authoring_candidate_hash(bundle)
        except AuthoringCandidateHashError:
            self._set_candidate_invalid_diagnostic(compilation)
            return None
        return {
            "candidate_hash": candidate_hash,
            **bundle,
            "update_time": utc_now(),
        }

    @staticmethod
    def _set_candidate_invalid_diagnostic(
        compilation: CandidateCompilation,
    ) -> None:
        compilation.diagnostics = [
            {
                "severity": "error",
                "code": "candidate_invalid",
                "message": _ERRORS["candidate_invalid"][1],
            }
        ]

    @staticmethod
    def _set_candidate_identity_conflict_diagnostic(
        compilation: CandidateCompilation,
    ) -> None:
        """把跨工作流节点/连线身份占用投影为可行动候选诊断。"""

        compilation.diagnostics = [
            {
                "severity": "error",
                "code": "candidate_identity_conflict",
                "message": _ERRORS["candidate_identity_conflict"][1],
            }
        ]

    @classmethod
    def _normalize_candidate_diagnostics(
        cls,
        compilation: CandidateCompilation,
        *,
        python_source: str,
    ) -> bool:
        try:
            if not isinstance(compilation.diagnostics, list):
                raise ValueError
            compilation.diagnostics = [
                CandidateDiagnostic.model_validate(item).model_dump(
                    exclude_none=True,
                )
                for item in compilation.diagnostics
            ]
            source_ranges = [
                item["source_range"]
                for item in compilation.diagnostics
                if item.get("source_range") is not None
            ]
            if not source_ranges_fit(python_source, source_ranges):
                raise ValueError
        except (TypeError, ValidationError, ValueError):
            cls._set_candidate_invalid_diagnostic(compilation)
            return False
        return True

    @staticmethod
    def _backend_graph_projection(
        graph: dict[str, Any],
    ) -> dict[str, Any]:
        """按后端（Backend）JSON omitempty 语义投影候选稿（Candidate）。

        参数：``graph`` 是编译器产出的完整候选图。返回：删除可选 ``None`` 字段、
        保留后端读取容器形状的新字典；输入图不被修改。
        """

        def omit_none(value: Any) -> Any:
            """删除单个实体中的 ``None`` 字段；非字典值保持原样返回。"""

            if not isinstance(value, dict):
                return value
            return {key: item for key, item in value.items() if item is not None}

        return {
            "workflow": omit_none(graph.get("workflow") or {}),
            "nodes": [omit_none(item) for item in (graph.get("nodes") or [])],
            "edges": [omit_none(item) for item in (graph.get("edges") or [])],
            "inventory_requirements": [
                omit_none(item)
                for item in (graph.get("inventory_requirements") or [])
            ],
            "node_templates": [
                omit_none(item) for item in (graph.get("node_templates") or [])
            ],
            "handle_templates": [
                omit_none(item) for item in (graph.get("handle_templates") or [])
            ],
        }

    @classmethod
    def _backend_candidate_graph(
        cls,
        graph: dict[str, Any],
        *,
        applied_graph: dict[str, Any],
    ) -> dict[str, Any]:
        """把编译器写实体补全为冻结的后端（Backend）读取形状。

        参数：``graph`` 是编译器写模型，``applied_graph`` 是当前已应用图。返回：
        补齐稳定身份、时间与读取字段的候选图；非法图抛出稳定工作流错误。
        """

        applied = cls._validated_applied_backend_graph(applied_graph)
        cls._require_candidate_graph_containers(graph)
        projected = cls._backend_graph_projection(graph)
        applied_workflow = applied["workflow"]
        workflow_uuid = applied_workflow["uuid"]
        timestamp = applied_workflow["update_time"]
        applied_nodes = {item["uuid"]: item for item in applied["nodes"]}
        applied_edges = {item["uuid"]: item for item in applied["edges"]}
        applied_requirements = {
            item["uuid"]: item for item in applied["inventory_requirements"]
        }
        applied_node_templates = {
            item["uuid"]: item for item in applied["node_templates"]
        }
        applied_handle_templates = {
            item["uuid"]: item for item in applied["handle_templates"]
        }

        nodes = []
        for item in projected["nodes"]:
            value = WorkflowNodeWrite.model_validate(item).model_dump(
                exclude_none=True,
            )
            # 普通节点的缺省人工确认配置不属于既有作者图语义。数据库读取会
            # 省略空对象，这里也保持同一 wire 形状，避免源码往返被误判成改图。
            if not value.get("manual_confirmation"):
                value.pop("manual_confirmation", None)
            persisted = applied_nodes.get(value["uuid"], {})
            nodes.append(
                {
                    "uuid": value["uuid"],
                    "create_time": persisted.get("create_time", timestamp),
                    "update_time": persisted.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    "workflow_uuid": workflow_uuid,
                    **value,
                }
            )
        cls._require_backend_read_fields(
            nodes,
            _NODE_REQUIRED_READ_FIELDS,
        )

        edges = []
        for item in projected["edges"]:
            value = WorkflowEdgeWrite.model_validate(item).model_dump(
                exclude_none=True,
            )
            persisted = applied_edges.get(value["uuid"], {})
            edges.append(
                {
                    "uuid": value["uuid"],
                    "create_time": persisted.get("create_time", timestamp),
                    "update_time": persisted.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    **value,
                }
            )
        cls._require_backend_read_fields(
            edges,
            _EDGE_REQUIRED_READ_FIELDS,
        )

        requirements = []
        for sort_order, item in enumerate(projected["inventory_requirements"]):
            value = WorkflowInventoryRequirementWrite.model_validate(item).model_dump(
                exclude_none=True,
            )
            identity = value.get("uuid")
            if identity is None:
                raise WorkflowError("candidate_invalid")
            persisted = applied_requirements.get(identity, {})
            requirements.append(
                {
                    "uuid": identity,
                    "create_time": persisted.get("create_time", timestamp),
                    "update_time": persisted.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    "workflow_uuid": workflow_uuid,
                    "sort_order": sort_order,
                    **value,
                }
            )
        cls._require_backend_read_fields(
            requirements,
            _INVENTORY_REQUIREMENT_REQUIRED_READ_FIELDS,
        )

        projected["workflow"] = {
            key: value
            for key, value in {
                **applied_workflow,
                **projected["workflow"],
                "uuid": workflow_uuid,
                "create_time": applied_workflow["create_time"],
                "update_time": timestamp,
            }.items()
            if key in _WORKFLOW_READ_FIELDS
        }
        projected["nodes"] = nodes
        projected["edges"] = edges
        projected["inventory_requirements"] = requirements
        projected["node_templates"] = cls._hydrate_backend_catalog_entities(
            projected["node_templates"],
            persisted=applied_node_templates,
            timestamp=timestamp,
            uuid_fields={"uuid", "resource_template_uuid"},
            allowed_fields=_NODE_TEMPLATE_READ_FIELDS,
            required_fields=_NODE_TEMPLATE_REQUIRED_READ_FIELDS,
        )
        projected["handle_templates"] = cls._hydrate_backend_catalog_entities(
            projected["handle_templates"],
            persisted=applied_handle_templates,
            timestamp=timestamp,
            uuid_fields={"uuid", "workflow_node_template_uuid"},
            allowed_fields=_HANDLE_TEMPLATE_READ_FIELDS,
            required_fields=_HANDLE_TEMPLATE_REQUIRED_READ_FIELDS,
        )
        cls._require_backend_read_fields(
            [projected["workflow"]],
            _WORKFLOW_REQUIRED_READ_FIELDS,
        )
        try:
            cls._require_backend_entity_types(projected)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise WorkflowError("candidate_invalid") from None
        if projected["workflow"]["revision"] != applied_workflow["revision"]:
            raise WorkflowError("candidate_invalid")
        return projected

    @classmethod
    def _validated_applied_backend_graph(
        cls,
        graph: dict[str, Any],
    ) -> dict[str, Any]:
        """检查候选稿（Candidate）前先校验权威（Authority）持有的工作流图。"""

        try:
            applied = cls._backend_graph_projection(graph)
            cls._require_backend_read_fields(
                [applied["workflow"]],
                _WORKFLOW_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["nodes"],
                _NODE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["edges"],
                _EDGE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["inventory_requirements"],
                _INVENTORY_REQUIREMENT_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["node_templates"],
                _NODE_TEMPLATE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["handle_templates"],
                _HANDLE_TEMPLATE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_entity_types(applied)
            return applied
        except WorkflowError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError):
            raise WorkflowError("internal_error") from None

    @staticmethod
    def _require_backend_entity_types(graph: dict[str, Any]) -> None:
        """在完整工作流图上强制执行冻结的后端（Backend）JSON 类型。

        参数：``graph`` 是待验证的完整工作流（Workflow）图。返回：无；任一字段
        类型偏离冻结合同即抛出 ``ValueError``。
        """

        def exact(entity: dict[str, Any], fields: set[str], expected: type) -> None:
            """要求 ``entity`` 指定字段严格等于 ``expected`` 类型。"""

            if any(type(entity[field]) is not expected for field in fields):
                raise ValueError

        def optional(
            entity: dict[str, Any],
            fields: set[str],
            expected: type,
        ) -> None:
            """要求存在的可选字段严格等于 ``expected`` 类型。"""

            if any(
                field in entity and type(entity[field]) is not expected
                for field in fields
            ):
                raise ValueError

        def uuids(entity: dict[str, Any], fields: set[str]) -> None:
            """要求指定字段均为合法 UUID 字符串；非法值抛出 ``ValueError``。"""

            exact(entity, fields, str)
            for field in fields:
                validate_uuid(entity[field])

        def optional_uuids(entity: dict[str, Any], fields: set[str]) -> None:
            """验证存在的可选 UUID 字段；缺失字段保持合法。"""

            for field in fields:
                if field in entity:
                    uuids(entity, {field})

        workflow = graph["workflow"]
        uuids(workflow, {"uuid"})
        exact(workflow, {"create_time", "update_time", "name"}, str)
        exact(workflow, {"meta_data"}, dict)
        exact(workflow, {"tags"}, list)
        normalize_json_object(workflow["meta_data"])
        normalize_json_array(workflow["tags"])
        optional(workflow, {"description", "workflow_type"}, str)
        if "workflow_type" in workflow:
            normalize_workflow_type(workflow["workflow_type"])
        revision = workflow["revision"]
        if type(revision) is not int or not 1 <= revision <= (1 << 63) - 1:
            raise ValueError

        for node in graph["nodes"]:
            uuids(node, {"uuid", "workflow_uuid"})
            optional_uuids(
                node,
                {
                    "workflow_node_template_uuid",
                    "parent_uuid",
                    "material_uuid",
                },
            )
            exact(
                node,
                {"create_time", "update_time", "name", "status", "type"},
                str,
            )
            exact(
                node,
                {"meta_data", "pose", "param", "execution_policy"},
                dict,
            )
            for field in ("meta_data", "pose", "param", "execution_policy"):
                normalize_json_object(node[field])
            exact(node, {"disabled", "minimized"}, bool)
            optional(
                node,
                {
                    "description",
                    "icon",
                    "footer",
                    "action_name",
                    "action_type",
                    "script",
                },
                str,
            )

        for edge in graph["edges"]:
            uuids(
                edge,
                {
                    "uuid",
                    "source_node_uuid",
                    "target_node_uuid",
                    "source_handle_uuid",
                    "target_handle_uuid",
                },
            )
            exact(edge, {"create_time", "update_time"}, str)
            exact(edge, {"meta_data"}, dict)
            normalize_json_object(edge["meta_data"])
            optional(edge, {"description"}, str)

        for requirement in graph["inventory_requirements"]:
            uuids(
                requirement,
                {"uuid", "workflow_uuid", "consume_node_uuid"},
            )
            optional_uuids(requirement, {"reagent_info_uuid"})
            exact(
                requirement,
                {
                    "create_time",
                    "update_time",
                    "requirement_key",
                    "target_type",
                    "quantity_unit",
                },
                str,
            )
            exact(requirement, {"meta_data"}, dict)
            normalize_json_object(requirement["meta_data"])
            exact(requirement, {"allow_split"}, bool)
            optional(requirement, {"description"}, str)
            quantity = requirement["required_quantity"]
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, (int, float))
                or not math.isfinite(float(quantity))
                or float(quantity) <= 0
            ):
                raise ValueError
            if requirement["target_type"] not in {
                "reagent_info",
                "current_substance",
            }:
                raise ValueError
            sort_order = requirement["sort_order"]
            if type(sort_order) is not int or sort_order < 0:
                raise ValueError

        for template in graph["node_templates"]:
            uuids(template, {"uuid", "resource_template_uuid"})
            exact(
                template,
                {
                    "create_time",
                    "update_time",
                    "name",
                    "display_name",
                    "type",
                    "node_type",
                },
                str,
            )
            exact(
                template,
                {
                    "meta_data",
                    "goal",
                    "goal_default",
                    "feedback",
                    "result",
                },
                dict,
            )
            for field in (
                "meta_data",
                "goal",
                "goal_default",
                "feedback",
                "result",
            ):
                normalize_json_object(template[field])
            optional(
                template,
                {
                    "description",
                    "class",
                    "schema",
                    "icon",
                    "header",
                    "footer",
                },
                str,
            )

        for handle in graph["handle_templates"]:
            uuids(handle, {"uuid", "workflow_node_template_uuid"})
            exact(
                handle,
                {
                    "create_time",
                    "update_time",
                    "handle_key",
                    "io_type",
                    "display_name",
                    "type",
                },
                str,
            )
            exact(handle, {"meta_data"}, dict)
            normalize_json_object(handle["meta_data"])
            exact(handle, {"required"}, bool)
            optional(
                handle,
                {"description", "data_source", "data_key"},
                str,
            )

    @staticmethod
    def _require_candidate_graph_containers(graph: dict[str, Any]) -> None:
        workflow = graph.get("workflow")
        if workflow is not None and not isinstance(workflow, dict):
            raise WorkflowError("candidate_invalid")
        for field in (
            "nodes",
            "edges",
            "inventory_requirements",
            "node_templates",
            "handle_templates",
        ):
            entities = graph.get(field)
            if entities is None:
                continue
            if not isinstance(entities, list) or any(
                not isinstance(item, dict) for item in entities
            ):
                raise WorkflowError("candidate_invalid")

    @staticmethod
    def _hydrate_backend_catalog_entities(
        entities: list[dict[str, Any]],
        *,
        persisted: dict[str, dict[str, Any]],
        timestamp: str,
        uuid_fields: set[str],
        allowed_fields: set[str],
        required_fields: set[str],
    ) -> list[dict[str, Any]]:
        hydrated = []
        for item in entities:
            value = {
                key: child
                for key, child in item.items()
                if key in allowed_fields and child is not None
            }
            for field in uuid_fields:
                try:
                    value[field] = validate_uuid(value[field])
                except (KeyError, ValueError):
                    raise WorkflowError("candidate_invalid") from None
            previous = persisted.get(value["uuid"], {})
            hydrated.append(
                {
                    "uuid": value["uuid"],
                    "create_time": previous.get("create_time", timestamp),
                    "update_time": previous.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    **value,
                }
            )
        WorkflowService._require_backend_read_fields(
            hydrated,
            required_fields,
        )
        return hydrated

    @staticmethod
    def _require_backend_read_fields(
        entities: list[dict[str, Any]],
        required_fields: set[str],
        *,
        error_code: str = "candidate_invalid",
    ) -> None:
        if any(
            not isinstance(item, dict) or not required_fields.issubset(item)
            for item in entities
        ):
            raise WorkflowError(error_code)

    @classmethod
    def _post_commit_candidate_graph(
        cls,
        graph: dict[str, Any],
        *,
        workflow: dict[str, Any],
    ) -> dict[str, Any]:
        projected = cls._backend_graph_projection(graph)
        projected["workflow"] = {
            **projected["workflow"],
            **workflow,
        }
        return projected

    def _authoring_aggregate(
        self,
        *,
        workflow: dict[str, Any],
        graph: dict[str, Any],
        registration: dict[str, Any],
        source: dict[str, Any] | None,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        draft: dict[str, Any] | None = None
        diagnostics: list[dict[str, Any]] = []
        if source is not None:
            if record["observed_draft_hash"] == source["draft_hash"]:
                diagnostics = record["diagnostics"]
            draft = {
                "source_uri": registration["source_uri"],
                **source,
                "diagnostics": diagnostics,
            }

        stored_candidate = record.get("candidate")
        candidate: dict[str, Any] | None = None
        candidate_stale = False
        if stored_candidate is not None and source is not None:
            catalog_matches = False
            try:
                catalog_matches = (
                    stored_candidate["template_catalog_fingerprint"]
                    == self._catalog_fingerprint()
                )
            except WorkflowError:
                catalog_matches = False
            candidate_current = (
                record["observed_draft_hash"] == source["draft_hash"]
                and stored_candidate["draft_hash"] == source["draft_hash"]
                and stored_candidate["base_workflow_revision"] == workflow["revision"]
                and catalog_matches
            )
            if candidate_current:
                candidate = stored_candidate
            else:
                candidate_stale = True

        applied_source = record.get("applied_source")
        if source is None:
            state = "draft_missing"
        elif candidate_stale:
            state = "candidate_stale"
        elif any(
            str(item.get("severity", "")).lower() == "error" for item in diagnostics
        ):
            state = "draft_invalid"
        elif candidate is not None:
            state = (
                "unapplied_source_only"
                if candidate["changeset"]["kind"] == "source_only"
                else "unapplied_graph"
            )
        elif (
            applied_source is not None
            and applied_source["workflow_revision"] == workflow["revision"]
            and applied_source["source_hash"] == source["draft_hash"]
        ):
            state = "applied"
        else:
            state = "applied_source_stale"

        return {
            "workflow_uuid": workflow["uuid"],
            "workflow_revision": workflow["revision"],
            # ``state`` 是创作诊断状态；前端正常展示发布徽标应使用这个稳定的
            # 两值业务状态，避免把 candidate_stale 等内部中间态暴露成产品状态。
            "status": self._public_workflow_with_status(workflow)["status"],
            "state": state,
            "applied_graph": graph,
            "draft": draft,
            "candidate": candidate,
            "applied_source": applied_source,
        }

    def _authoring_lock(self, workflow_uuid: str) -> threading.RLock:
        with self._locks_guard:
            return self._authoring_locks.setdefault(
                workflow_uuid,
                threading.RLock(),
            )

    @staticmethod
    def _normalize_page(page: int, page_size: int) -> tuple[int, int]:
        page = max(page, 1)
        if page_size < 1:
            page_size = 20
        page_size = min(page_size, 100)
        return page, page_size

    @staticmethod
    def _optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _validate_hash(value: str | None, *, nullable: bool) -> None:
        if value is None:
            if nullable:
                return
            raise WorkflowError("invalid_input")
        if _HASH_TOKEN.fullmatch(value) is None:
            raise WorkflowError("invalid_input")


__all__ = [
    "AuthoringCompiler",
    "WorkflowConflict",
    "WorkflowError",
    "WorkflowService",
]
