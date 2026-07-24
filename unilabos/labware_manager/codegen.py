"""JSON → prcxi_labware.py 代码生成。

读取 labware_db.json，输出完整的 prcxi_labware.py（或 prcxi_labware_test.py）。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

from unilabos.labware_manager.models import LabwareDB, LabwareItem

_TARGET_DIR = Path(__file__).resolve().parents[1] / "devices" / "liquid_handling" / "prcxi"

# ---------- 固定头部 ----------
_HEADER = '''\
from typing import Any, Callable, Dict, List, Optional, Tuple
from pylabrobot.resources import Tube, Coordinate
from pylabrobot.resources.well import Well, WellBottomType, CrossSectionType
from pylabrobot.resources.tip import Tip, TipCreator
from pylabrobot.resources.tip_rack import TipRack, TipSpot
from pylabrobot.resources.utils import create_ordered_items_2d
from pylabrobot.resources.height_volume_functions import (
    compute_height_from_volume_rectangle,
    compute_volume_from_height_rectangle,
)

from .prcxi import PRCXI9300Plate, PRCXI9300TipRack, PRCXI9300Trash, PRCXI9300TubeRack, PRCXI9300PlateAdapter

def _make_tip_helper(volume: float, length: float, depth: float) -> Tip:
    """
    PLR 的 Tip 类参数名为: maximal_volume, total_tip_length, fitting_depth
    """
    return Tip(
        has_filter=False, # 默认无滤芯
        maximal_volume=volume,
        total_tip_length=length,
        fitting_depth=depth
    )

'''


def _gen_plate(item: LabwareItem) -> str:
    """生成 Plate 类型的工厂函数代码。"""
    lines = []
    fn = item.function_name
    doc = item.docstring or f"Code: {item.material_info.Code}"

    has_vf = item.volume_functions is not None

    if has_vf:
        # 有 volume_functions 时需要 well_kwargs 方式
        vf = item.volume_functions
        well = item.well
        grid = item.grid

        lines.append(f'def {fn}(name: str) -> PRCXI9300Plate:')
        lines.append(f'    """')
        for dl in doc.split('\n'):
            lines.append(f'    {dl}')
        lines.append(f'    """')

        # 计算 well_size 变量
        lines.append(f'    well_size_x = {well.size_x}')
        lines.append(f'    well_size_y = {well.size_y}')

        lines.append(f'    well_kwargs = {{')
        lines.append(f'        "size_x": well_size_x,')
        lines.append(f'        "size_y": well_size_y,')
        lines.append(f'        "size_z": {well.size_z},')
        lines.append(f'        "bottom_type": WellBottomType.{well.bottom_type},')
        if well.cross_section_type and well.cross_section_type != "CIRCLE":
            lines.append(f'        "cross_section_type": CrossSectionType.{well.cross_section_type},')
        lines.append(f'        "compute_height_from_volume": lambda liquid_volume: compute_height_from_volume_rectangle(')
        lines.append(f'            liquid_volume=liquid_volume, well_length=well_size_x, well_width=well_size_y')
        lines.append(f'        ),')
        lines.append(f'        "compute_volume_from_height": lambda liquid_height: compute_volume_from_height_rectangle(')
        lines.append(f'            liquid_height=liquid_height, well_length=well_size_x, well_width=well_size_y')
        lines.append(f'        ),')
        if well.material_z_thickness is not None:
            lines.append(f'        "material_z_thickness": {well.material_z_thickness},')
        lines.append(f'    }}')
        lines.append(f'')
        lines.append(f'    return PRCXI9300Plate(')
        lines.append(f'        name=name,')
        lines.append(f'        size_x={item.size_x},')
        lines.append(f'        size_y={item.size_y},')
        lines.append(f'        size_z={item.size_z},')
        lines.append(f'        lid=None,')
        lines.append(f'        model="{item.model}",')
        lines.append(f'        category="plate",')
        lines.append(f'        material_info={_fmt_dict(item.material_info.model_dump(exclude_none=True))},')
        lines.append(f'        ordered_items=create_ordered_items_2d(')
        lines.append(f'            Well,')
        lines.append(f'            num_items_x={grid.num_items_x},')
        lines.append(f'            num_items_y={grid.num_items_y},')
        lines.append(f'            dx={grid.dx},')
        lines.append(f'            dy={grid.dy},')
        lines.append(f'            dz={grid.dz},')
        lines.append(f'            item_dx={grid.item_dx},')
        lines.append(f'            item_dy={grid.item_dy},')
        lines.append(f'            **well_kwargs,')
        lines.append(f'        ),')
        lines.append(f'    )')
    else:
        # 普通 plate
        well = item.well
        grid = item.grid

        lines.append(f'def {fn}(name: str) -> PRCXI9300Plate:')
        lines.append(f'    """')
        for dl in doc.split('\n'):
            lines.append(f'    {dl}')
        lines.append(f'    """')
        lines.append(f'    return PRCXI9300Plate(')
        lines.append(f'        name=name,')
        lines.append(f'        size_x={item.size_x},')
        lines.append(f'        size_y={item.size_y},')
        lines.append(f'        size_z={item.size_z},')
        if item.plate_type:
            lines.append(f'        plate_type="{item.plate_type}",')
        lines.append(f'        model="{item.model}",')
        lines.append(f'        category="plate",')
        lines.append(f'        material_info={_fmt_dict(item.material_info.model_dump(exclude_none=True))},')
        if grid and well:
            lines.append(f'        ordered_items=create_ordered_items_2d(')
            lines.append(f'            Well,')
            lines.append(f'            num_items_x={grid.num_items_x},')
            lines.append(f'            num_items_y={grid.num_items_y},')
            lines.append(f'            dx={grid.dx},')
            lines.append(f'            dy={grid.dy},')
            lines.append(f'            dz={grid.dz},')
            lines.append(f'            item_dx={grid.item_dx},')
            lines.append(f'            item_dy={grid.item_dy},')
            lines.append(f'            size_x={well.size_x},')
            lines.append(f'            size_y={well.size_y},')
            lines.append(f'            size_z={well.size_z},')
            if well.max_volume is not None:
                lines.append(f'            max_volume={well.max_volume},')
            if well.material_z_thickness is not None:
                lines.append(f'            material_z_thickness={well.material_z_thickness},')
            if well.bottom_type and well.bottom_type != "FLAT":
                lines.append(f'            bottom_type=WellBottomType.{well.bottom_type},')
            if well.cross_section_type:
                lines.append(f'            cross_section_type=CrossSectionType.{well.cross_section_type},')
            lines.append(f'        ),')
        lines.append(f'    )')

    return '\n'.join(lines)


