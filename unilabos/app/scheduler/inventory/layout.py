"""实验室布局层（Lab OS 的空间维度）.

- profile：实验室名称 + 领域（domain pack），存 lab_meta
- zone：2D 俯视图分区（实验台/仪器区/存储区/安全设施…）
- placement：分区内的摆放（**在台**容器实例 / 设备），2D 坐标——
  对应云端 MaterialNode location=bench 的"出库上台"语义
- storage_summary：存储区内容**不靠手工摆放**，从 inventory_lot 的
  warehouse_zone_id（库位）按品类派生（对齐云端 MaterialWarehouse：
  仓储组织依据是物料本身的性质/品类，画布只是映射交互）
- assembly：从某个容器实例出发，沿 resource_relation 展开的组合树
  （前端 2.5D 视图数据源：deck → rack/plate → tube/well）

布局写操作走 store 事务并记 ledger（本地审计）；暂不进 sync_outbox，
云端同步布局属后续演进。
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
# zone / placement 写操作
# ---------------------------------------------------------------------------

def upsert_zone(store: InventoryStore, zone: Dict[str, Any]) -> Dict[str, Any]:
    zone_id = str(zone.get("zone_id") or "").strip()
    if not zone_id:
        raise ValueError("zone_id required")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO lab_zone(zone_id, name, kind, x, y, w, h, meta_json, version) "
            "VALUES (?,?,?,?,?,?,?,?,1) ON CONFLICT(zone_id) DO UPDATE SET "
            "name = excluded.name, kind = excluded.kind, x = excluded.x, y = excluded.y, "
            "w = excluded.w, h = excluded.h, meta_json = excluded.meta_json, "
            "version = lab_zone.version + 1",
            (
                zone_id,
                str(zone.get("name") or zone_id),
                str(zone.get("kind") or "bench"),
                float(zone.get("x") or 0),
                float(zone.get("y") or 0),
                float(zone.get("w") or 100),
                float(zone.get("h") or 100),
                json.dumps(zone.get("meta") or {}, ensure_ascii=False),
            ),
        )
        InventoryStore.tx_insert_ledger(
            conn, _now_ms(), "layout.zone_upsert", "zone", zone_id, {"zone": zone_id}
        )
    row = store.query_one("SELECT * FROM lab_zone WHERE zone_id = ?", (zone_id,))
    assert row is not None
    return row


def delete_zone(store: InventoryStore, zone_id: str) -> Dict[str, Any]:
    with store.transaction() as conn:
        conn.execute("DELETE FROM lab_zone WHERE zone_id = ?", (zone_id,))
        conn.execute("UPDATE lab_placement SET zone_id = '' WHERE zone_id = ?", (zone_id,))
        InventoryStore.tx_insert_ledger(
            conn, _now_ms(), "layout.zone_delete", "zone", zone_id, {}
        )
    return {"zone_id": zone_id, "deleted": True}


def upsert_placement(store: InventoryStore, placement: Dict[str, Any]) -> Dict[str, Any]:
    subject_id = str(placement.get("subject_id") or "").strip()
    if not subject_id:
        raise ValueError("subject_id required")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO lab_placement(subject_id, subject_kind, zone_id, x, y, w, h, "
            "rotation, label, meta_json, version) VALUES (?,?,?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(subject_id) DO UPDATE SET subject_kind = excluded.subject_kind, "
            "zone_id = excluded.zone_id, x = excluded.x, y = excluded.y, w = excluded.w, "
            "h = excluded.h, rotation = excluded.rotation, label = excluded.label, "
            "meta_json = excluded.meta_json, version = lab_placement.version + 1",
            (
                subject_id,
                str(placement.get("subject_kind") or "container"),
                str(placement.get("zone_id") or ""),
                float(placement.get("x") or 0),
                float(placement.get("y") or 0),
                float(placement.get("w") or 40),
                float(placement.get("h") or 40),
                float(placement.get("rotation") or 0),
                str(placement.get("label") or ""),
                json.dumps(placement.get("meta") or {}, ensure_ascii=False),
            ),
        )
        InventoryStore.tx_insert_ledger(
            conn, _now_ms(), "layout.placement_upsert", "placement", subject_id, {}
        )
    row = store.get_placement(subject_id)
    assert row is not None
    return row


def delete_placement(store: InventoryStore, subject_id: str) -> Dict[str, Any]:
    with store.transaction() as conn:
        conn.execute("DELETE FROM lab_placement WHERE subject_id = ?", (subject_id,))
        InventoryStore.tx_insert_ledger(
            conn, _now_ms(), "layout.placement_delete", "placement", subject_id, {}
        )
    return {"subject_id": subject_id, "deleted": True}


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
    """2D 俯视图数据：zones + placements（补实例/模板/子件数）+ 派生仓储.

    storage_summary 按 zone_id 给出该库位的品类库存（inventory_lot 派生），
    存储区渲染以它为准，不依赖手工 placement。
    """
    zones = store.list_zones()
    placements = store.list_placements()

    inst_ids = [p["subject_id"] for p in placements if p["subject_kind"] == "container"]
    instances: Dict[str, Dict[str, Any]] = {}
    child_counts: Dict[str, int] = {}
    if inst_ids:
        placeholders = ",".join("?" for _ in inst_ids)
        for row in store.query_all(
            f"SELECT * FROM material_instance WHERE edge_uuid IN ({placeholders})",
            tuple(inst_ids),
        ):
            instances[row["edge_uuid"]] = row
        for row in store.query_all(
            f"SELECT parent_uuid, COUNT(*) AS n FROM resource_relation "
            f"WHERE parent_uuid IN ({placeholders}) GROUP BY parent_uuid",
            tuple(inst_ids),
        ):
            child_counts[row["parent_uuid"]] = int(row["n"])

    templates = _template_map(
        store, sorted({i["template_id"] for i in instances.values() if i["template_id"]})
    )

    enriched = []
    for p in placements:
        inst = instances.get(p["subject_id"])
        tpl = templates.get(inst["template_id"]) if inst else None
        enriched.append({
            **p,
            "instance": inst,
            "template": tpl,
            "children_count": child_counts.get(p["subject_id"], 0),
        })

    return {
        "zones": zones,
        "placements": enriched,
        "storage_summary": build_zone_storage_summary(store),
    }


def get_assembly(store: InventoryStore, subject_id: str) -> Dict[str, Any]:
    """组合树：根实例 + 递归子件（2.5D 视图数据源）."""
    root = store.get_instance(subject_id)
    if root is None:
        raise KeyError(subject_id)

    tree = _assembly_node(store, root, slot_id="", depth=0)
    placement = store.get_placement(subject_id)
    return {"root": tree, "placement": placement}


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

DEMO_ZONES = [
    {"zone_id": "zone-bench-a", "name": "实验台 A", "kind": "bench",
     "x": 40, "y": 60, "w": 360, "h": 200},
    {"zone_id": "zone-instr", "name": "仪器区", "kind": "instrument",
     "x": 440, "y": 60, "w": 260, "h": 200},
    {"zone_id": "zone-storage", "name": "常温试剂柜", "kind": "storage",
     "x": 40, "y": 300, "w": 200, "h": 160},
    {"zone_id": "zone-cold", "name": "冷藏柜 2-8°C", "kind": "storage",
     "x": 260, "y": 300, "w": 140, "h": 160},
    {"zone_id": "zone-safety", "name": "通风橱", "kind": "safety",
     "x": 420, "y": 300, "w": 160, "h": 160},
    {"zone_id": "zone-waste", "name": "废弃物区", "kind": "waste",
     "x": 600, "y": 300, "w": 120, "h": 160},
]

# 批次 → 库位映射：仓储位置由品类的 storage_class 决定，而非手工摆放
DEMO_LOTS = [
    ("reagent-naoh", 500.0, "mL", "demo-lot-naoh", "zone-storage"),
    ("reagent-etoh", 1000.0, "mL", "demo-lot-etoh", "zone-storage"),
    ("reagent-enzyme", 50.0, "mL", "demo-lot-enzyme", "zone-cold"),
]


def seed_demo(service: InventoryService) -> Dict[str, Any]:
    """幂等写入演示布局（模板 / 组合实例 / 分区 / 摆放 / 批次）."""
    store = service.store
    created: Dict[str, Any] = {"zones": 0, "templates": 0, "instances": 0,
                               "placements": 0, "lots": 0}

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

    for zone in DEMO_ZONES:
        upsert_zone(store, zone)
        created["zones"] += 1

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

    # 摆放（仅"在台"语义）：bench 上放 deck；设备落位（subject_kind=device）。
    # 存储区不做手工摆放——其内容由批次库位派生（storage_summary）。
    for placement in [
        {"subject_id": "demo-deck-a", "subject_kind": "container", "zone_id": "zone-bench-a",
         "x": 60, "y": 50, "w": 220, "h": 110, "label": "移液工作站 deck"},
        {"subject_id": "liquid_handler", "subject_kind": "device", "zone_id": "zone-bench-a",
         "x": 20, "y": 30, "w": 30, "h": 140, "label": "移液工作站"},
        {"subject_id": "gcms", "subject_kind": "device", "zone_id": "zone-instr",
         "x": 30, "y": 40, "w": 90, "h": 120, "label": "GC-MS"},
    ]:
        upsert_placement(store, placement)
        created["placements"] += 1

    # 清理旧版种子在存储区的手工瓶摆放（迁移到派生渲染后不再需要）
    for legacy_subject in ("demo-bottle-naoh", "demo-bottle-etoh"):
        if store.get_placement(legacy_subject) is not None:
            delete_placement(store, legacy_subject)

    # 批次库存（品类 × 库位；与调度物料预留联动）
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

    @router.post("/zones")
    def post_zone(body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return upsert_zone(store, body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/zones/{zone_id}")
    def remove_zone(zone_id: str) -> Dict[str, Any]:
        return delete_zone(store, zone_id)

    @router.post("/placements")
    def post_placement(body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return upsert_placement(store, body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/placements/{subject_id}")
    def remove_placement(subject_id: str) -> Dict[str, Any]:
        return delete_placement(store, subject_id)

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
