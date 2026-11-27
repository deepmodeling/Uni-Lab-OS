"""物料内容物（液体/固体）写入的统一 helper。

- ``set_substance_on_target``：最底层原语，把单个 (名称, 量, 单位) 写到目标的 tracker。
- ``resolve_substance_targets``：把"物料 + slots"解析成实际写入目标列表。
- ``apply_substances``：编排（解析 → 1→N 广播 → 校验长度 → 逐个写入）。

单位约定（与 unilab 定制 PLR 一致，仅支持 ul/ug）：液体=微升(ul)、固体=微克(ug)。
"""

from typing import Any, List, Optional, Sequence

from pylabrobot.resources import ItemizedResource

from unilabos.utils.log import trace

# 单位约定
LIQUID_UNIT = "ul"  # 液体：微升
SOLID_UNIT = "ug"  # 固体：微克

# slots 为空或等于该哨兵时，目标为物料自身（而非其子孔位）
SELF_SLOT = -1


def set_substance_on_target(target: Any, name: str, amount: float, is_solid: bool = False) -> Any:
    """把单个内容物写到目标容器/孔位。

    目标可以是带 ``set_liquids`` 的孔位（Well/Tube），也可以是仅有 ``tracker`` 的
    容器（如 RegularContainer）。两者统一走 set_liquids 三元组 (名称, 量, 单位)。
    """
    unit = SOLID_UNIT if is_solid else LIQUID_UNIT
    target_name = getattr(target, "name", target)
    liquids = [(name, amount, unit)]
    if hasattr(target, "set_liquids"):
        target.set_liquids(liquids)
    elif hasattr(getattr(target, "tracker", None), "set_liquids"):
        target.tracker.set_liquids(liquids)
    else:
        raise ValueError(
            f"目标 {target_name} 不是容器，无法设置内容物（请检查 slots 是否指向子孔位）"
        )
    trace(
        f"[set_substance] {target_name} <- {'固体' if is_solid else '液体'} "
        f"{name}={amount}{unit}"
    )
    return target


def resolve_substance_targets(material: Any, slots: Optional[Sequence[Any]]) -> List[Any]:
    """定位内容物写入目标（物料均为 PLR 体系资源）。

    - slots 为空 / [SELF_SLOT]：目标是物料自身（material 本身是 container / well）。
    - slots 非空：目标是 material 的子孔位/子容器。按 material 类型分派：
      * ItemizedResource（Plate / TipRack / TubeRack ...）：直接 ``get_item``，
        原生支持 int 索引 / "A1" 标签 / (row, col)；数字串先转 int。
      * 其它带子节点的容器（如 Carrier）：__getitem__ → children[索引] → 按名称匹配。
    """
    if not slots or list(slots) == [SELF_SLOT]:
        return [material]

    targets: List[Any] = []
    for s in slots:
        child = None
        is_index = isinstance(s, int) or (isinstance(s, str) and s.isdigit())

        # ItemizedResource：get_item 统一处理 int 索引 / "A1" 标签 / (row, col)
        if isinstance(material, ItemizedResource):
            try:
                child = material.get_item(int(s) if isinstance(s, str) and s.isdigit() else s)
            except Exception:
                child = None

        # 其它容器（Carrier 等）或上面失败：__getitem__ → children[索引] → 按名称
        if child is None:
            try:
                child = material[int(s) if isinstance(s, str) and s.isdigit() else s]
            except Exception:
                child = None
        if child is None and is_index:
            try:
                child = material.children[int(s)]
            except Exception:
                child = None
        if child is None:
            for c in getattr(material, "children", []):
                if c.name == s or (isinstance(s, str) and c.name.endswith(f"_{s}")):
                    child = c
                    break

        if child is None:
            raise ValueError(f"无法在物料 {getattr(material, 'name', material)} 中定位子孔位 {s}")
        targets.append(child)
    return targets


def resolve_site_spot(parent: Any, site: Any) -> Optional[int]:
    """把 site 标识解析成父级 ``_ordering`` 上的 spot 索引（供 ``assign_child_resource(spot=...)`` 用）。

    与 set_substance **复用同一套 slot/site 标识解析**（``resolve_substance_targets``）：支持
    int 索引 / 数字串 / "A1" 标签 / 名称匹配。空 site 返回 None（由父级默认排布）。

    - 直接是 int / 数字串 → 当作 spot 索引返回。
    - 命中 ``_ordering`` 的 key（最常见，保持旧行为）→ 返回其位置索引。
    - 其余 → 复用 ``resolve_substance_targets`` 定位子目标，再回填其在 ``_ordering`` 的位置。
    - 无法解析 → None（交回调用方按原始 site / 默认排布处理）。
    """
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
        tname = getattr(target, "name", None)
        for i, k in enumerate(keys):
            if tname and (tname == k or tname.endswith(f"_{k}")):
                return i
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
    """把一批内容物写入物料（自身或子孔位），返回实际写入的目标列表。

    Args:
        material: 目标物料（container / well / 带子容器的 carrier|plate）。
        names: 每个目标的物质名（液体名或固体名）。
        amounts: 每个目标的用量（液体=体积/微升，固体=质量/微克）。
        slots: 子孔位 id/索引；为空或 [SELF_SLOT]=设在物料自身，非空=设在对应子容器。
        is_solid: 每个目标是否固体（缺省按液体处理，决定单位 ug/ul）。
        broadcast: 当 names/amounts 长度为 1 且目标多于 1 个时，自动广播到所有目标。
    """
    targets = resolve_substance_targets(material, slots)
    names = list(names)
    amounts = list(amounts)

    if broadcast and len(names) == 1 and len(amounts) == 1 and len(targets) > 1:
        names = names * len(targets)
        amounts = amounts * len(targets)

    if not (len(targets) == len(names) == len(amounts)):
        raise ValueError(
            f"增加内容物入参长度不一致：targets={len(targets)} names={len(names)} amounts={len(amounts)}"
        )

    solid_flags = list(is_solid or [])
    for i, (tgt, name, amount) in enumerate(zip(targets, names, amounts)):
        set_substance_on_target(tgt, name, amount, solid_flags[i] if i < len(solid_flags) else False)
    return targets
