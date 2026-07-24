"""
纽扣电池组装工作站物料类定义
Button Battery Assembly Station Resource Classes
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Optional, TypedDict, Union, cast

from pylabrobot.resources.coordinate import Coordinate
from pylabrobot.resources.container import Container
from pylabrobot.resources.deck import Deck
from pylabrobot.resources.itemized_resource import ItemizedResource
from pylabrobot.resources.resource import Resource
from pylabrobot.resources.resource_stack import ResourceStack
from pylabrobot.resources.tip_rack import TipRack, TipSpot
from pylabrobot.resources.trash import Trash
from pylabrobot.resources.utils import create_ordered_items_2d


class ElectrodeSheetState(TypedDict):
    diameter: float  # 直径 (mm)
    thickness: float  # 厚度 (mm)
    mass: float  # 质量 (g)
    material_type: str  # 材料类型（正极、负极、隔膜、弹片、垫片、铝箔等）
    info: Optional[str]  # 附加信息

class ElectrodeSheet(Resource):
    """极片类 - 包含正负极片、隔膜、弹片、垫片、铝箔等所有片状材料"""

    def __init__(
        self,
        name: str = "极片",
        size_x=10,
        size_y=10,
        size_z=10,
        category: str = "electrode_sheet",
        model: Optional[str] = None,
    ):
        """初始化极片

        Args:
            name: 极片名称
            category: 类别
            model: 型号
        """
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            category=category,
            model=model,
        )
        self._unilabos_state: ElectrodeSheetState = ElectrodeSheetState(
            diameter=14,
            thickness=0.1,
            mass=0.5,
            material_type="copper",
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

# TODO: 这个应该只能放一个极片
class MaterialHoleState(TypedDict):
    diameter: int
    depth: int
    max_sheets: int
    info: Optional[str]  # 附加信息

class MaterialHole(Resource):
    """料板洞位类"""
    children: List[ElectrodeSheet] = []

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        category: str = "material_hole",
        **kwargs
    ):
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            category=category,
        )
        self._unilabos_state: MaterialHoleState = MaterialHoleState(
            diameter=20,
            depth=10,
            max_sheets=1,
            info=None
        )

    def get_all_sheet_info(self):
        info_list = []
        for sheet in self.children:
            info_list.append(sheet._unilabos_state["info"])
        return info_list
    
    #这个函数函数好像没用，一般不会集中赋值质量
    def set_all_sheet_mass(self):
        for sheet in self.children:
            sheet._unilabos_state["mass"] = 0.5  # 示例：设置质量为0.5g

    def load_state(self, state: Dict[str, Any]) -> None:
        """格式不变"""
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(self._unilabos_state)  # Container自身的信息，云端物料将保存这一data，本地也通过这里的data进行读写，当前类用来表示这个物料的长宽高大小的属性，而data（state用来表示物料的内容，细节等）
        return data
    #移动极片前先取出对象
    def get_sheet_with_name(self, name: str) -> Optional[ElectrodeSheet]:
        for sheet in self.children:
            if sheet.name == name:
                return sheet
        return None

    def has_electrode_sheet(self) -> bool:
        """检查洞位是否有极片"""
        return len(self.children) > 0

    def assign_child_resource(
        self,
        resource: ElectrodeSheet,
        location: Optional[Coordinate],
        reassign: bool = True,
    ):
        """放置极片"""
        # TODO: 这里要改，diameter找不到，加入._unilabos_state后应该没问题
        if resource._unilabos_state["diameter"] > self._unilabos_state["diameter"]:
            raise ValueError(f"极片直径 {resource._unilabos_state['diameter']} 超过洞位直径 {self._unilabos_state['diameter']}")
        if len(self.children) >= self._unilabos_state["max_sheets"]:
            raise ValueError(f"洞位已满，无法放置更多极片")
        super().assign_child_resource(resource, location, reassign)

    # 根据children的编号取物料对象。
    def get_electrode_sheet_info(self, index: int) -> ElectrodeSheet:
        return self.children[index]



class MaterialPlateState(TypedDict):
    hole_spacing_x: float
    hole_spacing_y: float
    hole_diameter: float
    info: Optional[str]  # 附加信息



class MaterialPlate(ItemizedResource[MaterialHole]):
    """料板类 - 4x4个洞位，每个洞位放1个极片"""
    
    children: List[MaterialHole]

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        ordered_items: Optional[Dict[str, MaterialHole]] = None,
        ordering: Optional[OrderedDict[str, str]] = None,
        category: str = "material_plate",
        model: Optional[str] = None,
        fill: bool = False
    ):
        """初始化料板

        Args:
            name: 料板名称
            size_x: 长度 (mm)
            size_y: 宽度 (mm)
            size_z: 高度 (mm)
            hole_diameter: 洞直径 (mm)
            hole_depth: 洞深度 (mm)
            hole_spacing_x: X方向洞位间距 (mm)
            hole_spacing_y: Y方向洞位间距 (mm)
            number: 编号
            category: 类别
            model: 型号
        """
        self._unilabos_state: MaterialPlateState = MaterialPlateState(
            hole_spacing_x=24.0,
            hole_spacing_y=24.0,
            hole_diameter=20.0,
            info="",
        )
        # 创建4x4的洞位
        # TODO: 这里要改，对应不同形状
        holes = create_ordered_items_2d(
            klass=MaterialHole,
            num_items_x=4,
            num_items_y=4,
            dx=(size_x - 4 * self._unilabos_state["hole_spacing_x"]) / 2,  # 居中
            dy=(size_y - 4 * self._unilabos_state["hole_spacing_y"]) / 2,  # 居中
            dz=size_z,
            item_dx=self._unilabos_state["hole_spacing_x"],
            item_dy=self._unilabos_state["hole_spacing_y"],
            size_x = 16,
            size_y = 16,
            size_z = 16,
        )
        if fill:
            super().__init__(
                name=name,
                size_x=size_x,
                size_y=size_y,
                size_z=size_z,
                ordered_items=holes,
                category=category,
                model=model,
            )
        else:
            super().__init__(
                name=name,
                size_x=size_x,
                size_y=size_y,
                size_z=size_z,
                ordered_items=ordered_items,
                ordering=ordering,
                category=category,
                model=model,
            )

    def update_locations(self):
        # TODO:调多次相加
        holes = create_ordered_items_2d(
            klass=MaterialHole,
            num_items_x=4,
            num_items_y=4,
            dx=(self._size_x - 3 * self._unilabos_state["hole_spacing_x"]) / 2,  # 居中
            dy=(self._size_y - 3 * self._unilabos_state["hole_spacing_y"]) / 2,  # 居中
            dz=self._size_z,
            item_dx=self._unilabos_state["hole_spacing_x"],
            item_dy=self._unilabos_state["hole_spacing_y"],
            size_x = 1,
            size_y = 1,
            size_z = 1,
        )
        for item, original_item in zip(holes.items(), self.children):
            original_item.location = item[1].location


class PlateSlot(ResourceStack):
    """板槽位类 - 1个槽上能堆放8个板，移板只能操作最上方的板"""

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        max_plates: int = 8,
        category: str = "plate_slot",
        model: Optional[str] = None
    ):
        """初始化板槽位

        Args:
            name: 槽位名称
            max_plates: 最大板数量
            category: 类别
        """
        super().__init__(
            name=name,
            direction="z",  # Z方向堆叠
            resources=[],
        )
        self.max_plates = max_plates
        self.category = category

    def can_add_plate(self) -> bool:
        """检查是否可以添加板"""
        return len(self.children) < self.max_plates

    def add_plate(self, plate: MaterialPlate) -> None:
        """添加料板"""
        if not self.can_add_plate():
            raise ValueError(f"槽位 {self.name} 已满，无法添加更多板")
        self.assign_child_resource(plate)

    def get_top_plate(self) -> MaterialPlate:
        """获取最上方的板"""
        if len(self.children) == 0:
            raise ValueError(f"槽位 {self.name} 为空")
        return cast(MaterialPlate, self.get_top_item())

    def take_top_plate(self) -> MaterialPlate:
        """取出最上方的板"""
        top_plate = self.get_top_plate()
        self.unassign_child_resource(top_plate)
        return top_plate

    def can_access_for_picking(self) -> bool:
        """检查是否可以进行取料操作（只有最上方的板能进行取料操作）"""
        return len(self.children) > 0

    def serialize(self) -> dict:
        return {
            **super().serialize(),
            "max_plates": self.max_plates,
        }


class ClipMagazineHole(Container):
    """子弹夹洞位类"""
    children: List[ElectrodeSheet] = []
    def __init__(
        self,
        name: str,
        diameter: float,
        depth: float,
        category: str = "clip_magazine_hole",
    ):
        """初始化子弹夹洞位

        Args:
            name: 洞位名称
            diameter: 洞直径 (mm)
            depth: 洞深度 (mm)
            category: 类别
        """
        super().__init__(
            name=name,
            size_x=diameter,
            size_y=diameter,
            size_z=depth,
            category=category,
        )
        self.diameter = diameter
        self.depth = depth

    def can_add_sheet(self, sheet: ElectrodeSheet) -> bool:
        """检查是否可以添加极片
        
        根据洞的深度和极片的厚度来判断是否可以添加极片
        """
        # 检查极片直径是否适合洞的直径
        if sheet._unilabos_state["diameter"] > self.diameter:
            return False
        
        # 计算当前已添加极片的总厚度
        current_thickness = sum(s._unilabos_state["thickness"] for s in self.children)
        
        # 检查添加新极片后总厚度是否超过洞的深度
        if current_thickness + sheet._unilabos_state["thickness"] > self.depth:
            return False
        
        return True


    def assign_child_resource(
        self,
        resource: ElectrodeSheet,
        location: Optional[Coordinate] = None,
        reassign: bool = True,
    ):
        """放置极片到洞位中
        
        Args:
            resource: 要放置的极片
            location: 极片在洞位中的位置（对于洞位，通常为None）
            reassign: 是否允许重新分配
        """
        # 检查是否可以添加极片
        if not self.can_add_sheet(resource):
            raise ValueError(f"无法向洞位 {self.name} 添加极片：直径或厚度不匹配")
        
        # 调用父类方法实际执行分配
        super().assign_child_resource(resource, location, reassign)

    def unassign_child_resource(self, resource: ElectrodeSheet):
        """从洞位中移除极片
        
        Args:
            resource: 要移除的极片
        """
        if resource not in self.children:
            raise ValueError(f"极片 {resource.name} 不在洞位 {self.name} 中")
        
        # 调用父类方法实际执行移除
        super().unassign_child_resource(resource)



    def serialize_state(self) -> Dict[str, Any]:
        return {
            "sheet_count": len(self.children),
            "sheets": [sheet.serialize() for sheet in self.children],
        }
class ClipMagazine_four(ItemizedResource[ClipMagazineHole]):
    """子弹夹类 - 有4个洞位，每个洞位放多个极片"""
    children: List[ClipMagazineHole]
    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        hole_diameter: float = 14.0,
        hole_depth: float = 10.0,
        hole_spacing: float = 25.0,
        max_sheets_per_hole: int = 100,
        category: str = "clip_magazine_four",
        model: Optional[str] = None,
    ):
        """初始化子弹夹

        Args:
            name: 子弹夹名称
            size_x: 长度 (mm)
            size_y: 宽度 (mm)
            size_z: 高度 (mm)
            hole_diameter: 洞直径 (mm)
            hole_depth: 洞深度 (mm)
            hole_spacing: 洞位间距 (mm)
            max_sheets_per_hole: 每个洞位最大极片数量
            category: 类别
            model: 型号
        """
        # 创建4个洞位，排成2x2布局
        holes = create_ordered_items_2d(
            klass=ClipMagazineHole,
            num_items_x=2,
            num_items_y=2,
            dx=(size_x - 2 * hole_spacing) / 2,  # 居中
            dy=(size_y - hole_spacing) / 2,     # 居中
            dz=size_z - 0,
            item_dx=hole_spacing,
            item_dy=hole_spacing,
            diameter=hole_diameter,
            depth=hole_depth,
        )

        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            ordered_items=holes,
            category=category,
            model=model,
        )

        # 保存洞位的直径和深度
        self.hole_diameter = hole_diameter
        self.hole_depth = hole_depth
        self.max_sheets_per_hole = max_sheets_per_hole

    def serialize(self) -> dict:
        return {
            **super().serialize(),
            "hole_diameter": self.hole_diameter,
            "hole_depth": self.hole_depth,
            "max_sheets_per_hole": self.max_sheets_per_hole,
        }
# TODO: 这个要改
class ClipMagazine(ItemizedResource[ClipMagazineHole]):
    """子弹夹类 - 有6个洞位，每个洞位放多个极片"""
    children: List[ClipMagazineHole]
    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        hole_diameter: float = 14.0,
        hole_depth: float = 10.0,
        hole_spacing: float = 25.0,
        max_sheets_per_hole: int = 100,
        category: str = "clip_magazine",
        model: Optional[str] = None,
    ):
        """初始化子弹夹

        Args:
            name: 子弹夹名称
            size_x: 长度 (mm)
            size_y: 宽度 (mm)
            size_z: 高度 (mm)
            hole_diameter: 洞直径 (mm)
            hole_depth: 洞深度 (mm)
            hole_spacing: 洞位间距 (mm)
            max_sheets_per_hole: 每个洞位最大极片数量
            category: 类别
            model: 型号
        """
        # 创建6个洞位，排成2x3布局
        holes = create_ordered_items_2d(
            klass=ClipMagazineHole,
            num_items_x=3,
            num_items_y=2,
            dx=(size_x - 2 * hole_spacing) / 2,  # 居中
            dy=(size_y - hole_spacing) / 2,     # 居中
            dz=size_z - 0,
            item_dx=hole_spacing,
            item_dy=hole_spacing,
            diameter=hole_diameter,
            depth=hole_depth,
        )

        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            ordered_items=holes,
            category=category,
            model=model,
        )

        # 保存洞位的直径和深度
        self.hole_diameter = hole_diameter
        self.hole_depth = hole_depth
        self.max_sheets_per_hole = max_sheets_per_hole

    def serialize(self) -> dict:
        return {
            **super().serialize(),
            "hole_diameter": self.hole_diameter,
            "hole_depth": self.hole_depth,
            "max_sheets_per_hole": self.max_sheets_per_hole,
        }
#是一种类型注解，不用self
class BatteryState(TypedDict):
    """电池状态字典"""
    diameter: float
    height: float

    electrolyte_name: str
    electrolyte_volume: float

class Battery(Resource):
    """电池类 - 可容纳极片"""
    children: List[ElectrodeSheet] = []

    def __init__(
        self,
        name: str,
        category: str = "battery",
    ):
        """初始化电池

        Args:
            name: 电池名称
            diameter: 直径 (mm)
            height: 高度 (mm)
            max_volume: 最大容量 (μL)
            barcode: 二维码编号
            category: 类别
            model: 型号
        """
        super().__init__(
            name=name,
            size_x=1,
            size_y=1,
            size_z=1,
            category=category,
        )
        self._unilabos_state: BatteryState = BatteryState()

    def add_electrolyte_with_bottle(self, bottle: Bottle) -> bool:
        to_add_name = bottle._unilabos_state["electrolyte_name"]
        if bottle.aspirate_electrolyte(10):
            if self.add_electrolyte(to_add_name, 10):
                pass
            else:
                bottle._unilabos_state["electrolyte_volume"] += 10

    def set_electrolyte(self, name: str, volume: float) -> None:
        """设置电解液信息"""
        self._unilabos_state["electrolyte_name"] = name
        self._unilabos_state["electrolyte_volume"] = volume
    #这个应该没用，不会有加了后再加的事情
    def add_electrolyte(self, name: str, volume: float) -> bool:
        """添加电解液信息"""
        if name != self._unilabos_state["electrolyte_name"]:
            return False
        self._unilabos_state["electrolyte_volume"] += volume

    def load_state(self, state: Dict[str, Any]) -> None:
        """格式不变"""
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(self._unilabos_state)  # Container自身的信息，云端物料将保存这一data，本地也通过这里的data进行读写，当前类用来表示这个物料的长宽高大小的属性，而data（state用来表示物料的内容，细节等）
        return data

# 电解液作为属性放进去

class BatteryPressSlotState(TypedDict):
    """电池状态字典"""
    diameter: float =20.0
    depth: float = 4.0

class BatteryPressSlot(Resource):
    """电池压制槽类 - 设备，可容纳一个电池"""
    children: List[Battery] = []

    def __init__(
        self,
        name: str = "BatteryPressSlot",
        category: str = "battery_press_slot",
    ):
        """初始化电池压制槽

        Args:
            name: 压制槽名称
            diameter: 直径 (mm)
            depth: 深度 (mm)
            category: 类别
            model: 型号
        """
        super().__init__(
            name=name,
            size_x=10,
            size_y=12,
            size_z=13,
            category=category,
        )
        self._unilabos_state: BatteryPressSlotState = BatteryPressSlotState()

    def has_battery(self) -> bool:
        """检查是否有电池"""
        return len(self.children) > 0

    def load_state(self, state: Dict[str, Any]) -> None:
        """格式不变"""
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(self._unilabos_state)  # Container自身的信息，云端物料将保存这一data，本地也通过这里的data进行读写，当前类用来表示这个物料的长宽高大小的属性，而data（state用来表示物料的内容，细节等）
        return data
    
    def assign_child_resource(
        self,
        resource: Battery,
        location: Optional[Coordinate],
        reassign: bool = True,
    ):
        """放置极片"""
        if self.has_battery():
            raise ValueError(f"槽位已含有一个电池，无法再放置其他电池")
        super().assign_child_resource(resource, location, reassign)

    # 根据children的编号取物料对象。
    def get_battery_info(self, index: int) -> Battery:
        return self.children[0]

class TipBox64State(TypedDict):
    """电池状态字典"""
    tip_diameter: float = 5.0
    tip_length: float = 50.0
    with_tips: bool = True

class TipBox64(TipRack):
    """64孔枪头盒类"""

    children: List[TipSpot] = []
    def __init__(
        self,
        name: str,
        size_x: float = 127.8,
        size_y: float = 85.5,
        size_z: float = 60.0,
        category: str = "tip_box_64",
        model: Optional[str] = None,
    ):
        """初始化64孔枪头盒

        Args:
            name: 枪头盒名称
            size_x: 长度 (mm)
            size_y: 宽度 (mm)
            size_z: 高度 (mm)
            tip_diameter: 枪头直径 (mm)
            tip_length: 枪头长度 (mm)
            category: 类别
            model: 型号
            with_tips: 是否带枪头
        """
        from pylabrobot.resources.tip import Tip

        # 创建8x8=64个枪头位
        def make_tip():
            return Tip(
                has_filter=False,
                total_tip_length=20.0,
                maximal_volume=1000,  # 1mL
                fitting_depth=8.0,
            )

        tip_spots = create_ordered_items_2d(
            klass=TipSpot,
            num_items_x=8,
            num_items_y=8,
            dx=8.0,
            dy=8.0,
            dz=0.0,
            item_dx=9.0,
            item_dy=9.0,
            size_x=10,
            size_y=10,
            size_z=0.0,
            make_tip=make_tip,
        )
        self._unilabos_state: WasteTipBoxstate = WasteTipBoxstate()
        # 记录网格参数用于前端渲染
        self._grid_params = {
            "num_items_x": 8,
            "num_items_y": 8,
            "dx": 8.0,
            "dy": 8.0,
            "item_dx": 9.0,
            "item_dy": 9.0,
        }
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            ordered_items=tip_spots,
            category=category,
            model=model,
            with_tips=True,
        )

    def serialize(self) -> dict:
        return {
            **super().serialize(),
            **self._grid_params,
        }



class WasteTipBoxstate(TypedDict):
    """"废枪头盒状态字典"""
    max_tips: int = 100
    tip_count: int = 0

