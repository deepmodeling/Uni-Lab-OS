from os import name
from typing import Optional

from pylabrobot.resources import Deck, Coordinate, Rotation, Resource
from unilabos.devices.workstation.AI4M.warehouses import (
    Hydrogel_warehouse_5x3x1,
    Station_1_warehouse_1x1x1,
    Station_2_warehouse_1x1x1,
    Station_3_warehouse_1x1x1,
    Raw_electrode_warehouse_3x5x1,
    Finished_electrode_warehouse_3x5x1,
    Stir_1_warehouse_1x1x1,
    Stir_2_warehouse_1x1x1,
    Water_wash_warehouse_1x1x1,
    Acid_wash_warehouse_1x1x1,
)



class AI4M_deck(Deck):
    def __init__(
        self,
        name: str = "AI4M_deck",
        size_x: float = 1217.0,
        size_y: float = 1560.0,
        size_z: float = 2670.0,
        origin: Coordinate = Coordinate(0, 35, 0),
        category: str = "deck",
        setup: bool = False,
    ) -> None:
        super().__init__(name=name, size_x=size_x, size_y=size_y, size_z=size_z, origin=origin)
        self.warehouses = {}
        self.warehouse_locations = {}
        if setup:
            self.setup()

    def setup(self) -> None:
        # 添加仓库
        self.warehouses = {
            "水凝胶烧杯堆栈": Hydrogel_warehouse_5x3x1("水凝胶烧杯堆栈"),
            "反应工站1": Station_1_warehouse_1x1x1("反应工站1"),
            "反应工站2": Station_2_warehouse_1x1x1("反应工站2"),
            "反应工站3": Station_3_warehouse_1x1x1("反应工站3")
        }
        # warehouse 的位置
        # 根据前端显示位置转换计算（Deck尺寸: 1217x1580mm, 前端显示: 688x895px）
        # 缩放比例: x=1.769, y=1.766
        # 前端坐标 -> 实际坐标: x' = x * 1.769, y' = y * 1.766
        self.warehouse_locations = {
            "水凝胶烧杯堆栈": Coordinate(15.9, 1100.2, 0.0),     # 前端: 9x623
            "反应工站1": Coordinate(838.5, 1158.7, 0.0),         # 前端: 474x139
            "反应工站2": Coordinate(850.9, 706.4, 0.0),         # 前端: 481x400
            "反应工站3": Coordinate(842.0, 245.5, 0.0)         # 前端: 476x656
        }

        for warehouse_name, warehouse in self.warehouses.items():
            self.assign_child_resource(warehouse, location=self.warehouse_locations[warehouse_name])

    def assign_child_resource(
        self,
        resource: Resource,
        location: Optional[Coordinate],
        reassign: bool = True,
      ):
          super().assign_child_resource(resource, location, reassign)
          self.warehouses[resource.name] = resource
          self.warehouse_locations[resource.name] = location


class AI4M002_deck(Deck):
    def __init__(
        self,
        name: str = "AI4M002_deck",
        size_x: float = 1460.0,
        size_y: float = 1560.0,
        size_z: float = 2670.0,
        origin: Coordinate = Coordinate(0, 35, 0),
        category: str = "deck",
        setup: bool = False,
    ) -> None:
        super().__init__(name=name, size_x=size_x, size_y=size_y, size_z=size_z, origin=origin)
        self.warehouses = {}
        self.warehouse_locations = {}
        if setup:
            self.setup()

    def setup(self) -> None:
        # 添加仓库
        self.warehouses = {
            "原始电极堆栈": Raw_electrode_warehouse_3x5x1("原始电极堆栈"),
            "完成电极堆栈": Finished_electrode_warehouse_3x5x1("完成电极堆栈"),
            "搅拌仪1": Stir_1_warehouse_1x1x1("搅拌仪1"),
            "搅拌仪2": Stir_2_warehouse_1x1x1("搅拌仪2"),
            "水洗池": Water_wash_warehouse_1x1x1("水洗池"),
            "酸洗池": Acid_wash_warehouse_1x1x1("酸洗池")
        }
        # warehouse 的位置
        self.warehouse_locations = {
            "原始电极堆栈": Coordinate(930.0, 1069.0, 0.0),     
            "完成电极堆栈": Coordinate(300.0, 1069.0, 0.0),     
            "搅拌仪1": Coordinate(588.0, 931.0, 0.0),          
            "搅拌仪2": Coordinate(890.0, 931.0, 0.0),         
            "水洗池": Coordinate(508.0, 681.0, 0.0),            
            "酸洗池": Coordinate(924.0, 681.0, 0.0)             
        }

        for warehouse_name, warehouse in self.warehouses.items():
            self.assign_child_resource(warehouse, location=self.warehouse_locations[warehouse_name])

    def assign_child_resource(
        self,
        resource: Resource,
        location: Optional[Coordinate],
        reassign: bool = True,
      ):
          super().assign_child_resource(resource, location, reassign)
          self.warehouses[resource.name] = resource
          self.warehouse_locations[resource.name] = location
