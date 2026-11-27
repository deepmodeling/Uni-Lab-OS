"""工作流数量库存的库存侧权威协议。

本模块复用既有 ``inventory_reservation``、``inventory_ledger`` 与
``sync_outbox``。工作流库只保存分配投影和 Saga；可用数量、活动预留、实际扣减
与释放事实均在同一个库存 SQLite 写事务中判断和提交。
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from unilabos.app.scheduler.inventory.settled_quantity import aliquot_consumption_fact
from unilabos.app.scheduler.inventory.store import InventoryStore


class WorkflowQuantityReservationError(ValueError):
    """表示工作流数量预留、释放或消费违反库存权威约束。"""


class WorkflowQuantityShortageError(WorkflowQuantityReservationError):
    """可信库存扣除有效预留后不足；可由运行时来源交给补料分支。"""

    def __init__(
        self,
        message: str,
        shortages: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        """保存全部不够的库存实例，供预检原样展示。

        参数：``message`` 是带库位名的用户文案，``shortages`` 是每个不够实例的
        库位、容器、可用量和需求量。返回无。
        """

        super().__init__(message)
        self.shortages = [dict(item) for item in shortages or ()]


def format_quantity_shortage_message(shortages: Sequence[Mapping[str, Any]]) -> str:
    """把全部不够的库位拼进同一句预留失败文案。

    参数：``shortages`` 按检查顺序排列。返回：有库位名时用顿号连接，
    没有名字时退回不点名的原句。
    """

    names: list[str] = []
    for item in shortages:
        name = str(item.get("site_name") or item.get("material_name") or "").strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return "库存实例可用数量不足，无法完成整任务预留"
    return f"{'、'.join(names)}库存实例可用数量不足，无法完成整任务预留"


_QUANTITY_RELATIVE_TOLERANCE = 1e-12


def subtract_quantity(available: float, consumed: float) -> float:
    """扣减有限非负数量，把浮点舍入造成的微小正负余量都归零。

    容差随两个操作数的量级变化（浮点精度与相对容差取大），避免固定绝对容差在
    kg 与 µL 间放大真实超量；多次十进制扣减累积的 ``2.78e-17`` 这类尾差按
    “已扣完”处理，不能留下非零余量。调用方仍须在同一库存事务内验证并写入
    返回余量。
    """

    available, consumed = float(available), float(consumed)
    if any(not math.isfinite(value) or value < 0 for value in (available, consumed)):
        raise WorkflowQuantityReservationError("库存数量必须是有限非负数")
    difference = available - consumed
    tolerance = max(
        8 * max(math.ulp(available), math.ulp(consumed)),
        _QUANTITY_RELATIVE_TOLERANCE * max(available, consumed),
    )
    if difference < -tolerance:
        raise WorkflowQuantityReservationError("库存当前余量不足，不能提交扣减")
    if difference <= tolerance:
        return 0.0
    return difference


def _json(value: Any) -> str:
    """编码稳定 JSON；参数必须可编码，返回用于幂等比较的紧凑文本。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Any, fallback: Any) -> Any:
    """解码持久 JSON；参数非法时返回调用方提供的安全默认值。"""

    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _quantity_unit(unit: Any) -> tuple[str, float] | None:
    """识别 Backend 支持的质量或体积单位；未知单位返回 ``None``。"""

    definitions = {
        "mg": ("mass", 0.001),
        "g": ("mass", 1.0),
        "kg": ("mass", 1000.0),
        "μl": ("volume", 0.001),
        "ul": ("volume", 0.001),
        "ml": ("volume", 1.0),
        "l": ("volume", 1000.0),
    }
    return definitions.get(str(unit or "").strip().casefold())


def convert_quantity(
    quantity: float,
    source_unit: str,
    target_unit: str,
    *,
    inventory_type: str,
    physical_state: Any,
    concentration_value: Any,
    density_g_per_ml: Any,
) -> tuple[float, dict[str, Any]] | None:
    """按 Backend 规则把上报数量换算到库存单位。

    参数：数量、源/目标单位及试剂物性。返回：换算数量和审计信息；未知单位或
    不安全的质量/体积跨维度换算返回 ``None``。异常：无，调用方按单位不匹配
    记录事实且不得扣减库存。
    """

    normalized_source = str(source_unit or "").strip()
    normalized_target = str(target_unit or "").strip()
    # piece/count 等离散单位不在质量/体积表里。同单位不需要换算，预检已经按
    # 字符串放行，消费阶段必须同样放行，不能留到结算才报无法换算。
    if (
        normalized_source
        and normalized_source.casefold() == normalized_target.casefold()
    ):
        return quantity, {
            "source_quantity": quantity,
            "source_unit": source_unit,
            "target_unit": target_unit,
            "result_quantity": quantity,
            "kind": "same_unit",
        }
    source = _quantity_unit(source_unit)
    target = _quantity_unit(target_unit)
    if source is None or target is None:
        return None
    source_dimension, source_rate = source
    target_dimension, target_rate = target
    if source_dimension == target_dimension:
        converted = quantity * source_rate / target_rate
        return converted, {
            "source_quantity": quantity,
            "source_unit": source_unit,
            "target_unit": target_unit,
            "result_quantity": converted,
            "kind": "same_dimension",
        }
    try:
        density = float(density_g_per_ml)
    except (TypeError, ValueError):
        return None
    if (
        inventory_type != "reagent"
        or str(physical_state or "").strip().casefold() != "liquid"
        or concentration_value is not None
        or not math.isfinite(density)
        or density <= 0
    ):
        return None
    source_base = quantity * source_rate
    converted = (
        source_base / density / target_rate
        if source_dimension == "mass"
        else source_base * density / target_rate
    )
    return converted, {
        "source_quantity": quantity,
        "source_unit": source_unit,
        "target_unit": target_unit,
        "result_quantity": converted,
        "kind": "cross_dimension",
        "density_g_per_ml": density,
    }