#枪头不是一次性的（同一溶液则反复使用），根据寄存器判断
class WasteTipBox(Trash):
    """废枪头盒类 - 100个枪头容量"""

    def __init__(
        self,
        name: str,
        size_x: float = 127.8,
        size_y: float = 85.5,
        size_z: float = 60.0,
        category: str = "waste_tip_box",
        model: Optional[str] = None,
    ):
        """初始化废枪头盒

        Args:
            name: 废枪头盒名称
            size_x: 长度 (mm)
            size_y: 宽度 (mm)
            size_z: 高度 (mm)
            max_tips: 最大枪头容量
            category: 类别
            model: 型号
        """
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            category=category,
            model=model,
        )
        self._unilabos_state: WasteTipBoxstate = WasteTipBoxstate()

    def add_tip(self) -> None:
        """添加废枪头"""
        if self._unilabos_state["tip_count"] >= self._unilabos_state["max_tips"]:
            raise ValueError(f"废枪头盒 {self.name} 已满")
        self._unilabos_state["tip_count"] += 1

    def get_tip_count(self) -> int:
        """获取枪头数量"""
        return self._unilabos_state["tip_count"]

    def empty(self) -> None:
        """清空废枪头盒"""
        self._unilabos_state["tip_count"] = 0


    def load_state(self, state: Dict[str, Any]) -> None:
        """格式不变"""
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(self._unilabos_state)  # Container自身的信息，云端物料将保存这一data，本地也通过这里的data进行读写，当前类用来表示这个物料的长宽高大小的属性，而data（state用来表示物料的内容，细节等）
        return data


