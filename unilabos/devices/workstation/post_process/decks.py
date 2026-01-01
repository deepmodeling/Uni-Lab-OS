from os import name
from pylabrobot.resources import Deck, Coordinate, Rotation

from unilabos.devices.workstation.post_process.warehouses import (
    post_process_warehouse_4x3x1,
    post_process_warehouse_4x3x1_2, 
)



class post_process_deck(Deck):
    def __init__(
        self,
        name: str = "post_process_deck",
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
            "原料罐堆栈": post_process_warehouse_4x3x1("原料罐堆栈"),
            "反应罐堆栈": post_process_warehouse_4x3x1_2("反应罐堆栈"),
            
        }
        # warehouse 的位置
        self.warehouse_locations = {
            "原料罐堆栈": Coordinate(350.0, 55.0, 0.0),
            "反应罐堆栈": Coordinate(1000.0, 55.0, 0.0),

        }

        for warehouse_name, warehouse in self.warehouses.items():
            self.assign_child_resource(warehouse, location=self.warehouse_locations[warehouse_name])






