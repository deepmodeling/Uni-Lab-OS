"""仓储聚合视图（对齐云端 MaterialWarehouse 概念）.

云端模型：ResourceNodeTemplate（品类=性质载体）× MaterialWarehouse（批次库存，
含库位字典 material_standard_slot）× Barcode（一物一码）× MaterialNode（在台实例）。
库位是字典值而非几何位置——仓储的组织依据是**物料本身的性质**（品类/存储条件/
危险分级），前端画布只是把这套逻辑映射成交互，不引入平行的"手工摆放"事实。

本模块提供只读聚合：
    build_warehouse_view(store)  按品类聚合 批次/数量/在库实例/库位分布
性质约定放模板 spec_json（与云端模板 tags/schema 同职责）：
    storage_class: ambient | cold | frozen | flammable_cabinet | desiccator
    hazard_class:  none | flammable | corrosive | toxic | oxidizer | bio
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from unilabos.server.scheduler.inventory.store import InventoryStore

STORAGE_CLASSES = [
    {"id": "ambient", "name": "常温", "color": "#3b82f6"},
    {"id": "cold", "name": "冷藏 2-8°C", "color": "#0ea5e9"},
    {"id": "frozen", "name": "冷冻 -20°C", "color": "#6366f1"},
    {"id": "flammable_cabinet", "name": "防爆柜", "color": "#ef4444"},
    {"id": "desiccator", "name": "干燥器", "color": "#f59e0b"},
]

_ACTIVE_INSTANCE_STATES = ("warehouse", "reserved", "bench", "in_use")


def _parse_spec(spec_json: Any) -> Dict[str, Any]:
    try:
        spec = json.loads(spec_json or "{}")
    except (TypeError, ValueError):
        spec = {}
    return spec if isinstance(spec, dict) else {}


def build_warehouse_view(store: InventoryStore) -> Dict[str, Any]:
    """按品类（template）聚合仓储事实：批次 + 数量 + 实例 + 库位分布."""
    templates = {
        row["template_id"]: row
        for row in store.query_all("SELECT * FROM inventory_resource_template")
    }
    lots = store.query_all(
        "SELECT * FROM inventory_lot ORDER BY created_at ASC, rowid ASC"
    )
    instance_counts = store.query_all(
        "SELECT template_id, status, COUNT(*) AS n FROM material_instance "
        "GROUP BY template_id, status"
    )

    categories: Dict[str, Dict[str, Any]] = {}

    def _category(template_id: str) -> Dict[str, Any]:
        if template_id not in categories:
            tpl = templates.get(template_id)
            spec = _parse_spec(tpl["spec_json"]) if tpl else {}
            categories[template_id] = {
                "template_id": template_id,
                "name": (tpl or {}).get("name") or template_id,
                "category": (tpl or {}).get("category", ""),
                "properties": spec,
                "storage_class": str(spec.get("storage_class", "")),
                "hazard_class": str(spec.get("hazard_class", "")),
                "unit": "",
                "quantity_total": 0.0,
                "quantity_available": 0.0,
                "quantity_reserved": 0.0,
                "batch_count": 0,
                "quarantined_batches": 0,
                "instance_counts": {},
                "zones": {},
                "lots": [],
            }
        return categories[template_id]

    for lot in lots:
        cat = _category(lot["template_id"])
        cat["quantity_total"] += float(lot["quantity_total"])
        cat["quantity_available"] += float(lot["quantity_available"])
        cat["quantity_reserved"] += float(lot["quantity_reserved"])
        cat["batch_count"] += 1
        if lot["quarantined"]:
            cat["quarantined_batches"] += 1
        cat["unit"] = cat["unit"] or str(lot["unit"] or "")
        zone_id = str(lot["warehouse_zone_id"] or "")
        zone = cat["zones"].setdefault(
            zone_id, {"zone_id": zone_id, "quantity_available": 0.0, "batch_count": 0}
        )
        zone["quantity_available"] += float(lot["quantity_available"])
        zone["batch_count"] += 1
        cat["lots"].append({
            "lot_id": lot["lot_id"],
            "batch_no": lot["batch_no"],
            "quantity_total": lot["quantity_total"],
            "quantity_available": lot["quantity_available"],
            "quantity_reserved": lot["quantity_reserved"],
            "unit": lot["unit"],
            "expiry": lot["expiry"],
            "quarantined": lot["quarantined"],
            "warehouse_zone_id": zone_id,
            "created_at": lot["created_at"],
        })

    for row in instance_counts:
        cat = _category(row["template_id"] or "")
        cat["instance_counts"][row["status"]] = int(row["n"])

    # 纯模板（未入库也无实例）也展示，便于品类目录管理
    for template_id in templates:
        _category(template_id)

    result = []
    for cat in categories.values():
        cat["zones"] = sorted(cat["zones"].values(), key=lambda z: z["zone_id"])
        cat["in_stock_instances"] = cat["instance_counts"].get("warehouse", 0)
        cat["active_instances"] = sum(
            n for s, n in cat["instance_counts"].items() if s in _ACTIVE_INSTANCE_STATES
        )
        result.append(cat)
    result.sort(key=lambda c: (c["category"], c["template_id"]))

    return {"categories": result, "storage_classes": STORAGE_CLASSES}


def build_zone_storage_summary(store: InventoryStore) -> Dict[str, List[Dict[str, Any]]]:
    """库位（zone_id）→ 品类汇总，供 2D 地图存储区派生渲染.

    映射规则：inventory_lot.warehouse_zone_id == lab_zone.zone_id。
    未标库位的批次归入 ""（前端提示「未分配库位」）。
    """
    rows = store.query_all(
        "SELECT l.warehouse_zone_id AS zone_id, l.template_id, "
        "       SUM(l.quantity_available) AS available, COUNT(*) AS batches, "
        "       MAX(l.unit) AS unit, t.name AS tpl_name, t.spec_json AS spec_json "
        "FROM inventory_lot l LEFT JOIN inventory_resource_template t "
        "     ON t.template_id = l.template_id "
        "WHERE l.quantity_available > 0 "
        "GROUP BY l.warehouse_zone_id, l.template_id"
    )
    summary: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        spec = _parse_spec(row.get("spec_json"))
        summary.setdefault(str(row["zone_id"] or ""), []).append({
            "template_id": row["template_id"],
            "name": row["tpl_name"] or row["template_id"],
            "quantity_available": float(row["available"]),
            "unit": str(row["unit"] or ""),
            "batch_count": int(row["batches"]),
            "storage_class": str(spec.get("storage_class", "")),
            "hazard_class": str(spec.get("hazard_class", "")),
        })
    for items in summary.values():
        items.sort(key=lambda i: str(i["template_id"]))
    return summary
