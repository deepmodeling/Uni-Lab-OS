"""物料 ``class`` 的约定：初始化它用的注册表名（registry name）。

字段沿用 ``class`` / ``class_name``（图文件、旧后端与前端都按这个名字读），含义如下：

- 按注册表资源初始化的物料：``class`` 是注册表资源名（如 ``container``、``virtual_heating_sample``、
  ``opentrons_96_tiprack_300ul``），与它的资源模板名相同。实现所在的模块（注册表条目的
  ``class.module``）按这个名字在注册表里查，不另存；
- 没有注册表条目的 PLR 物料（孔位、枪头位等随父物料的注册表工厂一起建出来的孩子，或临时构造的
  PLR 资源）：``class`` 退回 PLR 类名；
- 设备：注册表设备名（不变）。

``config.type`` 是 PLR 类名：设备重启重建 PLR 实例就是按它直接反序列化，``class`` 只是记录，
不参与重建。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional


def registry_resource_entry(name: Any) -> Optional[Mapping[str, Any]]:
    """注册表资源条目；名字不是注册表资源名（或目录不可用）时返回 None。

    资源类目录在调度权威里是 Host 上报的生效条目，在 Host / Slave 里是本地扫描。
    """

    text = str(name or "").strip()
    if not text:
        return None
    try:
        from unilabos.registry.catalog import resource_entry

        entry = resource_entry(text)
    except Exception:  # noqa: BLE001 - 目录不可用时按没有注册表条目处理
        return None
    return entry if isinstance(entry, Mapping) else None


def registry_resource_module(name: Any) -> Optional[str]:
    """注册表资源名 → 实现所在的 ``module:attr``（条目的 ``class.module``）；查不到返回 None。"""

    entry = registry_resource_entry(name)
    definition = entry.get("class") if entry is not None else None
    module = definition.get("module") if isinstance(definition, Mapping) else None
    text = str(module or "").strip()
    module_name, sep, attribute = text.partition(":")
    return text if sep and module_name and attribute else None


def material_class(
    *,
    resource_type: Any = None,
    klass: Any = None,
    template_name: Any = None,
    plr_type: Any = None,
) -> str:
    """按约定得出物料的 ``class``：注册表资源名，没有注册表条目时退回 PLR 类名；设备不变。"""

    current = str(klass or "").strip()
    if str(resource_type or "") == "device":
        return current or str(template_name or plr_type or "Resource")
    template = str(template_name or "").strip()
    if template and registry_resource_entry(template) is not None:
        return template
    if current and registry_resource_entry(current) is not None:
        return current
    return str(plr_type or "").strip() or current or "Resource"


def resolve_material_class(
    *, template_name: Any, template_class: Any, identity_class: Any, plr_type: Any = None
) -> Optional[str]:
    """物料入库时落的 class；与模板对不上时返回 None。

    注册表模板的 class 就是模板名（注册表资源名）。两种过渡情形按模板归一：

    - 升级前登记的注册表模板 class 还是 PLR 类名（注册表同步一次后才改成注册表名）：物料带来的
      class 等于模板名，照收；
    - 带来物料的一方查不到注册表（如还没收到 Host 上报的调度权威、没升级的客户端），class 退回成了
      PLR 类名（等于 ``config.type``）：落模板的 class，也就是注册表名。
    """

    stored = str(template_class or "").strip()
    incoming = str(identity_class or "").strip()
    name = str(template_name or "").strip()
    if not stored or stored == incoming:
        return incoming or stored
    if incoming == name:
        return incoming
    if stored == name and incoming and incoming == str(plr_type or "").strip():
        return stored
    return None


__all__ = [
    "resolve_material_class",
    "material_class",
    "registry_resource_entry",
    "registry_resource_module",
]
