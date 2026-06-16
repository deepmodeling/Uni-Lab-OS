"""
EIT AGV 资源甲板定义

设计目标：
1) 为 AGV 提供一个独立的前端可视化区域（独立设备，不并入任一工站 deck）。
2) 只维护“车上槽位占用”这一事实，不依赖持续轮询外部库存接口。
3) 与 transfer_resource_to_another 兼容，作为资源转运的目标/来源设备。
"""

import uuid
from typing import Any

from pylabrobot.resources import Coordinate, Deck

from unilabos.resources.warehouse import WareHouse, warehouse_factory
from unilabos.utils.log import logger


def eit_warehouse_AGV(name: str) -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=4,
        num_items_y=2,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",
        name_by_layout_code=True,
    )


class EIT_AGV_Deck(Deck):
    """
    AGV 的独立 Deck。

    说明：
    - 仅包含一个名为 "AGV" 的 2x4 仓位区域（layout_code: AGV-1-1 ... AGV-2-4）。
    - 这个结构用于前端展示和资源树挂载，不代表实际硬件几何约束。
    """

    def __init__(
        self,
        name: str = "EIT_AGV_Deck",
        size_x: float = 1200.0,
        size_y: float = 800.0,
        size_z: float = 1200.0,
        setup: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, size_x=size_x, size_y=size_y, size_z=size_z)
        if not getattr(self, "unilabos_uuid", None):
            self.unilabos_uuid = str(uuid.uuid4())
        if setup:
            self.setup()

    def _recursive_assign_uuid(self, resource: Any) -> None:
        """
        递归补齐资源 UUID，避免资源树转运时出现“无 uuid 无法挂载”问题。
        """
        if not hasattr(resource, "unilabos_uuid") or not resource.unilabos_uuid:
            resource.unilabos_uuid = str(uuid.uuid4())
        if hasattr(resource, "children"):
            for child in resource.children:
                self._recursive_assign_uuid(child)

    def setup(self) -> None:
        """
        构建 AGV 资源结构：
        - 一个 AGV 仓库（2x4）
        - 挂到 deck 固定位置
        """
        self.warehouses = {
            "AGV": eit_warehouse_AGV("AGV"),
        }
        self.warehouse_locations = {
            "AGV": Coordinate(100.0, 100.0, 0.0),
        }

        for wh_name, wh in self.warehouses.items():
            self._recursive_assign_uuid(wh)
            self.assign_child_resource(wh, self.warehouse_locations[wh_name])
            logger.info(f"EIT AGV Deck: 已挂载仓库 {wh_name}")

        self._recursive_assign_uuid(self)
        logger.info("EIT AGV Deck: UUID 校验完成")

