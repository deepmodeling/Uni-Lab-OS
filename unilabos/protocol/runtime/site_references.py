"""按库位给参数值的线上形态（服务端识别、客户端构造共用）。

- 物料参数（``ResourceSlot`` / ``list[ResourceSlot]``）按库位绑定：``{"site_uuid": "<库位 uuid>"}``，
  表示「此刻占着这个库位的物料」。建任务 / 运行预检时解析成占用物料的 ``{"uuid": ...}`` 并冻结进任务，
  之后物料被挪走也不改变任务用的是哪件物料。
- 库位参数（``SiteSlot``，带 ``MaterialRole.site_picker`` 关系）按所属物料选择：``{"owner_uuid": "<所属物料 uuid>"}``，
  表示「这个物料下一个接受放入物料的空库位」。任务准入时调度器按库位序号选一个、经入口预留占住，
  并把选中的库位 uuid 冻结进任务。

只认恰好一个键的对象：带 ``uuid`` 的照旧是普通物料引用，库位 uuid 字符串照旧是精确库位。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from unilabos.protocol.runtime.workflow import validate_uuid

#: 物料参数按库位绑定的键
SITE_UUID_KEY = "site_uuid"
#: 库位参数按所属物料选择的键
OWNER_UUID_KEY = "owner_uuid"


def material_at_site(site_uuid: str) -> dict[str, str]:
    """构造「占着这个库位的物料」引用（物料参数的值）。"""

    return {SITE_UUID_KEY: validate_uuid(site_uuid)}


def site_reference(value: Any) -> Optional[str]:
    """``{"site_uuid": ...}`` 返回库位 uuid（原样，未规范化），否则 ``None``。"""

    if isinstance(value, Mapping) and len(value) == 1 and isinstance(value.get(SITE_UUID_KEY), str):
        return value[SITE_UUID_KEY]
    return None


def site_under_owner(owner_uuid: str) -> dict[str, str]:
    """构造「所属物料下一个接受放入物料的空库位」选择（库位参数的值）。"""

    return {OWNER_UUID_KEY: validate_uuid(owner_uuid)}


def owner_reference(value: Any) -> Optional[str]:
    """``{"owner_uuid": ...}`` 返回所属物料 uuid（原样，未规范化），否则 ``None``。"""

    if isinstance(value, Mapping) and len(value) == 1 and isinstance(value.get(OWNER_UUID_KEY), str):
        return value[OWNER_UUID_KEY]
    return None


__all__ = [
    "OWNER_UUID_KEY",
    "SITE_UUID_KEY",
    "material_at_site",
    "owner_reference",
    "site_reference",
    "site_under_owner",
]
