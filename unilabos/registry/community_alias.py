"""社区设备 class 名归一化(Plan 09 Task 7)。

社区包设备在扫描期被命名空间化为 ``community.<ns>.<id>`` 作为注册表实体 key
(见 registry.setup 的 community_namespaces 处理)。但 graph / 云端图里可能引用**短别名**
(如 ``liconic_stx110``)或反过来引用带前缀的全名。本模块把二者互相归一到注册表里实际存在的 key。

``initialize_device._lookup_registry_class`` 在 class_name 不在注册表时调用本函数:
若返回值命中注册表则采用,否则报 DeviceClassInvalid。
"""

from __future__ import annotations

from typing import Optional


def normalize_community_class(class_name: str, registry=None) -> str:
    """把社区 class 名归一化为注册表中实际存在的 key。

    双向处理:
    - 短别名 ``<id>`` -> 唯一的 ``community.<ns>.<id>``(若注册表里有且仅有一个匹配);
    - 全名 ``community.<ns>.<id>`` -> 短 ``<id>``(若短名已在注册表)。

    找不到唯一匹配时原样返回(交由调用方判定 DeviceClassInvalid)。
    """
    if not class_name:
        return class_name
    if registry is None:
        try:
            from unilabos.registry.registry import lab_registry as registry
        except Exception:
            return class_name
    try:
        reg = registry.device_type_registry
    except Exception:
        return class_name

    if class_name in reg:
        return class_name

    # 全名 community.<ns>.<id> -> 短名 <id>
    if class_name.startswith("community."):
        short = class_name.rsplit(".", 1)[-1]
        if short in reg:
            return short

    # 短别名 <id> -> 唯一 community.<ns>.<id>
    suffix = f".{class_name}"
    candidates = [k for k in reg if k.startswith("community.") and k.endswith(suffix)]
    if len(candidates) == 1:
        return candidates[0]

    return class_name