def _workflow_quantity_allocations(raw: Any) -> list[dict[str, Any]]:
    """读取库存预留载荷中的数量分配；非本协议记录返回空数组。"""

    payload = _load(raw, {})
    if not isinstance(payload, Mapping) or payload.get("kind") != "workflow_quantity":
        return []
    allocations = payload.get("allocations")
    if not isinstance(allocations, list) or any(
        not isinstance(item, Mapping) for item in allocations
    ):
        raise WorkflowQuantityReservationError("库存数量预留载荷损坏")
    return [dict(item) for item in allocations]


def active_workflow_reserved_quantities(
    connection: sqlite3.Connection,
    *,
    excluding_task_uuid: str = "",
) -> dict[tuple[str, str], float]:
    """扫描一次活动预留，按实例类型和身份批量汇总数量。

    每个键的数量使用实例单位；损坏的本协议载荷关闭式失败，不把它当作零预留。
    """

    rows = connection.execute(
        "SELECT workflow_id,amounts_json FROM inventory_reservation "
        "WHERE status='active'"
    ).fetchall()
    quantities: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        if excluding_task_uuid and str(row["workflow_id"]) == excluding_task_uuid:
            continue
        for allocation in _workflow_quantity_allocations(row["amounts_json"]):
            try:
                quantity = float(allocation["reserved_quantity"])
                identity = (str(allocation["inventory_type"]), str(allocation["inventory_uuid"]))
            except (KeyError, TypeError, ValueError) as error:
                raise WorkflowQuantityReservationError("库存数量预留载荷损坏") from error
            if not math.isfinite(quantity) or quantity <= 0:
                raise WorkflowQuantityReservationError("库存数量预留载荷损坏")
            quantities.setdefault(identity, []).append(quantity)
    return {identity: math.fsum(values) for identity, values in quantities.items()}


def active_workflow_reserved_quantity(
    connection: sqlite3.Connection,
    *,
    inventory_type: str,
    inventory_uuid: str,
    excluding_task_uuid: str = "",
) -> float:
    """返回单个实例的活动预留；批量展示应复用一次批量汇总结果。"""

    return active_workflow_reserved_quantities(
        connection, excluding_task_uuid=excluding_task_uuid
    ).get((inventory_type, inventory_uuid), 0.0)


def assert_workflow_quantity_mutation_allowed(
    connection: sqlite3.Connection,
    *,
    inventory_type: str,
    inventory_uuid: str,
    quantity: float,
    current_unit: str,
    next_unit: str,
) -> None:
    """保护普通库存写操作不得侵占工作流活动预留。

    参数：库存事务、实例身份、修改后余量与修改前后单位。返回无。异常：修改单位或
    把余量降到活动预留以下时抛 ``WorkflowQuantityReservationError``；增加余量
    始终允许，且本函数不改变任何库存事实。
    """

    reserved = active_workflow_reserved_quantity(
        connection,
        inventory_type=inventory_type,
        inventory_uuid=inventory_uuid,
    )
    if reserved <= 1e-9:
        return
    if current_unit.casefold() != next_unit.casefold():
        raise WorkflowQuantityReservationError("活动工作流预留存在，不能修改库存单位")
    if float(quantity) + 1e-9 < reserved:
        raise WorkflowQuantityReservationError(
            f"修改后余量 {float(quantity):g} 小于工作流已预留数量 {reserved:g}"
        )


