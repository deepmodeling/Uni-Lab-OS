"""实验室布局（V1 ``/api/v1/lab/*``）：档案、分区、在台摆放、仓储视图与组合树。

- 档案（名称 + 领域包）与分区 / 摆放存 materials.db（``lab_meta`` / ``lab_zone`` / ``lab_placement``），
  摆放的主体是物料权威里的物料（或设备 id），不另建库存；
- 存储区内容不靠手工摆放，按批次的 ``warehouse_zone_id`` 从 ``inventory_lot`` 派生；
- dev 原来的带修订网格文档（区域 / 围墙像素格，runtime.db ``lab_layout``）并入档案的 ``grid``
  字段：``GET /lab/profile`` 返回、``PUT /lab/profile`` 带 ``grid`` 写入（仍是 revision 乐观锁）。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from uuid import NAMESPACE_URL, uuid5

from unilabos.server.api.v1_fields import ms_to_iso
from unilabos.server.services.materials.core import (
    MaterialConflictError,
    MaterialNotFoundError,
    MaterialsService,
    MaterialValidationError,
)

MAX_ASSEMBLY_DEPTH = 6
META_LAB_NAME = "lab_name"
META_LAB_DOMAIN = "lab_domain"

# zone.kind 的通用取值（前端着色 / 图例依据）
ZONE_KINDS = [
    {"kind": "bench", "name": "实验台", "color": "#3b82f6"},
    {"kind": "instrument", "name": "仪器区", "color": "#8b5cf6"},
    {"kind": "storage", "name": "存储区", "color": "#f59e0b"},
    {"kind": "safety", "name": "安全设施", "color": "#ef4444"},
    {"kind": "prep", "name": "预处理区", "color": "#10b981"},
    {"kind": "waste", "name": "废弃物区", "color": "#71717a"},
]

STORAGE_CLASSES = [
    {"id": "ambient", "name": "常温", "color": "#3b82f6"},
    {"id": "cold", "name": "冷藏 2-8°C", "color": "#0ea5e9"},
    {"id": "frozen", "name": "冷冻 -20°C", "color": "#6366f1"},
    {"id": "flammable_cabinet", "name": "防爆柜", "color": "#ef4444"},
    {"id": "desiccator", "name": "干燥器", "color": "#f59e0b"},
]


def _pack(domain: str, name: str, accent: str, description: str, categories: list, hazards: list, zones: list) -> dict:
    return {
        "domain": domain, "name": name, "accent": accent, "description": description,
        "material_categories": [{"id": item[0], "name": item[1], "color": item[2]} for item in categories],
        "hazard_classes": [{"id": item[0], "name": item[1], "color": item[2]} for item in hazards],
        "default_zone_kinds": zones,
    }


# 领域包：同一套仓储 / 调度 / 布局内核，按学科描述物料类别、危险分级与默认分区（与 V1 一致）
DOMAIN_PACKS: dict[str, dict[str, Any]] = {
    "general": _pack(
        "general", "通用实验室", "#2563eb", "标准实验室操作系统基座：仓储 / 调度 / 布局通用内核。",
        [("reagent", "试剂", "#3b82f6"), ("consumable", "耗材", "#10b981"), ("labware", "器皿", "#8b5cf6"),
         ("sample", "样品", "#f59e0b")],
        [("none", "无危害", "#22c55e"), ("flammable", "易燃", "#ef4444"), ("corrosive", "腐蚀", "#f59e0b"),
         ("toxic", "有毒", "#8b5cf6")],
        ["bench", "instrument", "storage", "safety"],
    ),
    "organic": _pack(
        "organic", "有机化学实验室", "#f97316", "合成 / 分离纯化 / 表征流程：反应釜、旋蒸、柱层析、GC-MS。",
        [("solvent", "溶剂", "#3b82f6"), ("reagent", "试剂", "#f97316"), ("catalyst", "催化剂", "#8b5cf6"),
         ("substrate", "底物", "#10b981"), ("labware", "玻璃器皿", "#71717a")],
        [("flammable", "易燃", "#ef4444"), ("peroxide", "易过氧化", "#f59e0b"), ("toxic", "有毒", "#8b5cf6"),
         ("corrosive", "腐蚀", "#eab308")],
        ["bench", "instrument", "storage", "safety", "waste"],
    ),
    "inorganic": _pack(
        "inorganic", "无机化学实验室", "#0ea5e9", "无机合成 / 高温处理 / 晶体生长：马弗炉、水热釜、手套箱。",
        [("salt", "无机盐", "#0ea5e9"), ("acid_base", "酸碱", "#ef4444"), ("metal", "金属/氧化物", "#71717a"),
         ("crucible", "坩埚/耐高温器皿", "#f59e0b")],
        [("corrosive", "强腐蚀", "#ef4444"), ("oxidizer", "氧化剂", "#f59e0b"), ("high_temp", "高温", "#f97316")],
        ["bench", "instrument", "storage", "safety"],
    ),
    "bio": _pack(
        "bio", "生物实验室", "#22c55e", "细胞培养 / 分子生物学 / 高通量：生物安全柜、培养箱、移液工作站。",
        [("cell_line", "细胞系", "#22c55e"), ("medium", "培养基", "#f59e0b"), ("enzyme", "酶/抗体", "#8b5cf6"),
         ("plate", "板/管耗材", "#3b82f6"), ("primer", "引物/核酸", "#ec4899")],
        [("bsl1", "BSL-1", "#22c55e"), ("bsl2", "BSL-2", "#f59e0b"), ("cold_chain", "冷链", "#0ea5e9")],
        ["bench", "instrument", "storage", "safety", "prep"],
    ),
    "materials": _pack(
        "materials", "材料实验室", "#8b5cf6", "薄膜 / 粉体 / 器件制备与表征：镀膜机、球磨机、XRD、SEM。",
        [("powder", "粉体", "#8b5cf6"), ("target", "靶材", "#71717a"), ("substrate", "基片", "#3b82f6"),
         ("precursor", "前驱体", "#f59e0b")],
        [("nano", "纳米粉尘", "#f59e0b"), ("flammable", "易燃", "#ef4444"), ("inert_gas", "惰性气氛", "#0ea5e9")],
        ["bench", "instrument", "storage", "prep"],
    ),
}


def get_domain_pack(domain: str) -> dict[str, Any]:
    return DOMAIN_PACKS.get(domain, DOMAIN_PACKS["general"])


def _json(value: Any, fallback: Any) -> Any:
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return fallback
    return fallback if result is None else result


class LabLayout:
    """V1 实验室布局；``grid`` 为 dev 网格文档服务（runtime.db），可缺省。"""

    def __init__(self, materials: MaterialsService, *, grid: Any = None) -> None:
        self.materials = materials
        self.grid = grid

    @property
    def connection(self) -> Any:
        return self.materials.connection

    # ── 档案 ──────────────────────────────────────────────────

    def _meta(self, key: str, default: Any) -> Any:
        row = self.connection.execute("SELECT meta_value_json FROM lab_meta WHERE meta_key=?", (key,)).fetchone()
        return _json(row[0], default) if row else default

    def _set_meta(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT INTO lab_meta(meta_key, meta_value_json, updated_at_ms) VALUES (?,?,?) "
            "ON CONFLICT(meta_key) DO UPDATE SET meta_value_json=excluded.meta_value_json, updated_at_ms=excluded.updated_at_ms",
            (key, json.dumps(value, ensure_ascii=False), self.materials._now_ms()),
        )

    def _grid(self) -> Optional[dict[str, Any]]:
        if self.grid is None:
            return None
        layout = self.grid.get_layout().model_dump(mode="json")
        return {**{key: value for key, value in layout.items() if not key.endswith("_at_ms")},
                "create_time": ms_to_iso(layout["created_at_ms"]), "update_time": ms_to_iso(layout["updated_at_ms"])}

    def get_profile(self) -> dict[str, Any]:
        domain = str(self._meta(META_LAB_DOMAIN, "general"))
        return {
            "name": str(self._meta(META_LAB_NAME, "Uni-Lab 实验室")),
            "domain": domain,
            "pack": get_domain_pack(domain),
            "domains": list(DOMAIN_PACKS.values()),
            "zone_kinds": ZONE_KINDS,
            "grid": self._grid(),
        }

    def update_profile(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """``name`` / ``domain`` 空值保持原值；``grid`` 为对象时按 revision 乐观锁整份替换，为 null 时重置。"""

        with self.materials.write():
            name, domain = values.get("name"), values.get("domain")
            if isinstance(name, str) and name.strip():
                self._set_meta(META_LAB_NAME, name.strip())
            if isinstance(domain, str) and domain.strip():
                self._set_meta(META_LAB_DOMAIN, domain.strip())
        if "grid" in values and self.grid is not None:
            from unilabos.protocol.runtime.lab import LabLayoutWrite

            if values["grid"] is None:
                self.grid.reset_layout()
            else:
                from unilabos.server.services.runtime.lab import LabLayoutConflict

                try:
                    write = LabLayoutWrite.model_validate(values["grid"])
                except ValueError as exc:
                    raise MaterialValidationError(f"grid 不合法：{exc}") from None
                try:
                    self.grid.save_layout(write)
                except LabLayoutConflict as exc:
                    raise MaterialConflictError(
                        f"布局已被他人修改（当前 revision {exc.current}，提交的是 {exc.expected}），请刷新后重试",
                        conflict="state",
                    ) from None
        return self.get_profile()

    # ── 分区 / 摆放 ───────────────────────────────────────────

    @staticmethod
    def _zone(row: Any) -> dict[str, Any]:
        return {"zone_id": row["zone_id"], "name": row["name"], "kind": row["kind"], "x": row["x"], "y": row["y"],
                "w": row["w"], "h": row["h"], "meta_json": row["meta_json"], "meta": _json(row["meta_json"], {}),
                "version": row["version"]}

    @staticmethod
    def _placement(row: Any) -> dict[str, Any]:
        return {"subject_id": row["subject_id"], "subject_kind": row["subject_kind"], "zone_id": row["zone_id"],
                "x": row["x"], "y": row["y"], "w": row["w"], "h": row["h"], "rotation": row["rotation"],
                "label": row["label"], "meta_json": row["meta_json"], "meta": _json(row["meta_json"], {}),
                "version": row["version"]}

    def upsert_zone(self, zone: Mapping[str, Any]) -> dict[str, Any]:
        zone_id = str(zone.get("zone_id") or "").strip()
        if not zone_id:
            raise MaterialValidationError("zone_id required")
        with self.materials.write():
            self.connection.execute(
                "INSERT INTO lab_zone(zone_id, name, kind, x, y, w, h, meta_json, version, updated_at_ms) "
                "VALUES (?,?,?,?,?,?,?,?,1,?) ON CONFLICT(zone_id) DO UPDATE SET name=excluded.name, kind=excluded.kind, "
                "x=excluded.x, y=excluded.y, w=excluded.w, h=excluded.h, meta_json=excluded.meta_json, "
                "version=lab_zone.version+1, updated_at_ms=excluded.updated_at_ms",
                (zone_id, str(zone.get("name") or zone_id), str(zone.get("kind") or "bench"), float(zone.get("x") or 0),
                 float(zone.get("y") or 0), float(zone.get("w") or 100), float(zone.get("h") or 100),
                 json.dumps(dict(zone.get("meta") or {}), ensure_ascii=False), self.materials._now_ms()),
            )
        return self._zone(self.connection.execute("SELECT * FROM lab_zone WHERE zone_id=?", (zone_id,)).fetchone())

    def delete_zone(self, zone_id: str) -> dict[str, Any]:
        with self.materials.write():
            self.connection.execute("DELETE FROM lab_zone WHERE zone_id=?", (zone_id,))
            self.connection.execute("UPDATE lab_placement SET zone_id='' WHERE zone_id=?", (zone_id,))
        return {"zone_id": zone_id, "deleted": True}

    def upsert_placement(self, placement: Mapping[str, Any]) -> dict[str, Any]:
        subject_id = str(placement.get("subject_id") or "").strip()
        if not subject_id:
            raise MaterialValidationError("subject_id required")
        with self.materials.write():
            self.connection.execute(
                "INSERT INTO lab_placement(subject_id, subject_kind, zone_id, x, y, w, h, rotation, label, meta_json, "
                "version, updated_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(subject_id) DO UPDATE SET "
                "subject_kind=excluded.subject_kind, zone_id=excluded.zone_id, x=excluded.x, y=excluded.y, w=excluded.w, "
                "h=excluded.h, rotation=excluded.rotation, label=excluded.label, meta_json=excluded.meta_json, "
                "version=lab_placement.version+1, updated_at_ms=excluded.updated_at_ms",
                (subject_id, str(placement.get("subject_kind") or "container"), str(placement.get("zone_id") or ""),
                 float(placement.get("x") or 0), float(placement.get("y") or 0), float(placement.get("w") or 40),
                 float(placement.get("h") or 40), float(placement.get("rotation") or 0), str(placement.get("label") or ""),
                 json.dumps(dict(placement.get("meta") or {}), ensure_ascii=False), self.materials._now_ms()),
            )
        return self._placement(
            self.connection.execute("SELECT * FROM lab_placement WHERE subject_id=?", (subject_id,)).fetchone()
        )

    def delete_placement(self, subject_id: str) -> dict[str, Any]:
        with self.materials.write():
            self.connection.execute("DELETE FROM lab_placement WHERE subject_id=?", (subject_id,))
        return {"subject_id": subject_id, "deleted": True}

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

    def get_layout(self) -> dict[str, Any]:
        """2D 俯视图：分区 + 摆放（补物料 / 模板 / 子件数）+ 存储区派生汇总 + dev 网格。"""

        zones = [self._zone(row) for row in self.connection.execute("SELECT * FROM lab_zone ORDER BY zone_id")]
        placements = []
        for row in self.connection.execute("SELECT * FROM lab_placement ORDER BY subject_id"):
            placement = self._placement(row)
            instance = self._instance(placement["subject_id"]) if placement["subject_kind"] == "container" else None
            children = self.connection.execute(
                "SELECT COUNT(*) FROM material WHERE parent_material_uuid=? AND deleted_at_ms IS NULL",
                (placement["subject_id"],),
            ).fetchone()[0]
            placements.append({**placement, "instance": instance,
                               "template": self._template(instance["template_id"]) if instance else None,
                               "children_count": int(children)})
        return {"zones": zones, "placements": placements, "storage_summary": self.zone_storage_summary(),
                "grid": self._grid()}

    def zone_storage_summary(self) -> dict[str, list[dict[str, Any]]]:
        summary: dict[str, list[dict[str, Any]]] = {}
        rows = self.connection.execute(
            "SELECT warehouse_zone_id AS zone_id, template_uuid, SUM(quantity_available) AS available, COUNT(*) AS batches, "
            "MAX(unit) AS unit FROM inventory_lot WHERE quantity_available > 0 GROUP BY warehouse_zone_id, template_uuid"
        ).fetchall()
        for row in rows:
            template = self._template(row["template_uuid"]) or {}
            spec = template.get("spec") or {}
            summary.setdefault(str(row["zone_id"] or ""), []).append({
                "template_id": row["template_uuid"], "name": template.get("name") or row["template_uuid"],
                "quantity_available": float(row["available"]), "unit": str(row["unit"] or ""),
                "batch_count": int(row["batches"]), "storage_class": str(spec.get("storage_class") or ""),
                "hazard_class": str(spec.get("hazard_class") or ""),
            })
        for items in summary.values():
            items.sort(key=lambda item: str(item["template_id"]))
        return summary

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
        placement = self.connection.execute("SELECT * FROM lab_placement WHERE subject_id=?", (subject_id,)).fetchone()
        return {"root": self._assembly_node(root, slot_id="", depth=0),
                "placement": self._placement(placement) if placement else None}

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
        """幂等写入演示布局：分区与设备摆放（物料实例与批次走物料权威的正常入口，不在此伪造）。"""

        from unilabos.protocol.materials import InventoryLotInbound, ResourceTemplateWrite
        from unilabos.server.services.materials.resource_contract import new_mutation

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
        for zone in DEMO_ZONES:
            self.upsert_zone(zone)
            created["zones"] += 1
        for placement in DEMO_PLACEMENTS:
            self.upsert_placement(placement)
            created["placements"] += 1
        return created


DEMO_ZONES = [
    {"zone_id": "zone-bench-a", "name": "实验台 A", "kind": "bench", "x": 40, "y": 60, "w": 360, "h": 200},
    {"zone_id": "zone-instr", "name": "仪器区", "kind": "instrument", "x": 440, "y": 60, "w": 260, "h": 200},
    {"zone_id": "zone-storage", "name": "常温试剂柜", "kind": "storage", "x": 40, "y": 300, "w": 200, "h": 160},
    {"zone_id": "zone-cold", "name": "冷藏柜 2-8°C", "kind": "storage", "x": 260, "y": 300, "w": 140, "h": 160},
    {"zone_id": "zone-safety", "name": "通风橱", "kind": "safety", "x": 420, "y": 300, "w": 160, "h": 160},
    {"zone_id": "zone-waste", "name": "废弃物区", "kind": "waste", "x": 600, "y": 300, "w": 120, "h": 160},
]
DEMO_PLACEMENTS = [
    {"subject_id": "liquid_handler", "subject_kind": "device", "zone_id": "zone-bench-a",
     "x": 20, "y": 30, "w": 30, "h": 140, "label": "移液工作站"},
    {"subject_id": "gcms", "subject_kind": "device", "zone_id": "zone-instr",
     "x": 30, "y": 40, "w": 90, "h": 120, "label": "GC-MS"},
]
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


__all__ = ["DOMAIN_PACKS", "LabLayout", "STORAGE_CLASSES", "ZONE_KINDS", "get_domain_pack"]
