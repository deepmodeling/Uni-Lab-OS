"""面向设备代码的统一物料 helper。

这里集中放置 PLR 物料的高层操作：

- ``create``：向 materials authority 申请创建物料树并取回权威 UUID；
- ``apply_substances``：把液体或固体内容物写入物料或指定孔位；
- ``resolve_site_spot``：把 Site/slot 标识解析为 PLR spot。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence
from uuid import uuid4

from pylabrobot.resources import ItemizedResource

from unilabos.server.adapters.plr_materials import (
    CreatedPLRMaterials,
    MaterialGateway,
    create_plr_materials,
)
from unilabos.server.protocol.common import InventoryMutation
from unilabos.utils.log import trace


LIQUID_UNIT = "ul"
SOLID_UNIT = "ug"
SELF_SLOT = -1


def set_substance_on_target(
    target: Any,
    name: str,
    amount: float,
    is_solid: bool = False,
) -> Any:
    """把单个内容物写到目标容器或孔位。"""

    unit = SOLID_UNIT if is_solid else LIQUID_UNIT
    target_name = getattr(target, "name", target)
    substances = [(name, amount, unit)]
    if hasattr(target, "set_liquids"):
        target.set_liquids(substances)
    elif hasattr(getattr(target, "tracker", None), "set_liquids"):
        target.tracker.set_liquids(substances)
    else:
        raise ValueError(
            f"目标 {target_name} 不是容器，无法设置内容物（请检查 slots 是否指向子孔位）"
        )
    trace(
        f"[set_substance] {target_name} <- {'固体' if is_solid else '液体'} "
        f"{name}={amount}{unit}"
    )
    return target


def resolve_substance_targets(
    material: Any,
    slots: Optional[Sequence[Any]],
) -> List[Any]:
    """把物料和 slot 标识解析为实际的内容物写入目标。"""

    if not slots or list(slots) == [SELF_SLOT]:
        return [material]

    targets: List[Any] = []
    for slot in slots:
        child = None
        is_index = isinstance(slot, int) or (
            isinstance(slot, str) and slot.isdigit()
        )

        if isinstance(material, ItemizedResource):
            try:
                child = material.get_item(
                    int(slot)
                    if isinstance(slot, str) and slot.isdigit()
                    else slot
                )
            except Exception:
                child = None

        if child is None:
            try:
                child = material[
                    int(slot)
                    if isinstance(slot, str) and slot.isdigit()
                    else slot
                ]
            except Exception:
                child = None
        if child is None and is_index:
            try:
                child = material.children[int(slot)]
            except Exception:
                child = None
        if child is None:
            for candidate in getattr(material, "children", []):
                if candidate.name == slot or (
                    isinstance(slot, str)
                    and candidate.name.endswith(f"_{slot}")
                ):
                    child = candidate
                    break

        if child is None:
            raise ValueError(
                f"无法在物料 {getattr(material, 'name', material)} 中定位子孔位 {slot}"
            )
        targets.append(child)
    return targets


def resolve_site_spot(parent: Any, site: Any) -> Optional[int]:
    """把 Site 标识解析成父级 ``_ordering`` 上的 spot 索引。"""

    if site is None or (isinstance(site, str) and not site):
        return None
    if isinstance(site, int):
        return site
    if isinstance(site, str) and site.isdigit():
        return int(site)
    ordering = getattr(parent, "_ordering", None)
    keys = list(ordering.keys()) if ordering else []
    if site in keys:
        return keys.index(site)
    try:
        target = resolve_substance_targets(parent, [site])[0]
        target_name = getattr(target, "name", None)
        for index, key in enumerate(keys):
            if target_name and (
                target_name == key or target_name.endswith(f"_{key}")
            ):
                return index
    except Exception:
        pass
    return None


def apply_substances(
    material: Any,
    names: Sequence[str],
    amounts: Sequence[float],
    slots: Optional[Sequence[Any]] = None,
    is_solid: Optional[Sequence[bool]] = None,
    broadcast: bool = False,
) -> List[Any]:
    """把一批液体或固体写入物料自身或指定子孔位。"""

    targets = resolve_substance_targets(material, slots)
    normalized_names = list(names)
    normalized_amounts = list(amounts)

    if (
        broadcast
        and len(normalized_names) == 1
        and len(normalized_amounts) == 1
        and len(targets) > 1
    ):
        normalized_names *= len(targets)
        normalized_amounts *= len(targets)

    if not (
        len(targets) == len(normalized_names) == len(normalized_amounts)
    ):
        raise ValueError(
            "增加内容物入参长度不一致："
            f"targets={len(targets)} names={len(normalized_names)} "
            f"amounts={len(normalized_amounts)}"
        )

    solid_flags = list(is_solid or [])
    for index, (target, name, amount) in enumerate(
        zip(targets, normalized_names, normalized_amounts)
    ):
        set_substance_on_target(
            target,
            name,
            amount,
            solid_flags[index] if index < len(solid_flags) else False,
        )
    return targets


def resolve_materials_gateway() -> MaterialGateway:
    """按进程角色选择链路；Slave 永远经 HostLink，不直连 HTTP。"""

    from unilabos.config.config import BasicConfig, HTTPConfig

    if not BasicConfig.is_host_mode:
        from unilabos.hostlink.client import get_hostlink_client
        from unilabos.client.materials import HostLinkMaterialsClient

        client = get_hostlink_client()
        if client is None:
            raise RuntimeError("Slave 尚未连接 HostLink，无法创建物料")
        return HostLinkMaterialsClient(client)

    from unilabos.server.scheduler.integration import get_materials_gateway

    gateway = get_materials_gateway()
    if gateway is not None:
        return gateway

    if HTTPConfig.material_microbackend_addr:
        from unilabos.client.materials import HTTPMaterialsClient

        return HTTPMaterialsClient(HTTPConfig.material_microbackend_addr)

    from unilabos.client.materials import LocalMaterialsClient
    from unilabos.server.composition import get_server_services

    services = get_server_services()
    if services is None:
        raise RuntimeError("Host 尚未配置 materials authority")
    return LocalMaterialsClient(services.materials)


def create(
    plr_resource: Any | Sequence[Any],
    *,
    mutation: InventoryMutation | None = None,
    gateway: MaterialGateway | None = None,
) -> CreatedPLRMaterials:
    """创建一棵 PLR 物料树并返回带权威 UUID 的新对象，不修改输入。"""

    resources = (
        list(plr_resource)
        if isinstance(plr_resource, (list, tuple))
        else [plr_resource]
    )
    if not resources or resources == [None]:
        raise ValueError("创建物料时至少需要一个 PLR resource")
    command_uuid = str(uuid4())
    request = mutation or InventoryMutation(
        command_uuid=command_uuid,
        effect_key="create_material_tree",
        operation="create_material_tree",
    )
    return create_plr_materials(
        gateway or resolve_materials_gateway(), request, resources
    )


__all__ = [
    "LIQUID_UNIT",
    "SELF_SLOT",
    "SOLID_UNIT",
    "apply_substances",
    "create",
    "resolve_site_spot",
    "resolve_materials_gateway",
    "resolve_substance_targets",
    "set_substance_on_target",
]
