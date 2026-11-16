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


def _name_only_reference(value: Any) -> str:
    """只按名字引用物料（``{"name": ...}``，没有 uuid 类身份也没有 ``id``）时返回名字。"""

    if not isinstance(value, Mapping) or value.get("id") or extract_material_uuids(value):
        return ""
    name = value.get("name")
    return name.strip() if isinstance(name, str) else ""


def resolve_material_name_references(
    parameter_names: Any,
    action_args: Mapping[str, Any],
    gateway: Any,
) -> dict[str, Any]:
    """物料锁参数只给了名字时，派发前向物料权威按名字精确解析，把 uuid 补进参数。

    执行面（``materials.resolve``）同样按名字精确匹配、要求唯一；在这里先解析，物料锁才能锁到
    真实物料，执行面随后按补上的 uuid 取同一件物料。未命中或匹配到多个物料时拒绝派发。
    返回新的参数字典；没有名字引用时原样复制。
    """

    resolved = dict(action_args)
    names = normalize_material_parameter_names(parameter_names)

    def resolve(value: Any, parameter: str) -> Any:
        name = _name_only_reference(value)
        if not name:
            return value
        try:
            matches = list(gateway.search_materials(name))
        except Exception as error:  # noqa: BLE001 - 权威不可达同样不能无锁派发
            raise ValueError(f"物料参数 {parameter!r} 的名字 {name!r} 无法向物料权威解析：{error}") from error
        if not matches:
            raise ValueError(f"物料参数 {parameter!r} 的名字 {name!r} 在物料权威中找不到对应物料")
        if len(matches) > 1:
            raise ValueError(f"物料参数 {parameter!r} 的名字 {name!r} 匹配到多个物料，请改传 uuid")
        return {**value, "uuid": str(matches[0].material.material_uuid)}

    for parameter in names:
        value = resolved.get(parameter)
        if isinstance(value, list):
            resolved[parameter] = [resolve(item, parameter) for item in value]
        elif value is not None:
            resolved[parameter] = resolve(value, parameter)
    return resolved


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
                f"物料参数 {name!r} 无法解析权威物料 UUID："
                "动作的物料入参都默认加锁，请传带 uuid 的物料引用"
            )
        material_uuids.update(resolved)
    return tuple(sorted(material_uuids))


__all__ = [
    "extract_material_uuids",
    "material_uuids_for_parameters",
    "normalize_material_parameter_names",
    "resolve_material_name_references",
]
