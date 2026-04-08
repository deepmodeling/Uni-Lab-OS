"""
AGV 通用转运工站 Driver

继承 WorkstationBase，通过 WorkstationNodeCreator 自动获得 ROS2WorkstationNode 能力。
Warehouse 作为 children 中的资源节点，由 attach_resource() 自动注册到 resource_tracker。
deck=None，不使用 PLR Deck 抽象。
"""

from typing import Any, Dict, List, Optional

from pylabrobot.resources import Deck

from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.resources.warehouse import WareHouse
from unilabos.utils import logger


class AGVTransportStation(WorkstationBase):
    """通用 AGV 转运工站

    初始化链路（零框架改动）：
        ROS2DeviceNode.__init__():
          issubclass(AGVTransportStation, WorkstationBase) → True
          → WorkstationNodeCreator.create_instance(data):
              data["deck"] = None
              → DeviceClassCreator.create_instance(data) → AGVTransportStation(deck=None, ...)
              → attach_resource(): children 中 type="warehouse" → resource_tracker.add_resource(wh)
          → ROS2WorkstationNode(protocol_type=[...], children=[nav, arm], ...)
          → driver.post_init(ros_node):
              self.carrier 从 resource_tracker 中获取 WareHouse
    """

    def __init__(
        self,
        deck: Optional[Deck] = None,
        children: Optional[List[Any]] = None,
        route_table: Optional[Dict[str, Dict[str, str]]] = None,
        device_roles: Optional[Dict[str, str]] = None,
        **kwargs,
    ):
        super().__init__(deck=None, **kwargs)
        self.route_table: Dict[str, Dict[str, str]] = route_table or {}
        self.device_roles: Dict[str, str] = device_roles or {}

    # ============ 载具 (Warehouse) ============

    @property
    def carrier(self) -> Optional[WareHouse]:
        """从 resource_tracker 中找到 AGV 载具 Warehouse"""
        if not hasattr(self, "_ros_node"):
            return None
        for res in self._ros_node.resource_tracker.resources:
            if isinstance(res, WareHouse):
                return res
        return None

    @property
    def capacity(self) -> int:
        """AGV 载具总容量（slot 数）"""
        wh = self.carrier
        if wh is None:
            return 0
        return wh.num_items_x * wh.num_items_y * wh.num_items_z

    @property
    def free_slots(self) -> List[str]:
        """返回当前空闲 slot 名称列表"""
        wh = self.carrier
        if wh is None:
            return []
        ordering = getattr(wh, "_ordering", {})
        return [name for name, site in ordering.items() if site.resource is None]

    @property
    def occupied_slots(self) -> Dict[str, Any]:
        """返回已占用的 slot → Resource 映射"""
        wh = self.carrier
        if wh is None:
            return {}
        ordering = getattr(wh, "_ordering", {})
        return {name: site.resource for name, site in ordering.items() if site.resource is not None}

    # ============ 路由查询 ============

    def resolve_route(self, from_station: str, to_station: str) -> Dict[str, str]:
        """查询路由表，返回导航和机械臂指令

        Args:
            from_station: 来源工站 ID
            to_station: 目标工站 ID

        Returns:
            {"nav_command": "...", "arm_pick": "...", "arm_place": "..."}

        Raises:
            KeyError: 路由表中未找到对应路线
        """
        route_key = f"{from_station}->{to_station}"
        if route_key not in self.route_table:
            raise KeyError(f"路由表中未找到路线: {route_key}")
        return self.route_table[route_key]

    def get_device_id(self, role: str) -> str:
        """获取子设备 ID

        Args:
            role: 设备角色，如 "navigator", "arm"

        Returns:
            设备 ID 字符串

        Raises:
            KeyError: 未配置该角色的设备
        """
        if role not in self.device_roles:
            raise KeyError(f"未配置设备角色: {role}，当前已配置: {list(self.device_roles.keys())}")
        return self.device_roles[role]

    # ============ 生命周期 ============

    def post_init(self, ros_node) -> None:
        super().post_init(ros_node)
        wh = self.carrier
        if wh is not None:
            logger.info(f"AGV {ros_node.device_id} 载具已就绪: {wh.name}, 容量={self.capacity}")
        else:
            logger.warning(f"AGV {ros_node.device_id} 未发现 Warehouse 载具资源")
