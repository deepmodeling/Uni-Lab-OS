"""从现有 prcxi_labware.py + registry YAML 导入耗材数据到 labware_db.json。

策略：
1. 实例化每个工厂函数 → 提取物理尺寸、material_info、children
2. AST 解析源码 → 提取 docstring、volume_function 参数、plate_type
3. 从 children[0].location 反推 dx/dy/dz，相邻位置差推 item_dx/item_dy
4. 同时读取现有 YAML → 提取 registry_category / description
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# 将项目根目录加入 sys.path 以便 import
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from unilabos.labware_manager.models import (
    AdapterInfo,
    GridInfo,
    LabwareDB,
    LabwareItem,
    MaterialInfo,
    TipInfo,
    TubeInfo,
    VolumeFunctions,
    WellInfo,
)

# ---------- 路径常量 ----------
_LABWARE_PY = Path(__file__).resolve().parents[1] / "devices" / "liquid_handling" / "prcxi" / "prcxi_labware.py"
_REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry" / "resources" / "prcxi"
_DB_PATH = Path(__file__).resolve().parent / "labware_db.json"

# YAML 文件名 → type 映射
_YAML_MAP: Dict[str, str] = {
    "plates.yaml": "plate",
    "tip_racks.yaml": "tip_rack",
    "trash.yaml": "trash",
    "tube_racks.yaml": "tube_rack",
    "plate_adapters.yaml": "plate_adapter",
}

# PRCXI_TEMPLATE_FACTORY_KINDS 中列出的函数名（include_in_template_matching=True）
_TEMPLATE_FACTORY_NAMES = {
    "PRCXI_BioER_96_wellplate", "PRCXI_nest_1_troughplate",
    "PRCXI_BioRad_384_wellplate", "PRCXI_AGenBio_4_troughplate",
    "PRCXI_nest_12_troughplate", "PRCXI_CellTreat_96_wellplate",
    "PRCXI_10ul_eTips", "PRCXI_300ul_Tips",
    "PRCXI_PCR_Plate_200uL_nonskirted", "PRCXI_PCR_Plate_200uL_semiskirted",
    "PRCXI_PCR_Plate_200uL_skirted", "PRCXI_trash",
    "PRCXI_96_DeepWell", "PRCXI_EP_Adapter",
    "PRCXI_1250uL_Tips", "PRCXI_10uL_Tips",
    "PRCXI_1000uL_Tips", "PRCXI_200uL_Tips",
    "PRCXI_48_DeepWell",
}

# template_kind 对应
_TEMPLATE_KINDS: Dict[str, str] = {
    "PRCXI_BioER_96_wellplate": "plate",
    "PRCXI_nest_1_troughplate": "plate",
    "PRCXI_BioRad_384_wellplate": "plate",
    "PRCXI_AGenBio_4_troughplate": "plate",
    "PRCXI_nest_12_troughplate": "plate",
    "PRCXI_CellTreat_96_wellplate": "plate",
    "PRCXI_10ul_eTips": "tip_rack",
    "PRCXI_300ul_Tips": "tip_rack",
    "PRCXI_PCR_Plate_200uL_nonskirted": "plate",
    "PRCXI_PCR_Plate_200uL_semiskirted": "plate",
    "PRCXI_PCR_Plate_200uL_skirted": "plate",
    "PRCXI_trash": "trash",
    "PRCXI_96_DeepWell": "plate",
    "PRCXI_EP_Adapter": "tube_rack",
    "PRCXI_1250uL_Tips": "tip_rack",
    "PRCXI_10uL_Tips": "tip_rack",
    "PRCXI_1000uL_Tips": "tip_rack",
    "PRCXI_200uL_Tips": "tip_rack",
    "PRCXI_48_DeepWell": "plate",
}


def _load_registry_info() -> Dict[str, Dict[str, Any]]:
    """读取所有 registry YAML 文件，返回 {function_name: {category, description}} 映射。"""
    info: Dict[str, Dict[str, Any]] = {}
    for fname, ltype in _YAML_MAP.items():
        fpath = _REGISTRY_DIR / fname
        if not fpath.exists():
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for func_name, entry in data.items():
            info[func_name] = {
                "registry_category": entry.get("category", ["prcxi", ltype.replace("plate_adapter", "plate_adapters")]),
                "registry_description": entry.get("description", ""),
            }
    return info


def _parse_ast_info() -> Dict[str, Dict[str, Any]]:
    """AST 解析 prcxi_labware.py，提取每个工厂函数的 docstring 和 volume_function 参数。"""
    source = _LABWARE_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    result: Dict[str, Dict[str, Any]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        fname = node.name
        if not fname.startswith("PRCXI_"):
            continue
        if fname.startswith("_"):
            continue

        info: Dict[str, Any] = {"docstring": "", "volume_functions": None, "plate_type": None}

        # docstring
        doc = ast.get_docstring(node)
        if doc:
            info["docstring"] = doc.strip()

        # 搜索函数体中的 plate_type 赋值和 volume_function 参数
        func_source = ast.get_source_segment(source, node) or ""

        # plate_type
        m = re.search(r'plate_type\s*=\s*["\']([^"\']+)["\']', func_source)
        if m:
            info["plate_type"] = m.group(1)

        # volume_functions: 检查 compute_height_from_volume_rectangle
        if "compute_height_from_volume_rectangle" in func_source:
            # 提取 well_length 和 well_width
            vf: Dict[str, Any] = {"type": "rectangle"}
            # 尝试从 lambda 中提取
            wl_match = re.search(r'well_length\s*=\s*([\w_.]+)', func_source)
            ww_match = re.search(r'well_width\s*=\s*([\w_.]+)', func_source)
            if wl_match:
                vf["well_length_var"] = wl_match.group(1)
            if ww_match:
                vf["well_width_var"] = ww_match.group(1)
            info["volume_functions"] = vf

        result[fname] = info

    return result


def _probe_factory(factory_func) -> Any:
    """实例化工厂函数获取 resource 对象。"""
    if factory_func.__name__ == "PRCXI_trash":
        return factory_func()
    return factory_func("__probe__")


def _get_size(resource, attr: str) -> float:
    """获取 PLR Resource 的尺寸（兼容 size_x 和 _size_x）。"""
    val = getattr(resource, attr, None)
    if val is None:
        val = getattr(resource, f"_{attr}", None)
    if val is None:
        val = getattr(resource, f"get_{attr}", lambda: 0)()
    return float(val or 0)


def _extract_grid_from_children(resource) -> Optional[Dict[str, Any]]:
    """从 resource.children 提取网格信息。"""
    children = getattr(resource, "children", None) or []
    if not children:
        return None

    # 获取 num_items_x, num_items_y
    num_x = getattr(resource, "num_items_x", None)
    num_y = getattr(resource, "num_items_y", None)
    if num_x is None or num_y is None:
        return None

    c0 = children[0]
    loc0 = getattr(c0, "location", None)
    dx = loc0.x if loc0 else 0.0
    dy_raw = loc0.y if loc0 else 0.0  # 这是 PLR 布局后的位置，不是输入参数
    dz = loc0.z if loc0 else 0.0

    # 推算 item_dx, item_dy
    item_dx = 9.0
    item_dy = 9.0
    if len(children) > 1:
        c1 = children[1]
        loc1 = getattr(c1, "location", None)
        if loc1 and loc0:
            diff_x = abs(loc1.x - loc0.x)
            diff_y = abs(loc1.y - loc0.y)
            if diff_x > 0.1:
                item_dx = diff_x
            if diff_y > 0.1:
                item_dy = diff_y

    # 如果 num_items_y > 1 且 num_items_x > 1, 找列间距
    if int(num_y) > 1 and int(num_x) > 1 and len(children) >= int(num_y) + 1:
        cn = children[int(num_y)]
        locn = getattr(cn, "location", None)
        if locn and loc0:
            col_diff = abs(locn.x - loc0.x)
            row_diff = abs(children[1].location.y - loc0.y) if len(children) > 1 else item_dy
            if col_diff > 0.1:
                item_dx = col_diff
            if row_diff > 0.1:
                item_dy = row_diff

    # PLR create_ordered_items_2d 的 Y 轴排列是倒序的：
    # child[0].y = dy_param + (num_y - 1) * item_dy  (最上面一行)
    # 因此反推原始 dy 参数：
    dy = dy_raw - (int(num_y) - 1) * item_dy

    return {
        "num_items_x": int(num_x),
        "num_items_y": int(num_y),
        "dx": round(dx, 4),
        "dy": round(dy, 4),
        "dz": round(dz, 4),
        "item_dx": round(item_dx, 4),
        "item_dy": round(item_dy, 4),
    }


def _extract_well_info(child) -> Dict[str, Any]:
    """从 Well/TipSpot/Tube 子对象提取信息。"""
    # material_z_thickness 在 PLR 中如果未设置会抛 NotImplementedError
    mzt = None
    try:
        mzt = child.material_z_thickness
    except (NotImplementedError, AttributeError):
        mzt = getattr(child, "_material_z_thickness", None)

    return {
        "size_x": round(_get_size(child, "size_x"), 4),
        "size_y": round(_get_size(child, "size_y"), 4),
        "size_z": round(_get_size(child, "size_z"), 4),
        "max_volume": getattr(child, "max_volume", None),
        "bottom_type": getattr(child, "bottom_type", None),
        "cross_section_type": getattr(child, "cross_section_type", None),
        "material_z_thickness": mzt,
    }


def import_from_code() -> LabwareDB:
    """执行完整的导入流程，返回 LabwareDB 对象。"""
    # 1. 加载 registry 信息
    reg_info = _load_registry_info()

    # 2. AST 解析源码
    ast_info = _parse_ast_info()

    # 3. 导入工厂模块（通过包路径避免 relative import 问题）
    import importlib
    mod = importlib.import_module("unilabos.devices.liquid_handling.prcxi.prcxi_labware")

    # 4. 获取 PRCXI_TEMPLATE_FACTORY_KINDS 列出的函数
    factory_kinds = getattr(mod, "PRCXI_TEMPLATE_FACTORY_KINDS", [])
    template_func_names = {f.__name__ for f, _k in factory_kinds}

    # 5. 收集所有 PRCXI_ 开头的工厂函数
    all_factories: List[Tuple[str, Any]] = []
    for attr_name in dir(mod):
        if attr_name.startswith("PRCXI_") and not attr_name.startswith("_"):
            obj = getattr(mod, attr_name)
            if callable(obj) and not isinstance(obj, type):
                all_factories.append((attr_name, obj))

    # 按源码行号排序
    all_factories.sort(key=lambda x: getattr(x[1], "__code__", None) and x[1].__code__.co_firstlineno or 0)

    items: List[LabwareItem] = []

    for func_name, factory in all_factories:
        try:
            resource = _probe_factory(factory)
        except Exception as e:
            print(f"跳过 {func_name}: {e}")
            continue

        # 确定类型
        type_name = "plate"
        class_name = type(resource).__name__
        if "TipRack" in class_name:
            type_name = "tip_rack"
        elif "Trash" in class_name:
            type_name = "trash"
        elif "TubeRack" in class_name:
            type_name = "tube_rack"
        elif "PlateAdapter" in class_name:
            type_name = "plate_adapter"

        # material_info
        state = getattr(resource, "_unilabos_state", {}) or {}
        mat = state.get("Material", {})
        mat_info = MaterialInfo(
            uuid=mat.get("uuid", ""),
            Code=mat.get("Code", ""),
            Name=mat.get("Name", ""),
            materialEnum=mat.get("materialEnum"),
            SupplyType=mat.get("SupplyType"),
        )

        # AST 信息
        ast_data = ast_info.get(func_name, {})
        docstring = ast_data.get("docstring", "")
        plate_type = ast_data.get("plate_type")

        # Registry 信息
        reg = reg_info.get(func_name, {})
        registry_category = reg.get("registry_category", ["prcxi", _type_to_yaml_subcategory(type_name)])
        registry_description = reg.get("registry_description", f'{mat_info.Name} (Code: {mat_info.Code})')

        # 构建 item
        item = LabwareItem(
            id=func_name.lower().replace("prcxi_", "")[:8] or func_name[:8],
            type=type_name,
            function_name=func_name,
            docstring=docstring,
            size_x=round(_get_size(resource, "size_x"), 4),
            size_y=round(_get_size(resource, "size_y"), 4),
            size_z=round(_get_size(resource, "size_z"), 4),
            model=getattr(resource, "model", None),
            category=getattr(resource, "category", type_name),
            plate_type=plate_type,
            material_info=mat_info,
            registry_category=registry_category,
            registry_description=registry_description,
            include_in_template_matching=func_name in template_func_names,
            template_kind=_TEMPLATE_KINDS.get(func_name),
        )

        # 提取子项信息
        children = getattr(resource, "children", None) or []
        grid_data = _extract_grid_from_children(resource)

        if type_name == "plate" and children:
            if grid_data:
                item.grid = GridInfo(**grid_data)
            c0 = children[0]
            well_data = _extract_well_info(c0)
            bt = well_data.get("bottom_type")
            if bt is not None:
                bt = bt.name if hasattr(bt, "name") else str(bt)
            else:
                bt = "FLAT"
            cst = well_data.get("cross_section_type")
            if cst is not None:
                cst = cst.name if hasattr(cst, "name") else str(cst)
            else:
                cst = "CIRCLE"
            item.well = WellInfo(
                size_x=well_data["size_x"],
                size_y=well_data["size_y"],
                size_z=well_data["size_z"],
                max_volume=well_data.get("max_volume"),
                bottom_type=bt,
                cross_section_type=cst,
                material_z_thickness=well_data.get("material_z_thickness"),
            )
            # volume_functions
            vf = ast_data.get("volume_functions")
            if vf:
                # 需要实际获取 well 尺寸作为 volume_function 参数
                item.volume_functions = VolumeFunctions(
                    type="rectangle",
                    well_length=well_data["size_x"],
                    well_width=well_data["size_y"],
                )

        elif type_name == "tip_rack" and children:
            if grid_data:
                item.grid = GridInfo(**grid_data)
            c0 = children[0]
            tip_obj = getattr(c0, "tip", None)
            tip_volume = 300.0
            tip_length = 60.0
            tip_depth = 51.0
            tip_filter = False
            if tip_obj:
                tip_volume = getattr(tip_obj, "maximal_volume", 300.0)
                tip_length = getattr(tip_obj, "total_tip_length", 60.0)
                tip_depth = getattr(tip_obj, "fitting_depth", 51.0)
                tip_filter = getattr(tip_obj, "has_filter", False)
            item.tip = TipInfo(
                spot_size_x=round(_get_size(c0, "size_x"), 4),
                spot_size_y=round(_get_size(c0, "size_y"), 4),
                spot_size_z=round(_get_size(c0, "size_z"), 4),
                tip_volume=tip_volume,
                tip_length=tip_length,
                tip_fitting_depth=tip_depth,
                has_filter=tip_filter,
            )
            # 计算 tip_above_rack_length = tip_length - (size_z - dz)
            if grid_data:
                _dz = grid_data.get("dz", 0.0)
                _above = tip_length - (item.size_z - _dz)
                item.tip.tip_above_rack_length = round(_above, 4) if _above > 0 else None

        elif type_name == "tube_rack" and children:
            if grid_data:
                item.grid = GridInfo(**grid_data)
            c0 = children[0]
            item.tube = TubeInfo(
                size_x=round(_get_size(c0, "size_x"), 4),
                size_y=round(_get_size(c0, "size_y"), 4),
                size_z=round(_get_size(c0, "size_z"), 4),
                max_volume=getattr(c0, "max_volume", 1500.0) or 1500.0,
            )

        elif type_name == "plate_adapter":
            # 提取 adapter 参数
            ahx = getattr(resource, "adapter_hole_size_x", 127.76)
            ahy = getattr(resource, "adapter_hole_size_y", 85.48)
            ahz = getattr(resource, "adapter_hole_size_z", 10.0)
            adx = getattr(resource, "dx", None)
            ady = getattr(resource, "dy", None)
            adz = getattr(resource, "dz", 0.0)
            item.adapter = AdapterInfo(
                adapter_hole_size_x=ahx,
                adapter_hole_size_y=ahy,
                adapter_hole_size_z=ahz,
                dx=adx,
                dy=ady,
                dz=adz,
            )

        items.append(item)

    return LabwareDB(items=items)


def _type_to_yaml_subcategory(type_name: str) -> str:
    mapping = {
        "plate": "plates",
        "tip_rack": "tip_racks",
        "trash": "trash",
        "tube_rack": "tube_racks",
        "plate_adapter": "plate_adapters",
    }
    return mapping.get(type_name, type_name)


def save_db(db: LabwareDB, path: Optional[Path] = None) -> Path:
    """保存 LabwareDB 到 JSON 文件。"""
    out = path or _DB_PATH
    with open(out, "w", encoding="utf-8") as f:
        json.dump(db.model_dump(), f, ensure_ascii=False, indent=2)
    return out


def load_db(path: Optional[Path] = None) -> LabwareDB:
    """从 JSON 文件加载 LabwareDB。"""
    src = path or _DB_PATH
    if not src.exists():
        return LabwareDB()
    with open(src, "r", encoding="utf-8") as f:
        return LabwareDB(**json.load(f))


if __name__ == "__main__":
    db = import_from_code()
    out = save_db(db)
    print(f"已导入 {len(db.items)} 个耗材 → {out}")
    for item in db.items:
        print(f"  [{item.type:14s}] {item.function_name}")
