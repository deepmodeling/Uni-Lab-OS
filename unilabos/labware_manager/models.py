"""Pydantic 数据模型，描述所有 PRCXI 耗材类型的 JSON 结构。"""

from __future__ import annotations

import uuid as _uuid
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class MaterialInfo(BaseModel):
    uuid: str = ""
    Code: str = ""
    Name: str = ""
    materialEnum: Optional[int] = None
    SupplyType: Optional[int] = None


class GridInfo(BaseModel):
    """孔位网格排列参数"""
    num_items_x: int = 12
    num_items_y: int = 8
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0
    item_dx: float = 9.0
    item_dy: float = 9.0


class WellInfo(BaseModel):
    """孔参数 (Plate)"""
    size_x: float = 8.0
    size_y: float = 8.0
    size_z: float = 10.0
    max_volume: Optional[float] = None
    bottom_type: str = "FLAT"          # V / U / FLAT
    cross_section_type: str = "CIRCLE"  # CIRCLE / RECTANGLE
    material_z_thickness: Optional[float] = None


class VolumeFunctions(BaseModel):
    """体积-高度计算函数参数 (矩形 well)"""
    type: str = "rectangle"
    well_length: float = 0.0
    well_width: float = 0.0


class TipInfo(BaseModel):
    """枪头参数 (TipRack)"""
    spot_size_x: float = 7.0
    spot_size_y: float = 7.0
    spot_size_z: float = 0.0
    tip_volume: float = 300.0
    tip_length: float = 60.0
    tip_fitting_depth: float = 51.0
    tip_above_rack_length: Optional[float] = None
    has_filter: bool = False


class TubeInfo(BaseModel):
    """管参数 (TubeRack)"""
    size_x: float = 10.6
    size_y: float = 10.6
    size_z: float = 40.0
    max_volume: float = 1500.0


class AdapterInfo(BaseModel):
    """适配器参数 (PlateAdapter)"""
    adapter_hole_size_x: float = 127.76
    adapter_hole_size_y: float = 85.48
    adapter_hole_size_z: float = 10.0
    dx: Optional[float] = None
    dy: Optional[float] = None
    dz: float = 0.0


LabwareType = Literal["plate", "tip_rack", "trash", "tube_rack", "plate_adapter"]


class LabwareItem(BaseModel):
    """一个耗材条目的完整 JSON 表示"""

    id: str = Field(default_factory=lambda: _uuid.uuid4().hex[:8])
    type: LabwareType = "plate"
    function_name: str = ""
    docstring: str = ""

    # 物理尺寸
    size_x: float = 127.0
    size_y: float = 85.0
    size_z: float = 20.0
    model: Optional[str] = None
    category: Optional[str] = None
    plate_type: Optional[str] = None  # non-skirted / semi-skirted / skirted

    # 材料信息
    material_info: MaterialInfo = Field(default_factory=MaterialInfo)

    # Registry 字段
    registry_category: List[str] = Field(default_factory=lambda: ["prcxi", "plates"])
    registry_description: str = ""

    # Plate 特有
    grid: Optional[GridInfo] = None
    well: Optional[WellInfo] = None
    volume_functions: Optional[VolumeFunctions] = None

    # TipRack 特有
    tip: Optional[TipInfo] = None

    # TubeRack 特有
    tube: Optional[TubeInfo] = None

    # PlateAdapter 特有
    adapter: Optional[AdapterInfo] = None

    # 模板匹配
    include_in_template_matching: bool = False
    template_kind: Optional[str] = None


class LabwareDB(BaseModel):
    """整个 labware_db.json 的结构"""
    version: str = "1.0"
    items: List[LabwareItem] = Field(default_factory=list)
