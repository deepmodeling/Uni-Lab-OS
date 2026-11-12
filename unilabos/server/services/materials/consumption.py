"""作业数量型库存消费事实：V1 ``inventory-consumptions`` 的读模型与人工数量核对。

V1 把每次结算写成统一台账上的一条消费快照；dev 的同一事实分散在 materials.db 的三处，这里只读不复制：

- **批次**（``inventory_lot``）：作业的 ``inventory_reservation`` 在动作开始（Host）或终态对账（调度器）时
  ``consumed`` / ``quarantined``，按预留量整额扣减；之后的实际用量校正是同一作业同一批次唯一一次的
  ``inventory.actual_quantity`` 账本记录（效果键 ``physical.inventory:<lot>``，物理回执与人工核对共用）。
  消费量 = 预留量 − 校正退回量。``inventory_type`` 为 dev 扩展值 ``lot``（V1 只有试剂 / 当前内容物）。
- **容器内容物**（``material_substance``，唯一数量权威）：作业的 ``material_content_reservation`` 结算为
  ``consumed`` 时按实际量扣减，计划量、上报量、实际量与前后余量记在 ``material_content_history`` 的
  ``consume`` 事件扩展里。``inventory_type`` 按内容物角色为 ``reagent`` 或 ``current_substance``。
- **物理分装**（``material_execution`` 已结算且带 ``aliquot``）：来源容器各内容物按分装实际总量等比扣减；
  由冻结快照与回执还原，没有计划量（``planned_quantity`` 为 ``null``）。

DTO 字段对齐 V1 ``_consumption_row``（``uuid`` / ``workflow_task_uuid`` / ``workflow_node_job_uuid`` /
``inventory_type`` / ``inventory_uuid`` / ``planned_quantity`` / ``actual_quantity`` / ``quantity_unit`` /
``balance_before`` / ``balance_after`` / ``status`` / ``consumed_at`` / ``meta_data`` / ``display_name`` /
``container_barcode``）；节点与工作流名称（``workflow_node_uuid`` / ``workflow_name`` / ``node_name``）由
runtime 一侧补齐。``uuid`` 由事实身份确定性派生，重复读取稳定。
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping, Optional
from uuid import NAMESPACE_URL, uuid5

from unilabos.server.api.v1_fields import ms_to_iso

_FACT_NAMESPACE = uuid5(NAMESPACE_URL, "unilabos://inventory-consumption")
#: 内容物角色 → V1 库存类型
_CONTENT_TYPES = {"reagent": "reagent"}


def _uuid(*parts: str) -> str:
    return str(uuid5(_FACT_NAMESPACE, ":".join(parts)))


def _load(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class InventoryConsumptions:
    """按作业 / 内容物还原消费事实；构造只拿物料服务，不持有 runtime 依赖。"""

    def __init__(self, materials: Any) -> None:
        self.materials = materials

    @property
    def connection(self) -> Any:
        return self.materials.connection

    # ── 查询入口 ─────────────────────────────────────────────

    def for_jobs(self, job_uuids: Iterable[str]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for job_uuid in job_uuids:
            records.extend(self.for_job(job_uuid))
        records.sort(key=lambda item: (item["_at_ms"], item["uuid"]))
        return [self._public(item) for item in records]

    def for_job(self, job_uuid: str) -> list[dict[str, Any]]:
        """单个作业的全部消费事实（内部形状，含排序键；对外用 :meth:`list_job`）。"""

        with self.materials._write_lock:
            records = [*self._lot_facts(job_uuid), *self._content_facts("job_uuid=?", (job_uuid,)),
                       *self._aliquot_facts(job_uuid)]
        records.sort(key=lambda item: (item["_at_ms"], item["uuid"]))
        return records

    def list_job(self, job_uuid: str) -> list[dict[str, Any]]:
        return [self._public(item) for item in self.for_job(job_uuid)]

    def list_substance(self, substance_uuid: str) -> list[dict[str, Any]]:
        """一份内容物（试剂）的消费谱系，新在前；内容物删除后历史仍可读（V1 同口径）。"""

        with self.materials._write_lock:
            records = self._content_facts("substance_uuid=?", (substance_uuid,))
            materials = {row[0] for row in self.connection.execute(
                "SELECT material_uuid FROM material_content_history WHERE subject_uuid=? "
                "UNION SELECT material_uuid FROM material_substance WHERE substance_uuid=? "
                "UNION SELECT material_uuid FROM material_content_reservation WHERE substance_uuid=?",
                (substance_uuid, substance_uuid, substance_uuid),
            )}
            for material_uuid in sorted(materials):
                for (job_uuid,) in self.connection.execute(
                    "SELECT job_uuid FROM material_execution WHERE state='settled' "
                    "AND json_extract(request_json, '$.aliquot.source_material_uuid')=?", (material_uuid,),
                ).fetchall():
                    records.extend(item for item in self._aliquot_facts(job_uuid)
                                   if item["inventory_uuid"] == substance_uuid)
        records.sort(key=lambda item: (item["_at_ms"], item["uuid"]), reverse=True)
        return [self._public(item) for item in records]

    @staticmethod
    def _public(record: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if not key.startswith("_")}

    # ── 批次 ────────────────────────────────────────────────

    def _lot_facts(self, job_uuid: str) -> list[dict[str, Any]]:
        reservation = self.materials.get_reservation_by_job(job_uuid)
        if reservation is None or reservation.status not in {"consumed", "quarantined"}:
            return []
        planned: dict[str, float] = {}
        units: dict[str, str] = {}
        keys: dict[str, list[str]] = {}
        for item in reservation.items:
            if item.get("kind") != "lot" or not item.get("lot_uuid"):
                continue
            lot_uuid = str(item["lot_uuid"])
            planned[lot_uuid] = planned.get(lot_uuid, 0.0) + float(item.get("quantity") or 0)
            units.setdefault(lot_uuid, str(item.get("unit") or ""))
            keys.setdefault(lot_uuid, []).append(str(item.get("key") or ""))
        records = []
        for lot_uuid, planned_quantity in sorted(planned.items()):
            settled = None
            corrections = []
            for row in self.connection.execute(
                "SELECT * FROM inventory_ledger WHERE aggregate_type='lot' AND aggregate_uuid=? "
                "AND operation IN ('inventory.lot.consumed','inventory.lot.quarantined','inventory.actual_quantity') "
                "ORDER BY sequence", (lot_uuid,),
            ).fetchall():
                delta = _load(row["delta_json"], {})
                if row["operation"] == "inventory.actual_quantity":
                    if delta.get("reservation_uuid") == reservation.reservation_uuid:
                        corrections.append((row, delta))
                elif settled is None and (
                    delta.get("reservation_uuid") == reservation.reservation_uuid
                    or (not delta.get("reservation_uuid") and row["job_uuid"] == job_uuid)
                ):
                    settled = (row, delta)
            refund = math.fsum(float(delta.get("quantity_delta") or 0) for _, delta in corrections)
            actual = max(0.0, planned_quantity - refund)
            last = corrections[-1] if corrections else settled
            at_ms = int(settled[0]["occurred_at_ms"]) if settled else int(reservation.updated_at_ms)
            lot = self.materials.get_lot(lot_uuid)
            template = self._template(lot.template_uuid) if lot is not None else None
            meta: dict[str, Any] = {
                "planned_quantity_unit": units[lot_uuid],
                "reported_quantity_unit": units[lot_uuid],
                "reservation_uuid": reservation.reservation_uuid,
                "requirement_keys": keys[lot_uuid],
                "actual_quantity_reported": bool(corrections),
            }
            if corrections:
                meta["physical_settlement_entry_uuid"] = corrections[-1][0]["event_uuid"]
            if lot is not None and lot.batch_no:
                meta["batch_no"] = lot.batch_no
            records.append({
                "uuid": _uuid(job_uuid, reservation.reservation_uuid, "lot", lot_uuid),
                "workflow_task_uuid": reservation.task_uuid,
                "workflow_node_job_uuid": job_uuid,
                "workflow_node_uuid": reservation.node_uuid,
                "inventory_type": "lot",
                "inventory_uuid": lot_uuid,
                "planned_quantity": planned_quantity,
                "actual_quantity": actual,
                "quantity_unit": units[lot_uuid] or (lot.unit if lot is not None else ""),
                "balance_before": settled[1].get("quantity_total_before") if settled else None,
                "balance_after": last[1].get("quantity_total_after") if last else None,
                "status": reservation.status,
                "consumed_at": ms_to_iso(at_ms),
                "meta_data": meta,
                "display_name": (template.display_name or template.name) if template is not None else "",
                "container_barcode": "",
                "_at_ms": at_ms,
            })
        return records

    def _template(self, template_uuid: str) -> Any:
        from .core import MaterialNotFoundError

        try:
            return self.materials.get_template(template_uuid)
        except MaterialNotFoundError:
            return None

    # ── 容器内容物 ─────────────────────────────────────────────

    def _content_type(self, substance_uuid: str, subject_type: Optional[str] = None) -> str:
        row = self.connection.execute(
            "SELECT content_kind FROM material_content WHERE substance_uuid=?", (substance_uuid,)
        ).fetchone()
        if row is not None:
            return _CONTENT_TYPES.get(row[0], "current_substance")
        return "reagent" if subject_type == "reagent" else "current_substance"

    def _container(self, material_uuid: str) -> tuple[str, str]:
        row = self.connection.execute(
            "SELECT barcode, display_name, name FROM material WHERE material_uuid=?", (material_uuid,)
        ).fetchone()
        if row is None:
            return "", ""
        return str(row["barcode"] or ""), str(row["display_name"] or row["name"] or "")

    def _content_facts(self, clause: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        records = []
        for row in self.connection.execute(
            f"SELECT * FROM material_content_reservation WHERE {clause} AND status='consumed' "
            "ORDER BY updated_at_ms, reservation_uuid", params,
        ).fetchall():
            if not row["job_uuid"]:
                continue
            history = None
            for candidate in self.connection.execute(
                "SELECT * FROM material_content_history WHERE subject_uuid=? AND event_type='consume' "
                "ORDER BY recorded_at_ms, history_uuid", (row["substance_uuid"],),
            ).fetchall():
                extension = _load(candidate["extension_json"], {})
                if extension.get("reservation_uuid") == row["reservation_uuid"] or (
                    not extension.get("reservation_uuid") and candidate["workflow_node_job_uuid"] == row["job_uuid"]
                ):
                    history = (candidate, extension)
                    break
            extension = history[1] if history else {}
            if history and "actual_quantity" not in extension and history[0]["quantity_delta"] is not None:
                extension = {"actual_quantity": -float(history[0]["quantity_delta"]),
                             "quantity_unit": history[0]["quantity_unit"]}
            barcode, name = self._container(row["material_uuid"])
            subject_type = history[0]["subject_type"] if history else None
            at_ms = int(history[0]["recorded_at_ms"]) if history else int(row["updated_at_ms"])
            meta = {
                "planned_quantity_unit": row["quantity_unit"],
                "reported_quantity_unit": extension.get("reported_quantity_unit") or row["quantity_unit"],
                "reservation_uuid": row["reservation_uuid"],
                "material_uuid": row["material_uuid"],
            }
            if extension.get("reconciliation") is not None:
                meta["reconciliation"] = extension["reconciliation"]
            records.append({
                "uuid": _uuid(row["job_uuid"], row["reservation_uuid"], "content", row["substance_uuid"]),
                "workflow_task_uuid": row["task_uuid"],
                "workflow_node_job_uuid": row["job_uuid"],
                "workflow_node_uuid": row["node_uuid"] or None,
                "inventory_type": self._content_type(row["substance_uuid"], subject_type),
                "inventory_uuid": row["substance_uuid"],
                "planned_quantity": float(row["quantity"]),
                "actual_quantity": extension.get("actual_quantity", float(row["quantity"])),
                "quantity_unit": extension.get("quantity_unit") or row["quantity_unit"],
                "balance_before": extension.get("balance_before"),
                "balance_after": extension.get("balance_after"),
                "status": "consumed",
                "consumed_at": ms_to_iso(at_ms),
                "meta_data": meta,
                "display_name": name,
                "container_barcode": barcode,
                "_at_ms": at_ms,
            })
        return records

    # ── 物理分装 ────────────────────────────────────────────

    def _aliquot_facts(self, job_uuid: str) -> list[dict[str, Any]]:
        from .execution import MaterialExecutions

        row = self.connection.execute(
            "SELECT * FROM material_execution WHERE job_uuid=? AND state='settled'", (job_uuid,)
        ).fetchone()
        if row is None:
            return []
        request = _load(row["request_json"], {})
        aliquot = request.get("aliquot") or None
        receipts = (_load(row["receipt_json"], {}) or {}).get("material_aliquot_receipts") or []
        if not aliquot or not receipts:
            return []
        source_uuid = str(aliquot["source_material_uuid"])
        before = ((_load(row["snapshot_json"], {}) or {}).get(source_uuid) or {}).get("substances") or []
        unit = str(receipts[0]["quantity_unit"])
        try:
            used = math.fsum(MaterialExecutions._convert(float(item["actual_quantity"]), item["quantity_unit"], unit)
                             for item in receipts)
            totals = []
            for item in before:
                quantity = float(item.get("quantity") or 0)
                totals.append(MaterialExecutions._convert(quantity, item["quantity_unit"], unit) if quantity else 0.0)
        except Exception:  # noqa: BLE001 - 单位不可换算时结算本身就不会成功，读模型跳过
            return []
        total = math.fsum(totals)
        if total <= 0 or used <= 0:
            return []
        entry = self.connection.execute(
            "SELECT event_uuid, occurred_at_ms FROM inventory_ledger WHERE job_uuid=? AND command_uuid=? "
            "AND effect_key=? ORDER BY sequence LIMIT 1", (job_uuid, job_uuid, "physical.aliquot:" + source_uuid),
        ).fetchone()
        at_ms = int(entry["occurred_at_ms"]) if entry is not None else int(row["updated_at_ms"])
        barcode, name = self._container(source_uuid)
        records = []
        for item in before:
            quantity = float(item.get("quantity") or 0)
            if quantity <= 0 or not item.get("substance_uuid"):
                continue
            consumed = quantity * used / total
            meta: dict[str, Any] = {
                "planned_quantity_unit": None,
                "reported_quantity_unit": unit,
                "material_uuid": source_uuid,
                "aliquot_target_material_uuids": [str(item["target_material_uuid"]) for item in receipts],
            }
            if entry is not None:
                meta["physical_settlement_entry_uuid"] = entry["event_uuid"]
            records.append({
                "uuid": _uuid(job_uuid, "aliquot", str(item["substance_uuid"])),
                "workflow_task_uuid": row["task_uuid"],
                "workflow_node_job_uuid": job_uuid,
                "workflow_node_uuid": None,
                "inventory_type": self._content_type(str(item["substance_uuid"])),
                "inventory_uuid": str(item["substance_uuid"]),
                "planned_quantity": None,
                "actual_quantity": consumed,
                "quantity_unit": item["quantity_unit"],
                "balance_before": quantity,
                "balance_after": max(0.0, quantity - consumed),
                "status": "consumed",
                "consumed_at": ms_to_iso(at_ms),
                "meta_data": meta,
                "display_name": str(item.get("name") or name),
                "container_barcode": barcode,
                "_at_ms": at_ms,
            })
        return records


__all__ = ["InventoryConsumptions"]