class WorkflowQuantityInventoryAuthority:
    """隐藏工作流协调器对库存表、台账和发件箱的全部了解。"""

    def __init__(self, store: InventoryStore, *, edge_id: str, lab_id: str) -> None:
        """绑定既有库存权威。

        参数：``store`` 是唯一库存写入口；Edge/Lab 身份用于事件发件箱。返回无。
        异常：无；所有写操作都复用 ``InventoryStore.transaction`` 串行提交。
        """

        self._store = store
        self._edge_id = str(edge_id)
        self._lab_id = str(lab_id)

    def content(self, inventory_type: str, inventory_uuid: str) -> dict[str, Any]:
        """读取活动库存实例与容器身份；不存在或已删除时关闭式失败。"""

        row = self._store.query_one(
            self._content_sql(inventory_type),
            (inventory_uuid,),
        )
        if row is None:
            raise WorkflowQuantityReservationError(
                f"库存实例 {inventory_type}:{inventory_uuid} 不存在或已删除"
            )
        return row

    def content_for_material_source(
        self,
        source_node: Mapping[str, Any],
    ) -> dict[str, Any]:
        """解析一个确定物料来源容器的唯一数量型内容物。

        参数：``source_node`` 是冻结执行计划中的 material_source 节点，必须以
        固定实例或固定库位唯一定位容器。返回：该容器唯一活动 reagent 或
        current_substance 库存投影，并携带 ``inventory_type``。异常：宽泛自动
        选择器、空库位、容器无内容物、同时存在两种内容物或数据损坏时
        关闭式失败；正式准入仍会在同一库存事务中复核来源与数量分配一致。
        """

        raw_requirements = source_node.get("material_requirements")
        if (
            not isinstance(raw_requirements, Sequence)
            or isinstance(raw_requirements, (str, bytes))
            or len(raw_requirements) != 1
            or not isinstance(raw_requirements[0], Mapping)
        ):
            raise WorkflowQuantityReservationError(
                "数量需求引用的物料来源必须唯一定位一个容器"
            )
        requirement = raw_requirements[0]
        material_uuid = str(requirement.get("instance_uuid") or "").strip()
        if not material_uuid:
            site_uuid = str(requirement.get("site_uuid") or "").strip()
            if not site_uuid:
                raise WorkflowQuantityReservationError(
                    "数量需求暂不支持宽泛自动选择的物料来源"
                )
            site = self._store.query_one(
                "SELECT occupied_material_uuid FROM site "
                "WHERE uuid=? AND deleted_at IS NULL",
                (site_uuid,),
            )
            if site is None:
                raise WorkflowQuantityReservationError(
                    f"数量需求引用的来源库位 {site_uuid} 不存在"
                )
            material_uuid = str(site.get("occupied_material_uuid") or "").strip()
            if not material_uuid:
                raise WorkflowQuantityReservationError(
                    f"数量需求引用的来源库位 {site_uuid} 当前为空"
                )
        candidates = [
            (inventory_type, row)
            for inventory_type, row in (
                (
                    "reagent",
                    self._store.query_one(
                        "SELECT uuid FROM reagent "
                        "WHERE material_uuid=? AND deleted_at IS NULL",
                        (material_uuid,),
                    ),
                ),
                (
                    "current_substance",
                    self._store.query_one(
                        "SELECT uuid FROM current_substance "
                        "WHERE material_uuid=? AND deleted_at IS NULL",
                        (material_uuid,),
                    ),
                ),
            )
            if row is not None
        ]
        if not candidates:
            raise WorkflowQuantityReservationError(
                f"来源容器 {material_uuid} 没有试剂或当前内容物数量"
            )
        if len(candidates) != 1:
            raise WorkflowQuantityReservationError(
                f"来源容器 {material_uuid} 存在多个数量型内容物"
            )
        inventory_type, candidate = candidates[0]
        return {
            **self.content(inventory_type, str(candidate["uuid"])),
            "inventory_type": inventory_type,
        }

    def reserve_task(
        self,
        task_uuid: str,
        allocations: Sequence[Mapping[str, Any]],
        connection: sqlite3.Connection | None = None,
        *,
        incremental: bool = False,
    ) -> None:
        """在库存权威内全有或全无地预留一个任务的数量。

        参数：任务身份和工作流库待写分配。返回无。异常：库存缺失、单位变化、余量
        不足或同一任务重放内容冲突时抛 ``WorkflowQuantityReservationError``。
        同一任务/Job/分配内容重放幂等，不重复写预留、台账或发件箱。
        """

        by_job: dict[str, list[dict[str, Any]]] = {}
        for allocation in allocations:
            by_job.setdefault(str(allocation["workflow_node_job_uuid"]), []).append(
                dict(allocation)
            )
        expected_payloads = {
            job_uuid: {
                "kind": "workflow_quantity",
                "workflow_task_uuid": task_uuid,
                "workflow_node_job_uuid": job_uuid,
                "allocations": sorted(
                    items,
                    key=lambda item: (
                        str(item["inventory_type"]),
                        str(item["inventory_uuid"]),
                        str(item["uuid"]),
                    ),
                ),
            }
            for job_uuid, items in by_job.items()
        }
        transaction = (
            self._store.transaction()
            if connection is None
            else nullcontext(connection)
        )
        with transaction as connection:
            existing_rows = connection.execute(
                "SELECT * FROM inventory_reservation WHERE workflow_id=?",
                (task_uuid,),
            ).fetchall()
            existing_quantity = {
                str(row["node_id"]): row
                for row in existing_rows
                if _workflow_quantity_allocations(row["amounts_json"])
            }
            # ``merged_payloads`` 是同一消费 Job 已有预留需要追加分配后的完整载荷。
            merged_payloads: dict[str, dict[str, Any]] = {}
            if incremental:
                # 逐来源准入只追加本次选中分支的分配，其他已执行来源保持原事实。
                # 同一消费动作可声明多个运行时来源的需求：预留仍按 Job 一行，
                # 后到来源把自己的分配并入该行；已存在的分配必须逐项一致。
                added_allocations: list[dict[str, Any]] = []
                for job_uuid in set(existing_quantity) & set(expected_payloads):
                    row = existing_quantity[job_uuid]
                    if row["status"] != "active":
                        raise WorkflowQuantityReservationError("运行时数量预留与首次提交冲突")
                    current_payload = _load(row["amounts_json"], {})
                    current_items = {
                        str(item["uuid"]): item
                        for item in _workflow_quantity_allocations(row["amounts_json"])
                    }
                    added: list[dict[str, Any]] = []
                    for item in expected_payloads[job_uuid]["allocations"]:
                        prior = current_items.get(str(item["uuid"]))
                        if prior is None:
                            added.append(item)
                        elif prior != item:
                            raise WorkflowQuantityReservationError("运行时数量预留与首次提交冲突")
                    if added:
                        added_allocations.extend(added)
                        merged_payloads[job_uuid] = {
                            **current_payload,
                            "allocations": sorted(
                                [*current_items.values(), *added],
                                key=lambda item: (
                                    str(item["inventory_type"]),
                                    str(item["inventory_uuid"]),
                                    str(item["uuid"]),
                                ),
                            ),
                        }
                allocations = [
                    *added_allocations,
                    *(
                        item for item in allocations
                        if str(item["workflow_node_job_uuid"]) not in existing_quantity
                    ),
                ]
                expected_payloads = {key: value for key, value in expected_payloads.items() if key not in existing_quantity}
                existing_quantity = {}
                if not allocations:
                    return
            if existing_quantity:
                if set(existing_quantity) != set(expected_payloads):
                    raise WorkflowQuantityReservationError("任务数量预留身份发生变化")
                for job_uuid, expected in expected_payloads.items():
                    row = existing_quantity[job_uuid]
                    if (
                        row["status"] != "active"
                        or _load(row["amounts_json"], {}) != expected
                    ):
                        raise WorkflowQuantityReservationError(
                            "任务数量预留与首次提交内容冲突"
                        )
                return

            self._check_available(
                connection,
                allocations,
                excluding_task_uuid="" if incremental else task_uuid,
                task_uuid=task_uuid,
            )

            now_ms = int(time.time() * 1000)
            for job_uuid, payload in expected_payloads.items():
                reservation_uuid = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"unilab:workflow-quantity-reservation:{task_uuid}:{job_uuid}",
                    )
                )
                connection.execute(
                    "INSERT INTO inventory_reservation("
                    "reservation_id,workflow_id,node_id,attempt,status,amounts_json,"
                    "created_at,version) VALUES(?,?,?,1,'active',?,?,1)",
                    (reservation_uuid, task_uuid, job_uuid, _json(payload), now_ms),
                )
                event_uuid = str(
                    uuid5(NAMESPACE_URL, f"unilab:reserve-event:{reservation_uuid}")
                )
                InventoryStore.tx_append_inventory_event(
                    connection,
                    entry_uuid=event_uuid,
                    edge_id=self._edge_id,
                    lab_id=self._lab_id,
                    occurred_at=now_ms,
                    aggregate_type="workflow_inventory_reservation",
                    aggregate_id=reservation_uuid,
                    aggregate_version=1,
                    event_type="workflow_inventory.reserved",
                    payload={**payload, "reservation_state": "reserved"},
                    actor="workflow",
                    reason="workflow_inventory_reserve",
                    causation_id=f"reserve:{task_uuid}",
                    workflow_task_uuid=task_uuid,
                    workflow_node_job_uuid=job_uuid,
                )
            for job_uuid, payload in merged_payloads.items():
                row = connection.execute(
                    "SELECT * FROM inventory_reservation "
                    "WHERE workflow_id=? AND node_id=? AND status='active'",
                    (task_uuid, job_uuid),
                ).fetchone()
                if row is None:
                    raise WorkflowQuantityReservationError("运行时数量预留与首次提交冲突")
                reservation_uuid = str(row["reservation_id"])
                revision = int(row["version"]) + 1
                changed = connection.execute(
                    "UPDATE inventory_reservation SET amounts_json=?,version=? "
                    "WHERE reservation_id=? AND status='active' AND version=?",
                    (_json(payload), revision, reservation_uuid, row["version"]),
                ).rowcount
                if changed != 1:
                    raise WorkflowQuantityReservationError("运行时数量预留修订已变化，请重试")
                # 事件身份由合并后的完整分配集合决定，同一追加重放得到同一事件。
                allocation_uuids = ",".join(
                    sorted(str(item["uuid"]) for item in payload["allocations"])
                )
                InventoryStore.tx_append_inventory_event(
                    connection,
                    entry_uuid=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"unilab:reserve-event:{reservation_uuid}:{allocation_uuids}",
                        )
                    ),
                    edge_id=self._edge_id,
                    lab_id=self._lab_id,
                    occurred_at=now_ms,
                    aggregate_type="workflow_inventory_reservation",
                    aggregate_id=reservation_uuid,
                    aggregate_version=revision,
                    event_type="workflow_inventory.reserved",
                    payload={**payload, "reservation_state": "reserved"},
                    actor="workflow",
                    reason="workflow_inventory_reserve",
                    causation_id=f"reserve:{task_uuid}",
                    workflow_task_uuid=task_uuid,
                    workflow_node_job_uuid=job_uuid,
                )

    def preflight_task(self, allocations: Sequence[Mapping[str, Any]]) -> None:
        """只读验证整任务数量绑定当前是否可全量预留。

        参数：已完成身份与需求匹配的分配集合。返回无。异常：库存实例缺失、单位
        变化或扣除其他活动任务预留后的可用量不足时抛
        ``WorkflowQuantityReservationError``。检查在一个库存事务快照内完成，不
        创建预留、台账或 Outbox 事件，因此结果只是提示而不是执行承诺。
        """

        with self._store.transaction() as connection:
            self._check_available(connection, allocations)

    def _check_available(
        self,
        connection: sqlite3.Connection,
        allocations: Sequence[Mapping[str, Any]],
        *,
        excluding_task_uuid: str = "",
        task_uuid: str = "",
    ) -> None:
        """在同一库存快照内按实例聚合并验证整组可用数量。"""

        requested: dict[tuple[str, str], list[float]] = {}
        for allocation in allocations:
            inventory_type = str(allocation["inventory_type"])
            inventory_uuid = str(allocation["inventory_uuid"])
            current = self._tx_content(
                connection,
                inventory_type=inventory_type,
                inventory_uuid=inventory_uuid,
                task_uuid=task_uuid,
            )
            if (
                str(current["quantity_unit"]).casefold()
                != str(allocation["quantity_unit"]).casefold()
            ):
                raise WorkflowQuantityReservationError(
                    "库存实例单位与工作流绑定单位不一致"
                )
            identity = (inventory_type, inventory_uuid)
            requested.setdefault(identity, []).append(float(allocation["reserved_quantity"]))
        reserved_by_identity = active_workflow_reserved_quantities(
            connection, excluding_task_uuid=excluding_task_uuid
        )
        shortages: list[dict[str, Any]] = []
        for (inventory_type, inventory_uuid), quantities in requested.items():
            current = self._tx_content(
                connection,
                inventory_type=inventory_type,
                inventory_uuid=inventory_uuid,
                task_uuid=task_uuid,
            )
            reserved = reserved_by_identity.get((inventory_type, inventory_uuid), 0.0)
            available = float(current["quantity"]) - reserved
            required = math.fsum(quantities)
            if available + 1e-9 < required:
                site_name, material_name = self._shortage_container(
                    connection, str(current["material_uuid"])
                )
                shortages.append(
                    {
                        "site_name": site_name,
                        "material_uuid": str(current["material_uuid"]),
                        "material_name": material_name,
                        "available_quantity": available,
                        "required_quantity": required,
                        "quantity_unit": str(current["quantity_unit"]),
                    }
                )
        if shortages:
            raise WorkflowQuantityShortageError(
                format_quantity_shortage_message(shortages),
                shortages,
            )

    def release_task(self, task_uuid: str, *, reason: str) -> None:
        """幂等释放一个任务在库存权威中的全部活动数量预留。

        参数：任务身份与审计原因。返回无。异常：库存事务失败原样传播；已消费或
        已释放记录不回退，重复释放不追加事件。
        """

        with self._store.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM inventory_reservation "
                "WHERE workflow_id=? AND status='active' ORDER BY node_id",
                (task_uuid,),
            ).fetchall()
            now_ms = int(time.time() * 1000)
            for row in rows:
                allocations = _workflow_quantity_allocations(row["amounts_json"])
                if not allocations:
                    continue
                revision = int(row["version"]) + 1
                connection.execute(
                    "UPDATE inventory_reservation SET status='released',version=? "
                    "WHERE reservation_id=? AND status='active'",
                    (revision, row["reservation_id"]),
                )
                event_uuid = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"unilab:release-event:{row['reservation_id']}",
                    )
                )
                InventoryStore.tx_append_inventory_event(
                    connection,
                    entry_uuid=event_uuid,
                    edge_id=self._edge_id,
                    lab_id=self._lab_id,
                    occurred_at=now_ms,
                    aggregate_type="workflow_inventory_reservation",
                    aggregate_id=str(row["reservation_id"]),
                    aggregate_version=revision,
                    event_type="workflow_inventory.released",
                    payload={
                        "kind": "workflow_quantity",
                        "workflow_task_uuid": task_uuid,
                        "workflow_node_job_uuid": str(row["node_id"]),
                        "reservation_state": "released",
                        "reason": str(reason),
                    },
                    actor="workflow",
                    reason=str(reason),
                    causation_id=f"release:{task_uuid}",
                    workflow_task_uuid=task_uuid,
                    workflow_node_job_uuid=str(row["node_id"]),
                )

    def release_orphans(self, task_exists: Callable[[str], bool]) -> int:
        """释放库存已预留但工作流任务未落库的崩溃残留。

        参数：``task_exists`` 查询工作流权威。返回：完成释放的任务数。异常：库存
        载荷损坏或释放失败原样传播；存在任务的活动预留保持不变。
        """

        rows = self._store.query_all(
            "SELECT DISTINCT workflow_id,amounts_json FROM inventory_reservation "
            "WHERE status='active' ORDER BY workflow_id"
        )
        candidates = {
            str(row["workflow_id"])
            for row in rows
            if _workflow_quantity_allocations(row["amounts_json"])
        }
        released = 0
        for task_uuid in sorted(candidates):
            if task_exists(task_uuid):
                continue
            self.release_task(task_uuid, reason="orphan_task_not_committed")
            released += 1
        return released

    def apply_consumption(
        self,
        *,
        task_uuid: str,
        operation_key: str,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        """在库存事务内幂等扣减实际用量并结算对应预留。

        参数：任务、稳定操作键与冻结消费记录。返回无。异常：实例消失、实际用量
        超过预留、余量不足或修订冲突时关闭式失败；同一事件 UUID 重放不重复扣减。
        正式重试的新 attempt 结算首个 attempt 留下的预留时，记录携带
        ``reservation_job_uuid`` 指向实际持有库存预留的作业；消费事实仍记在
        实际成功的作业上。
        """

        if not records:
            return
        # 数量预留按“首次持有预留的 Job”建行（库存侧 attempt 恒为 1）；
        # 同一次结算的全部记录必须来自同一预留行，禁止跨行混结。
        reservation_jobs = {
            str(record.get("reservation_job_uuid") or record["workflow_node_job_uuid"])
            for record in records
        }
        if len(reservation_jobs) != 1:
            raise WorkflowQuantityReservationError("一次数量结算不能跨越多个库存预留")
        reservation_job_uuid = reservation_jobs.pop()
        now_ms = int(time.time() * 1000)
        now_text = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._store.transaction() as connection:
            reservation = connection.execute(
                "SELECT * FROM inventory_reservation "
                "WHERE workflow_id=? AND node_id=? AND attempt=1",
                (task_uuid, reservation_job_uuid),
            ).fetchone()
            if reservation is None or not _workflow_quantity_allocations(
                reservation["amounts_json"]
            ):
                raise WorkflowQuantityReservationError("作业没有库存侧数量预留")
            reserved_allocation_uuids = {
                str(item.get("uuid"))
                for item in _workflow_quantity_allocations(reservation["amounts_json"])
            }
            if any(str(record["uuid"]) not in reserved_allocation_uuids for record in records):
                raise WorkflowQuantityReservationError("消费记录不属于该作业的库存数量预留")
            if reservation["status"] not in {"active", "consumed"}:
                raise WorkflowQuantityReservationError("作业数量预留已被释放")
            for record in records:
                event_uuid = str(record["event_uuid"])
                if (
                    connection.execute(
                        "SELECT 1 FROM inventory_ledger WHERE entry_uuid=?",
                        (event_uuid,),
                    ).fetchone()
                    is not None
                ):
                    continue
                self._tx_consume_record(
                    connection,
                    task_uuid=task_uuid,
                    operation_key=operation_key,
                    record=record,
                    now_ms=now_ms,
                    now_text=now_text,
                )
            if reservation["status"] == "active":
                revision = int(reservation["version"]) + 1
                connection.execute(
                    "UPDATE inventory_reservation SET status='consumed',version=? "
                    "WHERE reservation_id=? AND status='active'",
                    (revision, reservation["reservation_id"]),
                )

    def list_task_consumptions(self, task_uuid: str) -> list[dict[str, Any]]:
        """按任务返回本模块写入的实际消费事实。"""

        return self._consumptions("workflow_task_uuid=?", (task_uuid,), reverse=False)

    def list_task_material_events(
        self,
        task_uuid: str,
        job_uuids: set[str],
        *,
        since_ms: int = 0,
    ) -> list[dict[str, Any]]:
        """按任务读取物料变化台账：搬运登记、数量消耗、标为已使用，按发生顺序排列。

        参数：``task_uuid`` 是任务身份；``job_uuids`` 是该任务全部作业身份，用于认领
        以作业身份作为因果标识写入的"已使用"事件；``since_ms`` 是任务创建时刻，
        缩小扫描范围。返回：只读投影，不改变任何库存事实。
        """

        rows = self._store.query_all(
            "SELECT * FROM inventory_ledger "
            "WHERE occurred_at >= ? AND ("
            " (op_type IN ('material.transfer','reagent.consume') AND workflow_task_uuid = ?)"
            " OR op_type = 'instance.used') "
            "ORDER BY occurred_at ASC, ledger_id ASC",
            (int(since_ms), task_uuid),
        )
        events: list[dict[str, Any]] = []
        names: dict[str, str] = {}

        def material_name(material_uuid: str) -> str:
            if material_uuid not in names:
                row = self._store.query_one(
                    "SELECT name, barcode FROM material WHERE uuid=?", (material_uuid,)
                )
                names[material_uuid] = (
                    str(row["name"] or row["barcode"] or "") if row is not None else ""
                )
            return names[material_uuid]

        for row in rows:
            payload = _load(row.get("delta_json"), {})
            payload = payload if isinstance(payload, Mapping) else {}
            changes = payload.get("changes")
            changes = changes if isinstance(changes, Mapping) else {}
            occurred_at = (
                datetime.fromtimestamp(int(row["occurred_at"]) / 1000, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            base = {"uuid": str(row["entry_uuid"] or row["ledger_id"]), "occurred_at": occurred_at}
            op_type = str(row["op_type"])
            if op_type == "material.transfer":
                material = str(row["material_uuid"] or row["aggregate_id"])
                events.append({
                    **base,
                    "kind": "moved",
                    "material_uuid": material,
                    "material_name": str(changes.get("name") or material_name(material)),
                    "from_site": changes.get("site_name_before"),
                    "to_site": changes.get("site_name_after"),
                    "from_owner_uuid": changes.get("parent_uuid_before"),
                    "to_owner_uuid": changes.get("parent_uuid_after"),
                    "workflow_node_job_uuid": row["workflow_node_job_uuid"],
                })
            elif op_type == "reagent.consume":
                if payload.get("status") != "consumed":
                    continue
                material = str(row["material_uuid"] or "")
                events.append({
                    **base,
                    "kind": "consumed",
                    "material_uuid": material,
                    "material_name": material_name(material) if material else "",
                    "reagent_name": payload.get("display_name"),
                    "quantity": payload.get("actual_quantity"),
                    "quantity_unit": payload.get("quantity_unit"),
                    "balance_after": payload.get("balance_after"),
                    "workflow_node_job_uuid": row["workflow_node_job_uuid"],
                })
            elif op_type == "instance.used":
                job = str(row["causation_id"] or "")
                if job not in job_uuids:
                    continue
                material = str(row["aggregate_id"])
                events.append({
                    **base,
                    "kind": "used",
                    "material_uuid": material,
                    "material_name": material_name(material),
                    "reason": payload.get("reason") or row["reason"],
                    "workflow_node_job_uuid": job,
                })
        return events

    def list_job_consumptions(self, job_uuid: str) -> list[dict[str, Any]]:
        """按作业返回本模块写入的实际消费事实。"""

        return self._consumptions(
            "workflow_node_job_uuid=?", (job_uuid,), reverse=False
        )

    def list_reagent_consumptions(self, reagent_uuid: str) -> list[dict[str, Any]]:
        """按试剂实例倒序返回实际消费谱系，删除后的历史仍可读取。"""

        return self._consumptions(
            "subject_type='reagent' AND aggregate_id=?", (reagent_uuid,), reverse=True
        )

    @staticmethod
    def _content_sql(inventory_type: str) -> str:
        """返回受控库存类型的活动实例查询；未知类型关闭式失败。"""

        if inventory_type == "reagent":
            return """
                SELECT reagent.uuid,reagent.material_uuid,reagent.reagent_info_uuid,
                       reagent.quantity,reagent.quantity_unit,reagent.revision,
                       reagent.physical_state,reagent.density_g_per_ml,
                       reagent.concentration_value,reagent_info.name AS display_name,
                       material.barcode AS container_barcode
                FROM reagent JOIN material ON material.uuid=reagent.material_uuid
                JOIN reagent_info ON reagent_info.uuid=reagent.reagent_info_uuid
                WHERE reagent.uuid=? AND reagent.deleted_at IS NULL
                  AND material.deleted_at IS NULL AND reagent_info.deleted_at IS NULL
            """
        if inventory_type == "current_substance":
            return """
                SELECT current_substance.uuid,current_substance.material_uuid,
                       current_substance.quantity,current_substance.quantity_unit,
                       current_substance.revision,NULL AS reagent_info_uuid,
                       NULL AS density_g_per_ml,
                       current_substance.physical_state,NULL AS concentration_value,
                       COALESCE(current_substance.name,material.name) AS display_name,
                       material.barcode AS container_barcode
                FROM current_substance
                JOIN material ON material.uuid=current_substance.material_uuid
                WHERE current_substance.uuid=? AND current_substance.deleted_at IS NULL
                  AND material.deleted_at IS NULL
            """
        raise WorkflowQuantityReservationError("库存类型无效")

    @staticmethod
    def _shortage_container(
        connection: sqlite3.Connection, material_uuid: str
    ) -> tuple[str, str]:
        """读取容器当前占用的库位名和容器名。

        参数：库存事务和容器物料 UUID。返回：库位名、容器名；没有占用库位时
        库位名为空，调用方改用容器名。
        """

        row = connection.execute(
            """
            SELECT material.name AS material_name,
                   (
                       SELECT site.name FROM site
                       WHERE site.occupied_material_uuid = material.uuid
                         AND site.deleted_at IS NULL
                       ORDER BY site.sort_order, site.name
                       LIMIT 1
                   ) AS site_name
            FROM material
            WHERE material.uuid = ? AND material.deleted_at IS NULL
            """,
            (material_uuid,),
        ).fetchone()
        if row is None:
            return "", ""
        return (
            str(row["site_name"] or "").strip(),
            str(row["material_name"] or "").strip(),
        )

    def _tx_content(
        self,
        connection: sqlite3.Connection,
        *,
        inventory_type: str,
        inventory_uuid: str,
        task_uuid: str = "",
    ) -> sqlite3.Row:
        """在既有库存事务内读取活动实例；缺失时关闭式失败。"""

        row = connection.execute(
            self._content_sql(inventory_type),
            (inventory_uuid,),
        ).fetchone()
        if row is None:
            raise WorkflowQuantityReservationError("库存实例不存在或已删除")
        from unilabos.app.scheduler.inventory.material_inputs import material_input_blocker
        from unilabos.app.scheduler.resource_lock import material_lock_key

        if material_input_blocker(connection, [material_lock_key(row["material_uuid"])], task_uuid=task_uuid) is not None:
            raise WorkflowQuantityReservationError("库存实例已为另一个中心调用的本地任务保留")
        if connection.execute(
            "SELECT 1 FROM station_handoff_resource WHERE material_uuid=? AND active=1",
            (row["material_uuid"],),
        ).fetchone():
            raise WorkflowQuantityReservationError("库存实例处于跨站交接保护，不能预留或结算数量")
        return row

    def _tx_consume_record(
        self,
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        operation_key: str,
        record: Mapping[str, Any],
        now_ms: int,
        now_text: str,
    ) -> None:
        """在库存事务内应用单条稳定消费事件；调用方负责去重。"""

        inventory_type = str(record["inventory_type"])
        inventory_uuid = str(record["inventory_uuid"])
        current = self._tx_content(
            connection,
            inventory_type=inventory_type,
            inventory_uuid=inventory_uuid,
            task_uuid=task_uuid,
        )
        live_unit = str(current["quantity_unit"])
        planned_unit = str(record["quantity_unit"])
        reported_unit = str(record["reported_quantity_unit"])
        planned_conversion = convert_quantity(
            float(record["planned_quantity"]),
            planned_unit,
            live_unit,
            inventory_type=inventory_type,
            physical_state=current["physical_state"],
            concentration_value=current["concentration_value"],
            density_g_per_ml=current["density_g_per_ml"],
        )
        actual_conversion = convert_quantity(
            float(record["actual_quantity"]),
            reported_unit,
            live_unit,
            inventory_type=inventory_type,
            physical_state=current["physical_state"],
            concentration_value=current["concentration_value"],
            density_g_per_ml=current["density_g_per_ml"],
        )
        try:
            physical_fact = aliquot_consumption_fact(connection, record)
        except ValueError as error:
            raise WorkflowQuantityReservationError(str(error)) from error
        if physical_fact is not None:
            physical_conversion = convert_quantity(
                float(physical_fact["actual_quantity"]),
                str(physical_fact["quantity_unit"]),
                live_unit,
                inventory_type=inventory_type,
                physical_state=current["physical_state"],
                concentration_value=current["concentration_value"],
                density_g_per_ml=current["density_g_per_ml"],
            )
            if physical_conversion is None:
                raise WorkflowQuantityReservationError("分装消费事实单位无法安全换算，需人工对账")
            if record.get("actual_quantity_reported", True):
                if actual_conversion is None or not math.isclose(
                    actual_conversion[0], physical_conversion[0], rel_tol=1e-9, abs_tol=1e-9
                ):
                    raise WorkflowQuantityReservationError("实际上报消耗与已提交分装事实不一致，需人工对账")
            else:
                reported_unit = str(physical_fact["quantity_unit"])
            actual_conversion = physical_conversion
        if planned_conversion is None or actual_conversion is None:
            raise WorkflowQuantityReservationError(
                "库存消费单位无法安全换算，需人工对账："
                f"计划 {planned_unit}、实际上报 {reported_unit}、库存 {live_unit}"
            )
        planned_quantity = planned_conversion[0]
        actual_quantity = actual_conversion[0]
        overrun_quantity = 0.0
        try:
            subtract_quantity(planned_quantity, actual_quantity)
        except WorkflowQuantityReservationError as error:
            if physical_fact is None and not record.get("reconciliation"):
                raise WorkflowQuantityReservationError("实际消耗量不能超过本作业的预留量") from error
            # 已提交物理事实和明确人工核对的用量必须如实记账，不能要求伪造为计划值。
            overrun_quantity = actual_quantity - planned_quantity
        balance_before = float(current["quantity"])
        revision = int(current["revision"])
        if physical_fact is not None:
            # 分装已原子扣减源瓶并增加目标瓶，本事件只确认数量预留的消费归属。
            balance_before = float(physical_fact["balance_before"])
            balance_after = float(physical_fact["balance_after"])
        else:
            balance_after = subtract_quantity(balance_before, actual_quantity)
            if overrun_quantity:
                reserved = active_workflow_reserved_quantity(
                    connection, inventory_type=inventory_type, inventory_uuid=inventory_uuid
                )
                remaining_reserved = subtract_quantity(reserved, planned_quantity)
                try:
                    subtract_quantity(balance_after, remaining_reserved)
                except WorkflowQuantityReservationError as error:
                    raise WorkflowQuantityReservationError(
                        "实际超耗会侵占其他作业的活动预留，需先完成库存对账"
                    ) from error
            revision += 1
            # 试剂和现配物已经是 container_content 上的视图。SQLite 对 INSTEAD OF
            # UPDATE 的 rowcount 恒为 0，乐观修订必须直接改基表。
            kind = "reagent" if inventory_type == "reagent" else "substance"
            changed = connection.execute(
                "UPDATE container_content SET quantity=?,revision=?,update_time=? "
                "WHERE uuid=? AND kind=? AND deleted_at IS NULL AND revision=?",
                (
                    balance_after,
                    revision,
                    now_text,
                    inventory_uuid,
                    kind,
                    current["revision"],
                ),
            ).rowcount
            if changed != 1:
                raise WorkflowQuantityReservationError(
                    "库存修订已变化，请重放同一作业结果"
                )
        consumption = {
            "workflow_task_uuid": task_uuid,
            "workflow_node_job_uuid": str(record["workflow_node_job_uuid"]),
            "workflow_node_uuid": str(record["workflow_node_uuid"]),
            "planned_quantity": planned_quantity,
            "actual_quantity": actual_quantity,
            "planned_quantity_unit": planned_unit,
            "reported_quantity_unit": reported_unit,
            "quantity_unit": live_unit,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "status": "consumed",
            "display_name": str(current["display_name"] or ""),
            "container_barcode": str(current["container_barcode"] or ""),
            "workflow_name": str(record["workflow_name"] or ""),
            "node_name": str(record["node_name"] or ""),
        }
        if physical_fact is not None:
            consumption["physical_settlement_entry_uuid"] = physical_fact["entry_uuid"]
        if overrun_quantity:
            consumption["overrun_quantity"] = overrun_quantity
            consumption["overrun_authority"] = (
                "physical_settlement" if physical_fact is not None else "manual_reconciliation"
            )
        if record.get("reconciliation") is not None:
            consumption["reconciliation"] = record["reconciliation"]
        if (
            actual_conversion is not None
            and reported_unit.casefold() != live_unit.casefold()
        ):
            consumption["quantity_conversion"] = actual_conversion[1]
        InventoryStore.tx_append_inventory_event(
            connection,
            entry_uuid=str(record["event_uuid"]),
            edge_id=self._edge_id,
            lab_id=self._lab_id,
            occurred_at=now_ms,
            aggregate_type=inventory_type,
            aggregate_id=inventory_uuid,
            aggregate_version=revision,
            event_type=f"{inventory_type}.consume",
            payload={
                "changes": {
                    "result": {
                        "quantity": float(current["quantity"]) if physical_fact is not None else balance_after,
                        "quantity_unit": live_unit,
                        "revision": revision,
                        "consumption": consumption,
                    }
                },
                "extension": {},
                **consumption,
            },
            actor="workflow",
            reason="workflow_inventory_consumption",
            causation_id=operation_key,
            material_uuid=str(record["material_uuid"]),
            subject_type=inventory_type,
            quantity_delta=0.0 if physical_fact is not None else balance_after - balance_before,
            quantity_unit=live_unit,
            revision=revision,
            workflow_task_uuid=task_uuid,
            workflow_node_job_uuid=str(record["workflow_node_job_uuid"]),
        )

    def _consumptions(
        self,
        where: str,
        params: tuple[Any, ...],
        *,
        reverse: bool,
    ) -> list[dict[str, Any]]:
        """按受控条件读取统一台账并投影消费 DTO。"""

        order = "DESC" if reverse else "ASC"
        rows = self._store.query_all(
            "SELECT * FROM inventory_ledger WHERE "
            + where
            + " AND subject_type IN ('reagent','current_substance') "
            + f"ORDER BY occurred_at {order},entry_uuid {order}",
            params,
        )
        return [self._consumption_row(row) for row in rows if self._is_consumption(row)]

    @staticmethod
    def _is_consumption(row: Mapping[str, Any]) -> bool:
        """判断统一库存台账行是否包含工作流消费快照。"""

        payload = _load(row.get("delta_json"), {})
        return (
            isinstance(payload, Mapping)
            and isinstance(payload.get("workflow_task_uuid"), str)
            and "status" in payload
        )

    @staticmethod
    def _consumption_row(row: Mapping[str, Any]) -> dict[str, Any]:
        """把统一台账事件投影成 Backend 兼容库存消费事实。"""

        payload = _load(row.get("delta_json"), {})
        return {
            "uuid": str(row["entry_uuid"]),
            "workflow_task_uuid": payload.get("workflow_task_uuid"),
            "workflow_node_job_uuid": payload.get("workflow_node_job_uuid"),
            "workflow_node_uuid": payload.get("workflow_node_uuid"),
            "inventory_type": str(row["subject_type"]),
            "inventory_uuid": str(row["aggregate_id"]),
            "planned_quantity": payload.get("planned_quantity"),
            "actual_quantity": payload.get("actual_quantity"),
            "quantity_unit": payload.get("quantity_unit"),
            "balance_before": payload.get("balance_before"),
            "balance_after": payload.get("balance_after"),
            "status": payload.get("status"),
            "consumed_at": datetime.fromtimestamp(
                int(row["occurred_at"]) / 1000,
                timezone.utc,
            )
            .isoformat()
            .replace("+00:00", "Z"),
            "meta_data": {
                "planned_quantity_unit": payload.get("planned_quantity_unit"),
                "reported_quantity_unit": payload.get("reported_quantity_unit"),
                **(
                    {"quantity_conversion": payload["quantity_conversion"]}
                    if payload.get("quantity_conversion") is not None
                    else {}
                ),
                **{
                    key: payload[key]
                    for key in (
                        "physical_settlement_entry_uuid", "reconciliation",
                        "overrun_quantity", "overrun_authority",
                    )
                    if key in payload
                },
            },
            "display_name": str(payload.get("display_name") or ""),
            "container_barcode": str(payload.get("container_barcode") or ""),
            "workflow_name": str(payload.get("workflow_name") or ""),
            "node_name": str(payload.get("node_name") or ""),
        }


__all__ = [
    "WorkflowQuantityInventoryAuthority",
    "WorkflowQuantityReservationError",
    "active_workflow_reserved_quantities",
    "active_workflow_reserved_quantity",
    "assert_workflow_quantity_mutation_allowed",
    "convert_quantity",
    "subtract_quantity",
]
