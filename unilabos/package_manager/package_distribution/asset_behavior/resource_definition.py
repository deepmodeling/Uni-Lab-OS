"""从耗材资产生成物料定义（PLR + OS ``@resource`` 工厂函数）。

耗材资产自带孔位或吸头位（``unilab.json`` 的 ``interfaces_data.sites``，资产坐标系，库位点是孔口或插板上表面的孔中心）
和几何；物料定义是 OS 和驱动认的那一份（板的孔、吸头的长度和配合深度）。两边各写一遍会对不上，这里从资产生成后者：

* 资产管线用 ``asset_measure.measure_material`` 量几何（需要 OpenUSD），连同人给的输入和依据写进资产修订的
  ``parameters.material_definition``；
* 构建期 ``facts_from_asset`` 只读资产（标准库），``generate_resource_definition`` 把事实写成模块源码。

孔深和吸头配合深度从这些量不出来，必须由调用方给出并写依据，不猜、不给默认值。
板的底座（PLR ``PlateAdapter``：一格一格托住孔底的孔格）和盖子（PLR ``Lid``）没有孔位：外形从几何量，
孔格的位置、孔的大小和间距、盖子扣下去的深度由调用方给出并写依据。
生成的工厂默认不带子节点（设备图里不放 96 个孔，资源树会过大）；``with_items=True`` 才展开，供运行时按需算点。
物料坐标系的原点取资产包围盒的最小角点，``ASSET_ORIGIN_MM`` 记录资产原点在其中的位置。
资产名称保留为子物料名；``ITEM_IDENTIFIERS`` 把它映射到 PLR 的 A1 孔位编号，供 ``get_item`` 使用。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

KINDS = ("plate", "tip_rack", "plate_adapter", "lid")
ITEM_KINDS = ("plate", "tip_rack")   # 带孔位或吸头位的种类；底座和盖子没有


def _stated(value: str | None, flag: str) -> tuple[float, str]:
    number, _, basis = (value or "").partition(":")
    if not number.strip() or not basis.strip():
        raise ValueError(f"{flag} 必须写成 毫米:依据")
    return float(number), basis.strip()


BLOCK_SCHEMA = "unilab.material-definition/1"


def _sites(adapter: Mapping[str, Any]) -> list[dict]:
    interfaces = adapter.get("interfaces_data") or {}
    if interfaces.get("units", "mm") != "mm":
        raise ValueError("资产库位的单位不是毫米")
    sites = [site for site in interfaces.get("sites") or [] if "position_mm" in site]
    if not sites:
        raise ValueError("资产没有带 position_mm 的库位，生成不出孔位或吸头位")
    return sites


def facts_from_asset(adapter: Mapping[str, Any], package_name: str = "") -> dict[str, Any]:
    """构建期用（只用标准库）：从资产 ``parameters.material_definition`` 和库位表得到生成物料定义的事实。"""
    block = (adapter.get("parameters") or {}).get("material_definition")
    if not isinstance(block, Mapping) or block.get("schema") != BLOCK_SCHEMA:
        raise ValueError("资产没有 parameters.material_definition（" + BLOCK_SCHEMA + "）")
    kind, geometry, inputs = block["kind"], block["geometry"], block.get("inputs") or {}
    low, high = geometry["body_min_mm"], geometry["body_max_mm"]
    facts: dict[str, Any] = {"kind": kind, "asset_id": adapter["asset_id"]}
    if kind not in ITEM_KINDS:
        evidence = {"asset_id": adapter["asset_id"], "package": package_name, "size": geometry.get("basis")}
        if kind == "plate_adapter":
            grid = inputs["hole_grid"]
            evidence.update(hole_grid=grid["basis"])
            facts.update(hole_corner_mm=[round(float(grid["corner_mm"][i]) - low[i], 4) for i in range(3)],
                         hole_size_mm=[float(v) for v in grid["size_mm"]], hole_pitch_mm=[float(v) for v in grid["pitch_mm"]],
                         plate_z_offset_mm=float((inputs.get("plate_z_offset") or {}).get("mm", 0.0)))
            if inputs.get("plate_z_offset"):
                evidence.update(plate_z_offset=inputs["plate_z_offset"]["basis"])
        else:
            nesting = inputs["nesting_z_height"]
            evidence.update(nesting_z_height=nesting["basis"])
            facts.update(nesting_z_height_mm=float(nesting["mm"]))
        facts.update(size_mm=[round(high[i] - low[i], 4) for i in range(3)],
                     asset_origin_mm=[round(0.0 - low[i], 4) for i in range(3)], items=[], evidence=evidence,
                     resource_id=block["resource_id"])
        return facts
    sites = _sites(adapter)
    evidence: dict[str, Any] = {"asset_id": adapter["asset_id"], "package": package_name,
                                "items": f"interfaces_data.sites（{len(sites)} 个）", "size": geometry.get("basis")}
    if kind == "tip_rack":
        top = geometry["tip_top_z_mm"]
        fitting = inputs["fitting_depth"]
        evidence.update(tip_length=f"{geometry.get('tip_basis')}：Z 范围 {geometry['tip_length_mm']} mm",
                        tip_top=f"吸头模型顶面 Z={top} mm", tip_diameter=f"吸头模型的最大外径 {geometry['tip_diameter_mm']} mm",
                        fitting_depth=fitting["basis"])
        facts.update(tip_length_mm=geometry["tip_length_mm"], tip_diameter_mm=geometry["tip_diameter_mm"],
                     fitting_depth_mm=float(fitting["mm"]), tip_volume_ul=float(inputs["tip_volume_ul"]),
                     tip_has_filter=bool(inputs.get("tip_has_filter")))
        items = [(str(s["name"]), round(s["position_mm"][0] - low[0], 4), round(s["position_mm"][1] - low[1], 4),
                  round(top - low[2], 4), float(s.get("hole_diameter_mm") or 7.0)) for s in sites]
    else:
        declared = [site for site in sites if site.get("lowest_internal_z_mm") is not None]
        if len(declared) == len(sites):  # 资产库位自己声明了腔内最低点：孔深 = 库位点高度 - 最低点
            depths = [round(float(s["position_mm"][2]) - float(s["lowest_internal_z_mm"]), 4) for s in sites]
            evidence.update(well_depth="库位点高度减资产库位声明的 lowest_internal_z_mm")
        elif declared:
            raise ValueError("只有一部分库位声明了 lowest_internal_z_mm")
        else:
            depths = [float(inputs["well_depth"]["mm"])] * len(sites)
            evidence.update(well_depth=inputs["well_depth"]["basis"])
        fallback = inputs.get("well_opening_mm") or ((adapter.get("parameters") or {}).get("opening_mm") or [None])[0]
        openings = []
        for site in sites:
            opening = site.get("opening_mm", fallback)
            if not opening:
                raise ValueError("库位和资产参数里都没有开口尺寸，资产块里要给 well_opening_mm")
            openings.append([float(v) for v in opening] if isinstance(opening, (list, tuple)) else [float(opening)] * 2)
        evidence.update(well_opening="库位的 opening_mm，没有时取资产参数或资产块里的 well_opening_mm")
        items = [(str(s["name"]), round(s["position_mm"][0] - low[0], 4), round(s["position_mm"][1] - low[1], 4),
                  round(s["position_mm"][2] - low[2], 4), o[0], o[1], d) for s, o, d in zip(sites, openings, depths)]
    facts.update(size_mm=[round(high[i] - low[i], 4) for i in range(3)],
                 asset_origin_mm=[round(0.0 - low[i], 4) for i in range(3)], items=items, evidence=evidence,
                 resource_id=block["resource_id"])
    return facts


def _item_identifiers(items: list[tuple]) -> dict[str, str]:
    """保留已有孔位编号，其余按 x 从左到右、y 从上到下生成；不改变资产名称或几何。"""
    columns = {x: index + 1 for index, x in enumerate(sorted({item[1] for item in items}))}
    rows = {y: index for index, y in enumerate(sorted({item[2] for item in items}, reverse=True))}
    identifiers: dict[str, str] = {}
    for label, x, y, *_ in items:
        if re.fullmatch(r"[A-Z]+[1-9][0-9]*", label):
            identifier = label
        else:
            index = rows[y] + 1
            row = ""
            while index:
                index, letter = divmod(index - 1, 26)
                row = chr(ord("A") + letter) + row
            identifier = f"{row}{columns[x]}"
        if identifier in identifiers.values():
            raise ValueError(f"PLR 孔位编号重复：{identifier}；请在资产中明确互不冲突的 A1 格式编号")
        identifiers[label] = identifier
    return identifiers


def generate_resource_definition(resource_id: str, facts: Mapping[str, Any], *, category: list[str] | None = None,
                                 displayname: str = "", model: Mapping[str, str] | None = None) -> str:
    """返回物料定义模块的源码；``resource_id`` 就是 ``@resource`` 的注册 ID，工厂函数名是 ``make_<id>``。

    ``model`` 给出时写进 ``@resource`` 的三维模型声明（``{"format", "entry"}``，``entry`` 相对定义文件）：
    耗材是物料，它的显示模型就挂在物料定义上，不另建显示用的设备类。
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]*", resource_id):
        raise ValueError("resource_id 只能用小写字母、数字和下划线，且以字母开头")
    kind = facts.get("kind")
    if kind not in KINDS:
        raise ValueError(f"facts.kind 必须是 {KINDS}")
    items = [tuple(item) for item in facts["items"]]
    if kind in ITEM_KINDS and (not items or len({item[0] for item in items}) != len(items)):
        raise ValueError("孔位或吸头位为空，或名字重复")
    category = category or ["labware", kind]
    with_items = kind in ITEM_KINDS
    lines = ['"""由 unilabos 的 generate_resource_definition 从耗材资产生成；不要手改，改资产后重新生成。', "",
             json.dumps(facts.get("evidence") or {}, ensure_ascii=False, indent=1), '"""']
    if kind == "tip_rack":
        lines += ["import inspect"]
    if with_items:
        lines += ["from collections import OrderedDict", ""]
    lines += {"plate": ["from pylabrobot.resources import Coordinate, Plate, Well"],
              "tip_rack": ["from pylabrobot.resources import Coordinate, Tip, TipRack, TipSpot"],
              "plate_adapter": ["from pylabrobot.resources import PlateAdapter"],
              "lid": ["from pylabrobot.resources import Lid"]}[kind]
    lines += ["", "from unilabos.registry.decorators import resource", "",
              f"ASSET_ID = {facts['asset_id']!r}",
              f"PACKAGE_DIGEST = {(facts.get('evidence') or {}).get('package_digest')!r}",
              f"ASSET_ORIGIN_MM = {list(facts['asset_origin_mm'])!r}  # 资产原点在物料坐标系里的位置（物料原点 = 资产包围盒最小角点）",
              f"SIZE_MM = {list(facts['size_mm'])!r}"]
    if with_items:
        lines += [("# (名字, 中心 x, 中心 y, 孔口 z, 开口 x, 开口 y, 孔深)；物料坐标系，毫米" if kind == "plate"
                   else "# (名字, 中心 x, 中心 y, 吸头顶面 z, 孔径)；物料坐标系，毫米"), "ITEMS = ["]
        lines += [f"    {item!r}," for item in items] + ["]"]
        lines += ["# 资产名称 -> PLR 孔位编号；名称和坐标保持资产原值，get_item 使用编号。",
                  f"ITEM_IDENTIFIERS = {_item_identifiers(items)!r}"]
    declared = ""
    if model is not None:
        if set(model) != {"format", "entry"} or not all(isinstance(value, str) and value for value in model.values()):
            raise ValueError("model 必须是 {format, entry}，都非空")
        declaration: dict[str, Any] = dict(model)
        origin = [float(value) for value in facts["asset_origin_mm"]]
        if any(abs(value) > 1e-6 for value in origin):
            # 物料原点是包围盒最小角点，模型原点不一定在那里（例如在盒子中心）。显示端把模型放在物料原点上，
            # 差值按显示端的模型基准写：米，Y 朝上（物料坐标系的 x, y, z -> x, z, -y）。
            declaration["position"] = [round(origin[0] / 1000, 6), round(origin[2] / 1000, 6), round(-origin[1] / 1000, 6)]
        declared = "model=" + json.dumps(declaration, ensure_ascii=False) + ", "
    decorator = (f"@resource(id={resource_id!r}, category={category!r}, displayname={displayname!r}, " + declared +
                 'metadata={"asset_id": ASSET_ID, "package_digest": PACKAGE_DIGEST, "generated_from_asset": True})')
    if kind == "plate":
        lines += ["", "",
                  "def _items():", "    found = OrderedDict()", "    for label, x, y, top, open_x, open_y, depth in ITEMS:",
                  "        # 孔的原点就是孔内底，所以底厚记 0",
                  "        shape = \"circle\" if open_x == open_y else \"rectangle\"  # PLR 的圆形截面要求长宽相等",
                  "        well = Well(label, size_x=open_x, size_y=open_y, size_z=depth, material_z_thickness=0.0,",
                  "                    cross_section_type=shape)",
                  "        well.location = Coordinate(x - open_x / 2, y - open_y / 2, top - depth)",
                  "        found[ITEM_IDENTIFIERS[label]] = well", "    return found", "", "", decorator,
                  f"def make_{resource_id}(name: str = {resource_id!r}, with_items: bool = False) -> Plate:",
                  '    """设备图里默认不带孔（资源树会过大）；with_items=True 时展开全部孔，供运行时算点。"""',
                  "    if with_items:",
                  f"        return Plate(name, *SIZE_MM, ordered_items=_items(), model={resource_id!r})",
                  f"    return Plate(name, *SIZE_MM, ordering=OrderedDict(), model={resource_id!r})"]
    elif kind == "plate_adapter":
        corner, hole, pitch = facts["hole_corner_mm"], facts["hole_size_mm"], facts["hole_pitch_mm"]
        lines += ["# 孔格：第一格（A1）最小角点和孔底高度、孔的大小、孔距；物料坐标系，毫米",
                  f"HOLE_CORNER_MM = {[float(v) for v in corner]!r}",
                  f"HOLE_SIZE_MM = {[float(v) for v in hole]!r}",
                  f"HOLE_PITCH_MM = {[float(v) for v in pitch]!r}",
                  f"PLATE_Z_OFFSET_MM = {float(facts.get('plate_z_offset_mm') or 0.0)!r}", "", "", decorator,
                  f"def make_{resource_id}(name: str = {resource_id!r}, with_items: bool = False) -> PlateAdapter:",
                  '    """板的底座：没有孔位，with_items 只为与其它物料定义的工厂签名一致。"""',
                  "    return PlateAdapter(name, *SIZE_MM, dx=HOLE_CORNER_MM[0], dy=HOLE_CORNER_MM[1], dz=HOLE_CORNER_MM[2],",
                  "                        adapter_hole_size_x=HOLE_SIZE_MM[0], adapter_hole_size_y=HOLE_SIZE_MM[1],",
                  "                        adapter_hole_dx=HOLE_PITCH_MM[0], adapter_hole_dy=HOLE_PITCH_MM[1],",
                  f"                        plate_z_offset=PLATE_Z_OFFSET_MM, model={resource_id!r})"]
    elif kind == "lid":
        lines += [f"NESTING_Z_HEIGHT_MM = {float(facts['nesting_z_height_mm'])!r}  # 扣在板上时与板重叠的高度", "", "", decorator,
                  f"def make_{resource_id}(name: str = {resource_id!r}, with_items: bool = False) -> Lid:",
                  '    """盖子（压框）：没有孔位，with_items 只为与其它物料定义的工厂签名一致。"""',
                  f"    return Lid(name, *SIZE_MM, nesting_z_height=NESTING_Z_HEIGHT_MM, model={resource_id!r})"]
    else:
        lines += [f"TIP_LENGTH_MM = {float(facts['tip_length_mm'])!r}",
                  f"TIP_DIAMETER_MM = {float(facts['tip_diameter_mm'])!r}",
                  f"TIP_FITTING_DEPTH_MM = {float(facts['fitting_depth_mm'])!r}", "", "",
                  "def _tip(name=None) -> Tip:",
                  '    """新版 PLR 里吸头是带名字和外径的资源（长度是 size_z）；旧版用 total_tip_length。两种签名都支持。"""',
                  "    shared = dict(has_filter=%r, maximal_volume=%r, fitting_depth=TIP_FITTING_DEPTH_MM)"
                  % (bool(facts["tip_has_filter"]), float(facts["tip_volume_ul"])),
                  '    if "total_tip_length" in inspect.signature(Tip.__init__).parameters:',
                  "        return Tip(total_tip_length=TIP_LENGTH_MM, name=name, **shared)",
                  '    return Tip(name=name or "tip", diameter=TIP_DIAMETER_MM, size_z=TIP_LENGTH_MM, **shared)', "", "",
                  "def _items():", "    found = OrderedDict()", "    for label, x, y, top, opening in ITEMS:",
                  "        spot = TipSpot(label, size_x=opening, size_y=opening, make_tip=_tip, size_z=0)",
                  "        spot.location = Coordinate(x - opening / 2, y - opening / 2, top)  # 顶面 = 吸头顶面",
                  "        found[ITEM_IDENTIFIERS[label]] = spot", "    return found", "", "", decorator,
                  f"def make_{resource_id}(name: str = {resource_id!r}, with_items: bool = False) -> TipRack:",
                  '    """设备图里默认不带吸头位（资源树会过大）；with_items=True 时展开，供运行时算点。"""',
                  "    if with_items:",
                  f"        return TipRack(name, *SIZE_MM, ordered_items=_items(), model={resource_id!r}, with_tips=False)",
                  f"    return TipRack(name, *SIZE_MM, ordering=OrderedDict(), model={resource_id!r}, with_tips=False)"]
    return "\n".join(lines) + "\n"
