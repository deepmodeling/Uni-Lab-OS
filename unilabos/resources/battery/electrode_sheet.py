from typing import Any, Dict, Optional, TypedDict

from pylabrobot.resources import Resource as ResourcePLR
from pylabrobot.resources import Container


electrode_colors = {
    "PositiveCan": "#ff0000",
    "PositiveElectrode": "#cc3333",
    "NegativeCan": "#000000",
    "NegativeElectrode": "#666666",
    "SpringWasher": "#8b7355",
    "FlatWasher": "a9a9a9",
    "AluminumFoil": "#ffcccc",
    "Battery": "#00ff00",
}

class ElectrodeSheetState(TypedDict):
    diameter: float  # 直径 (mm)
    thickness: float  # 厚度 (mm)
    mass: float  # 质量 (g)
    material_type: str  # 材料类型（铜、铝、不锈钢、弹簧钢等）
    color: str  # 材料类型对应的颜色
    info: Optional[str]  # 附加信息


class ElectrodeSheet(ResourcePLR):
    """极片类 - 包含正负极片、隔膜、弹片、垫片、铝箔等所有片状材料"""

    def __init__(
        self,
        name: str = "极片",
        size_x: float = 10,
        size_y: float = 10,
        size_z: float = 10,
        category: str = "electrode_sheet",
        model: Optional[str] = None,
        **kwargs
    ):
        """初始化极片

        Args:
            name: 极片名称
            size_x: 长度 (mm)
            size_y: 宽度 (mm)
            size_z: 高度 (mm)
            category: 类别
            model: 型号
            **kwargs: 其他参数传递给父类
        """
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            category=category,
            model=model,
            **kwargs
        )
        self._unilabos_state: ElectrodeSheetState = ElectrodeSheetState(
            diameter=14,
            thickness=0.1,
            mass=0.5,
            material_type="copper",
            color="#8b4513",
            info=None
        )

    # TODO: 这个还要不要？给self._unilabos_state赋值的？
    def load_state(self, state: Dict[str, Any]) -> None:
        """格式不变"""
        super().load_state(state)
        self._unilabos_state = state
    #序列化
    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(self._unilabos_state)  # Container自身的信息，云端物料将保存这一data，本地也通过这里的data进行读写，当前类用来表示这个物料的长宽高大小的属性，而data（state用来表示物料的内容，细节等）
        return data


def PositiveCan(name: str) -> ElectrodeSheet:
    """创建正极壳"""
    sheet = ElectrodeSheet(name=name, size_x=12, size_y=12, size_z=3.0, model="PositiveCan")
    sheet.load_state({"diameter": 20.0, "thickness": 0.5, "mass": 0.5, "material_type": "aluminum", "color": electrode_colors["PositiveCan"], "info": None})
    return sheet


def PositiveElectrode(name: str) -> ElectrodeSheet:
    """创建正极片"""
    sheet = ElectrodeSheet(name=name, size_x=10, size_y=10, size_z=0.1, model="PositiveElectrode")
    sheet.load_state({"material_type": "positive_electrode", "color": electrode_colors["PositiveElectrode"]})
    return sheet


def NegativeCan(name: str) -> ElectrodeSheet:
    """创建负极壳"""
    sheet = ElectrodeSheet(name=name, size_x=12, size_y=12, size_z=2.0, model="NegativeCan")
    sheet.load_state({"material_type": "steel", "color": electrode_colors["NegativeCan"]})
    return sheet


def NegativeElectrode(name: str) -> ElectrodeSheet:
    """创建负极片"""
    sheet = ElectrodeSheet(name=name, size_x=10, size_y=10, size_z=0.1, model="NegativeElectrode")
    sheet.load_state({"material_type": "negative_electrode", "color": electrode_colors["NegativeElectrode"]})
    return sheet


def SpringWasher(name: str) -> ElectrodeSheet:
    """创建弹片"""
    sheet = ElectrodeSheet(name=name, size_x=10, size_y=10, size_z=0.5, model="SpringWasher")
    sheet.load_state({"material_type": "spring_steel", "color": electrode_colors["SpringWasher"]})
    return sheet


def FlatWasher(name: str) -> ElectrodeSheet:
    """创建垫片"""
    sheet = ElectrodeSheet(name=name, size_x=10, size_y=10, size_z=0.2, model="FlatWasher")
    sheet.load_state({"material_type": "steel", "color": electrode_colors["FlatWasher"]})
    return sheet


def AluminumFoil(name: str) -> ElectrodeSheet:
    """创建铝箔"""
    sheet = ElectrodeSheet(name=name, size_x=10, size_y=10, size_z=0.05, model="AluminumFoil")
    sheet.load_state({"material_type": "aluminum", "color": electrode_colors["AluminumFoil"]})
    return sheet


class BatteryState(TypedDict):
    color: str  # 材料类型对应的颜色
    electrolyte_name: str
    data_electrolyte_code: str
    open_circuit_voltage: float
    assembly_pressure: float
    electrolyte_volume: float

    info: Optional[str]  # 附加信息


class Battery(Container):
    """电池类 - 包含组装好的电池"""

    def __init__(
        self,
        name: str = "电池",
        size_x: float = 12,
        size_y: float = 12,
        size_z: float = 6,
        category: str = "battery",
        model: Optional[str] = None,
        **kwargs
    ):
        """初始化电池

        Args:
            name: 电池名称
            size_x: 长度 (mm)
            size_y: 宽度 (mm)
            size_z: 高度 (mm)
            category: 类别
            model: 型号
            **kwargs: 其他参数传递给父类
        """
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            category=category,
            model=model,
            **kwargs
        )
        self._unilabos_state: BatteryState = BatteryState(
            color=electrode_colors["Battery"],
            electrolyte_name="无",
            data_electrolyte_code="",
            open_circuit_voltage=0.0,
            assembly_pressure=0.0,
            electrolyte_volume=0.0,
            info=None
        )

    def load_state(self, state: Dict[str, Any]) -> None:
        """格式不变"""
        super().load_state(state)
        self._unilabos_state = state

    #序列化
    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(self._unilabos_state)  # Container自身的信息，云端物料将保存这一data，本地也通过这里的data进行读写，当前类用来表示这个物料的长宽高大小的属性，而data（state用来表示物料的内容，细节等）
        return data