class BottleRackState(TypedDict):
    """     bottle_diameter: 瓶子直径 (mm)
            bottle_height: 瓶子高度 (mm)
            position_spacing: 位置间距 (mm)"""
    bottle_diameter: float
    bottle_height: float
    name_to_index: dict


class BottleRackState(TypedDict):
    """     bottle_diameter: 瓶子直径 (mm)
            bottle_height: 瓶子高度 (mm)
            position_spacing: 位置间距 (mm)"""
    bottle_diameter: float
    bottle_height: float
    position_spacing: float
    name_to_index: dict


class BottleRack(Resource):
    """瓶架类 - 12个待配位置+12个已配位置"""
    children: List[Resource] = []

    def __init__(
            self,
            name: str,
            size_x: float,
            size_y: float,
            size_z: float,
            category: str = "bottle_rack",
            model: Optional[str] = None,
            num_items_x: int = 3,
            num_items_y: int = 4,
            position_spacing: float = 35.0,
            orientation: str = "horizontal",
            padding_x: float = 20.0,
            padding_y: float = 20.0,
    ):
        """初始化瓶架

        Args:
            name: 瓶架名称
            size_x: 长度 (mm)
            size_y: 宽度 (mm)
            size_z: 高度 (mm)
            category: 类别
            model: 型号
        """
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            category=category,
            model=model,
        )
        # 初始化状态
        self._unilabos_state: BottleRackState = BottleRackState(
            bottle_diameter=30.0,
            bottle_height=100.0,
            position_spacing=position_spacing,
            name_to_index={},
        )
        # 基于网格生成瓶位坐标映射（居中摆放）
        # 使用内边距，避免点跑到容器外（前端渲染不按mm等比缩放时更稳妥）
        origin_x = padding_x
        origin_y = padding_y
        self.index_to_pos = {}
        for j in range(num_items_y):
            for i in range(num_items_x):
                idx = j * num_items_x + i
                if orientation == "vertical":
                    # 纵向：沿 y 方向优先排列
                    self.index_to_pos[idx] = Coordinate(
                        x=origin_x + j * position_spacing,
                        y=origin_y + i * position_spacing,
                        z=0,
                    )
                else:
                    # 横向（默认）：沿 x 方向优先排列
                    self.index_to_pos[idx] = Coordinate(
                        x=origin_x + i * position_spacing,
                        y=origin_y + j * position_spacing,
                        z=0,
                    )
        self.name_to_index = {}
        self.name_to_pos = {}
        self.num_items_x = num_items_x
        self.num_items_y = num_items_y
        self.orientation = orientation
        self.padding_x = padding_x
        self.padding_y = padding_y

    def load_state(self, state: Dict[str, Any]) -> None:
        """格式不变"""
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(
            self._unilabos_state)  # Container自身的信息，云端物料将保存这一data，本地也通过这里的data进行读写，当前类用来表示这个物料的长宽高大小的属性，而data（state用来表示物料的内容，细节等）
        return data

    # TODO: 这里有些问题要重新写一下
    def assign_child_resource_old(self, resource: Resource, location=Coordinate.zero(), reassign=True):
        capacity = self.num_items_x * self.num_items_y
        assert len(self.children) < capacity, "瓶架已满，无法添加更多瓶子"
        index = len(self.children)
        location = self.index_to_pos.get(index, Coordinate.zero())
        self.name_to_pos[resource.name] = location
        self.name_to_index[resource.name] = index
        return super().assign_child_resource(resource, location, reassign)

    def assign_child_resource(self, resource: Resource, index: int):
        capacity = self.num_items_x * self.num_items_y
        assert 0 <= index < capacity, "无效的瓶子索引"
        self.name_to_index[resource.name] = index
        location = self.index_to_pos[index]
        return super().assign_child_resource(resource, location)

    def unassign_child_resource(self, resource: Bottle):
        super().unassign_child_resource(resource)
        self.index_to_pos.pop(self.name_to_index.pop(resource.name, None), None)

    def serialize(self) -> dict:
        return {
            **super().serialize(),
            "num_items_x": self.num_items_x,
            "num_items_y": self.num_items_y,
            "position_spacing": self._unilabos_state.get("position_spacing", 35.0),
            "orientation": self.orientation,
            "padding_x": self.padding_x,
            "padding_y": self.padding_y,
        }


