"""作业的人工结算：V1 ``reconcile-quantity``（实际用量核对）与 ``resolve-uncertain``（结果未知处置）。

两者都落在 runtime.db 与 materials.db 的接缝上，只复用 dev 既有的幂等路径，不新造状态机：

**数量核对**（任何作业，不限搬运 / 分装）：

- 批次：作业的库存预留必须已经按既有生命周期结算（Host 动作开始扣减或调度器终态对账，状态
  ``consumed`` / ``quarantined``）；实际用量经 ``MaterialExecutions.apply_actual_lot_quantity`` 写入，
  与物理回执结算同一效果键 ``(job_uuid, physical.inventory:<lot>)``。同一差额重放不改账，已由回执或此前核对
  落账的不同用量按冲突拒绝——同一作业的同一批次最多校正一次，绝不重复扣减。预留仍为 ``active``（作业未开始
  扣减）或已 ``released`` 时拒绝。
- 容器内容物：作业的 ``material_content_reservation`` 仍为 ``active`` 时按实际量结算为 ``consumed``（从
  ``material_substance`` 扣减，不能超过容器现有量）；已结算时只接受同一实际量的重放。
- 全部物料写入在一个 materials.db 事务里，任一项被拒绝整体回滚；作业仍在执行（pending / dispatched /
  running …）时拒绝——核对只结算已有结果，不授权重新派发。核对记录（请求、原因、操作员）追加在作业
  ``control_data.quantity_reconciliation``。

**结果未知处置**（V1 ``resolution=canceled``）：作业必须结果未知（``execution_unknown`` /
``intervention_required`` / 带 ``uncertainty_reason`` / 物理结算类决策）且有一条待裁决的决策；处置按 dev 的
裁决通道投递 ``abort``（``ErrorDecisionDesk``，物理结果未知只允许补记或终止的规则照常生效）。dev 规则保持：
物理结果未知的动作在终止后仍保留物料与库位占用（``material_execution`` 仍为 prepared），直到经
``/scheduler/physical-recovery`` 或 ``settle-material-transfer`` 核实结算。处置记录写在
``control_data.manual_resolution``，同一处置重放返回原结果（``created=false``），不同处置冲突。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from unilabos.protocol.utils.json_codec import decode_json_bytes, encode_json
from unilabos.server.services.runtime.workflow.errors import StoreConflict

#: 可以核对实际用量的作业状态：已有结果（终态）或结果未知待裁决
RECONCILABLE_JOB_STATUSES = frozenset({
    "succeeded", "failed", "canceled", "timeout", "skipped", "execution_unknown", "intervention_required",
})
_TERMINAL = frozenset({"succeeded", "failed", "canceled", "timeout", "skipped"})
_UNCERTAIN_STATUSES = frozenset({"execution_unknown", "intervention_required"})
_UNCERTAIN_CATEGORIES = frozenset({"physical_settlement", "execution_state_unknown"})
_CONTENT_TYPES = frozenset({"reagent", "current_substance"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _close(left: float, right: float) -> bool:
    from unilabos.server.services.materials.quantity import quantity_tolerance

    return abs(float(left) - float(right)) <= quantity_tolerance(float(left), float(right))


def _invalid(message: str) -> Exception:
    from unilabos.server.services.runtime.workflow.service import WorkflowError

    return WorkflowError("invalid_input", detail=message)


def _update_control_data(workflow: Any, job_uuid: str, update: Callable[[Dict[str, Any]], None]) -> None:
    with workflow.transaction() as conn:
        row = conn.execute("SELECT control_data FROM workflow_node_job WHERE uuid=?", (job_uuid,)).fetchone()
        data = decode_json_bytes(str(row[0] or "{}").encode("utf-8"))
        update(data)
        conn.execute("UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
                     (encode_json(data).decode("utf-8"), job_uuid))


class QuantityReconciliation:
    """人工核对一个作业的实际用量；``materials`` 是本进程的物料权威（``MaterialsService``）。"""

    def __init__(self, workflow: Any, materials: Any) -> None:
        self.workflow = workflow
        self.materials = materials

    @staticmethod
    def normalize(consumptions: Sequence[Mapping[str, Any]], reason: str) -> Dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise _invalid("必须填写核对原因")
        if not consumptions:
            raise _invalid("至少核对一项库存")
        items: List[Dict[str, Any]] = []
        seen = set()
        for raw in consumptions:
            kind = str(raw.get("inventory_type") or "").strip().lower()
            identity = str(raw.get("inventory_uuid") or "").strip()
            unit = str(raw.get("quantity_unit") or "").strip()
            try:
                quantity = float(raw.get("actual_quantity"))
            except (TypeError, ValueError):
                raise _invalid("actual_quantity 必须是数值") from None
            if kind not in _CONTENT_TYPES | {"lot"} or not identity or not unit:
                raise _invalid("库存类型、身份与单位必填")
            if not math.isfinite(quantity) or quantity < 0:
                raise _invalid("actual_quantity 必须是非负有限数值")
            if (kind, identity) in seen:
                raise _invalid("同一库存在一次核对里只能出现一次")
            seen.add((kind, identity))
            items.append({"inventory_type": kind, "inventory_uuid": identity, "actual_quantity": quantity,
                          "quantity_unit": unit})
        return {"consumptions": items, "reason": reason}

    def reconcile(self, job_uuid: str, consumptions: Sequence[Mapping[str, Any]], reason: str, *,
                  operator_id: str = "") -> Dict[str, Any]:
        job = self.workflow.get_workflow_node_job(job_uuid)
        job_uuid = str(job["uuid"])
        if job["status"] not in RECONCILABLE_JOB_STATUSES:
            raise StoreConflict(f"作业状态为 {job['status']}，仍在执行或尚未开始，不能核对实际用量")
        request = self.normalize(consumptions, reason)
        plans = [self._plan(job, item) for item in request["consumptions"]]
        changed = False
        with self.materials.write():
            for plan in plans:
                changed = plan() or changed
        record = {**request, "operator_id": operator_id or "local-workspace", "reconciled_at": _now(),
                  "changed": changed}

        def append(data: Dict[str, Any]) -> None:
            previous = data.get("quantity_reconciliation")
            if previous and previous.get("consumptions") == request["consumptions"] \
                    and previous.get("reason") == request["reason"]:
                return  # 同一核对重放：保留首次记录
            history = list(data.get("quantity_reconciliation_history") or [])
            if previous:
                history.append(previous)
            data["quantity_reconciliation"] = record
            if history:
                data["quantity_reconciliation_history"] = history

        _update_control_data(self.workflow, job_uuid, append)
        return self.workflow.get_workflow_node_job(job_uuid)

    # ── 每项核对：先校验，返回在同一 materials 事务里执行的写入 ─────────────────

    def _plan(self, job: Mapping[str, Any], item: Mapping[str, Any]) -> Callable[[], bool]:
        if item["inventory_type"] == "lot":
            return self._plan_lot(job, item)
        return self._plan_content(job, item)

    def _plan_lot(self, job: Mapping[str, Any], item: Mapping[str, Any]) -> Callable[[], bool]:
        from unilabos.server.services.materials.execution import MaterialExecutions

        job_uuid, lot_uuid = str(job["uuid"]), item["inventory_uuid"]
        reservation = self.materials.get_reservation_by_job(job_uuid)
        if reservation is None:
            raise StoreConflict("作业没有库存预留，不能核对批次用量")
        lot_items = [entry for entry in reservation.items if entry.get("kind") == "lot" and entry.get("lot_uuid") == lot_uuid]
        if not lot_items:
            raise StoreConflict(f"批次 {lot_uuid} 不在该作业的库存预留里")
        if reservation.status == "active":
            raise StoreConflict("作业的库存预留尚未结算（动作未开始扣减或等待库存对账），不能核对实际用量")
        if reservation.status not in {"consumed", "quarantined"}:
            raise StoreConflict(f"作业的库存预留已 {reservation.status}，不能再核对实际用量")
        unit = str(lot_items[0].get("unit") or "")
        planned = math.fsum(float(entry.get("quantity") or 0) for entry in lot_items)
        actual = MaterialExecutions._convert(item["actual_quantity"], item["quantity_unit"], unit)
        difference = planned - actual
        effect = self.materials.get_effect(job_uuid, "physical.inventory:" + lot_uuid)
        if effect is not None:
            recorded = float(((effect.request_json or {}).get("payload") or {}).get("quantity_delta") or 0)
            if effect.status == "applied" and _close(recorded, difference):
                return lambda: False  # 物理回执或此前核对已按同一用量落账
            raise StoreConflict("该批次的实际用量已经落账（物理回执或此前的核对），不能改成其他数量")

        def apply() -> bool:
            MaterialExecutions(self.materials).apply_actual_lot_quantity(
                job_uuid, reservation.reservation_uuid, lot_uuid, difference)
            return True

        return apply

    def _content_reservation(self, job: Mapping[str, Any], substance_uuid: str) -> Any:
        rows = self.materials.connection.execute(
            "SELECT * FROM material_content_reservation WHERE substance_uuid=? AND (job_uuid=? OR "
            "(job_uuid IS NULL AND task_uuid=? AND node_uuid=?)) ORDER BY created_at_ms, reservation_uuid",
            (substance_uuid, job["uuid"], job["workflow_task_uuid"], job.get("workflow_node_uuid") or ""),
        ).fetchall()
        if not rows:
            raise StoreConflict(f"内容物 {substance_uuid} 不在该作业的库存预留里")
        if len(rows) > 1:
            raise StoreConflict(f"内容物 {substance_uuid} 在该作业下有多条预留，需要逐条结算")
        return rows[0]

    def _plan_content(self, job: Mapping[str, Any], item: Mapping[str, Any]) -> Callable[[], bool]:
        from unilabos.server.services.materials.contents import ContentContract
        from unilabos.server.services.materials.quantity import convert_quantity

        row = self._content_reservation(job, item["inventory_uuid"])
        try:
            converted = convert_quantity(item["actual_quantity"], item["quantity_unit"], row["quantity_unit"],
                                         inventory_type="content")
        except ValueError as exc:
            raise _invalid(str(exc)) from None
        if converted is None:
            raise _invalid(f"实际用量单位 {item['quantity_unit']} 不能换算为预留单位 {row['quantity_unit']}")
        actual = converted[0]
        if row["status"] == "released":
            raise StoreConflict("该内容物预留已释放，不能再核对实际用量")
        if row["status"] == "consumed":
            recorded = self._recorded_content_actual(row)
            if recorded is not None and _close(recorded, actual):
                return lambda: False
            raise StoreConflict("该内容物的实际用量已经落账，不能改成其他数量")
        reservation_uuid = row["reservation_uuid"]

        def apply() -> bool:
            ContentContract(self.materials, compound_source=None).finish_content_reservation(
                reservation_uuid, "consumed", consumed_quantity=actual)
            return True

        return apply

    def _recorded_content_actual(self, row: Any) -> Optional[float]:
        """已结算内容物预留的实际量（预留单位）；旧记录没有扩展时按预留量。"""

        for candidate in self.materials.connection.execute(
            "SELECT extension_json FROM material_content_history WHERE subject_uuid=? AND event_type='consume' "
            "ORDER BY recorded_at_ms DESC, history_uuid DESC", (row["substance_uuid"],),
        ).fetchall():
            extension = decode_json_bytes(str(candidate[0] or "{}").encode("utf-8"))
            if extension.get("reservation_uuid") == row["reservation_uuid"]:
                value = extension.get("reported_quantity")
                return float(value) if value is not None else None
        return float(row["quantity"])


class UncertainResolution:
    """把 V1 的结果未知处置落到 dev 的裁决通道；``desk`` 是 ``ErrorDecisionDesk``。"""

    def __init__(self, workflow: Any, desk: Any) -> None:
        self.workflow = workflow
        self.desk = desk

    def resolve(self, job_uuid: str, *, resolution: str, reason: str, device_command_id: Optional[str] = None,
                operator_id: str = "") -> Dict[str, Any]:
        normalized = str(resolution or "").strip().lower()
        reason = str(reason or "").strip()
        if normalized != "canceled" or not reason:
            raise _invalid("resolution 只接受 canceled，且必须填写原因")
        if device_command_id is not None and not str(device_command_id).strip():
            raise _invalid("device_command_id 不能为空")
        job = self.workflow.get_workflow_node_job(job_uuid)
        job_uuid = str(job["uuid"])
        manual = (job.get("control_data") or {}).get("manual_resolution")
        if manual:
            if manual.get("resolution") != normalized or manual.get("reason") != reason:
                raise StoreConflict("作业已经按另一人工结论处置")
            return self._result(job_uuid, manual, created=False)
        decision = next((item for item in self.desk.items() if item.get("job_id") == job_uuid), None)
        uncertain = (
            job["status"] in _UNCERTAIN_STATUSES
            or bool(str(job.get("uncertainty_reason") or "").strip())
            or (decision is not None and decision.get("category") in _UNCERTAIN_CATEGORIES)
        )
        if decision is None or not uncertain:
            raise StoreConflict("作业不是等待人工核实的结果未知作业")
        manual = {
            "resolution": normalized,
            "reason": reason,
            "command_uuid": str(decision["decision_id"]),
            "device_command_id": str(device_command_id).strip() if device_command_id else f"workflow-node-job:{job_uuid}",
            "operator_id": operator_id or "local-workspace",
            "requested_at": _now(),
            "selected_action": "abort",
        }

        def record(data: Dict[str, Any]) -> None:
            data["manual_resolution"] = manual

        _update_control_data(self.workflow, job_uuid, record)
        try:
            self.desk.resolve(manual["command_uuid"], {"action": "abort", "reason": reason},
                              idempotency_key=f"resolve-uncertain:{job_uuid}", operator_id=manual["operator_id"])
        except BaseException:
            _update_control_data(self.workflow, job_uuid, lambda data: data.pop("manual_resolution", None))
            raise
        return self._result(job_uuid, manual, created=True)

    def _result(self, job_uuid: str, manual: Mapping[str, Any], *, created: bool) -> Dict[str, Any]:
        job = self.workflow.get_workflow_node_job(job_uuid)
        return {
            "job": job,
            "resolution_command_uuid": manual.get("command_uuid"),
            # 执行面持有的决策异步收敛：作业尚未终态时等待执行面确认（V1 的 202）
            "pending_edge_confirmation": job["status"] not in _TERMINAL,
            "created": created,
        }


__all__ = ["QuantityReconciliation", "RECONCILABLE_JOB_STATUSES", "UncertainResolution"]
