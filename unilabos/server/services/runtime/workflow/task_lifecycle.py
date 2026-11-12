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

from unilabos.server.services.runtime.workflow.errors import StoreConflict  # noqa: E402
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



# ── 任务观测：展示投影、删除、单步候选、执行锁、运行日志、等待图 ─────────────


def _node_index(task: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """执行计划节点 + 图快照里的名称 / 动作 / 设备，合成 V1 控制台需要的紧凑节点。"""

    snapshot_nodes = {
        str(node.get("uuid")): node
        for node in (task.get("workflow_snapshot") or {}).get("nodes") or []
        if isinstance(node, dict)
    }
    result: Dict[str, Dict[str, Any]] = {}
    for planned in (task.get("execution_plan") or {}).get("nodes") or []:
        if not isinstance(planned, dict):
            continue
        uuid = str(planned.get("uuid"))
        source = snapshot_nodes.get(uuid, {})
        meta = source.get("meta_data") if isinstance(source.get("meta_data"), dict) else {}
        result[uuid] = {
            "uuid": uuid,
            "name": source.get("name") or planned.get("name") or uuid,
            "kind": planned.get("kind") or source.get("type") or "device_action",
            "action_name": source.get("action_name") or planned.get("action_name"),
            "device_id": meta.get("target_device_id") or planned.get("device_id"),
            "material_uuid": planned.get("material_uuid") or source.get("material_uuid"),
            "topological_index": planned.get("topological_index"),
        }
    return result


def _compact_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """V1 ``_task_presentation_row``：裁掉完整图与参数，只留矩阵绘制所需字段。"""

    workflow = (task.get("workflow_snapshot") or {}).get("workflow") or {}
    plan = task.get("execution_plan") or {}
    nodes = sorted(_node_index(task).values(), key=lambda node: (node["topological_index"] is None,
                                                                node["topological_index"] or 0))
    compact = {
        key: task[key]
        for key in (
            "uuid", "create_time", "update_time", "meta_data", "description", "workflow_uuid", "execution_kind",
            "priority", "status", "run_mode", "execution_mode", "dispatch_gate", "control_status",
            "cleanup_status", "wait_reason", "trace_context", "input", "error_info", "attention_reason",
            "started_at", "finished_at", "target_node_uuid",
        )
        if key in task
    }
    compact["workflow_snapshot"] = {"workflow": {key: workflow.get(key) for key in ("uuid", "name", "revision")}}
    compact["execution_plan"] = {
        "run_mode": plan.get("run_mode"),
        "target_node_uuid": plan.get("target_node_uuid"),
        "nodes": nodes,
        "edges": [
            {"source_node_uuid": edge.get("source_node_uuid"), "target_node_uuid": edge.get("target_node_uuid")}
            for edge in plan.get("edges") or []
            if isinstance(edge, dict)
        ],
    }
    return compact


_COMPACT_JOB_FIELDS = (
    "uuid", "create_time", "update_time", "workflow_task_uuid", "workflow_node_uuid", "workflow_node_run_uuid",
    "status", "attempt", "attempt_no", "current_attempt", "topological_index", "executor_kind", "start_state",
    "execution_source", "uncertainty_reason", "started_at", "finished_at", "wait_reason", "error_info",
    "manual_confirmation", "material_uuid",
)


class TaskObservability:
    """V1 任务观测与删除接口的读写实现（runtime.db 同一连接与写锁）。"""

    def __init__(self, service: Any) -> None:
        self.service = service

    # 展示投影 -------------------------------------------------------------

    def presentations(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        workflow_uuid: Optional[str] = None,
        execution_kind: str = "",
        status: str = "",
        cleanup_status: str = "",
        view: str = "",
        terminal_limit: int = 20,
    ) -> Dict[str, Any]:
        service = self.service
        view = (view or "").strip().lower()
        if view not in {"", "matrix"}:
            raise _invalid()
        if view == "matrix":
            if status or cleanup_status or page != 1:
                raise _invalid("矩阵视图不接受状态筛选与翻页")
            if isinstance(terminal_limit, bool) or not 0 <= terminal_limit <= 100:
                raise _invalid()
            listing = self._matrix(workflow_uuid=workflow_uuid, execution_kind=execution_kind,
                                   terminal_limit=terminal_limit)
        else:
            listing = service.list_workflow_tasks(
                page=page, page_size=page_size, workflow_uuid=workflow_uuid,
                execution_kind=execution_kind, status=status, cleanup_status=cleanup_status,
            )
        items = []
        for task in listing["items"]:
            jobs = enrich_jobs(service, service.list_jobs(task["uuid"]))
            compact = _compact_task(task)
            compact["jobs"] = [{key: job[key] for key in _COMPACT_JOB_FIELDS if key in job} for job in jobs]
            items.append(compact)
        return {**listing, "items": items}

    def _matrix(self, *, workflow_uuid: Optional[str], execution_kind: str, terminal_limit: int) -> Dict[str, Any]:
        service = self.service
        if workflow_uuid is not None:
            workflow_uuid = _uuid(workflow_uuid)
        execution_kind = (execution_kind or "").strip().lower()
        if execution_kind and execution_kind not in {"workflow", "ad_hoc_device_action"}:
            raise _invalid()
        filters, values = ["deleted_at IS NULL"], []
        if workflow_uuid:
            filters.append("workflow_uuid = ?")
            values.append(workflow_uuid)
        if execution_kind:
            filters.append("execution_kind = ?")
            values.append(execution_kind)
        where = " AND ".join(filters)
        with service.write_lock:
            rows = service.connection.execute(
                f"""
                SELECT * FROM workflow_task WHERE {where} AND (
                    status IN ('pending', 'running', 'canceling') OR cleanup_status = 'requires_attention'
                    OR uuid IN (
                        SELECT uuid FROM workflow_task WHERE {where}
                          AND cleanup_status <> 'requires_attention'
                          AND status IN ('succeeded', 'failed', 'canceled', 'timeout')
                        ORDER BY COALESCE(finished_at, update_time, create_time) DESC, uuid DESC LIMIT ?
                    )
                ) ORDER BY create_time DESC, uuid
                """,
                (*values, *values, terminal_limit),
            ).fetchall()
            items = [service._task_row(row) for row in rows]
        return {"items": items, "total": len(items), "page": 1, "page_size": len(items), "has_more": False}

    # 删除 -----------------------------------------------------------------

    @staticmethod
    def _eligibility(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
        task_uuid = str(row["uuid"])
        status = str(row["status"] or "")
        cleanup = str(row["cleanup_status"] or "none")
        if row["deleted_at"] is not None:
            return {"can_delete": False, "already_deleted": True, "delete_blockers": [], "status": status,
                    "cleanup_status": cleanup, "deleted_at": row["deleted_at"]}
        blockers: List[str] = []
        if status not in TERMINAL_TASK:
            blockers.append(f"任务状态为 {status or 'unknown'}，尚未进入终态")
        if cleanup not in {"none", "settled"}:
            blockers.append(f"任务清理状态为 {cleanup}，资源尚未完全收敛")

        def count(sql: str) -> int:
            return int(conn.execute(sql, (task_uuid,)).fetchone()[0])

        checks = (
            ("SELECT COUNT(*) FROM workflow_node_job WHERE workflow_task_uuid=? AND deleted_at IS NULL "
             "AND status NOT IN ('succeeded', 'failed', 'skipped', 'canceled', 'timeout')", "个 Job 未进入终态"),
            ("SELECT COUNT(*) FROM workflow_node_job WHERE workflow_task_uuid=? AND deleted_at IS NULL "
             "AND COALESCE(uncertainty_reason, '') <> ''", "个 Job 存在结果不确定原因"),
            ("SELECT COUNT(*) FROM workflow_execution_claim WHERE task_uuid=? AND state!='released'",
             "个执行 Claim 未释放"),
            ("SELECT COUNT(*) FROM workflow_resource_interval WHERE task_uuid=? AND state!='released'",
             "个区间资源占用未释放"),
            ("SELECT COUNT(*) FROM workflow_resource_waiter WHERE task_uuid=?", "个资源锁请求在等待"),
            ("SELECT COUNT(*) FROM workflow_device_tenancy WHERE task_uuid=? AND state!='released'",
             "个设备托管未释放"),
            ("SELECT COUNT(*) FROM workflow_task_command WHERE workflow_task_uuid=? AND deleted_at IS NULL "
             "AND status='pending'", "个任务控制命令待处理"),
            ("SELECT COUNT(*) FROM workflow_intervention WHERE workflow_task_uuid=? AND deleted_at IS NULL "
             "AND status IN ('open', 'selected')", "个人工干预未关闭"),
            ("SELECT COUNT(*) FROM workflow_manual_confirmation WHERE workflow_task_uuid=? AND deleted_at IS NULL "
             "AND status='pending'", "个人工确认未完成"),
            ("SELECT COUNT(*) FROM workflow_human_interaction WHERE task_uuid=? AND status='pending'",
             "个人工交互未完成"),
        )
        for sql, label in checks:
            amount = count(sql)
            if amount:
                blockers.append(f"仍有 {amount} {label}")
        return {"can_delete": not blockers, "already_deleted": False, "delete_blockers": blockers,
                "status": status, "cleanup_status": cleanup, "deleted_at": None}

    def delete_eligibility(self, task_uuid: str) -> Dict[str, Any]:
        identity = _uuid(task_uuid)
        service = self.service
        with service.write_lock:
            row = service.connection.execute("SELECT * FROM workflow_task WHERE uuid=?", (identity,)).fetchone()
            if row is None:
                raise _not_found()
            result = self._eligibility(service.connection, row)
        return {"workflow_task_uuid": identity, **result}

    def delete(self, task_uuid: str) -> Dict[str, Any]:
        """软删除已终态且资源全部收敛的任务；重复请求返回 ``already_deleted``。"""

        identity = _uuid(task_uuid)
        service = self.service
        with service.transaction() as conn:
            row = conn.execute("SELECT * FROM workflow_task WHERE uuid=?", (identity,)).fetchone()
            if row is None:
                raise _not_found()
            eligibility = self._eligibility(conn, row)
            if eligibility["already_deleted"]:
                return {"status": "already_deleted", "workflow_task_uuid": identity,
                        "deleted_at": eligibility["deleted_at"]}
            if not eligibility["can_delete"]:
                raise _conflict("任务当前不能删除：" + "；".join(eligibility["delete_blockers"]))
            now = utc_now()
            conn.execute("UPDATE workflow_task SET deleted_at=?, update_time=? WHERE uuid=? AND deleted_at IS NULL",
                         (now, now, identity))
            service._append_event(conn, event="workflow.task.changed",
                                  data={"workflow_task_uuid": identity, "event": "task_deleted"}, now=now)
        return {"status": "deleted", "workflow_task_uuid": identity, "deleted_at": now}

    # 单步候选 -------------------------------------------------------------

    def step_state(self, task_uuid: str) -> Dict[str, Any]:
        """V1 单步候选：暂停且无在途动作时，前置依赖都已完成的待运行节点。"""

        service = self.service
        task = service.get_workflow_task(task_uuid)
        execution_mode = task["execution_mode"]
        base = {"workflow_task_uuid": task["uuid"], "execution_mode": execution_mode,
                "control_status": task["control_status"]}
        if task["status"] in TERMINAL_TASK or task["status"] == "canceling":
            return {**base, "in_flight_job_count": 0, "requires_selection": False, "can_step": False,
                    "candidates": []}
        with service.write_lock:
            conn = service.connection
            in_flight = TaskCommands._in_flight(conn, task["uuid"])
            runs = {
                row["workflow_node_uuid"]: row["status"]
                for row in conn.execute(
                    "SELECT workflow_node_uuid, status FROM workflow_node_run WHERE workflow_task_uuid=? "
                    "AND deleted_at IS NULL",
                    (task["uuid"],),
                ).fetchall()
            }
        paused = task["control_status"] == "paused" and (
            execution_mode == "step" or task["dispatch_gate"] == "paused"
        )
        candidates: List[Dict[str, Any]] = []
        if paused and not in_flight:
            nodes = _node_index(task)
            depends: Dict[str, List[str]] = {}
            for planned in (task.get("execution_plan") or {}).get("nodes") or []:
                policy = planned.get("execution_policy") if isinstance(planned.get("execution_policy"), dict) else {}
                depends[str(planned.get("uuid"))] = [str(item) for item in policy.get("depends_on") or []]
            for edge in (task.get("execution_plan") or {}).get("edges") or []:
                if isinstance(edge, dict) and edge.get("target_node_uuid"):
                    depends.setdefault(str(edge["target_node_uuid"]), []).append(str(edge.get("source_node_uuid")))
            for uuid, node in sorted(nodes.items(), key=lambda item: item[1]["topological_index"] or 0):
                if runs.get(uuid) != "pending":
                    continue
                if any(runs.get(parent) not in {"succeeded", "skipped"} for parent in depends.get(uuid, [])
                       if parent in runs):
                    continue
                candidates.append({
                    "node_uuid": uuid,
                    "name": str(node["name"]),
                    "kind": str(node["kind"]),
                    "device_id": str(node["device_id"] or ""),
                    "action_name": str(node["action_name"] or ""),
                })
        return {**base, "in_flight_job_count": in_flight, "requires_selection": len(candidates) > 1,
                "can_step": bool(candidates), "candidates": candidates}

    # 执行锁 ---------------------------------------------------------------

    @staticmethod
    def lease_uuid(claim_uuid: str, lock_key: str) -> str:
        """dev 的租约以 lock_key 为主键；对外给 V1 需要的稳定租约 uuid（claim + lock_key 派生）。"""

        return str(uuid5(NAMESPACE_URL, f"unilab:execution-lease:{claim_uuid}:{lock_key}"))

    def _leases(self, conn: sqlite3.Connection, task_uuid: str) -> List[Dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT claim.claim_uuid, claim.job_uuid, claim.task_uuid, claim.state AS claim_state,
                   claim.acquired_at, claim.released_at, claim.fences, lease.lock_key, lease.token,
                   job.status AS job_status, job.uncertainty_reason, job.workflow_node_uuid,
                   run.material_uuid
            FROM workflow_execution_claim AS claim
            JOIN workflow_execution_lease AS lease ON lease.claim_uuid = claim.claim_uuid
            JOIN workflow_node_job AS job ON job.uuid = claim.job_uuid
            LEFT JOIN workflow_node_run AS run ON run.uuid = job.workflow_node_run_uuid
            WHERE claim.task_uuid = ? AND claim.state != 'released'
            ORDER BY claim.acquired_at, lease.lock_key
            """,
            (task_uuid,),
        ).fetchall()
        return [
            {
                "uuid": self.lease_uuid(row["claim_uuid"], row["lock_key"]),
                "lock_key": row["lock_key"],
                "material_uuid": row["material_uuid"],
                "workflow_task_uuid": row["task_uuid"],
                "workflow_node_job_uuid": row["job_uuid"],
                "workflow_node_uuid": row["workflow_node_uuid"],
                "state": row["claim_state"],
                "claim_uuid": row["claim_uuid"],
                "fencing_token": int(row["token"]),
                "acquired_at": row["acquired_at"],
                "job_status": row["job_status"],
                "claim_state": row["claim_state"],
                "_uncertainty": str(row["uncertainty_reason"] or ""),
            }
            for row in rows
        ]

    @staticmethod
    def _block_reason(task_status: str, lease: Dict[str, Any], tenancies: int) -> Optional[str]:
        if task_status not in ABNORMAL_TERMINAL_TASK:
            return "只有失败、取消或超时任务才能人工释放执行锁"
        if lease["job_status"] not in {"failed", "canceled", "timeout"}:
            return "只有终态作业才能人工释放执行锁"
        if lease["_uncertainty"]:
            return "作业仍有结果不确定原因，不能人工释放执行锁"
        if lease["claim_state"] == "uncertain":
            return "Claim 处于结果不确定状态，必须先完成物理结算"
        if tenancies:
            return "任务仍有活动设备托管，不能人工释放执行锁"
        return None

    def execution_locks(self, task_uuid: str) -> Dict[str, Any]:
        service = self.service
        task = service.get_workflow_task(task_uuid)
        with service.write_lock:
            conn = service.connection
            leases = self._leases(conn, task["uuid"])
            tenancies = int(conn.execute(
                "SELECT COUNT(*) FROM workflow_device_tenancy WHERE task_uuid=? AND state!='released'",
                (task["uuid"],),
            ).fetchone()[0])
        locks = []
        for lease in leases:
            reason = self._block_reason(task["status"], lease, tenancies)
            lease.pop("_uncertainty")
            locks.append({**lease, "can_release": reason is None, "release_block_reason": reason})
        return {"workflow_task_uuid": task["uuid"], "task_status": task["status"], "locks": locks,
                "active_device_tenancy_count": tenancies}

    def force_release(
        self,
        task_uuid: str,
        lease_uuid: str,
        *,
        expected_claim_uuid: str,
        expected_fencing_token: int,
        reason: str,
        physical_settlement_confirmed: bool,
        operator_id: str = "",
    ) -> Dict[str, Any]:
        """确认现场安全且 Claim / Fence CAS 一致后，释放目标作业的全部执行锁。"""

        service = self.service
        task_identity = service.get_workflow_task(task_uuid)["uuid"]
        lease_identity = _uuid(lease_uuid)
        expected_claim = _uuid(expected_claim_uuid)
        reason = str(reason or "").strip()
        if (isinstance(expected_fencing_token, bool) or not isinstance(expected_fencing_token, int)
                or expected_fencing_token <= 0 or not reason or len(reason) > 500):
            raise _invalid()
        job_uuid = ""
        with service.transaction() as conn:
            task = service.get_task(task_identity)
            leases = self._leases(conn, task_identity)
            lease = next((item for item in leases if item["uuid"] == lease_identity), None)
            if lease is None:
                released = conn.execute(
                    "SELECT claim_uuid FROM workflow_execution_claim WHERE task_uuid=? AND state='released' "
                    "AND claim_uuid=?",
                    (task_identity, expected_claim),
                ).fetchone()
                if released is not None:
                    return {"status": "already_released", "workflow_task_uuid": task_identity,
                            "lease_uuid": lease_identity, "claim_uuid": expected_claim}
                raise _not_found(f"执行锁租约不存在：{lease_identity}")
            tenancies = int(conn.execute(
                "SELECT COUNT(*) FROM workflow_device_tenancy WHERE task_uuid=? AND state!='released'",
                (task_identity,),
            ).fetchone()[0])
            block = self._block_reason(task["status"], lease, tenancies)
            if block:
                raise _conflict(block)
            if not physical_settlement_confirmed:
                raise _conflict("必须确认设备已停止且物理现场已安全")
            if lease["claim_uuid"] != expected_claim:
                raise _conflict("Claim 已变化，请刷新任务锁列表后重试")
            if lease["fencing_token"] != expected_fencing_token:
                raise _conflict("Fence 已变化，请刷新任务锁列表后重试")
            job_uuid = lease["workflow_node_job_uuid"]
            released_locks = [item["uuid"] for item in leases if item["claim_uuid"] == lease["claim_uuid"]]
            now = utc_now()
            conn.execute("DELETE FROM workflow_execution_lease WHERE claim_uuid=?", (lease["claim_uuid"],))
            conn.execute("UPDATE workflow_execution_claim SET state='released', released_at=? WHERE claim_uuid=?",
                         (now, lease["claim_uuid"]))
            service._append_event(conn, event="workflow.resource.changed", data={
                "workflow_task_uuid": task_identity, "operation": "force_release_execution_lock",
                "workflow_node_job_uuid": job_uuid, "claim_uuid": lease["claim_uuid"], "reason": reason,
                "operator_id": operator_id or "local-workspace", "released_lock_uuids": released_locks,
            }, now=now)
        releaser = getattr(service, "_task_resource_releaser", None)
        if releaser is not None:
            try:
                releaser(task_identity, [job_uuid])
            except Exception:  # noqa: BLE001 - 持久事实已释放
                logger.exception("执行锁已人工释放，调度器内存占用释放失败")
        listener = getattr(service, "_resource_change_listener", None)
        if listener is not None:
            try:
                listener()
            except Exception:  # noqa: BLE001
                logger.exception("执行锁已人工释放，等待调度器恢复")
        return {"status": "released", "workflow_task_uuid": task_identity, "lease_uuid": lease_identity,
                "claim_uuid": expected_claim, "workflow_node_job_uuid": job_uuid,
                "released_lock_uuids": released_locks, "reason": reason}

    # 运行日志 -------------------------------------------------------------

    _EVENT_KINDS = {
        "workflow.task.changed": "task_transition",
        "workflow.node_run.changed": "node_run_transition",
        "workflow.node_job.changed": "job_transition",
        "workflow.manual_confirmation.changed": "manual_confirmation",
        "workflow.resource.changed": "resource",
    }

    def task_events(self, task_uuid: str, *, after_sequence: int = 0, limit: int = 100) -> Dict[str, Any]:
        """任务运行日志（V1 ``after_sequence`` 游标），由持久前端通知按任务归集。"""

        service = self.service
        task_identity = service.get_workflow_task(task_uuid)["uuid"]
        if (isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500):
            raise _invalid()
        with service.write_lock:
            rows = service.connection.execute(
                """
                SELECT event.sequence, event.type, event.payload, event.create_time,
                       job.uuid AS job_uuid, job.workflow_node_uuid, job.attempt_no, job.param,
                       job.return_info, job.error_info, run.executor_kind
                FROM frontend_event AS event
                LEFT JOIN workflow_node_job AS job
                  ON job.uuid = json_extract(event.payload, '$.workflow_node_job_uuid')
                LEFT JOIN workflow_node_run AS run ON run.uuid = job.workflow_node_run_uuid
                WHERE event.sequence > ? AND (
                    json_extract(event.payload, '$.workflow_task_uuid') = ? OR job.workflow_task_uuid = ?
                    OR (event.aggregate_uuid = ? AND event.type LIKE 'workflow.%')
                )
                ORDER BY event.sequence LIMIT ?
                """,
                (after_sequence, task_identity, task_identity, task_identity, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        items = []
        for row in selected:
            data = _load(row["payload"], {})
            kind = self._EVENT_KINDS.get(row["type"], row["type"])
            item: Dict[str, Any] = {
                "sequence": row["sequence"], "workflow_task_uuid": task_identity, "kind": kind,
                "data": data, "create_time": row["create_time"],
            }
            if row["job_uuid"] is not None:
                item.update({"workflow_node_job_uuid": row["job_uuid"],
                             "workflow_node_uuid": row["workflow_node_uuid"], "attempt": row["attempt_no"]})
                if row["executor_kind"] is not None:
                    item["executor_kind"] = row["executor_kind"]
            to_status = data.get("status") if isinstance(data, dict) else None
            if to_status:
                item["to_status"] = to_status
            if kind == "job_transition" and to_status in {"dispatched", "running"} and row["param"] is not None:
                item["param"] = _load(row["param"], {})
            if kind == "job_transition" and to_status in TERMINAL_JOB and row["job_uuid"] is not None:
                item["return_info"] = _load(row["return_info"], {})
                item["error_info"] = _load(row["error_info"], [])
            items.append(item)
        return {"items": items, "next_cursor": selected[-1]["sequence"] if selected else after_sequence,
                "has_more": len(rows) > limit}

    # 等待图 ---------------------------------------------------------------

    def wait_graph(self) -> Dict[str, Any]:
        """整站作业等待图：持久等待记录（等锁的作业 → 持锁的作业）及环路诊断。"""

        service = self.service
        with service.write_lock:
            conn = service.connection
            waiters = conn.execute(
                """
                SELECT waiter.job_uuid, waiter.task_uuid, waiter.lock_keys, waiter.blockers
                FROM workflow_resource_waiter AS waiter
                JOIN workflow_node_job AS job ON job.uuid = waiter.job_uuid
                JOIN workflow_task AS task ON task.uuid = waiter.task_uuid
                WHERE job.status = 'pending' AND task.deleted_at IS NULL AND task.status IN ('pending', 'running')
                ORDER BY task.create_time, task.uuid, waiter.job_uuid
                """
            ).fetchall()
            holders: Dict[str, set] = {}
            for row in conn.execute(
                "SELECT lease.lock_key, claim.job_uuid FROM workflow_execution_lease AS lease "
                "JOIN workflow_execution_claim AS claim ON claim.claim_uuid = lease.claim_uuid "
                "WHERE claim.state != 'released'"
            ).fetchall():
                holders.setdefault(row["lock_key"], set()).add(row["job_uuid"])
            for row in conn.execute("SELECT lock_key, job_uuid FROM workflow_resource_use").fetchall():
                holders.setdefault(row["lock_key"], set()).add(row["job_uuid"])
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, str]] = []
        adjacency: Dict[str, set] = {}
        for row in waiters:
            job_uuid = str(row["job_uuid"])
            blocking = sorted({
                holder for key in _load(row["lock_keys"], []) for holder in holders.get(key, ())
                if holder != job_uuid
            })
            node: Dict[str, Any] = {"job_uuid": job_uuid, "task_uuid": row["task_uuid"],
                                    "wait_code": "resource_busy", "waiting_since": "",
                                    "blocking_task_uuids": _load(row["blockers"], [])}
            if blocking:
                node["blocking_job_uuid"] = blocking[0]
            for holder in blocking:
                adjacency.setdefault(job_uuid, set()).add(holder)
                edges.append({"waiting_job_uuid": job_uuid, "blocking_job_uuid": holder})
            nodes.append(node)
        cycles = _cycles(adjacency)
        in_cycle = {job for cycle in cycles for job in cycle}
        for node in nodes:
            node["deadlock_cycle"] = node["job_uuid"] in in_cycle
        return {"nodes": nodes, "edges": sorted(edges, key=lambda e: (e["waiting_job_uuid"], e["blocking_job_uuid"])),
                "cycles": cycles, "deadlock_detected": bool(cycles)}

    # 过程反馈 -------------------------------------------------------------

    def feedback(self, job_uuid: str, *, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        service = self.service
        identity = service.get_workflow_node_job(job_uuid)["uuid"]
        page, page_size = service._normalize_page(page, page_size)
        with service.write_lock:
            conn = service.connection
            total = int(conn.execute(
                "SELECT COUNT(*) FROM workflow_node_job_feedback_history WHERE workflow_node_job_uuid=? "
                "AND deleted_at IS NULL",
                (identity,),
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT * FROM workflow_node_job_feedback_history WHERE workflow_node_job_uuid=? "
                "AND deleted_at IS NULL ORDER BY sequence LIMIT ? OFFSET ?",
                (identity, page_size, (page - 1) * page_size),
            ).fetchall()
            items = [service._job_feedback_row(row) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "has_more": page * page_size < total}


def _cycles(adjacency: Dict[str, set]) -> List[List[str]]:
    """Tarjan 强连通分量：大小 > 1 或自环的分量即死锁环。"""

    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: set = set()
    result: List[List[str]] = []
    counter = [0]

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(adjacency.get(node, ())):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component = []
            while True:
                item = stack.pop()
                on_stack.discard(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1 or node in adjacency.get(node, ()):
                result.append(sorted(component))

    for node in sorted(adjacency):
        if node not in index:
            visit(node)
    return sorted(result)


# ── 任务物料变化（V1 /workflow-tasks/{uuid}/material-events） ─────────────────


def task_material_events(service: Any, task_uuid: str, materials: Any, *, page_limit: int = 1000,
                         max_pages: int = 50) -> List[Dict[str, Any]]:
    """任务造成的物料变化：物料权威台账里 ``job_uuid`` 属于本任务作业的变更，标注节点与上级步骤。

    台账在 materials.db（物料域拥有）；这里只按作业身份筛选并投影，不写库。未装配物料权威时为空。
    """

    from unilabos.server.api.v1_fields import ms_to_iso

    task = service.get_workflow_task(task_uuid)
    if materials is None:
        return []
    jobs = {job["uuid"]: job for job in service.list_jobs(task["uuid"])}
    nodes = {str(node.get("uuid")): node for node in (task.get("workflow_snapshot") or {}).get("nodes") or []
             if isinstance(node, dict)}

    def step_path(node_uuid: str) -> List[str]:
        path: List[str] = []
        seen: set = set()
        parent = str((nodes.get(node_uuid) or {}).get("parent_uuid") or "")
        while parent and parent in nodes and parent not in seen:
            seen.add(parent)
            if nodes[parent].get("name"):
                path.append(str(nodes[parent]["name"]))
            parent = str(nodes[parent].get("parent_uuid") or "")
        return list(reversed(path))

    result: List[Dict[str, Any]] = []
    after = 0
    for _ in range(max_pages):
        changes = materials.changes(after_sequence=after, limit=page_limit)
        if not changes:
            break
        for change in changes:
            after = max(after, change.sequence)
            if change.job_uuid not in jobs:
                continue
            node_uuid = str(jobs[change.job_uuid].get("workflow_node_uuid") or "")
            node = nodes.get(node_uuid) or {}
            result.append({
                "sequence": change.sequence, "event_uuid": change.event_uuid, "kind": change.operation,
                "aggregate_type": change.aggregate_type,
                "material_uuid": change.aggregate_uuid if change.aggregate_type == "material" else None,
                "aggregate_uuid": change.aggregate_uuid, "delta": change.delta,
                "occurred_at": ms_to_iso(change.occurred_at_ms), "actor_type": change.actor_type,
                "workflow_node_job_uuid": change.job_uuid, "workflow_task_uuid": task["uuid"],
                "workflow_node_uuid": node_uuid or None, "node_name": node.get("name"),
                "step_path": step_path(node_uuid) if node_uuid else [],
            })
        if len(changes) < page_limit:
            break
    return result


# ── 暂停任务修改未下发作业参数（V1 PATCH /workflow-tasks/{t}/jobs/{j}/param） ───────


def patch_pending_job_param(service: Any, task_uuid: str, job_uuid: str, param: Dict[str, Any]) -> Dict[str, Any]:
    """顶层合并参数补丁到暂停任务中尚未派发的当前 attempt（同时更新节点运行与计划节点）。

    任务必须暂停（普通模式关闸或单步暂停）；作业必须属于该任务、仍为 pending 且是节点运行的
    当前 attempt。在途或已下发的作业绝不改写；工作流拓扑不变。
    """

    task_identity = service.get_workflow_task(task_uuid)["uuid"]
    job_identity = _uuid(job_uuid)
    try:
        patch = normalize_json_object(param)
    except ValueError:
        raise _invalid() from None
    now = utc_now()
    with service.transaction() as conn:
        task = service.get_task(task_identity)
        if task["status"] not in {"pending", "running"} or task["control_status"] != "paused":
            raise _conflict("只有暂停中的任务可以修改未下发参数")
        job = conn.execute(
            "SELECT job.*, run.current_job_uuid, run.param AS run_param FROM workflow_node_job AS job "
            "JOIN workflow_node_run AS run ON run.uuid = job.workflow_node_run_uuid "
            "WHERE job.uuid=? AND job.deleted_at IS NULL",
            (job_identity,),
        ).fetchone()
        if job is None or job["workflow_task_uuid"] != task_identity:
            raise _not_found("作业不属于该任务")
        if job["status"] != "pending" or job["current_job_uuid"] != job_identity:
            raise _conflict("作业已离开 pending，不能修改参数")
        merged = {**_load(job["param"], {}), **patch}
        run_param = {**_load(job["run_param"], {}), **patch}
        conn.execute("UPDATE workflow_node_job SET param=?, update_time=? WHERE uuid=?", (_json(merged), now, job_identity))
        conn.execute("UPDATE workflow_node_run SET param=?, update_time=? WHERE uuid=?",
                     (_json(run_param), now, job["workflow_node_run_uuid"]))
        plan = task["execution_plan"]
        for planned in plan.get("nodes") or []:
            if isinstance(planned, dict) and planned.get("uuid") == job["workflow_node_uuid"]:
                planned["param"] = {**(planned.get("param") or {}), **patch}
        conn.execute("UPDATE workflow_task SET execution_plan=?, update_time=? WHERE uuid=?",
                     (_json(plan), now, task_identity))
        service._append_event(conn, event="workflow.node_job.changed", data={
            "workflow_node_job_uuid": job_identity, "workflow_task_uuid": task_identity, "status": "pending",
            "param_updated": True,
        }, now=now)
    controller = getattr(service, "_task_controller", None)
    if controller is not None:
        try:
            controller(task_identity)
        except Exception:  # noqa: BLE001 - 参数已落库，调度器恢复时按库内参数重载
            logger.exception("任务 %s 参数已修改，等待调度器重载", task_identity)
    return {"task": service.get_workflow_task(task_identity),
            "job": enrich_jobs(service, [service.get_job(job_identity)])[0]}
