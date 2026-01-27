from os import name
from pylabrobot.resources import Deck, Coordinate, Rotation
from unilabos.utils.log import logger
import uuid

from unilabos.resources.eit_synthesis_station.warehouses import (
    eit_warehouse_W,
    eit_warehouse_N,
    eit_warehouse_TB,
    eit_warehouse_AS,
    eit_warehouse_FF,
    eit_warehouse_MS,
    eit_warehouse_MSB,
    eit_warehouse_SC,
    eit_warehouse_T,
    eit_warehouse_TS,
)

class EIT_Synthesis_Station_Deck(Deck):
    def __init__(
        self,
        name: str = "Synthesis_Station_Deck",
        size_x: float = 2800.0,
        size_y: float = 1500.0,
        size_z: float = 1500.0,
        category: str = "deck",
        setup: bool = False,
        **kwargs
    ) -> None:
        super().__init__(name=name, size_x=size_x, size_y=size_y, size_z=size_z)
        if not getattr(self, "unilabos_uuid", None):
            self.unilabos_uuid = str(uuid.uuid4())
        if setup:
            self.setup()
    
    def _recursive_assign_uuid(self, res):
        """递归为资源及其所有现有子资源分配 UUID"""
        if not hasattr(res, "unilabos_uuid") or not res.unilabos_uuid:
            res.unilabos_uuid = str(uuid.uuid4())
        for child in res.children:
            self._recursive_assign_uuid(child)
        

    def setup(self) -> None:
        self.warehouses = {
            "W": eit_warehouse_W("W"),
            "N": eit_warehouse_N("N"), 
            "TB": eit_warehouse_TB("TB"),
            "AS": eit_warehouse_AS("AS"),
            "FF": eit_warehouse_FF("FF"),
            "MS": eit_warehouse_MS("MS"),
            "MSB": eit_warehouse_MSB("MSB"),
            "SC": eit_warehouse_SC("SC"),
            "T": eit_warehouse_T("T"),
            "TS": eit_warehouse_TS("TS"),
        }
        self.warehouse_locations = {
            "W": Coordinate(80.0, 80.0, 0.0),
            "TB": Coordinate(80.0, 560.0, 0.0),
            "N": Coordinate(80.0, 848.0, 0.0),
            "AS": Coordinate(1400.0, 80.0, 0.0),
            "FF": Coordinate(1400.0, 360.0, 0.0),
            "MS": Coordinate(1400.0, 540.0, 0.0),
            "MSB": Coordinate(1400.0, 720.0, 0.0),
            "SC": Coordinate(2100.0, 720.0, 0.0),
            "T": Coordinate(2100.0, 80.0, 0.0),
            "TS": Coordinate(2100.0, 360.0, 0.0),
        }

        for zone_key, warehouse in self.warehouses.items():
            location = self.warehouse_locations.get(zone_key)
            if location:
                self._recursive_assign_uuid(warehouse)
                self.assign_child_resource(warehouse, location)
                logger.info(f"已将仓库 {zone_key} 挂载到 Deck")
        
        self._recursive_assign_uuid(self)
        logger.info("EIT Deck 全量资源 UUID 校验完成")
    
    def _recursive_assign_uuid(self, res):
        # 如果没有 uuid，则分配一个
        if not hasattr(res, "unilabos_uuid") or not res.unilabos_uuid:
            res.unilabos_uuid = str(uuid.uuid4())
        
        # 强制遍历所有 children
        if hasattr(res, "children"):
            for child in res.children:
                self._recursive_assign_uuid(child)
