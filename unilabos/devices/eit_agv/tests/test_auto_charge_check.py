# coding: utf-8
"""
功能:
    auto_charge_check 验收测试
    覆盖5个测试场景:
        1. 空闲 + 电量60%        → battery_sufficient, 无移动
        2. 空闲 + 电量45% + 不在CP6 → low_battery_return_to_cp6
        3. 任务锁有效 + 电量45%   → skipped / task_locked
        4. 仅导航忙碌 + 电量45%   → skipped / skipped_busy_nav
        5. 锁文件心跳超时         → 视为过期清理, 后续正常判定

运行方式(从 devices/ 目录):
    python -m pytest eit_agv/tests/test_auto_charge_check.py -v
"""

import unittest

def load_tests(loader, tests, pattern):
    """
    功能:
        兼容旧测试模块路径, 并避免在 unittest discover 时重复加载新回归用例.
    参数:
        loader: unittest 测试加载器.
        tests: 默认测试集合.
        pattern: 测试发现模式.
    返回:
        unittest.TestSuite, 直接运行旧模块时转发到新回归用例, discover 时返回空集合.
    """
    if pattern is not None:
        return loader.suiteClass()

    from eit_agv.tests import test_auto_charge_check_regression

    return loader.loadTestsFromModule(test_auto_charge_check_regression)

import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch


# 以下 patch 在 AGVController 被导入前替换驱动, 避免连接真实设备
_ARM_DRIVER_PATH = "eit_agv.controller.agv_controller.ArmDriver"
_POS_MGR_PATH = "eit_agv.controller.agv_controller.PositionManager"


def _make_controller(lock_file_path: str):
    """
    功能:
        创建 AGVController 实例, 将机械臂驱动和位置管理器全部 mock 掉
    参数:
        lock_file_path: 测试用临时锁文件路径
    返回:
        AGVController 实例
    """
    with patch(_ARM_DRIVER_PATH), patch(_POS_MGR_PATH):
        from eit_agv.controller.agv_controller import AGVController
        return AGVController(lock_file_path=lock_file_path)