def _gen_tip_rack(item: LabwareItem) -> str:
    """生成 TipRack 工厂函数代码。"""
    lines = []
    fn = item.function_name
    doc = item.docstring or f"Code: {item.material_info.Code}"
    grid = item.grid
    tip = item.tip

    lines.append(f'def {fn}(name: str) -> PRCXI9300TipRack:')
    lines.append(f'    """')
    for dl in doc.split('\n'):
        lines.append(f'    {dl}')
    lines.append(f'    """')
    lines.append(f'    return PRCXI9300TipRack(')
    lines.append(f'        name=name,')
    lines.append(f'        size_x={item.size_x},')
    lines.append(f'        size_y={item.size_y},')
    lines.append(f'        size_z={item.size_z},')
    lines.append(f'        model="{item.model}",')
    lines.append(f'        material_info={_fmt_dict(item.material_info.model_dump(exclude_none=True))},')
    if grid and tip:
        lines.append(f'        ordered_items=create_ordered_items_2d(')
        lines.append(f'            TipSpot,')
        lines.append(f'            num_items_x={grid.num_items_x},')
        lines.append(f'            num_items_y={grid.num_items_y},')
        lines.append(f'            dx={grid.dx},')
        lines.append(f'            dy={grid.dy},')
        lines.append(f'            dz={grid.dz},')
        lines.append(f'            item_dx={grid.item_dx},')
        lines.append(f'            item_dy={grid.item_dy},')
        lines.append(f'            size_x={tip.spot_size_x},')
        lines.append(f'            size_y={tip.spot_size_y},')
        lines.append(f'            size_z={tip.spot_size_z},')
        lines.append(f'            make_tip=lambda: _make_tip_helper(volume={tip.tip_volume}, length={tip.tip_length}, depth={tip.tip_fitting_depth})')
        lines.append(f'        )')
    lines.append(f'    )')

    return '\n'.join(lines)


def _gen_trash(item: LabwareItem) -> str:
    """生成 Trash 工厂函数代码。"""
    lines = []
    fn = item.function_name
    doc = item.docstring or f"Code: {item.material_info.Code}"

    lines.append(f'def {fn}(name: str = "trash") -> PRCXI9300Trash:')
    lines.append(f'    """')
    for dl in doc.split('\n'):
        lines.append(f'    {dl}')
    lines.append(f'    """')
    lines.append(f'    return PRCXI9300Trash(')
    lines.append(f'        name="trash",')
    lines.append(f'        size_x={item.size_x},')
    lines.append(f'        size_y={item.size_y},')
    lines.append(f'        size_z={item.size_z},')
    lines.append(f'        category="trash",')
    lines.append(f'        model="{item.model}",')
    lines.append(f'        material_info={_fmt_dict(item.material_info.model_dump(exclude_none=True))}')
    lines.append(f'    )')

    return '\n'.join(lines)


