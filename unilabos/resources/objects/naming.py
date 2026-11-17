"""物料命名规则：物料层 ``name`` 恒等于 uuid，人读标签是 ``display_name``，PLR 键放 ``config.name``。

- ``uuid``：身份；``name`` 与它相同（所有物料，包括设备）；
- ``display_name``：人读标签，可重名、可编辑；孔位取 ordering 标识（如 ``"A1"``）；
- ``id`` / ``resource_id``：业务 id，保持人可读，不从 uuid 派生；
- PLR 键（``config.name``）：PLR 树里的名字，整棵 PLR 树内唯一、创建后不可改。默认沿用迁移前的
  name；新建根物料取 ``resource_id``；「名字等于 ordering 标识」的孔和管（Opentrons 的裸 ``"A1"``）
  取 uuid。设备不是 PLR 资源，``config`` 是驱动构造参数，不写 PLR 键。

规则「name 已是 UUID 就复用，否则旧名迁到 display_name（为空时）和 PLR 键（缺失时）」在每个写入
入口执行：图文件读取边界（legacy 升级）、Graph Authority 对账、物料创建 ``create_tree``、
PLR 导入 ``from_plr_resources``。
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping, MutableMapping, Optional
from uuid import UUID

#: ``config`` 里 PLR 键的字段名。
PLR_KEY_FIELD = "name"
#: 名字可能就是 ordering 标识的孩子类型（PLR category）：孔和管。
BARE_LABEL_TYPES = frozenset({"well", "tube"})


class MaterialNameConflict(ValueError):
    """UUID 形式的 name 与物料 uuid 不一致：身份冲突，不猜。"""


def canonical_uuid(value: Any) -> Optional[str]:
    """``value`` 是 UUID 形式时返回小写带连字符的规范写法，否则返回 None。

    大写、带花括号、不带连字符的写法都按 UUID 处理（物料表主键是区分大小写的 TEXT）。
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except ValueError:
        return None


def legacy_label(name: Any) -> str:
    """旧格式的 name：不是 UUID 形式的非空字符串；是 UUID 或为空时返回空串。"""

    text = name.strip() if isinstance(name, str) else ""
    return "" if not text or canonical_uuid(text) else text


def plr_key(config: Any) -> str:
    """``config.name``（PLR 键）；缺失或不是非空字符串时返回空串。"""

    if not isinstance(config, Mapping):
        return ""
    key = config.get(PLR_KEY_FIELD)
    return key.strip() if isinstance(key, str) and key.strip() else ""


def check_name_identity(name: Any, material_uuid: Any, *, label: Any) -> Optional[str]:
    """UUID 形式的 name 就是身份：返回它的规范写法；与已知 uuid 不一致时抛冲突。

    ``material_uuid`` 为空表示节点还没有 uuid，此时由调用方决定是否采纳 name 作为 uuid。
    """

    named = canonical_uuid(name)
    if named is None or not material_uuid:
        return named
    if canonical_uuid(material_uuid) != named:
        raise MaterialNameConflict(
            f"物料 {label} 的 name ({name}) 是 UUID，但与物料 uuid ({material_uuid}) 不一致"
        )
    return named


def migrated_name_fields(
    *,
    name: Any,
    display_name: Any,
    config: Optional[Mapping[str, Any]],
    resource_type: Any,
    default_plr_key: str = "",
    default_label: str = "",
) -> tuple[str, dict[str, Any]]:
    """把非 UUID 的旧 name 迁到 display_name（为空时）和 PLR 键（缺失时）。

    返回 ``(display_name, config)``；调用方随后把 name 写成 uuid。设备不写 PLR 键。
    PLR 键缺失且没有旧名时取 ``default_plr_key``；展示名依次回退到旧名、不是 UUID 的 PLR 键、
    ``default_label``。
    """

    label = legacy_label(name)
    migrated = copy.deepcopy(dict(config or {}))
    if str(resource_type or "") != "device" and not plr_key(migrated):
        key = label or default_plr_key
        if key:
            migrated[PLR_KEY_FIELD] = key
    display = (
        str(display_name or "").strip()
        or label
        or legacy_label(plr_key(migrated))
        or default_label
        or plr_key(migrated)
    )
    return display, migrated


def rekey_bare_label_items(
    parent_config: MutableMapping[str, Any],
    children: Iterable[tuple[str, Any, MutableMapping[str, Any]]],
) -> int:
    """「名字等于 ordering 标识」的孔和管改用 uuid 作 PLR 键，同步父节点 ordering；返回改键数。

    ``children`` 是 ``(孩子 uuid, 孩子 resource_type, 孩子 config)``，原地修改。Opentrons 的板、
    储液槽和部分管架把孔 / 管直接命名为 ``"A1"``，同一台面放两块就在 PLR 树里撞名；改键后
    ordering 标识不变（``{"A1": <uuid>}``），``plate["A1"]`` / ``get_item("A1")`` 照常可用，
    人读标签仍是 ``display_name="A1"``。构造函数生成的结构名（台面槽位、``trash``、Carrier
    位点、枪头 ``#n``）不是孔或管，或者名字不等于 ordering 标识，不受影响。
    """

    ordering = parent_config.get("ordering")
    if not isinstance(ordering, MutableMapping):
        return 0
    rekeyed = 0
    for child_uuid, resource_type, child_config in children:
        key = plr_key(child_config)
        if (
            str(resource_type or "") in BARE_LABEL_TYPES
            and key
            and key != child_uuid
            and ordering.get(key) == key
        ):
            child_config[PLR_KEY_FIELD] = child_uuid
            ordering[key] = child_uuid
            rekeyed += 1
    return rekeyed


__all__ = [
    "BARE_LABEL_TYPES",
    "MaterialNameConflict",
    "PLR_KEY_FIELD",
    "canonical_uuid",
    "check_name_identity",
    "legacy_label",
    "migrated_name_fields",
    "plr_key",
    "rekey_bare_label_items",
]
