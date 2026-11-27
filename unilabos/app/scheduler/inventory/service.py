"""仓储业务写操作.

每个写操作 = 单个 SQLite 事务：业务行更新 + inventory_ledger + sync_outbox 一起提交。
领域不变量在此层强制（数量非负 / available+reserved<=total / barcode active 唯一 /
(workflow_id,node_id,attempt) 幂等 / move 不改数量）。
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from inspect import signature
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionConflict,
    assert_inventory_mutation_unclaimed,
    validate_physical_settlement_credentials,
)
from unilabos.app.scheduler.inventory.domain import (
    ACTIVE_INSTANCE_STATES,
    CommandRejected,
    DuplicateBarcode,
    InstanceState,
    InsufficientStock,
    InvariantViolation,
    MaterialClaimedByOtherTask,
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
    NotFound,
    ReservationState,
    VersionConflict,
    check_instance_transition,
    check_lot_invariants,
    new_event_id,
)
from unilabos.app.scheduler.inventory.material_io import MaterialIOService
from unilabos.app.scheduler.inventory.source_selection import material_claim_owner, material_source_selector_rank
from unilabos.app.scheduler.inventory.station_resource import (
    MaterialAliquotCommand,
    MaterialTransferCommand,
    SqliteStationResourceInventory,
    StationResourceError,
    StationResourceInventory,
)
from unilabos.app.scheduler.inventory.store import (
    InventoryStore,
    SiteOccupancyConflict,
    clear_site_occupancy,
    set_site_occupancy,
)
from unilabos.app.scheduler.inventory.workflow_quantity import (
    WorkflowQuantityInventoryAuthority,
    WorkflowQuantityReservationError,
    subtract_quantity,
)
from unilabos.utils.tracing import add_event, inject_trace_context, span
from unilabos.workflow.resource_lock_key import site_lock_key

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


def _material_place_label(connection: sqlite3.Connection, parent_uuid: str, slot_id: str) -> str:
    """把父物料名称和槽位拼成台账里的位置。"""

    parent_name = ""
    if parent_uuid:
        row = connection.execute(
            "SELECT name FROM material WHERE uuid=?",
            (parent_uuid,),
        ).fetchone()
        parent_name = str(row["name"]) if row is not None and row["name"] else parent_uuid
    if parent_name and slot_id:
        return f"{parent_name} / {slot_id}"
    return parent_name or slot_id


class _ConnectionStore:
    """把事务内的 sqlite 连接包成库位地址解析需要的只读查询接口。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def query_all(self, sql: str, parameters: Any = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, parameters)]