def _gen_tube_rack(item: LabwareItem) -> str:
    """生成 TubeRack 工厂函数代码。"""
    lines = []
    fn = item.function_name
    doc = item.docstring or f"Code: {item.material_info.Code}"
    grid = item.grid
    tube = item.tube

    lines.append(f'def {fn}(name: str) -> PRCXI9300TubeRack:')
    lines.append(f'    """')
    for dl in doc.split('\n'):
        lines.append(f'    {dl}')
    lines.append(f'    """')
    lines.append(f'    return PRCXI9300TubeRack(')
    lines.append(f'        name=name,')
    lines.append(f'        size_x={item.size_x},')
    lines.append(f'        size_y={item.size_y},')
    lines.append(f'        size_z={item.size_z},')
    lines.append(f'        model="{item.model}",')
    lines.append(f'        category="tube_rack",')
    lines.append(f'        material_info={_fmt_dict(item.material_info.model_dump(exclude_none=True))},')
    if grid and tube:
        lines.append(f'        ordered_items=create_ordered_items_2d(')
        lines.append(f'            Tube,')
        lines.append(f'            num_items_x={grid.num_items_x},')
        lines.append(f'            num_items_y={grid.num_items_y},')
        lines.append(f'            dx={grid.dx},')
        lines.append(f'            dy={grid.dy},')
        lines.append(f'            dz={grid.dz},')
        lines.append(f'            item_dx={grid.item_dx},')
        lines.append(f'            item_dy={grid.item_dy},')
        lines.append(f'            size_x={tube.size_x},')
        lines.append(f'            size_y={tube.size_y},')
        lines.append(f'            size_z={tube.size_z},')
        lines.append(f'            max_volume={tube.max_volume}')
        lines.append(f'        )')
    lines.append(f'    )')

    return '\n'.join(lines)


def _gen_plate_adapter(item: LabwareItem) -> str:
    """生成 PlateAdapter 工厂函数代码。"""
    lines = []
    fn = item.function_name
    doc = item.docstring or f"Code: {item.material_info.Code}"

    lines.append(f'def {fn}(name: str) -> PRCXI9300PlateAdapter:')
    lines.append(f'    """ {doc} """')
    lines.append(f'    return PRCXI9300PlateAdapter(')
    lines.append(f'        name=name,')
    lines.append(f'        size_x={item.size_x},')
    lines.append(f'        size_y={item.size_y},')
    lines.append(f'        size_z={item.size_z},')
    if item.model:
        lines.append(f'        model="{item.model}",')
    lines.append(f'        material_info={_fmt_dict(item.material_info.model_dump(exclude_none=True))}')
    lines.append(f'    )')

    return '\n'.join(lines)


def _fmt_dict(d: dict) -> str:
    """格式化字典为 Python 代码片段。"""
    parts = []
    for k, v in d.items():
        if isinstance(v, str):
            parts.append(f'"{k}": "{v}"')
        elif v is None:
            continue
        else:
            parts.append(f'"{k}": {v}')
    return '{' + ', '.join(parts) + '}'


def _gen_template_factory_kinds(items: List[LabwareItem]) -> str:
    """生成 PRCXI_TEMPLATE_FACTORY_KINDS 列表。"""
    lines = ['PRCXI_TEMPLATE_FACTORY_KINDS: List[Tuple[Callable[..., Any], str]] = [']
    for item in items:
        if item.include_in_template_matching and item.template_kind:
            lines.append(f'    ({item.function_name}, "{item.template_kind}"),')
    lines.append(']')
    return '\n'.join(lines)