class BottleState(TypedDict):
    diameter: float
    height: float
    electrolyte_name: str
    electrolyte_volume: float
    max_volume: float

class Bottle(Resource):
    """瓶子类 - 容纳电解液"""

    def __init__(
        self,
        name: str,
        category: str = "bottle",
    ):
        """初始化瓶子

        Args:
            name: 瓶子名称
            diameter: 直径 (mm)
            height: 高度 (mm)
            max_volume: 最大体积 (μL)
            barcode: 二维码
            category: 类别
            model: 型号
        """
        super().__init__(
            name=name,
            size_x=1,
            size_y=1,
            size_z=1,
            category=category,
        )
        self._unilabos_state: BottleState = BottleState()
    
    def aspirate_electrolyte(self, volume: float) -> bool:
        current_volume = self._unilabos_state["electrolyte_volume"]
        assert current_volume > volume, f"Cannot aspirate {volume}μL, only {current_volume}μL available."
        self._unilabos_state["electrolyte_volume"] -= volume
        return True

    def load_state(self, state: Dict[str, Any]) -> None:
        """格式不变"""
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(self._unilabos_state)  # Container自身的信息，云端物料将保存这一data，本地也通过这里的data进行读写，当前类用来表示这个物料的长宽高大小的属性，而data（state用来表示物料的内容，细节等）
        return data

