# opsky_atr30007.py
import logging
import time as time_mod
import csv
from datetime import datetime
from typing import Optional, Dict, Any

# 兼容 pymodbus 在不同版本中的位置与 API
try:
    from pymodbus.client import ModbusTcpClient
except Exception:
    ModbusTcpClient = None

# 导入 run_raman_test（假定与本文件同目录）
# 如果你的项目是包结构且原先使用相对导入，请改回 `from .raman_module import run_raman_test`
try:
    from .raman_module import run_raman_test
except Exception:
    # 延迟导入失败不会阻止主流程（在 run 时会再尝试）
    run_raman_test = None

logger = logging.getLogger("opsky")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%y-%m-%d %H:%M:%S")
ch.setFormatter(formatter)
logger.addHandler(ch)

class opsky_ATR30007:
    """
    封装 UniLabOS 设备动作逻辑，兼容 pymodbus 2.x / 3.x。
    放在独立文件中：opsky_atr30007.py
    """

    def __init__(
        self,
        plc_ip: str = "192.168.1.88",
        plc_port: int = 502,
        robot_ip: str = "192.168.1.200",
        robot_port: int = 502,
        scan_csv_file: str = "scan_results.csv",
    ):
        self.plc_ip = plc_ip
        self.plc_port = plc_port
        self.robot_ip = robot_ip
        self.robot_port = robot_port
        self.scan_csv_file = scan_csv_file

    # ----------------- 参数字符串转换 helpers -----------------
    @staticmethod
    def _str_to_int(s, default):
        try:
            return int(float(str(s).strip()))
        except Exception:
            return int(default)

    @staticmethod
    def _str_to_float(s, default):
        try:
            return float(str(s).strip())
        except Exception:
            return float(default)

    @staticmethod
    def _str_to_bool(s, default):
        try:
            v = str(s).strip().lower()
            if v in ("true", "1", "yes", "y", "t"):
                return True
            if v in ("false", "0", "no", "n", "f"):
                return False
            return default
        except Exception:
            return default

    # ----------------- Modbus / 安全读写 -----------------
    @staticmethod
    def _adapt_req_kwargs_for_read(func_name: str, args: tuple, kwargs: dict):
        # 如果调用方传的是 (address, count) positional，在新版接口可能是 address=..., count=...
        if len(args) == 2 and func_name.startswith("read_"):
            address, count = args
            args = ()
            kwargs.setdefault("address", address)
            kwargs.setdefault("count", count)
        return args, kwargs

    @staticmethod
    def _adapt_req_kwargs_for_write(func_name: str, args: tuple, kwargs: dict):
        if len(args) == 2 and func_name.startswith("write_"):
            address, value = args
            args = ()
            kwargs.setdefault("address", address)
            kwargs.setdefault("value", value)
        return args, kwargs

    def ensure_connected(self, client, name, ip, port):
        """确保连接存在，失败则尝试重连并返回新的 client 或 None"""
        if client is None:
            return None
        try:
            # 不同 pymodbus 版本可能有不同方法检测 socket
            is_open = False
            try:
                is_open = bool(client.is_socket_open())
            except Exception:
                # fallback: try to read nothing or attempt connection test
                try:
                    # 轻试一次
                    is_open = client.connected if hasattr(client, "connected") else False
                except Exception:
                    is_open = False

            if not is_open:
                logger.warning("%s 掉线，尝试重连...", name)
                try:
                    client.close()
                except Exception:
                    pass
                time_mod.sleep(0.5)
                if ModbusTcpClient:
                    new_client = ModbusTcpClient(ip, port=port)
                    try:
                        if new_client.connect():
                            logger.info("%s 重新连接成功 (%s:%s)", name, ip, port)
                            return new_client
                    except Exception:
                        pass
                logger.warning("%s 重连失败", name)
                time_mod.sleep(1)
                return None
            return client
        except Exception as e:
            logger.exception("%s 连接检查异常: %s", name, e)
            return None

    def safe_read(self, client, name, func, *args, retries=3, delay=0.3, **kwargs):
        """兼容 pymodbus 2.x/3.x 的读函数，返回 response 或 None"""
        if client is None:
            return None
        for attempt in range(1, retries + 1):
            try:
                # adapt args/kwargs for different API styles
                args, kwargs = self._adapt_req_kwargs_for_read(func.__name__, args, kwargs)
                # unit->slave compatibility
                if "unit" in kwargs:
                    kwargs["slave"] = kwargs.pop("unit")
                res = func(*args, **kwargs)
                # pymodbus Response 在不同版本表现不同，尽量检测错误
                if res is None:
                    raise RuntimeError("返回 None")
                if hasattr(res, "isError") and res.isError():
                    raise RuntimeError("Modbus 返回 isError()")
                return res
            except Exception as e:
                logger.warning("%s 读异常 (尝试 %d/%d): %s", name, attempt, retries, e)
            time_mod.sleep(delay)
        logger.error("%s 连续读取失败 %d 次", name, retries)
        return None

    def safe_write(self, client, name, func, *args, retries=3, delay=0.3, **kwargs):
        """兼容 pymodbus 2.x/3.x 的写函数，返回 True/False"""
        if client is None:
            return False
        for attempt in range(1, retries + 1):
            try:
                args, kwargs = self._adapt_req_kwargs_for_write(func.__name__, args, kwargs)
                if "unit" in kwargs:
                    kwargs["slave"] = kwargs.pop("unit")
                res = func(*args, **kwargs)
                if res is None:
                    raise RuntimeError("返回 None")
                if hasattr(res, "isError") and res.isError():
                    raise RuntimeError("Modbus 返回 isError()")
                return True
            except Exception as e:
                logger.warning("%s 写异常 (尝试 %d/%d): %s", name, attempt, retries, e)
            time_mod.sleep(delay)
        logger.error("%s 连续写入失败 %d 次", name, retries)
        return False

    def wait_with_quit_check(self, robot, seconds, addr_quit=270):
        """等待指定时间，同时每 0.2s 检查 R270 是否为 1（立即退出）"""
        if robot is None:
            time_mod.sleep(seconds)
            return False
        checks = max(1, int(seconds / 0.2))
        for _ in range(checks):
            rr = self.safe_read(robot, "机器人", robot.read_holding_registers, address=addr_quit, count=1)
            if rr and getattr(rr, "registers", [None])[0] == 1:
                logger.info("检测到 R270=1，立即退出等待")
                return True
            time_mod.sleep(0.2)
        return False

    # ----------------- 主流程 run_once -----------------
    def run_once(
        self,
        integration_time: str = "5000",
        laser_power: str = "200",
        save_csv: str = "true",
        save_plot: str = "true",
        normalize: str = "true",
        norm_max: str = "1.0",
        **_: Any,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {"success": False, "event": "none", "details": {}}

        integration_time_v = self._str_to_int(integration_time, 5000)
        laser_power_v = self._str_to_int(laser_power, 200)
        save_csv_v = self._str_to_bool(save_csv, True)
        save_plot_v = self._str_to_bool(save_plot, True)
        normalize_v = self._str_to_bool(normalize, True)
        norm_max_v = None if norm_max in (None, "", "none", "null") else self._str_to_float(norm_max, 1.0)

        if ModbusTcpClient is None:
            result["details"]["error"] = "未安装 pymodbus，无法执行连接"
            logger.error(result["details"]["error"])
            return result

        # 建立连接
        plc = ModbusTcpClient(self.plc_ip, port=self.plc_port)
        robot = ModbusTcpClient(self.robot_ip, port=self.robot_port)
        try:
            if not plc.connect():
                result["details"]["error"] = "无法连接 PLC"
                logger.error(result["details"]["error"])
                return result
            if not robot.connect():
                plc.close()
                result["details"]["error"] = "无法连接 机器人"
                logger.error(result["details"]["error"])
                return result

            logger.info("✅ PLC 与 机器人连接成功")
            time_mod.sleep(0.2)
            #这里再写 R275 = 1（启动标志）
            if self.safe_write(robot, "机器人", robot.write_register, address=275, value=1):
                logger.info(" 已写入启动标志 R275 = 1")
            else:
                logger.warning(" 写入启动标志 R275 = 1 失败")

            # 伺服使能 (coil 写示例)
            if self.safe_write(plc, "PLC", plc.write_coil, 10, True):
                logger.info("✅ 伺服使能成功 (M10=True)")
            else:
                logger.warning("⚠️ 伺服使能失败")

            # 初始化 CSV 文件
            try:
                with open(self.scan_csv_file, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(["Bottle_No", "Scan_Result", "Time"])
            except Exception as e:
                logger.warning("⚠️ 初始化CSV失败: %s", e)

            bottle_count = 0
            logger.info("🟢 等待机器人触发信号... (R260=1扫码 / R256=1拉曼 / R270=1退出)")

            # 主循环：仅响应事件（每次循环后短暂 sleep）
            while True:
                plc = self.ensure_connected(plc, "PLC", self.plc_ip, self.plc_port) or plc
                robot = self.ensure_connected(robot, "机器人", self.robot_ip, self.robot_port) or robot

                # 检查退出寄存器
                quit_signal = self.safe_read(robot, "机器人", robot.read_holding_registers, 270, 1)
                if quit_signal and getattr(quit_signal, "registers", [None])[0] == 1:
                    logger.info("🟥 检测到 R270=1，准备退出...")
                    result["event"] = "quit"
                    result["success"] = True
                    break

                # 读取关键寄存器（256..260）
                rr = self.safe_read(robot, "机器人", robot.read_holding_registers, 256, 5)
                if not rr or not hasattr(rr, "registers"):
                    time_mod.sleep(0.3)
                    continue

                r256, r257, r258, r259, r260 = (rr.registers + [0, 0, 0, 0, 0])[:5]

                # ---------- 扫码逻辑 ----------
                if r260 == 1:
                    bottle_count += 1
                    logger.info("📸 第 %d 瓶触发扫码 (R260=1)", bottle_count)
                    try:
                        # 调用外部扫码函数（用户实现）
                        from .dmqfengzhuang import scan_once as scan_once_local
                        scan_result = scan_once_local(ip="192.168.1.50", port_in=2001, port_out=2002)
                        if scan_result:
                            logger.info("✅ 扫码成功: %s", scan_result)
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            with open(self.scan_csv_file, "a", newline="", encoding="utf-8") as f:
                                csv.writer(f).writerow([bottle_count, scan_result, timestamp])
                        else:
                            logger.warning("⚠️ 扫码失败或无返回")
                    except Exception as e:
                        logger.exception("❌ 扫码异常: %s", e)

                    # 写 R260->0, R261->1
                    self.safe_write(robot, "机器人", robot.write_register, 260, 0)
                    time_mod.sleep(0.15)
                    self.safe_write(robot, "机器人", robot.write_register, 261, 1)
                    logger.info("➡️ 扫码完成 (R260→0, R261→1)")
                    result["event"] = "scan"
                    result["success"] = True

                # ---------- 拉曼逻辑 ----------
                if r256 == 1:
                    logger.info("⚙️ 检测到 R256=1（放瓶完成）")
                    # PLC 电机右转指令
                    self.safe_write(plc, "PLC", plc.write_register, 1199, 1)
                    self.safe_write(plc, "PLC", plc.write_register, 1200, 1)
                    logger.info("➡️ 电机右转中...")
                    if self.wait_with_quit_check(robot, 3):
                        result["event"] = "quit"
                        break
                    self.safe_write(plc, "PLC", plc.write_register, 1199, 0)
                    logger.info("✅ 电机右转完成")

                    # 调用拉曼测试（尽量捕获异常并记录）
                    logger.info("🧪 开始拉曼测试...")
                    try:
                        # 尝试使用模块导入好的 run_raman_test，否则再动态导入
                        rr_func = run_raman_test
                        if rr_func is None:
                            from raman_module import run_raman_test as rr_func
                        success, file_prefix, df = rr_func(
                            integration_time=integration_time_v,
                            laser_power=laser_power_v,
                            save_csv=save_csv_v,
                            save_plot=save_plot_v,
                            normalize=normalize_v,
                            norm_max=norm_max_v,
                        )
                        if success:
                            logger.info("✅ 拉曼测试完成: %s", file_prefix)
                            result["event"] = "raman"
                            result["success"] = True
                        else:
                            logger.warning("⚠️ 拉曼测试失败")
                    except Exception as e:
                        logger.exception("❌ 拉曼模块异常: %s", e)

                    # 电机左转回位
                    self.safe_write(plc, "PLC", plc.write_register, address=1299, value=1)
                    self.safe_write(plc, "PLC", plc.write_register, address=1300, value=1)
                    logger.info("⬅️ 电机左转中...")
                    if self.wait_with_quit_check(robot, 3):
                        result["event"] = "quit"
                        break
                    self.safe_write(plc, "PLC", plc.write_register, address=1299, value=0)
                    logger.info("✅ 电机左转完成")

                    # 通知机器人拉曼完成 R257=1
                    self.safe_write(robot, "机器人", robot.write_register, address=257, value=1)
                    logger.info("✅ 已写入 R257=1（拉曼完成）")

                    # 延迟后清零 R256
                    logger.info("⏳ 延迟4秒后清零 R256")
                    if self.wait_with_quit_check(robot, 4):
                        result["event"] = "quit"
                        break
                    self.safe_write(robot, "机器人", robot.write_register, address=256, value=0)
                    logger.info("✅ 已清零 R256")

                    # 等待机器人清 R257
                    logger.info("等待 R257 清零中...")
                    while True:
                        rr2 = self.safe_read(robot, "机器人", robot.read_holding_registers, address=257, count=1)
                        if rr2 and getattr(rr2, "registers", [None])[0] == 0:
                            logger.info("✅ 检测到 R257=0，准备下一循环")
                            break
                        if self.wait_with_quit_check(robot, 1):
                            result["event"] = "quit"
                            break
                        time_mod.sleep(0.2)

                time_mod.sleep(0.25)

        finally:
            logger.info("🧹 开始清理...")
            try:
                self.safe_write(plc, "PLC", plc.write_coil, address=10, value=False)
            except Exception:
                pass
            for addr in [256, 257, 260, 261, 270]:
                try:
                    self.safe_write(robot, "机器人", robot.write_register, address=addr, value=0)
                except Exception:
                    pass

            try:
                if plc:
                    plc.close()
            except Exception:
                pass
            try:
                if robot:
                    robot.close()
            except Exception:
                pass
            logger.info("🔚 已关闭所有连接")

        return result
