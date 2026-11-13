"""执行资源锁键的无依赖规范语法与通用命名身份。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import NewType
from uuid import NAMESPACE_URL, UUID, uuid5

GENERIC_RESOURCE_SCOPE = "resource"
CanonicalResourceLockKey = NewType("CanonicalResourceLockKey", str)
_GENERIC_RESOURCE_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "unilabos:scheduler:named-resource-lock:v1",
)


@dataclass(frozen=True, slots=True)
class CanonicalResourceLockIdentity:
    """严格规范锁键中可被持久层信任的语义身份。"""

    scope: str
    material_uuid: str | None = None
    site_uuid: str | None = None


def named_resource_lock_key(alias: str) -> CanonicalResourceLockKey:
    """把作者显式声明的资源别名转换为跨 Workflow 稳定的互斥键。

    别名仅作为 UUID5 的输入，不直接进入锁键语法，因此任意 Unicode 名称都
    不会注入层级分隔符。调用方仍必须证明该别名来自显式资源作用域，不能用
    本函数给动作合同中缺失的设备或物料绑定兜底。
    """

    if not isinstance(alias, str) or not alias.strip():
        raise ValueError("命名资源别名必须是非空字符串")
    identity = uuid5(_GENERIC_RESOURCE_NAMESPACE, alias.strip())
    return CanonicalResourceLockKey(f"{GENERIC_RESOURCE_SCOPE}:{identity}")


def device_lock_key(device_identity: str) -> CanonicalResourceLockKey:
    """生成设备级执行互斥键，并关闭式校验身份段。"""

    return CanonicalResourceLockKey(
        f"/devices/{_require_identity_token(device_identity)}"
    )


def device_key_from_action_key(action_key: object) -> str | None:
    """严格动作键只提升为设备内存互斥事实，不生成持久 Claim 或 Fence。"""
    if not isinstance(action_key, str):
        return None
    parts = action_key.split("/")
    if len(parts) != 4 or parts[0] != "" or parts[1] != "devices" or not parts[2] or not parts[3]:
        return None
    try:
        return device_lock_key(parts[2])
    except ValueError:
        return None


def material_lock_key(material_uuid: str) -> CanonicalResourceLockKey:
    """生成整物料执行互斥键，并关闭式校验身份段。"""

    return CanonicalResourceLockKey(
        f"material/{_require_identity_token(material_uuid)}/exclusive"
    )


def site_lock_key(
    owner_material_uuid: str,
    site_uuid: str,
) -> CanonicalResourceLockKey:
    """生成父物料下具体库位的执行互斥键。"""

    owner = _require_identity_token(owner_material_uuid)
    site = _require_identity_token(site_uuid)
    return CanonicalResourceLockKey(f"material/{owner}/site/{site}/exclusive")


def require_canonical_resource_lock_key(value: object) -> CanonicalResourceLockKey:
    """返回已验证锁键；非规范持久事实抛出 ``ValueError``。"""

    if not isinstance(value, str) or parse_canonical_resource_lock_key(value) is None:
        raise ValueError("执行资源锁键不符合规范语法")
    return CanonicalResourceLockKey(value)


def is_canonical_generic_resource_lock_key(lock_key: object) -> bool:
    """判断值是否为严格规范的 ``resource:<UUID>`` 通用互斥键。"""

    if not isinstance(lock_key, str) or not lock_key.startswith(
        f"{GENERIC_RESOURCE_SCOPE}:"
    ):
        return False
    raw_identity = lock_key.removeprefix(f"{GENERIC_RESOURCE_SCOPE}:")
    try:
        identity = UUID(raw_identity)
    except (AttributeError, TypeError, ValueError):
        return False
    return lock_key == f"{GENERIC_RESOURCE_SCOPE}:{identity}"


def canonical_resource_lock_scope(lock_key: object) -> str | None:
    """返回规范锁键的 scope；未知前缀、空身份或多余层级返回 ``None``。"""

    parsed = parse_canonical_resource_lock_key(lock_key)
    return parsed.scope if parsed is not None else None


def parse_canonical_resource_lock_key(
    lock_key: object,
) -> CanonicalResourceLockIdentity | None:
    """严格解析锁键，并返回 scope 与键内编码的物理身份。

    本函数不依赖调度器或持久层模块，可由 Workflow 与 Inventory 共同复用。
    任何首尾空白、空身份、未知前缀或多余层级都返回 ``None``。通用命名锁没有
    物理身份，因此其 ``material_uuid`` 与 ``site_uuid`` 均为 ``None``。
    """

    if not isinstance(lock_key, str) or lock_key != lock_key.strip():
        return None
    if is_canonical_generic_resource_lock_key(lock_key):
        return CanonicalResourceLockIdentity(scope=GENERIC_RESOURCE_SCOPE)
    parts = lock_key.split("/")
    if (
        len(parts) == 3
        and parts[0] == ""
        and parts[1] == "devices"
        and _canonical_token(parts[2])
    ):
        return CanonicalResourceLockIdentity(
            scope="device",
            material_uuid=parts[2],
        )
    if (
        len(parts) == 3
        and parts[0] == "material"
        and _canonical_token(parts[1])
        and parts[2] == "exclusive"
    ):
        return CanonicalResourceLockIdentity(
            scope="material",
            material_uuid=parts[1],
        )
    if (
        len(parts) == 5
        and parts[0] == "material"
        and _canonical_token(parts[1])
        and parts[2] == "site"
        and _canonical_token(parts[3])
        and parts[4] == "exclusive"
    ):
        return CanonicalResourceLockIdentity(
            scope="material_site",
            material_uuid=parts[1],
            site_uuid=parts[3],
        )
    return None


def _canonical_token(value: str) -> bool:
    """锁键层级身份必须非空、不含空白且不能注入路径层级。"""

    return (
        bool(value)
        and "/" not in value
        and not any(character.isspace() for character in value)
    )


def _require_identity_token(value: object) -> str:
    """校验构造器的单个身份段，并返回原始稳定文本。"""

    if not isinstance(value, str) or not _canonical_token(value):
        raise ValueError("执行资源身份必须是非空、无空白且不含斜杠的字符串")
    return value


__all__ = [
    "CanonicalResourceLockIdentity",
    "CanonicalResourceLockKey",
    "GENERIC_RESOURCE_SCOPE",
    "canonical_resource_lock_scope",
    "device_lock_key",
    "is_canonical_generic_resource_lock_key",
    "material_lock_key",
    "named_resource_lock_key",
    "parse_canonical_resource_lock_key",
    "require_canonical_resource_lock_key",
    "site_lock_key",
]


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