class CoincellDeck(Deck):
    """纽扣电池组装工作站台面类"""

    def __init__(
        self,
        name: str = "coin_cell_deck",
        size_x: float = 1620.0,  # 3.66m
        size_y: float = 1270.0,  # 1.23m
        size_z: float = 500.0,
        origin: Coordinate = Coordinate(0, 0, 0),
        category: str = "coin_cell_deck",
    ):
        """初始化纽扣电池组装工作站台面

        Args:
            name: 台面名称
            size_x: 长度 (mm) - 3.66m
            size_y: 宽度 (mm) - 1.23m
            size_z: 高度 (mm)
            origin: 原点坐标
            category: 类别
        """
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            origin=origin,
            category=category,
        )

#if __name__ == "__main__":
#    # 转移极片的测试代码
#    deck = CoincellDeck("coin_cell_deck")
#    ban_cao_wei = PlateSlot("ban_cao_wei", max_plates=8)
#    deck.assign_child_resource(ban_cao_wei, Coordinate(x=0, y=0, z=0))
#
#    plate_1 = MaterialPlate("plate_1", 1,1,1, fill=True)
#    for i, hole in enumerate(plate_1.children):
#        sheet = ElectrodeSheet(f"hole_{i}_sheet_1")
#        sheet._unilabos_state = {
#            "diameter": 14,
#            "info": "NMC",
#            "mass": 5.0,
#            "material_type": "positive_electrode",
#            "thickness": 0.1
#        }
#        hole._unilabos_state = {
#            "depth": 1.0,
#            "diameter": 14,
#            "info": "",
#            "max_sheets": 1
#        }
#        hole.assign_child_resource(sheet, Coordinate.zero())
#    plate_1._unilabos_state = {
#        "hole_spacing_x": 20.0,
#        "hole_spacing_y": 20.0,
#        "hole_diameter": 5,
#        "info": "这是第一块料板"
#    }
#    plate_1.update_locations()
#    ban_cao_wei.assign_child_resource(plate_1, Coordinate.zero())
#    # zi_dan_jia = ClipMagazine("zi_dan_jia", 1, 1, 1)
#    # deck.assign_child_resource(ban_cao_wei, Coordinate(x=200, y=200, z=0))
#
#    from unilabos.resources.graphio import *
#    A = tree_to_list([resource_plr_to_ulab(deck)])
#    with open("test.json", "w") as f:
#        json.dump(A, f)
#
#
#def get_plate_with_14mm_hole(name=""):
#    plate = MaterialPlate(name=name)
#    for i in range(4):
#        for j in range(4):
#            hole = MaterialHole(f"{i+1}x{j+1}")
#            hole._unilabos_state["diameter"] = 14
#            hole._unilabos_state["max_sheets"] = 1
#            plate.assign_child_resource(hole)
#    return plate

