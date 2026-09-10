"""OS 本地库存（Inventory）到旧 HostNode 物料查询合同的兼容投影。

库存服务规范保存资源模板、物料实例、父关系、库位（Site）和运行内容；旧
HostNode 调用方仍消费扁平 ``ResourceDict`` 行。本模块集中承担两个模型之间的
只读兼容边界，微后端之外的调用方不需要了解库存表结构，也不能把兼容投影写回
库存权威（Inventory Authority）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Dict, List, Optional

from unilabos.app.scheduler.inventory.store import InventoryStore

_RESOURCE_FIELDS = {
    "id",
    "uuid",
    "name",
    "description",
    "schema",
    "model",
    "icon",
    "parent_uuid",
    "type",
    "class",
    "pose",
    "position",
    "config",
    "data",
    "extra",
    "machine_name",
    "barcode",
    "barcode_symbology",
    "liquids",
    "liquid_history",
    "unknown_counter",
}
_TRACKER_STATE_FIELDS = ("liquids", "liquid_history", "unknown_counter")


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _canonical_config_info_prototype(
    store: InventoryStore,
    template_uuid: str,
) -> Dict[str, Any]:
    """读取注册表（Registry）同步的规范 PLR 资源原型。

    参数：``store`` 是本地资源模板（ResourceTemplate）只读存储，
    ``template_uuid`` 是物料引用的稳定模板身份。返回：``config_info`` 中的单根
    ``ResourceDict`` 原型；缺失或旧格式无法识别时返回空对象。JSON/SQL 异常中，
    SQL 异常原样传播，非法 JSON 关闭为无原型并由普通资源路径处理。
    """

    row = store.query_one(
        "SELECT config_info FROM resource_template "
        "WHERE uuid=? AND deleted_at IS NULL",
        (template_uuid,),
    )
    if row is None:
        return {}
    try:
        config_info = json.loads(str(row.get("config_info") or "[]"))
    except (TypeError, ValueError):
        return {}
    if not isinstance(config_info, list) or not config_info:
        return {}
    # ``roots`` 兼容早期多包一层的 dump，但只接受第一棵树的单根资源原型。
    roots = config_info[0] if isinstance(config_info[0], list) else config_info
    if not isinstance(roots, list) or not roots or not isinstance(roots[0], dict):
        return {}
    return deepcopy(roots[0])


def _resource_spec(
    store: InventoryStore,
    template_uuid: str,
    template: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """提取物料模板对应的可恢复 PLR ``ResourceDict`` 原型。

    参数：``store`` 和 ``template_uuid`` 定位注册表同步的 ``config_info``；
    ``template`` 是旧库存视图的兼容模板。返回：只含 ``ResourceDict`` 公共字段的
    独立字典。规范原型优先，旧 ``resource/resource_dict`` 仅作未迁移数据回退。
    """

    candidate = _canonical_config_info_prototype(store, template_uuid)
    spec = _json_object((template or {}).get("spec_json", "{}"))
    nested = spec.get("resource")
    if not isinstance(nested, dict):
        nested = spec.get("resource_dict")
    if not candidate:
        candidate = nested if isinstance(nested, dict) else spec
    resource = {
        key: deepcopy(value)
        for key, value in candidate.items()
        if key in _RESOURCE_FIELDS
    }
    if "schema" not in resource and isinstance(candidate.get("resource_schema"), dict):
        resource["schema"] = deepcopy(candidate["resource_schema"])
    if "class" not in resource and isinstance(candidate.get("klass"), str):
        resource["class"] = candidate["klass"]
    return resource


def _instance_by_uuid(store: InventoryStore, value: str) -> Optional[Dict[str, Any]]:
    """Resolve both the Edge UUID and the retained legacy Cloud UUID."""

    return store.query_one(
        "SELECT * FROM material_instance "
        "WHERE edge_uuid = ? OR legacy_cloud_id = ? "
        "ORDER BY CASE WHEN edge_uuid = ? THEN 0 ELSE 1 END LIMIT 1",
        (value, value, value),
    )


def _canonical_material(
    store: InventoryStore, material_uuid: str
) -> Optional[Dict[str, Any]]:
    """Return the Backend-shaped material row hidden by the legacy view."""

    return store.query_one(
        "SELECT * FROM material WHERE uuid = ? AND deleted_at IS NULL",
        (material_uuid,),
    )


def _config_with_authoritative_sites(
    store: InventoryStore,
    material_uuid: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """把权威库位 UUID 合并进父物料的一等 ``config.sites[]`` 描述。

    参数：``store`` 是库存权威（Inventory Authority）只读接缝，
    ``material_uuid`` 是父物料身份，``config`` 是注册表原型与实例配置的合并值。
    返回：无共享引用的新配置；只为名称匹配且未软删除的直属库位写入 ``uuid``。
    异常：SQL 异常原样传播；没有结构化 ``sites[]`` 时保持普通物料配置不变。

    安全约束：模板自带的 UUID 一律先删除；动作只有在权威行与 PLR 局部名称
    共同存在时才会得到稳定身份，避免 ``unilabos_extra`` 形成第二份状态。
    """

    projected = deepcopy(config)
    raw_sites = projected.get("sites")
    if not isinstance(raw_sites, list):
        return projected
    # ``site_rows`` 仅含该父物料直接拥有且未软删除的规范库位事实。
    site_rows = store.query_all(
        "SELECT uuid,name FROM site "
        "WHERE material_uuid=? AND deleted_at IS NULL "
        "ORDER BY sort_order,create_time,uuid",
        (material_uuid,),
    )
    site_by_name = {
        str(site_row["name"]).strip().casefold(): str(site_row["uuid"])
        for site_row in site_rows
    }
    normalized_sites: list[Any] = []
    for raw_site in raw_sites:
        if not isinstance(raw_site, dict):
            normalized_sites.append(deepcopy(raw_site))
            continue
        # ``site`` 先移除任何模板 UUID，再按局部名称接入权威稳定身份。
        site = deepcopy(raw_site)
        site.pop("uuid", None)
        site_name = str(site.get("label") or site.get("name") or "").strip()
        site_uuid = site_by_name.get(site_name.casefold())
        if site_uuid is not None:
            site["uuid"] = site_uuid
        normalized_sites.append(site)
    projected["sites"] = normalized_sites
    return projected


def _canonical_pose(
    store: InventoryStore,
    material_uuid: str,
    *,
    config: Dict[str, Any],
    fallback: Any,
) -> Dict[str, Any]:
    """Project authoritative relative geometry back to ResourceDict shape."""

    pose = deepcopy(fallback) if isinstance(fallback, dict) else {}
    relative = store.query_one(
        "SELECT * FROM relative_position "
        "WHERE material_uuid = ? AND deleted_at IS NULL LIMIT 1",
        (material_uuid,),
    )
    if relative is not None:
        pose.update(
            {
                "position": {
                    "x": float(relative.get("position_x") or 0),
                    "y": float(relative.get("position_y") or 0),
                    "z": float(relative.get("position_z") or 0),
                },
                "size": {
                    "width": float(relative.get("width") or 0),
                    "height": float(relative.get("length") or 0),
                    "depth": float(relative.get("depth") or 0),
                },
                "scale": {
                    "x": float(relative.get("scale_x") or 1),
                    "y": float(relative.get("scale_y") or 1),
                    "z": float(relative.get("scale_z") or 1),
                },
                "rotation": {
                    "x": float(relative.get("rotation_x") or 0),
                    "y": float(relative.get("rotation_y") or 0),
                    "z": float(relative.get("rotation_z") or 0),
                },
            }
        )
    elif any(key in config for key in ("size_x", "size_y", "size_z")):
        pose["size"] = {
            "width": float(config.get("size_x") or 0),
            "height": float(config.get("size_y") or 0),
            "depth": float(config.get("size_z") or 0),
        }
    return pose


def _node_from_instance(
    store: InventoryStore, instance: Dict[str, Any]
) -> Dict[str, Any]:
    """把一个库存物料实例投影为旧 HostNode ``ResourceDict`` 行。

    参数说明：``store`` 提供规范物料、库位和运行内容只读事实；``instance`` 是
    当前兼容物料实例。返回：保留稳定物料身份、父关系和物理位置资源（PLR）
    兼容字段的独立字典；支持库位反查的 PLR 原型会在 ``config.sites[]`` 获得
    权威稳定 UUID，普通物料不增加库位字段。

    异常说明：数据库或模板数据无法读取时原样传播；模板携带的库位 UUID 会被
    删除并按权威库位事实重建，调用方不能通过模板注入伪造身份。
    """

    template = store.get_template(str(instance.get("template_id") or ""))
    template_uuid = str(instance.get("template_id") or "")
    base = _resource_spec(store, template_uuid, template)

    # ``edge_uuid`` 是当前物料在 OS 兼容投影中的稳定身份。
    edge_uuid = str(instance.get("edge_uuid") or "")
    # ``material`` 是未删除的 Backend 形状规范物料事实。
    material = _canonical_material(store, edge_uuid) or {}
    material_config = _json_object(material.get("config", "{}"))
    material_data = _json_object(material.get("data", "{}"))
    barcode = str(
        instance.get("barcode")
        or material.get("barcode")
        or base.get("barcode")
        or ""
    )
    node_id = str(base.get("id") or barcode or edge_uuid)
    template_name = str((template or {}).get("name") or "")

    config = base.get("config") if isinstance(base.get("config"), dict) else {}
    data = base.get("data") if isinstance(base.get("data"), dict) else {}
    extra = base.get("extra") if isinstance(base.get("extra"), dict) else {}
    config = deepcopy(config)
    data = deepcopy(data)
    extra = deepcopy(extra)
    config.update(material_config)
    # 一等库位目录必须由库存权威重建；普通物料没有 ``sites[]`` 时保持原合同。
    config = _config_with_authoritative_sites(
        store,
        edge_uuid,
        config,
    )

    # ``relation`` 与 ``slot_id`` 是子物料当前设备局部挂载关系，不是父库位目录。
    relation = store.get_relation(edge_uuid)
    slot_id = str((relation or {}).get("slot_id") or "")
    if slot_id:
        # 既有设备侧挂载代码已消费该兼容键，不能在本投影中擅自改名。
        extra.setdefault("update_resource_site", slot_id)

    inventory_meta = extra.setdefault("edge_inventory", {})
    if not isinstance(inventory_meta, dict):
        inventory_meta = {}
        extra["edge_inventory"] = inventory_meta
    inventory_meta.update(
        {
            "template_id": str(instance.get("template_id") or ""),
            "lot_id": str(instance.get("lot_id") or ""),
            "status": str(instance.get("status") or ""),
            "version": int(instance.get("version") or 1),
            "legacy_cloud_id": str(instance.get("legacy_cloud_id") or ""),
            "slot_id": slot_id,
        }
    )

    content = store.get_content(edge_uuid)
    state = _json_object(
        (content or {}).get("state_json", material_data)
    )
    nested_data = state.pop("data", None)
    if isinstance(nested_data, dict):
        data.update(nested_data)
    for key in _TRACKER_STATE_FIELDS:
        if key in state:
            base[key] = state.pop(key)
    # ``content`` 是运行时状态；未知状态键继续留在 ``data``，避免旧调用方在投影时丢失信息。
    data.update(state)

    node: Dict[str, Any] = {
        **base,
        "id": node_id,
        "uuid": edge_uuid,
        "name": str(
            material.get("name") or base.get("name") or template_name or node_id
        ),
        "description": str(
            material.get("description") or base.get("description") or ""
        ),
        "schema": base.get("schema") if isinstance(base.get("schema"), dict) else {},
        "model": base.get("model") if isinstance(base.get("model"), dict) else {},
        "icon": str(base.get("icon") or ""),
        "parent_uuid": str(instance.get("parent_uuid") or ""),
        # 对只携带早期仓库属性的库存实例，``container`` 是最安全的物理位置资源（PLR）兼容回退类型。
        "type": str(
            base.get("type") or (template or {}).get("category") or "container"
        ),
        "class": str(material.get("class") or base.get("class") or ""),
        "pose": _canonical_pose(
            store,
            edge_uuid,
            config=config,
            fallback=base.get("pose"),
        ),
        "config": config,
        "data": data,
        "extra": extra,
        "machine_name": str(base.get("machine_name") or ""),
        "barcode": barcode,
        "barcode_symbology": str(base.get("barcode_symbology") or ""),
    }
    return node


def _instance_by_id(
    store: InventoryStore, resource_id: str
) -> Optional[Dict[str, Any]]:
    # Most local IDs are one of these indexed instance identities.
    direct = store.query_one(
        "SELECT * FROM material_instance "
        "WHERE edge_uuid = ? OR legacy_cloud_id = ? OR barcode = ? "
        "ORDER BY CASE WHEN edge_uuid = ? THEN 0 "
        "WHEN legacy_cloud_id = ? THEN 1 ELSE 2 END LIMIT 1",
        (resource_id, resource_id, resource_id, resource_id, resource_id),
    )
    if direct is not None:
        return direct

    # A full ResourceDict prototype may define a legacy logical ``id``.  This
    # is intentionally a compatibility scan; Edge UUID remains the canonical
    # identity for new callers.
    for instance in store.query_all(
        "SELECT * FROM material_instance ORDER BY edge_uuid ASC"
    ):
        if _node_from_instance(store, instance).get("id") == resource_id:
            return instance
    return None


def build_legacy_material_nodes(
    store: InventoryStore,
    *,
    uuids: Optional[Iterable[str]] = None,
    resource_id: Optional[str] = None,
    with_children: bool = True,
) -> List[Dict[str, Any]]:
    """Return a deterministic flat ResourceDict list for legacy callers."""

    roots: List[Dict[str, Any]] = []
    for value in uuids or []:
        instance = _instance_by_uuid(store, str(value))
        if instance is not None:
            roots.append(instance)
    if resource_id:
        instance = _instance_by_id(store, resource_id)
        if instance is not None:
            roots.append(instance)

    nodes: List[Dict[str, Any]] = []
    visited: set[str] = set()

    def append(instance: Dict[str, Any]) -> None:
        edge_uuid = str(instance.get("edge_uuid") or "")
        if not edge_uuid or edge_uuid in visited:
            return
        visited.add(edge_uuid)
        nodes.append(_node_from_instance(store, instance))
        if with_children:
            for child in store.component_children_of(edge_uuid):
                append(child)

    for root in roots:
        append(root)
    return nodes


__all__ = ["build_legacy_material_nodes"]
