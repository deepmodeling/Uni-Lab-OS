"""V1 任务生命周期合同在 dev Workflow Authority 上的实现（创建字段、控制命令、观测投影）。"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from unilabos.protocol.runtime.workflow import normalize_json_object, validate_uuid

TASK_PRIORITIES = ("normal", "high")
INVENTORY_TYPES = ("reagent", "current_substance")


def normalize_task_priority(value: Any) -> str:
    """V1 ``WorkflowTaskPriority``：``normal`` / ``high``；空值按 normal。"""

    if value is None or value == "":
        return "normal"
    text = str(getattr(value, "value", value)).strip().lower()
    if text not in TASK_PRIORITIES:
        raise ValueError("任务优先级只能是 normal 或 high")
    return text


def normalize_inventory_bindings(value: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """V1 ``WorkflowInventoryBindingRequest`` 列表：逐项规范并拒绝重复的逻辑需求键。

    绑定只随任务冻结；库存实例（试剂 / 当前内容物）的存在性与数量预留由库存域负责，
    这里不读物料库。
    """

    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("inventory_bindings 必须是数组")
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        item = normalize_json_object(getattr(raw, "model_dump", lambda: raw)())
        requirement_key = str(item.get("requirement_key") or "").strip()
        unit = str(item.get("quantity_unit") or "").strip()
        inventory_type = item.get("inventory_type")
        quantity = item.get("reserved_quantity")
        if not requirement_key or not unit:
            raise ValueError("库存绑定的需求键与单位不能为空")
        if inventory_type not in INVENTORY_TYPES:
            raise ValueError("库存绑定类型只能是 reagent 或 current_substance")
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not math.isfinite(quantity)
            or quantity <= 0
        ):
            raise ValueError("库存绑定的预留量必须是正数")
        if requirement_key in seen:
            raise ValueError(f"库存需求 {requirement_key} 重复绑定")
        seen.add(requirement_key)
        result.append({
            "requirement_key": requirement_key,
            "inventory_type": inventory_type,
            "inventory_uuid": validate_uuid(str(item.get("inventory_uuid") or "")),
            "reserved_quantity": float(quantity),
            "quantity_unit": unit,
        })
    return result


# ── 公共帮助 ────────────────────────────────────────────────────────

import logging  # noqa: E402
import sqlite3  # noqa: E402
from uuid import NAMESPACE_URL, uuid4, uuid5  # noqa: E402

from unilabos.server.services.runtime.workflow.errors import StoreConflict, StoreNotFound  # noqa: E402
from unilabos.server.services.runtime.workflow.store import _json, _load, utc_now  # noqa: E402

logger = logging.getLogger(__name__)

TERMINAL_TASK = frozenset({"succeeded", "failed", "canceled", "timeout"})
ABNORMAL_TERMINAL_TASK = frozenset({"failed", "canceled", "timeout"})
TERMINAL_JOB = frozenset({"succeeded", "failed", "skipped", "canceled", "timeout"})
IN_FLIGHT_JOB = frozenset({"dispatched", "running", "cancel_requested"})
V1_COMMAND_TYPES = frozenset({"step", "switch_to_step", "pause", "resume", "cancel", "unlock_resources"})


def _errors():
    # 延迟导入：service 模块反向依赖本模块
    from unilabos.server.services.runtime.workflow.service import WorkflowConflict, WorkflowError

    return WorkflowError, WorkflowConflict


def _invalid(detail: str = ""):
    WorkflowError, _ = _errors()
    return WorkflowError("invalid_input", detail=detail)


def _not_found(detail: str = ""):
    WorkflowError, _ = _errors()
    return WorkflowError("not_found", detail=detail)


def _conflict(detail: str = ""):
    _, WorkflowConflict = _errors()
    return WorkflowConflict("conflict", detail=detail)


def _uuid(value: Any) -> str:
    try:
        return validate_uuid(str(value or ""))
    except ValueError:
        raise _invalid() from None


def command_row(row: sqlite3.Row) -> Dict[str, Any]:
    """V1 ``WorkflowTaskCommand`` 投影。"""

    result: Dict[str, Any] = {
        "uuid": row["uuid"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "meta_data": _load(row["meta_data"], {}),
        "workflow_task_uuid": row["workflow_task_uuid"],
        "type": row["type"],
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "result": _load(row["result"], {}),
        "trace_context": _load(row["trace_context"], {}),
    }
    for field in ("description", "target_node_uuid", "consumed_at"):
        if row[field] is not None:
            result[field] = row[field]
    return result


# ── V1 任务控制命令 ───────────────────────────────────────────────────


class TaskCommands:
    """V1 ``POST /workflow-tasks/{uuid}/commands`` 的状态机，落在 dev 的命令日志与派发闸门上。

    - 命令先按 ``(task, idempotency_key)`` 幂等登记：同键同内容重放返回原命令，同键不同
      类型 / 目标按 ``invalid_input`` 拒绝（V1 语义）；这也是 dev 防跨页面重复放行的保护。
    - 业务上不允许的命令不是接口错误：登记为 ``rejected`` 并在 ``result.reason`` 说明原因。
    - ``expected_revision``（可选）仍是 dev 的跨页面 CAS：给出且过期时 3003，不登记命令。
    - ``pause``：普通模式关派发闸门，执行模式不变，在途动作照常跑完；
      ``switch_to_step``：普通模式转单步（dev 原 ``pause`` 语义）；``step`` 在暂停的普通任务上
      先切单步再放行一个节点；``resume`` 单步转自动或重开普通任务的闸门；
      ``unlock_resources``：异常终态任务的整组资源人工解锁，须 ``meta_data.confirmed_physical_safe``
      与说明。
    """

    def __init__(self, service: Any) -> None:
        self.service = service

    def apply(
        self,
        task_uuid: str,
        *,
        command_type: str,
        target_node_uuid: Optional[str],
        idempotency_key: str,
        description: Optional[str],
        meta_data: Dict[str, Any],
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        WorkflowError, _ = _errors()
        service = self.service
        if not service.authority_profile.can_create_local_workflow_task:
            raise WorkflowError("local_task_authority_forbidden")
        task_uuid = service.get_workflow_task(task_uuid)["uuid"]
        command_type = str(command_type or "").strip()
        key = str(idempotency_key or "").strip()
        if command_type not in V1_COMMAND_TYPES or not key or len(key) > 200:
            raise _invalid()
        if target_node_uuid is not None:
            target_node_uuid = _uuid(target_node_uuid)
            if command_type != "step":
                raise _invalid("只有 step 命令可以指定目标节点")
        try:
            meta_data = normalize_json_object(meta_data)
        except ValueError:
            raise _invalid() from None
        description = (str(description).strip() or None) if description is not None else None
        if command_type == "unlock_resources" and (
            meta_data.get("confirmed_physical_safe") is not True or description is None
        ):
            raise _invalid("解除任务资源须确认现场安全并填写说明")
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0
        ):
            raise _invalid()

        unlock_jobs: List[str] = []
        with service.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM workflow_task_command WHERE workflow_task_uuid=? AND idempotency_key=? "
                "AND deleted_at IS NULL",
                (task_uuid, key),
            ).fetchone()
            if existing is not None:
                # 单步许可被认领时 dev 会把实际节点写回 target_node_uuid，因此只比较请求显式给出的目标
                if existing["type"] != command_type or (
                    target_node_uuid is not None and existing["target_node_uuid"] not in (None, target_node_uuid)
                ):
                    raise _invalid("同一任务控制幂等键对应的命令内容已变化")
                return command_row(existing)
            task = service.get_task(task_uuid)
            if expected_revision is not None and task["control_revision"] != expected_revision:
                raise _conflict("任务控制版本已变化，请刷新后重试")
            now = utc_now()
            outcome = self._decide(conn, task, command_type, target_node_uuid, key, now)
            status, reason = outcome["status"], outcome.get("reason")
            if outcome.get("inserted"):
                # dev 内部步骤（暂停 / 取消）已登记同键命令行，这里补齐 V1 字段
                conn.execute(
                    "UPDATE workflow_task_command SET type=?, description=?, meta_data=?, result=?, update_time=? "
                    "WHERE workflow_task_uuid=? AND idempotency_key=? AND deleted_at IS NULL",
                    (command_type, description, _json(meta_data), _json(outcome.get("result") or {}), now,
                     task_uuid, key),
                )
            else:
                result = dict(outcome.get("result") or {})
                if reason:
                    result["reason"] = reason
                conn.execute(
                    "INSERT INTO workflow_task_command(uuid, create_time, update_time, description, meta_data, "
                    "workflow_task_uuid, type, target_node_uuid, idempotency_key, status, result, consumed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), now, now, description, _json(meta_data), task_uuid, command_type,
                     target_node_uuid if command_type == "step" else None, key, status, _json(result),
                     None if status == "pending" else now),
                )
            if status != "rejected":
                service._append_event(conn, event="workflow.task.changed",
                                      data={"workflow_task_uuid": task_uuid, "command": command_type}, now=now)
            unlock_jobs = list(outcome.get("unlock_jobs") or [])
            row = conn.execute(
                "SELECT * FROM workflow_task_command WHERE workflow_task_uuid=? AND idempotency_key=? "
                "AND deleted_at IS NULL",
                (task_uuid, key),
            ).fetchone()
            command = command_row(row)
        if command["status"] != "rejected":
            self._after_commit(task_uuid, command_type, unlock_jobs)
        return command

    # 状态机 ---------------------------------------------------------------

    def _decide(
        self, conn: sqlite3.Connection, task: Dict[str, Any], command_type: str,
        target_node_uuid: Optional[str], key: str, now: str,
    ) -> Dict[str, Any]:
        service = self.service
        status = task["status"]
        mode = "step" if task["run_mode"] == "step" else "normal"
        gate = task.get("dispatch_gate") or "active"
        control = task["control_status"]
        task_uuid = task["uuid"]

        def rejected(reason: str) -> Dict[str, Any]:
            return {"status": "rejected", "reason": reason}

        if command_type == "unlock_resources":
            if status not in ABNORMAL_TERMINAL_TASK:
                return rejected("task_is_not_abnormal_terminal")
            return self._unlock_resources(conn, task_uuid, now)
        if command_type == "cancel":
            if status == "canceling":
                return {"status": "succeeded", "result": {"status": "canceling"}}
            if status not in {"pending", "running"}:
                return rejected("task_is_terminal")
            service._apply_cancel(conn, task, idempotency_key=key,
                                  expected_revision=task["control_revision"], now=now)
            return {"status": "succeeded", "inserted": True, "result": {"status": "canceling"}}
        if status not in {"pending", "running"}:
            return rejected("task_is_terminal")
        if task["execution_kind"] != "workflow":
            return rejected("only_workflow_tasks_accept_execution_control")
        if command_type == "pause":
            if mode != "normal":
                return rejected("task_is_not_in_normal_mode")
            self._set_gate(conn, task, "paused", now)
            return {"status": "succeeded", "result": {"dispatch_gate": "paused"}}
        if command_type == "switch_to_step":
            if mode != "normal":
                return rejected("task_is_not_in_normal_mode")
            try:
                service._apply_pause(conn, task, idempotency_key=key,
                                     expected_revision=task["control_revision"], now=now)
            except StoreConflict as exc:
                return rejected(str(exc))
            conn.execute("UPDATE workflow_task SET dispatch_gate='active' WHERE uuid=?", (task_uuid,))
            return {"status": "succeeded", "inserted": True, "result": {"execution_mode": "step"}}
        if command_type == "resume":
            if mode == "normal":
                if gate != "paused":
                    return rejected("task_is_not_paused")
                self._set_gate(conn, task, "active", now)
                return {"status": "succeeded", "result": {"dispatch_gate": "active"}}
            # V1 只在稳定暂停时恢复；dev 还允许单步执行中直接转自动（收拢未用的许可），保留
            if control not in {"paused", "active"}:
                return rejected("task_is_not_paused_step")
            blocker = self._unresolved_node(conn, task_uuid)
            if blocker:
                return rejected(blocker)
            conn.execute(
                "UPDATE workflow_task_command SET status='succeeded', consumed_at=?, update_time=? "
                "WHERE workflow_task_uuid=? AND status='pending' AND type='step' AND deleted_at IS NULL",
                (now, now, task_uuid),
            )
            conn.execute(
                "UPDATE workflow_task SET run_mode='normal', control_status='active', dispatch_gate='active', "
                "update_time=? WHERE uuid=?",
                (now, task_uuid),
            )
            return {"status": "succeeded", "result": {"execution_mode": "normal"}}
        # step
        if mode == "normal":
            if not (gate == "paused" and control == "paused"):
                return rejected("task_is_not_paused_step")
            if self._in_flight(conn, task_uuid):
                return rejected("step_in_progress")
            # 暂停中的普通任务也可单步：先切到单步，再只放行一个节点
            conn.execute(
                "UPDATE workflow_task SET run_mode='step', dispatch_gate='active', update_time=? WHERE uuid=?",
                (now, task_uuid),
            )
        elif control != "paused":
            return rejected("task_is_not_paused_step")
        blocker = self._unresolved_node(conn, task_uuid)
        if blocker:
            return rejected(blocker)
        if target_node_uuid is not None and conn.execute(
            "SELECT 1 FROM workflow_node_run WHERE workflow_task_uuid=? AND workflow_node_uuid=? "
            "AND deleted_at IS NULL AND status='pending'",
            (task_uuid, target_node_uuid),
        ).fetchone() is None:
            return rejected("target_node_not_ready")
        conn.execute(
            "UPDATE workflow_task SET control_status='active', update_time=? WHERE uuid=?",
            (now, task_uuid),
        )
        # dev 单步许可：pending 命令由下一次派发认领（claim_task_step），认领后才算完成
        return {"status": "pending"}

    @staticmethod
    def _set_gate(conn: sqlite3.Connection, task: Dict[str, Any], gate: str, now: str) -> None:
        control = task["control_status"]
        if control in {"active", "paused"}:
            conn.execute(
                "UPDATE workflow_task SET dispatch_gate=?, control_status=?, update_time=? WHERE uuid=?",
                (gate, gate, now, task["uuid"]),
            )
        elif control == "waiting_reconciliation":
            # 等待对账时保留更高优先级的控制态，恢复目标记为新的闸门意图
            conn.execute(
                "UPDATE workflow_task SET dispatch_gate=?, reconciliation_resume_control_status=?, update_time=? "
                "WHERE uuid=?",
                (gate, gate, now, task["uuid"]),
            )
        else:
            conn.execute(
                "UPDATE workflow_task SET dispatch_gate=?, update_time=? WHERE uuid=?",
                (gate, now, task["uuid"]),
            )

    @staticmethod
    def _in_flight(conn: sqlite3.Connection, task_uuid: str) -> int:
        return int(conn.execute(
            "SELECT COUNT(*) FROM workflow_node_job WHERE workflow_task_uuid=? AND deleted_at IS NULL "
            "AND status IN ('dispatched', 'running', 'cancel_requested')",
            (task_uuid,),
        ).fetchone()[0])

    @staticmethod
    def _unresolved_node(conn: sqlite3.Connection, task_uuid: str) -> Optional[str]:
        if conn.execute(
            "SELECT 1 FROM workflow_node_run WHERE workflow_task_uuid=? AND deleted_at IS NULL "
            "AND status NOT IN ('succeeded', 'skipped', 'failed', 'timeout', 'canceled') LIMIT 1",
            (task_uuid,),
        ).fetchone() is None:
            return "all_nodes_finished"
        if conn.execute(
            "SELECT 1 FROM workflow_node_run WHERE workflow_task_uuid=? AND deleted_at IS NULL AND status IN "
            "('execution_unknown', 'intervention_required', 'cancel_requested', 'failed', 'timeout', 'canceled') "
            "LIMIT 1",
            (task_uuid,),
        ).fetchone() is not None:
            return "current_node_requires_resolution"
        return None

    def _unlock_resources(self, conn: sqlite3.Connection, task_uuid: str, now: str) -> Dict[str, Any]:
        """异常终态任务的整组人工解锁：执行 Claim、区间占用、设备托管一并释放。"""

        unsettled = conn.execute(
            "SELECT COUNT(*) FROM workflow_node_job WHERE workflow_task_uuid=? AND deleted_at IS NULL AND "
            "(status IN ('execution_unknown', 'intervention_required', 'dispatched', 'running', 'cancel_requested') "
            "OR COALESCE(uncertainty_reason, '') <> '')",
            (task_uuid,),
        ).fetchone()[0]
        if unsettled:
            return {"status": "rejected", "reason": "job_outcome_unsettled"}
        claims = conn.execute(
            "SELECT claim_uuid, job_uuid, state FROM workflow_execution_claim WHERE task_uuid=? AND state!='released'",
            (task_uuid,),
        ).fetchall()
        if any(row["state"] == "uncertain" for row in claims):
            return {"status": "rejected", "reason": "claim_outcome_uncertain"}
        for row in claims:
            conn.execute("DELETE FROM workflow_execution_lease WHERE claim_uuid=?", (row["claim_uuid"],))
            conn.execute(
                "UPDATE workflow_execution_claim SET state='released', released_at=? WHERE claim_uuid=?",
                (now, row["claim_uuid"]),
            )
        intervals = conn.execute(
            "UPDATE workflow_resource_interval SET state='released' WHERE task_uuid=? AND state!='released'",
            (task_uuid,),
        ).rowcount
        tenancies = conn.execute(
            "UPDATE workflow_device_tenancy SET state='released', released_at=?, release_reason='unlock_resources', "
            "version=version+1 WHERE task_uuid=? AND state!='released'",
            (now, task_uuid),
        ).rowcount
        conn.execute(
            "UPDATE workflow_task SET cleanup_status='settled', update_time=? WHERE uuid=? "
            "AND cleanup_status IN ('pending', 'canceling', 'requires_attention')",
            (now, task_uuid),
        )
        cleanup = conn.execute("SELECT cleanup_status FROM workflow_task WHERE uuid=?", (task_uuid,)).fetchone()[0]
        return {
            "status": "succeeded",
            "result": {
                "cleanup_status": cleanup,
                "released": {"claims": len(claims), "intervals": intervals, "device_tenancies": tenancies},
            },
            "unlock_jobs": [row["job_uuid"] for row in claims],
        }

    def _after_commit(self, task_uuid: str, command_type: str, unlock_jobs: List[str]) -> None:
        service = self.service
        if command_type == "unlock_resources":
            releaser = getattr(service, "_task_resource_releaser", None)
            if releaser is not None:
                try:
                    releaser(task_uuid, unlock_jobs)
                except Exception:  # noqa: BLE001 - 持久事实已释放，调度器恢复时重新对账
                    logger.exception("任务 %s 资源已人工解锁，调度器内存占用释放失败", task_uuid)
            listener = getattr(service, "_resource_change_listener", None)
            if listener is not None:
                try:
                    listener()
                except Exception:  # noqa: BLE001
                    logger.exception("任务 %s 资源已人工解锁，等待调度器恢复", task_uuid)
            return
        controller = getattr(service, "_task_controller", None)
        if controller is not None:
            try:
                controller(task_uuid)
            except Exception:  # noqa: BLE001 - 命令已落库，执行器恢复时重新消费
                logger.exception("任务 %s 控制已持久化，等待调度器恢复", task_uuid)


# ── V1 作业投影 ─────────────────────────────────────────────────────


def enrich_jobs(service: Any, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """dev attempt 行补齐 V1 ``WorkflowNodeJob`` 字段（节点运行上的定义列 + 人工确认）。

    ``topological_index`` / ``executor_kind`` / ``execution_policy`` / ``execution_timeout_seconds`` /
    ``material_uuid`` 来自节点运行；``attempt`` = ``attempt_no``；``current_attempt`` 表示它是该节点
    运行当前的 attempt；``start_state`` 按是否已有开始时间给出 V1 的两态；dev 字段原样保留。
    """

    if not jobs:
        return []
    run_uuids = sorted({str(job["workflow_node_run_uuid"]) for job in jobs})
    job_uuids = [str(job["uuid"]) for job in jobs]
    marks = ",".join("?" for _ in run_uuids)
    job_marks = ",".join("?" for _ in job_uuids)
    with service.write_lock:
        conn = service.connection
        runs = {
            row["uuid"]: row
            for row in conn.execute(
                f"SELECT uuid, topological_index, executor_kind, execution_policy, execution_timeout_seconds, "
                f"material_uuid, current_job_uuid FROM workflow_node_run WHERE uuid IN ({marks})",
                run_uuids,
            ).fetchall()
        }
        confirmations = {
            row["workflow_node_job_uuid"]: service._manual_confirmation_row(row)
            for row in conn.execute(
                f"SELECT * FROM workflow_manual_confirmation WHERE workflow_node_job_uuid IN ({job_marks}) "
                f"AND deleted_at IS NULL",
                job_uuids,
            ).fetchall()
        }
    result = []
    for job in jobs:
        run = runs.get(str(job["workflow_node_run_uuid"]))
        enriched = dict(job)
        if run is not None:
            enriched.update({
                "topological_index": run["topological_index"],
                "executor_kind": run["executor_kind"],
                "execution_policy": _load(run["execution_policy"], {}),
                "execution_timeout_seconds": run["execution_timeout_seconds"],
                "current_attempt": run["current_job_uuid"] == job["uuid"],
            })
            if run["material_uuid"] is not None:
                enriched["material_uuid"] = run["material_uuid"]
        enriched["attempt"] = job["attempt_no"]
        enriched["start_state"] = "START_COMMITTED" if job.get("started_at") else "NOT_STARTED"
        enriched.setdefault("execution_source", str((job.get("control_data") or {}).get("execution_source") or "workflow"))
        enriched.setdefault("wait_reason", {})
        enriched.setdefault("expected_change_set", {})
        confirmation = confirmations.get(str(job["uuid"]))
        if confirmation is not None:
            enriched["manual_confirmation"] = confirmation
        result.append(enriched)
    return result

