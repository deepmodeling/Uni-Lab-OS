"""实验室视图（V1 ``/api/v1/lab/*`` 中不依赖档案 / 分区 / 摆放的部分）：仓储视图、组合树与演示种子。

- 仓储视图按品类（模板）聚合 materials.db 的批次与物料，``zones`` 按批次的 ``warehouse_zone_id`` 分组；
- 组合树是根物料 + 递归子件（含所在库位标签与内容物），数据都来自物料权威；
- 演示种子只写品类模板与批次（走物料权威的正常入口）。

实验室档案 / 分区 / 摆放（``lab_meta`` / ``lab_zone`` / ``lab_placement``）与网格布局（runtime.db
``lab_layout``）及其 ``/lab/profile``、``/lab/layout``、``/lab/zones``、``/lab/placements`` 路由按负责人
2026-11-17 的决定已删除。
"""

from __future__ import annotations

import json
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid5

from unilabos.server.api.v1_fields import ms_to_iso
from unilabos.server.services.materials.core import MaterialNotFoundError, MaterialsService

MAX_ASSEMBLY_DEPTH = 6

STORAGE_CLASSES = [
    {"id": "ambient", "name": "常温", "color": "#3b82f6"},
    {"id": "cold", "name": "冷藏 2-8°C", "color": "#0ea5e9"},
    {"id": "frozen", "name": "冷冻 -20°C", "color": "#6366f1"},
    {"id": "flammable_cabinet", "name": "防爆柜", "color": "#ef4444"},
    {"id": "desiccator", "name": "干燥器", "color": "#f59e0b"},
]


def _json(value: Any, fallback: Any) -> Any:
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return fallback
    return fallback if result is None else result


