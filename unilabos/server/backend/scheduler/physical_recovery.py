"""跨 runtime/materials 两库保存结算意图，回执重放不再调用设备。"""

from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any

from unilabos.protocol.material_execution import MaterialExecutionReceipt, MaterialExecutionRelease
from unilabos.protocol.utils.json_codec import encode_json, decode_json_bytes
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.errors import StoreConflict

_TERMINAL = {"succeeded", "failed", "skipped", "canceled", "timeout"}


class PhysicalSettlementPending(RuntimeError):
    pass


class PhysicalRecovery:
    def __init__(self, workflow, gateway, epoch):
        self.workflow, self.gateway, self.epoch = workflow, gateway, epoch
        self.guard = threading.RLock()

    def get(self, job_uuid):
        reader = getattr(self.gateway, "get_material_execution", None)
        return reader(job_uuid) if callable(reader) else None

    def _save(self, job_uuid, outcome):
        with self.workflow.transaction() as conn:
            ExecutionClaims._require_epoch(conn, self.epoch)
            job = ExecutionClaims._job(conn, job_uuid)
            data = decode_json_bytes(job["control_data"].encode("utf-8"))
            previous = data.get("physical_outcome")
            if previous and previous.get("return_info") != outcome.get("return_info"):
                if job["status"] in _TERMINAL or (outcome.get("resolution") or {}).get("selected_action") not in {
                    "operator_intervention",
                    "abort",
                }:
                    raise StoreConflict("物理动作收到不同的结果，必须人工核实")
                data.setdefault("physical_outcome_history", []).append(previous)
            data["physical_outcome"] = outcome
            conn.execute(
                "UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
                (encode_json(data).decode("utf-8"), job_uuid),
            )

    def settle(self, job_uuid, *, job_status, return_info, error_info, resolution):
        with self.guard:
            with self.workflow.write_lock:
                dispatch_row = self.workflow.connection.execute(
                    "SELECT identity FROM workflow_execution_dispatch WHERE job_uuid=?", (job_uuid,)
                ).fetchone()
            identity = decode_json_bytes(dispatch_row[0].encode("utf-8")) if dispatch_row else {}
            outcome = {
                "job_status": job_status,
                "return_info": deepcopy(return_info),
                "error_info": deepcopy(error_info),
                "resolution": deepcopy(resolution),
                "settled": False,
            }
            if identity.get("material_execution"):
                try:
                    self._save(job_uuid, outcome)
                except StoreConflict as error:
                    raise PhysicalSettlementPending(str(error)) from error
            try:
                record = self.get(job_uuid)
            except Exception as error:
                raise PhysicalSettlementPending(f"物理结算权威暂不可用：{error}") from error
            if record is None:
                if identity.get("material_execution"):
                    raise PhysicalSettlementPending("冻结派发引用的物理准备记录丢失，禁止重复执行")
                return
            physical = bool(record["request"].get("moves") or record["request"].get("aliquot"))
            if job_status != "succeeded":
                with self.workflow.write_lock:
                    dispatch = self.workflow.connection.execute(
                        "SELECT authorized_at FROM workflow_execution_dispatch WHERE job_uuid=?", (job_uuid,)
                    ).fetchone()
                if record["state"] == "prepared" and (not physical or not dispatch or not dispatch[0]):
                    self.gateway.release_material_execution(
                        job_uuid,
                        MaterialExecutionRelease(
                            expected_version=record["version"],
                            reason="权威确认未开始物理动作；库存由独立对账处理",
                            outcome="not_started",
                        ),
                    )
                elif (
                    physical and record["state"] == "prepared" and (resolution or {}).get("selected_action") != "abort"
                ):
                    raise PhysicalSettlementPending("动作可能部分执行，请核实库位和实际用量；终止任务后仍保留物料占用")
                return
            outcome = {
                "job_status": job_status,
                "return_info": deepcopy(return_info),
                "error_info": deepcopy(error_info),
                "resolution": deepcopy(resolution),
                "settled": False,
            }
            self._save(job_uuid, outcome)
            try:
                receipt = self._receipt(record, return_info.get("return_value"))
                self.gateway.settle_material_execution(receipt)
                outcome["settled"] = True
                self._save(job_uuid, outcome)
            except Exception as error:
                outcome["error"] = str(error)
                self._save(job_uuid, outcome)
                raise PhysicalSettlementPending(f"设备结果已保存，物理结算待核实：{error}") from error

    @staticmethod
    def _receipt(record, value):
        physical = bool(record["request"].get("moves") or record["request"].get("aliquot"))
        value = value or {}
        if not isinstance(value, dict):
            if physical:
                raise ValueError("物理动作结果必须包含具名回执")
            value = {}
        moves = []
        expected = {item["material_uuid"]: item for item in record["request"].get("moves", [])}
        for item in value.get("material_transfer_receipts", []):
            if not isinstance(item, dict):
                raise ValueError("搬运回执必须是对象")
            frozen = expected.get(item.get("material_uuid"), {})
            moves.append(
                {
                    "source_owner_uuid": frozen.get("source_owner_uuid"),
                    "source_site_uuid": frozen.get("source_site_uuid"),
                    **item,
                }
            )
        return MaterialExecutionReceipt(
            job_uuid=record["job_uuid"],
            claim_uuid=record["claim_uuid"],
            plan_hash=record["plan_hash"],
            material_transfer_receipts=moves,
            material_aliquot_receipts=value.get("material_aliquot_receipts", []),
            inventory_consumptions=value.get("inventory_consumptions"),
        )

    def confirm_terminal_settlement(self, job_uuid, expected_version, reason, result):
        if not reason.strip():
            raise StoreConflict("必须填写现场核实说明")
        with self.guard:
            with self.workflow.transaction() as conn:
                ExecutionClaims._require_epoch(conn, self.epoch)
                job = ExecutionClaims._job(conn, job_uuid)
                if job["status"] not in _TERMINAL or job["task_status"] not in _TERMINAL:
                    raise StoreConflict("活动任务请使用执行核实；任务终止后才能补记实际结算")
                data = decode_json_bytes(job["control_data"].encode("utf-8"))
                record = self.get(job_uuid)
                if record is None:
                    raise StoreConflict("物理动作准备记录不存在")
                receipt = self._receipt(record, result)
                command = {
                    "expected_version": expected_version,
                    "reason": reason,
                    "receipt": receipt.model_dump(mode="json"),
                }
                previous = data.get("manual_physical_settlement")
                if previous:
                    if previous["command"] != command:
                        raise StoreConflict("已经提交过不同的人工结算，请先核对原回执")
                else:
                    if record["version"] != expected_version or record["state"] != "prepared":
                        raise StoreConflict("物理动作状态已变化，请刷新后核实")
                    self.gateway.validate_material_execution(receipt)
                    data["manual_physical_settlement"] = {"command": command, "completed": False}
                    conn.execute(
                        "UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
                        (encode_json(data).decode("utf-8"), job_uuid),
                    )
            return self._apply_manual(job_uuid)

    def _apply_manual(self, job_uuid):
        with self.guard:
            with self.workflow.transaction() as conn:
                ExecutionClaims._require_epoch(conn, self.epoch)
                row = conn.execute("SELECT control_data FROM workflow_node_job WHERE uuid=?", (job_uuid,)).fetchone()
                intent = decode_json_bytes(row[0].encode("utf-8"))["manual_physical_settlement"]
            result = self.gateway.settle_material_execution(
                MaterialExecutionReceipt.model_validate(intent["command"]["receipt"])
            )
            with self.workflow.transaction() as conn:
                ExecutionClaims._require_epoch(conn, self.epoch)
                job = ExecutionClaims._job(conn, job_uuid)
                data = decode_json_bytes(job["control_data"].encode("utf-8"))
                data["manual_physical_settlement"]["completed"] = True
                conn.execute(
                    "UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
                    (encode_json(data).decode("utf-8"), job_uuid),
                )
            self._refresh_cleanup()
            return result

    def pending(self) -> dict[str, dict[str, Any]]:
        with self.workflow.write_lock:
            rows = self.workflow.connection.execute(
                "SELECT uuid,control_data FROM workflow_node_job WHERE "
                "status IN ('running','dispatched','execution_unknown','intervention_required') "
                "AND json_type(control_data,'$.physical_outcome') IS NOT NULL"
            ).fetchall()
            return {
                row["uuid"]: decode_json_bytes(row["control_data"].encode("utf-8"))["physical_outcome"] for row in rows
            }

    def reconcile_unstarted(self):
        reader = getattr(self.gateway, "list_material_executions", None)
        if not callable(reader):
            return
        with self.workflow.write_lock:
            pending = [
                row[0]
                for row in self.workflow.connection.execute(
                    "SELECT uuid FROM workflow_node_job "
                    "WHERE json_extract(control_data,'$.manual_physical_settlement.completed')=0"
                )
            ]
        for job_uuid in pending:
            self._apply_manual(job_uuid)
        records = reader()
        for record in records:
            if record["state"] != "prepared":
                continue
            with self.workflow.transaction() as conn:
                ExecutionClaims._require_epoch(conn, self.epoch)
                row = conn.execute(
                    "SELECT status FROM workflow_node_job WHERE uuid=?", (record["job_uuid"],)
                ).fetchone()
                dispatch = conn.execute(
                    "SELECT authorized_at FROM workflow_execution_dispatch WHERE job_uuid=?", (record["job_uuid"],)
                ).fetchone()
                if row and row[0] in _TERMINAL - {"succeeded"} and (not dispatch or not dispatch[0]):
                    self.gateway.release_material_execution(
                        record["job_uuid"],
                        MaterialExecutionRelease(
                            expected_version=record["version"],
                            reason="终态尝试未获开始许可，回收准备阶段占用",
                            outcome="not_started",
                        ),
                    )

        self._refresh_cleanup()

    def _refresh_cleanup(self):
        from unilabos.server.services.runtime.workflow.device_tenancy import DeviceTenancies

        reader = getattr(self.gateway, "list_material_executions", None)
        pending = {row["task_uuid"] for row in reader() if row["state"] == "prepared"} if callable(reader) else set()
        with self.workflow.transaction() as conn:
            ExecutionClaims._require_epoch(conn, self.epoch)
            for row in conn.execute(
                "SELECT uuid,attention_reason FROM workflow_task WHERE status IN ('succeeded','failed','canceled','timeout')"
            ).fetchall():
                if row["uuid"] in pending:
                    conn.execute(
                        "UPDATE workflow_task SET cleanup_status='requires_attention',attention_reason='physical_execution_pending' WHERE uuid=?",
                        (row["uuid"],),
                    )
                elif row["attention_reason"] == "physical_execution_pending":
                    tenancy = bool(DeviceTenancies.snapshot(conn, row["uuid"]))
                    conn.execute(
                        "UPDATE workflow_task SET cleanup_status=?,attention_reason=? WHERE uuid=?",
                        (
                            "requires_attention" if tenancy else "settled",
                            "device_tenancy_active" if tenancy else "physical_execution_settled",
                            row["uuid"],
                        ),
                    )

    def blockers(self, job_uuid, material_ids, device_ids=()):
        reader = getattr(self.gateway, "list_material_executions", None)
        if not (material_ids or device_ids) or not callable(reader):
            return []
        return [
            row["job_uuid"]
            for row in reader()
            if row["job_uuid"] != job_uuid
            and row["state"] == "prepared"
            and (
                set(row["snapshot"]) & set(material_ids)
                or set(row["request"].get("safety_device_ids", [])) & set(device_ids)
            )
        ]

    def snapshot(self):
        reader = getattr(self.gateway, "list_material_executions", None)
        records = reader() if callable(reader) else []
        result = []
        with self.workflow.write_lock:
            for record in records:
                if record["state"] != "prepared":
                    continue
                row = self.workflow.connection.execute(
                    "SELECT j.status,j.control_data,t.status AS task_status FROM workflow_node_job j "
                    "JOIN workflow_task t ON t.uuid=j.workflow_task_uuid WHERE j.uuid=?",
                    (record["job_uuid"],),
                ).fetchone()
                if row:
                    data = decode_json_bytes(row["control_data"].encode("utf-8"))
                    record = {
                        **record,
                        "job_status": row["status"],
                        "task_status": row["task_status"],
                        "can_release": row["status"] in _TERMINAL
                        and row["task_status"] in _TERMINAL
                        and not data.get("manual_physical_settlement"),
                        "error": data.get("physical_outcome", {}).get("error"),
                    }
                result.append(record)
        return result

    def release_verified(self, job_uuid, expected_version, reason):
        with self.workflow.transaction() as conn:
            ExecutionClaims._require_epoch(conn, self.epoch)
            row = ExecutionClaims._job(conn, job_uuid)
            if row["status"] not in _TERMINAL or row["task_status"] not in _TERMINAL:
                raise StoreConflict("请先终止任务并确认设备已经停止，再释放物理占用")
            data = decode_json_bytes(row["control_data"].encode("utf-8"))
            if data.get("manual_physical_settlement"):
                raise StoreConflict("已有经确认的实际结算，不能再按恢复原状释放")
            result = self.gateway.release_material_execution(
                job_uuid,
                MaterialExecutionRelease(
                    expected_version=expected_version, reason=reason, outcome="physically_verified"
                ),
            )
        self._refresh_cleanup()
        return result
