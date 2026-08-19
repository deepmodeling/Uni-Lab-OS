"""Backend-compatible ``config_info`` material/Site expansion helpers.

Template component UUIDs and parent UUIDs are registry metadata, not instance
identity.  A Material aggregate gets fresh Material and Site UUIDs while the
component order, type, config/data defaults, pose and Site admissibility are
preserved.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from unilabos.resources.site_definition import normalize_available_sites


def _load(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _mapping(value: Any) -> Dict[str, Any]:
    loaded = _load(value, {})
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def _site_list(value: Any) -> List[Dict[str, Any]]:
    loaded = _load(value, [])
    if not isinstance(loaded, list):
        return []
    return [dict(item) for item in loaded if isinstance(item, Mapping)]


def template_components(
    template: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Return normalized config components in declaration order.

    Backend ignores template component ``uuid``/``parent_uuid``.  Child
    component IDs, however, are required because they define generated
    barcodes and stable semantic identity inside the frozen template.
    """

    components = _site_list((template or {}).get("config_info"))
    seen_child_ids: set[str] = set()
    for ordinal, component in enumerate(components):
        component_id = str(component.get("id") or "").strip()
        component["id"] = component_id
        component["name"] = str(component.get("name") or component_id).strip()
        if ordinal == 0:
            continue
        if not component_id:
            raise ValueError(f"config_info component {ordinal} id is required")
        normalized = component_id.casefold()
        if normalized in seen_child_ids:
            raise ValueError(f"duplicate config_info component id: {component_id}")
        seen_child_ids.add(normalized)
    return components


def _merge_maps(base: Dict[str, Any], override: Mapping[str, Any]) -> None:
    for key, value in override.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge_maps(current, value)
        else:
            base[key] = deepcopy(value)


def merge_json_objects(
    base: Any,
    override: Any,
    *,
    protected_fields: tuple[str, ...] = (),
) -> Dict[str, Any]:
    """Recursively merge JSON objects, preserving template-owned fields."""

    base_values = _mapping(base)
    override_values = _mapping(override)
    result = deepcopy(base_values)
    _merge_maps(result, override_values)
    for field in protected_fields:
        if field in base_values:
            result[field] = deepcopy(base_values[field])
        else:
            result.pop(field, None)
    return result


