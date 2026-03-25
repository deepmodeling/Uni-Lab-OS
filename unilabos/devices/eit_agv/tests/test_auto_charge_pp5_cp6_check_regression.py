# coding: utf-8
"""
功能:
    覆盖 auto_charge_pp5_cp6_check 的核心回归场景, 验证PP5/CP6待命充电切换逻辑.
参数:
    无.
返回:
    无, 通过 unittest 执行断言.
"""

import unittest
from unittest.mock import patch


_ARM_DRIVER_PATH = "eit_agv.controller.agv_controller.ArmDriver"
_POS_MGR_PATH = "eit_agv.controller.agv_controller.PositionManager"


def _make_controller():
    """
    功能:
        创建 AGVController 实例, 并替换硬件相关依赖, 避免连接真实设备.
    参数:
        无.
    返回:
        AGVController, 可用于PP5/CP6自动充电逻辑测试的控制器实例.
    """
    with patch(_ARM_DRIVER_PATH), patch(_POS_MGR_PATH):
        from eit_agv.controller.agv_controller import AGVController

        return AGVController()


class TestAutoChargePp5Cp6CheckRegression(unittest.TestCase):
    """
    功能:
        PP5/CP6自动充电检查回归测试套件.
    参数:
        无.
    返回:
        无.
    """

    def setUp(self):
        """
        功能:
            为每个测试用例创建独立的控制器实例.
        参数:
            无.
        返回:
            无.
        """
        self.controller = _make_controller()

    def test_nav_busy_skips_check(self):
        """
        功能:
            验证导航忙碌时直接跳过PP5/CP6充电检查.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 2, "task_status_name": "RUNNING"},
        ), patch.object(
            self.controller,
            "query_current_station",
        ) as mock_query_station, patch.object(
            self.controller,
            "query_battery_status",
        ) as mock_query_battery:
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["action"], "skipped_busy_nav")
        mock_query_station.assert_not_called()
        mock_query_battery.assert_not_called()

    def test_station_query_failure_returns_error(self):
        """
        功能:
            验证站点查询失败时返回 query_location 错误.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"},
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value=None,
        ):
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["action"], "query_location")

    def test_battery_query_failure_returns_error(self):
        """
        功能:
            验证电量查询失败时返回 query_battery 错误.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"},
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value={"station_id": "PP5", "station_name": "charging_transition_point"},
        ), patch.object(
            self.controller,
            "query_battery_status",
            return_value=None,
        ):
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["action"], "query_battery")
        self.assertEqual(result["current_station"], "PP5")

    def test_pp5_low_battery_moves_to_cp6(self):
        """
        功能:
            验证AGV在PP5且电量低于50%时, 会移动到CP6充电.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"},
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value={"station_id": "PP5", "station_name": "charging_transition_point"},
        ), patch.object(
            self.controller,
            "query_battery_status",
            return_value={"battery_level": 0.45, "ret_code": 0},
        ) as mock_query_battery, patch.object(
            self.controller,
            "safe_navigate_to_station",
            return_value={"ret_code": 0},
        ) as mock_safe_nav:
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "pp5_to_cp6_for_charge")
        self.assertAlmostEqual(result["battery_level"], 0.45)
        mock_query_battery.assert_called_once_with(simple=True)
        mock_safe_nav.assert_called_once_with("CP6")

    def test_pp5_threshold_battery_stays_put(self):
        """
        功能:
            验证AGV在PP5且电量等于50%时, 继续在PP5待命.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"},
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value={"station_id": "PP5", "station_name": "charging_transition_point"},
        ), patch.object(
            self.controller,
            "query_battery_status",
            return_value={"battery_level": 0.5, "ret_code": 0},
        ), patch.object(
            self.controller,
            "safe_navigate_to_station",
        ) as mock_safe_nav:
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "standby_at_pp5")
        self.assertAlmostEqual(result["battery_level"], 0.5)
        mock_safe_nav.assert_not_called()

    def test_cp6_high_battery_moves_to_pp5(self):
        """
        功能:
            验证AGV在CP6且电量高于90%时, 会返回PP5待命.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"},
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value={"station_id": "CP6", "station_name": "charging_station"},
        ), patch.object(
            self.controller,
            "query_battery_status",
            return_value={"battery_level": 0.91, "ret_code": 0},
        ) as mock_query_battery, patch.object(
            self.controller,
            "safe_navigate_to_station",
            return_value={"ret_code": 0},
        ) as mock_safe_nav:
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "cp6_to_pp5_after_charge")
        self.assertAlmostEqual(result["battery_level"], 0.91)
        mock_query_battery.assert_called_once_with(simple=True)
        mock_safe_nav.assert_called_once_with("PP5")

    def test_cp6_threshold_battery_stays_put(self):
        """
        功能:
            验证AGV在CP6且电量等于90%时, 继续在CP6待命.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"},
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value={"station_id": "CP6", "station_name": "charging_station"},
        ), patch.object(
            self.controller,
            "query_battery_status",
            return_value={"battery_level": 0.9, "ret_code": 0},
        ), patch.object(
            self.controller,
            "safe_navigate_to_station",
        ) as mock_safe_nav:
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "standby_at_cp6")
        self.assertAlmostEqual(result["battery_level"], 0.9)
        mock_safe_nav.assert_not_called()

    def test_non_pp5_cp6_station_skips_check(self):
        """
        功能:
            验证AGV不在PP5或CP6时, 视为工作途中并跳过检查.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"},
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value={"station_id": "LM1", "station_name": "synthesis_station"},
        ), patch.object(
            self.controller,
            "query_battery_status",
            return_value={"battery_level": 0.7, "ret_code": 0},
        ), patch.object(
            self.controller,
            "safe_navigate_to_station",
        ) as mock_safe_nav:
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["action"], "working_in_progress")
        self.assertEqual(result["current_station"], "LM1")
        mock_safe_nav.assert_not_called()

    def test_pp5_low_battery_move_to_cp6_failure(self):
        """
        功能:
            验证AGV在PP5且低电量时, 如果进入CP6失败则返回 move_to_cp6 错误.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"},
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value={"station_id": "PP5", "station_name": "charging_transition_point"},
        ), patch.object(
            self.controller,
            "query_battery_status",
            return_value={"battery_level": 0.2, "ret_code": 0},
        ), patch.object(
            self.controller,
            "safe_navigate_to_station",
            return_value=None,
        ) as mock_safe_nav:
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["action"], "move_to_cp6")
        mock_safe_nav.assert_called_once_with("CP6")

    def test_cp6_high_battery_move_to_pp5_failure(self):
        """
        功能:
            验证AGV在CP6且高电量时, 如果返回PP5失败则返回 move_to_pp5 错误.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"},
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value={"station_id": "CP6", "station_name": "charging_station"},
        ), patch.object(
            self.controller,
            "query_battery_status",
            return_value={"battery_level": 0.95, "ret_code": 0},
        ), patch.object(
            self.controller,
            "safe_navigate_to_station",
            return_value=None,
        ) as mock_safe_nav:
            result = self.controller.auto_charge_pp5_cp6_check()

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["action"], "move_to_pp5")
        mock_safe_nav.assert_called_once_with("PP5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
