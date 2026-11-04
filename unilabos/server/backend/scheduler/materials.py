"""把动作参数解析为 Materials Authority 分配的规范物料 UUID。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.registry.material_locks import normalize_material_parameter_names


_MATERIAL_UUID_FIELDS = (
    "material_uuid",
    "resource_uuid",
    "unilabos_uuid",
    "uuid",
)


def extract_material_uuids(value: Any) -> set[str]:
    """Extract authoritative material UUIDs from a resolved action argument.

    Supported inputs are UUID strings, resource reference dictionaries, flat
    resource-tree lists, and resolved PLR objects.  ``id``/``name`` are
    deliberately not accepted as lock identities: only authority-issued UUIDs
    may define mutual exclusion.
    """

    if value is None:
        return set()
    if isinstance(value, str):
        stripped = value.strip()
        return {stripped} if stripped else set()
    if isinstance(value, Mapping):
        for field_name in _MATERIAL_UUID_FIELDS:
            field_value = value.get(field_name)
            if isinstance(field_value, str) and field_value.strip():
                return {field_value.strip()}
        for nested_name in ("data", "identity", "material", "resource"):
            nested = value.get(nested_name)
            if isinstance(nested, Mapping):
                nested_ids = extract_material_uuids(nested)
                if nested_ids:
                    return nested_ids
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        result: set[str] = set()
        for item in value:
            result.update(extract_material_uuids(item))
        return result

    for field_name in _MATERIAL_UUID_FIELDS:
        field_value = getattr(value, field_name, None)
        if isinstance(field_value, str) and field_value.strip():
            return {field_value.strip()}
    content = getattr(value, "res_content", None)
    if content is not None:
        content_ids = extract_material_uuids(content)
        if content_ids:
            return content_ids
    extra = getattr(value, "unilabos_extra", None)
    if isinstance(extra, Mapping):
        return extract_material_uuids(extra)
    return set()


def _is_absent_material(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return len(value) == 0
    return False


def material_uuids_for_parameters(
    parameter_names: Any,
    action_args: Mapping[str, Any],
) -> tuple[str, ...]:
    """解析动作要独占的全部物料。

    可选物料入参未传或为空（None / "" / [] / {}）表示没有物料，不加锁；给了值却
    解析不出物料权威分配的 UUID 时拒绝，不能无锁派发。
    """

    names = normalize_material_parameter_names(parameter_names)
    material_uuids: set[str] = set()
    for name in names:
        value = action_args.get(name)
        if _is_absent_material(value):
            continue
        resolved = extract_material_uuids(value)
        if not resolved:
            raise ValueError(
                f"materials_need_lock 参数 {name!r} 无法解析权威物料 UUID："
                "物料入参默认加锁，请传带 uuid 的物料引用；确实不需要独占时在 "
                "@action 里用 materials_free 排除该参数"
            )
        material_uuids.update(resolved)
    return tuple(sorted(material_uuids))


__all__ = [
    "extract_material_uuids",
    "material_uuids_for_parameters",
    "normalize_material_parameter_names",
]
