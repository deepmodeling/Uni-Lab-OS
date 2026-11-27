"""资产自带的库位：读出来、按显式对应表放到设备图的库位上。

资产是库位的唯一来源，有两种写法，读出来统一成一张表（资产坐标系、零位、毫米；位置是支撑面中心，
z 是支撑面高度；width 沿 x，length 沿 y）：

* ``unilab.json`` 的 ``interfaces_data`` 里带 ``position_x`` 的库位列表（按组名分组，常见是 ``sites``）；
* USD 里 ``Sites/<名字>`` 下的 Xform（组名 ``usd_site_prims``）；prim 的
  ``customData.unilab.site_definition_json`` 给编号、标签和尺寸。

库位挂在运动部件上时，记下最近的“被关节带动的连杆”（``physics:body1``）及其在资产坐标系里的位姿，
设备图可以把物料挂到这根连杆上。

设备图里驱动自己的库位（``config.sites``）和资产库位的对应关系由设备包显式写出（库位名 → 资产库位编号），
这里不按名字猜。``apply_site_map`` 只负责换算：资产支撑面中心 → 设备图库位的左下角位置，
支撑面更长的方向与库位长边不一致时交换库位的宽和高（库位自己没有旋转）。

读表（``asset_measure.site_table``，需要 OpenUSD）不在这里；这里只放换算，只用标准库。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

FRAME = ("asset frame at the asset's zero pose; mm; position is the centre of the support, z its surface; "
         "width along x, length along y")
KEEP_META = ("position_frame", "live_frame", "qualification", "role", "number_domain",
             "candidate_device_id", "candidate_robot_site", "hardware_verified", "motion_verified")


def _site_lists(interfaces: Mapping[str, Any]) -> dict[str, list[dict]]:
    return {key: value for key, value in interfaces.items()
            if isinstance(value, list) and value and isinstance(value[0], dict) and "position_x" in value[0]}


def _link_pose(link: Mapping[str, Any], corner: Mapping[str, float]) -> dict:
    """库位左下角（设备坐标系，毫米）→ 在连杆坐标系里的位姿：``link⁻¹ · T(corner)``。"""
    frame = link["asset_from_link"]
    rotation = [[frame[i][j] for j in range(3)] for i in range(3)]
    offset = [float(corner[axis]) - frame[i][3] for i, axis in enumerate("xyz")]
    position = [sum(rotation[k][i] * offset[k] for k in range(3)) for i in range(3)]      # Rᵀ·(p − t)
    inverse = [[rotation[j][i] for j in range(3)] for i in range(3)]
    cy = math.hypot(inverse[0][0], inverse[0][1])
    if cy > 1e-9:
        angles = (math.atan2(-inverse[1][2], inverse[2][2]), math.atan2(inverse[0][2], cy), math.atan2(-inverse[0][1], inverse[0][0]))
    else:
        angles = (math.atan2(inverse[2][1], inverse[1][1]), math.atan2(inverse[0][2], cy), 0.0)
    return {"name": link["name"], "position_mm": [round(value, 3) + 0.0 for value in position],
            "rotation_deg": [round(math.degrees(angle), 6) + 0.0 for angle in angles]}


def apply_site_map(sites: list[dict], definitions: list[Mapping[str, Any]], site_map: Mapping[str, Any]) -> list[dict]:
    """按对应表把设备图库位放到资产支撑面上；就地修改 ``sites``，返回对应记录。

    ``site_map``：库位名 → 资产库位编号，或 ``{"index": 编号, "prim": USD 路径}``（同一编号的库位 prim
    出现不止一份时用路径区分）。设备图库位给的是物料外形在库位组坐标系里的左下角，资产给的是支撑面中心；
    库位组应已放在设备原点。对应表里的库位名或资产库位不存在时报错，不静默跳过。
    """
    def pick(target):
        index, prim = (str(target["index"]), target.get("prim")) if isinstance(target, Mapping) else (str(target), None)
        found = [d for d in definitions if str(d["index"]) == index and (prim is None or d.get("prim") == prim)]
        return found[-1] if found and (prim is not None or len({id(d) for d in found}) >= 1) else None

    by_name = {(site.get("name") or site.get("label")): site for site in sites}
    chosen = {name: pick(target) for name, target in site_map.items()}
    unknown = [name for name in site_map if name not in by_name] + [f"{name}→{site_map[name]}" for name, d in chosen.items() if d is None]
    if unknown:
        raise ValueError("库位对应表里有不存在的库位名或资产库位：" + ", ".join(map(str, unknown)))
    records = []
    for name in site_map:
        site, definition = by_name[name], chosen[name]
        width, height = float(site["size"]["width"]), float(site["size"]["height"])
        if definition["length_mm"] != definition["width_mm"] and (definition["length_mm"] > definition["width_mm"]) != (height > width):
            width, height = height, width
            site["size"]["width"], site["size"]["height"] = width, height
        centre = definition["position_mm"]
        site["position"] = {"x": round(centre[0] - width / 2.0, 3), "y": round(centre[1] - height / 2.0, 3), "z": round(centre[2], 3)}
        site.pop("meta_data", None)
        if definition.get("link"):
            # 库位在运动部件上：另给它在该连杆坐标系里的位姿（资产零位），显示端把物料挂到连杆上。
            site["meta_data"] = {"anchor_link": _link_pose(definition["link"], site["position"])}
        records.append({"site": name, "asset_site_index": definition["index"], "asset_site_label": definition["label"],
                        "rides_on": definition.get("live_frame") or definition.get("parent_link") or definition.get("prim")})
    return records
