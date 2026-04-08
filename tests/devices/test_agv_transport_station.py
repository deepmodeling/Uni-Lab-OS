"""
AGVTransportStation driver 测试

覆盖：初始化、carrier property、slot 查询、路由查询、capacity 计算。
"""

import pytest
from unittest.mock import MagicMock, patch

from unilabos.devices.transport.agv_workstation import AGVTransportStation
from unilabos.resources.warehouse import WareHouse, warehouse_factory


class TestAGVTransportStation:
    def _make_driver(self, route_table=None, device_roles=None):
        """创建一个 AGVTransportStation 实例"""
        return AGVTransportStation(
            deck=None,
            route_table=route_table or {
                "A->B": {"nav_command": '{"target":"LM1"}', "arm_pick": "pick.urp", "arm_place": "place.urp"}
            },
            device_roles=device_roles or {"navigator": "agv_nav", "arm": "agv_arm"},
        )

    def _make_warehouse(self, name="agv_platform", nx=2, ny=1, nz=1):
        """创建一个测试用 Warehouse"""
        return warehouse_factory(name=name, num_items_x=nx, num_items_y=ny, num_items_z=nz)

    def test_init_deck_none(self):
        """AGVTransportStation 初始化时 deck=None"""
        driver = self._make_driver()
        assert driver.deck is None

    def test_init_route_table(self):
        """路由表正确存储"""
        driver = self._make_driver()
        assert "A->B" in driver.route_table

    def test_init_device_roles(self):
        """设备角色正确存储"""
        driver = self._make_driver()
        assert driver.device_roles["navigator"] == "agv_nav"
        assert driver.device_roles["arm"] == "agv_arm"

    def test_carrier_without_ros_node(self):
        """未 post_init 时 carrier 返回 None"""
        driver = self._make_driver()
        assert driver.carrier is None

    def test_carrier_with_warehouse(self):
        """post_init 后 carrier 返回正确的 WareHouse"""
        driver = self._make_driver()
        wh = self._make_warehouse()

        # 模拟 ros_node 和 resource_tracker
        mock_ros_node = MagicMock()
        mock_ros_node.resource_tracker.resources = [wh]
        mock_ros_node.device_id = "AGV"
        driver.post_init(mock_ros_node)

        assert driver.carrier is wh
        assert isinstance(driver.carrier, WareHouse)

    def test_capacity(self):
        """容量计算正确"""
        driver = self._make_driver()
        wh = self._make_warehouse(nx=2, ny=1, nz=1)
        mock_ros_node = MagicMock()
        mock_ros_node.resource_tracker.resources = [wh]
        mock_ros_node.device_id = "AGV"
        driver.post_init(mock_ros_node)

        assert driver.capacity == 2

    def test_capacity_multi_layer(self):
        """多层 Warehouse 容量"""
        driver = self._make_driver()
        wh = self._make_warehouse(nx=1, ny=2, nz=3)
        mock_ros_node = MagicMock()
        mock_ros_node.resource_tracker.resources = [wh]
        mock_ros_node.device_id = "AGV"
        driver.post_init(mock_ros_node)

        assert driver.capacity == 6

    def test_capacity_no_carrier(self):
        """无 carrier 时容量为 0"""
        driver = self._make_driver()
        assert driver.capacity == 0

    def test_free_slots(self):
        """空载时所有 slot 为空闲"""
        driver = self._make_driver()
        wh = self._make_warehouse(nx=2, ny=1, nz=1)
        mock_ros_node = MagicMock()
        mock_ros_node.resource_tracker.resources = [wh]
        mock_ros_node.device_id = "AGV"
        driver.post_init(mock_ros_node)

        free = driver.free_slots
        assert len(free) == 2

    def test_occupied_slots_empty(self):
        """空载时 occupied_slots 为空"""
        driver = self._make_driver()
        wh = self._make_warehouse(nx=2, ny=1, nz=1)
        mock_ros_node = MagicMock()
        mock_ros_node.resource_tracker.resources = [wh]
        mock_ros_node.device_id = "AGV"
        driver.post_init(mock_ros_node)

        assert len(driver.occupied_slots) == 0

    def test_resolve_route(self):
        """路由查询返回正确的指令"""
        driver = self._make_driver()
        route = driver.resolve_route("A", "B")
        assert route["nav_command"] == '{"target":"LM1"}'
        assert route["arm_pick"] == "pick.urp"

    def test_resolve_route_not_found(self):
        """查询不存在的路线时抛出 KeyError"""
        driver = self._make_driver()
        with pytest.raises(KeyError, match="路由表"):
            driver.resolve_route("X", "Y")

    def test_get_device_id(self):
        """获取子设备 ID"""
        driver = self._make_driver()
        assert driver.get_device_id("navigator") == "agv_nav"
        assert driver.get_device_id("arm") == "agv_arm"

    def test_get_device_id_not_found(self):
        """获取不存在的角色时抛出 KeyError"""
        driver = self._make_driver()
        with pytest.raises(KeyError, match="未配置设备角色"):
            driver.get_device_id("gripper")