class LabViews:
    """V1 实验室视图：只读物料权威（materials.db），不另存任何布局数据。"""

    def __init__(self, materials: MaterialsService) -> None:
        self.materials = materials

    @property
    def connection(self) -> Any:
        return self.materials.connection

    # ── 视图 ──────────────────────────────────────────────────

    def _instance(self, material_uuid: str) -> Optional[dict[str, Any]]:
        row = self.connection.execute(
            "SELECT material_uuid, template_uuid, name, barcode, lifecycle_status, resource_type FROM material "
            "WHERE material_uuid=? AND deleted_at_ms IS NULL", (material_uuid,),
        ).fetchone()
        if row is None:
            return None
        return {"edge_uuid": row["material_uuid"], "template_id": row["template_uuid"], "name": row["name"],
                "barcode": row["barcode"], "status": row["lifecycle_status"], "resource_type": row["resource_type"]}

    def _template(self, template_uuid: str) -> Optional[dict[str, Any]]:
        row = self.connection.execute(
            "SELECT template_uuid, name, display_name, resource_type, category_json, definition_json FROM resource_template "
            "WHERE template_uuid=?", (template_uuid,),
        ).fetchone()
        if row is None:
            return None
        definition = _json(row["definition_json"], {})
        categories = _json(row["category_json"], [])
        return {"template_id": row["template_uuid"], "name": row["display_name"] or row["name"],
                "category": categories[0] if categories else row["resource_type"],
                "spec": {key: definition.get(key) for key in ("storage_class", "hazard_class") if key in definition}}

    def warehouse(self) -> dict[str, Any]:
        """按品类（模板）聚合批次 / 数量 / 在库实例 / 库位分布（对齐云端 MaterialWarehouse）。"""

        categories: dict[str, dict[str, Any]] = {}

        def category(template_uuid: str) -> dict[str, Any]:
            if template_uuid not in categories:
                template = self._template(template_uuid) or {}
                spec = template.get("spec") or {}
                categories[template_uuid] = {
                    "template_id": template_uuid, "name": template.get("name") or template_uuid,
                    "category": template.get("category", ""), "properties": spec,
                    "storage_class": str(spec.get("storage_class") or ""), "hazard_class": str(spec.get("hazard_class") or ""),
                    "unit": "", "quantity_total": 0.0, "quantity_available": 0.0, "quantity_reserved": 0.0,
                    "batch_count": 0, "quarantined_batches": 0, "instance_counts": {}, "zones": {}, "lots": [],
                }
            return categories[template_uuid]

        for lot in self.connection.execute("SELECT * FROM inventory_lot ORDER BY created_at_ms, lot_uuid"):
            item = category(lot["template_uuid"])
            item["quantity_total"] += float(lot["quantity_total"])
            item["quantity_available"] += float(lot["quantity_available"])
            item["quantity_reserved"] += float(lot["quantity_reserved"])
            item["batch_count"] += 1
            item["quarantined_batches"] += int(bool(lot["quarantined"]))
            item["unit"] = item["unit"] or str(lot["unit"] or "")
            zone_id = str(lot["warehouse_zone_id"] or "")
            zone = item["zones"].setdefault(zone_id, {"zone_id": zone_id, "quantity_available": 0.0, "batch_count": 0})
            zone["quantity_available"] += float(lot["quantity_available"])
            zone["batch_count"] += 1
            item["lots"].append({
                "lot_id": lot["lot_uuid"], "batch_no": lot["batch_no"], "quantity_total": lot["quantity_total"],
                "quantity_available": lot["quantity_available"], "quantity_reserved": lot["quantity_reserved"],
                "unit": lot["unit"], "expiry": ms_to_iso(lot["expiry_at_ms"]), "quarantined": bool(lot["quarantined"]),
                "warehouse_zone_id": zone_id, "location_site_uuid": lot["location_site_uuid"],
                "created_at": lot["created_at_ms"],
            })
        for row in self.connection.execute(
            "SELECT template_uuid, lifecycle_status, COUNT(*) AS n FROM material WHERE deleted_at_ms IS NULL "
            "GROUP BY template_uuid, lifecycle_status"
        ):
            category(row["template_uuid"])["instance_counts"][row["lifecycle_status"]] = int(row["n"])
        result = []
        for item in categories.values():
            item["zones"] = sorted(item["zones"].values(), key=lambda zone: zone["zone_id"])
            item["in_stock_instances"] = item["instance_counts"].get("active", 0)
            item["active_instances"] = sum(count for status, count in item["instance_counts"].items()
                                           if status in ("active", "reserved", "in_use"))
            result.append(item)
        result.sort(key=lambda item: (str(item["category"]), item["template_id"]))
        return {"categories": result, "storage_classes": STORAGE_CLASSES}

    def assembly(self, subject_id: str) -> dict[str, Any]:
        """组合树：根物料 + 递归子件（2.5D 视图数据源）。"""

        root = self._instance(subject_id)
        if root is None:
            raise MaterialNotFoundError(f"instance {subject_id} not found", subject="material")
        # V1 的 ``placement`` 是分区里的在台摆放；摆放表已删除，保留键以维持 V1 形状，恒为 null
        return {"root": self._assembly_node(root, slot_id="", depth=0), "placement": None}

    def _assembly_node(self, instance: dict[str, Any], *, slot_id: str, depth: int) -> dict[str, Any]:
        template = self._template(instance["template_id"]) or {}
        substances = [
            {"name": row["name"], "quantity": row["quantity"], "quantity_unit": row["quantity_unit"]}
            for row in self.connection.execute(
                "SELECT name, quantity, quantity_unit FROM material_substance WHERE material_uuid=? ORDER BY ordinal",
                (instance["edge_uuid"],),
            )
        ]
        node = {
            "edge_uuid": instance["edge_uuid"], "template_id": instance["template_id"],
            "template_name": template.get("name", ""), "category": template.get("category", ""),
            "spec": template.get("spec", {}), "barcode": instance["barcode"], "status": instance["status"],
            "slot_id": slot_id, "content": {"substances": substances} if substances else {}, "children": [],
        }
        if depth >= MAX_ASSEMBLY_DEPTH:
            return node
        rows = self.connection.execute(
            "SELECT m.material_uuid, s.label FROM material m LEFT JOIN site s ON s.occupied_material_uuid = m.material_uuid "
            "AND s.deleted_at_ms IS NULL WHERE m.parent_material_uuid=? AND m.deleted_at_ms IS NULL ORDER BY m.ordinal, m.material_uuid",
            (instance["edge_uuid"],),
        ).fetchall()
        for row in rows:
            child = self._instance(row["material_uuid"])
            if child is not None:
                node["children"].append(self._assembly_node(child, slot_id=row["label"] or "", depth=depth + 1))
        return node

    # ── 演示种子（幂等） ──────────────────────────────────────────

    def seed_demo(self) -> dict[str, Any]:
        """幂等写入仓储视图的演示数据：品类模板与批次（走物料权威的正常入口，不在此伪造物料实例）。"""

        from unilabos.protocol.materials import InventoryLotInbound, ResourceTemplateWrite
        from unilabos.server.services.materials.resource_contract import new_mutation

        # 计数键沿用 V1 形状；分区 / 摆放已删除，zones / placements 恒为 0
        created = {"zones": 0, "templates": 0, "instances": 0, "placements": 0, "lots": 0}
        # 品类模板（性质 = 仓储组织依据）与批次库存（库位由品类的存储条件决定）
        for name, display, category, spec in DEMO_TEMPLATES:
            if self.materials.get_template_by_name(name) is None:
                write = ResourceTemplateWrite(name=name, display_name=display, resource_type=category,
                                              category=[category], definition=spec)
                self.materials.put_template(new_mutation("put_template", payload=write.model_dump(mode="json")), write)
                created["templates"] += 1
        for name, quantity, unit, lot_name, zone_id in DEMO_LOTS:
            lot_uuid = str(uuid5(DEMO_NAMESPACE, lot_name))
            if self.materials.get_lot(lot_uuid) is None:
                template = self.materials.get_template_by_name(name)
                value = InventoryLotInbound(lot_uuid=lot_uuid, template_uuid=template.template_uuid, batch_no="DEMO",
                                            unit=unit, quantity=quantity, warehouse_zone_id=zone_id)
                self.materials.inbound_inventory_lot(
                    new_mutation("inbound_inventory_lot", payload=value.model_dump(mode="json", exclude_none=False)), value
                )
                created["lots"] += 1
        return created


DEMO_NAMESPACE = uuid5(NAMESPACE_URL, "unilabos://lab-demo")
DEMO_TEMPLATES = [
    ("demo-reagent-naoh", "NaOH 1M 溶液", "reagent", {"storage_class": "ambient", "hazard_class": "corrosive"}),
    ("demo-reagent-etoh", "乙醇 99.5%", "reagent", {"storage_class": "flammable_cabinet", "hazard_class": "flammable"}),
    ("demo-reagent-enzyme", "蛋白酶 K 储液", "reagent", {"storage_class": "cold", "hazard_class": "none"}),
]
DEMO_LOTS = [
    ("demo-reagent-naoh", 500.0, "ml", "demo-lot-naoh", "zone-storage"),
    ("demo-reagent-etoh", 1000.0, "ml", "demo-lot-etoh", "zone-storage"),
    ("demo-reagent-enzyme", 50.0, "ml", "demo-lot-enzyme", "zone-cold"),
]


__all__ = ["LabViews", "STORAGE_CLASSES"]