def create_a_liaopan():
    liaopan = MaterialPlate(name="liaopan", size_x=120.8, size_y=120.5, size_z=10.0, fill=True)
    for i in range(16):
        jipian = ElectrodeSheet(name=f"jipian_{i}", size_x= 12, size_y=12, size_z=0.1)
        liaopan1.children[i].assign_child_resource(jipian, location=None)
    return liaopan

def create_a_coin_cell_deck():
    deck = Deck(size_x=1200,
                size_y=800,
                size_z=900)

    #liaopan =  TipBox64(name="liaopan")

    #创建一个4*4的物料板
    liaopan1 =  MaterialPlate(name="liaopan1", size_x=120.8, size_y=120.5, size_z=10.0, fill=True)
    #把物料板放到桌子上
    deck.assign_child_resource(liaopan1, Coordinate(x=0, y=0, z=0))
    #创建一个极片
    for i in range(16):
        jipian = ElectrodeSheet(name=f"jipian_{i}", size_x= 12, size_y=12, size_z=0.1)
        liaopan1.children[i].assign_child_resource(jipian, location=None)
    #创建一个4*4的物料板
    liaopan2 =  MaterialPlate(name="liaopan2", size_x=120.8, size_y=120.5, size_z=10.0, fill=True)
    #把物料板放到桌子上
    deck.assign_child_resource(liaopan2, Coordinate(x=500, y=0, z=0))

    #创建一个4*4的物料板
    liaopan3 =  MaterialPlate(name="liaopan3", size_x=120.8, size_y=120.5, size_z=10.0, fill=True)
    #把物料板放到桌子上
    deck.assign_child_resource(liaopan3, Coordinate(x=1000, y=0, z=0))

    print(deck)

    return deck


