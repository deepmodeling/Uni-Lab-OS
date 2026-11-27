"""显式跨站库存适配：只迁移已核验树与内容事实，不复制远端可执行配置。"""

from __future__ import annotations

import copy
import json
import math
from typing import Any
from uuid import UUID

from unilabos.app.scheduler.inventory.backend_contract import BackendContractError
from unilabos.app.scheduler.inventory.capacity import (
    material_capacity,
    normalize_capacity,
    stricter_capacity,
    validate_material_config,
)
from unilabos.app.scheduler.inventory.content_contract import BackendContainerContentService
from unilabos.app.scheduler.inventory.handoff_content import (
    SAMPLE_HISTORY_FIELDS,
    SUBSTANCE_FIELDS,
    export_composition,
    validate_sample_history,
    validate_substance,
)
from unilabos.app.scheduler.inventory.material_io import MaterialIOError, MaterialIOService
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.app.scheduler.inventory.store import set_site_occupancy
from unilabos.app.scheduler.inventory.workflow_quantity import active_workflow_reserved_quantity, convert_quantity
from unilabos.center_protocol.digest import digest

ADAPTER = "unilab.inventory.v1"
_SAMPLE_FIELDS = ("code", "name", "quantity", "quantity_unit", "description", "meta_data")
_REAGENT_FIELDS = (
    "quantity", "quantity_unit", "concentration_value", "concentration_unit",
    "description", "meta_data", "physical_state", "density_g_per_ml", "density_source",
)
_CHEMICAL_FIELDS = ("cas", "inchi_key", "molecular_formula")


def _reject(code: str, message: str) -> None:
    from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError

    raise HandoffInventoryError(code, message)