class TestAutoChargeCheck(unittest.TestCase):
    """auto_charge_check 验收测试套件"""

    def setUp(self):
        # 每个用例使用独立临时目录, 保证锁文件互不干扰
        self.tmp_dir = tempfile.mkdtemp()
        self.lock_file = os.path.join(self.tmp_dir, "agv_task.lock.json")
        self.controller = _make_controller(self.lock_file)

    def tearDown(self):
        # 确保锁已释放, 清理临时文件
        try:
            self.controller.release_task_lock()
        except Exception:
            pass
        if os.path.exists(self.lock_file):
            os.remove(self.lock_file)
        os.rmdir(self.tmp_dir)

    # ------------------------------------------------------------------
    # 场景1: 空闲 + 电量60%
    # ------------------------------------------------------------------

    def test_scenario1_idle_sufficient_battery(self):
        """
        验收条件:
            空闲(无锁, 导航NONE) + 在CP6 + 电量60%
        预期:
            返回 action=battery_sufficient, 不调用任何导航方法
        """
        with patch.object(
            self.controller, "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"}
        ), patch.object(
            self.controller, "query_current_station",
            return_value={"station_id": "CP6", "station_name": "charging_station"}
        ), patch.object(
            self.controller, "query_battery_status",
            return_value={"battery_level": 0.6, "ret_code": 0}
        ), patch.object(
            self.controller, "go_to_charging_station"
        ) as mock_go, patch.object(
            self.controller, "safe_navigate_to_station"
        ) as mock_safe_nav:

            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "battery_sufficient")
        self.assertAlmostEqual(result["battery_level"], 0.6)
        # 不允许发出任何导航命令
        mock_go.assert_not_called()
        mock_safe_nav.assert_not_called()

    # ------------------------------------------------------------------
    # 场景2: 空闲 + 电量45% + 不在CP6
    # ------------------------------------------------------------------

    def test_scenario2_low_battery_not_at_cp6(self):
        """
        验收条件:
            空闲(无锁, 导航NONE) + 在LM1 + 电量45%
        预期:
            返回 action=low_battery_return_to_cp6, 调用 go_to_charging_station
        """
        with patch.object(
            self.controller, "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"}
        ), patch.object(
            self.controller, "query_current_station",
            return_value={"station_id": "LM1", "station_name": "synthesis_station"}
        ), patch.object(
            self.controller, "query_battery_status",
            return_value={"battery_level": 0.45, "ret_code": 0}
        ), patch.object(
            self.controller, "go_to_charging_station",
            return_value={"ret_code": 0}
        ) as mock_go, patch.object(
            self.controller, "safe_navigate_to_station"
        ) as mock_safe_nav:

            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "low_battery_return_to_cp6")
        self.assertAlmostEqual(result["battery_level"], 0.45)
        # 必须调用了导航回CP6
        mock_go.assert_called_once()
        # 不应触发PP5充电循环
        mock_safe_nav.assert_not_called()

    # ------------------------------------------------------------------
    # 场景3: 执行 batch_transfer_materials 期间 + 电量45%
    # ------------------------------------------------------------------

    def test_scenario3_task_locked_skip(self):
        """
        验收条件:
            任务锁有效(batch_transfer_materials) + 电量45%
        预期:
            返回 status=skipped, action=task_locked
            不发送任何导航命令
        """
        # 模拟任务正在执行, 持有锁
        self.controller.acquire_task_lock("batch_transfer_materials")

        with patch.object(
            self.controller, "go_to_charging_station"
        ) as mock_go, patch.object(
            self.controller, "safe_navigate_to_station"
        ) as mock_safe_nav:

            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["action"], "task_locked")
        self.assertEqual(result["task_name"], "batch_transfer_materials")
        mock_go.assert_not_called()
        mock_safe_nav.assert_not_called()

    # ------------------------------------------------------------------
    # 场景4: 仅导航忙碌(无锁文件) + 电量45%
    # ------------------------------------------------------------------

    def test_scenario4_nav_busy_no_lock(self):
        """
        验收条件:
            无锁文件, 但 AGV 导航状态为 RUNNING(2) + 电量45%
        预期:
            返回 status=skipped, action=skipped_busy_nav
            不发送任何导航命令
        """
        # 确认无锁文件
        self.assertFalse(os.path.exists(self.lock_file))

        with patch.object(
            self.controller, "query_nav_task_status",
            return_value={"task_status": 2, "task_status_name": "RUNNING"}
        ), patch.object(
            self.controller, "go_to_charging_station"
        ) as mock_go, patch.object(
            self.controller, "safe_navigate_to_station"
        ) as mock_safe_nav:

            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["action"], "skipped_busy_nav")
        self.assertEqual(result["nav_task_status"], 2)
        mock_go.assert_not_called()
        mock_safe_nav.assert_not_called()

    def test_scenario4_nav_waiting_no_lock(self):
        """导航状态 WAITING(1) 同样触发 skipped_busy_nav"""
        with patch.object(
            self.controller, "query_nav_task_status",
            return_value={"task_status": 1, "task_status_name": "WAITING"}
        ), patch.object(self.controller, "go_to_charging_station") as mock_go:

            result = self.controller.auto_charge_check()

        self.assertEqual(result["action"], "skipped_busy_nav")
        mock_go.assert_not_called()

    # ------------------------------------------------------------------
    # 场景5: 锁文件存在但心跳超时
    # ------------------------------------------------------------------

    def test_scenario5_expired_lock_cleared_and_charged(self):
        """
        验收条件:
            锁文件存在, 但 heartbeat_ts 超过 heartbeat_timeout(90s)
            AGV 在 CP6, 电量60%
        预期:
            锁被视为过期并自动清理
            后续按正常逻辑判定 → battery_sufficient
            锁文件不再存在
        """
        # 手动写入一个已过期的锁文件(心跳200秒前)
        expired_lock = {
            "pid": 99999,
            "task_name": "old_stale_task",
            "start_ts": time.time() - 400,
            "heartbeat_ts": time.time() - 200,   # 默认 timeout=90s, 200s > 90s
        }
        with open(self.lock_file, "w", encoding="utf-8") as f:
            json.dump(expired_lock, f)

        # 锁文件确实存在
        self.assertTrue(os.path.exists(self.lock_file))

        with patch.object(
            self.controller, "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"}
        ), patch.object(
            self.controller, "query_current_station",
            return_value={"station_id": "CP6", "station_name": "charging_station"}
        ), patch.object(
            self.controller, "query_battery_status",
            return_value={"battery_level": 0.6, "ret_code": 0}
        ), patch.object(
            self.controller, "go_to_charging_station"
        ) as mock_go:

            result = self.controller.auto_charge_check()

        # 过期锁已被自动清理
        self.assertFalse(
            os.path.exists(self.lock_file),
            "过期锁文件应被自动清理"
        )
        # 按正常逻辑判定: 电量充足
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "battery_sufficient")
        mock_go.assert_not_called()

    def test_scenario5_expired_lock_cleared_then_charge(self):
        """
        验收条件:
            锁文件过期, AGV 在 CP6, 电量45%
        预期:
            锁清理后触发充电循环(charge_cycle_completed)
        """
        expired_lock = {
            "pid": 99999,
            "task_name": "old_stale_task",
            "start_ts": time.time() - 400,
            "heartbeat_ts": time.time() - 200,
        }
        with open(self.lock_file, "w", encoding="utf-8") as f:
            json.dump(expired_lock, f)

        with patch.object(
            self.controller, "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"}
        ), patch.object(
            self.controller, "query_current_station",
            return_value={"station_id": "CP6", "station_name": "charging_station"}
        ), patch.object(
            self.controller, "query_battery_status",
            return_value={"battery_level": 0.45, "ret_code": 0}
        ), patch.object(
            self.controller, "safe_navigate_to_station",
            return_value={"ret_code": 0}
        ) as mock_safe_nav:

            result = self.controller.auto_charge_check()

        self.assertFalse(os.path.exists(self.lock_file), "过期锁文件应被自动清理")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "charge_cycle_completed")
        # PP5 和 CP6 各调用一次
        self.assertEqual(mock_safe_nav.call_count, 2)
        calls = [c.args[0] for c in mock_safe_nav.call_args_list]
        self.assertIn("PP5", calls)
        self.assertIn("CP6", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
