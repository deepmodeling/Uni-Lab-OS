# coding: utf-8
"""
功能:
    AGV通信重试与重连机制的单元测试
    覆盖场景:
        1. 控制器层 _query_with_retry 通用重试逻辑
        2. query_current_station / query_battery_status / query_nav_task_status 重试
        3. 驱动层 navigate_to_target 轮询循环的socket重连逻辑
        4. AGVDriver.reconnect 方法本身的行为
        5. auto_charge_check 在重试机制下的集成验证

运行方式(从 devices/ 目录):
    python -m pytest eit_agv/tests/test_agv_comm_retry.py -v
"""

import socket
import unittest
from unittest.mock import MagicMock, patch

# patch路径, 与现有测试保持一致
_ARM_DRIVER_PATH = "eit_agv.controller.agv_controller.ArmDriver"
_POS_MGR_PATH = "eit_agv.controller.agv_controller.PositionManager"
_AGV_DRIVER_CLS_PATH = "eit_agv.controller.agv_controller.AGVDriver"
_CONTROLLER_SLEEP_PATH = "eit_agv.controller.agv_controller.time.sleep"
_DRIVER_SLEEP_PATH = "eit_agv.driver.agv_driver.time.sleep"


def _make_controller():
    """
    功能:
        创建AGVController实例, 替换硬件依赖, 避免连接真实设备
    返回:
        AGVController实例
    """
    with patch(_ARM_DRIVER_PATH), patch(_POS_MGR_PATH):
        from eit_agv.controller.agv_controller import AGVController
        return AGVController()


# =====================================================================
# 控制器层: _query_with_retry 通用重试机制
# =====================================================================

