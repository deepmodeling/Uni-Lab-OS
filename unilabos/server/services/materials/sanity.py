"""派发前物料健全性检查的只读事实（调度器 ``server/scheduler/material_sanity.py`` 消费）。

节点就绪、申请资源之前，调度器把它引用的全部物料和它持有的库存预留一次交给这里，在同一把锁下读出：

- 每个物料：是否登记过、是否已删除、生命周期、所在库位、是否被未结算的物理动作占着（``material_execution_hold``），
  生命周期是 ``reserved`` / ``in_use`` 时还有占着它的库存预留（作业、任务、状态）；
- 本作业的库存预留：状态与各条分配此刻的批次 / 内容物现状（是否还在、数量、单位能否换算）。

只读不写，也不做判定：哪些算等待、哪些算预检报错由调度器决定。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any, Optional

from .quantity import convert_quantity

#: 单条 SQL 的 IN 参数上限（远低于 SQLite 限制，物料集合通常只有几个）
_CHUNK = 400


def _chunks(values: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start:start + _CHUNK]


def _convertible(quantity: Any, source: Any, target: Any, **physical: Any) -> bool:
    try:
        return convert_quantity(float(quantity or 0), str(source or ""), str(target or ""), **physical) is not None
    except ValueError:
        return False


def _material_facts(conn: Any, identities: Sequence[str]) -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {
        identity: {"material_uuid": identity, "exists": False} for identity in identities
    }
    for chunk in _chunks(identities):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            "SELECT material_uuid, lifecycle_status, deleted_at_ms, resource_type, parent_material_uuid "
            f"FROM material WHERE material_uuid IN ({marks})", tuple(chunk),
        ):
            facts[row["material_uuid"]] = {
                "material_uuid": row["material_uuid"],
                "exists": True,
                "deleted": row["deleted_at_ms"] is not None,
                "lifecycle_status": row["lifecycle_status"],
                "resource_type": row["resource_type"],
                "parent_material_uuid": row["parent_material_uuid"],
                "site_uuid": None,
                "site_owner_uuid": None,
                "held_by_job_uuid": None,
                "reservations": [],
            }
        for row in conn.execute(
            "SELECT occupied_material_uuid, site_uuid, owner_material_uuid FROM site "
            f"WHERE occupied_material_uuid IN ({marks}) AND deleted_at_ms IS NULL", tuple(chunk),
        ):
            fact = facts.get(row["occupied_material_uuid"])
            if fact and fact["exists"]:
                fact["site_uuid"] = row["site_uuid"]
                fact["site_owner_uuid"] = row["owner_material_uuid"]
        for row in conn.execute(
            "SELECT aggregate_uuid, job_uuid FROM material_execution_hold "
            f"WHERE aggregate_type='material' AND aggregate_uuid IN ({marks})", tuple(chunk),
        ):
            fact = facts.get(row["aggregate_uuid"])
            if fact and fact["exists"]:
                fact["held_by_job_uuid"] = row["job_uuid"]
    # 只有处于预留 / 使用中的物料才去查是谁的预留（预留条目存成 JSON，按物料反查要展开）
    claimed = sorted(
        identity for identity, fact in facts.items()
        if fact["exists"] and not fact["deleted"] and fact["lifecycle_status"] in {"reserved", "in_use"}
    )
    for chunk in _chunks(claimed):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            "SELECT r.reservation_uuid, r.job_uuid, r.task_uuid, r.status, "
            "json_extract(item.value, '$.material_uuid') AS material_uuid "
            "FROM inventory_reservation r, json_each(r.items_json) item "
            "WHERE r.status IN ('active','consumed') AND json_extract(item.value, '$.kind') = 'material' "
            f"AND json_extract(item.value, '$.material_uuid') IN ({marks}) "
            "ORDER BY r.created_at_ms, r.reservation_uuid", tuple(chunk),
        ):
            facts[row["material_uuid"]]["reservations"].append({
                "reservation_uuid": row["reservation_uuid"], "job_uuid": row["job_uuid"],
                "task_uuid": row["task_uuid"], "status": row["status"],
            })
    return facts


def _lot_current(conn: Any, item: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        "SELECT unit, quantity_total, quarantined FROM inventory_lot WHERE lot_uuid=?", (item.get("lot_uuid"),),
    ).fetchone()
    if row is None:
        return {"exists": False}
    return {
        "exists": True, "quantity": float(row["quantity_total"]), "unit": row["unit"],
        "quarantined": bool(row["quarantined"]),
        "unit_convertible": _convertible(item.get("quantity"), item.get("unit"), row["unit"]),
    }


def _content_current(conn: Any, item: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        "SELECT ms.material_uuid, ms.quantity, ms.quantity_unit, ms.physical_state, mc.concentration_value, "
        "mc.density_g_per_ml FROM material_substance ms LEFT JOIN material_content mc USING(substance_uuid) "
        "WHERE ms.substance_uuid=?", (item.get("substance_uuid"),),
    ).fetchone()
    if row is None:
        return {"exists": False}
    current: dict[str, Any] = {
        "exists": True, "material_uuid": row["material_uuid"], "quantity": float(row["quantity"]),
        "unit": row["quantity_unit"],
        "unit_convertible": _convertible(
            item.get("quantity"), item.get("unit"), row["quantity_unit"], inventory_type="content",
            physical_state=row["physical_state"], concentration_value=row["concentration_value"],
            density_g_per_ml=row["density_g_per_ml"],
        ),
        "reservation_status": None,
    }
    if item.get("content_reservation_uuid"):
        reservation = conn.execute(
            "SELECT status FROM material_content_reservation WHERE reservation_uuid=?",
            (item["content_reservation_uuid"],),
        ).fetchone()
        current["reservation_status"] = reservation["status"] if reservation is not None else "missing"
    return current


def _reservation_facts(conn: Any, reservation_uuid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT reservation_uuid, job_uuid, task_uuid, status, items_json FROM inventory_reservation "
        "WHERE reservation_uuid=?", (reservation_uuid,),
    ).fetchone()
    if row is None:
        return {"reservation_uuid": reservation_uuid, "exists": False}
    items = []
    for item in json.loads(row["items_json"]):
        current: dict[str, Any] = {}
        if item.get("kind") == "lot":
            current = _lot_current(conn, item)
        elif item.get("kind") == "content":
            current = _content_current(conn, item)
        items.append({**item, "current": current})
    return {
        "reservation_uuid": row["reservation_uuid"], "exists": True, "job_uuid": row["job_uuid"],
        "task_uuid": row["task_uuid"], "status": row["status"], "items": items,
    }


def material_sanity_facts(
    service: Any, material_uuids: Iterable[str], *, reservation_uuid: Optional[str] = None,
) -> dict[str, Any]:
    """一次读出健全性检查要的物料与预留事实；未登记的物料 ``exists=False``，已删除的 ``deleted=True``。"""

    identities = sorted({str(item).strip() for item in material_uuids if str(item or "").strip()})
    with service._write_lock:
        conn = service.connection
        # 同一个读事务：物料、库位、物理占用与预留是同一时刻的事实
        nested = conn.in_transaction
        if not nested:
            conn.execute("BEGIN")
        try:
            return {
                "materials": _material_facts(conn, identities),
                "reservation": _reservation_facts(conn, reservation_uuid) if reservation_uuid else None,
            }
        finally:
            if not nested:
                conn.rollback()


__all__ = ["material_sanity_facts"]
