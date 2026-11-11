"""按持久 attempt 与库存流水对账；不从库存扣减推断设备执行成功。"""
from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any

from unilabos.protocol.materials import InventoryMutation, InventoryReservationTransition
from unilabos.protocol.utils.json_codec import decode_json_bytes
from unilabos.server.backend.inventory import ExecutionInventoryCoordinator
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.store import utc_now

_TERMINAL = {"succeeded", "skipped", "failed", "canceled", "timeout"}
_STATUSES = frozenset({"active", "consumed", "released", "quarantined"})


def _load(value: str) -> Any:
    return decode_json_bytes(value.encode("utf-8"))


class InventoryRecovery:
    """库存记录保存跨库关联键，runtime 的终态保存待结算事实；两者均可重放。"""

    def __init__(self, workflow: Any, gateway: Any, authority_epoch: str):
        self.workflow = workflow
        self.gateway = gateway
        self.epoch = authority_epoch
        self._guard = threading.RLock()
        self._report: dict[str, Any] = {"checked_at": None, "issues": [], "held": [], "changed": []}

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            return deepcopy(self._report)

    def reconcile(self) -> dict[str, Any]:
        report: dict[str, Any] = {"checked_at": utc_now(), "issues": [], "held": [], "changed": []}
        with self._guard:
            try:
                if self.gateway is None:
                    with self.workflow.write_lock:
                        required = self.workflow.connection.execute(
                            "SELECT 1 FROM workflow_execution_dispatch WHERE "
                            "json_extract(identity,'$.inventory_reservation_uuid') IS NOT NULL LIMIT 1").fetchone()
                    if required:
                        report["issues"].append({"reason": "持久派发需要库存权威，但库存服务未配置"})
                    self._report = report
                    return deepcopy(report)
                reservations = self.gateway.list_inventory_reservations()
                by_job = {r.job_uuid: r for r in reservations}
                # 全局列表只能用于发现身份；每次决定前重新读取当前库存，不能凭旧快照退款。
                for reservation in reservations:
                    try:
                        with self.workflow.transaction() as conn:
                            ExecutionClaims._require_epoch(conn, self.epoch)
                            self._reconcile_one(conn, reservation.reservation_uuid, reservation.job_uuid, report)
                    except Exception as exc:
                        report["issues"].append({"job_uuid": reservation.job_uuid, "task_uuid": reservation.task_uuid,
                            "reservation_uuid": reservation.reservation_uuid, "reason": str(exc)})
                with self.workflow.write_lock:
                    # 已封存的派发不能在库存记录丢失后被当成无需库存的动作。
                    for row in self.workflow.connection.execute("SELECT job_uuid,identity FROM workflow_execution_dispatch"):
                        identity = _load(row["identity"])
                        if identity.get("inventory_reservation_uuid") and row["job_uuid"] not in by_job:
                            report["issues"].append({"job_uuid": row["job_uuid"],
                                "reservation_uuid": identity["inventory_reservation_uuid"], "reason": "派发引用的库存预留不存在"})
            except Exception as exc:
                report["issues"].append({"reason": f"库存对账暂不可用：{exc}"})
            self._report = report
            return deepcopy(report)

    def _reconcile_one(self, conn, reservation_uuid: str, job_uuid: str, report: dict[str, Any]) -> None:
        job = conn.execute("SELECT j.*,t.execution_plan,t.meta_data AS task_meta_data,t.status AS task_status FROM workflow_node_job j "
                           "JOIN workflow_task t ON t.uuid=j.workflow_task_uuid WHERE j.uuid=?", (job_uuid,)).fetchone()
        reservation = self.gateway.get_inventory_reservation(reservation_uuid)
        if not job:
            # 物料服务也服务非工作流作业；只处理可归属本权威的工作流库存。
            if conn.execute("SELECT 1 FROM workflow_task WHERE uuid=?", (reservation.task_uuid,)).fetchone():
                raise ValueError("库存预留关联的工作流尝试不存在，保留库存等待核实")
            if reservation.status in {"active", "consumed"}:
                report["held"].append({"job_uuid": job_uuid, "reservation_uuid": reservation_uuid,
                    "reason": "无本地工作流尝试记录，不自动结算外部预留", "status": reservation.status})
            return
        dispatch = conn.execute("SELECT * FROM workflow_execution_dispatch WHERE job_uuid=?", (job_uuid,)).fetchone()
        if dispatch:
            payload = _load(dispatch["identity"])
            if payload.get("inventory_reservation_uuid") != reservation_uuid:
                raise ValueError("库存预留与冻结派发身份不一致")
        else:
            node = next((n for n in _load(job["execution_plan"]).get("nodes", [])
                         if n["uuid"] == job["workflow_node_uuid"]), None)
            if node is None:
                raise ValueError("库存预留对应的冻结节点不存在")
            payload = {"job_id": job_uuid, "task_id": job["workflow_task_uuid"],
                       "node_id": job["workflow_node_uuid"], "inventory_requirements": node.get("inventory_requirements") or []}
        payload["scheduler_revision"] = int(_load(job["task_meta_data"]).get("scheduler_revision") or 1)
        ExecutionInventoryCoordinator._validate_reservation(reservation, payload, allowed_statuses=_STATUSES)
        status = job["status"]
        identity = {"job_uuid": job_uuid, "reservation_uuid": reservation_uuid, "task_uuid": reservation.task_uuid}
        if status not in _TERMINAL:
            if status == "pending" and reservation.status != "active":
                raise ValueError("未开始的尝试对应库存已经结算，禁止重复执行")
            if status != "pending":
                report["held"].append({**identity, "reason": "执行尚未核实，保留库存状态", "status": reservation.status})
            return
        if status == "succeeded":
            target = "consumed"
            if reservation.status in {"released", "quarantined"}:
                raise ValueError("成功尝试与已释放或隔离的库存冲突，需要人工核实")
        else:
            # 新协议的开始许可是可能产生物理副作用的边界；旧作业没有凭据时保守隔离。
            claim = conn.execute("SELECT 1 FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,)).fetchone()
            may_have_started = bool(dispatch and dispatch["authorized_at"])
            if not dispatch and not claim:
                may_have_started = bool(job["started_at"])
            target = "quarantined" if may_have_started or reservation.status in {"consumed", "quarantined"} else "released"
            # 装载托管跨动作/跨任务终态保留。兼容托管迁移前的库。
            has_tenancy = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_device_tenancy'").fetchone()
            if target == "released" and has_tenancy and conn.execute(
                    "SELECT 1 FROM workflow_device_tenancy WHERE task_uuid=? AND state!='released'", (reservation.task_uuid,)).fetchone():
                report["held"].append({**identity, "reason": "设备仍托管样品，暂不释放库存", "status": reservation.status})
                return
        if reservation.status == target:
            return
        if reservation.status == "released":
            raise ValueError("可能执行过的尝试已返还库存，需要人工核实")
        action = {"consumed": "consume", "released": "release", "quarantined": "quarantine"}[target]
        operation = f"{action}_inventory_reservation"
        # 独立于 Host 的幂等命名空间，固定内容；HTTP 提交成功但应答丢失也可安全重放。
        mutation = InventoryMutation(command_uuid=job_uuid, effect_key=f"inventory.recovery.{action}",
                                     operation=operation, actor_type="scheduler", job_uuid=job_uuid)
        value = InventoryReservationTransition(reservation_uuid=reservation_uuid, reason="workflow_inventory_reconciliation")
        try:
            getattr(self.gateway, operation)(mutation, value)
        except Exception:
            # Host 也可能先结算；只接受已经抵达目标的真实记录，绝不根据异常猜测。
            if self.gateway.get_inventory_reservation(reservation_uuid).status != target:
                raise
        report["changed"].append({**identity, "from_status": reservation.status, "status": target})