class TestQueryWithRetry(unittest.TestCase):
    """测试 _query_with_retry 通用重试逻辑"""

    def setUp(self):
        self.controller = _make_controller()

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_first_attempt_succeeds_no_sleep(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            首次查询成功
        预期:
            直接返回结果, 不调用sleep(无需退避等待)
        """
        mock_driver = MagicMock()
        mock_driver_cls.return_value = mock_driver

        query_result = {"task_status": 0}
        query_func = MagicMock(return_value=query_result)

        result = self.controller._query_with_retry(query_func, "测试查询")

        self.assertEqual(result, query_result)
        query_func.assert_called_once()
        mock_sleep.assert_not_called()

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_retry_on_timeout_then_succeeds(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            第一次connect超时, 第二次查询成功
        预期:
            返回第二次的结果, sleep被调用一次(退避等待)
        """
        # 第一个driver: connect超时
        mock_driver_fail = MagicMock()
        mock_driver_fail.connect.side_effect = socket.timeout("timed out")
        # 第二个driver: 正常
        mock_driver_ok = MagicMock()
        mock_driver_cls.side_effect = [mock_driver_fail, mock_driver_ok]

        query_result = {"station_id": "CP6"}
        # query_func只在第二次被调用时能成功(第一次connect就抛异常了)
        query_func = MagicMock(return_value=query_result)

        result = self.controller._query_with_retry(query_func, "测试查询")

        self.assertEqual(result, query_result)
        # sleep在重试前调用了一次
        mock_sleep.assert_called_once()
        # 两个driver都被close了
        mock_driver_fail.close.assert_called_once()
        mock_driver_ok.close.assert_called_once()

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_all_retries_exhausted_returns_none(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            所有重试(默认3次)都超时
        预期:
            返回None
        """
        mock_driver = MagicMock()
        mock_driver.connect.side_effect = socket.timeout("timed out")
        mock_driver_cls.return_value = mock_driver

        query_func = MagicMock()

        result = self.controller._query_with_retry(query_func, "测试查询")

        self.assertIsNone(result)

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_exponential_backoff_delay(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            3次重试均失败
        预期:
            sleep的退避时间为 1.0s, 2.0s (指数退避, 最后一次失败不sleep)
        """
        mock_driver = MagicMock()
        mock_driver.connect.side_effect = socket.timeout("timed out")
        mock_driver_cls.return_value = mock_driver

        query_func = MagicMock()

        self.controller._query_with_retry(
            query_func, "测试查询", max_retries=3, retry_delay=1.0
        )

        # 第1次失败 → sleep(1.0), 第2次失败 → sleep(2.0), 第3次失败 → 不sleep
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(2.0)

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_query_func_exception_triggers_retry(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            connect成功, 但query_func第一次抛异常, 第二次返回结果
        预期:
            重试后返回正确结果
        """
        mock_driver = MagicMock()
        mock_driver_cls.return_value = mock_driver

        query_func = MagicMock(
            side_effect=[RuntimeError("AGV返回错误"), {"result": "ok"}]
        )

        result = self.controller._query_with_retry(query_func, "测试查询")

        self.assertEqual(result, {"result": "ok"})
        self.assertEqual(query_func.call_count, 2)


# =====================================================================
# 控制器层: 三个查询方法各自的重试验证
# =====================================================================

class TestQueryCurrentStationRetry(unittest.TestCase):
    """测试 query_current_station 的重试机制"""

    def setUp(self):
        self.controller = _make_controller()

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_station_query_retry_success_on_second_attempt(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            第一次connect超时, 第二次查询到CP6
        预期:
            返回CP6站点信息
        """
        mock_driver_fail = MagicMock()
        mock_driver_fail.connect.side_effect = socket.timeout("timed out")

        mock_driver_ok = MagicMock()
        mock_driver_ok.query_robot_location.return_value = {
            "current_station": "CP6"
        }

        mock_driver_cls.side_effect = [mock_driver_fail, mock_driver_ok]

        result = self.controller.query_current_station()

        self.assertIsNotNone(result)
        self.assertEqual(result["station_id"], "CP6")
        self.assertEqual(result["station_name"], "charging_station")

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_station_query_all_fail_returns_none(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            所有重试均超时
        预期:
            返回None
        """
        mock_driver = MagicMock()
        mock_driver.connect.side_effect = socket.timeout("timed out")
        mock_driver_cls.return_value = mock_driver

        result = self.controller.query_current_station()

        self.assertIsNone(result)


class TestQueryBatteryStatusRetry(unittest.TestCase):
    """测试 query_battery_status 的重试机制"""

    def setUp(self):
        self.controller = _make_controller()

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_battery_query_retry_success(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            第一次超时, 第二次成功返回电量60%
        预期:
            返回包含电池信息的dict
        """
        mock_driver_fail = MagicMock()
        mock_driver_fail.connect.side_effect = socket.timeout("timed out")

        mock_driver_ok = MagicMock()
        mock_driver_ok.query_battery_status.return_value = {
            "battery_level": 0.6, "ret_code": 0
        }

        mock_driver_cls.side_effect = [mock_driver_fail, mock_driver_ok]

        result = self.controller.query_battery_status(simple=True)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["battery_level"], 0.6)

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_battery_query_ret_code_error_triggers_retry(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            第一次返回ret_code非0(业务错误), 第二次正常
        预期:
            业务错误也触发重试, 最终返回正确结果
        """
        mock_driver_1 = MagicMock()
        mock_driver_1.query_battery_status.return_value = {
            "ret_code": -1, "err_msg": "AGV忙碌"
        }

        mock_driver_2 = MagicMock()
        mock_driver_2.query_battery_status.return_value = {
            "battery_level": 0.45, "ret_code": 0
        }

        mock_driver_cls.side_effect = [mock_driver_1, mock_driver_2]

        result = self.controller.query_battery_status(simple=True)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["battery_level"], 0.45)


class TestQueryNavTaskStatusRetry(unittest.TestCase):
    """测试 query_nav_task_status 的重试机制"""

    def setUp(self):
        self.controller = _make_controller()

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_nav_status_retry_success(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            第一次超时, 第二次查询到NONE状态
        预期:
            返回task_status=0
        """
        mock_driver_fail = MagicMock()
        mock_driver_fail.connect.side_effect = socket.timeout("timed out")

        mock_driver_ok = MagicMock()
        mock_driver_ok.query_agv_nav_status.return_value = {"task_status": 0}

        mock_driver_cls.side_effect = [mock_driver_fail, mock_driver_ok]

        result = self.controller.query_nav_task_status()

        self.assertIsNotNone(result)
        self.assertEqual(result["task_status"], 0)
        self.assertEqual(result["task_status_name"], "NONE")

    @patch(_CONTROLLER_SLEEP_PATH)
    @patch(_AGV_DRIVER_CLS_PATH)
    def test_nav_status_returns_none_triggers_retry(self, mock_driver_cls, mock_sleep):
        """
        验收条件:
            第一次query_agv_nav_status返回None, 第二次正常
        预期:
            None结果被当作异常触发重试, 最终返回正确结果
        """
        mock_driver_1 = MagicMock()
        mock_driver_1.query_agv_nav_status.return_value = None

        mock_driver_2 = MagicMock()
        mock_driver_2.query_agv_nav_status.return_value = {"task_status": 2}

        mock_driver_cls.side_effect = [mock_driver_1, mock_driver_2]

        result = self.controller.query_nav_task_status()

        self.assertIsNotNone(result)
        self.assertEqual(result["task_status"], 2)
        self.assertEqual(result["task_status_name"], "RUNNING")


# =====================================================================
# 驱动层: AGVDriver.reconnect 方法
# =====================================================================

class TestReconnectMethod(unittest.TestCase):
    """测试 AGVDriver.reconnect 方法"""

    @patch("eit_agv.driver.agv_driver.socket.socket")
    def test_reconnect_closes_old_socket_and_creates_new(self, mock_socket_cls):
        """
        验收条件:
            已有旧socket的情况下调用reconnect
        预期:
            旧socket被关闭, 新socket被创建并连接
        """
        from eit_agv.driver.agv_driver import AGVDriver, AGVDriverConfig

        driver = AGVDriver(AGVDriverConfig(
            host="127.0.0.1",
            port=19204,
            port_navigation=19206,
            timeout_s=1.0,
        ))

        # 模拟旧socket
        old_sock = MagicMock()
        driver._sock = old_sock

        # 新socket
        new_sock = MagicMock()
        mock_socket_cls.return_value = new_sock

        driver.reconnect()

        old_sock.close.assert_called_once()
        new_sock.settimeout.assert_called_once_with(1.0)
        new_sock.connect.assert_called_once_with(("127.0.0.1", 19204))
        self.assertIs(driver._sock, new_sock)

    @patch("eit_agv.driver.agv_driver.socket.socket")
    def test_reconnect_without_existing_socket(self, mock_socket_cls):
        """
        验收条件:
            无旧socket时调用reconnect
        预期:
            直接创建新连接, 不抛异常
        """
        from eit_agv.driver.agv_driver import AGVDriver, AGVDriverConfig

        driver = AGVDriver(AGVDriverConfig(
            host="127.0.0.1",
            port=19204,
            port_navigation=19206,
            timeout_s=1.0,
        ))
        driver._sock = None

        new_sock = MagicMock()
        mock_socket_cls.return_value = new_sock

        driver.reconnect()

        new_sock.connect.assert_called_once_with(("127.0.0.1", 19204))
        self.assertIs(driver._sock, new_sock)

    @patch("eit_agv.driver.agv_driver.socket.socket")
    def test_reconnect_navigation_closes_old_and_creates_new(self, mock_socket_cls):
        """
        验收条件:
            已有旧导航socket的情况下调用reconnect_navigation
        预期:
            旧导航socket被关闭, 新导航socket被创建并连接到导航端口
        """
        from eit_agv.driver.agv_driver import AGVDriver, AGVDriverConfig

        driver = AGVDriver(AGVDriverConfig(
            host="127.0.0.1",
            port=19204,
            port_navigation=19206,
            timeout_s=1.0,
        ))

        old_nav_sock = MagicMock()
        driver._sock_nav = old_nav_sock

        new_sock = MagicMock()
        mock_socket_cls.return_value = new_sock

        driver.reconnect_navigation()

        old_nav_sock.close.assert_called_once()
        new_sock.connect.assert_called_once_with(("127.0.0.1", 19206))
        self.assertIs(driver._sock_nav, new_sock)


# =====================================================================
# 驱动层: navigate_to_target 轮询循环重连
# =====================================================================

class TestNavigateToTargetReconnect(unittest.TestCase):
    """测试 navigate_to_target 轮询循环中的socket重连逻辑"""

    def _make_driver(self):
        """
        功能:
            创建AGVDriver测试实例
        返回:
            AGVDriver实例
        """
        from eit_agv.driver.agv_driver import AGVDriver, AGVDriverConfig
        driver = AGVDriver(AGVDriverConfig(
            host="127.0.0.1",
            port=19204,
            port_navigation=19206,
            timeout_s=1.0,
            debug_hex=False,
        ))
        return driver

    @patch(_DRIVER_SLEEP_PATH)
    def test_reconnect_on_connection_reset_then_succeeds(self, mock_sleep):
        """
        验收条件:
            轮询过程中连接被重置(WinError 10054), 重连后查询成功
        预期:
            调用reconnect一次, 最终返回COMPLETED状态
        """
        driver = self._make_driver()
        driver._sock_nav = MagicMock()
        driver._sock = MagicMock()

        nav_response = {"ret_code": 0}
        completed_status = {"task_status": 4, "target_id": "LM1"}

        with patch.object(driver, "_send_and_recv_json") as mock_send_recv, \
             patch.object(driver, "connect"), \
             patch.object(driver, "connect_navigation"), \
             patch.object(driver, "reconnect") as mock_reconnect:

            mock_send_recv.side_effect = [
                nav_response,                                    # 发送导航指令成功
                ConnectionResetError("[WinError 10054]"),        # 查询状态时连接被重置
                completed_status,                                # 重连后查询成功
            ]

            result = driver.navigate_to_target(target_id="LM1")

        self.assertEqual(result["task_status"], 4)
        mock_reconnect.assert_called_once()

    @patch(_DRIVER_SLEEP_PATH)
    def test_reconnect_exhausted_raises_connection_error(self, mock_sleep):
        """
        验收条件:
            连续多次重连全部失败
        预期:
            抛出ConnectionError, 不会无限循环
        """
        driver = self._make_driver()
        driver._sock_nav = MagicMock()
        driver._sock = MagicMock()

        with patch.object(driver, "_send_and_recv_json") as mock_send_recv, \
             patch.object(driver, "connect"), \
             patch.object(driver, "connect_navigation"), \
             patch.object(driver, "reconnect", side_effect=OSError("连接失败")):

            mock_send_recv.side_effect = [
                {"ret_code": 0},  # 导航指令发送成功
            ] + [ConnectionResetError("[WinError 10054]")] * 10

            with self.assertRaises(ConnectionError):
                driver.navigate_to_target(target_id="LM1")

    @patch(_DRIVER_SLEEP_PATH)
    def test_intermittent_errors_reset_counter_on_success(self, mock_sleep):
        """
        验收条件:
            间歇性连接错误: 错误→成功(RUNNING)→错误→成功(COMPLETED)
        预期:
            成功查询会重置连续错误计数, 不会累积触发放弃
        """
        driver = self._make_driver()
        driver._sock_nav = MagicMock()
        driver._sock = MagicMock()

        running_status = {"task_status": 2}     # RUNNING
        completed_status = {"task_status": 4}   # COMPLETED

        with patch.object(driver, "_send_and_recv_json") as mock_send_recv, \
             patch.object(driver, "connect"), \
             patch.object(driver, "connect_navigation"), \
             patch.object(driver, "reconnect") as mock_reconnect:

            mock_send_recv.side_effect = [
                {"ret_code": 0},                         # 导航指令发送
                ConnectionResetError("reset 1"),         # 第1次连接错误
                running_status,                          # 重连后查询成功(计数归零)
                ConnectionResetError("reset 2"),         # 第2次连接错误(从0开始)
                completed_status,                        # 重连后导航完成
            ]

            result = driver.navigate_to_target(target_id="LM1")

        self.assertEqual(result["task_status"], 4)
        self.assertEqual(mock_reconnect.call_count, 2)

    @patch(_DRIVER_SLEEP_PATH)
    def test_non_connection_error_also_counted(self, mock_sleep):
        """
        验收条件:
            非连接类异常(如ValueError)连续发生达到上限
        预期:
            同样触发终止, 抛出异常
        """
        driver = self._make_driver()
        driver._sock_nav = MagicMock()
        driver._sock = MagicMock()

        with patch.object(driver, "_send_and_recv_json") as mock_send_recv, \
             patch.object(driver, "connect"), \
             patch.object(driver, "connect_navigation"):

            mock_send_recv.side_effect = [
                {"ret_code": 0},  # 导航指令发送成功
            ] + [ValueError("解析响应失败")] * 10

            with self.assertRaises(ValueError):
                driver.navigate_to_target(target_id="LM1")


# =====================================================================
# 集成层: auto_charge_check 在重试机制下的行为
# =====================================================================

class TestAutoChargeCheckWithRetry(unittest.TestCase):
    """验证 auto_charge_check 在查询方法带重试机制后仍能正常工作"""

    def setUp(self):
        self.controller = _make_controller()

    def test_auto_charge_check_normal_flow(self):
        """
        验收条件:
            所有查询方法正常返回(内部可能经历了重试, 但外部看不到)
        预期:
            auto_charge_check 正常返回 battery_sufficient
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
        ):
            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "battery_sufficient")

    def test_auto_charge_check_nav_query_none_continues(self):
        """
        验收条件:
            query_nav_task_status 所有重试耗尽返回None
        预期:
            nav_status为None时不进入busy分支, 继续查询位置和电量, 正常完成检查
        """
        with patch.object(
            self.controller, "query_nav_task_status",
            return_value=None
        ), patch.object(
            self.controller, "query_current_station",
            return_value={"station_id": "CP6", "station_name": "charging_station"}
        ), patch.object(
            self.controller, "query_battery_status",
            return_value={"battery_level": 0.6, "ret_code": 0}
        ):
            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "battery_sufficient")

    def test_auto_charge_check_station_query_none_returns_error(self):
        """
        验收条件:
            query_current_station 所有重试耗尽返回None
        预期:
            auto_charge_check 返回 error + query_location
        """
        with patch.object(
            self.controller, "query_nav_task_status",
            return_value={"task_status": 0, "task_status_name": "NONE"}
        ), patch.object(
            self.controller, "query_current_station",
            return_value=None
        ):
            result = self.controller.auto_charge_check()

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["action"], "query_location")


if __name__ == "__main__":
    unittest.main(verbosity=2)
