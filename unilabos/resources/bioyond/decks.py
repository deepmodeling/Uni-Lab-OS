from os import name
from pylabrobot.resources import Deck, Coordinate, Rotation

from unilabos.resources.bioyond.YB_warehouses import (
    bioyond_warehouse_1x4x4,
    bioyond_warehouse_1x4x4_right,  # 新增：右侧仓库 (A05～D08)
    bioyond_warehouse_1x4x2,
    bioyond_warehouse_reagent_stack,  # 新增：试剂堆栈 (A1-B4)
    bioyond_warehouse_liquid_and_lid_handling,
    bioyond_warehouse_1x2x2,
    bioyond_warehouse_2x2x1,  # 新增：321和43窗口 (2行×2列)
    bioyond_warehouse_1x3x3,
    bioyond_warehouse_5x3x1,  # 新增：手动传递窗仓库 (5行×3列)
    bioyond_warehouse_10x1x1,
    bioyond_warehouse_3x3x1,
    bioyond_warehouse_3x3x1_2,
    bioyond_warehouse_5x1x1,
    bioyond_warehouse_1x8x4,
    bioyond_warehouse_reagent_storage,
    # bioyond_warehouse_liquid_preparation,
    bioyond_warehouse_tipbox_storage,  # 新增：Tip盒堆栈
    bioyond_warehouse_density_vial,
)


class BIOYOND_PolymerReactionStation_Deck(Deck):
    def __init__(
        self,
        name: str = "PolymerReactionStation_Deck",
        size_x: float = 2700.0,
        size_y: float = 1080.0,
        size_z: float = 1500.0,
        category: str = "deck",
        setup: bool = False
    ) -> None:
        super().__init__(name=name, size_x=2700.0, size_y=1080.0, size_z=1500.0)
        if setup:
            self.setup()

    def setup(self) -> None:
        # 添加仓库
        # 说明: 堆栈1物理上分为左右两部分
        #   - 堆栈1左: A01～D04 (4行×4列, 位于反应站左侧)
        #   - 堆栈1右: A05～D08 (4行×4列, 位于反应站右侧)
        self.warehouses = {
            "堆栈1左": bioyond_warehouse_1x4x4("堆栈1左"),  # 左侧堆栈: A01～D04
            "堆栈1右": bioyond_warehouse_1x4x4_right("堆栈1右"),  # 右侧堆栈: A05～D08
            "站内试剂存放堆栈": bioyond_warehouse_reagent_storage("站内试剂存放堆栈"),  # A01～A02
            # "移液站内10%分装液体准备仓库": bioyond_warehouse_liquid_preparation("移液站内10%分装液体准备仓库"),  # A01～B04
            "站内Tip盒堆栈": bioyond_warehouse_tipbox_storage("站内Tip盒堆栈"),  # A01～B03, 存放枪头盒.
            "测量小瓶仓库(测密度)": bioyond_warehouse_density_vial("测量小瓶仓库(测密度)"),  # A01～B03
        }
        self.warehouse_locations = {
            "堆栈1左": Coordinate(0.0, 430.0, 0.0),  # 左侧位置
            "堆栈1右": Coordinate(2500.0, 430.0, 0.0),  # 右侧位置
            "站内试剂存放堆栈": Coordinate(640.0, 480.0, 0.0),
            # "移液站内10%分装液体准备仓库": Coordinate(1200.0, 600.0, 0.0),
            "站内Tip盒堆栈": Coordinate(300.0, 150.0, 0.0),
            "测量小瓶仓库(测密度)": Coordinate(922.0, 552.0, 0.0),
        }
        self.warehouses["站内试剂存放堆栈"].rotation = Rotation(z=90)
        self.warehouses["测量小瓶仓库(测密度)"].rotation = Rotation(z=270)

        for warehouse_name, warehouse in self.warehouses.items():
            self.assign_child_resource(warehouse, location=self.warehouse_locations[warehouse_name])


