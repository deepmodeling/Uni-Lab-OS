# coding: utf-8
"""
功能:
    覆盖 auto_charge_check 的核心回归场景, 验证低电量时的充电状态保护逻辑.
参数:
    无.
返回:
    无. 通过 unittest 执行断言.
"""

import unittest
from unittest.mock import call, patch


_ARM_DRIVER_PATH = "eit_agv.controller.agv_controller.ArmDriver"
_POS_MGR_PATH = "eit_agv.controller.agv_controller.PositionManager"


def _make_controller():
    """
    功能:
        创建 AGVController 实例, 并在构造阶段替换硬件相关依赖, 避免连接真实设备.
    参数:
        无.
    返回:
        AGVController, 可用于自动充电逻辑测试的控制器实例.
    """
    with patch(_ARM_DRIVER_PATH), patch(_POS_MGR_PATH):
        from eit_agv.controller.agv_controller import AGVController

        return AGVController()


class TestAutoChargeCheckRegression(unittest.TestCase):
    """
    功能:
        自动充电检查回归测试套件.
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

    def test_cp6_sufficient_battery_no_navigation(self):
        """
        功能:
            验证 AGV 在 CP6 且电量充足时, 不触发任何导航动作.
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
            return_value={"battery_level": 0.6, "ret_code": 0},
        ) as mock_query_battery, patch.object(
            self.controller,
            "safe_navigate_to_station",
        ) as mock_safe_nav:
            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "battery_sufficient")
        self.assertAlmostEqual(result["battery_level"], 0.6)
        mock_query_battery.assert_called_once_with(simple=True)
        mock_safe_nav.assert_not_called()

    def test_cp6_low_battery_already_charging_skip_navigation(self):
        """
        功能:
            验证 AGV 在 CP6 且电量低于50%但已经在充电时, 不执行进出站动作.
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
            side_effect=[
                {"battery_level": 0.45, "ret_code": 0},
                {"battery_level": 0.45, "charging": True, "ret_code": 0},
            ],
        ) as mock_query_battery, patch.object(
            self.controller,
            "safe_navigate_to_station",
        ) as mock_safe_nav:
            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "already_charging")
        self.assertAlmostEqual(result["battery_level"], 0.45)
        self.assertIs(result["charging"], True)
        self.assertEqual(
            mock_query_battery.call_args_list,
            [call(simple=True), call(simple=False)],
        )
        mock_safe_nav.assert_not_called()

    def test_cp6_low_battery_not_charging_runs_redock_cycle(self):
        """
        功能:
            验证 AGV 在 CP6 且低电量未充电时, 会执行 PP5 到 CP6 的重对接流程.
        参数:
            无.
        返回:
            无.
        """
        with patch.object(
            self.controller,
            "query_nav_task_status",
            side_effect=[
                {"task_status": 0, "task_status_name": "NONE"},
                {"task_status": 0, "task_status_name": "NONE"},
            ],
        ), patch.object(
            self.controller,
            "query_current_station",
            return_value={"station_id": "CP6", "station_name": "charging_station"},
        ), patch.object(
            self.controller,
            "query_battery_status",
            side_effect=[
                {"battery_level": 0.45, "ret_code": 0},
                {"battery_level": 0.45, "charging": False, "ret_code": 0},
                {"battery_level": 0.45, "charging": True, "ret_code": 0},
            ],
        ) as mock_query_battery, patch.object(
            self.controller,
            "safe_navigate_to_station",
            side_effect=[{"ret_code": 0}, {"ret_code": 0}],
        ) as mock_safe_nav:
            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "charge_cycle_completed")
        self.assertAlmostEqual(result["battery_level"], 0.45)
        self.assertIs(result["charging"], True)
        self.assertEqual(
            mock_query_battery.call_args_list,
            [call(simple=True), call(simple=False), call(simple=False)],
        )
        self.assertEqual(
            mock_safe_nav.call_args_list,
            [call("PP5"), call("CP6")],
        )

    def test_cp6_low_battery_unknown_charging_state_skip_navigation(self):
        """
        功能:
            验证 AGV 在 CP6 且低电量但无法确认充电状态时, 保守跳过进出站.
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
            side_effect=[
                {"battery_level": 0.45, "ret_code": 0},
                {"battery_level": 0.45, "ret_code": 0},
            ],
        ) as mock_query_battery, patch.object(
            self.controller,
            "safe_navigate_to_station",
        ) as mock_safe_nav:
            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["action"], "charging_state_unknown")
        self.assertAlmostEqual(result["battery_level"], 0.45)
        self.assertEqual(
            mock_query_battery.call_args_list,
            [call(simple=True), call(simple=False)],
        )
        mock_safe_nav.assert_not_called()

    def test_nav_busy_skips_charge_check(self):
        """
        功能:
            验证 AGV 导航忙碌时, 自动充电检查直接跳过.
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
        ) as mock_query_battery, patch.object(
            self.controller,
            "safe_navigate_to_station",
        ) as mock_safe_nav:
            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["action"], "skipped_busy_nav")
        self.assertEqual(result["nav_task_status"], 2)
        mock_query_station.assert_not_called()
        mock_query_battery.assert_not_called()
        mock_safe_nav.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