def _identity(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _revision(value: Any, *, minimum: int = 1) -> bool:
    return type(value) is int and minimum <= value <= 2**53 - 1


def _finite_number(value: Any) -> bool:
    # 先限整数范围，避免 math.isfinite 对任意大整数抛 OverflowError。
    return (type(value) is int and abs(value) <= 2**53 - 1
            or type(value) is float and math.isfinite(value))

def _content(c, material: str, station_uuid: str) -> dict[str, Any] | None:
    row = c.execute(
        "SELECT * FROM container_content WHERE material_uuid=? AND deleted_at IS NULL", (material,)
    ).fetchone()
    if row is None:
        return None
    row = dict(row)
    if row["kind"] == "substance":
        result = {
            "kind": "substance", "source_content_uuid": row["uuid"], "source_revision": row["revision"],
            "values": {key: row[key] for key in SUBSTANCE_FIELDS},
            "components": export_composition(c, row["composition"], station_uuid),
        }
        result["values"]["meta_data"] = json.loads(row["meta_data"])
        validate_substance(result)
        return result
    if row["kind"] not in {"sample", "reagent"}:
        _reject("content_adapter_required", "尚未定义该内容类型的跨站映射")
    fields = _SAMPLE_FIELDS if row["kind"] == "sample" else _REAGENT_FIELDS
    result = {"kind": row["kind"], "source_content_uuid": row["uuid"],
              "source_revision": row["revision"], "values": {key: row[key] for key in fields}}
    result["values"]["meta_data"] = json.loads(row["meta_data"])
    if row["kind"] == "sample" and any(row[key] is not None for key in SAMPLE_HISTORY_FIELDS):
        result["historical_fields"] = {key: row[key] for key in SAMPLE_HISTORY_FIELDS}
        validate_sample_history(result["historical_fields"])
    if row["kind"] == "reagent":
        info = c.execute(
            "SELECT * FROM reagent_info WHERE uuid=? AND deleted_at IS NULL", (row["reagent_info_uuid"],)
        ).fetchone()
        if info is None:
            _reject("chemical_identity_missing", "来源试剂目录身份不存在")
        result.update(source_reagent_info_uuid=info["uuid"], chemical_identity={key: info[key] for key in _CHEMICAL_FIELDS})
    return result


def export_snapshot(authority, c, carrier: str, refs: list[dict[str, Any]], *, template_evidence: bool = False) -> dict[str, Any]:
    """调用方事务内完整读取；未登记谱系、损坏结构和不支持内容均拒绝。"""
    identities = authority._subtree(c, carrier)
    by_id = {ref["local_material_uuid"]: ref for ref in refs}
    if len(by_id) != len(refs) or set(by_id) != identities:
        _reject("lineage_plan_mismatch", "复杂适配要求载体和所有子物料各有唯一谱系")
    for identity in identities:
        row, ref = authority._material(c, identity), by_id[identity]
        if ref["station_uuid"] != authority.station_uuid or ref["local_revision"] != row["aggregate_version"]:
            _reject("source_revision_mismatch", "来源物料引用不是本站当前修订")
    return inventory_snapshot(authority, c, carrier, template_evidence=template_evidence)


def inventory_snapshot(authority, c, carrier: str, *, template_evidence: bool = True) -> dict[str, Any]:
    """读取完整实际树；尚无谱系的首次来源也能冻结物理事实，不制造引用。"""
    identities = authority._subtree(c, carrier)
    materials = []
    for identity in sorted(identities):
        row = authority._material(c, identity)
        sites = [dict(s) for s in c.execute(
            "SELECT name,occupied_material_uuid FROM site WHERE material_uuid=? AND deleted_at IS NULL ORDER BY name",
            (identity,),
        ).fetchall()]
        for site in sites:
            occupant = site["occupied_material_uuid"]
            if occupant and (occupant not in identities or authority._material(c, occupant)["parent_uuid"] != identity):
                _reject("source_tree_conflict", "实际库位占用与父子树不一致")
        placements = c.execute(
            "SELECT material_uuid FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL", (identity,)
        ).fetchall()
        if identity != carrier and any(s[0] != row["parent_uuid"] for s in placements):
            _reject("source_tree_conflict", "子物料被树外库位占用")
        materials.append({
            "local_material_uuid": identity, "local_revision": row["aggregate_version"],
            "name": row["name"],
            "parent_material_uuid": None if identity == carrier else row["parent_uuid"], "barcode": row["barcode"],
            "sites": sites, "content": _content(c, identity, authority.station_uuid),
            "capacity": material_capacity(c, identity)["capacity"],
        })
        if template_evidence:
            materials[-1]["resource_template_uuid"] = row["resource_template_uuid"]
    return {"adapter": ADAPTER, "carrier_material_uuid": carrier, "materials": materials}


def export_matching_snapshot(authority, c, carrier: str, refs: list[dict[str, Any]], original: dict[str, Any]) -> dict[str, Any]:
    """按原证明的版本取当前事实；旧证明不会被新字段改写摘要。"""
    return export_snapshot(authority, c, carrier, refs,
                           template_evidence=any("resource_template_uuid" in node for node in original["materials"]))


def validate_source(authority, c, item, carrier: str) -> bool:
    snapshot = item["source_snapshot"]
    if not isinstance(snapshot, dict) or snapshot.get("adapter") != ADAPTER:
        return False
    current = export_matching_snapshot(authority, c, carrier, item["source_material_refs"], snapshot)
    if digest(current) != digest(snapshot):
        _reject("source_snapshot_changed", "来源结构、容量或内容已变化，必须重新建立交接快照")
    for material in current["materials"]:
        content = material["content"]
        if content and content["kind"] in {"reagent", "substance"} and active_workflow_reserved_quantity(
            c, inventory_type="current_substance" if content["kind"] == "substance" else "reagent",
            inventory_uuid=content["source_content_uuid"]
        ):
            _reject("task_reserved", "试剂仍有工作流数量预留，不能转移")
    return True


def validate_snapshot(snapshot: Any, refs, plan):
    """中心摘要只保证传输完整；接收端还须验证全部结构和映射覆盖。"""
    if not isinstance(snapshot, dict) or set(snapshot) != {"adapter", "carrier_material_uuid", "materials"} or snapshot["adapter"] != ADAPTER:
        _reject("invalid_adapter_snapshot", "复杂适配需要完整的版本化来源快照")
    adapter = plan["adapter"]
    if not isinstance(adapter, dict) or set(adapter) != {"kind", "site_names", "reagent_info_uuids"} or adapter["kind"] != ADAPTER:
        _reject("invalid_local_plan", "适配配置必须提供库位名称与本站试剂目录映射")
    materials = snapshot["materials"]
    if not isinstance(materials, list) or not materials or len(materials) > 1024:
        _reject("invalid_adapter_snapshot", "来源物料树为空或过大")
    by_id = {}
    for node in materials:
        fields = {"local_material_uuid", "local_revision", "parent_material_uuid", "barcode", "sites", "content", "capacity"}
        if not isinstance(node, dict) or set(node) - {"resource_template_uuid", "name"} != fields:
            _reject("invalid_adapter_snapshot", "来源物料快照字段不完整")
        if "name" in node and (not isinstance(node["name"], str) or not node["name"].strip()):
            _reject("invalid_adapter_snapshot", "来源物料名称必须为非空字符串")
        if "resource_template_uuid" in node and not _identity(node["resource_template_uuid"]):
            _reject("invalid_adapter_snapshot", "来源物料模板不是规范身份")
        identity = node["local_material_uuid"]
        if not _identity(identity) or identity in by_id or not _revision(node["local_revision"]):
            _reject("invalid_adapter_snapshot", "来源物料身份重复或无效")
        if not isinstance(node["barcode"], str) or node["parent_material_uuid"] is not None and not _identity(node["parent_material_uuid"]):
            _reject("invalid_adapter_snapshot", "来源条码或父物料身份无效")
        by_id[identity] = node
    by_ref = {ref["local_material_uuid"]: ref for ref in refs}
    if set(by_id) != set(by_ref) or len(by_ref) != len(refs):
        _reject("lineage_plan_mismatch", "快照必须精确覆盖所有来源引用")
    root = snapshot["carrier_material_uuid"]
    if not _identity(root) or root not in by_ref or by_ref[root]["material_lineage_uuid"] != plan["carrier_lineage_uuid"]:
        _reject("lineage_plan_mismatch", "适配载体必须具有显式载体谱系")
    if not isinstance(adapter["site_names"], dict) or set(adapter["site_names"]) != set(by_id):
        _reject("site_plan_mismatch", "库位映射必须覆盖每个物料，包括无库位物料")
    chemicals = set()
    occupied = set()
    for identity, node in by_id.items():
        if node["local_revision"] != by_ref[identity]["local_revision"]:
            _reject("source_revision_mismatch", "快照修订与引用不一致")
        path, cursor = set(), identity
        while cursor != root:
            if cursor in path or cursor not in by_id:
                _reject("source_tree_conflict", "来源树包含环或树外父物料")
            path.add(cursor)
            cursor = by_id[cursor]["parent_material_uuid"]
        if by_id[root]["parent_material_uuid"] is not None:
            _reject("source_tree_conflict", "根载体不能声明内部父物料")
        sites, mapping = node["sites"], adapter["site_names"][identity]
        if not isinstance(sites, list) or not isinstance(mapping, dict):
            _reject("site_plan_mismatch", "库位与映射必须为显式列表和对象")
        names = []
        for site in sites:
            if not isinstance(site, dict) or set(site) != {"name", "occupied_material_uuid"} or not isinstance(site["name"], str):
                _reject("site_plan_mismatch", "来源库位字段无效")
            names.append(site["name"])
            occupant = site["occupied_material_uuid"]
            if occupant is not None:
                if not _identity(occupant):
                    _reject("site_plan_mismatch", "库位占用身份必须为规范 UUID 或 null")
                if occupant in occupied or occupant not in by_id or by_id[occupant]["parent_material_uuid"] != identity:
                    _reject("source_tree_conflict", "占位重复或与父子树不一致")
                occupied.add(occupant)
        if len(set(names)) != len(names) or set(names) != set(mapping) or any(not isinstance(v, str) or not v for v in mapping.values()) or len({v.casefold() for v in mapping.values()}) != len(mapping):
            _reject("site_plan_mismatch", "库位映射必须一对一完整覆盖，不允许别名折叠")
        content = node["content"]
        if content is not None:
            if not isinstance(content, dict) or not isinstance(content.get("kind"), str) or content["kind"] not in {"sample", "reagent", "substance"}:
                _reject("content_adapter_required", "只支持已显式定义的样品、试剂和混合物适配")
            if content["kind"] == "substance":
                chemicals.update(validate_substance(content))
                continue
            fields = {"kind", "source_content_uuid", "source_revision", "values"}
            if content["kind"] == "sample" and "historical_fields" in content:
                fields.add("historical_fields")
                validate_sample_history(content["historical_fields"])
            if content["kind"] == "reagent":
                fields |= {"source_reagent_info_uuid", "chemical_identity"}
                if not _identity(content.get("source_reagent_info_uuid")):
                    _reject("invalid_adapter_snapshot", "来源试剂目录身份必须为规范 UUID")
                chemicals.add(content["source_reagent_info_uuid"])
            expected_values = _SAMPLE_FIELDS if content["kind"] == "sample" else _REAGENT_FIELDS
            if set(content) != fields or not isinstance(content["values"], dict) or set(content["values"]) != set(expected_values):
                _reject("invalid_adapter_snapshot", "内容事实字段不完整或包含未知字段")
            revision = content["source_revision"]
            # 现有 sample 公共存储没有内容修订列值；保持 null 原事实，通过完整快照比较防变更。
            unversioned_sample = content["kind"] == "sample" and revision is None
            if not _identity(content["source_content_uuid"]) or not (unversioned_sample or _revision(revision, minimum=0)):
                _reject("invalid_adapter_snapshot", "来源内容身份或修订无效")
            values = content["values"]
            if values["description"] is not None and not isinstance(values["description"], str):
                _reject("invalid_adapter_snapshot", "内容描述必须为字符串或 null")
            if content["kind"] == "sample" and any(not isinstance(values[key], str) or not values[key].strip() for key in ("code", "name")):
                _reject("invalid_adapter_snapshot", "样品名称与编码必须为非空字符串")
            quantity = values["quantity"]
            if not _finite_number(quantity) or quantity < 0 or not isinstance(values["quantity_unit"], str) or not values["quantity_unit"].strip() or not isinstance(values["meta_data"], dict):
                _reject("invalid_adapter_snapshot", "内容数量、单位或元数据无效")
            if content["kind"] == "reagent":
                density = values["density_g_per_ml"]
                if not isinstance(values["physical_state"], str) or values["physical_state"] not in {"solid", "liquid", "gas", "other", "unknown"} or density is not None and (not _finite_number(density) or density <= 0):
                    _reject("invalid_adapter_snapshot", "试剂物态或密度无效")
                concentration = values["concentration_value"]
                if (concentration is not None and (not _finite_number(concentration) or concentration < 0)
                        or any(values[key] is not None and not isinstance(values[key], str)
                               for key in ("concentration_unit", "density_source"))):
                    _reject("invalid_adapter_snapshot", "试剂浓度或来源字段无效")
                chemistry = content["chemical_identity"]
                if (not isinstance(chemistry, dict) or set(chemistry) != set(_CHEMICAL_FIELDS)
                        or any(value is not None and not isinstance(value, str) for value in chemistry.values())):
                    _reject("chemical_identity_mismatch", "来源化学身份字段无效")
    if not isinstance(adapter["reagent_info_uuids"], dict) or set(adapter["reagent_info_uuids"]) != chemicals:
        _reject("chemical_identity_mismatch", "本站试剂身份映射必须精确覆盖源目录身份")
    if any(not _identity(value) for value in adapter["reagent_info_uuids"].values()):
        _reject("chemical_identity_mismatch", "本站试剂身份映射必须为规范 UUID")
    return by_id, by_ref


def prepare_structure(authority, c, item, plan, local_by_source: dict[str, str]) -> None:
    """以 handoff 领域错误返回可恢复的配置/容量拒绝，保持 runtime 通知待处理。"""
    try:
        _prepare_structure(authority, c, item, plan, local_by_source)
    except (BackendContractError, MaterialIOError) as error:
        _reject("adapter_rejected", str(error))


def _prepare_structure(authority, c, item, plan, local_by_source: dict[str, str]) -> None:
    """新建或复用载体均以同一版本化来源快照恢复实际结构和内容。"""
    nodes, _ = validate_snapshot(item["source_snapshot"], item["source_material_refs"], plan)
    adapter = plan["adapter"]
    for source, node in nodes.items():
        local = local_by_source[source]
        if not isinstance(node["barcode"], str):
            _reject("invalid_adapter_snapshot", "来源条码必须是字符串")
        if authority._material(c, local)["barcode"] != node["barcode"]:
            if node["local_material_uuid"] == item["source_snapshot"]["carrier_material_uuid"]:
                _reject("barcode_mismatch", "来源载体条码与中心容器详情不一致")
            c.execute("UPDATE material SET barcode=? WHERE uuid=?", (node["barcode"], local))
        parent = node["parent_material_uuid"]
        c.execute("UPDATE material SET parent_uuid=? WHERE uuid=?", (local_by_source.get(parent), local))
        row = authority._material(c, local)
        template = dict(c.execute("SELECT * FROM resource_template WHERE uuid=? AND deleted_at IS NULL", (row["resource_template_uuid"],)).fetchone())
        definitions = json.loads(template["available_sites"] or "[]")
        labels = {d["label"] for d in definitions}
        names = adapter["site_names"][source]
        if set(names.values()) != labels:
            _reject("site_plan_mismatch", "本站模板库位必须与映射完整一致")
        existing = c.execute("SELECT name,occupied_material_uuid FROM site WHERE material_uuid=? AND deleted_at IS NULL", (local,)).fetchall()
        if existing:
            if {r["name"] for r in existing} != labels or any(r["occupied_material_uuid"] for r in existing):
                _reject("site_plan_mismatch", "复用载体库位结构或占用与本地模板不一致")
        else:
            # 实际交接不执行模板工厂默认子物料；所有内容由来源谱系逐个显式创建。
            clean = copy.deepcopy(definitions)
            for definition in clean:
                definition["initial_material_template_uuid"] = None
                definition.get("meta_data", {}).get("unilab", {}).pop("initial_material_template", None)
            MaterialIOService(authority.store).materialize_template_contents(
                c, material_uuid=local, template={"available_sites": clean}, material_name=row["name"], now=row["create_time"]
            )
        # 返站复用的是本站库存身份，保留本站几何/标签等配置及更严格的容量限制。
        config = json.loads(row["config"] or "{}")
        config["capacity"] = stricter_capacity(
            normalize_capacity(config.get("capacity")), normalize_capacity(node["capacity"])
        )
        validate_material_config(c, local, config)
        c.execute("UPDATE material SET config=? WHERE uuid=?", (json.dumps(config, allow_nan=False), local))
    for source, node in nodes.items():
        local = local_by_source[source]
        for site in node["sites"]:
            occupant = site["occupied_material_uuid"]
            if occupant is None:
                continue
            target_site = c.execute("SELECT * FROM site WHERE material_uuid=? AND name=? AND deleted_at IS NULL", (local, adapter["site_names"][source][site["name"]])).fetchone()
            child = authority._material(c, local_by_source[occupant])
            authority._validate_site_template(c, target_site["uuid"], child["resource_template_uuid"])
            set_site_occupancy(c, site_uuid=target_site["uuid"], material_uuid=child["uuid"], update_time=child["update_time"])
        content = node["content"]
        if content is None:
            continue
        values = copy.deepcopy(content["values"])
        values["material_uuid"] = local
        if content["kind"] in {"sample", "substance"}:
            capacity = material_capacity(c, local)["capacity"]
            applicable = False
            for key, unit in (("max_volume_ul", "µL"), ("max_mass_g", "g"), ("max_count", "粒")):
                if key not in capacity:
                    continue
                converted = convert_quantity(values["quantity"], values["quantity_unit"], unit,
                    inventory_type="sample", physical_state=None, concentration_value=None, density_g_per_ml=None)
                if converted is not None:
                    applicable = True
                    if converted[0] > capacity[key] + max(1e-9, capacity[key] * 1e-12):
                        _reject("sample_capacity_exceeded", "样品数量超过目标容器已登记容量")
            if not applicable:
                _reject("sample_capacity_missing", "样品需要已登记的同维度容量；不能猜测物态或进行质量体积换算")
            service = BackendContainerContentService(authority.store, edge_id=authority.station_uuid, lab_id=authority.lab_uuid)
            if content["kind"] == "sample":
                service.create_sample_in_transaction(c, values, historical_fields=content.get("historical_fields"))
            else:
                composition = copy.deepcopy(content["components"])
                for component in composition:
                    component["reagent_info_uuid"] = adapter["reagent_info_uuids"][component["reagent_info_uuid"]]
                service.create_current_substance_in_transaction(c, values, historical_composition=composition)
        else:
            info_uuid = adapter["reagent_info_uuids"][content["source_reagent_info_uuid"]]
            info = c.execute("SELECT * FROM reagent_info WHERE uuid=? AND deleted_at IS NULL", (info_uuid,)).fetchone()
            chemistry = content["chemical_identity"]
            if not isinstance(chemistry, dict) or set(chemistry) != set(_CHEMICAL_FIELDS) or info is None or any(info[key] != chemistry[key] for key in _CHEMICAL_FIELDS):
                _reject("chemical_identity_mismatch", "本地试剂目录与来源化学身份不一致")
            if not chemistry["cas"] and not chemistry["inchi_key"]:
                _reject("chemical_identity_mismatch", "跨站试剂身份需要可核验 CAS 或 InChIKey")
            values["reagent_info_uuid"] = info_uuid
            BackendReagentService(authority.store, edge_id=authority.station_uuid, lab_id=authority.lab_uuid).create_reagent_in_transaction(
                c, values, record_history=False, source_snapshot=content["values"], restore_empty=True
            )