import json

if __name__ == "__main__":
    electrode1 = BatteryPressSlot()
    #print(electrode1.get_size_x())
    #print(electrode1.get_size_y())
    #print(electrode1.get_size_z())
    #jipian = ElectrodeSheet()
    #jipian._unilabos_state["diameter"] = 18
    #print(jipian.serialize())
    #print(jipian.serialize_state())

    deck = CoincellDeck()
    """======================================子弹夹============================================"""
    zip_dan_jia = ClipMagazine_four("zi_dan_jia", 80, 80, 10)
    deck.assign_child_resource(zip_dan_jia, Coordinate(x=1400, y=50, z=0))
    zip_dan_jia2 = ClipMagazine_four("zi_dan_jia2", 80, 80, 10)
    deck.assign_child_resource(zip_dan_jia2, Coordinate(x=1600, y=200, z=0))
    zip_dan_jia3 = ClipMagazine("zi_dan_jia3", 80, 80, 10)
    deck.assign_child_resource(zip_dan_jia3, Coordinate(x=1500, y=200, z=0))
    zip_dan_jia4 = ClipMagazine("zi_dan_jia4", 80, 80, 10)
    deck.assign_child_resource(zip_dan_jia4, Coordinate(x=1500, y=300, z=0))
    zip_dan_jia5 = ClipMagazine("zi_dan_jia5", 80, 80, 10)
    deck.assign_child_resource(zip_dan_jia5, Coordinate(x=1600, y=300, z=0))
    zip_dan_jia6 = ClipMagazine("zi_dan_jia6", 80, 80, 10)
    deck.assign_child_resource(zip_dan_jia6, Coordinate(x=1530, y=500, z=0))
    zip_dan_jia7 = ClipMagazine("zi_dan_jia7", 80, 80, 10)
    deck.assign_child_resource(zip_dan_jia7, Coordinate(x=1180, y=400, z=0))
    zip_dan_jia8 = ClipMagazine("zi_dan_jia8", 80, 80, 10)
    deck.assign_child_resource(zip_dan_jia8, Coordinate(x=1280, y=400, z=0))
    for i in range(4):
        jipian = ElectrodeSheet(name=f"zi_dan_jia_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        zip_dan_jia2.children[i].assign_child_resource(jipian, location=None)
    for i in range(4):
        jipian2 = ElectrodeSheet(name=f"zi_dan_jia2_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        zip_dan_jia.children[i].assign_child_resource(jipian2, location=None)
    for i in range(6):
        jipian3 = ElectrodeSheet(name=f"zi_dan_jia3_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        zip_dan_jia3.children[i].assign_child_resource(jipian3, location=None)
    for i in range(6):
        jipian4 = ElectrodeSheet(name=f"zi_dan_jia4_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        zip_dan_jia4.children[i].assign_child_resource(jipian4, location=None)
    for i in range(6):
        jipian5 = ElectrodeSheet(name=f"zi_dan_jia5_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        zip_dan_jia5.children[i].assign_child_resource(jipian5, location=None)
    for i in range(6):
        jipian6 = ElectrodeSheet(name=f"zi_dan_jia6_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        zip_dan_jia6.children[i].assign_child_resource(jipian6, location=None)
    for i in range(6):
        jipian7 = ElectrodeSheet(name=f"zi_dan_jia7_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        zip_dan_jia7.children[i].assign_child_resource(jipian7, location=None)
    for i in range(6):
        jipian8 = ElectrodeSheet(name=f"zi_dan_jia8_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        zip_dan_jia8.children[i].assign_child_resource(jipian8, location=None)
    """======================================子弹夹============================================"""
    #liaopan =  TipBox64(name="liaopan")
    """======================================物料板============================================"""
    #创建一个4*4的物料板
    liaopan1 = MaterialPlate(name="liaopan1", size_x=120, size_y=100, size_z=10.0, fill=True)
    deck.assign_child_resource(liaopan1, Coordinate(x=1010, y=50, z=0))
    for i in range(16):
        jipian_1 = ElectrodeSheet(name=f"{liaopan1.name}_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        liaopan1.children[i].assign_child_resource(jipian_1, location=None)

    liaopan2 = MaterialPlate(name="liaopan2", size_x=120, size_y=100, size_z=10.0, fill=True)
    deck.assign_child_resource(liaopan2, Coordinate(x=1130, y=50, z=0))

    liaopan3 = MaterialPlate(name="liaopan3", size_x=120, size_y=100, size_z=10.0, fill=True)
    deck.assign_child_resource(liaopan3, Coordinate(x=1250, y=50, z=0))

    liaopan4 = MaterialPlate(name="liaopan4", size_x=120, size_y=100, size_z=10.0, fill=True)
    deck.assign_child_resource(liaopan4, Coordinate(x=1010, y=150, z=0))
    for i in range(16):
        jipian_4 = ElectrodeSheet(name=f"{liaopan4.name}_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        liaopan4.children[i].assign_child_resource(jipian_4, location=None)
    liaopan5 = MaterialPlate(name="liaopan5", size_x=120, size_y=100, size_z=10.0, fill=True)
    deck.assign_child_resource(liaopan5, Coordinate(x=1130, y=150, z=0))
    liaopan6 = MaterialPlate(name="liaopan6", size_x=120, size_y=100, size_z=10.0, fill=True)
    deck.assign_child_resource(liaopan6, Coordinate(x=1250, y=150, z=0))
    #liaopan.children[3].assign_child_resource(jipian, location=None)
    """======================================物料板============================================"""
    """======================================瓶架，移液枪============================================"""
    # 在台面上放置 3x4 瓶架、6x2 瓶架 与 64孔移液枪头盒
    bottle_rack_3x4 = BottleRack(
        name="bottle_rack_3x4",
        size_x=210.0,
        size_y=140.0,
        size_z=100.0,
        num_items_x=3,
        num_items_y=4,
        position_spacing=35.0,
        orientation="vertical",
    )
    deck.assign_child_resource(bottle_rack_3x4, Coordinate(x=100, y=200, z=0))

    bottle_rack_6x2 = BottleRack(
        name="bottle_rack_6x2",
        size_x=120.0,
        size_y=250.0,
        size_z=100.0,
        num_items_x=6,
        num_items_y=2,
        position_spacing=35.0,
        orientation="vertical",
    )
    deck.assign_child_resource(bottle_rack_6x2, Coordinate(x=300, y=300, z=0))

    bottle_rack_6x2_2 = BottleRack(
        name="bottle_rack_6x2_2",
        size_x=120.0,
        size_y=250.0,
        size_z=100.0,
        num_items_x=6,
        num_items_y=2,
        position_spacing=35.0,
        orientation="vertical",
    )
    deck.assign_child_resource(bottle_rack_6x2_2, Coordinate(x=430, y=300, z=0))


    # 将 ElectrodeSheet 放满 3x4 与 6x2 的所有孔位
    for idx in range(bottle_rack_3x4.num_items_x * bottle_rack_3x4.num_items_y):
        sheet = ElectrodeSheet(name=f"sheet_3x4_{idx}", size_x=12, size_y=12, size_z=0.1)
        bottle_rack_3x4.assign_child_resource(sheet, index=idx)

    for idx in range(bottle_rack_6x2.num_items_x * bottle_rack_6x2.num_items_y):
        sheet = ElectrodeSheet(name=f"sheet_6x2_{idx}", size_x=12, size_y=12, size_z=0.1)
        bottle_rack_6x2.assign_child_resource(sheet, index=idx)

    tip_box = TipBox64(name="tip_box_64")
    deck.assign_child_resource(tip_box, Coordinate(x=300, y=100, z=0))

    waste_tip_box = WasteTipBox(name="waste_tip_box")
    deck.assign_child_resource(waste_tip_box, Coordinate(x=300, y=200, z=0))
    """======================================瓶架，移液枪============================================"""
    print(deck)


    from unilabos.resources.graphio import convert_resources_from_type
    from unilabos.config.config import BasicConfig 
    BasicConfig.ak = "56bbed5b-6e30-438c-b06d-f69eaa63bb45"
    BasicConfig.sk = "238222fe-0bf7-4350-a426-e5ced8011dcf"
    from unilabos.app.web.client import http_client

    resources = convert_resources_from_type([deck], [Resource])
    
    # 检查序列化后的资源

    json.dump({"nodes": resources, "links": []}, open("button_battery_decks_unilab.json", "w"), indent=2)
   
      
    #print(resources)
    http_client.remote_addr = "https://uni-lab.test.bohrium.com/api/v1"
 
    http_client.resource_add(resources)