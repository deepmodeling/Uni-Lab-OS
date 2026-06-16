# coding: utf-8
"""
功能:
    覆盖 transfer_analysis_to_shelf 的核心流程, 验证分析站落架后自动回充逻辑.
参数:
    无.
返回:
    无. 通过 unittest 执行断言.
"""

import unittest
from unittest.mock import MagicMock, patch


_ARM_DRIVER_PATH = "eit_agv.controller.agv_controller.ArmDriver"
_POS_MGR_PATH = "eit_agv.controller.agv_controller.PositionManager"
_SHELF_MANAGER_PATH = "eit_agv.controller.agv_controller.ShelfManager"
_ZHIDA_CLIENT_PATH = "unilabos.devices.eit_analysis_station.driver.zhida_driver.ZhidaClient"
_SLEEP_PATH = "eit_agv.controller.agv_controller.time.sleep"


def _make_status_detail(raw_status: str, base_status: str = None, sub_status: str = "") -> dict:
    """
    功能:
        构造智达状态明细字典, 统一测试里的状态 mock 结构.
    参数:
        raw_status: 原始状态字符串.
        base_status: 主状态, None 表示与原始状态一致.
        sub_status: 子状态字符串.
    返回:
        Dict, 包含 raw_status/base_status/sub_status 三个字段.
    """
    if base_status is None:
        base_status = raw_status
    return {
        "raw_status": raw_status,
        "base_status": base_status,
        "sub_status": sub_status,
    }


def _make_controller():
    """
    功能:
        创建 AGVController 实例, 并替换硬件与货架管理依赖, 避免连接真实设备和写入真实状态.
    参数:
        无.
    返回:
        AGVController, 用于测试分析站落架流程的控制器实例.
    """
    with patch(_ARM_DRIVER_PATH), patch(_POS_MGR_PATH), patch(_SHELF_MANAGER_PATH):
        from eit_agv.controller.agv_controller import AGVController

        return AGVController()


