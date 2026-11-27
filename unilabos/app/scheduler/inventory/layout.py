"""实验室身份与组合树。

- profile：实验室名称 + 领域（domain pack），存 lab_meta 单行
- storage_summary：从 inventory_lot.warehouse_zone_id 按品类派生
- assembly：从某个容器实例出发，沿 resource_relation 展开的组合树
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from unilabos.app.scheduler.inventory.domains import (
    ZONE_KINDS,
    get_domain_pack,
    list_domain_packs,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.warehouse import (
    build_warehouse_view,
    build_zone_storage_summary,
)

MAX_ASSEMBLY_DEPTH = 6

META_LAB_NAME = "lab_name"
META_LAB_DOMAIN = "lab_domain"


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

def get_profile(store: InventoryStore) -> Dict[str, Any]:
    domain = store.get_meta(META_LAB_DOMAIN, "general")
    return {
        "name": store.get_meta(META_LAB_NAME, "Uni-Lab 实验室"),
        "domain": domain,
        "pack": get_domain_pack(domain),
        "domains": list_domain_packs(),
        "zone_kinds": ZONE_KINDS,
    }


def update_profile(store: InventoryStore, name: Optional[str] = None,
                   domain: Optional[str] = None) -> Dict[str, Any]:
    if name is not None and name.strip():
        store.set_meta(META_LAB_NAME, name.strip())
    if domain is not None and domain.strip():
        store.set_meta(META_LAB_DOMAIN, domain.strip())
    return get_profile(store)


# ---------------------------------------------------------------------------
# layout / assembly 查询
# ---------------------------------------------------------------------------

def _template_map(store: InventoryStore, template_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not template_ids:
        return {}
    placeholders = ",".join("?" for _ in template_ids)
    rows = store.query_all(
        f"SELECT * FROM inventory_resource_template "
        f"WHERE template_id IN ({placeholders})",
        tuple(template_ids),
    )
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        try:
            spec = json.loads(r.get("spec_json") or "{}")
        except (TypeError, ValueError):
            spec = {}
        out[r["template_id"]] = {**r, "spec": spec}
    return out


def get_layout(store: InventoryStore) -> Dict[str, Any]:
    """仓储派生视图。俯视图分区和手工摆放已删除。"""

    return {
        "zones": [],
        "placements": [],
        "storage_summary": build_zone_storage_summary(store),
    }


def get_assembly(store: InventoryStore, subject_id: str) -> Dict[str, Any]:
    """组合树：根实例 + 递归子件（2.5D 视图数据源）."""
    root = store.get_instance(subject_id)
    if root is None:
        raise KeyError(subject_id)

    tree = _assembly_node(store, root, slot_id="", depth=0)
    return {"root": tree, "placement": None}


def _assembly_node(
    store: InventoryStore, inst: Dict[str, Any], slot_id: str, depth: int
) -> Dict[str, Any]:
    templates = _template_map(store, [inst["template_id"]] if inst["template_id"] else [])
    tpl = templates.get(inst["template_id"])
    content = store.get_content(inst["edge_uuid"])
    state: Dict[str, Any] = {}
    if content:
        try:
            state = json.loads(content.get("state_json") or "{}")
        except (TypeError, ValueError):
            state = {}

    node: Dict[str, Any] = {
        "edge_uuid": inst["edge_uuid"],
        "template_id": inst["template_id"],
        "template_name": (tpl or {}).get("name", ""),
        "category": (tpl or {}).get("category", ""),
        "spec": (tpl or {}).get("spec", {}),
        "barcode": inst["barcode"],
        "status": inst["status"],
        "slot_id": slot_id,
        "content": state,
        "children": [],
    }
    if depth >= MAX_ASSEMBLY_DEPTH:
        return node

    for rel in store.children_of(inst["edge_uuid"]):
        child = store.get_instance(rel["child_uuid"])
        if child is None:
            continue
        node["children"].append(
            _assembly_node(store, child, slot_id=rel["slot_id"], depth=depth + 1)
        )
    return node


# ---------------------------------------------------------------------------
# 演示种子（幂等）：一套「移液工作站 deck + 试管架 + 96 孔板」组合
# ---------------------------------------------------------------------------

DEMO_TEMPLATES = [
    # 器材品类（storage_class 均常温）
    ("tpl-deck-2x3", "移液工作站台面", "deck",
     {"grid": {"rows": 2, "cols": 3}, "slot_size": [120, 90], "height": 8,
      "storage_class": "ambient"}),
    ("tpl-rack-4x6", "试管架 4×6", "rack",
     {"grid": {"rows": 4, "cols": 6}, "height": 40, "storage_class": "ambient"}),
    ("tpl-plate-96", "96 孔板", "plate",
     {"grid": {"rows": 8, "cols": 12}, "height": 14, "storage_class": "ambient"}),
    ("tpl-tube-15", "15mL 离心管", "tube",
     {"height": 118, "volume_ml": 15, "storage_class": "ambient"}),
    ("tpl-bottle-500", "500mL 试剂瓶", "bottle",
     {"height": 170, "volume_ml": 500, "storage_class": "ambient"}),
    # 试剂品类（性质 = 仓储组织依据，对齐云端资源模板承载品类语义）
    ("reagent-naoh", "NaOH 1M 溶液", "reagent",
     {"storage_class": "ambient", "hazard_class": "corrosive"}),
    ("reagent-etoh", "乙醇 99.5%", "reagent",
     {"storage_class": "flammable_cabinet", "hazard_class": "flammable"}),
    ("reagent-enzyme", "蛋白酶 K 储液", "reagent",
     {"storage_class": "cold", "hazard_class": "none"}),
]

# 批次 → 库位字符串：warehouse_zone_id 只是品类库位标识，不再对应分区表。
DEMO_LOTS = [
    ("reagent-naoh", 500.0, "mL", "demo-lot-naoh", "zone-storage"),
    ("reagent-etoh", 1000.0, "mL", "demo-lot-etoh", "zone-storage"),
    ("reagent-enzyme", 50.0, "mL", "demo-lot-enzyme", "zone-cold"),
]


def seed_demo(service: InventoryService) -> Dict[str, Any]:
    """幂等写入演示数据（模板 / 组合实例 / 批次）."""
    store = service.store
    created: Dict[str, Any] = {"templates": 0, "instances": 0, "lots": 0}

    with store.transaction() as conn:
        for template_id, name, category, spec in DEMO_TEMPLATES:
            encoded_spec = json.dumps(spec, ensure_ascii=False)
            current = conn.execute(
                "SELECT version FROM inventory_resource_template "
                "WHERE template_id=?",
                (template_id,),
            ).fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO inventory_resource_template("
                    "template_id,name,category,spec_json,version) VALUES (?,?,?,?,1)",
                    (template_id, name, category, encoded_spec),
                )
                created["templates"] += 1
            else:
                conn.execute(
                    "UPDATE inventory_resource_template "
                    "SET name=?,category=?,spec_json=? WHERE template_id=?",
                    (name, category, encoded_spec, template_id),
                )

    def _ensure_instance(edge_uuid: str, template_id: str, barcode: str = "",
                         parent_uuid: str = "", slot_id: str = "") -> None:
        if store.get_instance(edge_uuid) is not None:
            return
        service.register_instance(
            template_id=template_id, edge_uuid=edge_uuid, barcode=barcode,
            parent_uuid=parent_uuid, slot_id=slot_id, actor="demo-seed",
        )
        created["instances"] += 1

    # deck 组合：deck → rack ×2 + plate96；rack-1 里放几支离心管
    _ensure_instance("demo-deck-a", "tpl-deck-2x3", barcode="DECK-A")
    _ensure_instance("demo-rack-1", "tpl-rack-4x6", "RACK-1", "demo-deck-a", "A1")
    _ensure_instance("demo-plate-1", "tpl-plate-96", "PLATE-1", "demo-deck-a", "A2")
    _ensure_instance("demo-rack-2", "tpl-rack-4x6", "RACK-2", "demo-deck-a", "B1")
    for i, slot in enumerate(["A1", "A2", "B3", "C5", "D6"], start=1):
        _ensure_instance(f"demo-tube-{i}", "tpl-tube-15", f"TUBE-{i}", "demo-rack-1", slot)
    for i, slot in enumerate(["A1", "B2"], start=6):
        _ensure_instance(f"demo-tube-{i}", "tpl-tube-15", f"TUBE-{i}", "demo-rack-2", slot)

    # 试剂瓶实例（在库，一物一码；仓储归属由批次库位派生，不做手工摆放）
    _ensure_instance("demo-bottle-naoh", "tpl-bottle-500", "BTL-NAOH")
    _ensure_instance("demo-bottle-etoh", "tpl-bottle-500", "BTL-ETOH")

    # 管内容物（2.5D 填充度展示）
    for uuid, substance, volume in [
        ("demo-tube-1", "NaOH 1M", 8.0),
        ("demo-tube-2", "乙醇", 12.0),
        ("demo-tube-3", "样品 S-01", 4.5),
        ("demo-bottle-naoh", "NaOH 1M", 420.0),
        ("demo-bottle-etoh", "乙醇 99.5%", 260.0),
    ]:
        service.update_content(uuid, {"substance": substance, "volume_ml": volume},
                               actor="demo-seed")

    # 批次库存（品类 × 库位字符串；与调度物料预留联动）
    for template_id, qty, unit, lot_id, zone_id in DEMO_LOTS:
        existing_lot = store.get_lot(lot_id)
        if existing_lot is None:
            service.inbound_lot(template_id=template_id, quantity=qty, unit=unit,
                                lot_id=lot_id, batch_no="DEMO", actor="demo-seed",
                                warehouse_zone_id=zone_id)
            created["lots"] += 1
        elif not existing_lot.get("warehouse_zone_id"):
            # 旧版种子没有库位，幂等补齐（不动数量/版本语义之外的字段）
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE inventory_lot SET warehouse_zone_id = ? "
                    "WHERE lot_id = ? AND warehouse_zone_id = ''",
                    (zone_id, lot_id),
                )

    return created


# ---------------------------------------------------------------------------
# FastAPI 路由
# ---------------------------------------------------------------------------

def create_lab_router(service: InventoryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/lab", tags=["lab"])
    store = service.store

    @router.get("/profile")
    def profile() -> Dict[str, Any]:
        return get_profile(store)

    @router.put("/profile")
    def put_profile(body: Dict[str, Any]) -> Dict[str, Any]:
        return update_profile(store, name=body.get("name"), domain=body.get("domain"))

    @router.get("/layout")
    def layout() -> Dict[str, Any]:
        return get_layout(store)

    @router.get("/warehouse")
    def warehouse() -> Dict[str, Any]:
        """品类库存聚合（对齐云端 MaterialWarehouse：性质/品类为组织依据）."""
        return build_warehouse_view(store)

    @router.get("/assembly/{subject_id}")
    def assembly(subject_id: str) -> Dict[str, Any]:
        try:
            return get_assembly(store, subject_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"instance {subject_id} not found")

    @router.post("/demo")
    def demo() -> Dict[str, Any]:
        return {"seeded": seed_demo(service)}

    return router
