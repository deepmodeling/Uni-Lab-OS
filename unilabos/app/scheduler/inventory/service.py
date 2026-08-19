"""仓储业务写操作.

每个写操作 = 单个 SQLite 事务：业务行更新 + inventory_ledger + sync_outbox 一起提交。
领域不变量在此层强制（数量非负 / available+reserved<=total / barcode active 唯一 /
(workflow_id,node_id,attempt) 幂等 / move 不改数量）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from typing import Any, Callable, Dict, Iterator, List, Optional

from unilabos.app.scheduler.inventory.domain import (
    ACTIVE_INSTANCE_STATES,
    CommandRejected,
    DuplicateBarcode,
    InstanceState,
    InsufficientStock,
    InvariantViolation,
    MaterialRequirement,
    NotFound,
    ReservationState,
    VersionConflict,
    check_instance_transition,
    check_lot_invariants,
    new_event_id,
)
from unilabos.app.scheduler.inventory.material_type import material_type_from_template
from unilabos.app.scheduler.inventory.site_spec import materialize_template_sites
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.utils.tracing import add_event, inject_trace_context, span

_ACTIVE_STATES_TUPLE = tuple(s.value for s in ACTIVE_INSTANCE_STATES)


def _traced_operation(operation: str):
    """为仓储写操作生成低基数 span；只提取标识/版本，不记录业务 payload。"""

    def decorate(function):
        function_signature = signature(function)

        @wraps(function)
        def wrapped(self, *args, **kwargs):
            try:
                arguments = function_signature.bind_partial(self, *args, **kwargs).arguments
            except TypeError:
                arguments = {}
            attributes: Dict[str, Any] = {
                "inventory.operation": operation,
                "edge.uuid": getattr(self, "edge_id", ""),
                "lab.id": getattr(self, "lab_id", ""),
            }
            keys = {
                "workflow_id": "workflow.uuid",
                "node_id": "workflow.node.uuid",
                "attempt": "workflow.node.attempt",
                "template_id": "resource_template.uuid",
                "lot_id": "inventory.lot.id",
                "edge_uuid": "material.uuid",
                "instance_uuid": "material.uuid",
                "parent_uuid": "material.parent.uuid",
                "causation_id": "inventory.causation.id",
                "expected_version": "inventory.expected_version",
            }
            for argument_name, attribute_name in keys.items():
                value = arguments.get(argument_name)
                if value not in (None, ""):
                    attributes[attribute_name] = value
            with span(f"material.{operation}", attributes=attributes):
                return function(self, *args, **kwargs)

        return wrapped

    return decorate


class InventoryService:
    """Edge 仓储唯一事实源的业务入口."""

    def __init__(
        self,
        store: InventoryStore,
        edge_id: str = "edge-default",
        lab_id: str = "edge-lab",
        time_fn: Callable[[], float] = time.time,
        monitor: Any = None,
    ):
        self.store = store
        self.edge_id = edge_id
        self.lab_id = lab_id
        self._time_fn = time_fn
        # 实时监控总线（duck-typed emit(channel, type, data)）；None = 关闭
        self._monitor = monitor
        # 事务内暂存的监控事件（提交成功才发布，回滚即丢弃）
        self._tx_local = threading.local()

    def _now_ms(self) -> int:
        return int(self._time_fn() * 1000)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """业务事务 + 监控事件缓冲.

        Command execution may establish one ambient transaction around a
        service method.  Nested service calls reuse that connection, so the
        command claim, business mutation, ledger/outbox and final result commit
        atomically instead of opening a second crash window.
        """
        ambient = getattr(self._tx_local, "connection", None)
        if ambient is not None:
            yield ambient
            return

        events: List[Dict[str, Any]] = []
        self._tx_local.events = events
        store = self.store
        try:
            with store.transaction() as conn:
                self._tx_local.connection = conn
                yield conn
        finally:
            self._tx_local.connection = None
            self._tx_local.events = None
        # 到这里说明事务已提交（异常路径在 finally 清理后向上抛，不会执行到此）
        if self._monitor is not None:
            for data in events:
                try:
                    self._monitor.emit("material", data.pop("event_type"), data)
                except Exception:  # noqa: BLE001 - 监控故障不影响业务
                    pass

    @contextmanager
    def command_transaction(self) -> Iterator[sqlite3.Connection]:
        """命令原子事务入口；commands.py 之外不应写 processed_command."""

        with self._tx() as conn:
            yield conn

    @contextmanager
    def command_attempt(self, conn: sqlite3.Connection) -> Iterator[None]:
        """用 SAVEPOINT 隔离可预期拒绝，避免提交半截业务变更.

        领域拒绝需要持久化为幂等结果，因此不能回滚整个命令事务；这里只回滚
        handler 产生的业务/ledger/outbox，并同步丢弃尚未发布的监控事件。
        """

        events = getattr(self._tx_local, "events", None)
        checkpoint = len(events) if isinstance(events, list) else 0
        conn.execute("SAVEPOINT inventory_command_attempt")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO SAVEPOINT inventory_command_attempt")
            conn.execute("RELEASE SAVEPOINT inventory_command_attempt")
            if isinstance(events, list):
                del events[checkpoint:]
            raise
        else:
            conn.execute("RELEASE SAVEPOINT inventory_command_attempt")

    # ------------------------------------------------------------------
    # 事务内公共 helper
    # ------------------------------------------------------------------

    def _emit(
        self,
        conn: sqlite3.Connection,
        now_ms: int,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        payload: Dict[str, Any],
        causation_id: str = "",
        actor: str = "",
        reason: str = "",
    ) -> None:
        """同事务写 ledger + outbox."""
        common_attributes = {
            "inventory.aggregate.type": aggregate_type,
            "inventory.aggregate.id": aggregate_id,
            "inventory.aggregate.version": aggregate_version,
            "inventory.event.type": event_type,
            "inventory.causation.id": causation_id,
        }
        add_event("inventory.ledger.append", common_attributes)
        trace_carrier: Dict[str, Any] = {}
        inject_trace_context(trace_carrier)
        InventoryStore.tx_insert_ledger(
            conn, now_ms, event_type, aggregate_type, aggregate_id, payload,
            actor=actor, reason=reason, causation_id=causation_id,
            trace_id=str(trace_carrier.get("trace_id") or ""),
            span_id=str(trace_carrier.get("span_id") or ""),
        )
        InventoryStore.tx_insert_outbox(
            conn, new_event_id(now_ms), self.edge_id, self.lab_id,
            aggregate_type, aggregate_id, aggregate_version, event_type,
            now_ms, causation_id, payload,
            traceparent=str(trace_carrier.get("traceparent") or ""),
            tracestate=str(trace_carrier.get("tracestate") or ""),
            trace_id=str(trace_carrier.get("trace_id") or ""),
            span_id=str(trace_carrier.get("span_id") or ""),
        )
        add_event("inventory.outbox.enqueue", common_attributes)
        # 事务缓冲监控事件：commit 成功后由 _tx 发布到 material 通道
        buffered = getattr(self._tx_local, "events", None)
        if buffered is not None:
            buffered.append(
                {
                    "event_type": event_type,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "version": aggregate_version,
                    "payload": payload,
                    "reason": reason,
                    "actor": actor,
                }
            )

    @staticmethod
    def _tx_get_lot(conn: sqlite3.Connection, lot_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM inventory_lot WHERE lot_id = ?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound(f"lot {lot_id} not found")
        return dict(row)

    @staticmethod
    def _tx_get_instance(conn: sqlite3.Connection, edge_uuid: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM material_instance WHERE edge_uuid = ?", (edge_uuid,)).fetchone()
        if row is None:
            raise NotFound(f"instance {edge_uuid} not found")
        return dict(row)

    def _tx_update_lot_quantities(
        self,
        conn: sqlite3.Connection,
        lot: Dict[str, Any],
        d_total: float = 0.0,
        d_available: float = 0.0,
        d_reserved: float = 0.0,
    ) -> Dict[str, Any]:
        total = lot["quantity_total"] + d_total
        available = lot["quantity_available"] + d_available
        reserved = lot["quantity_reserved"] + d_reserved
        # 浮点残余归零
        total, available, reserved = (0.0 if abs(v) < 1e-9 else v for v in (total, available, reserved))
        check_lot_invariants(total, available, reserved)
        new_version = lot["version"] + 1
        conn.execute(
            "UPDATE inventory_lot SET quantity_total = ?, quantity_available = ?, "
            "quantity_reserved = ?, version = ? WHERE lot_id = ?",
            (total, available, reserved, new_version, lot["lot_id"]),
        )
        lot = dict(lot)
        lot.update(quantity_total=total, quantity_available=available,
                   quantity_reserved=reserved, version=new_version)
        return lot

    def _tx_set_instance_status(
        self,
        conn: sqlite3.Connection,
        instance: Dict[str, Any],
        target: InstanceState,
    ) -> Dict[str, Any]:
        previous = instance["status"]
        check_instance_transition(InstanceState(instance["status"]), target)
        new_version = instance["version"] + 1
        conn.execute(
            "UPDATE material_instance SET status = ?, version = ? WHERE edge_uuid = ?",
            (target.value, new_version, instance["edge_uuid"]),
        )
        instance = dict(instance)
        instance.update(status=target.value, version=new_version)
        add_event(
            "material.state.transition",
            {
                "material.instance.id": instance["edge_uuid"],
                "material.state.from": previous,
                "material.state.to": target.value,
                "inventory.aggregate.version": new_version,
            },
        )
        return instance

    # ------------------------------------------------------------------
    # template / 品类模板
    # ------------------------------------------------------------------

    @_traced_operation("template.upsert")
    def upsert_template(
        self,
        template_id: str,
        name: str = "",
        category: str = "",
        spec: Optional[Dict[str, Any]] = None,
        actor: str = "",
        causation_id: str = "",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """新建或更新资源模板；更新使用乐观版本并产生 ledger/outbox."""
        template_id = template_id.strip()
        if not template_id:
            raise CommandRejected("template_id required")
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_resource_template WHERE template_id = ?", (template_id,)
            ).fetchone()
            if row is None:
                if expected_version not in (None, 0):
                    raise VersionConflict(
                        f"expected version {expected_version}, current 0"
                    )
                version = 1
                conn.execute(
                    "INSERT INTO inventory_resource_template"
                    "(template_id, name, category, spec_json, version) VALUES (?,?,?,?,?)",
                    (
                        template_id,
                        name,
                        category,
                        json.dumps(spec or {}, ensure_ascii=False),
                        version,
                    ),
                )
                event_type = "template.created"
            else:
                current = dict(row)
                self._tx_check_version(current, expected_version)
                version = current["version"] + 1
                conn.execute(
                    "UPDATE inventory_resource_template SET name = ?, category = ?, spec_json = ?, "
                    "version = ? WHERE template_id = ?",
                    (
                        name if name != "" else current["name"],
                        category if category != "" else current["category"],
                        json.dumps(
                            spec if spec is not None else json.loads(current["spec_json"]),
                            ensure_ascii=False,
                        ),
                        version,
                        template_id,
                    ),
                )
                event_type = "template.updated"
            result = conn.execute(
                "SELECT * FROM inventory_resource_template WHERE template_id = ?", (template_id,)
            ).fetchone()
            assert result is not None
            self._emit(
                conn,
                now,
                "template",
                template_id,
                version,
                event_type,
                {
                    "name": result["name"],
                    "category": result["category"],
                    "spec": json.loads(result["spec_json"]),
                },
                causation_id=causation_id,
                actor=actor,
            )
        return dict(result)

    @_traced_operation("template.delete")
    def delete_template(
        self,
        template_id: str,
        actor: str = "",
        causation_id: str = "",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """删除无批次/实例引用的模板；有引用时拒绝，避免悬空领域对象."""
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_resource_template WHERE template_id = ?", (template_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"template {template_id} not found")
            current = dict(row)
            self._tx_check_version(current, expected_version)
            lot_count = conn.execute(
                "SELECT COUNT(*) FROM inventory_lot WHERE template_id = ?", (template_id,)
            ).fetchone()[0]
            instance_count = conn.execute(
                "SELECT COUNT(*) FROM material_instance WHERE template_id = ?", (template_id,)
            ).fetchone()[0]
            if lot_count or instance_count:
                raise CommandRejected(
                    f"template {template_id} is referenced by "
                    f"{lot_count} lot(s) and {instance_count} instance(s)"
                )
            conn.execute(
                "DELETE FROM inventory_resource_template WHERE template_id = ?", (template_id,)
            )
            self._emit(
                conn,
                now,
                "template",
                template_id,
                current["version"] + 1,
                "template.deleted",
                {},
                causation_id=causation_id,
                actor=actor,
            )
        return {"template_id": template_id, "deleted": True}

    # ------------------------------------------------------------------
    # inbound / 登记
    # ------------------------------------------------------------------

    @_traced_operation("inbound")
    def inbound_lot(
        self,
        template_id: str,
        quantity: float,
        unit: str = "",
        batch_no: str = "",
        expiry: str = "",
        lot_id: str = "",
        warehouse_zone_id: str = "",
        actor: str = "",
        causation_id: str = "",
    ) -> Dict[str, Any]:
        """批次入库（数量层）；lot_id 已存在则追加数量."""
        if quantity <= 0:
            raise InvariantViolation(f"inbound quantity must be > 0, got {quantity}")
        now = self._now_ms()
        lot_id = lot_id or f"lot-{uuid.uuid4().hex[:16]}"
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM inventory_lot WHERE lot_id = ?", (lot_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO inventory_lot(lot_id, template_id, batch_no, unit, quantity_total, "
                    "quantity_available, quantity_reserved, expiry, quarantined, warehouse_zone_id, "
                    "created_at, version) VALUES (?,?,?,?,?,?,0,?,0,?,?,1)",
                    (lot_id, template_id, batch_no, unit, quantity, quantity, expiry,
                     warehouse_zone_id, now),
                )
                lot = self._tx_get_lot(conn, lot_id)
                event_type = "lot.created"
            else:
                lot = self._tx_update_lot_quantities(conn, dict(row), d_total=quantity, d_available=quantity)
                event_type = "lot.inbound"
            self._emit(
                conn, now, "lot", lot_id, lot["version"], event_type,
                {"template_id": template_id, "quantity": quantity, "unit": unit,
                 "batch_no": batch_no, "expiry": expiry,
                 "quantity_total": lot["quantity_total"],
                 "quantity_available": lot["quantity_available"]},
                causation_id=causation_id, actor=actor,
            )
        return lot

    @_traced_operation("instance.register")
    def register_instance(
        self,
        template_id: str = "",
        lot_id: str = "",
        barcode: str = "",
        edge_uuid: str = "",
        legacy_cloud_id: str = "",
        parent_uuid: str = "",
        slot_id: str = "",
        actor: str = "",
        causation_id: str = "",
        origin: str = "",
    ) -> Dict[str, Any]:
        """实例登记（实体层）.

        edge_uuid 由 Edge 生成且永久稳定；cloud UUID 只写入 legacy_cloud_id 映射，
        永远不会覆盖 edge_uuid。
        """
        now = self._now_ms()
        edge_uuid = edge_uuid or f"mi-{uuid.uuid4().hex}"
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM material_instance WHERE edge_uuid = ?", (edge_uuid,)
            ).fetchone()
            if existing is not None:
                inst = dict(existing)
                # 幂等重放：仅补 legacy mapping，绝不改 edge_uuid/status
                if legacy_cloud_id and not inst["legacy_cloud_id"]:
                    conn.execute(
                        "UPDATE material_instance SET legacy_cloud_id = ? WHERE edge_uuid = ?",
                        (legacy_cloud_id, edge_uuid),
                    )
                    inst["legacy_cloud_id"] = legacy_cloud_id
                return inst
            if barcode:
                placeholders = ",".join("?" for _ in _ACTIVE_STATES_TUPLE)
                dup = conn.execute(
                    f"SELECT edge_uuid FROM material_instance WHERE barcode = ? "
                    f"AND status IN ({placeholders})",
                    (barcode, *_ACTIVE_STATES_TUPLE),
                ).fetchone()
                if dup is not None:
                    raise DuplicateBarcode(f"barcode {barcode} already active on {dup['edge_uuid']}")
            template = conn.execute(
                "SELECT name, resource_type, config_info, model "
                "FROM resource_template WHERE uuid = ? AND deleted_at IS NULL",
                (template_id,),
            ).fetchone()
            material_type = material_type_from_template(
                dict(template) if template is not None else None,
                material_name=edge_uuid,
                root=not bool(parent_uuid),
            )
            conn.execute(
                "INSERT INTO material_instance(edge_uuid, legacy_cloud_id, lot_id, template_id, "
                "barcode, status, parent_uuid, version) VALUES (?,?,?,?,?,?,?,1)",
                (edge_uuid, legacy_cloud_id, lot_id, template_id, barcode,
                 InstanceState.WAREHOUSE.value, ""),
            )
            # material.type is canonical and server-derived.  The legacy View
            # deliberately has no write authority for it, so set it on the
            # canonical row in the same transaction.
            conn.execute(
                "UPDATE material SET type = ? WHERE uuid = ?",
                (material_type, edge_uuid),
            )
            try:
                materialize_template_sites(
                    conn,
                    edge_uuid,
                    dict(template) if template is not None else None,
                )
            except ValueError as exc:
                raise CommandRejected(str(exc)) from exc
            if parent_uuid:
                self._tx_upsert_relation(conn, parent_uuid, slot_id, edge_uuid)
            registered_payload = {
                "template_id": template_id,
                "lot_id": lot_id,
                "barcode": barcode,
                "legacy_cloud_id": legacy_cloud_id,
                "type": material_type,
                "parent_uuid": parent_uuid,
                "slot_id": slot_id,
            }
            if origin:
                registered_payload["origin"] = origin
            self._emit(
                conn, now, "instance", edge_uuid, 1, "instance.registered",
                registered_payload,
                causation_id=causation_id, actor=actor,
            )
            inst = self._tx_get_instance(conn, edge_uuid)
        return inst

    # ------------------------------------------------------------------
    # interactive deduct / 人工扣减
    # ------------------------------------------------------------------

    @_traced_operation("deduct")
    def deduct(
        self,
        *,
        quantity: float,
        operator: str,
        lot_id: str = "",
        template_id: str = "",
        unit: str = "",
        reason: str = "",
        instantiate: bool = False,
        edge_uuid: str = "",
        barcode: str = "",
        actor: str = "",
        causation_id: str = "",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """直接扣减可用量；与 workflow reservation 相互独立。

        实体扣减必须由单一批次满足，以便新实例保留确定的 lot_id；试剂扣减
        可按创建时间 FIFO 跨批次。所有 lot 变更和可选实例创建都复用同一事务。
        """

        if quantity <= 0:
            raise InvariantViolation(f"deduct quantity must be > 0, got {quantity}")
        if not operator.strip():
            raise CommandRejected("deduct requires operator for audit")
        if bool(lot_id) == bool(template_id):
            raise CommandRejected("deduct requires exactly one of lot_id or template_id")
        if expected_version is not None and not lot_id:
            raise CommandRejected("expected_version requires a concrete lot_id")

        now = self._now_ms()
        requirement = MaterialRequirement(
            lot_id=lot_id,
            template_id=template_id,
            quantity=quantity,
            unit=unit,
        )
        deductions: List[Dict[str, Any]] = []
        created_instance: Optional[Dict[str, Any]] = None
        with self._tx() as conn:
            candidates = self._tx_candidate_lots(conn, requirement)
            if lot_id and not candidates:
                raise NotFound(f"available lot {lot_id} not found")
            if expected_version is not None:
                self._tx_check_version(candidates[0], expected_version)

            matching: List[Dict[str, Any]] = []
            for candidate in candidates:
                candidate_unit = str(candidate.get("unit") or "")
                if unit and candidate_unit and candidate_unit != unit:
                    if lot_id:
                        raise CommandRejected(
                            f"lot {lot_id} unit {candidate_unit!r} does not match {unit!r}"
                        )
                    continue
                matching.append(candidate)

            if instantiate:
                matching = [
                    candidate
                    for candidate in matching
                    if float(candidate["quantity_available"]) + 1e-9 >= quantity
                ][:1]

            remaining = quantity
            for candidate in matching:
                if remaining <= 1e-9:
                    break
                take = min(float(candidate["quantity_available"]), remaining)
                if take <= 0:
                    continue
                updated = self._tx_update_lot_quantities(
                    conn,
                    candidate,
                    d_total=-take,
                    d_available=-take,
                )
                item = {
                    "lot_id": str(updated["lot_id"]),
                    "template_id": str(updated["template_id"]),
                    "quantity": take,
                    "unit": str(updated.get("unit") or unit),
                    "quantity_total": float(updated["quantity_total"]),
                    "quantity_available": float(updated["quantity_available"]),
                    "version": int(updated["version"]),
                }
                deductions.append(item)
                remaining -= take
                self._emit(
                    conn,
                    now,
                    "lot",
                    str(updated["lot_id"]),
                    int(updated["version"]),
                    "lot.deducted",
                    {
                        **item,
                        "operator": operator,
                        "reason": reason,
                        "kind": "resource" if instantiate else "reagent",
                    },
                    causation_id=causation_id,
                    actor=actor,
                    reason=reason,
                )

            if remaining > 1e-9:
                selector = lot_id or f"template:{template_id}"
                raise InsufficientStock(f"short {remaining} of {selector}")

            if instantiate:
                selected = deductions[0]
                created_instance = self.register_instance(
                    template_id=str(selected["template_id"]),
                    lot_id=str(selected["lot_id"]),
                    barcode=barcode,
                    edge_uuid=edge_uuid,
                    actor=actor,
                    causation_id=causation_id,
                    origin="deduct",
                )

        result: Dict[str, Any] = {
            "quantity": quantity,
            "unit": unit or (str(deductions[0]["unit"]) if deductions else ""),
            "operator": operator,
            "deductions": deductions,
        }
        if created_instance is not None:
            result["instance"] = created_instance
        return result

    @_traced_operation("deduct.revert")
    def revert_deduct(
        self,
        *,
        deduct_command_id: str,
        operator: str,
        reason: str,
        actor: str = "",
        causation_id: str = "",
    ) -> Dict[str, Any]:
        """完整补偿原扣减；恢复数量，保留并终态化已创建实例。"""

        if not operator.strip() or not reason.strip():
            raise CommandRejected("deduct revert requires operator and reason")
        now = self._now_ms()
        restored: List[Dict[str, Any]] = []
        reverted_instance: Optional[Dict[str, Any]] = None
        with self._tx() as conn:
            original_rows = conn.execute(
                "SELECT * FROM inventory_ledger WHERE op_type = 'lot.deducted' "
                "AND causation_id = ? ORDER BY ledger_id ASC",
                (deduct_command_id,),
            ).fetchall()
            if not original_rows:
                raise NotFound(f"deduct command {deduct_command_id} not found")

            revert_rows = conn.execute(
                "SELECT delta_json FROM inventory_ledger "
                "WHERE op_type = 'lot.deduct_reverted' ORDER BY ledger_id ASC"
            ).fetchall()
            for row in revert_rows:
                payload = json.loads(str(row["delta_json"] or "{}"))
                if payload.get("deduct_command_id") == deduct_command_id:
                    raise CommandRejected(
                        f"deduct command {deduct_command_id} already reverted"
                    )

            for row in original_rows:
                payload = json.loads(str(row["delta_json"] or "{}"))
                quantity = float(payload.get("quantity") or 0)
                lot_id = str(row["aggregate_id"])
                lot = self._tx_get_lot(conn, lot_id)
                lot = self._tx_update_lot_quantities(
                    conn, lot, d_total=quantity, d_available=quantity
                )
                item = {
                    "lot_id": lot_id,
                    "quantity": quantity,
                    "quantity_total": float(lot["quantity_total"]),
                    "quantity_available": float(lot["quantity_available"]),
                    "version": int(lot["version"]),
                }
                restored.append(item)
                self._emit(
                    conn,
                    now,
                    "lot",
                    lot_id,
                    int(lot["version"]),
                    "lot.deduct_reverted",
                    {
                        **item,
                        "deduct_command_id": deduct_command_id,
                        "operator": operator,
                        "reason": reason,
                    },
                    causation_id=causation_id,
                    actor=actor,
                    reason=reason,
                )

            instance_row = conn.execute(
                "SELECT aggregate_id FROM inventory_ledger "
                "WHERE op_type = 'instance.registered' AND causation_id = ? "
                "ORDER BY ledger_id ASC LIMIT 1",
                (deduct_command_id,),
            ).fetchone()
            if instance_row is not None:
                instance = self._tx_get_instance(conn, str(instance_row["aggregate_id"]))
                if instance["status"] != InstanceState.WAREHOUSE.value:
                    raise CommandRejected(
                        f"deducted instance {instance['edge_uuid']} is {instance['status']}, cannot revert"
                    )
                instance = self._tx_set_instance_status(
                    conn, instance, InstanceState.DISCARDED
                )
                conn.execute(
                    "DELETE FROM resource_relation WHERE child_uuid = ?",
                    (instance["edge_uuid"],),
                )
                conn.execute(
                    "UPDATE material_instance SET parent_uuid = '' WHERE edge_uuid = ?",
                    (instance["edge_uuid"],),
                )
                instance["parent_uuid"] = ""
                self._emit(
                    conn,
                    now,
                    "instance",
                    str(instance["edge_uuid"]),
                    int(instance["version"]),
                    "instance.deduct_reverted",
                    {
                        "deduct_command_id": deduct_command_id,
                        "operator": operator,
                        "reason": reason,
                    },
                    causation_id=causation_id,
                    actor=actor,
                    reason=reason,
                )
                reverted_instance = instance

        result: Dict[str, Any] = {
            "deduct_command_id": deduct_command_id,
            "operator": operator,
            "restored": restored,
        }
        if reverted_instance is not None:
            result["instance"] = reverted_instance
        return result

    # ------------------------------------------------------------------
    # reserve / release / consume（workflow 幂等键）
    # ------------------------------------------------------------------

    @_traced_operation("reserve")
    def reserve_workflow(
        self,
        workflow_id: str,
        node_requirements: Dict[str, List[MaterialRequirement]],
        attempt: int = 1,
        actor: str = "",
        causation_id: str = "",
    ) -> Dict[str, Any]:
        """整 DAG 预留（all-or-nothing，单事务）.

        每个节点一行 reservation；任一节点不足则整体回滚并抛 InsufficientStock。
        (workflow_id, node_id, attempt) 幂等：已有 active/consumed 预留的节点跳过。
        """
        now = self._now_ms()
        created: List[str] = []
        with self._tx() as conn:
            for node_id, requirements in node_requirements.items():
                if not requirements:
                    continue
                existing = conn.execute(
                    "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? "
                    "AND attempt = ?",
                    (workflow_id, node_id, attempt),
                ).fetchone()
                if existing is not None and existing["status"] in (
                    ReservationState.ACTIVE.value, ReservationState.CONSUMED.value,
                ):
                    continue  # 幂等重放
                amounts = self._tx_allocate(conn, now, workflow_id, node_id, requirements,
                                            actor, causation_id)
                reservation_id = f"rsv-{uuid.uuid4().hex[:16]}"
                if existing is not None:
                    conn.execute(
                        "UPDATE inventory_reservation SET status = ?, amounts_json = ?, "
                        "version = version + 1 WHERE workflow_id = ? AND node_id = ? AND attempt = ?",
                        (ReservationState.ACTIVE.value, json.dumps(amounts), workflow_id,
                         node_id, attempt),
                    )
                    reservation_id = existing["reservation_id"]
                else:
                    conn.execute(
                        "INSERT INTO inventory_reservation(reservation_id, workflow_id, node_id, "
                        "attempt, status, amounts_json, created_at, version) VALUES (?,?,?,?,?,?,?,1)",
                        (reservation_id, workflow_id, node_id, attempt,
                         ReservationState.ACTIVE.value, json.dumps(amounts), now),
                    )
                created.append(node_id)
                self._emit(
                    conn, now, "reservation", reservation_id, 1, "reservation.created",
                    {"workflow_id": workflow_id, "node_id": node_id, "attempt": attempt,
                     "amounts": amounts},
                    causation_id=causation_id, actor=actor,
                )
        return {"workflow_id": workflow_id, "reserved_nodes": created}

    def _tx_allocate(
        self,
        conn: sqlite3.Connection,
        now: int,
        workflow_id: str,
        node_id: str,
        requirements: List[MaterialRequirement],
        actor: str,
        causation_id: str,
    ) -> Dict[str, Any]:
        """事务内为一个节点分配预留：FIFO 扣 lot available→reserved；实例置 RESERVED."""
        amounts: Dict[str, Any] = {"lots": {}, "instances": []}
        for req in requirements:
            if req.is_instance_requirement():
                inst = self._tx_resolve_instance(conn, req)
                if inst["status"] != InstanceState.WAREHOUSE.value:
                    raise InsufficientStock(
                        f"instance {inst['edge_uuid']} not in warehouse (status={inst['status']})"
                    )
                inst = self._tx_set_instance_status(conn, inst, InstanceState.RESERVED)
                amounts["instances"].append(inst["edge_uuid"])
                self._emit(
                    conn, now, "instance", inst["edge_uuid"], inst["version"], "instance.reserved",
                    {"workflow_id": workflow_id, "node_id": node_id},
                    causation_id=causation_id, actor=actor,
                )
            elif req.quantity > 0:
                remaining = req.quantity
                candidates = self._tx_candidate_lots(conn, req)
                for lot in candidates:
                    if remaining <= 1e-9:
                        break
                    take = min(lot["quantity_available"], remaining)
                    if take <= 0:
                        continue
                    lot = self._tx_update_lot_quantities(conn, lot, d_available=-take, d_reserved=take)
                    amounts["lots"][lot["lot_id"]] = amounts["lots"].get(lot["lot_id"], 0.0) + take
                    remaining -= take
                    self._emit(
                        conn, now, "lot", lot["lot_id"], lot["version"], "lot.reserved",
                        {"workflow_id": workflow_id, "node_id": node_id, "quantity": take,
                         "quantity_available": lot["quantity_available"],
                         "quantity_reserved": lot["quantity_reserved"]},
                        causation_id=causation_id, actor=actor,
                    )
                if remaining > 1e-9:
                    raise InsufficientStock(
                        f"node {node_id}: short {remaining} of "
                        f"{req.lot_id or 'template:' + req.template_id}"
                    )
        return amounts

    @staticmethod
    def _tx_resolve_instance(conn: sqlite3.Connection, req: MaterialRequirement) -> Dict[str, Any]:
        if req.instance_uuid:
            row = conn.execute(
                "SELECT * FROM material_instance WHERE edge_uuid = ?", (req.instance_uuid,)
            ).fetchone()
        else:
            placeholders = ",".join("?" for _ in _ACTIVE_STATES_TUPLE)
            row = conn.execute(
                f"SELECT * FROM material_instance WHERE barcode = ? AND status IN ({placeholders})",
                (req.barcode, *_ACTIVE_STATES_TUPLE),
            ).fetchone()
        if row is None:
            raise NotFound(f"instance {req.instance_uuid or req.barcode} not found")
        return dict(row)

    def _tx_candidate_lots(
        self, conn: sqlite3.Connection, req: MaterialRequirement
    ) -> List[Dict[str, Any]]:
        if req.lot_id:
            row = conn.execute(
                "SELECT * FROM inventory_lot WHERE lot_id = ? AND quarantined = 0", (req.lot_id,)
            ).fetchone()
            return [dict(row)] if row is not None else []
        # FIFO：created_at 升序，同毫秒按插入序（rowid）
        rows = conn.execute(
            "SELECT * FROM inventory_lot WHERE template_id = ? AND quarantined = 0 "
            "AND quantity_available > 0 ORDER BY created_at ASC, rowid ASC",
            (req.template_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_traced_operation("consume")
    def consume_reservation(
        self,
        workflow_id: str,
        node_id: str,
        attempt: int = 1,
        parent_uuid: str = "",
        slot_id: str = "",
        actor: str = "",
        causation_id: str = "",
    ) -> Dict[str, Any]:
        """节点开始：预留 → 实际消费（lot reserved/total 扣减；实例 deploy 上台）.

        幂等：已 consumed 直接返回；无预留（无物料节点）no-op。
        """
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? "
                "AND attempt = ?",
                (workflow_id, node_id, attempt),
            ).fetchone()
            if row is None:
                return {"status": "no_reservation"}
            rsv = dict(row)
            if rsv["status"] == ReservationState.CONSUMED.value:
                return {"status": "already_consumed", "reservation_id": rsv["reservation_id"]}
            if rsv["status"] != ReservationState.ACTIVE.value:
                raise CommandRejected(
                    f"reservation {rsv['reservation_id']} in {rsv['status']}, cannot consume"
                )
            amounts = json.loads(rsv["amounts_json"])
            for lot_id, qty in amounts.get("lots", {}).items():
                lot = self._tx_get_lot(conn, lot_id)
                lot = self._tx_update_lot_quantities(conn, lot, d_total=-qty, d_reserved=-qty)
                self._emit(
                    conn, now, "lot", lot_id, lot["version"], "lot.consumed",
                    {"workflow_id": workflow_id, "node_id": node_id, "quantity": qty,
                     "quantity_total": lot["quantity_total"]},
                    causation_id=causation_id, actor=actor,
                )
            for inst_uuid in amounts.get("instances", []):
                inst = self._tx_get_instance(conn, inst_uuid)
                inst = self._tx_set_instance_status(conn, inst, InstanceState.BENCH)
                if parent_uuid:
                    self._tx_upsert_relation(conn, parent_uuid, slot_id, inst_uuid)
                self._emit(
                    conn, now, "instance", inst_uuid, inst["version"], "instance.deployed",
                    {"workflow_id": workflow_id, "node_id": node_id,
                     "parent_uuid": parent_uuid, "slot_id": slot_id},
                    causation_id=causation_id, actor=actor,
                )
            conn.execute(
                "UPDATE inventory_reservation SET status = ?, version = version + 1 "
                "WHERE reservation_id = ?",
                (ReservationState.CONSUMED.value, rsv["reservation_id"]),
            )
            self._emit(
                conn, now, "reservation", rsv["reservation_id"], rsv["version"] + 1,
                "reservation.consumed",
                {"workflow_id": workflow_id, "node_id": node_id, "attempt": attempt},
                causation_id=causation_id, actor=actor,
            )
        return {"status": "consumed", "reservation_id": rsv["reservation_id"], "amounts": amounts}

    @_traced_operation("release")
    def release_reservation(
        self,
        workflow_id: str,
        node_id: str,
        attempt: int = 1,
        reason: str = "",
        actor: str = "",
        causation_id: str = "",
    ) -> Dict[str, Any]:
        """释放未消费的预留：lot reserved→available，实例 RESERVED→WAREHOUSE。幂等."""
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? "
                "AND attempt = ?",
                (workflow_id, node_id, attempt),
            ).fetchone()
            if row is None:
                return {"status": "no_reservation"}
            rsv = dict(row)
            if rsv["status"] != ReservationState.ACTIVE.value:
                return {"status": f"noop_{rsv['status']}", "reservation_id": rsv["reservation_id"]}
            self._tx_release_amounts(conn, now, workflow_id, node_id, json.loads(rsv["amounts_json"]),
                                     reason, actor, causation_id)
            conn.execute(
                "UPDATE inventory_reservation SET status = ?, version = version + 1 "
                "WHERE reservation_id = ?",
                (ReservationState.RELEASED.value, rsv["reservation_id"]),
            )
            self._emit(
                conn, now, "reservation", rsv["reservation_id"], rsv["version"] + 1,
                "reservation.released",
                {"workflow_id": workflow_id, "node_id": node_id, "attempt": attempt,
                 "reason": reason},
                causation_id=causation_id, actor=actor, reason=reason,
            )
        return {"status": "released", "reservation_id": rsv["reservation_id"]}

    def _tx_release_amounts(
        self,
        conn: sqlite3.Connection,
        now: int,
        workflow_id: str,
        node_id: str,
        amounts: Dict[str, Any],
        reason: str,
        actor: str,
        causation_id: str,
    ) -> None:
        for lot_id, qty in amounts.get("lots", {}).items():
            lot = self._tx_get_lot(conn, lot_id)
            lot = self._tx_update_lot_quantities(conn, lot, d_available=qty, d_reserved=-qty)
            self._emit(
                conn, now, "lot", lot_id, lot["version"], "lot.released",
                {"workflow_id": workflow_id, "node_id": node_id, "quantity": qty,
                 "quantity_available": lot["quantity_available"]},
                causation_id=causation_id, actor=actor, reason=reason,
            )
        for inst_uuid in amounts.get("instances", []):
            inst = self._tx_get_instance(conn, inst_uuid)
            if inst["status"] == InstanceState.RESERVED.value:
                inst = self._tx_set_instance_status(conn, inst, InstanceState.WAREHOUSE)
                self._emit(
                    conn, now, "instance", inst_uuid, inst["version"], "instance.released",
                    {"workflow_id": workflow_id, "node_id": node_id},
                    causation_id=causation_id, actor=actor, reason=reason,
                )

    @_traced_operation("quarantine")
    def quarantine_reservation(
        self,
        workflow_id: str,
        node_id: str,
        attempt: int = 1,
        reason: str = "node_failed",
        actor: str = "",
        causation_id: str = "",
    ) -> Dict[str, Any]:
        """节点失败但物料已物理使用：实例转 QUARANTINED（人工复核），lot 不虚假加回."""
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? "
                "AND attempt = ?",
                (workflow_id, node_id, attempt),
            ).fetchone()
            if row is None:
                return {"status": "no_reservation"}
            rsv = dict(row)
            if rsv["status"] != ReservationState.CONSUMED.value:
                return {"status": f"noop_{rsv['status']}", "reservation_id": rsv["reservation_id"]}
            amounts = json.loads(rsv["amounts_json"])
            for inst_uuid in amounts.get("instances", []):
                inst = self._tx_get_instance(conn, inst_uuid)
                if inst["status"] in (InstanceState.BENCH.value, InstanceState.IN_USE.value):
                    inst = self._tx_set_instance_status(conn, inst, InstanceState.QUARANTINED)
                    self._emit(
                        conn, now, "instance", inst_uuid, inst["version"], "instance.quarantined",
                        {"workflow_id": workflow_id, "node_id": node_id, "reason": reason},
                        causation_id=causation_id, actor=actor, reason=reason,
                    )
            conn.execute(
                "UPDATE inventory_reservation SET status = ?, version = version + 1 "
                "WHERE reservation_id = ?",
                (ReservationState.QUARANTINED.value, rsv["reservation_id"]),
            )
            self._emit(
                conn, now, "reservation", rsv["reservation_id"], rsv["version"] + 1,
                "reservation.quarantined",
                {"workflow_id": workflow_id, "node_id": node_id, "attempt": attempt,
                 "reason": reason},
                causation_id=causation_id, actor=actor, reason=reason,
            )
        return {"status": "quarantined", "reservation_id": rsv["reservation_id"]}

    @_traced_operation("workflow.release")
    def release_workflow(
        self, workflow_id: str, reason: str = "workflow_cancelled",
        actor: str = "", causation_id: str = "",
    ) -> Dict[str, Any]:
        """cancel/restart：释放该 workflow 全部 active 预留（依据 DB 状态，不依赖内存）."""
        released: List[str] = []
        for rsv in self.store.reservations_for_workflow(workflow_id):
            if rsv["status"] == ReservationState.ACTIVE.value:
                self.release_reservation(
                    workflow_id, rsv["node_id"], rsv["attempt"],
                    reason=reason, actor=actor, causation_id=causation_id,
                )
                released.append(rsv["node_id"])
        return {"workflow_id": workflow_id, "released_nodes": released}

    # ------------------------------------------------------------------
    # deploy / move / consume / discard / adjust / content
    # ------------------------------------------------------------------

    @staticmethod
    def _tx_upsert_relation(
        conn: sqlite3.Connection, parent_uuid: str, slot_id: str, child_uuid: str
    ) -> None:
        """relation 主键是 child_uuid：transfer 时旧父关系被原子替换，源端不残留.

        单一父不变量：`material_instance.parent_uuid` 与 `relation.parent_uuid`
        始终一致——它映射 Backend canonical `material.parent_uuid`。relation 只补充
        「父物料的哪个具名位」（`slot_id` = PLR site 名 = Backend `site.name`）；
        稳定的 `site.uuid` 是身份，不能用名称替代。每次 upsert 同步父列。

        参数：
            conn: 当前 SQLite 事务连接。
            parent_uuid: 父 Material UUID。
            slot_id: 父 Material 内的 Site 语义名。
            child_uuid: 子 Material UUID。

        返回：
            None；变更写入当前事务。
        """
        current = conn.execute(
            "SELECT version FROM resource_relation WHERE child_uuid = ?",
            (child_uuid,),
        ).fetchone()
        version = int(current["version"]) + 1 if current is not None else 1
        if current is not None:
            conn.execute(
                "DELETE FROM resource_relation WHERE child_uuid = ?", (child_uuid,)
            )
        conn.execute(
            "INSERT INTO resource_relation(parent_uuid, slot_id, child_uuid, version) "
            "VALUES (?,?,?,?)",
            (parent_uuid, slot_id, child_uuid, version),
        )
        conn.execute(
            "UPDATE material_instance SET parent_uuid = ? WHERE edge_uuid = ?",
            (parent_uuid, child_uuid),
        )

    def check_parent_consistency(self) -> List[Dict[str, Any]]:
        """只读列出 parent_uuid 与 relation 的确定性冲突."""

        return self.store.parent_consistency_issues()

    @_traced_operation("parent.repair")
    def repair_parent_consistency(
        self,
        actor: str,
        reason: str,
        causation_id: str = "",
    ) -> Dict[str, Any]:
        """只填补空 parent_uuid，不覆盖冲突值、不删除孤儿 relation.

        老 v3 数据可能由早期 ``register_instance`` 写出 relation、却漏写实例
        parent_uuid。relation 提供唯一且确定的父时可审计修复；双方非空但不一致
        或 relation 指向不存在实例时只报告，由操作者人工裁决。
        """

        if not actor or not reason:
            raise CommandRejected("parent consistency repair requires actor and reason")
        now = self._now_ms()
        repaired: List[str] = []
        unresolved: List[Dict[str, Any]] = []
        with self._tx() as conn:
            for issue in InventoryStore.tx_parent_consistency_issues(conn):
                if (
                    issue["kind"] == "parent_mismatch"
                    and not issue["instance_parent_uuid"]
                    and issue["relation_parent_uuid"]
                ):
                    edge_uuid = issue["child_uuid"]
                    row = self._tx_get_instance(conn, edge_uuid)
                    version = row["version"] + 1
                    conn.execute(
                        "UPDATE material_instance SET parent_uuid = ?, version = ? "
                        "WHERE edge_uuid = ? AND parent_uuid = ''",
                        (issue["relation_parent_uuid"], version, edge_uuid),
                    )
                    self._emit(
                        conn,
                        now,
                        "instance",
                        edge_uuid,
                        version,
                        "instance.parent_repaired",
                        {
                            "from_parent": "",
                            "parent_uuid": issue["relation_parent_uuid"],
                            "repair_source": "resource_relation",
                        },
                        causation_id=causation_id,
                        actor=actor,
                        reason=reason,
                    )
                    repaired.append(edge_uuid)
                else:
                    unresolved.append(issue)
        return {"repaired": repaired, "unresolved": unresolved}

    @_traced_operation("deploy")
    def deploy_instance(
        self, edge_uuid: str, parent_uuid: str = "", slot_id: str = "",
        actor: str = "", causation_id: str = "", expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        now = self._now_ms()
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            inst = self._tx_set_instance_status(conn, inst, InstanceState.BENCH)
            if parent_uuid:
                self._tx_upsert_relation(conn, parent_uuid, slot_id, edge_uuid)
            self._emit(
                conn, now, "instance", edge_uuid, inst["version"], "instance.deployed",
                {"parent_uuid": parent_uuid, "slot_id": slot_id},
                causation_id=causation_id, actor=actor,
            )
        return inst

    @_traced_operation("move")
    def move_instance(
        self, edge_uuid: str, parent_uuid: str, slot_id: str = "",
        actor: str = "", causation_id: str = "", expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """move/transfer：只改物理层级关系，不改任何库存数量."""
        now = self._now_ms()
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            old = conn.execute(
                "SELECT * FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
            ).fetchone()
            self._tx_upsert_relation(conn, parent_uuid, slot_id, edge_uuid)
            new_version = inst["version"] + 1
            conn.execute(
                "UPDATE material_instance SET version = ? WHERE edge_uuid = ?",
                (new_version, edge_uuid),
            )
            self._emit(
                conn, now, "instance", edge_uuid, new_version, "instance.moved",
                {"from_parent": old["parent_uuid"] if old else "",
                 "from_slot": old["slot_id"] if old else "",
                 "to_parent": parent_uuid, "to_slot": slot_id},
                causation_id=causation_id, actor=actor,
            )
            inst = self._tx_get_instance(conn, edge_uuid)
        return inst

    @_traced_operation("detach")
    def detach_instance(
        self,
        edge_uuid: str,
        actor: str = "",
        causation_id: str = "",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """解除物理父关系；实例与库存数量保持不变，重复 detach 为幂等 no-op."""
        now = self._now_ms()
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            old = conn.execute(
                "SELECT * FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
            ).fetchone()
            if old is None and not inst.get("parent_uuid"):
                return inst
            if old is not None:
                conn.execute(
                    "DELETE FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
                )
            version = inst["version"] + 1
            # 单一父不变量：取下即脱离父物料（回到顶层/未分配）
            conn.execute(
                "UPDATE material_instance SET version = ?, parent_uuid = '' WHERE edge_uuid = ?",
                (version, edge_uuid),
            )
            self._emit(
                conn,
                now,
                "instance",
                edge_uuid,
                version,
                "instance.detached",
                {
                    "from_parent": (
                        old["parent_uuid"] if old is not None else inst["parent_uuid"]
                    ),
                    "from_slot": old["slot_id"] if old is not None else "",
                },
                causation_id=causation_id,
                actor=actor,
            )
            inst = self._tx_get_instance(conn, edge_uuid)
        return inst

    @_traced_operation("set_parent")
    def set_instance_parent(
        self, edge_uuid: str, parent_uuid: str = "", slot_id: Optional[str] = None,
        actor: str = "", causation_id: str = "", expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """设置或清除父 Material（映射 Backend `material.parent_uuid`，保持单一父）。

        资源只有一个父层级：父物料 + 可选具名位（slot_id = PLR site 名，
        ↔ Backend `site.name`）。`site.uuid` 是稳定身份，不由名称或序号替代。语义：

        - parent_uuid 空串：顶层——父与具名位一并清除；
        - parent_uuid 非空、slot_id 空/None：有父但不占具名位（sites 讨论稿场景
          「父子关系不需要用 site 表达」），relation 行删除；
        - parent_uuid 非空、slot_id 非空：父 + 具名位，与 deploy/move 同一不变量
          （relation.parent 始终等于 parent_uuid 列）。

        沿 parent 链防环（云端由业务层校验，Edge 同等语义）。

        参数：
            edge_uuid: 当前 Material UUID（旧兼容列名仍为 edge_uuid）。
            parent_uuid: 新父 Material UUID；空串表示清除父级。
            slot_id: 可选 Site 语义名，不是 Site UUID。
            actor: 领域操作主体。
            causation_id: 触发本次变更的命令或事件 UUID。
            expected_version: 可选乐观锁版本。

        返回：
            更新后的 Material 实例字典。
        """
        now = self._now_ms()
        new_slot = slot_id or ""
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            old_parent = inst.get("parent_uuid", "")
            old_rel = conn.execute(
                "SELECT slot_id FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
            ).fetchone()
            old_slot = old_rel["slot_id"] if old_rel else ""
            if parent_uuid == old_parent and new_slot == old_slot:
                return inst  # 幂等 no-op
            if parent_uuid:
                if parent_uuid == edge_uuid:
                    raise CommandRejected("instance cannot be its own parent")
                parent = conn.execute(
                    "SELECT edge_uuid, status, parent_uuid FROM material_instance "
                    "WHERE edge_uuid = ?", (parent_uuid,),
                ).fetchone()
                if parent is None:
                    raise NotFound(f"parent instance {parent_uuid} not found")
                if parent["status"] not in {s.value for s in ACTIVE_INSTANCE_STATES}:
                    raise CommandRejected(
                        f"parent instance {parent_uuid} is {parent['status']}, not active"
                    )
                # 沿父链向上防环（链长即遍历深度）
                cursor, seen = parent["parent_uuid"], {parent_uuid}
                while cursor:
                    if cursor == edge_uuid or cursor in seen:
                        raise CommandRejected(
                            f"parent chain of {parent_uuid} would form a cycle"
                        )
                    seen.add(cursor)
                    row = conn.execute(
                        "SELECT parent_uuid FROM material_instance WHERE edge_uuid = ?",
                        (cursor,),
                    ).fetchone()
                    cursor = row["parent_uuid"] if row else ""
            new_version = inst["version"] + 1
            conn.execute(
                "UPDATE material_instance SET parent_uuid = ?, version = ? WHERE edge_uuid = ?",
                (parent_uuid, new_version, edge_uuid),
            )
            if parent_uuid and new_slot:
                self._tx_upsert_relation(conn, parent_uuid, new_slot, edge_uuid)
            else:
                conn.execute(
                    "DELETE FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
                )
            self._emit(
                conn, now, "instance", edge_uuid, new_version, "instance.parent_changed",
                {"from_parent": old_parent, "parent_uuid": parent_uuid,
                 "from_slot": old_slot, "slot_id": new_slot},
                causation_id=causation_id, actor=actor,
            )
            inst = self._tx_get_instance(conn, edge_uuid)
        return inst

    @_traced_operation("instance.consume")
    def consume_instance(
        self, edge_uuid: str, actor: str = "", causation_id: str = "",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._terminal_instance_op(
            edge_uuid, InstanceState.CONSUMED, "instance.consumed", "",
            actor, causation_id, expected_version,
        )

    @_traced_operation("discard")
    def discard_instance(
        self, edge_uuid: str, reason: str = "", actor: str = "", causation_id: str = "",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._terminal_instance_op(
            edge_uuid, InstanceState.DISCARDED, "instance.discarded", reason,
            actor, causation_id, expected_version,
        )

    def _terminal_instance_op(
        self,
        edge_uuid: str,
        target: InstanceState,
        event_type: str,
        reason: str,
        actor: str,
        causation_id: str,
        expected_version: Optional[int],
    ) -> Dict[str, Any]:
        """终态操作：删除物理关系（remove 真正持久化）+ 状态迁移."""
        now = self._now_ms()
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            inst = self._tx_set_instance_status(conn, inst, target)
            conn.execute("DELETE FROM resource_relation WHERE child_uuid = ?", (edge_uuid,))
            # 终态实例不再是任何物料的组成部分（历史保留在 ledger）
            conn.execute(
                "UPDATE material_instance SET parent_uuid = '' WHERE edge_uuid = ?",
                (edge_uuid,),
            )
            inst["parent_uuid"] = ""
            self._emit(
                conn, now, "instance", edge_uuid, inst["version"], event_type,
                {"reason": reason}, causation_id=causation_id, actor=actor, reason=reason,
            )
        return inst

    @_traced_operation("adjust")
    def adjust_lot(
        self,
        lot_id: str,
        new_total: float,
        reason: str,
        actor: str,
        causation_id: str = "",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """人工盘点调整：必须带 reason + actor（审计），调整 total 并同步 available."""
        if not reason or not actor:
            raise CommandRejected("adjust requires both reason and actor for audit")
        now = self._now_ms()
        with self._tx() as conn:
            lot = self._tx_get_lot(conn, lot_id)
            self._tx_check_version(lot, expected_version)
            delta = new_total - lot["quantity_total"]
            # available 跟随 total 变化；不允许把 total 调到低于已预留量
            lot = self._tx_update_lot_quantities(conn, lot, d_total=delta, d_available=delta)
            self._emit(
                conn, now, "lot", lot_id, lot["version"], "lot.adjusted",
                {"delta": delta, "new_total": lot["quantity_total"],
                 "quantity_available": lot["quantity_available"], "reason": reason},
                causation_id=causation_id, actor=actor, reason=reason,
            )
        return lot

    @_traced_operation("content.set")
    def update_content(
        self, instance_uuid: str, state: Dict[str, Any],
        actor: str = "", causation_id: str = "",
        expected_version: Optional[int] = None,
        event_type: str = "content.updated",
    ) -> Dict[str, Any]:
        """更新内容物状态（substance_content）."""
        now = self._now_ms()
        with self._tx() as conn:
            self._tx_get_instance(conn, instance_uuid)
            row = conn.execute(
                "SELECT * FROM substance_content WHERE instance_uuid = ?", (instance_uuid,)
            ).fetchone()
            if row is None and expected_version not in (None, 0):
                raise VersionConflict(
                    f"expected version {expected_version}, current 0"
                )
            if row is not None:
                self._tx_check_version(dict(row), expected_version)
            version = (row["version"] + 1) if row is not None else 1
            encoded_state = json.dumps(state, ensure_ascii=False)
            if row is None:
                conn.execute(
                    "INSERT INTO substance_content(instance_uuid, state_json, version) "
                    "VALUES (?,?,?)",
                    (instance_uuid, encoded_state, version),
                )
            else:
                conn.execute(
                    "UPDATE substance_content SET state_json=?, version=? "
                    "WHERE instance_uuid=?",
                    (encoded_state, version, instance_uuid),
                )
            self._emit(
                conn, now, "content", instance_uuid, version, event_type,
                {"state": state}, causation_id=causation_id, actor=actor,
            )
        return {"instance_uuid": instance_uuid, "version": version, "state": state}

    @_traced_operation("content.clear")
    def clear_content(
        self,
        instance_uuid: str,
        actor: str = "",
        causation_id: str = "",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """清空内容物但保留行与递增版本，避免 optimistic version 回退."""
        return self.update_content(
            instance_uuid,
            {},
            actor=actor,
            causation_id=causation_id,
            expected_version=expected_version,
            event_type="content.cleared",
        )

    @staticmethod
    def _tx_check_version(row: Dict[str, Any], expected_version: Optional[int]) -> None:
        """乐观并发：expected_version 不匹配直接 reject（禁止 Last-Write-Wins）."""
        if expected_version is not None and row["version"] != expected_version:
            raise VersionConflict(
                f"expected version {expected_version}, current {row['version']}"
            )
