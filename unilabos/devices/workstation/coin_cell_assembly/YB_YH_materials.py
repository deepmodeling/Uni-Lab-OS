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

from unilabos.resources.battery.magazine import MagazineHolder_4_Cathode, MagazineHolder_6_Cathode, MagazineHolder_6_Anode, MagazineHolder_6_Battery
from unilabos.resources.battery.bottle_carriers import YIHUA_Electrolyte_12VialCarrier
from unilabos.resources.battery.electrode_sheet import ElectrodeSheet



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
        #if resource._unilabos_state["diameter"] > self._unilabos_state["diameter"]:
        #    raise ValueError(f"极片直径 {resource._unilabos_state['diameter']} 超过洞位直径 {self._unilabos_state['diameter']}")
        #if len(self.children) >= self._unilabos_state["max_sheets"]:
        #    raise ValueError(f"洞位已满，无法放置更多极片")
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


#是一种类型注解，不用self
class BatteryState(TypedDict):
    """电池状态字典"""
    diameter: float
    height: float
    assembly_pressure: float
    electrolyte_volume: float
    electrolyte_name: str

class Battery(Resource):
    """电池类 - 可容纳极片"""
    children: List[ElectrodeSheet] = []

    def __init__(
        self,
        name: str,
        size_x=1,
        size_y=1,
        size_z=1,
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
        self._unilabos_state: BatteryState = BatteryState(
                                 diameter = 1.0,
                                 height = 1.0,
                                 assembly_pressure = 1.0,
                                 electrolyte_volume = 1.0,
                                 electrolyte_name = "DP001"
                                 )

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
        # TODO: 让高京看下槽位只有一个电池时是否这么写。
        if self.has_battery():
            raise ValueError(f"槽位已含有一个电池，无法再放置其他电池")
        super().assign_child_resource(resource, location, reassign)

    # 根据children的编号取物料对象。
    def get_battery_info(self, index: int) -> Battery:
        return self.children[0]


def TipBox64(
        name: str,
        size_x: float = 127.8,
        size_y: float = 85.5,
        size_z: float = 60.0,
        category: str = "tip_rack",
        model: Optional[str] = None,
):
    """64孔枪头盒类"""
    from pylabrobot.resources.tip import Tip

    # 创建12x8=96个枪头位
    def make_tip():
        return Tip(
            has_filter=False,
            total_tip_length=20.0,
            maximal_volume=1000,  # 1mL
            fitting_depth=8.0,
        )

    tip_spots = create_ordered_items_2d(
        klass=TipSpot,
        num_items_x=12,
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
    idx_available = list(range(0, 32)) + list(range(64, 96))
    tip_spots_available = {k: v for i, (k, v) in enumerate(tip_spots.items()) if i in idx_available}
    tip_rack = TipRack(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        # ordered_items=tip_spots_available,
        ordered_items=tip_spots,
        category=category,
        model=model,
        with_tips=False,
    )
    tip_rack.set_tip_state([True]*32 + [False]*32 + [True]*32)  # 前32和后32个有枪头，中间32个无枪头
    return tip_rack


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
        material_z_thickness=0,
        max_volume=float("inf"),
        category="trash",
        model=None,
        compute_volume_from_height=None,
        compute_height_from_volume=None,
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


class CoincellDeck(Deck):
    """纽扣电池组装工作站台面类"""

    def __init__(
        self,
        name: str = "coin_cell_deck",
        size_x: float = 1450.0,  # 1m
        size_y: float = 1450.0,  # 1m
        size_z: float = 100.0,   # 0.9m
        origin: Coordinate = Coordinate(-2200, 0, 0),
        category: str = "coin_cell_deck",
        setup: bool = False,  # 是否自动执行 setup
    ):
        """初始化纽扣电池组装工作站台面

        Args:
            name: 台面名称
            size_x: 长度 (mm) - 1m
            size_y: 宽度 (mm) - 1m
            size_z: 高度 (mm) - 0.9m
            origin: 原点坐标
            category: 类别
            setup: 是否自动执行 setup 配置标准布局
        """
        super().__init__(
            name=name,
            size_x=1450.0,
            size_y=1450.0,
            size_z=100.0,
            origin=origin,
        )
        if setup:
            self.setup()

    def setup(self) -> None:
        """设置工作站的标准布局 - 包含子弹夹、料盘、瓶架等完整配置"""
        # ====================================== 子弹夹 ============================================
        
        # 正极片（4个洞位，2x2布局）
        zhengji_zip = MagazineHolder_4_Cathode("正极&铝箔弹夹")
        self.assign_child_resource(zhengji_zip, Coordinate(x=402.0, y=830.0, z=0))
        
        # 正极壳、平垫片（6个洞位，2x2+2布局）
        zhengjike_zip = MagazineHolder_6_Cathode("正极壳&平垫片弹夹")
        self.assign_child_resource(zhengjike_zip, Coordinate(x=566.0, y=272.0, z=0))
        
        # 负极壳、弹垫片（6个洞位，2x2+2布局）
        fujike_zip = MagazineHolder_6_Anode("负极壳&弹垫片弹夹")
        self.assign_child_resource(fujike_zip, Coordinate(x=474.0, y=276.0, z=0))
        
        # 成品弹夹（6个洞位，3x2布局）
        chengpindanjia_zip = MagazineHolder_6_Battery("成品弹夹")
        self.assign_child_resource(chengpindanjia_zip, Coordinate(x=260.0, y=156.0, z=0))
        
        # ====================================== 物料板 ============================================
        # 创建物料板（料盘carrier）- 4x4布局
        # 负极料盘
        fujiliaopan = MaterialPlate(name="负极料盘", size_x=120, size_y=100, size_z=10.0, fill=True)
        self.assign_child_resource(fujiliaopan, Coordinate(x=708.0, y=794.0, z=0))
        # for i in range(16):
        #     fujipian = ElectrodeSheet(name=f"{fujiliaopan.name}_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        #     fujiliaopan.children[i].assign_child_resource(fujipian, location=None)

        # 隔膜料盘
        gemoliaopan = MaterialPlate(name="隔膜料盘", size_x=120, size_y=100, size_z=10.0, fill=True)
        self.assign_child_resource(gemoliaopan, Coordinate(x=718.0, y=918.0, z=0))
        # for i in range(16):
        #     gemopian = ElectrodeSheet(name=f"{gemoliaopan.name}_jipian_{i}", size_x=12, size_y=12, size_z=0.1)
        #     gemoliaopan.children[i].assign_child_resource(gemopian, location=None)

        # ====================================== 瓶架、移液枪 ============================================
        # 在台面上放置 3x4 瓶架、6x2 瓶架 与 64孔移液枪头盒
        # 奔耀上料5ml分液瓶小板 - 由奔曜跨站转运而来，不单独写，但是这里应该有一个堆栈用于摆放分液瓶小板
        
        # bottle_rack_3x4 = BottleRack(
        #     name="bottle_rack_3x4",
        #     size_x=210.0,
        #     size_y=140.0,
        #     size_z=100.0,
        #     num_items_x=2,
        #     num_items_y=4,
        #     position_spacing=35.0,
        #     orientation="vertical",
        # )
        # self.assign_child_resource(bottle_rack_3x4, Coordinate(x=1542.0, y=717.0, z=0))
        
        # 电解液缓存位 - 6x2布局
        bottle_rack_6x2 = YIHUA_Electrolyte_12VialCarrier(name="bottle_rack_6x2")
        self.assign_child_resource(bottle_rack_6x2, Coordinate(x=1050.0, y=358.0, z=0))
        # 电解液回收位6x2
        bottle_rack_6x2_2 = YIHUA_Electrolyte_12VialCarrier(name="bottle_rack_6x2_2")
        self.assign_child_resource(bottle_rack_6x2_2, Coordinate(x=914.0, y=358.0, z=0))

        tip_box = TipBox64(name="tip_box_64")
        self.assign_child_resource(tip_box, Coordinate(x=782.0, y=514.0, z=0))

        waste_tip_box = WasteTipBox(name="waste_tip_box")
        self.assign_child_resource(waste_tip_box, Coordinate(x=778.0, y=622.0, z=0))


def YH_Deck(name=""):
    cd = CoincellDeck(name=name)
    cd.setup()
    return cd


if __name__ == "__main__":
    deck = create_coin_cell_deck()
    print(deck)