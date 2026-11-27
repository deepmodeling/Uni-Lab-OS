"""把本地调度器（EdgeScheduler）状态投影到标准任务/作业聚合。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unilabos.app.scheduler.timing_store import TimingObservation
from uuid import UUID, uuid4

from unilabos.utils.tracing import normalize_trace_context
from unilabos.workflow._execution_plan_graph import final_target_data_key
from unilabos.workflow.device_tenancy import (
    active_task_device_tenancies,
    check_device_tenancy,
    prepare_device_tenancy,
    require_active_device_tenancy,
    settle_device_tenancy,
    validate_native_operate_custody,
)
from unilabos.workflow.event_writer import append_frontend_event, append_runtime_event
from unilabos.workflow.execution_claim import (
    get_execution_claim,
    list_active_execution_claim_uuids,
    require_dispatchable_execution_claim,
)
from unilabos.workflow.execution_lock_lease import (
    list_execution_locks,
    mark_execution_locks_running,
    mark_execution_locks_uncertain,
    mirror_execution_locks_from_permit,
    record_execution_lock_operator_action,
    record_execution_lock_wait,
    release_execution_locks,
    release_task_execution_locks,
    try_acquire_execution_locks,
)
from unilabos.workflow.execution_restart_recovery import (
    fail_task_after_execution_process_restart,
    preserve_task_after_execution_process_restart,
)
from unilabos.workflow.execution_wait_graph import build_execution_wait_graph
from unilabos.workflow.frozen_json_cache import decode_large_json_text
from unilabos.workflow.intervention import (
    WorkflowInterventionStore,
    settle_intervention_for_job,
)
from unilabos.workflow.job_evidence import JobEvidenceStore, record_job_result
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.manual_confirmation import (
    ManualConfirmationStore,
    close_pending_manual_confirmation,
    manual_confirmation_projection,
    open_manual_confirmation,
)
from unilabos.workflow.material_source import MaterialCustodyPolicy
from unilabos.workflow.physical_settlement_policy import (
    MATERIAL_CONTENT_RECONCILIATION_REQUIRED,
    MATERIAL_TRANSFER_RECONCILIATION_REQUIRED,
    QUANTITY_RECONCILIATION_REQUIRED,
    PhysicalSettlementPolicyError,
    execution_stopped_evidence,
    plan_terminal_settlement,
)
from unilabos.workflow.resource_lock_key import (
    is_canonical_generic_resource_lock_key,
)
from unilabos.workflow.resource_lock_plan import node_has_explicit_resource_interval
from unilabos.workflow.scheduler_capacity import (
    activate_workflow_task,
    admit_job_dispatch,
    check_workflow_task_capacity,
)
from unilabos.workflow.station_event_outbox import append_station_event
from unilabos.workflow.station_status_projection import (
    append_job_state_event,
    append_task_state_event,
)
from unilabos.workflow.store import (
    StoreConflict,
    StoreNotFound,
    WorkflowStore,
    utc_now,
)
from unilabos.workflow.task_material_admission import (
    list_blocked_material_task_uuids,
    read_material_admission,
    record_admitted_admission,
    record_admitted_materials,
    record_blocked_admission,
)
from unilabos.workflow.workflow_boundary import (
    WorkflowBoundaryError,
    WorkflowBoundaryProjection,
    project_ready_workflow_output,
)


def _lock_release_block_reason(
    *,
    task_status: str,
    job_status: str,
    uncertainty_reason: str,
    lease_state: str,
    claim_state: str,
    has_active_tenancy: bool,
) -> str | None:
    """返回锁详情页展示的人工释放阻断原因；可释放时返回 ``None``。"""

    if task_status not in {"failed", "canceled", "timeout"}:
        return "任务尚未进入失败、取消或超时终态"
    if job_status not in {"failed", "canceled", "timeout"}:
        return "所属作业尚未进入终态"
    if uncertainty_reason:
        return "作业存在结果不确定原因，需先完成物理结算"
    if lease_state == "uncertain":
        return "锁处于结果不确定状态，禁止人工释放"
    if lease_state not in {"reserved", "running"}:
        return "锁已释放"
    if claim_state == "uncertain":
        return "Claim 处于结果不确定状态，禁止人工释放"
    if claim_state not in {"reserved", "running"}:
        return "Claim 不处于活动状态"
    if has_active_tenancy:
        return "任务仍有活动设备托管"
    return None


_ACTIVE_JOB_STATES = frozenset({"pending", "dispatched", "running"})
_TERMINAL_JOB_STATES = frozenset(
    {"succeeded", "failed", "skipped", "canceled", "timeout"}
)


def _retained_resource_intervals(
    *,
    connection: sqlite3.Connection,
    task_row: sqlite3.Row,
    job_row: sqlite3.Row,
    control_data: Mapping[str, Any],
    snapshot: tuple[Any, list[dict[str, Any]]] | None = None,
) -> tuple[str, ...]:
    """返回当前成功节点之后仍未到释放边界的区间身份。"""

    raw_ids = control_data.get("resource_interval_ids")
    if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes)):
        return ()
    interval_ids = {str(value).strip() for value in raw_ids if str(value).strip()}
    if not interval_ids:
        return ()
    plan, completed_jobs = snapshot or _resource_interval_snapshot(connection, task_row)
    if not isinstance(plan, Mapping):
        return ()
    from unilabos.workflow.resource_lock_plan import retained_resource_interval_ids

    return retained_resource_interval_ids(
        plan, interval_ids, dict(job_row), completed_jobs
    )


def _resource_interval_snapshot(
    connection: sqlite3.Connection, task_row: sqlite3.Row
) -> tuple[Any, list[dict[str, Any]]]:
    """读取同一事务内不随租约释放改变的计划和当前 attempt 状态。"""

    plan = _decode_json_field(task_row["execution_plan"], fallback={})
    completed_jobs = [
        dict(row)
        for row in connection.execute(
            # 历史失败 attempt 不能让新 attempt 的后继被判为 fail-fast 已停止。
            "SELECT workflow_node_uuid, status, meta_data, uncertainty_reason "
            "FROM workflow_node_job WHERE workflow_task_uuid=? "
            "AND current_attempt=1 AND deleted_at IS NULL",
            (task_row["uuid"],),
        )
    ]
    return plan, completed_jobs


def _reconcile_terminal_resource_intervals(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    now: str,
) -> None:
    """按最新 Job 终态重算并释放已经越过完成屏障的持久锁。

    只考察仍持有活跃租约或活跃 claim 的终态作业：``release_execution_locks``
    两个分支都只改 ``reserved/running/uncertain`` 状态的行，对已经全部释放的
    作业是空操作，但为它算保留区间（每个约 30 ms）并不免费。每个作业完成都要
    走一遍本函数，若扫全部终态作业，单次代价随已完成作业数线性增长——pTLC
    5383 节点任务实测 430 个带区间的终态作业耗时 6.8 s，而其中持有活跃租约的
    为 0，这正是相邻设备动作之间秒级调度空档的主要来源。
    """

    task_row = connection.execute(
        "SELECT * FROM workflow_task WHERE uuid=? AND deleted_at IS NULL",
        (task_uuid,),
    ).fetchone()
    if task_row is None:
        raise StoreNotFound(f"任务不存在：{task_uuid}")
    snapshot = None
    for job_row in connection.execute(
        "SELECT j.* FROM workflow_node_job AS j "
        "WHERE j.workflow_task_uuid=? "
        "AND j.status IN ('succeeded','failed','canceled','timeout') "
        "AND COALESCE(j.uncertainty_reason,'')='' AND j.deleted_at IS NULL "
        "AND (EXISTS (SELECT 1 FROM execution_lock_lease AS l "
        "             WHERE l.workflow_node_job_uuid=j.uuid "
        "               AND l.state IN ('reserved','running','uncertain') "
        "               AND l.deleted_at IS NULL) "
        "     OR EXISTS (SELECT 1 FROM execution_claim AS c "
        "                WHERE c.workflow_node_job_uuid=j.uuid "
        "                  AND c.state IN ('reserved','running','uncertain')))",
        (task_uuid,),
    ).fetchall():
        control_data = _decode_json_field(job_row["control_data"], fallback={})
        if not control_data.get("resource_interval_ids"):
            continue
        if snapshot is None:
            snapshot = _resource_interval_snapshot(connection, task_row)
        release_execution_locks(
            connection,
            job_uuid=job_row["uuid"],
            now=now,
            keep_interval_ids=_retained_resource_intervals(
                connection=connection,
                task_row=task_row,
                job_row=job_row,
                control_data=control_data,
                snapshot=snapshot,
            ),
        )


_FINISHED_STATE_MAP = {
    "success": "succeeded",
    "failed": "failed",
    "canceled": "canceled",
    "timeout": "timeout",
}
_WAIT_REASON_IDENTITY_FIELD_BY_SCOPE = {
    "device": "device_id",
    "material": "material_uuid",
    "material_site": "site_uuid",
    "resource": "lock_key",
}
_WAIT_REASON_RESOURCE_FIELDS = (
    "lock_key",
    "device_id",
    "material_uuid",
    "site_uuid",
    "local_device_id",
    "device_name",
    "material_name",
    "site_name",
    "wait_code",
    "wait_message",
)
_WAIT_REASON_ALLOWED_FIELDS = frozenset(
    ("scope", *_WAIT_REASON_RESOURCE_FIELDS)
)
CLEANUP_STATUSES_SETTLEABLE_AFTER_TERMINAL = frozenset(
    {"none", "pending", "required", "requires_attention", "canceling"}
)


def _encode_json_field(value: Any, *, field_name: str) -> str:
    """把一个结果字段编码成稳定 JSON 文本。

    参数：``value`` 是准备持久化的返回或错误信息；``field_name`` 是发生冲突时
    用于定位字段的代码标识。返回：键稳定排序的 JSON 文本。异常：值无法表达为
    JSON 时抛出 ``StoreConflict``，不允许部分状态写入。
    """

    try:
        return encode_json(value, sort_keys=True).decode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StoreConflict(f"{field_name} 不是合法 JSON") from exc


def _decode_json_field(value: str | None, *, fallback: Any) -> Any:
    """把数据库 JSON 文本恢复为领域值。

    参数：``value`` 是数据库字段文本；``fallback`` 是空字段使用的默认值。
    返回：解码后的 JSON 值。异常：损坏的持久化文本异常原样传播，因为它代表
    工作流存储（WorkflowStore）事实已经不可解释。
    """

    if value is None or value == "":
        return fallback
    # 冻结执行计划/工作流快照按内容缓存解码结果（见 frozen_json_cache）。
    return decode_large_json_text(value, _decode_json_text)


def _decode_json_text(value: str) -> Any:
    return decode_json_bytes(value.encode("utf-8"))


def _normalize_wait_reason_resources(
    values: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, str]]:
    """规范仅用于解释门禁等待的设备、物料或库位身份。

    设备选择器除了库存中的设备 Material UUID，还会携带本地设备 ID、展示名
    以及候选设备各自的不可用原因。这些字段属于公开等待诊断合同；保留它们既
    能让前端直接展示具体设备，也避免设备忙碌这一正常调度状态被误判为提交
    失败。未知字段仍然关闭式拒绝，防止任意注册快照数据泄漏进任务投影。
    """

    normalized: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for value in values or ():
        if not isinstance(value, Mapping):
            raise StoreConflict("等待资源必须是对象")
        if set(value) - _WAIT_REASON_ALLOWED_FIELDS:
            raise StoreConflict("等待资源包含未知字段")
        scope = str(value.get("scope") or "").strip()
        identity_field = _WAIT_REASON_IDENTITY_FIELD_BY_SCOPE.get(scope)
        if identity_field is None:
            raise StoreConflict("等待资源 scope 不合法")
        identity = value.get(identity_field)
        if not isinstance(identity, str) or not identity.strip():
            raise StoreConflict(f"等待资源缺少 {identity_field}")
        if scope == "resource" and not is_canonical_generic_resource_lock_key(
            identity.strip()
        ):
            raise StoreConflict("通用等待资源必须使用规范 resource:<UUID> 键")
        resource = {"scope": scope, identity_field: identity.strip()}
        for field in _WAIT_REASON_RESOURCE_FIELDS:
            extra = value.get(field)
            if field == identity_field or extra in (None, ""):
                continue
            if not isinstance(extra, str):
                raise StoreConflict(f"等待资源 {field} 必须是字符串")
            resource[field] = extra.strip()
        key = tuple(sorted(resource.items()))
        if key not in seen:
            normalized.append(resource)
            seen.add(key)
    return normalized


def _normalize_actual_executor(
    value: Mapping[str, Any] | None,
) -> dict[str, str]:
    """校验并规范本轮作业实际绑定的设备执行器快照。

    参数：``value`` 是调度门禁最终选择的本地设备业务 ID 与设备物料 UUID；
    ``None`` 只允许遗留的非设备执行责任省略该快照。返回：只含
    ``local_device_id``、可选 ``material_uuid`` 的稳定字典。异常：字段未知、
    本地设备身份为空或值不是字符串时抛出 ``StoreConflict``，避免结果证据无法
    追溯到真实执行器。
    """

    if value is None:
        return {}
    allowed = {"local_device_id", "material_uuid"}
    unknown = set(value) - allowed
    if unknown:
        raise StoreConflict(
            "actual_executor 包含未知字段：" + ",".join(sorted(unknown))
        )
    local_device_id = value.get("local_device_id")
    material_uuid = value.get("material_uuid", "")
    if not isinstance(local_device_id, str) or not local_device_id.strip():
        raise StoreConflict("actual_executor.local_device_id 必须是非空字符串")
    if not isinstance(material_uuid, str):
        raise StoreConflict("actual_executor.material_uuid 必须是字符串")
    snapshot = {"local_device_id": local_device_id.strip()}
    if material_uuid.strip():
        snapshot["material_uuid"] = material_uuid.strip()
    return snapshot


def _normalize_dispatch_permit(
    value: Mapping[str, Any] | None,
    execution_locks: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    """校验库存权威签发的派发凭据与完整锁集合一致。

    参数：``value`` 是 Claim、Fence、效果身份、参数哈希和预期 ChangeSet；
    ``execution_locks`` 是本次作业准备投影的完整锁请求。返回：规范字段及按锁键
    索引的 Fence；``None`` 只供不越过物理边界的投影单元测试。异常：字段缺失、
    UUID 非法、Fence 重复或锁集合不一致时抛 ``StoreConflict``。
    """

    if value is None:
        return None
    required = {
        "effect_uuid",
        "claim_uuid",
        "parameter_hash",
        "expected_change_set",
        "fences",
    }
    if set(value) != required:
        raise StoreConflict("库存 DispatchPermit 字段不完整")
    for field in ("effect_uuid", "claim_uuid"):
        try:
            UUID(str(value[field]))
        except (AttributeError, TypeError, ValueError) as error:
            raise StoreConflict(f"DispatchPermit.{field} 不是合法 UUID") from error
    parameter_hash = str(value["parameter_hash"] or "").strip()
    if not parameter_hash:
        raise StoreConflict("DispatchPermit.parameter_hash 不能为空")
    expected_change_set = value["expected_change_set"]
    if not isinstance(expected_change_set, Mapping):
        raise StoreConflict("DispatchPermit.expected_change_set 必须是对象")
    raw_fences = value["fences"]
    if not isinstance(raw_fences, Sequence) or isinstance(
        raw_fences,
        (str, bytes, bytearray),
    ):
        raise StoreConflict("DispatchPermit.fences 必须是数组")
    fencing_tokens: dict[str, int] = {}
    for fence in raw_fences:
        if not isinstance(fence, Mapping) or set(fence) != {
            "lock_key",
            "fencing_token",
        }:
            raise StoreConflict("DispatchPermit Fence 字段非法")
        lock_key = str(fence["lock_key"] or "").strip()
        token = fence["fencing_token"]
        if not lock_key or not isinstance(token, int) or token <= 0:
            raise StoreConflict("DispatchPermit Fence 键或令牌非法")
        if lock_key in fencing_tokens:
            raise StoreConflict(f"DispatchPermit Fence 重复：{lock_key}")
        fencing_tokens[lock_key] = token
    execution_keys = {
        str(item.get("lock_key") or "").strip()
        for item in (execution_locks or ())
        if isinstance(item, Mapping)
    }
    if set(fencing_tokens) != execution_keys:
        raise StoreConflict("DispatchPermit Fence 与完整执行锁集合不一致")
    return {
        "effect_uuid": str(value["effect_uuid"]),
        "claim_uuid": str(value["claim_uuid"]),
        "parameter_hash": parameter_hash,
        "expected_change_set": dict(expected_change_set),
        "fencing_tokens": fencing_tokens,
    }


class TaskRuntimeProjection:
    """原子推进工站工作流任务、节点作业、Claim 和物理结算事实。

    本模块是 ``workflow_history.db`` 运行状态的唯一事务投影入口；调度循环只提交
    领域命令，不直接写任务、作业、租约或发件箱表。
    """

    def __init__(self, store: WorkflowStore):
        """绑定唯一工作流存储（WorkflowStore）写权威。

        参数：``store`` 是持有任务和作业标准表的工作流存储。返回：无。
        异常：构造阶段幂等补齐作业证据表；DDL 或存储错误原样传播。
        """

        # ``_store`` 是本投影唯一允许写入的工作流任务（WorkflowTask）权威。
        self._store = store
        self._evidence = JobEvidenceStore(store)
        self._manual_confirmations = ManualConfirmationStore(store)
        self._interventions = WorkflowInterventionStore(store)

    @staticmethod
    def _append_invalidation(
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        now: str,
    ) -> None:
        """在状态事实同一事务内追加一次前端失效通知。"""

        append_frontend_event(
            connection,
            event="workflow.runtime.changed",
            data={"workflow_task_uuid": task_uuid},
            now=now,
        )

    @staticmethod
    def _task_row(
        connection: sqlite3.Connection,
        task_uuid: str,
    ) -> sqlite3.Row:
        """在当前事务读取一个工作流任务（WorkflowTask）数据库行。

        参数：``connection`` 是调用方持有的写事务；``task_uuid`` 是任务稳定身份。
        返回：未软删除的任务行。异常：身份不存在时抛出 ``StoreNotFound``。
        """

        # ``task_row`` 是后续作业状态聚合所属的父任务事实。
        task_row = connection.execute(
            "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
            (task_uuid,),
        ).fetchone()
        if task_row is None:
            raise StoreNotFound(f"工作流任务不存在：{task_uuid}")
        return task_row

    @staticmethod
    def _job_row(
        connection: sqlite3.Connection,
        job_uuid: str,
    ) -> sqlite3.Row:
        """在当前事务读取一个工作流节点作业（WorkflowNodeJob）数据库行。

        参数：``connection`` 是调用方持有的写事务；``job_uuid`` 是作业稳定身份。
        返回：未软删除的作业行。异常：身份不存在时抛出 ``StoreNotFound``。
        """

        # ``job_row`` 是本轮要投影状态或核对重放的目标作业事实。
        job_row = connection.execute(
            "SELECT * FROM workflow_node_job WHERE uuid = ? AND deleted_at IS NULL",
            (job_uuid,),
        ).fetchone()
        if job_row is None:
            raise StoreNotFound(f"工作流节点作业不存在：{job_uuid}")
        return job_row

    @staticmethod
    def _job_rows(
        connection: sqlite3.Connection,
        task_uuid: str,
    ) -> list[sqlite3.Row]:
        """读取父任务拥有的完整工作流节点作业（WorkflowNodeJob）集合。

        参数：``connection`` 是当前事务；``task_uuid`` 是父任务稳定身份。返回：按
        拓扑序和 UUID 稳定排序的作业行。异常：任务没有作业时抛出
        ``StoreConflict``，防止空集合被错误聚合为成功。
        """

        # ``job_rows`` 是决定父任务业务终态的完整兄弟作业集合。
        job_rows = connection.execute(
            """
            SELECT * FROM workflow_node_job
            WHERE workflow_task_uuid = ? AND deleted_at IS NULL
            ORDER BY topological_index ASC, uuid ASC
            """,
            (task_uuid,),
        ).fetchall()
        if not job_rows:
            raise StoreConflict(f"工作流任务没有可投影作业：{task_uuid}")
        return list(job_rows)

    @classmethod
    def _project_ready_output(
        cls,
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        now: str,
    ) -> WorkflowBoundaryProjection:
        """投影就绪的工作流输出边界并写入统一运行与工站事件。"""

        try:
            boundary = project_ready_workflow_output(
                connection,
                task_uuid=task_uuid,
                now=now,
                complete_task=False,
            )
        except WorkflowBoundaryError as error:
            raise StoreConflict(str(error)) from error
        if not boundary.output_changed or boundary.output_job_uuid is None:
            return boundary
        append_runtime_event(
            connection,
            task_uuid=task_uuid,
            job_uuid=boundary.output_job_uuid,
            kind="job_transition",
            from_status="pending",
            to_status="succeeded",
            now=now,
        )
        append_job_state_event(
            connection,
            job_row=cls._job_row(connection, boundary.output_job_uuid),
            status="succeeded",
            details={"return_info": boundary.result or {}},
        )
        return boundary

    @classmethod
    def _resume_task_after_reconciliation(
        cls,
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        now: str,
    ) -> bool:
        """在最后一个不确定作业完成对账后恢复任务控制与清理状态。

        参数：当前事务、父任务身份和统一提交时间。返回：确实关闭整任务对账状态
        时为真，仍有其他不确定作业时为假。异常：任务事实缺失时原样传播。终态
        异常任务恢复为 ``required``，由桥接层完成任务级资源清理后再转 ``settled``。
        """

        if cls._has_unsettled_job_reconciliation(connection, task_uuid=task_uuid):
            return False
        task = cls._task_row(connection, task_uuid)
        # 进程重启会先把任务置为 ``failed/active`` 并保留
        # ``cleanup_status=requires_attention``。当 Edge 随后提交了明确的停止
        # 证明时，不确定作业已经清零，但控制状态不会自动回到
        # ``waiting_reconciliation``；此时仍应进入统一的清理收尾，而不能因为
        # control_status 不是 waiting_reconciliation 而遗留 requires_attention。
        if task["control_status"] != "waiting_reconciliation" and task[
            "cleanup_status"
        ] != "requires_attention":
            return False
        cleanup_status = (
            "required"
            if task["status"] in {"failed", "canceled", "timeout"}
            else "none"
        )
        connection.execute(
            """
            UPDATE workflow_task
            SET control_status = CASE WHEN control_status='error_waiting' THEN 'error_waiting'
                    ELSE COALESCE(reconciliation_resume_control_status, dispatch_gate, 'active') END,
                dispatch_gate = COALESCE(reconciliation_resume_control_status, dispatch_gate, 'active'),
                cleanup_status = ?, attention_reason = NULL,
                reconciliation_resume_control_status = NULL,
                update_time = ?
            WHERE uuid = ? AND deleted_at IS NULL
            """,
            (cleanup_status, now, task_uuid),
        )
        return True

    @staticmethod
    def _has_unsettled_job_reconciliation(
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
    ) -> bool:
        """判断父任务是否仍有需要物理对账的作业。"""

        return (
            connection.execute(
                """
                SELECT 1 FROM workflow_node_job
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                  AND uncertainty_reason IS NOT NULL
                  AND TRIM(uncertainty_reason) != ''
                LIMIT 1
                """,
                (task_uuid,),
            ).fetchone()
            is not None
        )

    def _aggregate(
        self,
        connection: sqlite3.Connection,
        task_uuid: str,
    ) -> dict[str, Any]:
        """在同一事务生成标准任务/作业查询聚合。

        参数：``connection`` 是当前事务；``task_uuid`` 是父任务稳定身份。返回：
        包含公共任务投影和有序作业投影的字典。异常：任务或作业缺失时传播对应
        ``StoreNotFound`` 或 ``StoreConflict``。
        """

        from unilabos.persistence.inspection import query_many

        task_rows, job_rows, confirmation_rows = query_many(
            connection,
            [
                (
                    "SELECT * FROM workflow_task WHERE uuid=? AND deleted_at IS NULL",
                    (task_uuid,),
                ),
                (
                    "SELECT * FROM workflow_node_job WHERE workflow_task_uuid=? AND deleted_at IS NULL "
                    "ORDER BY topological_index ASC, uuid ASC",
                    (task_uuid,),
                ),
                (
                    "SELECT * FROM workflow_manual_confirmation WHERE workflow_task_uuid=? "
                    "ORDER BY opened_at DESC, workflow_node_job_uuid DESC",
                    (task_uuid,),
                ),
            ],
        )
        if not task_rows:
            raise StoreNotFound(f"工作流任务不存在：{task_uuid}")
        if not job_rows:
            raise StoreConflict(f"工作流任务没有可投影作业：{task_uuid}")
        task_row = task_rows[0]
        confirmations = {
            str(row["workflow_node_job_uuid"]): manual_confirmation_projection(row)
            for row in confirmation_rows
        }
        jobs: list[dict[str, Any]] = []
        for row in job_rows:
            job = WorkflowStore._job_row(row)
            confirmation = confirmations.get(job["uuid"])
            if confirmation is not None:
                job["manual_confirmation"] = confirmation
            jobs.append(job)
        return {
            "task": WorkflowStore._task_row(task_row),
            "jobs": jobs,
        }

    def project_submission(
        self,
        task_uuid: str,
        scheduler_state: str,
    ) -> dict[str, Any]:
        """投影本地调度器（EdgeScheduler）的首次接收状态。

        参数：``task_uuid`` 是既有工作流任务（WorkflowTask）身份；
        ``scheduler_state`` 接受 ``pending``、``waiting_for_material``、``running``、单步任务
        的 ``paused``，以及仅本地控制即可到达的 ``success``/``failed``。返回：不
        改写标准状态的任务/作业聚合。异常：未知本地状态或不合法的既有聚合抛出
        ``StoreConflict``；身份缺失抛出 ``StoreNotFound``。
        """

        if scheduler_state not in {
            "pending",
            "waiting_for_material",
            "running",
            "paused",
            "success",
            "failed",
        }:
            raise StoreConflict(f"不支持的本地提交状态：{scheduler_state}")
        with self._store.transaction() as connection:
            # ``aggregate`` 是首次提交后可公开给 Backend-shaped 接口的标准事实。
            aggregate = self._aggregate(connection, task_uuid)
            task_status = aggregate["task"]["status"]
            if aggregate["task"]["control_status"] == "error_waiting" and task_status == "running":
                return aggregate
            job_statuses = {job["status"] for job in aggregate["jobs"]}
            # 协调器会在普通动作提交前完成 MaterialSource 作业，因此暂停的调试
            # 任务可以合法呈现 ``pending task + succeeded sources + pending actions``。
            # 物料来源成功不是物理派发，也不应迫使父任务提前进入 running。
            if task_status in {"pending", "running", "succeeded"} and job_statuses <= (
                _ACTIVE_JOB_STATES | {"succeeded", "skipped"}
            ):
                return aggregate
            if (
                scheduler_state == "failed"
                and task_status == "failed"
                and job_statuses <= (_TERMINAL_JOB_STATES | {"pending"})
            ):
                return aggregate
            raise StoreConflict(
                f"本地提交状态与任务聚合冲突：{task_uuid}/{scheduler_state}"
            )

    def project_trace_context(
        self,
        task_uuid: str,
        trace_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """持久化 Scheduler 创建的工作流根 Trace 身份。

        该投影不改变任务业务状态或更新时间；恢复运行允许更新当前父 span，但不
        允许同一 Task 被改挂到另一个 Trace ID。
        """

        normalized = normalize_trace_context(trace_context)
        if "traceparent" not in normalized:
            raise StoreConflict("工作流任务 Trace Context 缺少合法 traceparent")
        with self._store.transaction() as connection:
            task_row = self._task_row(connection, task_uuid)
            current = normalize_trace_context(
                _decode_json_field(task_row["trace_context"], fallback={})
            )
            current_trace_id = current.get("trace_id")
            if current_trace_id and current_trace_id != normalized["trace_id"]:
                raise StoreConflict(f"工作流任务 Trace ID 不可变：{task_uuid}")
            if current != normalized:
                connection.execute(
                    "UPDATE workflow_task SET trace_context = ? WHERE uuid = ?",
                    (
                        _encode_json_field(normalized, field_name="trace_context"),
                        task_uuid,
                    ),
                )
            return self._aggregate(connection, task_uuid)

    def open_manual_prompt(self, job_uuid: str, config: Mapping[str, Any], opened_at: str) -> str:
        """登记等待许可，不创建执行 Claim、租约、在途状态或资源占用。"""
        with self._store.transaction() as connection:
            job = self._job_row(connection, job_uuid)
            existing = connection.execute("SELECT status FROM workflow_manual_confirmation WHERE workflow_node_job_uuid=?", (job_uuid,)).fetchone()
            if existing is not None:
                return str(existing["status"])
            if job["status"] != "pending" or job["executor_kind"] != "manual_confirm":
                raise StoreConflict("仅未派发的人工确认节点可登记请求")
            confirmation = open_manual_confirmation(connection, job_row=job, config=config, opened_at=opened_at)
            self._append_invalidation(connection, task_uuid=str(job["workflow_task_uuid"]), now=opened_at)
            return str(confirmation["status"])

    def project_manual_confirmation_decision(
        self,
        job_uuid: str,
        *,
        action: str,
        decided_at: str,
    ) -> tuple[dict[str, Any], bool]:
        """以 WorkflowStore 单事务 CAS 决定人工确认并推进同一 Job。"""

        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"approve", "reject"}:
            raise StoreConflict("人工确认 action 必须是 approve 或 reject")
        target = "approved" if normalized_action == "approve" else "rejected"
        reason = (
            "manual_confirmation_approved"
            if normalized_action == "approve"
            else "manual_confirmation_rejected"
        )
        with self._store.transaction() as connection:
            job = self._job_row(connection, job_uuid)
            task_uuid = str(job["workflow_task_uuid"])
            confirmation = connection.execute(
                "SELECT * FROM workflow_manual_confirmation "
                "WHERE workflow_node_job_uuid = ?",
                (job_uuid,),
            ).fetchone()
            if confirmation is None:
                raise StoreConflict("作业不是正在等待的人工确认节点")
            current = str(confirmation["status"])
            if current == target:
                return self._aggregate(connection, task_uuid), False
            if current != "pending":
                raise StoreConflict("人工确认已经由另一项决定关闭")
            if str(confirmation["deadline_at"]) <= decided_at:
                raise StoreConflict("人工确认已经超时")
            executor_kind = str(job["executor_kind"])
            if executor_kind not in {"manual_confirm", "material_io"} or str(
                job["status"]
            ) not in {"running", "pending"}:
                raise StoreConflict("作业不处于人工确认等待阶段")
            changed = connection.execute(
                """
                UPDATE workflow_manual_confirmation
                SET status = ?, decided_at = ?, resolution_reason = ?
                WHERE workflow_node_job_uuid = ? AND status = 'pending'
                """,
                (target, decided_at, reason, job_uuid),
            ).rowcount
            if changed != 1:
                raise StoreConflict("人工确认决定发生并发冲突")
            if normalized_action == "approve":
                if executor_kind == "manual_confirm":
                    changed = connection.execute(
                        """
                        UPDATE workflow_node_job
                        SET status = 'pending', executor_kind = 'device_action',
                            update_time = ?
                        WHERE uuid = ? AND status IN ('running', 'pending')
                          AND executor_kind = 'manual_confirm'
                          AND deleted_at IS NULL
                        """,
                        (decided_at, job_uuid),
                    ).rowcount
                    if changed != 1:
                        raise StoreConflict("人工确认继续设备动作发生并发冲突")
                    append_runtime_event(
                        connection,
                        task_uuid=task_uuid,
                        job_uuid=job_uuid,
                        kind="job_transition",
                        from_status=str(job["status"]),
                        to_status="pending",
                        data={"executor_kind": "device_action"},
                        now=decided_at,
                    )
            append_frontend_event(
                connection,
                event="manual_confirmation.resolved",
                data={"task_uuid": task_uuid, "job_uuid": job_uuid},
                now=decided_at,
            )
            return self._aggregate(connection, task_uuid), True

    def project_manual_confirmation_timeout(
        self,
        job_uuid: str,
        *,
        decided_at: str,
    ) -> tuple[dict[str, Any], bool]:
        """以同一 CAS 赢得人工确认超时；Task Cancel 由桥在提交后执行。"""

        with self._store.transaction() as connection:
            job = self._job_row(connection, job_uuid)
            task_uuid = str(job["workflow_task_uuid"])
            confirmation = connection.execute(
                "SELECT * FROM workflow_manual_confirmation "
                "WHERE workflow_node_job_uuid = ?",
                (job_uuid,),
            ).fetchone()
            if confirmation is None:
                raise StoreConflict("作业不是人工确认节点")
            current = str(confirmation["status"])
            if current == "timed_out":
                return self._aggregate(connection, task_uuid), False
            if current != "pending":
                raise StoreConflict("人工确认已经由另一项决定关闭")
            if str(confirmation["deadline_at"]) > decided_at:
                raise StoreConflict("人工确认尚未到期")
            changed = close_pending_manual_confirmation(
                connection,
                job_uuid=job_uuid,
                status="timed_out",
                decided_at=decided_at,
                resolution_reason="manual_confirmation_timeout",
            )
            if not changed:
                raise StoreConflict("人工确认超时发生并发冲突")
            return self._aggregate(connection, task_uuid), True

    def project_manual_continuation_dispatching(
        self,
        job_uuid: str,
        *,
        dispatched_at: str,
    ) -> dict[str, Any]:
        """把已批准的同一 Job 从 pending 推进到物理派发意图。"""

        with self._store.transaction() as connection:
            job = self._job_row(connection, job_uuid)
            task_uuid = str(job["workflow_task_uuid"])
            confirmation = connection.execute(
                "SELECT status FROM workflow_manual_confirmation "
                "WHERE workflow_node_job_uuid = ?",
                (job_uuid,),
            ).fetchone()
            if confirmation is None or str(confirmation["status"]) != "approved":
                raise StoreConflict("人工确认尚未批准")
            if str(job["executor_kind"]) != "device_action":
                raise StoreConflict("人工确认继续作业执行类型非法")
            if str(job["status"]) in {"dispatched", "running"}:
                return self._aggregate(connection, task_uuid)
            if str(job["status"]) != "pending":
                raise StoreConflict("人工确认继续作业不能派发")
            claim = require_dispatchable_execution_claim(
                connection,
                job_uuid=job_uuid,
            )
            changed = connection.execute(
                "UPDATE workflow_node_job SET status='dispatched', update_time=? "
                "WHERE uuid=? AND status='pending' AND executor_kind='device_action'",
                (dispatched_at, job_uuid),
            ).rowcount
            if changed != 1:
                raise StoreConflict("人工确认继续作业派发发生并发冲突")
            control_data = _decode_json_field(job["control_data"], fallback={})
            append_station_event(
                connection,
                event_type="job.dispatched",
                aggregate_type="workflow_node_job",
                aggregate_uuid=job_uuid,
                source_kind="job_dispatch",
                source_uuid=job_uuid,
                idempotency_key=f"station:{job_uuid}:dispatch:{int(job['attempt'])}",
                payload={
                    "task_uuid": task_uuid,
                    "job_uuid": job_uuid,
                    "workflow_node_uuid": str(job["workflow_node_uuid"]),
                    "attempt": int(job["attempt"]),
                    "command_uuid": str(job["edge_command_uuid"]),
                    "claim_uuid": claim["claim_uuid"],
                    "fences": claim["fences"],
                    "effect_uuid": job["dispatch_effect_uuid"],
                    "parameter_hash": job["dispatch_parameter_hash"],
                    "expected_change_set": _decode_json_field(
                        job["expected_change_set"], fallback={}
                    ),
                    "actual_param": _decode_json_field(job["param"], fallback={}),
                    "actual_executor": dict(control_data or {}).get(
                        "actual_executor", {}
                    ),
                    "execution_locks": dict(control_data or {}).get(
                        "execution_locks", []
                    ),
                    "dispatched_at": dispatched_at,
                },
            )
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="job_transition",
                from_status="pending",
                to_status="dispatched",
                now=dispatched_at,
            )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=dispatched_at,
            )
            return self._aggregate(connection, task_uuid)

    def project_canceled(self, task_uuid: str) -> dict[str, Any]:
        """取消可证明尚未派发的任务；在途任务必须走持久取消状态机。"""

        now = utc_now()
        with self._store.transaction() as connection:
            task_row = self._task_row(connection, task_uuid)
            job_rows = self._job_rows(connection, task_uuid)
            if task_row["status"] == "canceled":
                return self._aggregate(connection, task_uuid)
            if task_row["status"] in {"succeeded", "failed", "timeout"}:
                raise StoreConflict(f"终态任务不能取消：{task_uuid}")
            if any(
                row["status"]
                in {
                    "dispatched",
                    "running",
                    "cancel_requested",
                }
                for row in job_rows
            ):
                raise StoreConflict(f"在途任务不能直接投影为 canceled：{task_uuid}")
            for row in job_rows:
                if row["status"] in _TERMINAL_JOB_STATES:
                    release_execution_locks(
                        connection,
                        job_uuid=str(row["uuid"]),
                        now=now,
                    )
                    continue
                connection.execute(
                    """
                    UPDATE workflow_node_job
                    SET status = 'canceled', wait_reason = '{}',
                        finished_at = ?, update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (now, now, row["uuid"]),
                )
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=str(row["uuid"]),
                    kind="job_transition",
                    from_status=str(row["status"]),
                    to_status="canceled",
                    now=now,
                )
                release_execution_locks(
                    connection,
                    job_uuid=str(row["uuid"]),
                    now=now,
                )
            connection.execute(
                """
                UPDATE workflow_task
                SET status = 'canceled', control_status = 'active', dispatch_gate = 'active',
                    cleanup_status = 'required',
                    wait_reason = '{}', finished_at = ?, update_time = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (now, now, task_uuid),
            )
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                kind="task_transition",
                from_status=str(task_row["status"]),
                to_status="canceled",
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._aggregate(connection, task_uuid)

    def project_cancel_requested(
        self,
        task_uuid: str,
        *,
        command_uuid: str,
        ack_deadline_at: str,
        complete_deadline_at: str,
        reason: str = "task_canceled",
        execution_job_uuid: str | None = None,
    ) -> dict[str, Any]:
        """原子记录 Local 模式取消请求并区分未发送与在途作业。

        参数：``task_uuid`` 是任务身份；``command_uuid`` 是幂等控制命令身份；
        两个截止时间分别限制执行器受理和设备终态等待。返回任务/作业聚合。
        异常：终态冲突或缺少命令身份时抛 ``StoreConflict``。

        pending 作业可证明尚未越过物理边界，因此直接取消并释放预留锁；
        dispatched/running 作业只进入 ``cancel_requested``，继续持有执行占用。
        """

        normalized_command_uuid = str(command_uuid or "").strip()
        if not normalized_command_uuid:
            raise StoreConflict("取消命令 UUID 不能为空")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise StoreConflict("取消原因不能为空")
        canceled_error = _encode_json_field(
            [{"code": normalized_reason}],
            field_name="error_info",
        )
        requested_at = utc_now()
        with self._store.transaction() as connection:
            task_row = self._task_row(connection, task_uuid)
            job_rows = self._job_rows(connection, task_uuid)
            if execution_job_uuid is not None:
                job_rows = [row for row in job_rows if row["uuid"] == execution_job_uuid]
                if not job_rows:
                    raise StoreConflict("停止执行身份不属于指定任务")
            targets_current = execution_job_uuid is None or bool(job_rows[0]["current_attempt"])
            if targets_current and task_row["status"] == "canceled":
                return self._aggregate(connection, task_uuid)
            if targets_current and task_row["status"] in {"succeeded", "failed", "timeout"}:
                raise StoreConflict(f"终态任务不能取消：{task_uuid}")

            in_flight = False
            has_execution_attention = False
            for row in job_rows:
                status = str(row["status"])
                job_uuid = str(row["uuid"])
                # 失败的历史尝试也可能保留物理不确定性，不能被终态分支跳过。
                has_execution_attention = has_execution_attention or bool(
                    str(row["uncertainty_reason"] or "").strip()
                )
                if status in _TERMINAL_JOB_STATES:
                    continue
                if status == "pending":
                    connection.execute(
                        """
                        UPDATE workflow_node_job
                        SET status = 'canceled', wait_reason = '{}',
                            error_info = ?, cancel_command_uuid = ?,
                            finished_at = ?, update_time = ?
                        WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
                        """,
                        (
                            canceled_error,
                            normalized_command_uuid,
                            requested_at,
                            requested_at,
                            job_uuid,
                        ),
                    )
                    release_execution_locks(
                        connection,
                        job_uuid=job_uuid,
                        now=requested_at,
                    )
                    append_runtime_event(
                        connection,
                        task_uuid=task_uuid,
                        job_uuid=job_uuid,
                        kind="job_transition",
                        from_status="pending",
                        to_status="canceled",
                        data={"reason": normalized_reason},
                        now=requested_at,
                    )
                    continue
                if status in {"dispatched", "running"}:
                    confirmation = connection.execute(
                        "SELECT status FROM workflow_manual_confirmation "
                        "WHERE workflow_node_job_uuid = ?",
                        (job_uuid,),
                    ).fetchone()
                    manual_not_sent = (
                        str(row["executor_kind"])
                        in {"manual_confirm", "material_io"}
                        and confirmation is not None
                        and str(confirmation["status"]) != "approved"
                    )
                    if manual_not_sent:
                        changed = connection.execute(
                            """
                            UPDATE workflow_node_job
                            SET status='canceled', wait_reason='{}', error_info=?,
                                cancel_command_uuid=?, finished_at=?, update_time=?
                            WHERE uuid=? AND status IN ('dispatched', 'running')
                              AND executor_kind IN ('manual_confirm','material_io')
                              AND deleted_at IS NULL
                            """,
                            (
                                canceled_error,
                                normalized_command_uuid,
                                requested_at,
                                requested_at,
                                job_uuid,
                            ),
                        ).rowcount
                        if changed != 1:
                            raise StoreConflict(
                                f"人工确认作业取消状态发生并发变化：{job_uuid}"
                            )
                        release_execution_locks(
                            connection,
                            job_uuid=job_uuid,
                            now=requested_at,
                        )
                        close_pending_manual_confirmation(
                            connection,
                            job_uuid=job_uuid,
                            status="canceled",
                            decided_at=requested_at,
                            resolution_reason=normalized_reason,
                        )
                        append_runtime_event(
                            connection,
                            task_uuid=task_uuid,
                            job_uuid=job_uuid,
                            kind="job_transition",
                            from_status=status,
                            to_status="canceled",
                            data={"reason": normalized_reason},
                            now=requested_at,
                        )
                        continue
                    existing_uncertainty = str(row["uncertainty_reason"] or "").strip()
                    has_execution_attention = has_execution_attention or bool(
                        existing_uncertainty
                    )
                    changed = connection.execute(
                        """
                        UPDATE workflow_node_job
                        SET status = 'cancel_requested', cancel_command_uuid = ?,
                            cancel_ack_deadline_at = ?,
                            cancel_complete_deadline_at = ?,
                            cancel_accepted_at = NULL,
                            dispatch_deadline_at = NULL,
                            execution_deadline_at = NULL,
                            uncertainty_reason = ?,
                            wait_reason = '{}', update_time = ?
                        WHERE uuid = ? AND status IN ('dispatched', 'running')
                          AND deleted_at IS NULL
                        """,
                        (
                            normalized_command_uuid,
                            ack_deadline_at,
                            complete_deadline_at,
                            existing_uncertainty or None,
                            requested_at,
                            job_uuid,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise StoreConflict(f"作业取消状态发生并发变化：{job_uuid}")
                    in_flight = True
                    append_runtime_event(
                        connection,
                        task_uuid=task_uuid,
                        job_uuid=job_uuid,
                        kind="job_transition",
                        from_status=status,
                        to_status="cancel_requested",
                        data={"command_uuid": normalized_command_uuid},
                        now=requested_at,
                    )
                    continue
                if status == "cancel_requested":
                    in_flight = True
                    has_execution_attention = has_execution_attention or bool(
                        str(row["uncertainty_reason"] or "").strip()
                    )
                    continue
                raise StoreConflict(f"作业状态不能取消：{job_uuid}/{status}")

            if execution_job_uuid is not None and not job_rows[0]["current_attempt"]:
                self._append_invalidation(connection, task_uuid=task_uuid, now=requested_at)
                return self._aggregate(connection, task_uuid)
            target_status = "canceling" if in_flight else "canceled"
            cleanup_status = (
                "requires_attention"
                if has_execution_attention
                else ("canceling" if in_flight else "required")
            )
            control_status = (
                "waiting_reconciliation" if has_execution_attention else "active"
            )
            finished_at = None if in_flight else requested_at
            connection.execute(
                """
                UPDATE workflow_task
                SET status = ?, cleanup_status = ?, control_status = ?, dispatch_gate = 'active',
                    wait_reason = '{}', finished_at = ?, update_time = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (
                    target_status,
                    cleanup_status,
                    control_status,
                    finished_at,
                    requested_at,
                    task_uuid,
                ),
            )
            if str(task_row["status"]) != target_status:
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status=str(task_row["status"]),
                    to_status=target_status,
                    data={"command_uuid": normalized_command_uuid},
                    now=requested_at,
                )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=requested_at,
            )
            return self._aggregate(connection, task_uuid)

    def project_cancel_accepted(self, job_uuid: str) -> dict[str, Any]:
        """记录本地执行器已接受取消，但继续持有执行占用。

        参数：``job_uuid`` 是在途作业身份。返回任务/作业聚合。异常：作业不是
        ``cancel_requested`` 时抛 ``StoreConflict``；重复受理幂等返回。
        """

        with self._store.transaction() as connection:
            job_row = self._job_row(connection, job_uuid)
            task_uuid = str(job_row["workflow_task_uuid"])
            if job_row["status"] in _TERMINAL_JOB_STATES:
                return self._aggregate(connection, task_uuid)
            if job_row["status"] != "cancel_requested":
                raise StoreConflict(f"作业不能确认取消受理：{job_uuid}")
            if job_row["cancel_accepted_at"] is not None:
                return self._aggregate(connection, task_uuid)
            accepted_at = utc_now()
            changed = connection.execute(
                """
                UPDATE workflow_node_job
                SET cancel_accepted_at = ?, cancel_ack_deadline_at = NULL,
                    update_time = ?
                WHERE uuid = ? AND status = 'cancel_requested'
                  AND cancel_accepted_at IS NULL AND deleted_at IS NULL
                """,
                (accepted_at, accepted_at, job_uuid),
            ).rowcount
            if changed != 1:
                raise StoreConflict(f"作业取消受理发生并发变化：{job_uuid}")
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="job_transition",
                from_status="cancel_requested",
                to_status="cancel_requested",
                data={"cancel_accepted": True},
                now=accepted_at,
            )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=accepted_at,
            )
            return self._aggregate(connection, task_uuid)

    def project_material_source_blocked(
        self,
        task_uuid: str,
        *,
        reason: str = "任务所需物料暂不可用",
        wait_resources_by_node: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        source_node_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """持久化一次受阻的任务物料准入投影。

        参数：``task_uuid`` 是保持待处理的工作流任务（WorkflowTask）身份；
        ``reason`` 是可展示的受阻原因；``wait_resources_by_node`` 按物料来源节点
        保存库存尚不能准入的固定物料身份，自动选料节点使用空数组。返回：写入
        准入尝试和节点级等待原因后的标准任务/作业聚合。异常：任务不为
        ``pending``、没有物料来源
        解析作业（MaterialSourceResolutionJob），或来源作业已离开 ``pending`` 时
        抛出 ``StoreConflict``；身份不存在时传播 ``StoreNotFound``。
        """

        with self._store.transaction() as connection:
            aggregate = self._aggregate(connection, task_uuid)
            if aggregate["task"]["status"] != "pending":
                raise StoreConflict(f"任务不能保持准入受阻：{task_uuid}")
            # ``source_jobs`` 是本次全有或全无准入共同拥有的协调器作业。
            source_jobs = [
                job
                for job in aggregate["jobs"]
                if job.get("executor_kind") == "material_source"
                and bool(job.get("current_attempt", True))
                and (source_node_ids is None or job["workflow_node_uuid"] in source_node_ids)
            ]
            if not source_jobs or any(
                job["status"] != "pending" for job in source_jobs
            ):
                raise StoreConflict(f"物料来源作业不能保持待处理：{task_uuid}")
            source_node_uuids = {str(job["workflow_node_uuid"]) for job in source_jobs}
            raw_resources_by_node = dict(wait_resources_by_node or {})
            unknown_node_uuids = set(raw_resources_by_node) - source_node_uuids
            if unknown_node_uuids:
                raise StoreConflict(
                    "物料等待资源包含未知来源节点："
                    + ",".join(sorted(unknown_node_uuids))
                )
            resources_by_node = {
                node_uuid: _normalize_wait_reason_resources(
                    raw_resources_by_node.get(node_uuid)
                )
                for node_uuid in source_node_uuids
            }
            task_resources = _normalize_wait_reason_resources(
                [
                    resource
                    for job in source_jobs
                    for resource in resources_by_node[str(job["workflow_node_uuid"])]
                ]
            )
            wait_reason: dict[str, Any] = {
                "code": "material_unavailable",
                "message": reason,
            }
            if task_resources:
                wait_reason["resources"] = task_resources
            record_blocked_admission(
                connection,
                task_uuid=task_uuid,
                reason=reason,
                wait_reason=wait_reason,
            )
            now = utc_now()
            for job in source_jobs:
                node_uuid = str(job["workflow_node_uuid"])
                job_wait_reason: dict[str, Any] = {
                    "code": "material_unavailable",
                    "message": reason,
                }
                if resources_by_node[node_uuid]:
                    job_wait_reason["resources"] = resources_by_node[node_uuid]
                connection.execute(
                    "UPDATE workflow_node_job SET wait_reason=?, update_time=? "
                    "WHERE uuid=? AND status='pending'",
                    (
                        _encode_json_field(
                            job_wait_reason,
                            field_name="wait_reason",
                        ),
                        now,
                        str(job["uuid"]),
                    ),
                )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=utc_now(),
            )
            return self._aggregate(connection, task_uuid)

    def project_quantity_inventory_blocked(
        self,
        task_uuid: str,
        *,
        reason: str,
        wait_resources_by_job: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, Any]:
        """持久化仅含数量库存需求任务的可重试受阻事实。"""

        with self._store.transaction() as connection:
            aggregate = self._aggregate(connection, task_uuid)
            if aggregate["task"]["status"] != "pending":
                raise StoreConflict(f"任务不能保持数量库存准入受阻：{task_uuid}")
            pending_jobs = {
                str(job["uuid"]): job
                for job in aggregate["jobs"]
                if job["status"] == "pending"
            }
            if not wait_resources_by_job or set(wait_resources_by_job) - set(
                pending_jobs
            ):
                raise StoreConflict(f"数量库存等待作业集合无效：{task_uuid}")
            normalized_by_job = {
                job_uuid: _normalize_wait_reason_resources(resources)
                for job_uuid, resources in wait_resources_by_job.items()
            }
            task_resources = _normalize_wait_reason_resources(
                [
                    resource
                    for resources in normalized_by_job.values()
                    for resource in resources
                ]
            )
            wait_reason: dict[str, Any] = {
                "code": "quantity_inventory_unavailable",
                "message": reason,
            }
            if task_resources:
                wait_reason["resources"] = task_resources
            record_blocked_admission(
                connection,
                task_uuid=task_uuid,
                reason=reason,
                wait_reason=wait_reason,
            )
            now = utc_now()
            for job_uuid, resources in normalized_by_job.items():
                job_wait_reason: dict[str, Any] = dict(wait_reason)
                if resources:
                    job_wait_reason["resources"] = resources
                connection.execute(
                    "UPDATE workflow_node_job SET wait_reason=?,update_time=? "
                    "WHERE uuid=? AND status='pending'",
                    (
                        _encode_json_field(
                            job_wait_reason,
                            field_name="wait_reason",
                        ),
                        now,
                        job_uuid,
                    ),
                )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._aggregate(connection, task_uuid)

    def project_task_material_admission(self, task_uuid: str) -> dict[str, Any]:
        """记录仅含数量库存需求任务的统一准入成功事实。"""

        with self._store.transaction() as connection:
            aggregate = self._aggregate(connection, task_uuid)
            if aggregate["task"]["status"] not in {"pending", "running"}:
                raise StoreConflict(f"任务不能提交数量库存准入结果：{task_uuid}")
            now = utc_now()
            record_admitted_admission(connection, task_uuid=task_uuid, now=now)
            connection.execute(
                "UPDATE workflow_node_job SET wait_reason='{}',update_time=? "
                "WHERE workflow_task_uuid=? AND status='pending' "
                "AND json_extract(wait_reason,'$.code')="
                "'quantity_inventory_unavailable'",
                (now, task_uuid),
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._aggregate(connection, task_uuid)

    def validate_material_source_context(self, task_uuid: str, plan: Mapping[str, Any]) -> None:
        """在首次及动态来源准入前验证原调用的冻结物料上下文。"""
        from unilabos.workflow.center_boundary import validate_task_material_source_context

        with self._store.transaction() as connection:
            validate_task_material_source_context(connection, task_uuid, plan)

    def bind_inventory_resource_plan(self, task_uuid: str, inventory: Any) -> None:
        """在首次设备派发前原子冻结来源准入及部署资源，不修改运行中计划。"""
        from unilabos.workflow.inventory_resource_plan import bind_inventory_resource_plan

        with self._store.transaction() as connection:
            task = self._task_row(connection, task_uuid)
            plan = _decode_json_field(task["execution_plan"], fallback={})
            if plan.get("inventory_resource_binding") != "pending":
                return
            rows = self._job_rows(connection, task_uuid)
            if any(
                row["executor_kind"] not in {"material_source", "workflow_input", "workflow_output"}
                and row["status"] != "pending"
                for row in rows
            ):
                raise StoreConflict("已执行设备动作的任务不得重新绑定资源计划")
            jobs = [
                {**dict(row), "param": _decode_json_field(row["param"], fallback={}),
                 "return_info": _decode_json_field(row["return_info"], fallback={})}
                for row in rows
            ]
            bound = bind_inventory_resource_plan(
                {"execution_plan": plan, "workflow_snapshot": _decode_json_field(task["workflow_snapshot"], fallback={})},
                jobs, inventory,
            )
            connection.execute(
                "UPDATE workflow_task SET execution_plan = ?, update_time = ? WHERE uuid = ?",
                (_encode_json_field(bound, field_name="execution_plan"), utc_now(), task_uuid),
            )

    def project_material_source_admission(
        self,
        task_uuid: str,
        bindings: Mapping[str, Mapping[str, str | None]],
        *,
        runtime: bool = False,
    ) -> dict[str, Any]:
        """原子提交成功任务物料准入（TaskMaterialAdmission）的逐来源结果。

        参数：``task_uuid`` 是父工作流任务（WorkflowTask）身份；``bindings`` 按
        物料来源节点 UUID 提供已预留的 ``uuid``、``resource_template_uuid``
        与 ``custody_policy``，并可提供 ``site_uuid``、``flow_role``。返回：全部来源
        作业已直接变为 ``succeeded``
        的标准聚合；若没有普通动作，父任务也直接成功。异常：绑定集合、字段、
        状态或重放载荷冲突时抛出 ``StoreConflict``，整笔事务零部分写入。
        """

        if not isinstance(bindings, Mapping):
            raise StoreConflict("物料来源绑定必须是对象")
        # ``normalized_bindings`` 隔离调用方容器并验证有类型物料占位符身份。
        normalized_bindings: dict[str, dict[str, str]] = {}
        # ``material_facts`` 是后端形态契约（Backend-shaped Contract）准入事实；
        # 保管策略必须由
        # 当前准入决策显式提供，禁止投影层猜测领域语义。
        material_facts: dict[str, dict[str, str | None]] = {}
        for node_uuid, raw_binding in bindings.items():
            if not isinstance(raw_binding, Mapping):
                raise StoreConflict("物料来源绑定成员必须是对象")
            material_uuid = str(raw_binding.get("uuid") or "").strip()
            template_uuid = str(raw_binding.get("resource_template_uuid") or "").strip()
            custody_policy = str(raw_binding.get("custody_policy") or "").strip()
            if not node_uuid or not material_uuid or not template_uuid:
                raise StoreConflict("物料来源绑定身份不能为空")
            if not custody_policy:
                raise StoreConflict("物料来源保管策略不能为空")
            if custody_policy not in {member.value for member in MaterialCustodyPolicy}:
                raise StoreConflict("物料来源保管策略不在规范闭集")
            normalized_bindings[str(node_uuid)] = {
                "uuid": material_uuid,
                "resource_template_uuid": template_uuid,
                "custody_policy": custody_policy,
            }
            material_facts[str(node_uuid)] = {
                "material_uuid": material_uuid,
                "resource_template_uuid": template_uuid,
                "site_uuid": str(raw_binding.get("site_uuid") or "").strip() or None,
                "flow_role": str(
                    raw_binding.get("flow_role") or "primary_sample"
                ).strip(),
                "custody_policy": custody_policy,
            }

        with self._store.transaction() as connection:
            task_row = self._task_row(connection, task_uuid)
            job_rows = self._job_rows(connection, task_uuid)
            # ``source_rows`` 必须与成功准入一次提交的完整绑定集合严格相等。
            plan_nodes = _decode_json_field(task_row["execution_plan"], fallback={}).get("nodes", [])
            runtime_nodes = {node["uuid"] for node in plan_nodes if node.get("runtime_material_source")}
            source_rows = [
                row for row in job_rows if row["executor_kind"] == "material_source"
                and bool(row["current_attempt"])
                and (str(row["workflow_node_uuid"]) in normalized_bindings if runtime
                     else str(row["workflow_node_uuid"]) not in runtime_nodes)
            ]
            source_node_uuids = {str(row["workflow_node_uuid"]) for row in source_rows}
            if not source_rows or set(normalized_bindings) != source_node_uuids:
                raise StoreConflict(f"物料来源绑定集合不完整：{task_uuid}")
            if task_row["status"] not in {"pending", "running", "succeeded"}:
                raise StoreConflict(f"任务不能提交物料来源结果：{task_uuid}")

            record_admitted_materials(
                connection,
                task_uuid=task_uuid,
                source_jobs=source_rows,
                bindings=material_facts,
                incremental=runtime,
            )
            self._project_material_binding_params(
                connection,
                task_row=task_row,
                job_rows=job_rows,
                bindings=normalized_bindings,
                incremental=runtime,
            )
            projected_at = utc_now()
            changed = False
            for row in source_rows:
                node_uuid = str(row["workflow_node_uuid"])
                return_info = {"material": normalized_bindings[node_uuid]}
                if runtime:
                    return_info["sufficient"] = True
                return_info_json = _encode_json_field(
                    return_info,
                    field_name="return_info",
                )
                if row["status"] == "succeeded":
                    if (
                        _decode_json_field(row["return_info"], fallback={})
                        != return_info
                    ):
                        raise StoreConflict(f"物料来源终态载荷冲突：{row['uuid']}")
                    continue
                if row["status"] != "pending":
                    raise StoreConflict(f"物料来源作业不能成功：{row['uuid']}")
                updated_jobs = connection.execute(
                    """
                    UPDATE workflow_node_job
                    SET status = 'succeeded', return_info = ?, error_info = '[]',
                        wait_reason = '{}', finished_at = ?, update_time = ?
                    WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
                    """,
                    (return_info_json, projected_at, projected_at, row["uuid"]),
                ).rowcount
                if updated_jobs != 1:
                    raise StoreConflict(f"物料来源作业状态发生并发变化：{row['uuid']}")
                changed = True
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=str(row["uuid"]),
                    kind="job_transition",
                    from_status="pending",
                    to_status="succeeded",
                    now=projected_at,
                )

            self._project_ready_output(
                connection,
                task_uuid=task_uuid,
                now=projected_at,
            )

            # 没有普通动作表示任务业务目标就是完成供料绑定；协调器工作不经历
            # ``running``，也不产生设备执行开始时间。
            ordinary_rows = [
                row
                for row in self._job_rows(connection, task_uuid)
                if row["executor_kind"]
                not in {"material_source", "workflow_input", "workflow_output"}
            ]
            if not ordinary_rows and task_row["status"] == "pending":
                updated_tasks = connection.execute(
                    """
                    UPDATE workflow_task
                    SET status = 'succeeded', finished_at = ?, update_time = ?
                    WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
                    """,
                    (projected_at, projected_at, task_uuid),
                ).rowcount
                if updated_tasks != 1:
                    raise StoreConflict(f"来源任务终态发生并发变化：{task_uuid}")
                changed = True
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status="pending",
                    to_status="succeeded",
                    now=projected_at,
                )
            if changed:
                self._append_invalidation(
                    connection,
                    task_uuid=task_uuid,
                    now=projected_at,
                )
            return self._aggregate(connection, task_uuid)

    def project_material_shortage(self, job_uuid: str, *, reason: str) -> dict[str, Any]:
        """把可处理缺料作为成功检查结果持久化，交给条件节点决定后续路径。"""
        with self._store.transaction() as connection:
            row = self._job_row(connection, job_uuid)
            if row["executor_kind"] != "material_source":
                raise StoreConflict("缺料结果只允许写入物料来源作业")
            task_uuid = str(row["workflow_task_uuid"])
            now = utc_now()
            self._project_local_job_terminal(
                connection, task_uuid=task_uuid, job_row=row,
                target_status="succeeded",
                return_info={"sufficient": False, "material": None, "reason": reason},
                error_info=[], now=now, require_condition=False,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._aggregate(connection, task_uuid)

    def get_material_admission(self, task_uuid: str) -> dict[str, Any] | None:
        """读取一个任务最近的物料准入事实。

        参数：``task_uuid`` 是稳定工作流任务（WorkflowTask）身份。返回：尚未评估
        时为 ``None``，否则返回状态、尝试号、修订和原因。异常：数据库错误原样
        传播；本方法只读，不改变准入或库存状态。
        """

        with self._store.transaction() as connection:
            self._task_row(connection, task_uuid)
            return read_material_admission(connection, task_uuid=task_uuid)

    def list_blocked_material_tasks(self) -> list[str]:
        """列出重启后仍应重试物料准入的任务。

        参数：无。返回：按最近评估时间稳定排序的待处理任务 UUID。异常：数据库
        读取错误原样传播；列表只包含持久 ``blocked`` 且任务仍为 ``pending`` 的
        任务，不依赖桥接层内存集合。
        """

        with self._store.transaction() as connection:
            return list_blocked_material_task_uuids(connection)

    def project_cleanup_settled(self, task_uuid: str) -> dict[str, Any]:
        """记录终态任务的物理清理已经结算。

        参数：``task_uuid`` 是已终止的工作流任务（WorkflowTask）身份。返回：更新
        后的任务/作业聚合。异常：任务尚非失败、取消或超时时抛 ``StoreConflict``；
        重放 ``settled`` 幂等返回。该状态转换会触发独占任务物料 claim 的释放。
        """

        with self._store.transaction() as connection:
            task_row = self._task_row(connection, task_uuid)
            if task_row["status"] not in {"failed", "canceled", "timeout"}:
                raise StoreConflict(f"非异常终态任务不能结算清理：{task_uuid}")
            if task_row["cleanup_status"] == "settled":
                return self._aggregate(connection, task_uuid)
            if task_row["cleanup_status"] not in CLEANUP_STATUSES_SETTLEABLE_AFTER_TERMINAL:
                raise StoreConflict(f"任务清理状态不能结算：{task_uuid}")
            if self._has_unsettled_job_reconciliation(
                connection,
                task_uuid=task_uuid,
            ):
                raise StoreConflict(f"任务仍有作业等待物理对账：{task_uuid}")
            if active_task_device_tenancies(connection, task_uuid=task_uuid):
                raise StoreConflict(f"任务仍有活动设备托管：{task_uuid}")
            # 确定停止不等于物理交接完成；仍持料的区间不能被终态清理批量释放。
            retained_intervals = any(
                _retained_resource_intervals(
                    connection=connection,
                    task_row=task_row,
                    job_row=job,
                    control_data=_decode_json_field(job["control_data"], fallback={}),
                )
                for job in self._job_rows(connection, task_uuid)
            )
            if retained_intervals:
                connection.execute(
                    "UPDATE workflow_task SET cleanup_status='requires_attention', "
                    "attention_reason='resource_handoff_required', update_time=? WHERE uuid=?",
                    (utc_now(), task_uuid),
                )
                if task_row["attention_reason"] != "resource_handoff_required":
                    append_runtime_event(
                        connection,
                        task_uuid=task_uuid,
                        kind="uncertainty_opened",
                        now=utc_now(),
                        data={"reason": "resource_handoff_required"},
                    )
                return self._aggregate(connection, task_uuid)
            settled_at = utc_now()
            release_task_execution_locks(
                connection,
                task_uuid=task_uuid,
                now=settled_at,
            )
            connection.execute(
                """
                UPDATE workflow_task
                SET cleanup_status = 'settled', update_time = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (settled_at, task_uuid),
            )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=settled_at,
            )
            return self._aggregate(connection, task_uuid)

    def release_operator_confirmed_task_resources(
        self,
        task_uuid: str,
        *,
        command_uuid: str,
        reason: str,
    ) -> dict[str, Any]:
        """在操作员完成现场核对后释放异常终态 Task 的全部占用。

        参数：Task/Command UUID 已由应用服务校验；``reason`` 是操作员
        留下的现场处置说明。返回清理状态及各类已释放事实数量。
        从未越过物理边界的 pending Job 可随异常 Task 一并清理；仍处于
        dispatched/running 的 Job 会失败关闭。SQLite 故障使整笔工作流库事务回滚。
        """

        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise StoreConflict("人工释放资源必须填写现场处置说明")
        with self._store.transaction() as connection:
            task_row = self._task_row(connection, task_uuid)
            if task_row["status"] not in {"failed", "canceled", "timeout"}:
                raise StoreConflict("只有异常终态任务才能人工释放全部资源")
            active_job = connection.execute(
                """
                SELECT uuid FROM workflow_node_job
                WHERE workflow_task_uuid=? AND deleted_at IS NULL
                  AND status IN ('dispatched','running')
                ORDER BY topological_index, uuid LIMIT 1
                """,
                (task_uuid,),
            ).fetchone()
            if active_job is not None:
                raise StoreConflict(
                    "任务仍有已派发或运行中的作业，不能人工释放全部资源"
                )

            def active_count(table: str, predicate: str) -> int:
                row = connection.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE workflow_task_uuid=? AND {predicate}",
                    (task_uuid,),
                ).fetchone()
                return int(row[0]) if row is not None else 0

            released = {
                "device_tenancies": active_count(
                    "task_device_tenancy", "state='active'"
                ),
                "execution_claims": active_count(
                    "execution_claim",
                    "state IN ('reserved','running','uncertain')",
                ),
                "execution_locks": active_count(
                    "execution_lock_lease",
                    "state IN ('reserved','running','uncertain') "
                    "AND deleted_at IS NULL",
                ),
                "task_material_claims": active_count(
                    "workflow_task_material_claim",
                    "status='active' AND deleted_at IS NULL",
                ),
            }
            now = utc_now()
            connection.execute(
                """
                UPDATE task_device_tenancy
                SET state='released',
                    released_by_job_uuid=acquired_by_job_uuid,
                    released_at=?, update_time=?
                WHERE workflow_task_uuid=? AND state='active'
                """,
                (now, now, task_uuid),
            )
            connection.execute(
                """
                UPDATE job_device_tenancy_transition
                SET status='settled', settled_at=COALESCE(settled_at, ?),
                    update_time=?
                WHERE workflow_task_uuid=? AND status IN ('prepared','retained')
                """,
                (now, now, task_uuid),
            )
            release_task_execution_locks(
                connection,
                task_uuid=task_uuid,
                now=now,
            )
            connection.execute(
                """
                UPDATE workflow_task_material_claim
                SET status='released', released_at=?, revision=revision+1,
                    update_time=?
                WHERE workflow_task_uuid=? AND status='active'
                  AND deleted_at IS NULL
                """,
                (now, now, task_uuid),
            )
            connection.execute(
                """
                UPDATE workflow_node_job
                SET uncertainty_reason=NULL, update_time=?
                WHERE workflow_task_uuid=? AND deleted_at IS NULL
                  AND uncertainty_reason IS NOT NULL
                """,
                (now, task_uuid),
            )
            connection.execute(
                """
                UPDATE workflow_task
                SET cleanup_status='settled', control_status='active', dispatch_gate='active',
                    attention_reason=NULL,
                    reconciliation_resume_control_status=NULL,
                    wait_reason='{}', update_time=?
                WHERE uuid=? AND deleted_at IS NULL
                """,
                (now, task_uuid),
            )
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                command_uuid=command_uuid,
                kind="lock_operator_released",
                from_status=str(task_row["cleanup_status"]),
                to_status="settled",
                data={
                    "reason": normalized_reason,
                    "physical_settlement_confirmed": True,
                    "released": released,
                },
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return {"cleanup_status": "settled", "released": released}

    @staticmethod
    def _project_material_binding_params(
        connection: sqlite3.Connection,
        *,
        task_row: sqlite3.Row,
        job_rows: Sequence[sqlite3.Row],
        bindings: Mapping[str, Mapping[str, str]],
        incremental: bool = False,
    ) -> None:
        """把自动库存选择结果原子写入既有普通动作作业参数。

        参数：连接、任务行和作业行属于同一工作流存储（WorkflowStore）事务；
        ``bindings`` 是已整组占用的逐来源物料（Material）身份。返回无。异常：
        计划目标、作业状态或既有参数冲突时抛 ``StoreConflict``，来源成功状态与
        参数写入一起回滚。
        """

        plan = _decode_json_field(task_row["execution_plan"], fallback={})
        # 早期兼容任务没有冻结绑定目标；它们仍只投影来源结果，不补写动作参数。
        if plan == {}:
            return
        raw_nodes = plan.get("nodes") if isinstance(plan, Mapping) else None
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
            raise StoreConflict("执行计划节点必须是数组")
        jobs_by_node = {str(row["workflow_node_uuid"]): row for row in job_rows}
        raw_edges = plan.get("edges", [])
        if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, (str, bytes)):
            raise StoreConflict("执行计划边必须是数组")
        inferred_targets: dict[str, list[dict[str, str]]] = {}
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, Mapping):
                raise StoreConflict("执行计划边必须是对象")
            if (
                raw_edge.get("dependency_only") is True
                or raw_edge.get("source_type") != "ResourceSlot"
                or raw_edge.get("target_type") != "ResourceSlot"
            ):
                continue
            source_uuid = str(raw_edge.get("source_node_uuid") or "").strip()
            target_uuid = str(raw_edge.get("target_node_uuid") or "").strip()
            param_key = final_target_data_key(
                str(raw_edge.get("target_data_key") or "")
            )
            if source_uuid and target_uuid and param_key:
                inferred_targets.setdefault(source_uuid, []).append(
                    {"workflow_node_uuid": target_uuid, "param_key": param_key}
                )
        claimed_targets: set[tuple[str, str]] = set()
        for raw_node in raw_nodes:
            if (
                not isinstance(raw_node, Mapping)
                or raw_node.get("kind") != "material_source"
            ):
                continue
            source_uuid = str(raw_node.get("uuid") or "")
            binding = bindings.get(source_uuid)
            if binding is None:
                if incremental or raw_node.get("runtime_material_source"):
                    continue
                raise StoreConflict(f"物料来源缺少运行绑定：{source_uuid}")
            raw_targets = raw_node.get("material_binding_targets", [])
            if not isinstance(raw_targets, Sequence) or isinstance(
                raw_targets, (str, bytes)
            ):
                raise StoreConflict("物料来源绑定目标必须是数组")
            combined_targets = [*raw_targets, *inferred_targets.get(source_uuid, [])]
            for raw_target in combined_targets:
                if not isinstance(raw_target, Mapping):
                    raise StoreConflict("物料来源绑定目标必须是对象")
                target_uuid = str(raw_target.get("workflow_node_uuid") or "").strip()
                param_key = str(raw_target.get("param_key") or "").strip()
                target = (target_uuid, param_key)
                if not target_uuid or not param_key:
                    raise StoreConflict("物料来源绑定目标不能为空")
                if target in claimed_targets:
                    continue
                claimed_targets.add(target)
                target_row = jobs_by_node.get(target_uuid)
                if target_row is None:
                    raise StoreConflict(f"物料来源绑定目标不是普通动作：{target_uuid}")
                if target_row["executor_kind"] == "material_source":
                    # 来源→来源的 ResourceSlot（通常是 mount）由子级
                    # runtime_mount_binding 在该来源准入时读取上游 return_info，
                    # 不能在父级成功时把选中物料写成子级作业参数。
                    continue
                # 多个物料来源（MaterialSource）可以把不同参数绑定到同一个动作。
                # ``jobs_by_node`` 来自事务开始时的快照；每次写入前重新读取目标，
                # 否则后一个来源会用陈旧参数覆盖前一个来源刚提交的绑定。
                current_target_row = TaskRuntimeProjection._job_row(
                    connection,
                    str(target_row["uuid"]),
                )
                param = _decode_json_field(current_target_row["param"], fallback={})
                if not isinstance(param, Mapping):
                    raise StoreConflict(
                        f"工作流节点作业参数不是对象：{current_target_row['uuid']}"
                    )
                updated_param = dict(param)
                material_reference = {"uuid": str(binding["uuid"])}
                existing = updated_param.get(param_key)
                if existing is not None and existing != material_reference:
                    raise StoreConflict(
                        f"物料来源绑定与作业参数冲突：{current_target_row['uuid']}"
                    )
                if existing == material_reference:
                    continue
                updated_param[param_key] = material_reference
                changed = connection.execute(
                    "UPDATE workflow_node_job SET param = ?, update_time = ? "
                    "WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL",
                    (
                        _encode_json_field(updated_param, field_name="param"),
                        utc_now(),
                        current_target_row["uuid"],
                    ),
                ).rowcount
                if changed != 1:
                    raise StoreConflict(
                        "物料来源绑定目标状态发生并发变化："
                        f"{current_target_row['uuid']}"
                    )

    def project_pre_dispatch(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        resolved_param: Mapping[str, Any] | None = None,
        execution_locks: Sequence[Mapping[str, Any]] | None = None,
        device_tenancy: Mapping[str, Any] | None = None,
        required_device_tenancy: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        native_operate_condition: Mapping[str, str] | Sequence[Mapping[str, str]] | None = None,
        inventory_authority: Any = None,
        actual_executor: Mapping[str, Any] | None = None,
        dispatch_permit: Mapping[str, Any] | None = None,
        dispatch_payload: Mapping[str, Any] | None = None,
        resource_plan_id: str | None = None,
        resource_interval_ids: Sequence[str] = (),
        resource_acquire_set_id: str | None = None,
        resource_interval_ids_by_lock: Mapping[str, Sequence[str]] | None = None,
        preheld_lock_keys: Sequence[str] | None = None,
        preheld_job_uuids: Sequence[str] | None = None,
        manual_confirmation_config: Mapping[str, Any] | None = None,
        projected_at: str | None = None,
        max_active_tasks: int = 500,
        max_tasks_per_workflow: int = 100,
        max_in_flight_jobs: int = 100,
        aging_interval_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """在物理派发前原子推进目标作业及父任务。

        参数：``task_uuid`` 是父工作流任务（WorkflowTask）身份；``job_uuid`` 是
        即将派发的工作流节点作业（WorkflowNodeJob）身份；``resolved_param``
        是已投影全部父节点输出的最终参数；``execution_locks`` 是设备、物料与
        库位的全有或全无持久占用请求；``device_tenancy`` 是装载开始前取得、
        成功卸载后释放的主物料设备托管转换；``actual_executor`` 是本轮动态或固定
        选择得到的本地设备业务 ID 与设备物料 UUID；``dispatch_permit`` 是库存
        权威签发的 Claim、Fence、效果身份、最终参数哈希和预期 ChangeSet。返回：提交后的标准聚合；锁暂
        不可用时 Job 保持 ``pending`` 并写入 ``wait_reason``。原位操作通过
        ``required_device_tenancy`` 证明同一 Task 正长期托管实际设备内的物料。异常：身份不匹配、
        实际执行器合同损坏或状态转换冲突时抛出 ``StoreConflict``；身份缺失时抛出
        ``StoreNotFound``。同一派发意图重放时零写入。
        """

        with self._store.transaction() as connection:
            # ``job_row`` 是本次物理派发意图所指向的唯一作业。
            job_row = self._job_row(connection, job_uuid)
            if job_row["workflow_task_uuid"] != task_uuid:
                raise StoreConflict(f"作业不属于指定任务：{job_uuid}/{task_uuid}")
            task_row = self._task_row(connection, task_uuid)
            is_manual_confirmation = str(job_row["executor_kind"]) in {
                "manual_confirm",
                "material_io",
            }
            if (
                job_row["status"]
                == ("running" if is_manual_confirmation else "dispatched")
                and task_row["status"] == "running"
            ):
                return self._aggregate(connection, task_uuid)
            if job_row["status"] != "pending":
                raise StoreConflict(f"作业不能进入 dispatched：{job_uuid}")
            if task_row["status"] not in {"pending", "running"}:
                raise StoreConflict(f"任务不能开始派发：{task_uuid}")
            # 全局在途 Job 容量在激活父任务之前裁决。容量已满时，本次作业尚未
            # 越过任何资源或物理边界，父任务必须继续保持 pending，进程重启后
            # 才能按原 Task/Job 身份安全恢复，而不是被误判为活动 DAG 中断。
            dispatch_capacity = admit_job_dispatch(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                max_in_flight_jobs=max_in_flight_jobs,
            )
            if not dispatch_capacity.available:
                self._append_invalidation(
                    connection,
                    task_uuid=task_uuid,
                    now=utc_now(),
                )
                return self._aggregate(connection, task_uuid)
            if task_row["status"] == "pending":
                capacity = check_workflow_task_capacity(
                    connection,
                    task_uuid=task_uuid,
                    max_active_tasks=max_active_tasks,
                    max_tasks_per_workflow=max_tasks_per_workflow,
                )
                if not capacity.available:
                    self._append_invalidation(
                        connection,
                        task_uuid=task_uuid,
                        now=utc_now(),
                    )
                    return self._aggregate(connection, task_uuid)
            tenancy_decision = check_device_tenancy(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                transition=device_tenancy,
            )
            if not tenancy_decision.acquired:
                record_execution_lock_wait(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    requests=execution_locks,
                    blocking_task_uuid=tenancy_decision.blocking_task_uuid,
                    blocking_job_uuid=tenancy_decision.blocking_job_uuid,
                )
                self._append_invalidation(
                    connection,
                    task_uuid=task_uuid,
                    now=utc_now(),
                )
                return self._aggregate(connection, task_uuid)
            permit = _normalize_dispatch_permit(dispatch_permit, execution_locks)
            native_custody = False
            if native_operate_condition is not None and required_device_tenancy is not None:
                native_custody = validate_native_operate_custody(
                    task=dict(task_row),
                    job=dict(job_row),
                    requirement=required_device_tenancy,
                    condition=native_operate_condition,
                    resolved_param=resolved_param or {},
                    permit=permit,
                    authority=inventory_authority,
                    plan_id=resource_plan_id,
                    interval_ids=resource_interval_ids,
                )
            if not native_custody:
                requirements = (
                    required_device_tenancy
                    if isinstance(required_device_tenancy, (list, tuple))
                    else [required_device_tenancy]
                )
                for requirement in requirements:
                    require_active_device_tenancy(connection, task_uuid=task_uuid, requirement=requirement)
            if permit is not None:
                lock_decision = mirror_execution_locks_from_permit(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    requests=execution_locks,
                    claim_uuid=permit["claim_uuid"],
                    fencing_tokens=permit["fencing_tokens"],
                    resource_plan_id=resource_plan_id,
                    resource_interval_ids=resource_interval_ids,
                    resource_acquire_set_id=resource_acquire_set_id,
                    resource_interval_ids_by_lock=resource_interval_ids_by_lock,
                    preheld_lock_keys=preheld_lock_keys,
                    preheld_job_uuids=preheld_job_uuids,
                )
            else:
                lock_decision = try_acquire_execution_locks(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    requests=execution_locks,
                    resource_plan_id=resource_plan_id,
                    resource_interval_ids=resource_interval_ids,
                    resource_acquire_set_id=resource_acquire_set_id,
                    resource_interval_ids_by_lock=resource_interval_ids_by_lock,
                    preheld_lock_keys=preheld_lock_keys,
                    preheld_job_uuids=preheld_job_uuids,
                    aging_interval_seconds=aging_interval_seconds,
                )
            if not lock_decision.acquired:
                self._append_invalidation(
                    connection,
                    task_uuid=task_uuid,
                    now=utc_now(),
                )
                return self._aggregate(connection, task_uuid)
            # 只有设备托管和完整执行锁都已准入，才领取父 Task 的运行容量。
            # 前面的容量门禁在同一个写事务内完成，因此这里不会在拿到锁后
            # 意外发现容量已满；这样锁等待/设备等待都不会把父任务提前激活。
            if task_row["status"] == "pending":
                activation = activate_workflow_task(
                    connection,
                    task_uuid=task_uuid,
                    max_active_tasks=max_active_tasks,
                    max_tasks_per_workflow=max_tasks_per_workflow,
                )
                if not activation.available:
                    # SQLite 写事务在本地串行化；这是防御性分支，正常不会触发。
                    # 若未来容量策略发生变化，不能留下属于 pending Job 的活动锁。
                    release_execution_locks(
                        connection,
                        job_uuid=job_uuid,
                        now=utc_now(),
                    )
                    self._append_invalidation(
                        connection,
                        task_uuid=task_uuid,
                        now=utc_now(),
                    )
                    return self._aggregate(connection, task_uuid)
                task_row = self._task_row(connection, task_uuid)
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status="pending",
                    to_status="running",
                    now=utc_now(),
                )
                append_task_state_event(
                    connection,
                    task_uuid=task_uuid,
                    status="running",
                    details={
                        "started_at": str(task_row["started_at"] or utc_now()),
                    },
                )
            prepare_device_tenancy(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                transition=device_tenancy,
            )
            param_json = (
                job_row["param"]
                if resolved_param is None
                else _encode_json_field(resolved_param, field_name="resolved_param")
            )

            # ``projected_at`` 是同一事务内任务与作业共享的投影时间。
            projected_at = projected_at or utc_now()
            projected_job_status = (
                "running" if is_manual_confirmation else "dispatched"
            )
            dispatch_command_uuid = str(job_row["edge_command_uuid"] or uuid4())
            updated_jobs = connection.execute(
                """
                UPDATE workflow_node_job
                SET status = ?, param = ?, edge_command_uuid = ?,
                    dispatch_effect_uuid = ?, dispatch_parameter_hash = ?,
                    expected_change_set = ?, update_time = ?
                WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
                """,
                (
                    projected_job_status,
                    param_json,
                    dispatch_command_uuid,
                    permit["effect_uuid"] if permit else None,
                    permit["parameter_hash"] if permit else None,
                    _encode_json_field(
                        permit["expected_change_set"] if permit else {},
                        field_name="expected_change_set",
                    ),
                    projected_at,
                    job_uuid,
                ),
            ).rowcount
            if updated_jobs != 1:
                raise StoreConflict(f"作业派发前状态发生并发变化：{job_uuid}")
            actual_param = (
                _decode_json_field(param_json, fallback={})
                if isinstance(param_json, str)
                else dict(resolved_param or {})
            )
            actual_executor_snapshot = _normalize_actual_executor(actual_executor)
            control_data = _decode_json_field(job_row["control_data"], fallback={})
            if not isinstance(control_data, Mapping):
                raise StoreConflict(f"作业 control_data 已损坏：{job_uuid}")
            updated_control_data = dict(control_data)
            if dispatch_payload is not None:
                # 与作业派发意图同事务冻结完整重放载荷，覆盖跨进程出站前崩溃窗口。
                claim = permit if permit else get_execution_claim(connection, job_uuid=job_uuid)
                updated_control_data["dispatch_payload"] = {
                    **dict(dispatch_payload), "action_args": actual_param,
                    "attempt": int(job_row["attempt"]), "command_uuid": dispatch_command_uuid,
                    "claim_uuid": claim["claim_uuid"] if claim else None,
                    "fences": ([{"lock_key": key, "fencing_token": value}
                        for key, value in sorted(permit["fencing_tokens"].items())] if permit else (claim["fences"] if claim else [])),
                    "effect_uuid": permit["effect_uuid"] if permit else "",
                    "parameter_hash": permit["parameter_hash"] if permit else "",
                    "expected_change_set": permit["expected_change_set"] if permit else {},
                }
            if actual_executor_snapshot:
                updated_control_data["actual_executor"] = actual_executor_snapshot
            updated_control_data["execution_locks"] = [
                dict(request) for request in (execution_locks or ())
            ]
            updated_control_data["resource_plan_id"] = str(resource_plan_id or "")
            updated_control_data["resource_interval_ids"] = [
                str(value) for value in resource_interval_ids
            ]
            updated_control_data["resource_acquire_set_id"] = str(
                resource_acquire_set_id or ""
            )
            updated_control_data["resource_interval_ids_by_lock"] = {
                str(lock_key): [str(value) for value in values]
                for lock_key, values in (resource_interval_ids_by_lock or {}).items()
            }
            if permit is not None:
                # WorkflowStore 的活动 Lease 键保持全局唯一；显式共享 scope 因而
                # 只保存一条物理占用行。每个 Job 仍必须持久化 Inventory Permit
                # 的完整 Fence 快照，供派发载荷、重放与事后审计使用。
                updated_control_data["dispatch_fences"] = [
                    {
                        "lock_key": lock_key,
                        "fencing_token": token,
                    }
                    for lock_key, token in sorted(permit["fencing_tokens"].items())
                ]
                updated_control_data["dispatch_preheld_lock_keys"] = sorted(
                    {
                        str(value).strip()
                        for value in (preheld_lock_keys or ())
                        if str(value).strip()
                    }
                )
                updated_control_data["dispatch_preheld_job_uuids"] = sorted(
                    {
                        str(value).strip()
                        for value in (preheld_job_uuids or ())
                        if str(value).strip()
                    }
                )
            connection.execute(
                """
                UPDATE workflow_node_job
                SET control_data = ?, update_time = ?
                WHERE uuid = ? AND status = ?
                  AND deleted_at IS NULL
                """,
                (
                    _encode_json_field(
                        updated_control_data,
                        field_name="control_data",
                    ),
                    projected_at,
                    job_uuid,
                    projected_job_status,
                ),
            )
            claim = get_execution_claim(connection, job_uuid=job_uuid)
            if claim is None:
                raise StoreConflict(f"作业派发意图缺少 Claim：{job_uuid}")
            if not is_manual_confirmation:
                append_station_event(
                    connection,
                    event_type="job.dispatched",
                    aggregate_type="workflow_node_job",
                    aggregate_uuid=job_uuid,
                    source_kind="job_dispatch",
                    source_uuid=job_uuid,
                    idempotency_key=(
                        f"station:{job_uuid}:dispatch:{int(job_row['attempt'])}"
                    ),
                    payload={
                        "task_uuid": task_uuid,
                        "job_uuid": job_uuid,
                        "workflow_node_uuid": str(job_row["workflow_node_uuid"]),
                        "attempt": int(job_row["attempt"]),
                        "command_uuid": dispatch_command_uuid,
                        "claim_uuid": claim["claim_uuid"],
                        "fences": claim["fences"],
                        "effect_uuid": permit["effect_uuid"] if permit else None,
                        "parameter_hash": permit["parameter_hash"] if permit else None,
                        "expected_change_set": (
                            permit["expected_change_set"] if permit else {}
                        ),
                        "actual_param": actual_param,
                        "actual_executor": actual_executor_snapshot,
                        "execution_locks": [
                            dict(request) for request in (execution_locks or ())
                        ],
                        "device_tenancy": dict(device_tenancy or {}),
                        "dispatched_at": projected_at,
                    },
                )
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="job_transition",
                from_status="pending",
                to_status=projected_job_status,
                now=projected_at,
            )
            if is_manual_confirmation:
                open_manual_confirmation(
                    connection,
                    job_row=job_row,
                    config=manual_confirmation_config,
                    opened_at=projected_at,
                )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=projected_at,
            )
            return self._aggregate(connection, task_uuid)

    def project_execution_lock_wait(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        execution_locks: Sequence[Mapping[str, Any]] | None,
        blocking_task_uuid: str | None = None,
        blocking_job_uuid: str | None = None,
        wait_code: str | None = None,
        wait_message: str | None = None,
        wait_resources: Sequence[Mapping[str, Any]] | None = None,
        max_active_tasks: int = 500,
        max_tasks_per_workflow: int = 100,
        include_aggregate: bool = True,
    ) -> dict[str, Any] | None:
        """持久化具体资源冲突或尚未绑定具体资源的门禁等待。

        参数：``task_uuid``、``job_uuid`` 是等待任务和作业身份；
        ``execution_locks`` 是已经解析为具体设备/物料/库位的请求；两个阻塞身份
        指向当前持有者；``wait_code``、``wait_message`` 用于没有具体 Claim 的设备
        容量或库位候选等待；``wait_resources`` 是这类等待已知的候选设备、物料
        或库位身份；三项容量限制沿用本地调度门禁。
        ``include_aggregate=False`` 用于不读取返回值的调度回调，省去提交前的整图查询；
        默认仍返回任务聚合，关闭时返回 None。
        异常：身份、状态或等待合同冲突时抛 ``StoreConflict``。
        """

        with self._store.transaction() as connection:
            job_row = self._job_row(connection, job_uuid)
            if job_row["workflow_task_uuid"] != task_uuid:
                raise StoreConflict(f"作业不属于指定任务：{job_uuid}/{task_uuid}")
            task_row = self._task_row(connection, task_uuid)
            if job_row["status"] != "pending":
                return (
                    self._aggregate(connection, task_uuid)
                    if include_aggregate
                    else None
                )
            if task_row["status"] not in {"pending", "running"}:
                raise StoreConflict(f"任务不能等待执行资源：{task_uuid}")
            if task_row["status"] == "pending":
                activation = activate_workflow_task(
                    connection,
                    task_uuid=task_uuid,
                    max_active_tasks=max_active_tasks,
                    max_tasks_per_workflow=max_tasks_per_workflow,
                )
                if not activation.available:
                    self._append_invalidation(
                        connection,
                        task_uuid=task_uuid,
                        now=utc_now(),
                    )
                    return (
                        self._aggregate(connection, task_uuid)
                        if include_aggregate
                        else None
                    )
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status="pending",
                    to_status="running",
                    now=utc_now(),
                )
            resources = _normalize_wait_reason_resources(wait_resources)
            if execution_locks:
                record_execution_lock_wait(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    requests=execution_locks,
                    blocking_task_uuid=blocking_task_uuid,
                    blocking_job_uuid=blocking_job_uuid,
                    wait_resources=resources,
                )
            else:
                normalized_wait_code = str(wait_code or "").strip()
                normalized_wait_message = str(wait_message or "").strip()
                if not normalized_wait_code or not normalized_wait_message:
                    raise StoreConflict("无具体资源的门禁等待必须提供代码和中文原因")
                existing_reason = _decode_json_field(
                    job_row["wait_reason"], fallback={}
                )
                waiting_since = (
                    str(existing_reason.get("waiting_since"))
                    if isinstance(existing_reason, Mapping)
                    and existing_reason.get("waiting_since")
                    else utc_now()
                )
                reason = {
                    "code": normalized_wait_code,
                    "message": normalized_wait_message,
                    "waiting_since": waiting_since,
                }
                if resources:
                    reason["resources"] = resources
                encoded_reason = _encode_json_field(
                    reason,
                    field_name="wait_reason",
                )
                now = utc_now()
                connection.execute(
                    "UPDATE workflow_node_job SET wait_reason=?, update_time=? "
                    "WHERE uuid=? AND status='pending'",
                    (encoded_reason, now, job_uuid),
                )
                connection.execute(
                    "UPDATE workflow_task SET wait_reason=?, update_time=? "
                    "WHERE uuid=?",
                    (encoded_reason, now, task_uuid),
                )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=utc_now(),
            )
            return self._aggregate(connection, task_uuid) if include_aggregate else None

    def project_dispatch_accepted(self, job_uuid: str) -> dict[str, Any]:
        """记录本地执行适配器已经接受作业，并把持久占用推进为 running。"""

        with self._store.transaction() as connection:
            job_row = self._job_row(connection, job_uuid)
            task_uuid = str(job_row["workflow_task_uuid"])
            # HTTP 完成回执可能先于 WebSocket 开始通知到达。
            # 已提交终态是权威事实，晚到通知不能使其倒退或重新占用资源。
            if job_row["status"] in {"running", "succeeded", "failed", "canceled", "timeout"}:
                return self._aggregate(connection, task_uuid)
            if job_row["status"] != "dispatched":
                raise StoreConflict(f"作业不能确认进入 running：{job_uuid}")
            accepted_at = utc_now()
            changed = connection.execute(
                """
                UPDATE workflow_node_job
                SET status = 'running', started_at = COALESCE(started_at, ?),
                    start_state = CASE WHEN start_state='NOT_STARTED' THEN 'START_COMMITTED' ELSE start_state END,
                    update_time = ?
                WHERE uuid = ? AND status = 'dispatched' AND deleted_at IS NULL
                """,
                (accepted_at, accepted_at, job_uuid),
            ).rowcount
            if changed != 1:
                raise StoreConflict(f"作业执行确认发生并发变化：{job_uuid}")
            mark_execution_locks_running(
                connection,
                job_uuid=job_uuid,
                now=accepted_at,
            )
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="job_transition",
                from_status="dispatched",
                to_status="running",
                now=accepted_at,
            )
            claim = get_execution_claim(connection, job_uuid=job_uuid)
            if claim is None and job_row["execution_source"] != "error_manual":
                raise StoreConflict(f"运行中作业缺少持久 Claim：{job_uuid}")
            append_job_state_event(
                connection,
                job_row=job_row,
                status="running",
                details={
                    "command_uuid": str(job_row["edge_command_uuid"]),
                    "claim_uuid": str(claim["claim_uuid"]) if claim else None,
                    "fences": [dict(fence) for fence in claim["fences"]] if claim else [],
                    "started_at": accepted_at,
                },
            )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=accepted_at,
            )
            return self._aggregate(connection, task_uuid)

    def project_execution_attention(
        self,
        job_uuid: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """让物理结果不明的作业保持运行主状态并冻结全部执行资源。

        参数：``job_uuid`` 是稳定工作流节点作业 UUID；``reason`` 是可审计的不确定
        原因。返回：更新后的 Task/Job 聚合。异常：作业未越过派发边界、理由为空或
        既有理由冲突时抛 ``StoreConflict``。该操作不创建额外 Job 主状态；
        不确定性由 ``uncertainty_reason``、清理状态和 Claim 状态共同表达。
        """

        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise StoreConflict("物理不确定原因不能为空")
        with self._store.transaction() as connection:
            job_row = self._job_row(connection, job_uuid)
            task_uuid = str(job_row["workflow_task_uuid"])
            task_row = self._task_row(connection, task_uuid)
            if job_row["status"] == "running" and job_row["uncertainty_reason"]:
                if job_row["uncertainty_reason"] != normalized_reason:
                    raise StoreConflict(f"作业物理不确定原因冲突：{job_uuid}")
                return self._aggregate(connection, task_uuid)
            if job_row["status"] not in {
                "dispatched",
                "running",
                "cancel_requested",
            }:
                raise StoreConflict(f"作业不能进入物理对账等待：{job_uuid}")
            opened_at = utc_now()
            changed = connection.execute(
                """
                UPDATE workflow_node_job
                SET status = 'running', uncertainty_reason = ?,
                    cancel_ack_deadline_at = NULL,
                    cancel_complete_deadline_at = NULL,
                    wait_reason = '{}', update_time = ?
                WHERE uuid = ?
                  AND status IN ('dispatched', 'running', 'cancel_requested')
                  AND deleted_at IS NULL
                """,
                (normalized_reason, opened_at, job_uuid),
            ).rowcount
            if changed != 1:
                raise StoreConflict(f"作业物理对账状态发生并发变化：{job_uuid}")
            resume_control = (
                str(task_row["dispatch_gate"] or "active")
                if task_row["control_status"] != "waiting_reconciliation"
                else str(
                    task_row["reconciliation_resume_control_status"]
                    or task_row["dispatch_gate"]
                    or "active"
                )
            )
            connection.execute(
                """
                UPDATE workflow_task
                SET control_status = CASE WHEN control_status='error_waiting'
                    THEN 'error_waiting' ELSE 'waiting_reconciliation' END,
                    cleanup_status = 'requires_attention',
                    attention_reason = ?,
                    reconciliation_resume_control_status = ?,
                    wait_reason = '{}', update_time = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (normalized_reason, resume_control, opened_at, task_uuid),
            )
            mark_execution_locks_uncertain(
                connection,
                job_uuid=job_uuid,
                now=opened_at,
            )
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="uncertainty_opened",
                from_status=str(job_row["status"]),
                to_status="running",
                data={"reason": normalized_reason},
                now=opened_at,
            )
            append_job_state_event(
                connection,
                job_row=job_row,
                status="running",
                event_name="execution_attention",
                details={
                    "uncertainty_reason": normalized_reason,
                    "cleanup_status": "requires_attention",
                    "opened_at": opened_at,
                },
            )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=opened_at,
            )
            return self._aggregate(connection, task_uuid)

    def project_quantity_consumption_attention(
        self,
        job_uuid: str,
        *,
        reason: str,
        outcome: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """保存已执行但数量未结算的证据，并保持任务和资源等待人工核对。"""

        with self._store.transaction() as connection:
            job = self._job_row(connection, job_uuid)
            task_uuid = str(job["workflow_task_uuid"])
            task = self._task_row(connection, task_uuid)
            control = _decode_json_field(job["control_data"], fallback={})
            if not isinstance(control, dict):
                raise StoreConflict("数量结算控制证据损坏")
            evidence = dict(control.get("quantity_reconciliation") or {})
            if outcome is not None:
                if outcome.get("outcome") != "succeeded" or outcome.get("unknown_command_ids"):
                    raise StoreConflict("数量对账缺少明确成功的设备结果")
                committed = dict(outcome)
                if evidence.get("committed_outcome") not in (None, committed):
                    raise StoreConflict("数量对账的设备结果与首次提交不一致")
                evidence["committed_outcome"] = committed
            evidence["error"] = str(reason)
            control["quantity_reconciliation"] = evidence
            encoded = _encode_json_field(control, field_name="control_data")
            if (
                job["uncertainty_reason"] == QUANTITY_RECONCILIATION_REQUIRED
                and job["control_data"] == encoded
                and task["control_status"] == "waiting_reconciliation"
            ):
                return self._aggregate(connection, task_uuid)
            now = utc_now()
            connection.execute(
                """UPDATE workflow_node_job
                SET uncertainty_reason=?,control_data=?,update_time=?,
                    cancel_ack_deadline_at=NULL,cancel_complete_deadline_at=NULL
                WHERE uuid=? AND deleted_at IS NULL""",
                (QUANTITY_RECONCILIATION_REQUIRED, encoded, now, job_uuid),
            )
            connection.execute(
                """UPDATE workflow_task SET control_status='waiting_reconciliation',
                cleanup_status='requires_attention',attention_reason=?,
                reconciliation_resume_control_status=COALESCE(
                    reconciliation_resume_control_status, dispatch_gate, control_status),
                dispatch_gate=dispatch_gate,
                wait_reason='{}',update_time=? WHERE uuid=? AND deleted_at IS NULL""",
                (QUANTITY_RECONCILIATION_REQUIRED, now, task_uuid),
            )
            mark_execution_locks_uncertain(connection, job_uuid=job_uuid, now=now)
            append_runtime_event(
                connection, task_uuid=task_uuid, job_uuid=job_uuid,
                kind="uncertainty_opened", from_status=str(job["status"]),
                to_status=str(job["status"]),
                data={"reason": QUANTITY_RECONCILIATION_REQUIRED, "error": str(reason)},
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._aggregate(connection, task_uuid)

    def stage_quantity_reconciliation(
        self,
        job_uuid: str,
        *,
        consumptions: Sequence[Mapping[str, Any]],
        reason: str,
        can_replace: bool,
    ) -> dict[str, Any]:
        """在跨库存事务前保存更正请求，使响应丢失后的同请求可安全重放。"""

        request = {"consumptions": [dict(item) for item in consumptions], "reason": reason}
        with self._store.transaction() as connection:
            job = self._job_row(connection, job_uuid)
            control = _decode_json_field(job["control_data"], fallback={})
            evidence = control.get("quantity_reconciliation") if isinstance(control, dict) else None
            if not isinstance(evidence, dict) or not isinstance(evidence.get("committed_outcome"), dict):
                raise StoreConflict("数量对账缺少原始设备成功结果，须先恢复结果投递")
            previous = evidence.get("requested_correction")
            if previous == request:
                return dict(evidence["committed_outcome"])
            if not can_replace or job["uncertainty_reason"] != QUANTITY_RECONCILIATION_REQUIRED:
                raise StoreConflict("数量消费已经结算，不能提交另一份更正")
            evidence["requested_correction"] = request
            connection.execute(
                "UPDATE workflow_node_job SET control_data=?,update_time=? WHERE uuid=?",
                (_encode_json_field(control, field_name="control_data"), utc_now(), job_uuid),
            )
            return dict(evidence["committed_outcome"])

    def project_execution_restart_attention(self, task_uuid: str) -> dict[str, Any]:
        """重启只冻结待核实尝试，实际结果和占用按原身份继续结算。"""
        with self._store.transaction() as connection:
            preserve_task_after_execution_process_restart(connection, task_uuid=task_uuid)
            return self._aggregate(connection, task_uuid)

    def project_execution_process_restarted(
        self,
        task_uuid: str,
    ) -> dict[str, Any] | None:
        """在一个事务内失败因设备执行进程重启而中断的任务。

        参数：``task_uuid`` 是稳定工作流任务（WorkflowTask）UUID。返回：非终态
        Task 失败后的任务/作业聚合；既有终态 Task 返回 ``None``。已完成
        Job 保留结果，在途 Job 失败，未开始 Job 取消，旧软件执行占用释放。
        cleanup_status 保持 pending，待跨库软件资源清理完成后写 settled；
        此过程不推断物理停止或实际库存变化，也不创建对账任务。
        """

        with self._store.transaction() as connection:
            changed = fail_task_after_execution_process_restart(
                connection,
                task_uuid=task_uuid,
            )
            if not changed:
                return None
            return self._aggregate(connection, task_uuid)

    def project_uncertain_resolution_requested(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        reason: str,
        device_command_id: str,
    ) -> dict[str, Any]:
        """记录人工取消结论已请求 Edge 证明，但继续持有执行占用。"""

        normalized_command = str(command_uuid or "").strip()
        normalized_reason = str(reason or "").strip()
        normalized_device_command = str(device_command_id or "").strip()
        if not normalized_command or not normalized_reason:
            raise StoreConflict("UNKNOWN 处置命令和理由不能为空")
        with self._store.transaction() as connection:
            job = self._job_row(connection, job_uuid)
            task_uuid = str(job["workflow_task_uuid"])
            if (
                job["status"] not in {"running", "failed", "cancel_requested", "dispatched"}
                or not job["uncertainty_reason"]
            ):
                raise StoreConflict("只有等待物理对账的运行中或失败作业可以人工处置")
            leases = list_execution_locks(connection, job_uuid=job_uuid)
            if not leases or any(lease["state"] != "uncertain" for lease in leases):
                raise StoreConflict("作业缺少完整的 uncertain 执行占用")
            control = _decode_json_field(job["control_data"], fallback={})
            if not isinstance(control, Mapping):
                raise StoreConflict("作业 control_data 已损坏")
            expected = {
                "resolution": "canceled",
                "reason": normalized_reason,
                "command_uuid": normalized_command,
                "device_command_id": normalized_device_command,
                "status": "pending_edge_confirmation",
            }
            existing = control.get("manual_resolution")
            if existing is not None:
                if existing != expected:
                    raise StoreConflict("作业已有另一项 UNKNOWN 人工处置")
                return self._aggregate(connection, task_uuid)
            updated = dict(control)
            updated["manual_resolution"] = expected
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_node_job
                SET control_data = ?, update_time = ?
                WHERE uuid = ? AND status IN ('running', 'failed')
                  AND uncertainty_reason IS NOT NULL
                """,
                (
                    _encode_json_field(updated, field_name="control_data"),
                    now,
                    job_uuid,
                ),
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._aggregate(connection, task_uuid)

    def project_failed_job_execution_stopped(
        self,
        job_uuid: str,
        *,
        outcome: str,
        return_info: Mapping[str, Any],
        error_info: Sequence[Any],
    ) -> dict[str, Any]:
        """记录重启失败作业已经获得设备停止证明。

        参数：``job_uuid`` 是业务主状态已为 failed 的作业；``outcome`` 只接受
        ``canceled`` 或 ``failed`` 的设备终态；其余字段是 Edge 持久结果证据。
        返回：保持 failed 主状态的聚合；无库存变化时同时释放 Claim，物料转运则
        保留 Claim 等待实际位置对账。异常：缺少不确定事实、伪造成功或重放载荷
        冲突时抛 ``StoreConflict``。
        """

        try:
            stopped = execution_stopped_evidence(
                outcome=outcome,
                return_info=return_info,
                error_info=error_info,
            )
        except PhysicalSettlementPolicyError as error:
            raise StoreConflict(str(error)) from error
        with self._store.transaction() as connection:
            job = self._job_row(connection, job_uuid)
            task_uuid = str(job["workflow_task_uuid"])
            control = _decode_json_field(job["control_data"], fallback={})
            expected_change = _decode_json_field(
                job["expected_change_set"],
                fallback={},
            )
            if not isinstance(control, Mapping) or not isinstance(
                expected_change,
                Mapping,
            ):
                raise StoreConflict("作业物理结算审计字段已损坏")
            existing = control.get("physical_settlement")
            if existing is not None and existing != stopped:
                raise StoreConflict("作业已有另一份物理停止证明")
            if (
                job["status"] == "failed"
                and not job["uncertainty_reason"]
                and existing == stopped
            ):
                return self._aggregate(connection, task_uuid)
            if job["status"] != "failed" or not job["uncertainty_reason"]:
                raise StoreConflict("作业不是等待物理结算的失败作业")
            try:
                settlement = plan_terminal_settlement(
                    outcome=outcome,
                    return_info=return_info,
                    error_info=error_info,
                    expected_change_set=expected_change,
                    control_data=control,
                    proven_not_started=False,
                )
            except PhysicalSettlementPolicyError as error:
                raise StoreConflict(str(error)) from error
            no_inventory_change = not settlement.hold_claim
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_node_job
                SET control_data=?, uncertainty_reason=?, update_time=?
                WHERE uuid=? AND status='failed'
                  AND uncertainty_reason IS NOT NULL
                """,
                (
                    _encode_json_field(
                        settlement.control_data,
                        field_name="control_data",
                    ),
                    settlement.uncertainty_reason,
                    now,
                    job_uuid,
                ),
            )
            if no_inventory_change:
                updated_job = self._job_row(connection, job_uuid)
                release_execution_locks(
                    connection,
                    job_uuid=job_uuid,
                    now=now,
                    keep_interval_ids=_retained_resource_intervals(
                        connection=connection,
                        task_row=self._task_row(connection, task_uuid),
                        job_row=updated_job,
                        control_data=settlement.control_data,
                    ),
                )
                self._resume_task_after_reconciliation(
                    connection,
                    task_uuid=task_uuid,
                    now=now,
                )
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="uncertainty_resolved",
                from_status="failed",
                to_status="failed",
                data={
                    "settlement_stage": "physical_execution_stopped",
                    "outcome": outcome,
                    "inventory_reconciliation_required": not no_inventory_change,
                },
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._aggregate(connection, task_uuid)

    def project_failed_job_inventory_reconciled(
        self,
        job_uuid: str,
        *,
        actual_change_set: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """在库存实际位置已提交后完成失败转运作业的物理结算。

        参数：``job_uuid`` 是等待物料位置对账的失败作业；``actual_change_set`` 是
        已由库存权威提交的物料和实际位置；``reason`` 是操作员说明。返回：释放
        Job Claim 后的聚合，但保持 Task/Job failed 主状态。异常：缺设备停止证明、
        物料身份不匹配或重放冲突时抛 ``StoreConflict``。
        """

        normalized_reason = str(reason or "").strip()
        if not normalized_reason or not isinstance(actual_change_set, Mapping):
            raise StoreConflict("物理结算实际变化和原因不能为空")
        with self._store.transaction() as connection:
            job = self._job_row(connection, job_uuid)
            control = _decode_json_field(job["control_data"], fallback={})
            reconciled = {
                "actual_change_set": dict(actual_change_set),
                "reason": normalized_reason,
            }
            existing = (
                control.get("inventory_reconciliation")
                if isinstance(control, Mapping)
                else None
            )
            if (
                job["status"] in {"failed", "canceled", "timeout"}
                and not job["uncertainty_reason"]
                and existing == reconciled
            ):
                return self._aggregate(
                    connection,
                    str(job["workflow_task_uuid"]),
                )
            if job["status"] not in {"failed", "canceled", "timeout"} or job[
                "uncertainty_reason"
            ] not in {
                MATERIAL_TRANSFER_RECONCILIATION_REQUIRED,
                MATERIAL_CONTENT_RECONCILIATION_REQUIRED,
            }:
                raise StoreConflict("作业不是等待库存物理对账的失败作业")
            expected = _decode_json_field(job["expected_change_set"], fallback={})
            if not isinstance(expected, Mapping) or not isinstance(control, Mapping):
                raise StoreConflict("作业物理结算审计字段已损坏")
            stopped = control.get("physical_settlement")
            if not isinstance(stopped, Mapping) or not stopped.get("execution_stopped"):
                raise StoreConflict("物料位置对账前缺少设备停止证明")
            change_matches = (
                expected.get("kind") == "material_transfer"
                and actual_change_set.get("material_uuid")
                == expected.get("material_uuid")
            ) or (
                expected.get("kind") == "material_content_aliquot"
                and actual_change_set.get("source_material_uuid")
                == expected.get("source_material_uuid")
                and sorted(actual_change_set.get("target_material_uuids") or [])
                == sorted(expected.get("target_material_uuids") or [])
            )
            if not change_matches:
                raise StoreConflict("实际 ChangeSet 与原转运物料身份不一致")
            if existing is not None and existing != reconciled:
                raise StoreConflict("作业已有另一份库存位置对账")
            updated_control = dict(control)
            updated_control["inventory_reconciliation"] = reconciled
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_node_job
                SET control_data=?, uncertainty_reason=NULL, update_time=?
                WHERE uuid=? AND status IN ('failed', 'canceled', 'timeout')
                  AND uncertainty_reason=
                      ?
                """,
                (
                    _encode_json_field(updated_control, field_name="control_data"),
                    now,
                    job_uuid,
                    job["uncertainty_reason"],
                ),
            )
            updated_job = self._job_row(connection, job_uuid)
            task_uuid = str(job["workflow_task_uuid"])
            release_execution_locks(
                connection,
                job_uuid=job_uuid,
                now=now,
                keep_interval_ids=_retained_resource_intervals(
                    connection=connection,
                    task_row=self._task_row(connection, task_uuid),
                    job_row=updated_job,
                    control_data=updated_control,
                ),
            )
            self._resume_task_after_reconciliation(
                connection,
                task_uuid=task_uuid,
                now=now,
            )
            append_runtime_event(
                connection,
                task_uuid=str(job["workflow_task_uuid"]),
                job_uuid=job_uuid,
                kind="uncertainty_resolved",
                from_status="failed",
                to_status="failed",
                data={
                    "settlement_stage": "physical_inventory_reconciled",
                    **reconciled,
                },
                now=now,
            )
            self._append_invalidation(
                connection,
                task_uuid=str(job["workflow_task_uuid"]),
                now=now,
            )
            return self._aggregate(connection, str(job["workflow_task_uuid"]))

    def list_execution_locks(self, job_uuid: str | None = None) -> list[dict[str, Any]]:
        """读取本地持久执行占用；可按作业身份过滤。"""

        with self._store.transaction() as connection:
            return list_execution_locks(connection, job_uuid=job_uuid)

    def list_task_execution_locks(self, task_uuid: str) -> dict[str, Any]:
        """读取任务详情页所需的活动执行锁与人工释放资格。"""

        with self._store.transaction() as connection:
            task = self._task_row(connection, task_uuid)
            task_status = str(task["status"])
            terminal_task = task_status in {"failed", "canceled", "timeout"}
            execution_plan = _decode_json_field(task["execution_plan"], fallback={})
            tenancies = active_task_device_tenancies(
                connection,
                task_uuid=task_uuid,
            )
            # ``list_execution_locks`` is the historical fact reader and also
            # returns released lease rows for audit/history views.  The task
            # detail endpoint is an operator surface for *current* locks, so
            # keep only states that still represent an active reservation or
            # an unresolved outcome.
            leases = [
                lease
                for lease in list_execution_locks(connection, task_uuid=task_uuid)
                if str(lease["state"]) in {"reserved", "running", "uncertain"}
            ]
            job_rows = {
                str(row["uuid"]): row
                for row in connection.execute(
                    """
                    SELECT uuid, workflow_node_uuid, status, uncertainty_reason
                    FROM workflow_node_job
                    WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                    """,
                    (task_uuid,),
                ).fetchall()
            }
            result: list[dict[str, Any]] = []
            for lease in leases:
                job_uuid = str(lease["workflow_node_job_uuid"])
                job = job_rows.get(job_uuid)
                claim = connection.execute(
                    """
                    SELECT state FROM execution_claim
                    WHERE claim_uuid = ? LIMIT 1
                    """,
                    (lease.get("claim_uuid"),),
                ).fetchone()
                job_status = str(job["status"]) if job is not None else "unknown"
                reason = _lock_release_block_reason(
                    task_status=task_status,
                    job_status=job_status,
                    uncertainty_reason=(
                        str(job["uncertainty_reason"] or "")
                        if job is not None
                        else ""
                    ),
                    lease_state=str(lease["state"]),
                    claim_state=(
                        str(claim["state"]) if claim is not None else "missing"
                    ),
                    has_active_tenancy=bool(tenancies),
                )
                if job is not None and node_has_explicit_resource_interval(
                    execution_plan,
                    str(job["workflow_node_uuid"]),
                ):
                    reason = "连续区间锁必须通过任务级人工解锁整组释放"
                result.append(
                    {
                        **lease,
                        "job_status": job_status,
                        "claim_state": (
                            str(claim["state"]) if claim is not None else "missing"
                        ),
                        "can_release": terminal_task and reason is None,
                        "release_block_reason": reason,
                    }
                )
            return {
                "workflow_task_uuid": task_uuid,
                "task_status": task_status,
                "locks": result,
                "active_device_tenancy_count": len(tenancies),
            }

    def force_release_execution_lock(
        self,
        task_uuid: str,
        lease_uuid: str,
        *,
        expected_claim_uuid: str,
        expected_fencing_token: int,
        reason: str,
        physical_settlement_confirmed: bool,
    ) -> dict[str, Any]:
        """在安全条件满足时人工释放目标作业的完整执行占用。"""

        with self._store.transaction() as connection:
            task = self._task_row(connection, task_uuid)
            task_status = str(task["status"])
            if task_status not in {"failed", "canceled", "timeout"}:
                raise StoreConflict("只有失败、取消或超时任务才能人工释放执行锁")
            lease = connection.execute(
                """
                SELECT * FROM execution_lock_lease
                WHERE uuid = ? AND workflow_task_uuid = ? AND deleted_at IS NULL
                """,
                (lease_uuid, task_uuid),
            ).fetchone()
            if lease is None:
                raise StoreNotFound(f"执行锁租约不存在：{lease_uuid}")
            job_uuid = str(lease["workflow_node_job_uuid"])
            job = connection.execute(
                """
                SELECT uuid, workflow_node_uuid, status, uncertainty_reason
                FROM workflow_node_job
                WHERE uuid = ? AND workflow_task_uuid = ? AND deleted_at IS NULL
                """,
                (job_uuid, task_uuid),
            ).fetchone()
            if job is None:
                raise StoreNotFound(f"执行锁所属作业不存在：{job_uuid}")
            execution_plan = _decode_json_field(task["execution_plan"], fallback={})
            if node_has_explicit_resource_interval(
                execution_plan,
                str(job["workflow_node_uuid"]),
            ):
                raise StoreConflict("连续区间锁必须通过任务级人工解锁整组释放")
            job_status = str(job["status"])
            if job_status not in {"failed", "canceled", "timeout"}:
                raise StoreConflict("只有终态作业才能人工释放执行锁")
            if str(job["uncertainty_reason"] or "").strip():
                raise StoreConflict("作业仍有结果不确定原因，不能人工释放执行锁")
            if not physical_settlement_confirmed:
                raise StoreConflict("必须确认设备已停止且物理现场已安全")
            if not reason.strip():
                raise StoreConflict("人工释放必须填写原因")
            if active_task_device_tenancies(connection, task_uuid=task_uuid):
                raise StoreConflict("任务仍有活动设备托管，不能人工释放执行锁")
            claim = connection.execute(
                """
                SELECT * FROM execution_claim
                WHERE workflow_node_job_uuid = ?
                ORDER BY attempt DESC LIMIT 1
                """,
                (job_uuid,),
            ).fetchone()
            if claim is None:
                raise StoreConflict("执行锁租约缺少 Claim，拒绝人工处置")
            claim_uuid = str(claim["claim_uuid"])
            if str(lease["claim_uuid"] or "") != claim_uuid:
                raise StoreConflict("锁租约不属于当前作业 Claim，拒绝人工处置")
            if claim_uuid != expected_claim_uuid:
                raise StoreConflict("Claim 已变化，请刷新任务锁列表后重试")
            if int(lease["fencing_token"] or 0) != expected_fencing_token:
                raise StoreConflict("Fence 已变化，请刷新任务锁列表后重试")
            if str(lease["state"]) == "uncertain":
                raise StoreConflict("锁处于结果不确定状态，必须先完成物理结算")
            if str(claim["state"]) == "uncertain":
                raise StoreConflict("Claim 处于结果不确定状态，必须先完成物理结算")
            active_leases = [
                item
                for item in list_execution_locks(connection, job_uuid=job_uuid)
                if str(item["state"]) in {"reserved", "running", "uncertain"}
            ]
            if str(lease["state"]) not in {"reserved", "running", "uncertain"}:
                action = record_execution_lock_operator_action(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    lease_uuid=lease_uuid,
                    claim_uuid=claim_uuid,
                    expected_claim_uuid=expected_claim_uuid,
                    expected_fencing_token=expected_fencing_token,
                    reason=reason.strip(),
                    physical_settlement_confirmed=physical_settlement_confirmed,
                    result="already_released",
                    released_lock_uuids=[],
                    now=utc_now(),
                )
                return {"status": "already_released", "action": action}
            now = utc_now()
            released_uuids = [str(item["uuid"]) for item in active_leases]
            release_execution_locks(connection, job_uuid=job_uuid, now=now)
            action = record_execution_lock_operator_action(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                lease_uuid=lease_uuid,
                claim_uuid=claim_uuid,
                expected_claim_uuid=expected_claim_uuid,
                expected_fencing_token=expected_fencing_token,
                reason=reason.strip(),
                physical_settlement_confirmed=physical_settlement_confirmed,
                result="released",
                released_lock_uuids=released_uuids,
                now=now,
            )
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="lock_operator_released",
                from_status=str(lease["state"]),
                to_status="released",
                data={
                    "lease_uuid": lease_uuid,
                    "claim_uuid": claim_uuid,
                    "released_lock_uuids": released_uuids,
                    "reason": reason.strip(),
                    "physical_settlement_confirmed": True,
                    "operator_action_uuid": action["uuid"],
                },
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return {
                "status": "released",
                "action": action,
                "released_lock_uuids": released_uuids,
            }

    def list_device_tenancies(
        self,
        task_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取全部或指定 Task 尚未物理卸载结算的设备托管事实。"""

        with self._store.transaction() as connection:
            return active_task_device_tenancies(connection, task_uuid=task_uuid)

    def get_execution_claim(self, job_uuid: str) -> dict[str, Any] | None:
        """读取节点作业当前尝试的稳定 Claim 与资源 Fence。

        参数：``job_uuid`` 是工作流节点作业 UUID。返回：未取得门禁时为
        ``None``，否则返回 Claim 快照。异常：损坏的持久事实解码错误原样传播，
        调用方必须关闭式阻止物理派发。
        """

        with self._store.transaction() as connection:
            return get_execution_claim(connection, job_uuid=job_uuid)

    def require_dispatchable_execution_claim(
        self,
        job_uuid: str,
    ) -> dict[str, Any]:
        """在物理派发前复验 Workflow Claim、Lease 与 Fence 完整活动。"""

        with self._store.transaction() as connection:
            return require_dispatchable_execution_claim(
                connection,
                job_uuid=job_uuid,
            )

    def list_active_execution_claim_uuids(self) -> tuple[str, ...]:
        """返回启动跨库对账所需的全部活动工作流 Claim 身份。"""

        with self._store.read() as connection:
            return list_active_execution_claim_uuids(connection)

    def get_execution_wait_graph(self) -> dict[str, Any]:
        """返回当前整站 Job 等待边与运行时死锁循环的只读快照。"""

        with self._store.read() as connection:
            return build_execution_wait_graph(connection)

    def project_feedback(
        self,
        *,
        job_uuid: str,
        sequence: int,
        feedback_type: str,
        data: Mapping[str, Any],
        observed_at: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """把 Edge 已提交的过程反馈幂等投影为有序历史和最新摘要。"""

        return self._evidence.commit_feedback(
            job_uuid=job_uuid,
            sequence=sequence,
            feedback_type=feedback_type,
            data=data,
            observed_at=observed_at,
            idempotency_key=idempotency_key,
        )

    def project_local_control_evaluation(
        self,
        *,
        job_uuid: str,
        selected_branch: str | None,
        skipped_job_uuids: Sequence[str],
        error_code: str | None = None,
        error_message: str | None = None,
        local_command_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """原子结算调度器本地条件作业及其未选分支。

        本入口只接受尚未派发的 ``condition`` 作业和同任务 ``pending`` 作业，
        不创建执行 Claim、Fence 或设备结算事实。
        """

        normalized_error = str(error_code or "").strip() or None
        condition_status = "failed" if normalized_error else "succeeded"
        condition_return = (
            {} if normalized_error else {"selected_branch": selected_branch}
        )
        if local_command_result is not None and not normalized_error:
            condition_return = dict(local_command_result)
        condition_errors = (
            [
                {
                    "code": normalized_error,
                    "message": str(error_message or "条件求值失败"),
                }
            ]
            if normalized_error
            else []
        )
        normalized_skipped = tuple(
            dict.fromkeys(str(item) for item in skipped_job_uuids)
        )
        finished_at = utc_now()
        with self._store.transaction() as connection:
            condition_job = self._job_row(connection, job_uuid)
            task_uuid = str(condition_job["workflow_task_uuid"])
            task_row = self._task_row(connection, task_uuid)
            expected_kind = "workflow_command" if local_command_result is not None else "condition"
            if str(condition_job["executor_kind"]) != expected_kind:
                raise StoreConflict(f"本地控制作业类型非法：{job_uuid}")
            if task_row["status"] not in {"pending", "running", "failed", "succeeded"}:
                raise StoreConflict(f"父任务状态不接受本地控制结果：{task_uuid}")

            self._project_local_job_terminal(
                connection,
                task_uuid=task_uuid,
                job_row=condition_job,
                target_status=condition_status,
                return_info=condition_return,
                error_info=condition_errors,
                now=finished_at,
                require_condition=local_command_result is None,
            )
            skip_code = normalized_error or "branch_not_selected"
            for skipped_uuid in normalized_skipped:
                skipped_job = self._job_row(connection, skipped_uuid)
                if str(skipped_job["workflow_task_uuid"]) != task_uuid:
                    raise StoreConflict("条件节点不能跳过其他任务的作业")
                self._project_local_job_terminal(
                    connection,
                    task_uuid=task_uuid,
                    job_row=skipped_job,
                    target_status="skipped",
                    return_info={},
                    error_info=[{"code": skip_code}],
                    now=finished_at,
                    require_condition=False,
                )

            self._project_ready_output(
                connection,
                task_uuid=task_uuid,
                now=finished_at,
            )
            _reconcile_terminal_resource_intervals(
                connection,
                task_uuid=task_uuid,
                now=finished_at,
            )
            job_rows = self._job_rows(connection, task_uuid)
            statuses = [str(row["status"]) for row in job_rows]
            target_task_status: str | None = None
            if normalized_error:
                target_task_status = "failed"
            elif all(status in {"succeeded", "skipped"} for status in statuses):
                target_task_status = "succeeded"
            current_task_status = str(self._task_row(connection, task_uuid)["status"])
            if (
                target_task_status is not None
                and current_task_status != target_task_status
            ):
                changed = connection.execute(
                    """
                    UPDATE workflow_task
                    SET status = ?, finished_at = ?, update_time = ?
                    WHERE uuid = ? AND status IN ('pending', 'running')
                      AND deleted_at IS NULL
                    """,
                    (
                        target_task_status,
                        finished_at,
                        finished_at,
                        task_uuid,
                    ),
                ).rowcount
                if changed != 1:
                    raise StoreConflict(f"任务本地控制终态发生并发变化：{task_uuid}")
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status=current_task_status,
                    to_status=target_task_status,
                    now=finished_at,
                )
                append_task_state_event(
                    connection,
                    task_uuid=task_uuid,
                    status=target_task_status,
                    details={"finished_at": finished_at},
                )
            self._append_invalidation(connection, task_uuid=task_uuid, now=finished_at)
            return self._aggregate(connection, task_uuid)

    def materialize_repeat_iteration(
        self,
        *,
        control_job_uuid: str,
        iteration_index: int,
        control_path: str,
        jobs: Sequence[Mapping[str, Any]],
        max_task_jobs: int = 10_000,
    ) -> dict[str, Any]:
        """在一个事务中幂等创建 RepeatUntil 某轮的独立作业。"""

        if isinstance(iteration_index, bool) or iteration_index < 0:
            raise StoreConflict("循环轮次索引必须是非负整数")
        if not control_path or not jobs:
            raise StoreConflict("循环轮次必须声明控制路径和作业")
        now = utc_now()
        with self._store.transaction() as connection:
            control_job = self._job_row(connection, control_job_uuid)
            if str(control_job["executor_kind"]) != "repeat_until":
                raise StoreConflict("循环控制作业类型非法")
            task_uuid = str(control_job["workflow_task_uuid"])
            from unilabos.persistence.recovery_mode import EvidenceOnlyError, require_execution

            try:
                require_execution(connection, task_uuid=task_uuid)
            except EvidenceOnlyError as error:
                raise StoreConflict(str(error)) from error
            task = self._task_row(connection, task_uuid)
            if str(task["status"]) not in {"pending", "running"}:
                raise StoreConflict("父任务状态不允许创建新循环轮次")
            plan = _decode_json_field(task["execution_plan"], fallback={})
            planned_nodes = {
                str(node.get("uuid") or ""): node
                for node in plan.get("nodes", [])
                if isinstance(node, Mapping)
            }
            existing_count = connection.execute(
                "SELECT COUNT(*) AS count FROM workflow_node_job "
                "WHERE workflow_task_uuid = ? AND deleted_at IS NULL",
                (task_uuid,),
            ).fetchone()["count"]
            new_count = 0
            seen_templates: set[str] = set()
            for item in jobs:
                job_uuid = str(item.get("job_uuid") or "")
                template_uuid = str(item.get("workflow_node_uuid") or "")
                runtime_node_id = str(item.get("runtime_node_id") or "")
                if (
                    not job_uuid
                    or not runtime_node_id
                    or template_uuid in seen_templates
                    or template_uuid not in planned_nodes
                ):
                    raise StoreConflict("循环轮次作业身份或模板无效")
                seen_templates.add(template_uuid)
                planned = planned_nodes[template_uuid]
                executor_kind = str(item.get("executor_kind") or "")
                if executor_kind != str(planned.get("kind") or ""):
                    raise StoreConflict("循环轮次作业执行种类与冻结模板不一致")
                idempotency = {
                    "control_path": control_path,
                    "iteration_index": iteration_index,
                    "runtime_node_id": runtime_node_id,
                }
                duplicate = connection.execute(
                    """
                    SELECT * FROM workflow_node_job
                    WHERE workflow_task_uuid = ? AND workflow_node_uuid = ?
                      AND json_extract(meta_data, '$.unilab.control_path') = ?
                      AND json_extract(meta_data, '$.unilab.iteration_index') = ? AND current_attempt=1
                      AND deleted_at IS NULL
                    """,
                    (task_uuid, template_uuid, control_path, iteration_index),
                ).fetchone()
                if duplicate is not None:
                    duplicate_meta = _decode_json_field(
                        duplicate["meta_data"], fallback={}
                    )
                    expected_control_data = {
                        "control_path": control_path,
                        "iteration_index": iteration_index,
                        "runtime_node_id": runtime_node_id,
                    }
                    if (
                        str(duplicate["uuid"]) != job_uuid
                        or duplicate_meta.get("unilab") != idempotency
                        or str(duplicate["executor_kind"]) != executor_kind
                        or (duplicate["material_uuid"] or None)
                        != (item.get("material_uuid") or None)
                        or _decode_json_field(
                            duplicate["execution_policy"], fallback={}
                        )
                        != dict(item.get("execution_policy") or {})
                        or _decode_json_field(duplicate["param"], fallback={})
                        != dict(item.get("param") or {})
                        or _decode_json_field(
                            duplicate["control_data"], fallback={}
                        )
                        != expected_control_data
                    ):
                        raise StoreConflict("循环轮次幂等键载荷冲突")
                    continue
                connection.execute(
                    """
                    INSERT INTO workflow_node_job(
                        uuid, create_time, update_time, deleted_at, description,
                        meta_data, workflow_task_uuid, workflow_node_uuid,
                        material_uuid, feedback_sequence, topological_index,
                        executor_kind, execution_policy,
                        execution_timeout_seconds, status, attempt, param,
                        feedback_data, return_info, control_data, error_info
                    ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, 0, ?, ?, ?,
                              0, 'pending', 1, ?, '{}', '{}', ?, '[]')
                    """,
                    (
                        job_uuid,
                        now,
                        now,
                        _encode_json_field(
                            {"unilab": idempotency}, field_name="meta_data"
                        ),
                        task_uuid,
                        template_uuid,
                        item.get("material_uuid"),
                        int(planned.get("topological_index") or 0),
                        executor_kind,
                        _encode_json_field(
                            item.get("execution_policy") or {},
                            field_name="execution_policy",
                        ),
                        _encode_json_field(item.get("param") or {}, field_name="param"),
                        _encode_json_field(
                            {
                                "control_path": control_path,
                                "iteration_index": iteration_index,
                                "runtime_node_id": runtime_node_id,
                            },
                            field_name="control_data",
                        ),
                    ),
                )
                new_count += 1
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    kind="job_transition",
                    from_status=None,
                    to_status="pending",
                    now=now,
                )
                job_row = self._job_row(connection, job_uuid)
                append_job_state_event(
                    connection,
                    job_row=job_row,
                    status="pending",
                    details={
                        "control_path": control_path,
                        "iteration_index": iteration_index,
                    },
                )
            if int(existing_count) + new_count > max_task_jobs:
                raise StoreConflict("workflow_job_budget_exceeded")
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._aggregate(connection, task_uuid)

    def project_repeat_evaluation(
        self,
        *,
        control_job_uuid: str,
        iteration_index: int,
        condition_result: bool | None,
        carry: Mapping[str, Any],
        next_carry: Mapping[str, Any] | None,
        error_code: str | None = None,
        error_message: str | None = None,
        skipped_job_uuids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """持久提交一轮退出判断；仅退出或失败时结算循环控制作业。"""

        if (
            isinstance(iteration_index, bool)
            or not isinstance(iteration_index, int)
            or iteration_index < 0
        ):
            raise StoreConflict("循环轮次索引必须是非负整数")
        if condition_result is not None and type(condition_result) is not bool:
            raise StoreConflict("循环退出条件必须是严格布尔值")
        normalized_error = str(error_code or "").strip() or None
        now = utc_now()
        with self._store.transaction() as connection:
            control_job = self._job_row(connection, control_job_uuid)
            if str(control_job["executor_kind"]) != "repeat_until":
                raise StoreConflict("循环控制作业类型非法")
            task_uuid = str(control_job["workflow_task_uuid"])
            if not normalized_error:
                task = self._task_row(connection, task_uuid)
                plan = _decode_json_field(task["execution_plan"], fallback={})
                planned_nodes = {
                    str(item.get("uuid") or ""): item
                    for item in plan.get("nodes", [])
                    if isinstance(item, Mapping)
                }
                control_template_uuid = str(control_job["workflow_node_uuid"])

                def nearest_repeat_owner(node_uuid: str) -> str | None:
                    seen: set[str] = set()
                    current = node_uuid
                    while current not in seen:
                        seen.add(current)
                        parent = planned_nodes.get(current, {}).get("parent_uuid")
                        if not isinstance(parent, str):
                            return None
                        parent_node = planned_nodes.get(parent, {})
                        if str(parent_node.get("kind") or "") == "repeat_until":
                            return parent
                        current = parent
                    raise StoreConflict("循环计划父子关系包含环")

                expected_templates = {
                    node_uuid
                    for node_uuid in planned_nodes
                    if nearest_repeat_owner(node_uuid) == control_template_uuid
                    and str(planned_nodes[node_uuid].get("kind") or "")
                    not in {
                        "group",
                        "material_source",
                        "workflow",
                        "workflow_input",
                        "workflow_output",
                    }
                }
                control_meta = _decode_json_field(
                    control_job["meta_data"], fallback={}
                )
                unilab_meta = (
                    control_meta.get("unilab")
                    if isinstance(control_meta, Mapping)
                    else None
                )
                control_path = (
                    str(unilab_meta.get("runtime_node_id") or "")
                    if isinstance(unilab_meta, Mapping)
                    else ""
                ) or control_template_uuid
                iteration_rows = connection.execute(
                    """
                    SELECT * FROM workflow_node_job
                    WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                      AND json_extract(meta_data, '$.unilab.control_path') = ?
                      AND json_extract(meta_data, '$.unilab.iteration_index') = ? AND current_attempt=1
                    """,
                    (task_uuid, control_path, iteration_index),
                ).fetchall()
                if {
                    str(row["workflow_node_uuid"]) for row in iteration_rows
                } != expected_templates or any(
                    str(row["status"]) not in {"succeeded", "skipped"}
                    for row in iteration_rows
                ):
                    raise StoreConflict("循环轮次尚未完整成功结算")
            existing_control_data = _decode_json_field(
                control_job["control_data"], fallback={}
            )
            control_data = {
                **(
                    {
                        key: existing_control_data[key]
                        for key in ("control_path", "runtime_node_id")
                        if key in existing_control_data
                    }
                    if isinstance(existing_control_data, Mapping)
                    else {}
                ),
                "iteration_index": iteration_index,
                "condition_result": condition_result,
                "carry": dict(carry),
                "next_carry": dict(next_carry or {}),
            }
            existing_iteration = (
                existing_control_data.get("iteration_index")
                if isinstance(existing_control_data, Mapping)
                # A nested controller initially carries its OUTER iteration's
                # identity. Only an evaluated checkpoint has condition_result.
                and "condition_result" in existing_control_data
                else None
            )
            if (
                isinstance(existing_iteration, int)
                and not isinstance(existing_iteration, bool)
                and existing_iteration > iteration_index
            ):
                raise StoreConflict("循环决定不能回退到旧轮次")
            if existing_iteration == iteration_index and dict(
                existing_control_data
            ) != control_data:
                raise StoreConflict("同一循环轮次决定载荷冲突")
            if normalized_error or condition_result is True:
                self._project_local_job_terminal(
                    connection,
                    task_uuid=task_uuid,
                    job_row=control_job,
                    target_status="failed" if normalized_error else "succeeded",
                    return_info=(
                        {}
                        if normalized_error
                        else {
                            "iteration_index": iteration_index,
                            "carry": dict(carry),
                        }
                    ),
                    error_info=(
                        [
                            {
                                "code": normalized_error,
                                "message": str(error_message or "循环控制失败"),
                            }
                        ]
                        if normalized_error
                        else []
                    ),
                    now=now,
                    require_condition=False,
                )
                connection.execute(
                    "UPDATE workflow_node_job SET control_data = ? WHERE uuid = ?",
                    (
                        _encode_json_field(control_data, field_name="control_data"),
                        control_job_uuid,
                    ),
                )
                if normalized_error:
                    for skipped_uuid in dict.fromkeys(
                        str(value) for value in skipped_job_uuids
                    ):
                        skipped_job = self._job_row(connection, skipped_uuid)
                        if str(skipped_job["workflow_task_uuid"]) != task_uuid:
                            raise StoreConflict("循环节点不能跳过其他任务的作业")
                        self._project_local_job_terminal(
                            connection,
                            task_uuid=task_uuid,
                            job_row=skipped_job,
                            target_status="skipped",
                            return_info={},
                            error_info=[{"code": normalized_error}],
                            now=now,
                            require_condition=False,
                        )
                    task_row = self._task_row(connection, task_uuid)
                    current_task_status = str(task_row["status"])
                    if current_task_status in {"pending", "running"}:
                        changed = connection.execute(
                            """
                            UPDATE workflow_task
                            SET status = 'failed', finished_at = ?, update_time = ?
                            WHERE uuid = ? AND status IN ('pending', 'running')
                              AND deleted_at IS NULL
                            """,
                            (now, now, task_uuid),
                        ).rowcount
                        if changed != 1:
                            raise StoreConflict(
                                f"循环控制任务失败终态发生并发变化：{task_uuid}"
                            )
                        append_runtime_event(
                            connection,
                            task_uuid=task_uuid,
                            kind="task_transition",
                            from_status=current_task_status,
                            to_status="failed",
                            now=now,
                        )
                        append_task_state_event(
                            connection,
                            task_uuid=task_uuid,
                            status="failed",
                            details={"finished_at": now},
                        )
                    elif current_task_status != "failed":
                        raise StoreConflict(
                            f"父任务状态不接受循环失败：{task_uuid}"
                        )
                else:
                    self._project_ready_output(
                        connection,
                        task_uuid=task_uuid,
                        now=now,
                    )
                    statuses = [
                        str(row["status"])
                        for row in self._job_rows(connection, task_uuid)
                    ]
                    task_row = self._task_row(connection, task_uuid)
                    if (
                        statuses
                        and all(
                            status in {"succeeded", "skipped"}
                            for status in statuses
                        )
                        and str(task_row["status"]) in {"pending", "running"}
                    ):
                        changed = connection.execute(
                            """
                            UPDATE workflow_task
                            SET status = 'succeeded', finished_at = ?, update_time = ?
                            WHERE uuid = ? AND status IN ('pending', 'running')
                              AND deleted_at IS NULL
                            """,
                            (now, now, task_uuid),
                        ).rowcount
                        if changed != 1:
                            raise StoreConflict(
                                f"循环控制任务终态发生并发变化：{task_uuid}"
                            )
                        append_runtime_event(
                            connection,
                            task_uuid=task_uuid,
                            kind="task_transition",
                            from_status=str(task_row["status"]),
                            to_status="succeeded",
                            now=now,
                        )
                        append_task_state_event(
                            connection,
                            task_uuid=task_uuid,
                            status="succeeded",
                            details={"finished_at": now},
                        )
            else:
                if condition_result is not False or not isinstance(next_carry, Mapping):
                    raise StoreConflict("继续循环必须提交下一版 carry")
                connection.execute(
                    """
                    UPDATE workflow_node_job
                    SET control_data = ?, update_time = ?
                    WHERE uuid = ? AND status = 'pending'
                    """,
                    (
                        _encode_json_field(control_data, field_name="control_data"),
                        now,
                        control_job_uuid,
                    ),
                )
            _reconcile_terminal_resource_intervals(
                connection,
                task_uuid=task_uuid,
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._aggregate(connection, task_uuid)

    @staticmethod
    def _project_local_job_terminal(
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        job_row: sqlite3.Row,
        target_status: str,
        return_info: Mapping[str, Any],
        error_info: Sequence[Any],
        now: str,
        require_condition: bool,
    ) -> None:
        """在同一事务幂等推进一个从未派发的本地作业。"""

        job_uuid = str(job_row["uuid"])
        current_status = str(job_row["status"])
        if require_condition and str(job_row["executor_kind"]) != "condition":
            raise StoreConflict(f"本地控制作业类型非法：{job_uuid}")
        if current_status == target_status:
            if _decode_json_field(job_row["return_info"], fallback={}) == dict(
                return_info
            ) and _decode_json_field(job_row["error_info"], fallback=[]) == list(
                error_info
            ):
                return
            raise StoreConflict(f"本地作业终态载荷冲突：{job_uuid}")
        if current_status != "pending":
            raise StoreConflict(f"已派发作业不能由本地控制结算：{job_uuid}")
        # ``workflow_node_job_result`` 只冻结执行结果；分支跳过没有执行尝试，
        # 因此只写标准 Job 状态与状态事件，不能伪造 Edge 结果证据。
        if target_status != "skipped":
            record_job_result(
                connection,
                job_row=job_row,
                outcome=target_status,
                return_info=dict(return_info),
                error_info=list(error_info),
            )
        changed = connection.execute(
            """
            UPDATE workflow_node_job
            SET status = ?, return_info = ?, error_info = ?,
                wait_reason = '{}', finished_at = ?, update_time = ?
            WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
            """,
            (
                target_status,
                _encode_json_field(return_info, field_name="return_info"),
                _encode_json_field(error_info, field_name="error_info"),
                now,
                now,
                job_uuid,
            ),
        ).rowcount
        if changed != 1:
            raise StoreConflict(f"本地作业状态发生并发变化：{job_uuid}")
        append_runtime_event(
            connection,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            kind="job_transition",
            from_status="pending",
            to_status=target_status,
            now=now,
        )
        updated = connection.execute(
            "SELECT * FROM workflow_node_job WHERE uuid = ?",
            (job_uuid,),
        ).fetchone()
        append_job_state_event(
            connection,
            job_row=updated,
            status=target_status,
            details={"return_info": dict(return_info), "error_info": list(error_info)},
        )

    def project_job_finished(
        self,
        *,
        job_uuid: str,
        scheduler_state: str,
        return_info: Mapping[str, Any] | None = None,
        error_info: Sequence[Any] | None = None,
        manual_confirmation_status: str | None = None,
        inventory_reconciliation: Mapping[str, Any] | None = None,
        timing_sample: TimingObservation | None = None,
    ) -> dict[str, Any]:
        """投影工作流节点作业（WorkflowNodeJob）的明确业务结果。

        参数：``job_uuid`` 是作业稳定身份；``scheduler_state`` 只接受本地
        ``success``、``failed``、设备明确 ``canceled`` 或 ``timeout``；
        ``return_info`` 是结果对象；``error_info`` 是错误详情序列；
        ``manual_confirmation_status`` 只供截止时间或取消收敛把关联确认原子关闭。
        返回：提交后的标准任务/作业聚合。异常：未知状态、结果类型、终态或载荷
        冲突抛出 ``StoreConflict``；身份缺失抛出
        ``StoreNotFound``。相同终态和载荷的投递重放（DeliveryReplay）零写入。
        """

        if scheduler_state not in _FINISHED_STATE_MAP:
            raise StoreConflict(f"不支持的本地完成状态：{scheduler_state}")
        if manual_confirmation_status not in {None, "timed_out", "canceled"}:
            raise StoreConflict("人工确认自动关闭状态非法")
        if return_info is not None and not isinstance(return_info, Mapping):
            raise StoreConflict("return_info 必须是对象")
        if error_info is not None and (
            not isinstance(error_info, Sequence) or isinstance(error_info, (str, bytes, bytearray))
        ):
            raise StoreConflict("error_info 必须是序列")

        # ``target_job_status`` 是 Backend-shaped 合同采用的标准作业终态。
        target_job_status = _FINISHED_STATE_MAP[scheduler_state]
        normalized_return_info = dict(return_info or {})
        normalized_error_info = list(error_info or [])
        return_info_json = _encode_json_field(
            normalized_return_info,
            field_name="return_info",
        )
        error_info_json = _encode_json_field(
            normalized_error_info,
            field_name="error_info",
        )

        with self._store.transaction() as connection:
            job_row = self._job_row(connection, job_uuid)
            task_uuid = job_row["workflow_task_uuid"]
            task_row = self._task_row(connection, task_uuid)
            current_job_status = job_row["status"]
            record_job_result(
                connection,
                job_row=job_row,
                outcome=target_job_status,
                return_info=normalized_return_info,
                error_info=normalized_error_info,
            )
            no_send_proof = normalized_return_info.get("cancel_reason") == "local_no_send_proof"
            expected_change = _decode_json_field(
                job_row["expected_change_set"],
                fallback={},
            )
            control_data = _decode_json_field(job_row["control_data"], fallback={})
            if not isinstance(expected_change, Mapping) or not isinstance(
                control_data,
                Mapping,
            ):
                raise StoreConflict("作业物理结算审计字段已损坏")
            updated_control_data = dict(control_data)
            uncertainty_reason: str | None = None
            if target_job_status != "succeeded":
                try:
                    settlement = plan_terminal_settlement(
                        outcome=target_job_status,
                        return_info=normalized_return_info,
                        error_info=normalized_error_info,
                        expected_change_set=expected_change,
                        control_data=control_data,
                        proven_not_started=no_send_proof,
                    )
                except PhysicalSettlementPolicyError as error:
                    raise StoreConflict(str(error)) from error
                updated_control_data = settlement.control_data
                uncertainty_reason = settlement.uncertainty_reason
            inventory_difference = inventory_reconciliation or control_data.get("inventory_reconciliation")
            if inventory_difference:
                if target_job_status != "succeeded" or not isinstance(inventory_difference, Mapping):
                    raise StoreConflict("库存差异只能附加到已知成功的物理结果")
                updated_control_data["inventory_reconciliation"] = dict(inventory_difference)
                uncertainty_reason = "inventory_fact_changed"
            requires_inventory_reconciliation = uncertainty_reason is not None
            retained_tenancy = (
                bool(active_task_device_tenancies(connection, task_uuid=task_uuid)) if inventory_difference
                else settle_device_tenancy(connection, job_uuid=job_uuid,
                    succeeded=target_job_status == "succeeded", not_started=no_send_proof)
            )
            if current_job_status == target_job_status:
                same_return_info = (
                    _decode_json_field(
                        job_row["return_info"],
                        fallback={},
                    )
                    == normalized_return_info
                )
                same_error_info = (
                    _decode_json_field(
                        job_row["error_info"],
                        fallback=[],
                    )
                    == normalized_error_info
                )
                if same_return_info and same_error_info:
                    replayed_at = utc_now()
                    if not job_row["uncertainty_reason"]:
                        keep_interval_ids = _retained_resource_intervals(
                            connection=connection,
                            task_row=task_row,
                            job_row=job_row,
                            control_data=control_data,
                        )
                        release_execution_locks(
                            connection,
                            job_uuid=job_uuid,
                            now=replayed_at,
                            keep_interval_ids=keep_interval_ids,
                        )
                    close_pending_manual_confirmation(
                        connection,
                        job_uuid=job_uuid,
                        status=manual_confirmation_status or "canceled",
                        decided_at=replayed_at,
                    )
                    return self._aggregate(connection, task_uuid)
                raise StoreConflict(f"作业终态载荷冲突：{job_uuid}")
            if current_job_status in _TERMINAL_JOB_STATES:
                raise StoreConflict(f"作业终态冲突：{job_uuid}")
            if current_job_status not in {
                "dispatched",
                "running",
                "cancel_requested",
            }:
                raise StoreConflict(f"作业尚未派发，不能完成：{job_uuid}")
            if task_row["status"] not in {"running", "failed", "canceling", "canceled", "timeout"}:
                raise StoreConflict(f"父任务状态不接受作业结果：{task_uuid}")

            # 完整耗时样本与首次成功结果同事务保存；重复终态在上方返回，
            # 不会按重投消息的收包时间重新计时。取消、人工核实和对账不入正常样本。
            if timing_sample is not None:
                from unilabos.app.scheduler.timing_store import TimingObservation, TimingObservationStore
                if not isinstance(timing_sample, TimingObservation) or timing_sample.observation_id != job_uuid:
                    raise StoreConflict("耗时样本身份与当前作业不一致")
                if (target_job_status == "succeeded" and timing_sample.outcome == "success"
                        and not normalized_error_info and not requires_inventory_reconciliation
                        and current_job_status in {"dispatched", "running"}
                        and manual_confirmation_status is None):
                    samples = TimingObservationStore(connection)
                    samples.ensure_schema()
                    samples.append(timing_sample)

            # ``finished_at`` 是明确作业结果落盘的统一完成时间。
            finished_at = utc_now()
            updated_jobs = connection.execute(
                """
                UPDATE workflow_node_job
                SET status = ?, return_info = ?, error_info = ?,
                    cancel_ack_deadline_at = NULL,
                    cancel_complete_deadline_at = NULL,
                    control_data = ?, uncertainty_reason = ?,
                    finished_at = ?, update_time = ?
                WHERE uuid = ?
                  AND status IN (
                      'dispatched', 'running', 'cancel_requested'
                  )
                  AND deleted_at IS NULL
                """,
                (
                    target_job_status,
                    return_info_json,
                    error_info_json,
                    _encode_json_field(
                        updated_control_data,
                        field_name="control_data",
                    ),
                    (uncertainty_reason),
                    finished_at,
                    finished_at,
                    job_uuid,
                ),
            ).rowcount
            if updated_jobs != 1:
                raise StoreConflict(f"作业完成状态发生并发变化：{job_uuid}")
            if requires_inventory_reconciliation:
                mark_execution_locks_uncertain(
                    connection,
                    job_uuid=job_uuid,
                    now=finished_at,
                )
            else:
                keep_interval_ids = _retained_resource_intervals(
                    connection=connection,
                    task_row=task_row,
                    job_row=job_row,
                    control_data=updated_control_data,
                )
                release_execution_locks(
                    connection,
                    job_uuid=job_uuid,
                    now=finished_at,
                    keep_interval_ids=keep_interval_ids,
                )
            settle_intervention_for_job(
                connection,
                job_uuid=job_uuid,
                now=finished_at,
            )
            close_pending_manual_confirmation(
                connection,
                job_uuid=job_uuid,
                status=manual_confirmation_status or "canceled",
                decided_at=finished_at,
            )
            append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="job_transition",
                from_status=str(current_job_status),
                to_status=target_job_status,
                now=finished_at,
            )

            self._project_ready_output(
                connection,
                task_uuid=task_uuid,
                now=finished_at,
            )
            _reconcile_terminal_resource_intervals(
                connection,
                task_uuid=task_uuid,
                now=finished_at,
            )

            job_rows = self._job_rows(connection, task_uuid)
            was_waiting_reconciliation = task_row["control_status"] == "waiting_reconciliation"
            if was_waiting_reconciliation and not any(
                bool(str(row["uncertainty_reason"] or "").strip()) for row in job_rows
            ):
                connection.execute(
                    """
                    UPDATE workflow_task
                    SET control_status = COALESCE(
                            reconciliation_resume_control_status, dispatch_gate, 'active'
                        ),
                        dispatch_gate = COALESCE(
                            reconciliation_resume_control_status, dispatch_gate, 'active'
                        ),
                        cleanup_status = 'none', attention_reason = NULL,
                        reconciliation_resume_control_status = NULL,
                        update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (finished_at, task_uuid),
                )
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    kind="uncertainty_resolved",
                    from_status=str(current_job_status),
                    to_status=target_job_status,
                    now=finished_at,
                )
            # ``job_statuses`` 是决定父任务业务终态的完整兄弟作业状态集合。
            job_statuses = [row["status"] for row in job_rows if row["current_attempt"]]
            target_task_status: str | None = None
            if task_row["status"] == "canceling":
                if all(status in _TERMINAL_JOB_STATES for status in job_statuses):
                    target_task_status = "canceled"
            elif task_row["status"] not in {"failed", "canceled", "timeout"} and task_row["control_status"] != "error_waiting":
                if "timeout" in job_statuses:
                    target_task_status = "timeout"
                elif "failed" in job_statuses:
                    target_task_status = "failed"
                elif "canceled" in job_statuses:
                    # 物理对账中的人工取消证明终结的是整次 Task；后续节点
                    # 不得继续派发，物理清理由 settled 阶段统一完成。
                    target_task_status = "canceled"
                elif all(status in {"succeeded", "skipped"} for status in job_statuses):
                    target_task_status = "succeeded"
            if target_task_status is not None:
                active_tenancies = active_task_device_tenancies(
                    connection,
                    task_uuid=task_uuid,
                )
                if target_task_status == "succeeded" and active_tenancies and not inventory_difference:
                    target_task_status = "failed"
                    retained_tenancy = True
                updated_tasks = connection.execute(
                    """
                    UPDATE workflow_task
                    SET status = ?, finished_at = ?, update_time = ?
                    WHERE uuid = ? AND status IN ('running', 'canceling')
                      AND deleted_at IS NULL
                    """,
                    (target_task_status, finished_at, finished_at, task_uuid),
                ).rowcount
                if updated_tasks != 1:
                    raise StoreConflict(f"任务终态发生并发变化：{task_uuid}")
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status=str(task_row["status"]),
                    to_status=target_task_status,
                    now=finished_at,
                )
                append_task_state_event(
                    connection,
                    task_uuid=task_uuid,
                    status=target_task_status,
                    details={
                        "finished_at": finished_at,
                    },
                )
            if retained_tenancy:
                connection.execute(
                    """
                    UPDATE workflow_task
                    SET cleanup_status = 'requires_attention',
                        attention_reason = 'device_tenancy_requires_attention',
                        update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (finished_at, task_uuid),
                )
            if requires_inventory_reconciliation:
                resume_control = (
                    str(task_row["dispatch_gate"] or "active")
                    if task_row["control_status"] != "waiting_reconciliation"
                    else str(
                        task_row["reconciliation_resume_control_status"]
                        or task_row["dispatch_gate"]
                        or "active"
                    )
                )
                connection.execute(
                    """
                    UPDATE workflow_task
                    SET control_status = CASE WHEN ? THEN control_status WHEN control_status='error_waiting'
                        THEN 'error_waiting' ELSE 'waiting_reconciliation' END,
                        cleanup_status = 'requires_attention',
                        attention_reason =
                            ?,
                        reconciliation_resume_control_status = ?,
                        wait_reason = '{}', update_time = ?
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (
                        bool(inventory_difference),
                        uncertainty_reason,
                        resume_control,
                        finished_at,
                        task_uuid,
                    ),
                )
                append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    kind="uncertainty_opened",
                    from_status=str(current_job_status),
                    to_status=target_job_status,
                    data={
                        "reason": (uncertainty_reason),
                        "execution_stopped": True,
                    },
                    now=finished_at,
                )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=finished_at,
            )
            return self._aggregate(connection, task_uuid)


__all__ = ["TaskRuntimeProjection"]
