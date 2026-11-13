"""材料库内的物理占用和回执结算；全部内容与库位变更共用一个事务。"""

from __future__ import annotations

import json
import math
from typing import Any

from unilabos.protocol.base import canonical_hash
from unilabos.protocol.material_execution import (
    MaterialExecutionPrepare,
    MaterialExecutionReceipt,
    MaterialExecutionRelease,
)
from unilabos.protocol.materials import InventoryMutation, MaterialDataWrite, MaterialMove, MaterialSubstance
from .quantity import convert_quantity, quantity_tolerance


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class MaterialExecutions:
    def __init__(self, service):
        self.service = service

    def _site(self, material_uuid: str) -> str | None:
        site = self.service.occupied_site(material_uuid)
        return site.site_uuid if site else None

    def get(self, job_uuid: str) -> dict | None:
        with self.service._write_lock:
            row = self.service.connection.execute(
                "SELECT * FROM material_execution WHERE job_uuid=?", (job_uuid,)
            ).fetchone()
            if row is None:
                return None
            result = {
                **{
                    key: row[key]
                    for key in (
                        "job_uuid",
                        "task_uuid",
                        "claim_uuid",
                        "state",
                        "version",
                        "created_at_ms",
                        "updated_at_ms",
                    )
                },
                "plan_hash": row["request_hash"],
                **{
                    key: json.loads(row[key + "_json"]) if row[key + "_json"] else None
                    for key in ("request", "snapshot", "receipt", "result", "confirmation")
                },
            }
            reservation = result["request"].get("inventory_reservation_uuid")
            result["inventory_items"] = (
                [item.model_dump(mode="json") for item in self.service.get_inventory_reservation(reservation).items]
                if reservation
                else []
            )
            return result

    def list(self) -> list[dict]:
        with self.service._write_lock:
            return [
                self.get(row[0])
                for row in self.service.connection.execute(
                    "SELECT job_uuid FROM material_execution ORDER BY created_at_ms,job_uuid"
                )
            ]

    def prepare(self, value: MaterialExecutionPrepare) -> dict:
        from .core import MaterialConflictError, MaterialValidationError

        service = self.service
        request_hash = canonical_hash(value.model_dump(mode="json"))
        with service._transfer_lock, service.write() as conn:
            prior = self.get(value.job_uuid)
            if prior:
                if prior["plan_hash"] != request_hash or prior["state"] == "released":
                    raise MaterialConflictError("同一尝试的物理结算范围已改变或失效")
                return prior
            material_ids, site_ids = set(value.safety_material_uuids), set(value.reserved_site_uuids)
            moved = {move.material_uuid for move in value.moves}
            for identity in site_ids:
                site = service.get_site(identity)
                if site is None or site.occupied_material_uuid and site.occupied_material_uuid not in moved:
                    raise MaterialConflictError("夹爪或搬运器库位不可用")
                material_ids.add(site.owner_material_uuid)
            for move in value.moves:
                material_ids.update((move.material_uuid, move.target_owner_uuid))
                material_ids.update(item.material.material_uuid for item in service.get_tree(move.material_uuid).nodes)
                if move.source_owner_uuid:
                    material_ids.add(move.source_owner_uuid)
                site_ids.add(move.target_site_uuid)
                if move.source_site_uuid:
                    site_ids.add(move.source_site_uuid)
                current = service.get_material(move.material_uuid).material
                # 载架型 Site 上的物料挂在该 Site 的 holder 下，父物料不是来源容器本身
                expected_parents = {move.source_owner_uuid}
                source_site = service.get_site(move.source_site_uuid) if move.source_site_uuid else None
                if source_site is not None:
                    expected_parents.add(
                        service.site_parent_material_uuid(source_site, moving_material_uuid=move.material_uuid)
                    )
                if (
                    current.parent_material_uuid not in expected_parents
                    or self._site(move.material_uuid) != move.source_site_uuid
                ):
                    raise MaterialConflictError("搬运来源与权威库位不一致")
                destination = service.get_site(move.target_site_uuid)
                if (
                    destination is None
                    or not destination.visible
                    or destination.owner_material_uuid != move.target_owner_uuid
                ):
                    raise MaterialValidationError("搬运目标库位不属于目标容器")
                if destination.occupied_material_uuid not in (None, move.material_uuid):
                    raise MaterialConflictError("搬运目标库位已被占用", conflict="site_occupied")
            if value.aliquot:
                material_ids.update([value.aliquot.source_material_uuid, *value.aliquot.target_material_uuids])
            # 复用真实移动约束作事务内预检；只回滚这个保存点，不留下账本或位置变更。
            conn.execute("SAVEPOINT physical_move_preflight")
            try:
                for move in value.moves:
                    service.move_material(
                        self._mutation(value, "preflight:" + move.material_uuid, "move_material"),
                        MaterialMove(material_uuid=move.material_uuid, destination_site_uuid=move.target_site_uuid),
                    )
            finally:
                conn.execute("ROLLBACK TO physical_move_preflight")
                conn.execute("RELEASE physical_move_preflight")
            snapshots = {}
            for identity in sorted(material_ids):
                current = service.get_material(identity)
                if current.material.lifecycle_status in {"quarantined", "consumed", "retired", "deleted"}:
                    raise MaterialConflictError(f"物料 {identity} 当前不可用于物理动作")
                # 物料和其容器的占用必须已经包含在权威派发 Claim 中。
                fence_key = json.dumps(["material", identity], ensure_ascii=False, separators=(",", ":"))
                if fence_key not in value.fences:
                    raise MaterialValidationError(f"派发凭据缺少物料或库位容器占用：{identity}")
                snapshots[identity] = {
                    "parent_material_uuid": current.material.parent_material_uuid,
                    "site_uuid": self._site(identity),
                    "data_state_hash": current.data.state_hash,
                    "substances": [item.model_dump(mode="json") for item in current.data.substances],
                }
            if value.inventory_reservation_uuid:
                reservation = service.get_inventory_reservation(value.inventory_reservation_uuid)
                if (
                    reservation.job_uuid != value.job_uuid
                    or reservation.task_uuid != value.task_uuid
                    or reservation.status not in {"active", "consumed"}
                ):
                    raise MaterialConflictError("物理结算库存预留身份或状态无效")
            locks = [("material", identity) for identity in sorted(material_ids)] + [
                ("site", identity) for identity in sorted(site_ids)
            ]
            for kind, identity in locks:
                if conn.execute(
                    "SELECT 1 FROM material_execution_hold WHERE aggregate_type=? AND aggregate_uuid=?",
                    (kind, identity),
                ).fetchone():
                    raise MaterialConflictError(f"物理动作尚未结算：{kind}/{identity}", conflict="active_claim")
            now = service._now_ms()
            conn.execute(
                "INSERT INTO material_execution (job_uuid,task_uuid,claim_uuid,request_json,request_hash,snapshot_json,state,version,created_at_ms,updated_at_ms) "
                "VALUES (?,?,?,?,?,?,'prepared',1,?,?)",
                (
                    value.job_uuid,
                    value.task_uuid,
                    value.claim_uuid,
                    _json(value),
                    request_hash,
                    _json(snapshots),
                    now,
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO material_execution_hold VALUES (?,?,?)",
                [(kind, identity, value.job_uuid) for kind, identity in locks],
            )
            return self.get(value.job_uuid)

    def guard_mutation(self, mutation, affected) -> None:
        from .core import MaterialConflictError

        for item in affected:
            if (
                item.aggregate_type == "resource_template"
                and self.service.connection.execute(
                    "SELECT 1 FROM material_execution_hold h JOIN material m ON m.material_uuid=h.aggregate_uuid "
                    "WHERE h.aggregate_type='material' AND m.template_uuid=?",
                    (item.aggregate_uuid,),
                ).fetchone()
            ):
                raise MaterialConflictError("模板仍被未结算物理动作使用", conflict="active_claim")
            row = self.service.connection.execute(
                "SELECT job_uuid FROM material_execution_hold WHERE aggregate_type=? AND aggregate_uuid=?",
                (item.aggregate_type, item.aggregate_uuid),
            ).fetchone()
            if row is None:
                continue
            own_settlement = getattr(self.service, "_physical_settlement_job", None) == row[0]
            lifecycle = mutation.job_uuid == row[0] and mutation.operation in {
                "consume_inventory_reservation",
                "quarantine_inventory_reservation",
            }
            if not own_settlement and not lifecycle:
                raise MaterialConflictError(f"物料或库位仍由未结算动作 {row[0]} 占用", conflict="active_claim")

    def validate(self, receipt: MaterialExecutionReceipt) -> dict:
        with self.service._transfer_lock, self.service.write() as conn:
            conn.execute("SAVEPOINT physical_receipt_validation")
            try:
                return self.settle(receipt)
            finally:
                conn.execute("ROLLBACK TO physical_receipt_validation")
                conn.execute("RELEASE physical_receipt_validation")

    def settle(self, receipt: MaterialExecutionReceipt) -> dict:
        from .core import MaterialConflictError

        service = self.service
        with service._transfer_lock, service.write() as conn:
            record = self.get(receipt.job_uuid)
            if record is None or record["claim_uuid"] != receipt.claim_uuid or record["plan_hash"] != receipt.plan_hash:
                raise MaterialConflictError("物理回执与派发身份不一致")
            if record["state"] == "settled":
                if _json(record["receipt"]) != _json(receipt):
                    raise MaterialConflictError("同一物理动作已经收到不同的结算回执")
                return record
            if record["state"] != "prepared":
                raise MaterialConflictError("已释放的物理动作不能首次结算")
            plan = MaterialExecutionPrepare.model_validate(record["request"])
            site_ids = set(plan.reserved_site_uuids)
            for move in plan.moves:
                site_ids.add(move.target_site_uuid)
                if move.source_site_uuid:
                    site_ids.add(move.source_site_uuid)
            required_holds = {("material", identity) for identity in record["snapshot"]} | {
                ("site", identity) for identity in site_ids
            }
            actual_holds = {
                (row[0], row[1])
                for row in conn.execute(
                    "SELECT aggregate_type,aggregate_uuid FROM material_execution_hold WHERE job_uuid=?",
                    (receipt.job_uuid,),
                )
            }
            if actual_holds != required_holds:
                raise MaterialConflictError("物理准备记录缺少完整占用，不能结算")
            if sorted(_json(item) for item in receipt.material_transfer_receipts) != sorted(
                _json(item) for item in plan.moves
            ):
                raise MaterialConflictError("搬运回执必须完整匹配冻结的来源和目标库位")
            targets = [item.target_material_uuid for item in receipt.material_aliquot_receipts]
            if len(set(targets)) != len(targets) or set(targets) != set(
                plan.aliquot.target_material_uuids if plan.aliquot else []
            ):
                raise MaterialConflictError("分装回执必须完整覆盖冻结的目标集合")
            for identity, snapshot in record["snapshot"].items():
                current = service.get_material(identity)
                if {key: snapshot[key] for key in ("parent_material_uuid", "site_uuid", "data_state_hash")} != {
                    "parent_material_uuid": current.material.parent_material_uuid,
                    "site_uuid": self._site(identity),
                    "data_state_hash": current.data.state_hash,
                }:
                    raise MaterialConflictError("物理动作期间库位或内容物发生变化，需要人工核实")
            now = service._now_ms()
            service._physical_settlement_job = receipt.job_uuid
            try:
                for index, move in enumerate(plan.moves):
                    service.move_material(
                        self._mutation(plan, f"move:{index}", "move_material"),
                        MaterialMove(material_uuid=move.material_uuid, destination_site_uuid=move.target_site_uuid),
                    )
                if plan.aliquot:
                    self._aliquot(plan, receipt)
                self._inventory(plan, receipt, now)
                for identity in record["snapshot"]:
                    service._validate_material_capacity(identity)
                conn.execute(
                    "UPDATE material_execution SET state='settled',receipt_json=?,result_json=?,version=version+1,updated_at_ms=? WHERE job_uuid=?",
                    (_json(receipt), _json({"materials": sorted(record["snapshot"])}), now, receipt.job_uuid),
                )
                conn.execute("DELETE FROM material_execution_hold WHERE job_uuid=?", (receipt.job_uuid,))
            finally:
                service._physical_settlement_job = None
            return self.get(receipt.job_uuid)

    @staticmethod
    def _mutation(plan, suffix, operation):
        return InventoryMutation(
            command_uuid=plan.job_uuid,
            effect_key="physical." + suffix,
            operation=operation,
            actor_type="scheduler",
            job_uuid=plan.job_uuid,
        )

    def _aliquot(self, plan, receipt):
        from .core import MaterialConflictError

        source = self.service.get_material(plan.aliquot.source_material_uuid)
        contents = source.data.substances
        positive = [item for item in contents if item.quantity > 0]
        if not positive and all(item.actual_quantity == 0 for item in receipt.material_aliquot_receipts):
            return
        if not positive:
            raise MaterialConflictError("分装来源没有可核算的内容物")
        # 混合物按来源各组分比例分配；不可换算的物性须由操作员补全。
        unit = receipt.material_aliquot_receipts[0].quantity_unit
        quantities = [
            (self._convert(item.quantity, item.quantity_unit, unit, item) if item.quantity else 0.0)
            for item in contents
        ]
        total = math.fsum(quantities)
        amounts = [
            self._convert(item.actual_quantity, item.quantity_unit, unit) for item in receipt.material_aliquot_receipts
        ]
        used = math.fsum(amounts)
        if not math.isfinite(total) or total <= 0 or used > total + quantity_tolerance(total, used):
            raise MaterialConflictError("分装实际用量超过来源内容物")
        updates = {
            source.material.material_uuid: [
                item.model_copy(update={"quantity": max(0.0, item.quantity * (1 - used / total))}) for item in contents
            ]
        }
        for target_receipt, amount in zip(receipt.material_aliquot_receipts, amounts):
            target = self.service.get_material(target_receipt.target_material_uuid)
            merged = list(target.data.substances)
            for original in positive:
                part = original.model_copy(
                    update={"substance_uuid": None, "quantity": original.quantity * amount / total}
                )
                if part.quantity == 0:
                    continue
                match = next(
                    (
                        index
                        for index, item in enumerate(merged)
                        if self._chemical_key(item) == self._chemical_key(part)
                    ),
                    None,
                )
                if match is None:
                    merged.append(part)
                else:
                    item = merged[match]
                    extra = self._convert(part.quantity, part.quantity_unit, item.quantity_unit, part)
                    merged[match] = item.model_copy(update={"quantity": item.quantity + extra})
            updates[target_receipt.target_material_uuid] = merged
        for identity, substances in updates.items():
            current = self.service.get_material(identity)
            value = MaterialDataWrite.model_validate(
                {
                    **{key: getattr(current.data, key) for key in MaterialDataWrite.model_fields},
                    "substances": substances,
                }
            )
            from .core import MaterialNoChangeError

            try:
                self.service.put_data(self._mutation(plan, "aliquot:" + identity, "put_data"), identity, value)
            except MaterialNoChangeError:
                pass

    @staticmethod
    def _chemical_key(item):
        return _json(
            {
                key: value
                for key, value in item.model_dump(mode="json").items()
                if key not in {"substance_uuid", "quantity", "quantity_unit", "chemical"}
            }
        )

    @staticmethod
    def _convert(quantity, source, target, substance=None):
        from .core import MaterialValidationError

        result = convert_quantity(
            quantity,
            source,
            target,
            inventory_type="reagent",
            physical_state=substance.physical_state if substance else "unknown",
            density_g_per_ml=substance.chemical.density_g_per_ml if substance and substance.chemical else None,
        )
        if result is None or not math.isfinite(result[0]):
            raise MaterialValidationError(f"实际数量不能可靠地从 {source} 换算为 {target}")
        return result[0]

    def _inventory(self, plan, receipt, timestamp):
        from .core import MaterialConflictError

        if not plan.inventory_reservation_uuid:
            if receipt.inventory_consumptions:
                raise MaterialConflictError("动作没有冻结的库存预留，不能提交扣量回执")
            return
        reservation = self.service.get_inventory_reservation(plan.inventory_reservation_uuid)
        if reservation.status == "active":
            from unilabos.protocol.materials import InventoryReservationTransition

            reservation = self.service.consume_inventory_reservation(
                self._mutation(plan, "consume", "consume_inventory_reservation"),
                InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid, reason="成功回执结算"),
            ).data
        if reservation.status not in {"consumed", "quarantined"}:
            raise MaterialConflictError("实际用量结算需要已扣量或待核实隔离的库存")
        if receipt.inventory_consumptions is None:
            return  # 旧设备不声明实际量时保留原有开始扣减语义。
        expected = {(item.key, item.lot_uuid): item for item in reservation.items if item.kind == "lot"}
        actual = {(item.key, item.lot_uuid): item for item in receipt.inventory_consumptions}
        if len(actual) != len(receipt.inventory_consumptions) or set(expected) != set(actual):
            raise MaterialConflictError("实际用量回执必须完整匹配库存分配项")
        deltas = {}
        for key, item in expected.items():
            usage = actual[key]
            used = self._convert(usage.actual_quantity, usage.quantity_unit, item.unit)
            deltas[item.lot_uuid] = deltas.get(item.lot_uuid, 0) + item.quantity - used
        for identity, difference in deltas.items():
            self.apply_actual_lot_quantity(plan.job_uuid, reservation.reservation_uuid, identity, difference)

    def apply_actual_lot_quantity(self, job_uuid: str, reservation_uuid: str, lot_uuid: str, difference: float):
        """按实际用量校正一个批次（``difference`` = 预留量 − 实际量，正数退回）。

        物理回执结算与人工数量核对（``reconcile-quantity``）共用这一条幂等路径：效果键固定为
        ``(job_uuid, physical.inventory:<lot>)``、请求只含批次 / 差额 / 预留。同一差额重放不再改账，
        不同差额按 ``command_uuid/effect_key`` 冲突拒绝，因此同一作业的同一批次最多校正一次，绝不重复扣减。
        """

        from .core import MaterialConflictError, _Applied
        from unilabos.protocol.materials import AggregateVersion, InventoryLotRead, MutationResult

        if not math.isfinite(difference):
            raise MaterialConflictError("库存实际用量合计必须为有限数值")
        mutation = InventoryMutation(
            command_uuid=job_uuid,
            effect_key="physical.inventory:" + lot_uuid,
            operation="settle_actual_inventory",
            actor_type="scheduler",
            job_uuid=job_uuid,
        )

        def apply(timestamp):
            lot = self.service.get_lot(lot_uuid)
            updated = self.service._advance_lot(
                lot, total_delta=difference, available_delta=difference, timestamp=timestamp
            )
            self.service.update_lot(updated)
            state_hash = self.service._lot_state_hash(updated)
            sequence = self.service._ledger(
                mutation,
                aggregate_type="lot",
                aggregate_uuid=lot_uuid,
                operation="inventory.actual_quantity",
                previous_version=lot.version,
                aggregate_version=updated.version,
                state_hash=state_hash,
                delta={
                    "quantity_delta": difference,
                    "unit": lot.unit,
                    "reservation_uuid": reservation_uuid,
                    "quantity_total_before": lot.quantity_total,
                    "quantity_total_after": updated.quantity_total,
                },
                timestamp=timestamp,
            )
            return _Applied(
                self.service._lot_read(updated),
                [
                    AggregateVersion(
                        aggregate_type="lot",
                        aggregate_uuid=lot_uuid,
                        version=updated.version,
                        state_hash=state_hash,
                    )
                ],
                [sequence],
            )

        return self.service._run_mutation(
            mutation,
            {"lot_uuid": lot_uuid, "quantity_delta": difference, "reservation_uuid": reservation_uuid},
            MutationResult[InventoryLotRead],
            apply,
        )

    def release(self, job_uuid: str, value: MaterialExecutionRelease) -> dict:
        from .core import MaterialConflictError

        with self.service.write() as conn:
            record = self.get(job_uuid)
            if record is None:
                raise MaterialConflictError("物理动作不存在")
            if record["state"] == "released" and record["confirmation"] == value.model_dump(mode="json"):
                return record
            if record["version"] != value.expected_version or record["state"] != "prepared":
                raise MaterialConflictError("物理动作状态已变化，请刷新后重新核实")
            if not value.reason.strip():
                raise MaterialConflictError("必须记录现场核实说明")
            conn.execute(
                "UPDATE material_execution SET state='released',confirmation_json=?,version=version+1,updated_at_ms=? WHERE job_uuid=?",
                (_json(value), self.service._now_ms(), job_uuid),
            )
            conn.execute("DELETE FROM material_execution_hold WHERE job_uuid=?", (job_uuid,))
            return self.get(job_uuid)
