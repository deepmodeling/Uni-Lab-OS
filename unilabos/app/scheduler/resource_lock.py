"""本地调度器的物料与库位执行占用键及冲突判定。"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from unilabos.workflow.resource_lock_key import (
    GENERIC_RESOURCE_SCOPE,
    canonical_resource_lock_scope,
    device_lock_key,
    is_canonical_generic_resource_lock_key,
    material_lock_key,
    named_resource_lock_key,
    parse_canonical_resource_lock_key,
    site_lock_key,
)


def normalize_resource_lock_keys(keys: Iterable[str]) -> set[str]:
    """规范化一个作业持有的执行资源键集合。

    参数：``keys`` 可同时包含物料、库位和未来扩展资源键。返回：保留未知键，
    并在整物料键存在时删除同一物料下的冗余库位键。异常：无；无法识别的键
    不参与父子归并，但仍按原值保留，避免静默丢失既有互斥事实。
    """

    # ``whole_materials`` 是本作业已经整对象独占的父物料身份；其子 Site 键
    # 不再增加任何互斥能力，应从最终持有集合移除。
    normalized = set(keys)
    whole_materials: set[str] = set()
    for key in normalized:
        parsed = parse_canonical_resource_lock_key(key)
        if parsed is not None and parsed.scope == "material":
            whole_materials.add(str(parsed.material_uuid))

    result: set[str] = set()
    for key in normalized:
        parsed = parse_canonical_resource_lock_key(key)
        if (
            parsed is None
            or parsed.scope != "material_site"
            or parsed.material_uuid not in whole_materials
        ):
            result.add(key)
    return result


def conflicting_resource_lock_keys(
    requested: Collection[str],
    held: Collection[str],
) -> set[str]:
    """找出申请集合中与已持有集合冲突的执行资源键。

    参数：``requested`` 是候选作业准备取得的键，``held`` 是当前所有在途作业
    已持有的键。返回：发生冲突的申请键；整物料与其任一子库位互斥，同一库位
    互斥，同一物料下不同库位可并行；未知键继续按完全相等判断。异常：无。
    """

    # 先处理完全相等键，再补充整物料与子 Site 的层级冲突。
    conflicts = set(requested) & set(held)
    parsed_held = [
        parsed
        for key in held
        if (parsed := parse_canonical_resource_lock_key(key)) is not None
        and parsed.scope in {"material", "material_site"}
    ]
    for requested_key in requested:
        requested_lock = parse_canonical_resource_lock_key(requested_key)
        if requested_lock is None or requested_lock.scope not in {
            "material",
            "material_site",
        }:
            continue
        for held_lock in parsed_held:
            if requested_lock.material_uuid != held_lock.material_uuid:
                continue
            if (
                requested_lock.scope == "material"
                or held_lock.scope == "material"
                or requested_lock.site_uuid == held_lock.site_uuid
            ):
                conflicts.add(requested_key)
                break
    return conflicts


def narrow_runtime_owner_lock_to_site(
    runtime_material_keys: set[str],
    *,
    owner_material_uuid: str,
    site_uuid: str,
) -> None:
    """运行时才确定的所属物料，派发已把整盒锁收成孔位锁。

    参数：``runtime_material_keys`` 是提交时还没有具体身份、派发时才解析的锁；
    ``owner_material_uuid`` 与 ``site_uuid`` 是这次实际选中的盒子和孔。返回：无，
    原位把整盒锁换成孔位锁。异常：无。所属物料不在运行时集合里时保持原样，
    提交时已经冻住的盒子仍必须出现在资源计划中。
    """

    owner_key = material_lock_key(owner_material_uuid)
    if owner_key not in runtime_material_keys:
        return
    runtime_material_keys.discard(owner_key)
    runtime_material_keys.add(site_lock_key(owner_material_uuid, site_uuid))


__all__ = [
    "GENERIC_RESOURCE_SCOPE",
    "canonical_resource_lock_scope",
    "conflicting_resource_lock_keys",
    "device_lock_key",
    "is_canonical_generic_resource_lock_key",
    "material_lock_key",
    "named_resource_lock_key",
    "narrow_runtime_owner_lock_to_site",
    "normalize_resource_lock_keys",
    "site_lock_key",
]