class TestTransferAnalysisToShelf(unittest.TestCase):
    """
    功能:
        分析完成样品转运到货架并自动回充的测试套件.
    参数:
        无.
    返回:
        无.
    """

    def setUp(self):
        """
        功能:
            为每个测试用例准备独立的 AGVController 和常用 mock.
        参数:
            无.
        返回:
            无.
        """
        self.controller = _make_controller()
        self.controller.batch_transfer_materials = MagicMock(return_value=True)
        self.controller.go_to_charging_station = MagicMock(return_value={"ret_code": 0})
        self.controller.shelf_manager.find_empty_slots.return_value = ["shelf_tray_1-1"]

    @patch(_ZHIDA_CLIENT_PATH)
    def test_transfer_success_and_return_to_charging_station(self, mock_zhida_client):
        """
        功能:
            验证分析站空闲且转运成功时, 会更新货架状态并返回充电站.
        参数:
            mock_zhida_client: 智达客户端补丁对象.
        返回:
            无.
        """
        client = mock_zhida_client.return_value
        client.get_status_detail.return_value = _make_status_detail("Idle")

        result = self.controller.transfer_analysis_to_shelf(
            source_trays=["analysis_station_tray_1-2"]
        )

        self.assertIs(result, True)
        self.controller.batch_transfer_materials.assert_called_once_with(
            [
                {
                    "source_tray": "analysis_station_tray_1-2",
                    "target_tray": "shelf_tray_1-1",
                    "material_type": "FLASH_FILTER_OUTER_BOTTLE_TRAY",
                }
            ],
            block=True,
        )
        self.controller.shelf_manager.place_material.assert_called_once_with(
            slot_name="shelf_tray_1-1",
            material_type="FLASH_FILTER_OUTER_BOTTLE_TRAY",
            source="analysis_station_tray_1-2",
            description="分析完成样品(自动转运)",
        )
        self.controller.go_to_charging_station.assert_called_once_with(block=True)
        client.connect.assert_called_once_with()
        client.close.assert_called_once_with()

    @patch(_ZHIDA_CLIENT_PATH)
    def test_transfer_success_but_charging_station_failed_returns_false(self, mock_zhida_client):
        """
        功能:
            验证货架落位成功但回充失败时, 函数返回 False 且不回滚货架状态.
        参数:
            mock_zhida_client: 智达客户端补丁对象.
        返回:
            无.
        """
        client = mock_zhida_client.return_value
        client.get_status_detail.return_value = _make_status_detail("Idle")
        self.controller.go_to_charging_station.return_value = None

        result = self.controller.transfer_analysis_to_shelf(
            source_trays=["analysis_station_tray_1-2"]
        )

        self.assertIs(result, False)
        self.controller.shelf_manager.place_material.assert_called_once()
        self.controller.go_to_charging_station.assert_called_once_with(block=True)
        client.close.assert_called_once_with()

    @patch(_ZHIDA_CLIENT_PATH)
    def test_transfer_success_but_charging_station_exception_returns_false(self, mock_zhida_client):
        """
        功能:
            验证货架落位成功但回充抛出异常时, 函数返回 False 且记录错误日志.
        参数:
            mock_zhida_client: 智达客户端补丁对象.
        返回:
            无.
        """
        client = mock_zhida_client.return_value
        client.get_status_detail.return_value = _make_status_detail("Idle")
        self.controller.go_to_charging_station.side_effect = RuntimeError("导航异常")

        with self.assertLogs("eit_agv.controller.agv_controller", level="ERROR") as log_context:
            result = self.controller.transfer_analysis_to_shelf(
                source_trays=["analysis_station_tray_1-2"]
            )

        self.assertIs(result, False)
        self.assertTrue(
            any("返回充电站异常" in message for message in log_context.output)
        )
        self.controller.shelf_manager.place_material.assert_called_once()
        self.controller.go_to_charging_station.assert_called_once_with(block=True)
        client.close.assert_called_once_with()

    @patch(_ZHIDA_CLIENT_PATH)
    def test_batch_transfer_failure_skips_return_to_charging_station(self, mock_zhida_client):
        """
        功能:
            验证批量转运失败时, 不更新货架状态, 也不触发回充.
        参数:
            mock_zhida_client: 智达客户端补丁对象.
        返回:
            无.
        """
        client = mock_zhida_client.return_value
        client.get_status_detail.return_value = _make_status_detail("Idle")
        self.controller.batch_transfer_materials.return_value = False

        result = self.controller.transfer_analysis_to_shelf(
            source_trays=["analysis_station_tray_1-2"]
        )

        self.assertIs(result, False)
        self.controller.batch_transfer_materials.assert_called_once()
        self.controller.shelf_manager.place_material.assert_not_called()
        self.controller.go_to_charging_station.assert_not_called()
        client.close.assert_called_once_with()

    @patch(_ZHIDA_CLIENT_PATH)
    def test_insufficient_empty_slots_skips_transfer_and_return(self, mock_zhida_client):
        """
        功能:
            验证货架空位不足时, 不执行转运, 也不触发回充.
        参数:
            mock_zhida_client: 智达客户端补丁对象.
        返回:
            无.
        """
        client = mock_zhida_client.return_value
        client.get_status_detail.return_value = _make_status_detail("Idle")
        self.controller.shelf_manager.find_empty_slots.return_value = []

        result = self.controller.transfer_analysis_to_shelf(
            source_trays=["analysis_station_tray_1-2"]
        )

        self.assertIs(result, False)
        self.controller.batch_transfer_materials.assert_not_called()
        self.controller.go_to_charging_station.assert_not_called()
        client.close.assert_called_once_with()

    @patch(_ZHIDA_CLIENT_PATH)
    @patch(_SLEEP_PATH, return_value=None)
    def test_status_timeout_skips_transfer_and_return(self, _mock_sleep, mock_zhida_client):
        """
        功能:
            验证分析站长时间未进入 Idle 时, 轮询超时后直接返回 False.
        参数:
            _mock_sleep: sleep 补丁对象.
            mock_zhida_client: 智达客户端补丁对象.
        返回:
            无.
        """
        client = mock_zhida_client.return_value
        client.get_status_detail.return_value = _make_status_detail("Busy")

        result = self.controller.transfer_analysis_to_shelf(
            source_trays=["analysis_station_tray_1-2"],
            poll_interval=1.0,
            poll_timeout=2.0,
        )

        self.assertIs(result, False)
        self.controller.shelf_manager.find_empty_slots.assert_not_called()
        self.controller.batch_transfer_materials.assert_not_called()
        self.controller.go_to_charging_station.assert_not_called()
        client.close.assert_called_once_with()

    @patch(_ZHIDA_CLIENT_PATH)
    def test_composite_idle_status_allows_transfer(self, mock_zhida_client):
        """
        功能:
            验证复合状态 Idle#SeqRun:Error 会按 Idle 主状态放行转运.
        参数:
            mock_zhida_client: 智达客户端补丁对象.
        返回:
            无.
        """
        client = mock_zhida_client.return_value
        client.get_status_detail.return_value = _make_status_detail(
            raw_status="Idle#SeqRun:Error",
            base_status="Idle",
            sub_status="SeqRun:Error",
        )

        result = self.controller.transfer_analysis_to_shelf(
            source_trays=["analysis_station_tray_1-2"]
        )

        self.assertIs(result, True)
        self.controller.batch_transfer_materials.assert_called_once()
        self.controller.go_to_charging_station.assert_called_once_with(block=True)
        client.close.assert_called_once_with()

    @patch(_ZHIDA_CLIENT_PATH)
    def test_error_status_skips_transfer_and_return(self, mock_zhida_client):
        """
        功能:
            验证分析站出现 Error 状态时, 直接终止流程且不触发回充.
        参数:
            mock_zhida_client: 智达客户端补丁对象.
        返回:
            无.
        """
        client = mock_zhida_client.return_value
        client.get_status_detail.return_value = _make_status_detail("Error")

        result = self.controller.transfer_analysis_to_shelf(
            source_trays=["analysis_station_tray_1-2"]
        )

        self.assertIs(result, False)
        self.controller.shelf_manager.find_empty_slots.assert_not_called()
        self.controller.batch_transfer_materials.assert_not_called()
        self.controller.go_to_charging_station.assert_not_called()
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
