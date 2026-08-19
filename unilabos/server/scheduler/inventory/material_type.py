"""Backend-compatible material type derivation for Edge inventory.

Backend persists ``material.type`` from the referenced resource template and
falls back to ``resource``.  Canonical templates expose ``config_info`` and
``resource_type``; the legacy inventory view exposes the same definition as
``spec_json`` and ``category``.  This module is the single compatibility seam
for both shapes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, List, Optional


DEFAULT_MATERIAL_TYPE = "resource"


def _non_empty_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _json_mapping(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _json_list(raw: Any) -> List[Any]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _legacy_type_candidates(template: Mapping[str, Any]) -> List[Any]:
    candidates: List[Any] = []
    for field in ("model", "spec_json"):
        spec = _json_mapping(template.get(field))
        for key in ("resource", "resource_dict"):
            nested = spec.get(key)
            if isinstance(nested, Mapping):
                candidates.append(nested.get("type"))
        candidates.append(spec.get("type"))
    return candidates


def material_type_from_template(
    template: Optional[Mapping[str, Any]],
    *,
    material_name: str = "",
    material_class: str = "",
    root: Optional[bool] = None,
) -> str:
    """Derive a persisted material type without accepting it from the client.

    This mirrors Backend migration 000050: a root material uses the first
    ``config_info`` component; a child uses the component whose ``name`` (or
    legacy ``id``) matches the material name; then the template resource type
    and finally ``resource`` are used.  Legacy ``model/spec_json`` prototypes
    remain an explicit compatibility fallback for the Edge inventory API.
    """

    values = template or {}
    components = _json_list(values.get("config_info"))
    template_name = _non_empty_string(values.get("name"))
    normalized_name = _non_empty_string(material_name)
    normalized_class = _non_empty_string(material_class)
    is_root = (
        root
        if root is not None
        else bool(template_name and normalized_class == template_name)
    )

    candidates: List[Any] = []
    if is_root and components:
        first = components[0]
        if isinstance(first, Mapping):
            candidates.append(first.get("type"))
    elif normalized_name:
        for component in components:
            if not isinstance(component, Mapping):
                continue
            component_name = _non_empty_string(
                component.get("name") or component.get("id")
            )
            if component_name == normalized_name:
                candidates.append(component.get("type"))
                break

    candidates.extend(_legacy_type_candidates(values))
    candidates.extend((values.get("resource_type"), values.get("category")))
    for candidate in candidates:
        normalized = _non_empty_string(candidate)
        if normalized:
            return normalized
    return DEFAULT_MATERIAL_TYPE


__all__ = ["DEFAULT_MATERIAL_TYPE", "material_type_from_template"]
