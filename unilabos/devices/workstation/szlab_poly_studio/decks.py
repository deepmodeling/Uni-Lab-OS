from typing import Optional

from pylabrobot.resources import Coordinate, Deck, Resource

from unilabos.devices.workstation.szlab_poly_studio.warehouses import (
    powder_container_placeholder_warehouse,
    s1_loading_buffer_warehouse,
    s2_tip_placeholder_warehouse,
    s3_unused_beaker_warehouse,
    s3_unused_sample_vial_warehouse,
    s10_liquid_reagent_placeholder_warehouse,
    s11_used_beaker_warehouse,
    s11_used_sample_vial_warehouse,
)
from unilabos.registry.decorators import resource


@resource(
    id="szlab_poly_studio_deck",
    category=["szlab_poly_studio", "deck"],
    description="苏州实验室聚合物工作站 Deck，包含 S1/S3/S11 实际堆栈和 S2/粉桶/S10 占位堆栈",
)
class SZLabPolyStudioDeck(Deck):
    def __init__(
        self,
        name: str = "SZLabPolyStudioDeck",
        size_x: float = 3000.0,
        size_y: float = 1800.0,
        size_z: float = 2200.0,
        category: str = "deck",
        setup: bool = True,
    ) -> None:
        super().__init__(name=name, size_x=size_x, size_y=size_y, size_z=size_z, category=category)
        self.warehouses = {}
        self.warehouse_locations = {}
        if setup:
            self.setup()

    def setup(self) -> None:
        self.warehouses = {
            "S1上料过渡仓": s1_loading_buffer_warehouse("S1上料过渡仓"),
            "S2枪头仓占位": s2_tip_placeholder_warehouse("S2枪头仓占位"),
            "S3未使用烧杯仓": s3_unused_beaker_warehouse("S3未使用烧杯仓"),
            "S3未使用样品瓶仓": s3_unused_sample_vial_warehouse("S3未使用样品瓶仓"),
            "S10液体试剂瓶仓占位": s10_liquid_reagent_placeholder_warehouse("S10液体试剂瓶仓占位"),
            "S11使用烧杯成品仓": s11_used_beaker_warehouse("S11使用烧杯成品仓"),
            "S11使用样品瓶成品仓": s11_used_sample_vial_warehouse("S11使用样品瓶成品仓"),
            "固体粉桶仓占位": powder_container_placeholder_warehouse("固体粉桶仓占位"),
        }
        self.warehouse_locations = {
            "S1上料过渡仓": Coordinate(100.0, 100.0, 0.0),
            "S2枪头仓占位": Coordinate(100.0, 450.0, 0.0),
            "S3未使用烧杯仓": Coordinate(500.0, 100.0, 0.0),
            "S3未使用样品瓶仓": Coordinate(500.0, 600.0, 0.0),
            "S10液体试剂瓶仓占位": Coordinate(1800.0, 100.0, 0.0),
            "S11使用烧杯成品仓": Coordinate(1100.0, 100.0, 0.0),
            "S11使用样品瓶成品仓": Coordinate(1100.0, 600.0, 0.0),
            "固体粉桶仓占位": Coordinate(1800.0, 700.0, 0.0),
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