def component_sites(component: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Read Backend ``config.sites`` with a root-field Edge compatibility seam."""

    sites = _mapping(component.get("config")).get("sites")
    if sites is None:
        # Older PLR/Edge registry snapshots promoted sites to a root field.
        sites = component.get("sites")
    return _site_list(sites)


def canonical_component_sites(component: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """把模板中的新旧 Site 定义投影为 Provider 的扁平 Site 契约。

    早期模板把实例 ``uuid``/占用信息误放进模板；这些字段从未作为实例身份
    复用，迁移时显式丢弃。UniLabOS 核心模型已经把几何统一收敛进 ``pose``，
    而 Provider 数据库及 Edge API 仍使用扁平列，因此只在这一边界做投影。
    ``content_type`` 与 ``allowed_resource_template_uuids`` 属于 Provider 放置契约，
    不写回核心 ``SiteDefinition``。
    """

    definitions: List[Dict[str, Any]] = []
    for ordinal, site in enumerate(component_sites(component)):
        payload = deepcopy(site)
        for instance_field in (
            "uuid",
            "template_name",
            "material_uuid",
            "occupied_material_uuid",
            "occupied_by",
        ):
            payload.pop(instance_field, None)

        content_type = payload.pop("content_type", [])
        allowed_templates = payload.pop("allowed_resource_template_uuids", [])
        if not isinstance(content_type, list):
            raise ValueError("Site content_type must be an array")
        if not isinstance(allowed_templates, list):
            raise ValueError("Site allowed_resource_template_uuids must be an array")

        normalized = normalize_available_sites([payload])[0]
        pose = _mapping(normalized.pop("pose", {}))
        position = _mapping(pose.get("position"))
        if not position:
            position = _mapping(pose.get("position3d"))
        size = _mapping(pose.get("size"))
        rotation = _mapping(pose.get("rotation"))
        normalized.pop("allowed_resource_categories", None)
        normalized.update(
            {
                "position_x": _axis(position, "x", 0),
                "position_y": _axis(position, "y", 0),
                "position_z": _axis(position, "z", 0),
                "width": max(0.0, _number(size.get("width"))),
                "length": max(0.0, _number(size.get("height"))),
                "depth": max(0.0, _number(size.get("depth"))),
                "rotation_x": _axis(rotation, "x", 0),
                "rotation_y": _axis(rotation, "y", 0),
                "rotation_z": _axis(rotation, "z", 0),
                "content_type": deepcopy(content_type),
                "allowed_resource_template_uuids": deepcopy(allowed_templates),
            }
        )
        definitions.append(normalized)
    return definitions


def template_site_specs(template: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Return root Site specifications from canonical or legacy template shape."""

    values = template or {}
    components = template_components(values)
    if components:
        return component_sites(components[0])

    prototype: Dict[str, Any] = {}
    for field in ("model", "spec_json"):
        model = _mapping(values.get(field))
        nested = model.get("resource")
        if not isinstance(nested, Mapping):
            nested = model.get("resource_dict")
        candidate = nested if isinstance(nested, Mapping) else model
        if candidate:
            prototype = dict(candidate)
            break
    sites = prototype.get("sites")
    if sites is None:
        sites = _mapping(prototype.get("config")).get("sites")
    return _site_list(sites)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _axis(value: Any, axis: str, default: float) -> float:
    return _number(_mapping(value).get(axis), default)


def component_relative_position(
    component: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Project a Backend template component position/pose into canonical columns."""

    config = _mapping(component.get("config"))
    pose = _mapping(component.get("pose"))
    position = component.get("position")
    if not isinstance(position, Mapping):
        position = pose.get("position")
    pose_size = _mapping(pose.get("size"))
    pose_scale = pose.get("scale")
    rotation = config.get("rotation")
    if not isinstance(rotation, Mapping):
        rotation = pose.get("rotation")
    has_position = any(
        (
            isinstance(component.get("position"), Mapping),
            bool(pose),
            "size_x" in config,
            "size_y" in config,
            "size_z" in config,
            isinstance(config.get("rotation"), Mapping),
        )
    )
    if not has_position:
        return None
    return {
        "position_x": _axis(position, "x", 0),
        "position_y": _axis(position, "y", 0),
        "position_z": _axis(position, "z", 0),
        "depth": max(
            0.0, _number(config.get("size_z"), _number(pose_size.get("depth")))
        ),
        "length": max(
            0.0, _number(config.get("size_y"), _number(pose_size.get("height")))
        ),
        "width": max(
            0.0, _number(config.get("size_x"), _number(pose_size.get("width")))
        ),
        "scale_x": _axis(pose_scale, "x", 1),
        "scale_y": _axis(pose_scale, "y", 1),
        "scale_z": _axis(pose_scale, "z", 1),
        "rotation_x": _axis(rotation, "x", 0),
        "rotation_y": _axis(rotation, "y", 0),
        "rotation_z": _axis(rotation, "z", 0),
        "description": component.get("description"),
        "meta_data": {},
    }


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _site_admission_rules(site: Mapping[str, Any]) -> tuple[List[str], List[str]]:
    explicit = site.get("allowed_resource_template_uuids")
    if explicit is not None and not isinstance(explicit, list):
        raise ValueError("Site allowed_resource_template_uuids must be an array")
    allowed = sorted(
        {str(value).strip() for value in (explicit or []) if str(value).strip()}
    )

    content_types = site.get("content_type")
    if content_types is None:
        return allowed, []
    if not isinstance(content_types, list):
        raise ValueError("Site content_type must be an array")
    normalized_content_types = sorted(
        {str(value).strip() for value in content_types if str(value).strip()},
        key=str.casefold,
    )
    return allowed, normalized_content_types


def materialize_component_sites(
    connection: sqlite3.Connection,
    material_uuid: str,
    component: Mapping[str, Any],
) -> List[str]:
    """Create one component's Sites with fresh stable UUIDs and resolved tags."""

    created: List[str] = []
    seen_names: set[str] = set()
    for ordinal, site in enumerate(canonical_component_sites(component)):
        index = site["index"]
        name = site["label"]
        normalized_name = name.casefold()
        if normalized_name in seen_names:
            raise ValueError(f"duplicate template Site name: {name}")
        seen_names.add(normalized_name)

        existing = connection.execute(
            "SELECT uuid FROM site WHERE material_uuid=? AND LOWER(name)=LOWER(?) "
            "AND deleted_at IS NULL",
            (material_uuid, name),
        ).fetchone()
        if existing is not None:
            continue

        allowed, content_types = _site_admission_rules(site)
        sort_order = index if isinstance(index, int) and index >= 0 else ordinal
        site_uuid = str(uuid4())
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO site(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                material_uuid,name,sort_order,allowed_resource_template_uuids,
                content_type,
                occupied_material_uuid,position_x,position_y,position_z,
                depth,length,width,schema_version,site_index,visible,
                rotation_x,rotation_y,rotation_z,parent_link
            ) VALUES (
                ?,?,?,NULL,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                site_uuid,
                timestamp,
                timestamp,
                site.get("description"),
                json.dumps(
                    site.get("meta_data", {}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                material_uuid,
                name,
                sort_order,
                json.dumps(allowed, ensure_ascii=False, separators=(",", ":")),
                json.dumps(content_types, ensure_ascii=False, separators=(",", ":")),
                _number(site.get("position_x")),
                _number(site.get("position_y")),
                _number(site.get("position_z")),
                max(0.0, _number(site.get("depth"))),
                max(0.0, _number(site.get("length"))),
                max(0.0, _number(site.get("width"))),
                int(site.get("schema_version", 1)),
                json.dumps(index, ensure_ascii=False, separators=(",", ":")),
                int(bool(site.get("visible", True))),
                _number(site.get("rotation_x")),
                _number(site.get("rotation_y")),
                _number(site.get("rotation_z")),
                str(site.get("parent_link") or ""),
            ),
        )
        created.append(site_uuid)
    return created


def materialize_template_sites(
    connection: sqlite3.Connection,
    material_uuid: str,
    template: Optional[Mapping[str, Any]],
) -> List[str]:
    """Compatibility wrapper for callers that materialize only a root Material."""

    components = template_components(template)
    if components:
        return materialize_component_sites(connection, material_uuid, components[0])
    return materialize_component_sites(
        connection,
        material_uuid,
        {"config": {"sites": template_site_specs(template)}},
    )


__all__ = [
    "canonical_component_sites",
    "component_relative_position",
    "component_sites",
    "materialize_component_sites",
    "materialize_template_sites",
    "merge_json_objects",
    "template_components",
    "template_site_specs",
]