def _address_names_site(conn: Any, owner_material_uuid: str, name: str, site_uuid: str) -> bool:
    """``name`` 是指向 ``owner_material_uuid`` 上 ``site_uuid`` 的上层物料地址时为真。"""

    from unilabos.app.scheduler.inventory.site_address import addresses_on_owner

    address = addresses_on_owner(_ConnectionStore(conn), owner_material_uuid, name)
    return address is not None and str(address.site_uuid) == site_uuid


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
        """装配本地库存权威及其工站资源窄接口。

        参数：``store`` 是本地库存 SQLite 适配器；``edge_id`` 与 ``lab_id``
        标识事件来源；``time_fn`` 提供可替换毫秒时钟；``monitor`` 是可选遥测
        发布器。返回：无。异常：构造不读写业务行；工站资源适配器仅保存同一
        Store 与 ``move_instance`` 写入口，不创建第二库存权威。
        """

        self.store = store
        self._workflow_release_guard: Callable[[str], bool] | None = None
        self.edge_id = edge_id
        self.lab_id = lab_id
        self._time_fn = time_fn
        # 实时监控总线（duck-typed emit(channel, type, data)）；None = 关闭
        self._monitor = monitor
        # 事务内暂存的监控事件（提交成功才发布，回滚即丢弃）
        self._tx_local = threading.local()
        # ``_station_resources`` 是调度器唯一可见的设备/库位/转运库存接缝；
        # SQL、父链遍历和物理结算全部留在 inventory 模块内部。
        self._station_resources: StationResourceInventory = (
            SqliteStationResourceInventory(
                store,
                settle_material_aliquot=self._settle_claimed_material_aliquot,
                settle_material_transfer=self._settle_claimed_material_transfer,
                correct_inventory=self.correct_inventory,
            )
        )
        self._material_io = MaterialIOService(store)

    @property
    def station_resources(self) -> StationResourceInventory:
        """返回工站设备、库位（Site）和转运事实的窄库存接口。

        参数：无。返回：与本服务共享同一库存权威和写事务的稳定适配器。异常：
        不访问数据库，不主动抛出异常；调用方不得替换该接口或直接读取 Store。
        """

        return self._station_resources

    @property
    def material_io(self) -> MaterialIOService:
        """返回与库存权威共享同一事务边界的 Material I/O 服务。"""

        return self._material_io

    def describe_wait_resources(
        self,
        resources: Sequence[Mapping[str, str]],
    ) -> tuple[dict[str, str], ...]:
        """通过工站资源窄接口补齐等待物料与库位的展示名称。"""

        return self._station_resources.describe_wait_resources(resources)

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
        """命令原子事务入口。库存命令总线与 Material I/O 对账共用 processed_command；后者只写 material_io: 前缀，不走本入口的静默重放。"""

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
        if target not in (InstanceState.CONSUMED, InstanceState.DISCARDED):
            deleted = conn.execute(
                "SELECT deleted_at FROM material WHERE uuid = ?",
                (instance["edge_uuid"],),
            ).fetchone()
            if deleted is not None and deleted["deleted_at"] is not None:
                # 兼容视图触发器会在非终态时清空 deleted_at；已软删除（如整盒卸下）
                # 的物料只能停留在终态，拒绝任何让它重新进入有效库存的迁移
                raise CommandRejected(
                    f"instance {instance['edge_uuid']} 已软删除，不能迁移到 {target.value}"
                )
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
            conn.execute(
                "INSERT INTO material_instance(edge_uuid, legacy_cloud_id, lot_id, template_id, "
                "barcode, status, parent_uuid, version) VALUES (?,?,?,?,?,?,?,1)",
                (edge_uuid, legacy_cloud_id, lot_id, template_id, barcode,
                 InstanceState.WAREHOUSE.value, ""),
            )
            if parent_uuid:
                self._tx_assert_physical_mutation_unclaimed(
                    conn,
                    edge_uuid=edge_uuid,
                    related_material_uuids=(parent_uuid,),
                    target_parent_uuid=parent_uuid,
                    target_slot_id=slot_id,
                )
                self._tx_upsert_relation(conn, parent_uuid, slot_id, edge_uuid)
            self._emit(
                conn, now, "instance", edge_uuid, 1, "instance.registered",
                {"template_id": template_id, "lot_id": lot_id, "barcode": barcode,
                 "legacy_cloud_id": legacy_cloud_id, "parent_uuid": parent_uuid, "slot_id": slot_id},
                causation_id=causation_id, actor=actor,
            )
            inst = self._tx_get_instance(conn, edge_uuid)
        return inst

    # ------------------------------------------------------------------
    # reserve / release / consume（workflow 幂等键）
    # ------------------------------------------------------------------

    @_traced_operation("resolve_shared_source")
    def resolve_shared_workflow_materials(
        self,
        workflow_id: str,
        node_requirements: Dict[str, List[MaterialRequirement]],
    ) -> Dict[str, Any]:
        """只读解析共享来源，不创建任务独占库存预留。

        参数：``workflow_id`` 是用于追踪的工作流任务身份；``node_requirements``
        按来源节点提供实例型需求。返回：与 ``reserve_workflow`` 相同形状的确定性
        ``allocations``，但 ``reserved_nodes`` 为空。异常：数量型需求、实例不存在
        或当前不可用时抛 ``CommandRejected``/``InsufficientStock``。整个解析在一个
        SQLite 事务快照内完成，不写 ``inventory_reservation`` 或实例状态。
        """

        allocations: Dict[str, List[str]] = {}
        allocation_sites: Dict[str, Dict[str, str]] = {}
        with self._tx() as conn:
            for node_id, requirements in node_requirements.items():
                selected: List[str] = []
                for requirement in requirements:
                    if not requirement.is_instance_requirement():
                        raise CommandRejected(
                            "共享来源只支持实例型物料，数量型库存必须任务独占"
                        )
                    instance = self._tx_resolve_instance(conn, requirement)
                    if instance["status"] != InstanceState.WAREHOUSE.value:
                        raise InsufficientStock(
                            f"instance {instance['edge_uuid']} not in warehouse "
                            f"(status={instance['status']})"
                        )
                    selected.append(str(instance["edge_uuid"]))
                allocations[node_id] = selected
                allocation_sites[node_id] = {
                    material_uuid: site_uuid
                    for material_uuid in selected
                    if (
                        site_uuid := self._tx_instance_site_uuid(
                            conn,
                            material_uuid,
                        )
                    )
                }
        return {
            "workflow_id": workflow_id,
            "reserved_nodes": [],
            "allocations": allocations,
            "allocation_sites": allocation_sites,
        }

    def current_site_uuid(self, material_uuid: str) -> str:
        """读取物料当前占用库位。

        参数：``material_uuid`` 是物料实例身份。返回：当前库位 UUID，不在库位时
        返回空字符串。异常：数据库错误原样传播。该方法只读，不创建预留或改变
        库位占用。
        """

        with self._tx() as conn:
            return self._tx_instance_site_uuid(conn, material_uuid)

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
        if any(str(node_id).startswith("center-outbound:") for node_id in node_requirements):
            raise CommandRejected("出站产物保护命名空间不能用于普通预留")
        now = self._now_ms()
        created: List[str] = []
        allocations: Dict[str, List[str]] = {}
        with self._tx() as conn:
            for node_id, requirements in node_requirements.items():
                if not requirements:
                    continue
                amounts, was_created = self._tx_reserve_node(
                    conn,
                    now,
                    workflow_id,
                    node_id,
                    requirements,
                    attempt,
                    actor,
                    causation_id,
                )
                if was_created:
                    created.append(node_id)
                allocations[node_id] = list(amounts.get("instances", []))
        return {
            "workflow_id": workflow_id,
            "reserved_nodes": created,
            "allocations": allocations,
        }

    @_traced_operation("material_source.admit")
    def admit_material_sources(
        self,
        workflow_id: str,
        requests: List[MaterialSourceAdmissionRequest],
        attempt: int = 1,
        actor: str = "",
        causation_id: str = "",
    ) -> Dict[str, Any]:
        """原子准入并冻结一个工作流任务的全部物料来源绑定。

        参数：``workflow_id``/``attempt`` 标识任务尝试，``requests`` 是来源
        选择器与保管策略，``actor``/``causation_id`` 进入账本追踪。返回：每个
        来源节点的稳定物料分配，以及真正建立任务全程预留的节点。异常：策略、
        模板或幂等输入不一致抛 ``CommandRejected``；任一来源不可用抛
        ``InsufficientStock``，并由同一事务回滚整组新绑定与预留。
        """

        now = self._now_ms()
        allocations: Dict[str, List[str]] = {}
        reserved_nodes: List[str] = []
        node_ids = [request.node_id for request in requests]
        if not workflow_id or attempt < 1:
            raise CommandRejected("material source admission needs workflow_id and attempt >= 1")
        if any(not node_id for node_id in node_ids) or len(set(node_ids)) != len(node_ids):
            raise CommandRejected("material source admission node_id must be non-empty and unique")

        with self._tx() as conn:
            # 先冻结选择范围最窄的来源，避免无库位共享来源抢占同一请求中
            # 已明确指定给独占搬运来源的物料。约束程度相同时仍先处理共享
            # 来源，使独占分配稳定避开固定共享物料且不受节点 UUID 字典序影响。
            ordered_requests = sorted(
                requests,
                key=lambda item: (
                    material_source_selector_rank(item.requirement),
                    item.custody_policy != "shared_source",
                    item.node_id,
                ),
            )
            for request in ordered_requests:
                self._validate_material_source_request(request)
                selector_json = json.dumps(
                    request.requirement.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing = conn.execute(
                    "SELECT * FROM inventory_material_source_binding "
                    "WHERE workflow_id=? AND node_id=? AND attempt=?",
                    (workflow_id, request.node_id, attempt),
                ).fetchone()
                if request.center_source_context is not None:
                    from unilabos.app.scheduler.inventory.source_bindings import verify_source_admission
                    verify_source_admission(conn, workflow_id, request, existing)
                if existing is not None:
                    binding = dict(existing)
                    if binding["status"] != "active":
                        raise CommandRejected(
                            f"material source binding {request.node_id} is already released"
                        )
                    expected = (
                        request.resource_template_uuid,
                        request.custody_policy,
                        selector_json,
                    )
                    actual = (
                        binding["resource_template_uuid"],
                        binding["custody_policy"],
                        binding["selector_json"],
                    )
                    if actual != expected:
                        raise CommandRejected(
                            f"material source binding {request.node_id} changed during replay"
                        )
                    if request.planned_material_uuid and binding["material_uuid"] != request.planned_material_uuid:
                        raise CommandRejected(f"物料来源 {request.node_id} 与已冻结资源锁的身份不一致")
                    allocations[request.node_id] = [binding["material_uuid"]]
                    continue

                expected_uuid = request.planned_material_uuid
                if request.custody_policy == "task_exclusive":
                    amounts, was_created = self._tx_reserve_node(
                        conn,
                        now,
                        workflow_id,
                        request.node_id,
                        [request.requirement],
                        attempt,
                        actor,
                        causation_id,
                    )
                    material_uuids = list(amounts.get("instances", []))
                    if was_created:
                        reserved_nodes.append(request.node_id)
                else:
                    # 只在共享来源写明了单个库位时，才看见本任务已经独占预留的物料。
                    # 没指定库位的自动选择若也看见预留，会按排序抢走独占来源刚绑上的那只。
                    instance = self._tx_resolve_instance(
                        conn,
                        request.requirement,
                        workflow_id=workflow_id,
                        include_same_task_reserved=bool(
                            str(request.requirement.site_uuid or "").strip()
                        ),
                    )
                    # 同一任务先独占搬走盒子，之后按同一库位再当共享废盒用。
                    # 预留还挂在本任务上，状态是 reserved，不能当成别人占用。
                    reserved_by_this_task = (
                        instance["status"] == InstanceState.RESERVED.value
                        and InventoryService._tx_instance_reserved_by_workflow(
                            conn,
                            str(instance["edge_uuid"]),
                            workflow_id,
                        )
                    )
                    if (
                        instance["status"] != InstanceState.WAREHOUSE.value
                        and not reserved_by_this_task
                    ):
                        raise InsufficientStock(
                            f"shared source {instance['edge_uuid']} is not available"
                        )
                    material_uuids = [instance["edge_uuid"]]

                if len(material_uuids) != 1:
                    raise CommandRejected(
                        f"material source {request.node_id} must bind exactly one material"
                    )
                material_uuid = material_uuids[0]
                if expected_uuid and material_uuid != expected_uuid:
                    raise CommandRejected(f"物料来源 {request.node_id} 与已冻结资源锁的身份不一致")
                from unilabos.app.scheduler.inventory.custody import require_source_custody_available
                require_source_custody_available(conn, material_uuid=material_uuid, task_uuid=workflow_id,
                                                policy=request.custody_policy)
                from unilabos.app.scheduler.inventory.material_inputs import material_input_blocker
                from unilabos.app.scheduler.resource_lock import material_lock_key

                if material_input_blocker(conn, [material_lock_key(material_uuid)], task_uuid=workflow_id) is not None:
                    raise MaterialClaimedByOtherTask("物料已为另一个中心调用的本地任务保留")
                from unilabos.app.scheduler.inventory.outbound import outbound_blocker
                if outbound_blocker(conn, [material_lock_key(material_uuid)]) is not None:
                    raise MaterialClaimedByOtherTask("物料仍由原任务的出站责任保护")
                conn.execute(
                    "INSERT INTO inventory_material_source_binding("
                    "binding_id,workflow_id,node_id,attempt,material_uuid,"
                    "resource_template_uuid,custody_policy,selector_json,status,created_at,version"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                    (
                        f"msb-{uuid.uuid4().hex[:16]}",
                        workflow_id,
                        request.node_id,
                        attempt,
                        material_uuid,
                        request.resource_template_uuid,
                        request.custody_policy,
                        selector_json,
                        "active",
                        now,
                    ),
                )
                if request.custody_policy == "task_exclusive":
                    from unilabos.app.scheduler.inventory.custody import material_subtree
                    reservation = conn.execute(
                        "SELECT amounts_json FROM inventory_reservation WHERE workflow_id=? AND node_id=? AND attempt=?",
                        (workflow_id, request.node_id, attempt),
                    ).fetchone()
                    if reservation is None:
                        raise CommandRejected("独占来源缺少原库存预留，不能创建半份保管责任")
                    amounts = json.loads(reservation["amounts_json"])
                    amounts["custody_material_uuids"] = sorted(material_subtree(conn, material_uuid))
                    conn.execute(
                        "UPDATE inventory_reservation SET amounts_json=? WHERE workflow_id=? AND node_id=? AND attempt=?",
                        (json.dumps(amounts), workflow_id, request.node_id, attempt),
                    )
                allocations[request.node_id] = [material_uuid]

        return {
            "workflow_id": workflow_id,
            "reserved_nodes": reserved_nodes,
            "allocations": allocations,
        }

    def admit_task_materials(
        self,
        workflow_id: str,
        requests: List[MaterialSourceAdmissionRequest],
        quantity_allocations: Sequence[Mapping[str, Any]],
        *,
        quantity_builder: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]] | None = None,
        attempt: int = 1,
    ) -> Dict[str, Any]:
        """在一个库存事务中准入全部来源实例与全部数量型库存。

        ``attempt`` 是来源作业的当前尝试；运行时来源正式重试后用新尝试的绑定键，
        不复用上一尝试已结束的绑定。
        """

        authority = WorkflowQuantityInventoryAuthority(
            self.store,
            edge_id=self.edge_id,
            lab_id=self.lab_id,
        )
        with self._tx() as connection:
            source_result = self.admit_material_sources(workflow_id, requests, attempt=attempt)
            if quantity_builder is not None:
                quantity_allocations = quantity_builder(source_result)
            source_allocations = source_result.get("allocations", {})
            if not isinstance(source_allocations, Mapping):
                raise CommandRejected(
                    "物料来源准入返回了非法分配结果"
                )
            for allocation in quantity_allocations:
                source_node_uuid = str(
                    allocation.get("material_source_node_uuid") or ""
                ).strip()
                if not source_node_uuid:
                    continue
                selected = source_allocations.get(source_node_uuid)
                if (
                    not isinstance(selected, list)
                    or len(selected) != 1
                    or str(selected[0]) != str(allocation.get("material_uuid") or "")
                ):
                    raise InsufficientStock(
                        "数量库存容器在物料准入前已变化"
                    )
            if quantity_allocations:
                authority.reserve_task(
                    workflow_id, quantity_allocations, connection=connection,
                    incremental=quantity_builder is not None,
                )
            return source_result

    @staticmethod
    def _validate_material_source_request(request: MaterialSourceAdmissionRequest) -> None:
        """验证物料来源准入请求的精确线格式与单实例不变量。

        参数：``request`` 是一个来源准入值对象。返回：验证成功不返回数据。
        异常：来源身份、策略、模板或实例需求不满足规范时抛
        ``CommandRejected``，禁止隐式降级为任务独占或数量型批次预留。
        """

        if str(request.node_id).startswith("center-outbound:"):
            raise CommandRejected("出站产物保护命名空间不能用于普通物料来源")
        if not request.node_id or not request.resource_template_uuid:
            raise CommandRejected("material source request needs node_id and resource template")
        if request.custody_policy not in {"task_exclusive", "shared_source"}:
            raise CommandRejected(
                f"invalid material custody policy: {request.custody_policy}"
            )
        requirement = request.requirement
        if not requirement.is_instance_requirement() or requirement.quantity > 0:
            raise CommandRejected("material source request must select one material instance")
        if requirement.template_id != request.resource_template_uuid:
            raise CommandRejected("material source selector template does not match its contract")

    def _tx_reserve_node(
        self,
        conn: sqlite3.Connection,
        now: int,
        workflow_id: str,
        node_id: str,
        requirements: List[MaterialRequirement],
        attempt: int,
        actor: str,
        causation_id: str,
    ) -> tuple[Dict[str, Any], bool]:
        """在当前事务内幂等建立一个节点的任务全程物料预留。

        参数：连接与时间戳由外层事务提供，其余参数描述任务节点、尝试次数和
        物料需求。返回：``(amounts, created)``，其中 ``created`` 只在本次真正
        建立或重新激活预留时为真。异常：分配不足及状态不变量由
        ``_tx_allocate`` 原样抛出，外层事务统一回滚。
        """
        if str(node_id).startswith("center-outbound:"):
            raise CommandRejected("出站产物保护只能通过交接、正式输入移交或取消结算处理")


        existing = conn.execute(
            "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? "
            "AND attempt = ?",
            (workflow_id, node_id, attempt),
        ).fetchone()
        if existing is not None and existing["status"] in (
            ReservationState.ACTIVE.value,
            ReservationState.CONSUMED.value,
        ):
            return json.loads(existing["amounts_json"]), False

        amounts = self._tx_allocate(
            conn,
            now,
            workflow_id,
            node_id,
            requirements,
            actor,
            causation_id,
        )
        if existing is not None:
            # 仅释放通用预留不会结束 Task 来源保管；重建预留不得抹掉原内容范围。
            previous_custody = json.loads(existing["amounts_json"]).get("custody_material_uuids")
            if previous_custody is not None:
                amounts["custody_material_uuids"] = previous_custody
        reservation_id = f"rsv-{uuid.uuid4().hex[:16]}"
        aggregate_version = 1
        if existing is not None:
            aggregate_version = int(existing["version"]) + 1
            conn.execute(
                "UPDATE inventory_reservation SET status = ?, amounts_json = ?, "
                "version = version + 1 WHERE workflow_id = ? AND node_id = ? AND attempt = ?",
                (
                    ReservationState.ACTIVE.value,
                    json.dumps(amounts),
                    workflow_id,
                    node_id,
                    attempt,
                ),
            )
            reservation_id = existing["reservation_id"]
        else:
            conn.execute(
                "INSERT INTO inventory_reservation(reservation_id, workflow_id, node_id, "
                "attempt, status, amounts_json, created_at, version) VALUES (?,?,?,?,?,?,?,1)",
                (
                    reservation_id,
                    workflow_id,
                    node_id,
                    attempt,
                    ReservationState.ACTIVE.value,
                    json.dumps(amounts),
                    now,
                ),
            )
        self._emit(
            conn,
            now,
            "reservation",
            reservation_id,
            aggregate_version,
            "reservation.created",
            {
                "workflow_id": workflow_id,
                "node_id": node_id,
                "attempt": attempt,
                "amounts": amounts,
            },
            causation_id=causation_id,
            actor=actor,
        )
        return amounts, True

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
                # 任务全程预留不得把已经被活跃共享来源绑定的实例
                # 变为独占物料。共享来源故意保持 warehouse 状态，因此不能
                # 只依赖实例状态排除这种跨策略冲突。
                inst = self._tx_resolve_instance(
                    conn,
                    req,
                    exclude_active_shared=True,
                    workflow_id=workflow_id,
                )
                from unilabos.app.scheduler.inventory.custody import require_source_custody_available
                require_source_custody_available(conn, material_uuid=inst["edge_uuid"], task_uuid=workflow_id,
                                                policy="task_exclusive")
                if inst["status"] != InstanceState.WAREHOUSE.value:
                    raise InsufficientStock(
                        f"instance {inst['edge_uuid']} not in warehouse (status={inst['status']})"
                    )
                from unilabos.app.scheduler.inventory.material_inputs import material_input_blocker
                from unilabos.app.scheduler.resource_lock import material_lock_key

                if material_input_blocker(conn, [material_lock_key(inst["edge_uuid"])], task_uuid=workflow_id) is not None:
                    raise MaterialClaimedByOtherTask("物料已为另一个中心调用的本地任务保留")
                from unilabos.app.scheduler.inventory.outbound import outbound_blocker
                if outbound_blocker(conn, [material_lock_key(inst["edge_uuid"])]) is not None:
                    raise MaterialClaimedByOtherTask("物料仍由原任务的出站责任保护")
                active_foreign_use = conn.execute(
                    "SELECT claim.task_uuid,claim.job_uuid "
                    "FROM station_execution_lock_lease AS lease "
                    "JOIN station_execution_claim AS claim USING(claim_uuid) "
                    "WHERE lease.material_uuid=? "
                    "AND lease.state IN ('prepared','reserved','running','uncertain') "
                    "AND claim.task_uuid<>? "
                    "ORDER BY lease.acquired_at,lease.claim_uuid LIMIT 1",
                    (inst["edge_uuid"], workflow_id),
                ).fetchone()
                if active_foreign_use is not None:
                    raise InsufficientStock(
                        f"instance {inst['edge_uuid']} has an active action in task "
                        f"{active_foreign_use['task_uuid']}"
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
    def _tx_instance_site_uuid(
        conn: sqlite3.Connection,
        material_uuid: str,
    ) -> str:
        """读取实例当前占用库位。

        参数：``conn`` 是当前库存事务连接；``material_uuid`` 是物料实例身份。
        返回：当前占用该物料的稳定库位 UUID，不在库位时返回空字符串。异常：
        数据库错误原样传播；排序只为兼容历史重复脏数据，正常模型应至多一行。
        """

        row = conn.execute(
            """
            SELECT uuid FROM site
            WHERE occupied_material_uuid = ? AND deleted_at IS NULL
            ORDER BY sort_order ASC, create_time ASC, uuid ASC
            LIMIT 1
            """,
            (material_uuid,),
        ).fetchone()
        return str(row["uuid"]) if row is not None else ""

    @staticmethod
    def _tx_resolve_instance(
        conn: sqlite3.Connection,
        req: MaterialRequirement,
        *,
        exclude_active_shared: bool = False,
        workflow_id: str | None = None,
        include_same_task_reserved: bool = False,
        excluded_instance_uuids: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """在当前库存事务内解析并核对一个实例型物料需求。

        参数：``conn`` 是外层写事务，``req`` 是固定 UUID、条码或库位选择器；
        ``exclude_active_shared`` 表示任务独占预留必须排除其他任务的活跃共享来源；
        ``workflow_id`` 给定时，本任务已经占用的盒子仍可被后续节点选中。
        返回：兼容视图中的具体物料实例行。异常：混合选择器或固定实例模板不
        匹配抛 ``CommandRejected``，身份缺失抛 ``NotFound``，自动选择没有
        可用实例时由库位解析抛 ``InsufficientStock``。
        """
        selector_fields = bool(req.mount_uuid or req.site_uuid or req.slot_uuids)
        if (req.instance_uuid or req.barcode) and selector_fields:
            raise CommandRejected(
                "instance_uuid/barcode cannot be combined with a site selector"
            )
        if req.instance_uuid:
            row = conn.execute(
                "SELECT * FROM material_instance WHERE edge_uuid = ?", (req.instance_uuid,)
            ).fetchone()
        elif req.barcode:
            placeholders = ",".join("?" for _ in _ACTIVE_STATES_TUPLE)
            row = conn.execute(
                f"SELECT * FROM material_instance WHERE barcode = ? AND status IN ({placeholders})",
                (req.barcode, *_ACTIVE_STATES_TUPLE),
            ).fetchone()
        else:
            return InventoryService._tx_select_site_instance(
                conn,
                req,
                exclude_active_shared=exclude_active_shared,
                workflow_id=workflow_id,
                include_same_task_reserved=include_same_task_reserved,
                excluded_instance_uuids=excluded_instance_uuids,
            )
        if row is None:
            raise NotFound(f"instance {req.instance_uuid or req.barcode} not found")
        instance = dict(row)
        if instance["edge_uuid"] in excluded_instance_uuids:
            raise MaterialClaimedByOtherTask("同批候选已选用该容器")
        from unilabos.app.scheduler.inventory.material_inputs import material_input_blocker
        from unilabos.app.scheduler.resource_lock import material_lock_key

        if material_input_blocker(conn, [material_lock_key(instance["edge_uuid"])], task_uuid=workflow_id or "") is not None:
            raise MaterialClaimedByOtherTask("物料已为另一个中心调用的本地任务保留")
        if req.template_id and instance["template_id"] != req.template_id:
            raise CommandRejected(
                f"instance {instance['edge_uuid']} template does not match {req.template_id}"
            )
        if exclude_active_shared and InventoryService._tx_has_active_shared_binding(
            conn,
            str(instance["edge_uuid"]),
        ):
            raise InsufficientStock(
                f"instance {instance['edge_uuid']} has an active shared source binding"
            )
        return instance

    @staticmethod
    def _tx_select_site_instance(
        conn: sqlite3.Connection,
        req: MaterialRequirement,
        *,
        exclude_active_shared: bool = False,
        workflow_id: str | None = None,
        include_same_task_reserved: bool = False,
        excluded_instance_uuids: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """在当前占用事务内按挂载点与库位集合确定性选择一个实例。

        参数：``conn`` 是 ``BEGIN IMMEDIATE`` 写事务；``req`` 提供资源模板、
        挂载物料及可选精确库位（Site）/库位（Slot）集合；
        ``exclude_active_shared`` 用于独占预留过滤其他任务已绑定的共享来源；
        ``workflow_id`` 给定时，本任务已预留或已共享的盒子仍算在库位上。返回：
        首个兼容物料实例。异常：选择器结构非法抛
        ``CommandRejected``/``NotFound``，没有可用占用物料抛
        ``InsufficientStock``。
        """

        if not req.template_id or not req.mount_uuid:
            raise CommandRejected(
                "automatic instance requirement needs template_id and mount_uuid"
            )
        if req.site_uuid and req.slot_uuids:
            raise CommandRejected("site_uuid and slot_uuids are mutually exclusive")
        if len(set(req.slot_uuids)) != len(req.slot_uuids):
            raise CommandRejected("slot_uuids contains duplicate sites")
        mount = conn.execute(
            "SELECT uuid FROM material WHERE uuid = ? AND deleted_at IS NULL",
            (req.mount_uuid,),
        ).fetchone()
        if mount is None:
            raise NotFound(f"mount material {req.mount_uuid} not found")
        if conn.execute(
            "SELECT 1 FROM site WHERE material_uuid = ? AND deleted_at IS NULL LIMIT 1",
            (req.mount_uuid,),
        ).fetchone() is None:
            # 挂载位下没有任何库位是参数错误，不是缺料；不能触发补料（技术方案 4.4）。
            raise CommandRejected(
                f"mount material {req.mount_uuid} has no sites to select from"
            )

        selected_sites: List[str] = []
        if req.site_uuid:
            selected_sites = [req.site_uuid]
        elif req.slot_uuids:
            selected_sites = list(req.slot_uuids)
        if selected_sites:
            placeholders = ",".join("?" for _ in selected_sites)
            site_rows = conn.execute(
                f"SELECT uuid, material_uuid FROM site WHERE uuid IN ({placeholders}) "
                "AND deleted_at IS NULL",
                tuple(selected_sites),
            ).fetchall()
            found_sites = {str(row["uuid"]): str(row["material_uuid"]) for row in site_rows}
            missing = sorted(set(selected_sites) - set(found_sites))
            if missing:
                raise NotFound(f"sites not found: {','.join(missing)}")
            foreign = sorted(
                site_uuid
                for site_uuid, owner_uuid in found_sites.items()
                if owner_uuid != req.mount_uuid
            )
            if foreign:
                raise CommandRejected(
                    f"sites do not belong to mount {req.mount_uuid}: {','.join(foreign)}"
                )

        if req.search_descendants:
            from unilabos.app.scheduler.inventory.source_selection import select_descendant_instance

            return select_descendant_instance(
                conn, req, selected_sites=selected_sites,
                exclude_active_shared=exclude_active_shared,
                workflow_id=workflow_id,
                excluded_instance_uuids=excluded_instance_uuids,
            )

        where = [
            "site.deleted_at IS NULL",
            "site.material_uuid = ?",
            "material.deleted_at IS NULL",
            "material.resource_template_uuid = ?",
        ]
        values: List[Any] = [
            req.mount_uuid,
            req.template_id,
        ]
        if excluded_instance_uuids:
            where.append("instance.edge_uuid NOT IN (" + ",".join("?" for _ in excluded_instance_uuids) + ")")
            values.extend(excluded_instance_uuids)
        where.append("NOT EXISTS (SELECT 1 FROM station_material_input_resource r "
                     "JOIN station_material_input h USING(invocation_uuid) WHERE h.state='active' "
                     "AND r.material_uuid=instance.edge_uuid AND h.local_task_uuid<>?)")
        values.append(workflow_id or "")
        if include_same_task_reserved and workflow_id:
            where.append(
                "("
                "instance.status = ? OR ("
                "instance.status = ? AND EXISTS ("
                "SELECT 1 FROM inventory_reservation AS reservation, "
                "json_each(reservation.amounts_json, '$.instances') AS reserved_instance "
                "WHERE reservation.workflow_id = ? AND reservation.status = 'active' "
                "AND reserved_instance.value = instance.edge_uuid"
                "))"
                ")"
            )
            values.extend(
                [
                    InstanceState.WAREHOUSE.value,
                    InstanceState.RESERVED.value,
                    workflow_id,
                ]
            )
        else:
            where.append("instance.status = ?")
            values.append(InstanceState.WAREHOUSE.value)
        if selected_sites:
            placeholders = ",".join("?" for _ in selected_sites)
            where.append(f"site.uuid IN ({placeholders})")
            values.extend(selected_sites)
        if exclude_active_shared:
            shared_filter = (
                "NOT EXISTS ("
                "SELECT 1 FROM inventory_material_source_binding AS binding "
                "WHERE binding.material_uuid=instance.edge_uuid "
                "AND binding.custody_policy='shared_source' "
                "AND binding.status='active'"
            )
            if workflow_id:
                shared_filter += " AND binding.workflow_id <> ?"
                values.append(workflow_id)
            shared_filter += ")"
            where.append(shared_filter)
        row = conn.execute(
            "SELECT instance.* FROM site AS site "
            "JOIN material AS material ON material.uuid = site.occupied_material_uuid "
            "JOIN material_instance AS instance ON instance.edge_uuid = material.uuid "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY site.sort_order ASC, site.create_time ASC, site.uuid ASC, "
            "material.create_time ASC, material.uuid ASC LIMIT 1",
            tuple(values),
        ).fetchone()
        if row is None:
            scope = req.site_uuid or ",".join(req.slot_uuids) or req.mount_uuid
            claimable_where = [
                "site.deleted_at IS NULL",
                "site.material_uuid = ?",
                "material.deleted_at IS NULL",
                "material.resource_template_uuid = ?",
                "instance.status IN ('warehouse', 'reserved')",
            ]
            claimable_values: List[Any] = [req.mount_uuid, req.template_id]
            if selected_sites:
                placeholders = ",".join("?" for _ in selected_sites)
                claimable_where.append(f"site.uuid IN ({placeholders})")
                claimable_values.extend(selected_sites)
            claimable = [
                str(item["edge_uuid"])
                for item in conn.execute(
                    "SELECT instance.edge_uuid FROM site AS site "
                    "JOIN material AS material ON material.uuid = site.occupied_material_uuid "
                    "JOIN material_instance AS instance ON instance.edge_uuid = material.uuid "
                    f"WHERE {' AND '.join(claimable_where)}",
                    tuple(claimable_values),
                ).fetchall()
            ]
            blocker = material_claim_owner(conn, claimable, workflow_id)
            if blocker is not None:
                raise MaterialClaimedByOtherTask(
                    f"template {req.template_id} in {scope} is claimed by another task",
                    blocking_workflow_id=blocker,
                )
            raise InsufficientStock(
                f"no warehouse instance for template {req.template_id} in {scope}"
            )
        return dict(row)

    @staticmethod
    def _tx_has_active_shared_binding(
        conn: sqlite3.Connection,
        material_uuid: str,
    ) -> bool:
        """检查物料是否已被任意活跃共享来源绑定。"""

        return (
            conn.execute(
                "SELECT 1 FROM inventory_material_source_binding "
                "WHERE material_uuid=? AND custody_policy='shared_source' "
                "AND status='active' LIMIT 1",
                (material_uuid,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _tx_instance_reserved_by_workflow(
        conn: sqlite3.Connection,
        material_uuid: str,
        workflow_id: str,
    ) -> bool:
        """本任务的活跃预留是否持有这只实例。"""

        rows = conn.execute(
            "SELECT amounts_json FROM inventory_reservation "
            "WHERE workflow_id=? AND status='active'",
            (workflow_id,),
        ).fetchall()
        for row in rows:
            amounts = json.loads(row["amounts_json"] or "{}")
            if material_uuid in amounts.get("instances", []):
                return True
        return False

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
        """设备明确成功后结算预留（批次数量扣减；实例转为在台状态）。

        同一库存事务提交业务行、台账和 outbox；已 consumed 直接返回，无预留
        （无物料节点）为 no-op。调用方不得在派发或执行接受阶段调用本方法。
        """
        if str(node_id).startswith("center-outbound:"):
            raise CommandRejected("出站产物保护只能通过交接、正式输入移交或取消结算处理")

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
        if str(node_id).startswith("center-outbound:"):
            raise CommandRejected("出站产物保护只能通过交接、正式输入移交或取消结算处理")

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
            if inst["status"] != InstanceState.RESERVED.value:
                continue
            deleted = conn.execute(
                "SELECT deleted_at FROM material WHERE uuid=?",
                (inst_uuid,),
            ).fetchone()
            if deleted is not None and deleted["deleted_at"] is not None:
                # 整盒卸料已经软删除。释放保管不能把状态改回 warehouse，
                # 否则兼容视图触发器会把 deleted_at 清掉，旧盒重新进入活动库存。
                conn.execute(
                    "UPDATE material SET inventory_status='discarded', "
                    "disposition='discarded', aggregate_version=aggregate_version+1 "
                    "WHERE uuid=?",
                    (inst_uuid,),
                )
                self._emit(
                    conn, now, "instance", inst_uuid, inst["version"] + 1, "instance.released",
                    {"workflow_id": workflow_id, "node_id": node_id, "kept_deleted": True},
                    causation_id=causation_id, actor=actor, reason=reason,
                )
                continue
            before_revision = inst["version"]
            inst = self._tx_set_instance_status(conn, inst, InstanceState.WAREHOUSE)
            from unilabos.app.scheduler.inventory.outbound import advance_released_revision
            advance_released_revision(conn, task_uuid=workflow_id, material_uuid=inst_uuid,
                                      before_revision=before_revision, after_revision=inst["version"])

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
        if str(node_id).startswith("center-outbound:"):
            raise CommandRejected("出站产物保护只能通过交接、正式输入移交或取消结算处理")

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

    def set_workflow_release_guard(self, guard: Callable[[str], bool] | None) -> None:
        """由工作流权威在所有释放入口前完成产物保护，跨库读取失败保持原预留。"""
        self._workflow_release_guard = guard

    @_traced_operation("workflow.release")
    def release_workflow(
        self, workflow_id: str, reason: str = "workflow_cancelled",
        actor: str = "", causation_id: str = "",
    ) -> Dict[str, Any]:
        """cancel/restart：释放该 workflow 全部 active 预留（依据 DB 状态，不依赖内存）."""
        if self._workflow_release_guard is not None and self._workflow_release_guard(workflow_id) is not True:
            return {"workflow_id": workflow_id, "status": "held", "released_nodes": [], "released_bindings": []}
        released: List[str] = []
        for rsv in self.store.reservations_for_workflow(workflow_id):
            if rsv["status"] == ReservationState.ACTIVE.value:
                self.release_reservation(
                    workflow_id, rsv["node_id"], rsv["attempt"],
                    reason=reason, actor=actor, causation_id=causation_id,
                )
                released.append(rsv["node_id"])
        now = self._now_ms()
        with self._tx() as conn:
            binding_rows = conn.execute(
                "SELECT node_id FROM inventory_material_source_binding "
                "WHERE workflow_id=? AND status='active' ORDER BY node_id",
                (workflow_id,),
            ).fetchall()
            released_bindings = [str(row["node_id"]) for row in binding_rows]
            conn.execute(
                "UPDATE inventory_material_source_binding SET status='released', "
                "released_at=?, version=version+1 "
                "WHERE workflow_id=? AND status='active'",
                (now, workflow_id),
            )
        return {
            "workflow_id": workflow_id,
            "released_nodes": released,
            "released_bindings": released_bindings,
        }

    # ------------------------------------------------------------------
    # deploy / move / consume / discard / adjust / content
    # ------------------------------------------------------------------

    @staticmethod
    def _tx_assert_physical_mutation_unclaimed(
        conn: sqlite3.Connection,
        *,
        edge_uuid: str,
        related_material_uuids: tuple[str, ...] = (),
        target_parent_uuid: str = "",
        target_slot_id: str = "",
    ) -> None:
        """在兼容 Inventory 写入口修改规范物理事实前执行统一 Claim 防线。

        参数：当前事务、被改物料、相关父物料及可选目标具名库位。返回：无。
        异常：任一身份与活动 Claim 冲突时抛 ``InventoryMutationConflict``，由
        命令/API 适配层映射为稳定拒绝结果。
        """

        target_site_uuid = ""
        if target_parent_uuid and target_slot_id:
            row = conn.execute(
                "SELECT uuid FROM site WHERE material_uuid=? "
                "AND LOWER(name)=LOWER(?) AND deleted_at IS NULL",
                (target_parent_uuid, target_slot_id),
            ).fetchone()
            target_site_uuid = str(row["uuid"]) if row is not None else ""
        assert_inventory_mutation_unclaimed(
            conn,
            material_uuids=(edge_uuid, *related_material_uuids),
            site_uuids=(target_site_uuid,) if target_site_uuid else (),
        )

    @staticmethod
    def _tx_upsert_relation(
        conn: sqlite3.Connection, parent_uuid: str, slot_id: str, child_uuid: str
    ) -> None:
        """relation 主键是 child_uuid：transfer 时旧父关系被原子替换，源端不残留.

        单一父不变量：`material_instance.parent_uuid` 与 `relation.parent_uuid`
        始终一致——云端 `parent_material_uuid` 就是资源树父物料（≡ ResourceDict
        parent_uuid），relation 只补充「父物料的哪个具名位」（slot_id = PLR site
        名，↔ 云端 sites.label；uuid 仅后端索引）。每次 upsert 同步父列。
        """
        parent = conn.execute(
            "SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
            (parent_uuid,),
        ).fetchone()
        if parent is None:
            # 兼容旧 Inventory API：历史调用允许先引用尚未同步的父资源。只在
            # 兼容层创建隐藏占位父物料；SiteOccupancy 仍统一走下方原子 API。
            conn.execute(
                "INSERT INTO material_instance("
                "edge_uuid,legacy_cloud_id,lot_id,template_id,barcode,status,"
                "parent_uuid,version) VALUES (?,?,?,?,?,?,?,1)",
                (parent_uuid, "", "", "", "", InstanceState.WAREHOUSE.value, ""),
            )
            conn.execute(
                "UPDATE material SET description=?,meta_data=?,name=? WHERE uuid=?",
                (
                    "Edge legacy parent placeholder",
                    json.dumps(
                        {"unilab_edge_placeholder": True},
                        separators=(",", ":"),
                    ),
                    f"__edge_placeholder__:{parent_uuid}",
                    parent_uuid,
                ),
            )
        if slot_id:
            target = conn.execute(
                "SELECT uuid FROM site WHERE material_uuid=? "
                "AND LOWER(name)=LOWER(?) AND deleted_at IS NULL",
                (parent_uuid, slot_id),
            ).fetchone()
            if target is None:
                conn.execute(
                    "INSERT INTO site("
                    "create_time,update_time,deleted_at,description,meta_data,"
                    "material_uuid,name,sort_order,allowed_resource_template_uuids,"
                    "occupied_material_uuid,position_x,position_y,position_z,"
                    "depth,length,width"
                    ") VALUES ("
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now'),NULL,NULL,'{}',"
                    "?,?,0,'[]',NULL,0,0,0,0,0,0)",
                    (parent_uuid, slot_id),
                )
                target = conn.execute(
                    "SELECT uuid FROM site WHERE material_uuid=? "
                    "AND LOWER(name)=LOWER(?) AND deleted_at IS NULL",
                    (parent_uuid, slot_id),
                ).fetchone()
            if target is None:  # pragma: no cover - INSERT/约束异常会先终止事务。
                raise CommandRejected(f"site {parent_uuid}.{slot_id} was not created")
            try:
                set_site_occupancy(
                    conn,
                    site_uuid=str(target["uuid"]),
                    material_uuid=child_uuid,
                )
            except SiteOccupancyConflict as error:
                raise CommandRejected(
                    f"site {parent_uuid}.{slot_id} is occupied or invalid: {error}"
                ) from error
        else:
            clear_site_occupancy(conn, material_uuid=child_uuid)
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
                    self._tx_assert_physical_mutation_unclaimed(
                        conn,
                        edge_uuid=edge_uuid,
                        related_material_uuids=(issue["relation_parent_uuid"],),
                    )
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
            self._tx_assert_physical_mutation_unclaimed(
                conn,
                edge_uuid=edge_uuid,
                related_material_uuids=(parent_uuid,) if parent_uuid else (),
                target_parent_uuid=parent_uuid,
                target_slot_id=slot_id,
            )
            inst = self._tx_set_instance_status(conn, inst, InstanceState.BENCH)
            if parent_uuid:
                self._tx_upsert_relation(conn, parent_uuid, slot_id, edge_uuid)
            self._emit(
                conn, now, "instance", edge_uuid, inst["version"], "instance.deployed",
                {"parent_uuid": parent_uuid, "slot_id": slot_id},
                causation_id=causation_id, actor=actor,
            )
        return inst

    def correct_inventory(self, command_uuid: str, request: Dict[str, Any]) -> Dict[str, Any]:
        """人工登记物料位置，按既有实例版本和台账幂等写入，不检查执行锁。"""
        with self._tx() as conn:
            existing = conn.execute("SELECT result_json FROM processed_command WHERE command_id=?", (command_uuid,)).fetchone()
            if existing is not None:
                return json.loads(existing["result_json"])
            changes = request.get("changes") or []
            versions = request.get("expected_inventory_versions") or {}
            if not changes or len({item["material_uuid"] for item in changes}) != len(changes):
                raise CommandRejected("物料修正必须非空且每项物料只出现一次")
            planned_parents = {item["edge_uuid"]: str(item["parent_uuid"] or "") for item in conn.execute("SELECT edge_uuid,parent_uuid FROM material_instance")}
            planned_parents.update({item["material_uuid"]: str(item.get("parent_uuid") or "") for item in changes})
            for identity in planned_parents:
                seen, current = set(), identity
                while current:
                    if current in seen:
                        raise CommandRejected("人工位置修正会产生父级环")
                    seen.add(current)
                    current = planned_parents.get(current, "")
            facts = []
            for change in changes:
                identity = change["material_uuid"]
                inst = self._tx_get_instance(conn, identity)
                if identity not in versions:
                    raise CommandRejected("缺少人工修正的实例版本")
                self._tx_check_version(inst, versions[identity])
                parent_uuid, site_uuid = str(change.get("parent_uuid") or ""), str(change.get("site_uuid") or "")
                target = None
                if parent_uuid:
                    self._tx_get_instance(conn, parent_uuid)
                    if parent_uuid == identity:
                        raise CommandRejected("物料不能装入自身")
                if site_uuid:
                    target = conn.execute("SELECT * FROM site WHERE uuid=? AND deleted_at IS NULL", (site_uuid,)).fetchone()
                    if target is None or target["material_uuid"] != parent_uuid:
                        raise CommandRejected("库位与目标父物料不匹配")
                    if versions.get(site_uuid) != str(target["update_time"]):
                        raise VersionConflict("库位版本已变化")
                old = conn.execute("SELECT * FROM resource_relation WHERE child_uuid=?", (identity,)).fetchone()
                facts.append((change, inst, dict(old) if old else None, target))
            # 同批变更先移出来源，可原子表达交换与卸载，不以旧占用作为准入。
            for change, inst, old, target in facts:
                clear_site_occupancy(conn, material_uuid=change["material_uuid"])
                conn.execute("DELETE FROM resource_relation WHERE child_uuid=?", (change["material_uuid"],))
                conn.execute("UPDATE material_instance SET parent_uuid='' WHERE edge_uuid=?", (change["material_uuid"],))
            results = []
            for change, inst, old, target in facts:
                identity = change["material_uuid"]
                parent_uuid = str(change.get("parent_uuid") or "")
                if parent_uuid:
                    result = self._tx_move_instance(conn, inst=inst, old=None, parent_uuid=parent_uuid,
                        slot_id=str(target["name"]) if target is not None else "", actor=request["operator_id"], causation_id=command_uuid)
                else:
                    conn.execute("UPDATE material_instance SET version=version+1 WHERE edge_uuid=?", (identity,))
                    result = self._tx_get_instance(conn, identity)
                # 保留独立人工事实，不把动作成功猜成自动上下料。
                self._emit(conn, self._now_ms(), "instance", identity, result["version"], "instance.manual_correction",
                    {"before": inst, "before_relation": old, "after": result, "session_id": request["session_id"],
                     "source_job_uuid": request["source_job_uuid"], "related_manual_command_id": request.get("related_manual_command_id")},
                    actor=request["operator_id"], reason=request["reason"], causation_id=command_uuid)
                results.append(result)
            result = {"instances": results}
            conn.execute("INSERT INTO processed_command(command_id,result_json) VALUES(?,?)", (command_uuid,json.dumps(result,ensure_ascii=False)))
            return result

    @_traced_operation("move")
    def move_instance(
        self, edge_uuid: str, parent_uuid: str, slot_id: str = "",
        actor: str = "", causation_id: str = "", expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """幂等移动物料并原子切换来源/目标库位占用。

        参数：物料、目标父物料与库位名确定目标事实；actor/causation_id 用于审计，
        ``expected_version`` 可选保护首次写入。返回：移动后的物料实例；目标关系
        已经相同时零写返回。异常：物料、父级、库位或版本冲突原样传播。该幂等
        规则允许工作流库与库存库之间的结算 Saga 在崩溃后按同一 Job 重放。
        """

        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            old = conn.execute(
                "SELECT * FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
            ).fetchone()
            if (
                old is not None
                and str(old["parent_uuid"]) == str(parent_uuid)
                and str(old["slot_id"]) == str(slot_id)
                and str(inst.get("parent_uuid") or "") == str(parent_uuid)
            ):
                return inst
            self._tx_assert_physical_mutation_unclaimed(
                conn,
                edge_uuid=edge_uuid,
                related_material_uuids=(parent_uuid,),
                target_parent_uuid=parent_uuid,
                target_slot_id=slot_id,
            )
            inst = self._tx_move_instance(
                conn,
                inst=inst,
                old=old,
                parent_uuid=parent_uuid,
                slot_id=slot_id,
                actor=actor,
                causation_id=causation_id,
            )
        return inst

    def _tx_move_instance(
        self,
        conn: sqlite3.Connection,
        *,
        inst: Dict[str, Any],
        old: sqlite3.Row | None,
        parent_uuid: str,
        slot_id: str,
        actor: str,
        causation_id: str,
    ) -> Dict[str, Any]:
        """在已经授权的库存事务中提交一次物料父级/库位切换。"""

        edge_uuid = str(inst["edge_uuid"])
        self._tx_upsert_relation(conn, parent_uuid, slot_id, edge_uuid)
        new_version = int(inst["version"]) + 1
        conn.execute(
            "UPDATE material_instance SET version = ? WHERE edge_uuid = ?",
            (new_version, edge_uuid),
        )
        self._emit(
            conn,
            self._now_ms(),
            "instance",
            edge_uuid,
            new_version,
            "instance.moved",
            {
                "from_parent": old["parent_uuid"] if old else "",
                "from_slot": old["slot_id"] if old else "",
                "to_parent": parent_uuid,
                "to_slot": slot_id,
            },
            causation_id=causation_id,
            actor=actor,
        )
        return self._tx_get_instance(conn, edge_uuid)

    def _settle_claimed_material_transfer(
        self,
        command: MaterialTransferCommand,
    ) -> Dict[str, Any]:
        """验证完整 Permit 并在同一事务提交 Scheduler 物料转移结算。"""

        expected_change = dict(command.expected_change_set or {})
        fences = {
            str(fence.lock_key): int(fence.fencing_token)
            for fence in command.fences
        }
        if (
            expected_change.get("kind") != "material_transfer"
            or str(expected_change.get("material_uuid") or "")
            != command.material_uuid
        ):
            raise StationResourceError(
                "settlement_change_set_mismatch",
                "PhysicalSettlement 物料与派发时冻结的 ChangeSet 不一致",
            )
        # 失败转运的现场事实不一定是原计划目标：动作可能尚未开始，物料仍在
        # 来源库位；也可能已经取起而停在夹爪库位。允许操作员在派发时已经由
        # 同一 Claim/Fence 保护的任一库位上结算，但禁止借对账接口写入未声明
        # 的库位。这样既能表达真实物理位置，也不扩大原派发凭据的写权限。
        actual_site_lock = site_lock_key(
            command.target_owner_material_uuid,
            command.target_site_uuid,
        )
        if actual_site_lock not in fences:
            raise StationResourceError(
                "settlement_actual_site_not_claimed",
                "PhysicalSettlement 实际库位不在派发时冻结的 Claim/Fence 中",
            )
        settlement_event_uuid = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"unilabos-transfer-settlement:{command.effect_uuid}",
            )
        )
        with self._tx() as conn:
            claim_state = validate_physical_settlement_credentials(
                conn,
                effect_uuid=command.effect_uuid,
                claim_uuid=command.claim_uuid,
                job_uuid=command.job_uuid,
                attempt=command.attempt,
                parameter_hash=command.parameter_hash,
                expected_change_set=expected_change,
                fences=fences,
                allow_released_replay=True,
                evidence_only=True,
            )
            persisted = conn.execute(
                "SELECT delta_json,causation_id,workflow_node_job_uuid "
                "FROM inventory_ledger WHERE entry_uuid=? AND "
                "op_type='physical_settlement.material_transfer'",
                (settlement_event_uuid,),
            ).fetchone()
            if persisted is not None:
                if (
                    str(persisted["causation_id"]) != command.effect_uuid
                    or str(persisted["workflow_node_job_uuid"] or "")
                    != command.job_uuid
                ):
                    raise StationResourceError(
                        "transfer_replay_identity_mismatch",
                        "转运结算重放身份与已提交台账不一致",
                    )
                try:
                    result = json.loads(str(persisted["delta_json"]))[
                        "changes"
                    ]["result"]
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise StationResourceError(
                        "transfer_replay_evidence_invalid",
                        "转运结算重放台账证据损坏",
                    ) from error
                if not isinstance(result, dict):
                    raise StationResourceError(
                        "transfer_replay_evidence_invalid",
                        "转运结算重放台账结果不是对象",
                    )
                return result
            corrected = conn.execute("""SELECT l.causation_id,l.occurred_at FROM inventory_ledger l
                JOIN station_execution_claim c ON c.claim_uuid=?
                WHERE l.aggregate_id=? AND l.op_type='instance.manual_correction'
                AND l.occurred_at >= CAST((julianday(c.acquired_at)-2440587.5)*86400000 AS INTEGER)
                ORDER BY l.occurred_at DESC LIMIT 1""", (command.claim_uuid, command.material_uuid)).fetchone()
            if corrected is not None:
                raise StationResourceError("inventory_fact_changed", "派发后存在人工库存修正，保留动作证据供核对",
                    resources=[{"material_uuid": command.material_uuid,
                        "correction_command_uuid": str(corrected["causation_id"]),
                        "expected_target_site_uuid": command.target_site_uuid}])
            # 以上只读取历史证据。首次位置写入仍要求全部租约当前有效。
            validate_physical_settlement_credentials(conn, effect_uuid=command.effect_uuid,
                claim_uuid=command.claim_uuid, job_uuid=command.job_uuid, attempt=command.attempt,
                parameter_hash=command.parameter_hash, expected_change_set=expected_change,
                fences=fences, allow_released_replay=True)
            if claim_state == "released":
                raise DispatchAdmissionConflict(
                    "已释放 Claim 没有已提交的转运结算证据，禁止首次修改库存"
                )
            target = conn.execute(
                "SELECT uuid,material_uuid,name,occupied_material_uuid FROM site "
                "WHERE uuid=? AND deleted_at IS NULL",
                (command.target_site_uuid,),
            ).fetchone()
            if target is None:
                raise StationResourceError(
                    "site_not_found",
                    "PhysicalSettlement 目标库位不存在",
                )
            if (
                str(target["material_uuid"]) != command.target_owner_material_uuid
                or (
                    command.target_site_name
                    and str(target["name"]) != command.target_site_name
                    # 动作参数里的库位名可以是上层物料地址（交给驱动的厂家位号），按地址表指到的正是这个库位。
                    and not _address_names_site(conn, command.target_owner_material_uuid,
                                                command.target_site_name, str(target["uuid"]))
                )
            ):
                raise StationResourceError(
                    "site_identity_mismatch",
                    "PhysicalSettlement 目标库位身份或名称漂移",
                )
            occupied = str(target["occupied_material_uuid"] or "")
            if occupied and occupied != command.material_uuid:
                raise StationResourceError(
                    "site_occupied",
                    f"PhysicalSettlement 目标库位已被物料 {occupied} 占用",
                )
            source_site_uuid = str(expected_change.get("source_site_uuid") or "")
            source = conn.execute(
                "SELECT occupied_material_uuid FROM site WHERE uuid=? "
                "AND deleted_at IS NULL",
                (source_site_uuid,),
            ).fetchone()
            if source is None or str(source["occupied_material_uuid"] or "") not in {
                command.material_uuid,
                "",
            }:
                raise StationResourceError(
                    "settlement_source_mismatch",
                    "PhysicalSettlement 来源库位事实与派发快照不一致",
                )
            inst = self._tx_get_instance(conn, command.material_uuid)
            old = conn.execute(
                "SELECT * FROM resource_relation WHERE child_uuid=?",
                (command.material_uuid,),
            ).fetchone()
            moved_from: tuple[str, str] | None = None
            if (
                old is not None
                and str(old["parent_uuid"]) == command.target_owner_material_uuid
                and str(old["slot_id"]) == str(target["name"])
            ):
                result = inst
            else:
                moved_from = (
                    str(old["parent_uuid"]) if old is not None else "",
                    str(old["slot_id"]) if old is not None else "",
                )
                result = self._tx_move_instance(
                    conn,
                    inst=inst,
                    old=old,
                    parent_uuid=command.target_owner_material_uuid,
                    slot_id=str(target["name"]),
                    actor=command.actor,
                    causation_id=command.causation_id,
                )
            material_name = conn.execute(
                "SELECT name FROM material WHERE uuid=?",
                (command.material_uuid,),
            ).fetchone()
            InventoryStore.tx_append_inventory_event(
                conn,
                entry_uuid=settlement_event_uuid,
                edge_id=self.edge_id,
                lab_id=self.lab_id,
                occurred_at=self._now_ms(),
                aggregate_type="material_instance",
                aggregate_id=command.material_uuid,
                aggregate_version=int(result["version"]),
                event_type="physical_settlement.material_transfer",
                payload={
                    "changes": {
                        "result": result,
                        "name": (
                            str(material_name["name"])
                            if material_name is not None
                            else ""
                        ),
                    },
                    "extension": {
                        "target_site_uuid": command.target_site_uuid,
                    },
                },
                actor="scheduler",
                reason="physical_settlement",
                causation_id=command.effect_uuid,
                material_uuid=command.material_uuid,
                subject_type="material_instance",
                revision=int(result["version"]),
                workflow_node_job_uuid=command.job_uuid,
            )
            if moved_from is not None:
                claim = conn.execute(
                    "SELECT task_uuid FROM station_execution_claim WHERE claim_uuid=?",
                    (command.claim_uuid,),
                ).fetchone()
                InventoryStore.append_material_ledger(
                    conn,
                    material_uuid=command.material_uuid,
                    name=(
                        str(material_name["name"])
                        if material_name is not None
                        else ""
                    ),
                    event_type="material.transfer",
                    changes={
                        "site_name_before": _material_place_label(
                            conn, moved_from[0], moved_from[1]
                        ),
                        "site_name_after": _material_place_label(
                            conn,
                            command.target_owner_material_uuid,
                            str(target["name"]),
                        ),
                        "parent_uuid_before": moved_from[0],
                        "parent_uuid_after": command.target_owner_material_uuid,
                    },
                    causation_id=command.effect_uuid,
                    workflow_task_uuid=(
                        str(claim["task_uuid"]) if claim is not None else None
                    ),
                    workflow_node_job_uuid=command.job_uuid or None,
                )
            return result

    def _settle_claimed_material_aliquot(
        self,
        command: MaterialAliquotCommand,
    ) -> Dict[str, Any]:
        """验证完整 Permit 并原子扣减来源、写入全部目标内容物。"""

        expected_change = dict(command.expected_change_set or {})
        fences = {
            str(fence.lock_key): int(fence.fencing_token)
            for fence in command.fences
        }
        receipts = tuple(command.receipts)
        targets = tuple(sorted(receipt.target_material_uuid for receipt in receipts))
        expected_targets = tuple(
            sorted(str(value) for value in expected_change.get("target_material_uuids", ()))
        )
        if (
            expected_change.get("kind") != "material_content_aliquot"
            or expected_change.get("source_material_uuid") != command.source_material_uuid
            or targets != expected_targets
            or len(set(targets)) != len(targets)
            or not targets
        ):
            raise StationResourceError(
                "settlement_change_set_mismatch",
                "分装 PhysicalSettlement 回执与冻结目标闭集不一致",
            )
        if any(
            not math.isfinite(receipt.actual_quantity)
            or receipt.actual_quantity < 0
            or not receipt.quantity_unit.strip()
            for receipt in receipts
        ):
            raise StationResourceError(
                "aliquot_receipt_invalid", "分装目标回执数量或单位非法"
            )
        now_text = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        now_ms = self._now_ms()
        source_event_uuid = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"unilabos-aliquot-source:{command.effect_uuid}",
            )
        )
        with self._tx() as conn:
            source_claim = conn.execute("SELECT task_uuid FROM station_execution_claim WHERE claim_uuid=?",
                                        (command.claim_uuid,)).fetchone()
            source_task_uuid = str(source_claim["task_uuid"]) if source_claim is not None else None
            claim_state = validate_physical_settlement_credentials(
                conn,
                effect_uuid=command.effect_uuid,
                claim_uuid=command.claim_uuid,
                job_uuid=command.job_uuid,
                attempt=command.attempt,
                parameter_hash=command.parameter_hash,
                expected_change_set=expected_change,
                fences=fences,
                allow_released_replay=True,
            )
            persisted = conn.execute(
                "SELECT delta_json,causation_id,workflow_node_job_uuid "
                "FROM inventory_ledger WHERE entry_uuid=? AND "
                "op_type='current_substance.aliquot_source'",
                (source_event_uuid,),
            ).fetchone()
            if persisted is not None:
                if (
                    str(persisted["causation_id"]) != command.effect_uuid
                    or str(persisted["workflow_node_job_uuid"] or "")
                    != command.job_uuid
                ):
                    raise StationResourceError(
                        "aliquot_replay_identity_mismatch",
                        "分装结算重放身份与已提交台账不一致",
                    )
                try:
                    result = json.loads(str(persisted["delta_json"]))[
                        "changes"
                    ]["result"]
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise StationResourceError(
                        "aliquot_replay_evidence_invalid",
                        "分装结算重放台账证据损坏",
                    ) from error
                if not isinstance(result, dict):
                    raise StationResourceError(
                        "aliquot_replay_evidence_invalid",
                        "分装结算重放台账结果不是对象",
                    )
                return result
            if claim_state == "released":
                raise DispatchAdmissionConflict(
                    "已释放 Claim 没有已提交的分装结算证据，禁止首次修改库存"
                )
            source = conn.execute(
                "SELECT * FROM current_substance WHERE material_uuid=? "
                "AND deleted_at IS NULL",
                (command.source_material_uuid,),
            ).fetchone()
            if source is None:
                raise StationResourceError(
                    "aliquot_source_content_missing", "分装来源容器没有当前内容物"
                )
            source_unit = str(source["quantity_unit"])
            if any(
                receipt.quantity_unit.casefold() != source_unit.casefold()
                for receipt in receipts
            ):
                raise StationResourceError(
                    "aliquot_unit_mismatch", "分装目标回执单位与来源内容物不一致"
                )
            total = math.fsum(float(receipt.actual_quantity) for receipt in receipts)
            try:
                source_after = subtract_quantity(float(source["quantity"]), total)
            except WorkflowQuantityReservationError as error:
                raise StationResourceError(
                    "aliquot_source_insufficient", "分装来源内容物余量不足"
                ) from error
            placeholders = ",".join("?" for _ in targets)
            existing_materials = conn.execute(
                f"SELECT uuid FROM material WHERE uuid IN ({placeholders}) "
                "AND deleted_at IS NULL",
                targets,
            ).fetchall()
            if {str(row["uuid"]) for row in existing_materials} != set(targets):
                raise StationResourceError(
                    "aliquot_target_missing", "一个或多个分装目标容器不存在"
                )
            occupied_targets = conn.execute(
                f"SELECT material_uuid FROM current_substance WHERE material_uuid IN ({placeholders}) "
                "AND deleted_at IS NULL UNION SELECT material_uuid FROM reagent "
                f"WHERE material_uuid IN ({placeholders}) AND deleted_at IS NULL",
                (*targets, *targets),
            ).fetchall()
            if occupied_targets:
                raise StationResourceError(
                    "aliquot_target_not_empty", "一个或多个分装目标容器已有内容物"
                )
            source_revision = int(source["revision"]) + 1
            changed = conn.execute(
                "UPDATE container_content SET quantity=?,revision=?,update_time=?,observed_at=? "
                "WHERE uuid=? AND kind='substance' AND revision=? AND deleted_at IS NULL",
                (
                    source_after,
                    source_revision,
                    now_text,
                    now_text,
                    source["uuid"],
                    source["revision"],
                ),
            ).rowcount
            if changed != 1:
                raise StationResourceError(
                    "aliquot_source_revision_changed", "分装来源内容物修订已变化"
                )
            created: list[dict[str, Any]] = []
            for receipt in receipts:
                content_uuid = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"unilabos-aliquot:{command.effect_uuid}:{receipt.target_material_uuid}",
                    )
                )
                conn.execute(
                    """INSERT INTO current_substance(
                        uuid,create_time,update_time,description,meta_data,
                        material_uuid,name,composition,quantity,quantity_unit,
                        physical_state,revision,observed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        content_uuid,
                        now_text,
                        now_text,
                        source["description"],
                        source["meta_data"],
                        receipt.target_material_uuid,
                        source["name"],
                        source["composition"],
                        float(receipt.actual_quantity),
                        source_unit,
                        source["physical_state"],
                        1,
                        now_text,
                    ),
                )
                created.append(
                    {
                        "current_substance_uuid": content_uuid,
                        "target_material_uuid": receipt.target_material_uuid,
                        "quantity": float(receipt.actual_quantity),
                        "quantity_unit": source_unit,
                    }
                )
                InventoryStore.tx_append_inventory_event(
                    conn,
                    entry_uuid=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"unilabos-aliquot-target:{command.effect_uuid}:{receipt.target_material_uuid}",
                        )
                    ),
                    edge_id=self.edge_id,
                    lab_id=self.lab_id,
                    occurred_at=now_ms,
                    aggregate_type="current_substance",
                    aggregate_id=content_uuid,
                    aggregate_version=1,
                    event_type="current_substance.aliquot_receive",
                    payload={"changes": {"result": created[-1]}, "extension": {}},
                    actor="scheduler",
                    reason="physical_settlement",
                    causation_id=command.effect_uuid,
                    material_uuid=receipt.target_material_uuid,
                    subject_type="current_substance",
                    quantity_delta=float(receipt.actual_quantity),
                    quantity_unit=source_unit,
                    revision=1,
                    workflow_task_uuid=source_task_uuid,
                    workflow_node_job_uuid=command.job_uuid,
                )
            InventoryStore.tx_append_inventory_event(
                conn,
                entry_uuid=str(
                    source_event_uuid
                ),
                edge_id=self.edge_id,
                lab_id=self.lab_id,
                occurred_at=now_ms,
                aggregate_type="current_substance",
                aggregate_id=str(source["uuid"]),
                aggregate_version=source_revision,
                event_type="current_substance.aliquot_source",
                payload={
                    "changes": {
                        "result": {
                            "source_material_uuid": command.source_material_uuid,
                            "source_quantity": source_after,
                            "quantity_unit": source_unit,
                            "targets": created,
                        }
                    },
                    "extension": {},
                },
                actor="scheduler",
                reason="physical_settlement",
                causation_id=command.effect_uuid,
                material_uuid=command.source_material_uuid,
                subject_type="current_substance",
                quantity_delta=-total,
                quantity_unit=source_unit,
                revision=source_revision,
                workflow_task_uuid=source_task_uuid,
                workflow_node_job_uuid=command.job_uuid,
            )
            return {
                "source_material_uuid": command.source_material_uuid,
                "source_quantity": source_after,
                "quantity_unit": source_unit,
                "targets": created,
            }

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
            self._tx_assert_physical_mutation_unclaimed(
                conn,
                edge_uuid=edge_uuid,
                related_material_uuids=(
                    str(old["parent_uuid"])
                    if old is not None
                    else str(inst.get("parent_uuid") or ""),
                ),
            )
            if old is not None:
                clear_site_occupancy(conn, material_uuid=edge_uuid)
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
        """设置/清除父物料（云端 parent_material_uuid ≡ 资源树 parent_uuid，单一父）。

        资源只有一个父层级：父物料 + 可选具名位（slot_id = PLR site 名，
        ↔ 云端 sites.label；uuid 仅后端索引）。语义：

        - parent_uuid 空串：顶层——父与具名位一并清除；
        - parent_uuid 非空、slot_id 空/None：有父但不占具名位（sites 讨论稿场景
          「父子关系不需要用 site 表达」），relation 行删除；
        - parent_uuid 非空、slot_id 非空：父 + 具名位，与 deploy/move 同一不变量
          （relation.parent 始终等于 parent_uuid 列）。

        沿 parent 链防环（云端由业务层校验，Edge 同等语义）。
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
            self._tx_assert_physical_mutation_unclaimed(
                conn,
                edge_uuid=edge_uuid,
                related_material_uuids=tuple(
                    item for item in (str(old_parent or ""), parent_uuid) if item
                ),
                target_parent_uuid=parent_uuid,
                target_slot_id=new_slot,
            )
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
                clear_site_occupancy(conn, material_uuid=edge_uuid)
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

    @_traced_operation("instance.mark_used")
    def mark_instance_used(
        self, edge_uuid: str, reason: str = "", actor: str = "", causation_id: str = "",
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """把一件离散物料标为已使用：身份、库位占用与内容都保留，只退出自动选料。

        参数：``edge_uuid`` 是物料实例；``reason`` 进入台账。返回：迁移后的实例。
        已是 ``used`` 时直接返回（幂等）；状态机不允许的迁移按库存错误拒绝。
        不改变物理位置，因此不要求物料未被执行 Claim 覆盖。
        """
        now = self._now_ms()
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            if inst["status"] == InstanceState.USED.value:
                return {**inst, "previous_status": InstanceState.USED.value}
            previous = inst["status"]
            inst = self._tx_set_instance_status(conn, inst, InstanceState.USED)
            self._emit(
                conn, now, "instance", edge_uuid, inst["version"], "instance.used",
                {"reason": reason, "previous_status": previous},
                causation_id=causation_id, actor=actor, reason=reason,
            )
        return {**inst, "previous_status": previous}

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
            self._tx_assert_physical_mutation_unclaimed(
                conn,
                edge_uuid=edge_uuid,
                related_material_uuids=(str(inst.get("parent_uuid") or ""),),
            )
            inst = self._tx_set_instance_status(conn, inst, target)
            clear_site_occupancy(conn, material_uuid=edge_uuid)
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
            self._tx_assert_physical_mutation_unclaimed(
                conn,
                edge_uuid=instance_uuid,
            )
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