class BIOYOND_PolymerPreparationStation_Deck(Deck):
    def __init__(
        self,
        name: str = "PolymerPreparationStation_Deck",
        size_x: float = 2700.0,
        size_y: float = 1080.0,
        size_z: float = 1500.0,
        category: str = "deck",
        setup: bool = False
    ) -> None:
        super().__init__(name=name, size_x=2700.0, size_y=1080.0, size_z=1500.0)
        if setup:
            self.setup()

    def setup(self) -> None:
        # 添加仓库 - 配液站的3个堆栈，使用Bioyond系统中的实际名称
        # 样品类型（typeMode=1）：烧杯、试剂瓶、分装板 → 试剂堆栈、溶液堆栈
        # 试剂类型（typeMode=2）：样品板 → 粉末堆栈
        self.warehouses = {
            # 试剂类型 - 样品板
            "粉末堆栈": bioyond_warehouse_1x4x4("粉末堆栈"),  # 4行×4列 (A01-D04)

            # 样品类型 - 烧杯、试剂瓶、分装板
            "试剂堆栈": bioyond_warehouse_reagent_stack("试剂堆栈"),  # 2行×4列 (A01-B04)
            "溶液堆栈": bioyond_warehouse_1x4x4("溶液堆栈"),  # 4行×4列 (A01-D04)
        }
        self.warehouse_locations = {
            "粉末堆栈": Coordinate(0.0, 450.0, 0.0),
            "试剂堆栈": Coordinate(1850.0, 200.0, 0.0),
            "溶液堆栈": Coordinate(2500.0, 450.0, 0.0),
        }

        for warehouse_name, warehouse in self.warehouses.items():
            self.assign_child_resource(warehouse, location=self.warehouse_locations[warehouse_name])

class BIOYOND_YB_Deck(Deck):
    def __init__(
        self,
        name: str = "YB_Deck",
        size_x: float = 4150,
        size_y: float = 1400.0,
        size_z: float = 2670.0,
        category: str = "deck",
        setup: bool = False
    ) -> None:
        super().__init__(name=name, size_x=4150.0, size_y=1400.0, size_z=2670.0)
        if setup:
            self.setup()

    def setup(self) -> None:
        # 添加仓库
        self.warehouses = {
            "321窗口": bioyond_warehouse_2x2x1("321窗口"),  # 2行×2列
            "43窗口": bioyond_warehouse_2x2x1("43窗口"),  # 2行×2列
            "手动传递窗右": bioyond_warehouse_5x3x1("手动传递窗右", row_offset=0),  # A01-E03
            "手动传递窗左": bioyond_warehouse_5x3x1("手动传递窗左", row_offset=5),  # F01-J03
            "加样头堆栈左": bioyond_warehouse_10x1x1("加样头堆栈左"),
            "加样头堆栈右": bioyond_warehouse_10x1x1("加样头堆栈右"),

            "15ml配液堆栈左": bioyond_warehouse_3x3x1("15ml配液堆栈左"),
            "母液加样右": bioyond_warehouse_3x3x1_2("母液加样右"),
            "大瓶母液堆栈左": bioyond_warehouse_5x1x1("大瓶母液堆栈左"),
            "大瓶母液堆栈右": bioyond_warehouse_5x1x1("大瓶母液堆栈右"),
        }
        # warehouse 的位置
        self.warehouse_locations = {
            "321窗口": Coordinate(-150.0, 158.0, 0.0),
            "43窗口": Coordinate(4160.0, 158.0, 0.0),
            "手动传递窗左": Coordinate(-150.0, 877.0, 0.0),
            "手动传递窗右": Coordinate(4160.0, 877.0, 0.0),
            "加样头堆栈左": Coordinate(385.0, 1300.0, 0.0),
            "加样头堆栈右": Coordinate(2187.0, 1300.0, 0.0),

            "15ml配液堆栈左": Coordinate(749.0, 355.0, 0.0),
            "母液加样右": Coordinate(2152.0, 333.0, 0.0),
            "大瓶母液堆栈左": Coordinate(1164.0, 676.0, 0.0),
            "大瓶母液堆栈右": Coordinate(2717.0, 676.0, 0.0),
        }

        for warehouse_name, warehouse in self.warehouses.items():
            self.assign_child_resource(warehouse, location=self.warehouse_locations[warehouse_name])
def YB_Deck(name: str) -> Deck:
    by=BIOYOND_YB_Deck(name=name)
    by.setup()
    return by





