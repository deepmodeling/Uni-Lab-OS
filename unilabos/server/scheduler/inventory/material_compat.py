"""Edge inventory to the legacy HostNode material-query contract.

The inventory service deliberately stores normalized templates, instances,
relations and contents.  HostNode consumers still expect a flat list of
``ResourceDict``-shaped nodes.  This module is the compatibility seam between
those two models; callers outside the microbackend should not need to know the
inventory table layout.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional

from unilabos.server.scheduler.inventory.store import InventoryStore
from unilabos.resources.resource_tracker import ResourceSite


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
    "template_name",
    "sites",
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


def _resource_spec(template: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract an optional ResourceDict prototype from a template spec.

    ``resource`` is the preferred additive convention.  ``resource_dict`` is
    accepted for early fixtures, while a top-level ResourceDict remains
    compatible with templates created before the convention was introduced.
    Warehouse-only properties are ignored by the ResourceDict projection.
    """

    spec = _json_object((template or {}).get("spec_json", "{}"))
    nested = spec.get("resource")
    if not isinstance(nested, dict):
        nested = spec.get("resource_dict")
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


def _material_sites(
    store: InventoryStore,
    material_uuid: str,
    template: Optional[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    rows = store.list_material_sites(material_uuid)
    if not rows:
        # Site UUID 属于实例事实；模板定义不能在只读投影时临时生成身份。
        return None

    result: List[Dict[str, Any]] = []
    for row in rows:
        name = str(row["name"])
        allowed = _json_object_list(row.get("allowed_resource_template_uuids"))
        content_types = _json_object_list(row.get("content_type"))
        try:
            index = json.loads(str(row.get("site_index", "0")))
        except (TypeError, ValueError):
            index = int(row["sort_order"])
        site = ResourceSite.model_validate(
            {
                "schema_version": int(row.get("schema_version", 1)),
                "uuid": str(row["uuid"]),
                "template_name": str(
                    row.get("template_name")
                    or (template or {}).get("template_id")
                    or ""
                ),
                "material_uuid": material_uuid,
                "index": index,
                "label": name,
                "visible": bool(row.get("visible", 1)),
                "occupied_material_uuid": row.get("occupied_material_uuid"),
                "position_x": float(row["position_x"]),
                "position_y": float(row["position_y"]),
                "position_z": float(row["position_z"]),
                "width": float(row["width"]),
                "length": float(row["length"]),
                "depth": float(row["depth"]),
                "rotation_x": float(row.get("rotation_x", 0)),
                "rotation_y": float(row.get("rotation_y", 0)),
                "rotation_z": float(row.get("rotation_z", 0)),
                "content_type": content_types,
                "allowed_resource_template_uuids": allowed,
                "parent_link": str(row.get("parent_link") or ""),
                "description": str(row.get("description") or ""),
                "meta_data": _json_object(row.get("meta_data", "{}")),
            }
        ).model_dump()
        result.append(site)
    return result


def _json_object_list(value: Any) -> List[str]:
    if isinstance(value, list):
        values = value
    else:
        try:
            values = json.loads(str(value or "[]"))
        except (TypeError, ValueError):
            values = []
    return [str(item) for item in values] if isinstance(values, list) else []


def _node_from_instance(
    store: InventoryStore, instance: Dict[str, Any]
) -> Dict[str, Any]:
    template = store.get_template(str(instance.get("template_id") or ""))
    base = _resource_spec(template)

    edge_uuid = str(instance.get("edge_uuid") or "")
    barcode = str(instance.get("barcode") or base.get("barcode") or "")
    node_id = str(base.get("id") or barcode or edge_uuid)
    template_name = str((template or {}).get("name") or "")

    config = base.get("config") if isinstance(base.get("config"), dict) else {}
    data = base.get("data") if isinstance(base.get("data"), dict) else {}
    extra = base.get("extra") if isinstance(base.get("extra"), dict) else {}
    config = deepcopy(config)
    data = deepcopy(data)
    extra = deepcopy(extra)

    relation = store.get_relation(edge_uuid)
    slot_id = str((relation or {}).get("slot_id") or "")
    if slot_id:
        # Existing device-side mounting code already consumes this key.
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
            "type": str(instance.get("type") or "resource"),
            "version": int(instance.get("version") or 1),
            "legacy_cloud_id": str(instance.get("legacy_cloud_id") or ""),
            "slot_id": slot_id,
        }
    )

    content = store.get_content(edge_uuid)
    state = _json_object((content or {}).get("state_json", "{}"))
    nested_data = state.pop("data", None)
    if isinstance(nested_data, dict):
        data.update(nested_data)
    for key in _TRACKER_STATE_FIELDS:
        if key in state:
            base[key] = state.pop(key)
    # Content is runtime state.  Unknown state keys stay in ``data`` so older
    # consumers retain them instead of losing information during projection.
    data.update(state)

    node: Dict[str, Any] = {
        **base,
        "id": node_id,
        "uuid": edge_uuid,
        "name": str(base.get("name") or template_name or node_id),
        "description": str(base.get("description") or ""),
        "schema": base.get("schema") if isinstance(base.get("schema"), dict) else {},
        "model": base.get("model") if isinstance(base.get("model"), dict) else {},
        "icon": str(base.get("icon") or ""),
        "parent_uuid": str(instance.get("parent_uuid") or ""),
        # Prefer the persisted Backend-compatible type.  ``resource`` remains
        # the deterministic fallback for older templates without a PLR type.
        "type": str(
            instance.get("type")
            or base.get("type")
            or (template or {}).get("category")
            or "resource"
        ),
        "class": str(base.get("class") or ""),
        "config": config,
        "data": data,
        "extra": extra,
        "machine_name": str(base.get("machine_name") or ""),
        "barcode": barcode,
        "barcode_symbology": str(base.get("barcode_symbology") or ""),
        "template_name": str(instance.get("template_id") or ""),
        "sites": _material_sites(store, edge_uuid, template),
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