def _gen_footer() -> str:
    """生成文件尾部的模板相关代码。"""
    return '''

# ---------------------------------------------------------------------------
# 协议上传 / workflow 用：与设备端耗材字典字段对齐的模板描述（供 common 自动匹配）
# ---------------------------------------------------------------------------

_PRCXI_TEMPLATE_SPECS_CACHE: Optional[List[Dict[str, Any]]] = None


def _probe_prcxi_resource(factory: Callable[..., Any]) -> Any:
    probe = "__unilab_template_probe__"
    if factory.__name__ == "PRCXI_trash":
        return factory()
    return factory(probe)


def _first_child_capacity_for_match(resource: Any) -> float:
    """Well max_volume 或 Tip 的 maximal_volume，用于与设备端 Volume 类似的打分。"""
    ch = getattr(resource, "children", None) or []
    if not ch:
        return 0.0
    c0 = ch[0]
    mv = getattr(c0, "max_volume", None)
    if mv is not None:
        return float(mv)
    tip = getattr(c0, "tip", None)
    if tip is not None:
        mv2 = getattr(tip, "maximal_volume", None)
        if mv2 is not None:
            return float(mv2)
    return 0.0


def get_prcxi_labware_template_specs() -> List[Dict[str, Any]]:
    """返回与 ``prcxi._match_and_create_matrix`` 中耗材字段兼容的模板列表，用于按孔数+容量打分。"""
    global _PRCXI_TEMPLATE_SPECS_CACHE
    if _PRCXI_TEMPLATE_SPECS_CACHE is not None:
        return _PRCXI_TEMPLATE_SPECS_CACHE

    out: List[Dict[str, Any]] = []
    for factory, kind in PRCXI_TEMPLATE_FACTORY_KINDS:
        try:
            r = _probe_prcxi_resource(factory)
        except Exception:
            continue
        nx = int(getattr(r, "num_items_x", None) or 0)
        ny = int(getattr(r, "num_items_y", None) or 0)
        nchild = len(getattr(r, "children", []) or [])
        hole_count = nx * ny if nx > 0 and ny > 0 else nchild
        hole_row = ny if nx > 0 and ny > 0 else 0
        hole_col = nx if nx > 0 and ny > 0 else 0
        mi = getattr(r, "material_info", None) or {}
        vol = _first_child_capacity_for_match(r)
        menum = mi.get("materialEnum")
        if menum is None and kind == "tip_rack":
            menum = 1
        elif menum is None and kind == "trash":
            menum = 6
        out.append(
            {
                "class_name": factory.__name__,
                "kind": kind,
                "materialEnum": menum,
                "HoleRow": hole_row,
                "HoleColum": hole_col,
                "Volume": vol,
                "hole_count": hole_count,
                "material_uuid": mi.get("uuid"),
                "material_code": mi.get("Code"),
            }
        )

    _PRCXI_TEMPLATE_SPECS_CACHE = out
    return out
'''


def generate_code(db: LabwareDB, test_mode: bool = True) -> Path:
    """生成 prcxi_labware.py (或 _test.py)，返回输出文件路径。"""
    suffix = "_test" if test_mode else ""
    out_path = _TARGET_DIR / f"prcxi_labware{suffix}.py"

    # 备份
    if out_path.exists():
        bak = out_path.with_suffix(".py.bak")
        shutil.copy2(out_path, bak)

    # 按类型分组的生成器
    generators = {
        "plate": _gen_plate,
        "tip_rack": _gen_tip_rack,
        "trash": _gen_trash,
        "tube_rack": _gen_tube_rack,
        "plate_adapter": _gen_plate_adapter,
    }

    # 按 type 分段
    sections = {
        "plate": [],
        "tip_rack": [],
        "trash": [],
        "tube_rack": [],
        "plate_adapter": [],
    }

    for item in db.items:
        gen = generators.get(item.type)
        if gen:
            sections[item.type].append(gen(item))

    # 组装完整文件
    parts = [_HEADER]

    section_titles = {
        "plate": "# =========================================================================\n# Plates\n# =========================================================================",
        "tip_rack": "# =========================================================================\n# Tip Racks\n# =========================================================================",
        "trash": "# =========================================================================\n# Trash\n# =========================================================================",
        "tube_rack": "# =========================================================================\n# Tube Racks\n# =========================================================================",
        "plate_adapter": "# =========================================================================\n# Plate Adapters\n# =========================================================================",
    }

    for type_key in ["plate", "tip_rack", "trash", "tube_rack", "plate_adapter"]:
        if sections[type_key]:
            parts.append(section_titles[type_key])
            for code in sections[type_key]:
                parts.append(code)

    # Template factory kinds
    parts.append("")
    parts.append(_gen_template_factory_kinds(db.items))

    # Footer
    parts.append(_gen_footer())

    content = '\n'.join(parts)
    out_path.write_text(content, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    from unilabos.labware_manager.importer import load_db
    db = load_db()
    if not db.items:
        print("labware_db.json 为空，请先运行 importer.py")
    else:
        out = generate_code(db, test_mode=True)
        print(f"已生成 {out} ({len(db.items)} 个工厂函数)")
