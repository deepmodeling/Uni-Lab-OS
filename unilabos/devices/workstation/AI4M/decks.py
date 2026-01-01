from os import name
from pylabrobot.resources import Deck, Coordinate, Rotation

from unilabos.devices.workstation.AI4M.warehouses import (
    Hydrogel_warehouse_5x3x1,
    Station_1_warehouse_1x1x1,
    Station_2_warehouse_1x1x1,
    Station_3_warehouse_1x1x1,
)



class AI4M_deck(Deck):
    def __init__(
        self,
        name: str = "AI4M_deck",
        size_x: float = 2000.0,
        size_y: float = 1000.0,
        size_z: float = 2670.0,
        category: str = "deck",
        setup: bool = True,
    ) -> None:
        super().__init__(name=name, size_x=1700.0, size_y=1350.0, size_z=2670.0)
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
        self.warehouse_locations = {
            "水凝胶烧杯堆栈": Coordinate(350.0, 55.0, 0.0),
            "反应工站1": Coordinate(1300.0, 55.0, 0.0),
            "反应工站2": Coordinate(1300.0, 500.0, 0.0),
            "反应工站3": Coordinate(1300.0, 950.0, 0.0)
            

        }

        for warehouse_name, warehouse in self.warehouses.items():
            self.assign_child_resource(warehouse, location=self.warehouse_locations[warehouse_name])






