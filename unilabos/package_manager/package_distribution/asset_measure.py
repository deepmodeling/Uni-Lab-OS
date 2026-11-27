"""资产几何的量取（需要 OpenUSD）：给资产管线和构建脚本用，不在构建期的标准库路径上。

* ``measure_material``：耗材资产 → 写进资产修订 ``parameters.material_definition`` 的块；
* ``resource_facts``：量完直接给出物料定义事实（不写回资产）；
* ``usd_site_prims`` / ``site_table``：资产自带的库位表（见 ``asset_behavior.asset_sites``）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .asset_behavior.asset_sites import KEEP_META, _site_lists
from .asset_behavior.resource_definition import BLOCK_SCHEMA, ITEM_KINDS, KINDS, _sites, _stated, facts_from_asset


def measure_material(package: str | Path, *, resource_id: str, kind: str, well_depth: str | None = None,
                     well_opening: float | None = None, fitting_depth: str | None = None,
                     tip_volume_ul: float | None = None, tip_has_filter: bool = False,
                     hole_grid: Mapping[str, Any] | None = None, plate_z_offset: str | None = None,
                     nesting_z_height: str | None = None) -> dict[str, Any]:
    """在资产管线里用：量资产几何，连同人给的输入和依据，返回写进资产 ``parameters.material_definition`` 的块。

    需要 OpenUSD。量得出的（外形、吸头长度和外径、吸头顶面）由几何给出；量不出的（孔深、配合深度）必须
    写成 ``毫米:依据``。库位自己声明了 ``lowest_internal_z_mm`` 时孔深按库位算，不需要再给。
    底座（``plate_adapter``）给 ``hole_grid``：``{"corner_mm": 第一格最小角点和孔底高度（资产坐标系）,
    "size_mm": [x, y], "pitch_mm": [x, y], "basis": 依据}``，板不直接落在孔底时再给 ``plate_z_offset``；
    盖子（``lid``）给 ``nesting_z_height``（扣在板上时与板重叠的高度）。这两种不需要资产带库位。
    """
    from pxr import Usd, UsdGeom

    if kind not in KINDS:
        raise ValueError(f"kind 必须是 {KINDS}")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", resource_id):
        raise ValueError("resource_id 只能用小写字母、数字和下划线，且以字母开头")
    package = Path(package)
    adapter = json.loads((package / "simready_usd/unilab.json").read_text(encoding="utf8"))
    sites = _sites(adapter) if kind in ITEM_KINDS else []
    entry = package / adapter["entrypoints"]["usd"]
    if not entry.exists():  # 只拉了轻量档的缓存：轻量档与主入口的 prim 层级、包围盒一致
        entry = package / adapter["entrypoints"]["web_profile"]
    stage = Usd.Stage.Open(str(entry))
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])

    def bounds(prim):
        box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        return [round(v * 1000.0, 4) for v in box.GetMin()], [round(v * 1000.0, 4) for v in box.GetMax()]

    root = stage.GetDefaultPrim()
    block: dict[str, Any] = {"schema": BLOCK_SCHEMA, "resource_id": resource_id, "kind": kind, "inputs": {}, "geometry": {}}
    if kind == "tip_rack":
        holder = root.GetChild("Tips")
        if not holder or not holder.GetChildren():
            raise ValueError("资产里没有 Tips 下的吸头模型，量不出吸头长度")
        if tip_volume_ul is None:
            raise ValueError("吸头盒必须给出 tip_volume_ul")
        body = [bounds(child) for child in root.GetChildren() if child.GetName() != "Tips"]
        low = [min(b[0][i] for b in body) for i in range(3)]
        high = [max(b[1][i] for b in body) for i in range(3)]
        tips = [bounds(tip) for tip in holder.GetChildren()]
        lengths, tops = {round(t[1][2] - t[0][2], 3) for t in tips}, {round(t[1][2], 3) for t in tips}
        if len(lengths) != 1 or len(tops) != 1:
            raise ValueError(f"吸头模型长度或顶面高度不一致：{sorted(lengths)} / {sorted(tops)}")
        depth, basis = _stated(fitting_depth, "fitting_depth")
        block["geometry"] = {"body_min_mm": low, "body_max_mm": high, "basis": "盒体与插板的包围盒（不含吸头）",
                             "tip_length_mm": lengths.pop(), "tip_top_z_mm": tops.pop(),
                             "tip_diameter_mm": round(max(max(t[1][0] - t[0][0], t[1][1] - t[0][1]) for t in tips), 3),
                             "tip_basis": "资产里 Tips 下吸头模型的包围盒"}
        block["inputs"] = {"fitting_depth": {"mm": depth, "basis": basis}, "tip_volume_ul": float(tip_volume_ul),
                           "tip_has_filter": bool(tip_has_filter)}
    elif kind == "plate_adapter":
        low, high = bounds(root)
        block["geometry"] = {"body_min_mm": low, "body_max_mm": high, "basis": "资产包围盒"}
        grid = dict(hole_grid or {})
        if not str(grid.get("basis") or "").strip() or any(len(grid.get(key) or ()) != size for key, size in
                                                           (("corner_mm", 3), ("size_mm", 2), ("pitch_mm", 2))):
            raise ValueError("底座必须给 hole_grid：corner_mm（3 个数）、size_mm、pitch_mm（各 2 个数）和 basis")
        block["inputs"]["hole_grid"] = {"corner_mm": [float(v) for v in grid["corner_mm"]],
                                        "size_mm": [float(v) for v in grid["size_mm"]],
                                        "pitch_mm": [float(v) for v in grid["pitch_mm"]], "basis": str(grid["basis"]).strip()}
        if plate_z_offset is not None:
            offset, basis = _stated(plate_z_offset, "plate_z_offset")
            block["inputs"]["plate_z_offset"] = {"mm": offset, "basis": basis}
    elif kind == "lid":
        low, high = bounds(root)
        block["geometry"] = {"body_min_mm": low, "body_max_mm": high, "basis": "资产包围盒"}
        nesting, basis = _stated(nesting_z_height, "nesting_z_height")
        block["inputs"]["nesting_z_height"] = {"mm": nesting, "basis": basis}
    else:
        low, high = bounds(root)
        block["geometry"] = {"body_min_mm": low, "body_max_mm": high, "basis": "资产包围盒"}
        if not all(site.get("lowest_internal_z_mm") is not None for site in sites):
            depth, basis = _stated(well_depth, "well_depth")
            block["inputs"]["well_depth"] = {"mm": depth, "basis": basis}
        if well_opening is not None:
            block["inputs"]["well_opening_mm"] = float(well_opening)
    return block


def geometry_site_interfaces(package: str | Path) -> dict[str, Any]:
    """资产模型里自带的孔位（``Sites/<名字>`` 的 ``customData.unilab.geometry_site_json``）→ ``interfaces_data`` 的库位表。

    批量入库的板类资产把每个孔的名字、孔底位置和孔口高度写在模型的库位 prim 上，接口数据里却是空的；生成物料
    定义只读接口数据（构建期不开 USD），所以出新修订时先用这个函数把孔位搬进接口数据。每个库位给出孔口中心
    （``position_mm``，z 是孔口高度）和腔内最低点（``lowest_internal_z_mm``），孔深由两者相减得到，不用另给。
    需要 OpenUSD。模型里没有这种库位、缺名字或孔口高度、名字重复时报错。
    """
    from pxr import Usd

    package = Path(package)
    adapter = json.loads((package / "simready_usd/unilab.json").read_text(encoding="utf8"))
    stage = Usd.Stage.Open(str(package / adapter["entrypoints"]["usd"]))
    sites = []
    for prim in stage.Traverse():
        if prim.GetParent().GetName() != "Sites":
            continue
        raw = ((prim.GetCustomData() or {}).get("unilab") or {}).get("geometry_site_json")
        if not raw:
            continue
        declared = json.loads(raw)
        if not declared.get("name") or declared.get("opening_z_mm") is None or len(declared.get("position_mm") or []) != 3:
            raise ValueError(f"{prim.GetPath()}: 孔位缺名字、孔口高度或位置")
        x, y, floor = (float(value) for value in declared["position_mm"])
        site = {"name": str(declared["name"]), "normal": [0, 0, 1],
                "position_mm": [round(x, 4), round(y, 4), round(float(declared["opening_z_mm"]), 4)],
                "lowest_internal_z_mm": round(floor, 4)}
        if declared.get("qualification"):
            site["qualification"] = declared["qualification"]
        sites.append(site)
    if not sites:
        raise ValueError("资产模型里没有带 geometry_site_json 的库位")
    if len({site["name"] for site in sites}) != len(sites):
        raise ValueError("资产模型里的孔位名字重复")
    return {"origin": "asset frame", "units": "mm", "sites": sites,
            "source": "entry USD Sites/* customData.unilab.geometry_site_json"}


def resource_facts(package: str | Path, *, kind: str, well_depth: str | None = None, well_opening: float | None = None,
                   fitting_depth: str | None = None, tip_volume_ul: float | None = None,
                   tip_has_filter: bool = False, resource_id: str = "unnamed", hole_grid: Mapping[str, Any] | None = None,
                   plate_z_offset: str | None = None, nesting_z_height: str | None = None) -> dict[str, Any]:
    """量资产并直接给出事实（相当于 measure_material + facts_from_asset，不写回资产）。"""
    package = Path(package)
    adapter = json.loads((package / "simready_usd/unilab.json").read_text(encoding="utf8"))
    block = measure_material(package, resource_id=resource_id, kind=kind, well_depth=well_depth, well_opening=well_opening,
                             fitting_depth=fitting_depth, tip_volume_ul=tip_volume_ul, tip_has_filter=tip_has_filter,
                             hole_grid=hole_grid, plate_z_offset=plate_z_offset, nesting_z_height=nesting_z_height)
    adapter = {**adapter, "parameters": {**(adapter.get("parameters") or {}), "material_definition": block}}
    return facts_from_asset(adapter, package.name)


def usd_site_prims(usd: str | Path) -> list[dict]:
    """USD 里 ``Sites`` 下的库位 prim：资产坐标系位置（毫米）、编号、尺寸、所在运动连杆。"""
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.Open(str(usd))
    cache = UsdGeom.XformCache()
    to_asset = cache.GetLocalToWorldTransform(stage.GetDefaultPrim()).GetInverse()
    scale = UsdGeom.GetStageMetersPerUnit(stage) * 1000.0
    moved = set()
    for prim in stage.Traverse():
        if "Joint" in prim.GetTypeName():
            moved.update(str(target) for target in prim.GetRelationship("physics:body1").GetTargets())
    found = []
    for prim in stage.Traverse():
        if prim.GetParent().GetName() != "Sites":
            continue
        matrix = cache.GetLocalToWorldTransform(prim) * to_asset
        definition = json.loads(((prim.GetCustomData() or {}).get("unilab") or {}).get("site_definition_json") or "{}")
        link = prim.GetParent()
        while link and link.IsValid() and str(link.GetPath()) not in moved:
            link = link.GetParent()
        entry = {}
        if link and link.IsValid() and str(link.GetPath()) in moved:
            frame = cache.GetLocalToWorldTransform(link) * to_asset
            # 行主序、列向量口径（平移在第 4 列），毫米。
            entry = {"link": {"name": link.GetName(), "prim": link.GetPath().pathString,
                              "asset_from_link": [[round(frame[j][i] * (scale if j == 3 and i < 3 else 1.0), 6) + 0.0
                                                   for j in range(4)] for i in range(4)]}}
        found.append({
            **entry,
            "index": str(definition.get("index", prim.GetName())), "label": definition.get("label") or prim.GetName(),
            "position_mm": [round(matrix[3][axis] * scale, 4) + 0.0 for axis in range(3)],
            "width_mm": round(float(definition.get("width", 0.0)), 4), "length_mm": round(float(definition.get("length", 0.0)), 4),
            "depth_mm": round(float(definition.get("depth", 0.0)), 4),
            "prim": prim.GetPath().pathString,
        })
    return found


def site_table(package: str | Path, adapter: Mapping[str, Any] | None = None, *, prims: bool = True) -> dict:
    """一个资产包的库位表：``{"site_groups": {组名: [库位]}, "notes": {...}}``。"""
    package = Path(package)
    adapter = adapter or json.loads((package / "simready_usd/unilab.json").read_text(encoding="utf-8"))
    interfaces = adapter.get("interfaces_data") or {}
    groups = {}
    for group, sites in _site_lists(interfaces).items():
        groups[group] = [{
            "index": str(site.get("index")), "label": site.get("label"),
            "position_mm": [round(float(site[f"position_{axis}"]), 4) + 0.0 for axis in "xyz"],
            "rotation_deg": [round(float(site.get(f"rotation_{axis}", 0.0)), 6) + 0.0 for axis in "xyz"],
            "width_mm": round(float(site.get("width", 0.0)), 4), "length_mm": round(float(site.get("length", 0.0)), 4),
            "depth_mm": round(float(site.get("depth", 0.0)), 4),
            "parent_link": site.get("parent_link"),
            **{key: site["meta_data"][key] for key in KEEP_META if key in (site.get("meta_data") or {})},
        } for site in sites]
    found = usd_site_prims(package / adapter["entrypoints"]["usd"]) if prims else []
    # 同一个编号的库位 prim 可能出现两次（机体上一份、运动部件上一份）：取带连杆的。
    by_index: dict[str, dict] = {}
    for prim in found:
        if prim["index"] not in by_index or "link" in prim:
            by_index[prim["index"]] = prim
    for sites in groups.values():
        for site in sites:
            if "link" in by_index.get(site["index"], {}):
                site["link"] = by_index[site["index"]]["link"]
    if "sites" not in groups and found:
        groups["usd_site_prims"] = found
    other = {key: value for key, value in interfaces.items() if key not in groups and not isinstance(value, (list, dict))}
    return {"site_groups": groups, **({"notes": other} if other else {})}


