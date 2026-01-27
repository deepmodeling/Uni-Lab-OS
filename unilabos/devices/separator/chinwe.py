# -*- coding: utf-8 -*-
"""
Contains drivers for:
1. SyringePump: Runze Fluid SY-03B (ASCII)
2. EmmMotor: Emm V5.0 Closed-loop Stepper (Modbus-RTU variant)
3. XKCSensor: XKC Non-contact Level Sensor (Modbus-RTU)
"""

import socket
import serial
import time
import threading
import struct
import re
import traceback
import queue
from typing import Optional, Dict, List, Any

try:
    from unilabos.device_comms.universal_driver import UniversalDriver
except ImportError:
    import logging
    class UniversalDriver:
        def __init__(self):
            self.logger = logging.getLogger(self.__class__.__name__)

        def execute_command_from_outer(self, command: str):
            pass

# ==============================================================================
# 1. Transport Layer (通信层)
# ==============================================================================

class TransportManager:
    """
    统一通信管理类。
    自动识别 串口 (Serial) 或 网络 (TCP) 连接。
    """
    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 3.0, logger=None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.logger = logger
        self.lock = threading.RLock() # 线程锁，确保多设备共用一个连接时不冲突

        self.is_tcp = False
        self.serial = None
        self.socket = None

        # 简单判断: 如果包含 ':' (如 192.168.1.1:8899) 或者看起来像 IP，则认为是 TCP
        if ':' in self.port or (self.port.count('.') == 3 and not self.port.startswith('/')):
            self.is_tcp = True
            self._connect_tcp()
        else:
            self._connect_serial()

    def _log(self, msg):
        if self.logger:
             pass
            # self.logger.debug(f"[Transport] {msg}")

    def _connect_tcp(self):
        try:
            if ':' in self.port:
                host, p = self.port.split(':')
                self.tcp_host = host
                self.tcp_port = int(p)
            else:
                self.tcp_host = self.port
                self.tcp_port = 8899 # 默认端口

            # if self.logger: self.logger.info(f"Connecting TCP {self.tcp_host}:{self.tcp_port} ...")
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.tcp_host, self.tcp_port))
        except Exception as e:
            raise ConnectionError(f"TCP connection failed: {e}")

    def _connect_serial(self):
        try:
            # if self.logger: self.logger.info(f"Opening Serial {self.port} (Baud: {self.baudrate}) ...")
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout
            )
        except Exception as e:
            raise ConnectionError(f"Serial open failed: {e}")

    def close(self):
        """关闭连接"""
        if self.is_tcp and self.socket:
            try: self.socket.close()
            except: pass
        elif not self.is_tcp and self.serial and self.serial.is_open:
            self.serial.close()

    def clear_buffer(self):
        """清空缓冲区 (Thread-safe)"""
        with self.lock:
            if self.is_tcp:
                self.socket.setblocking(False)
                try:
                    while True:
                        if not self.socket.recv(1024): break
                except: pass
                finally: self.socket.settimeout(self.timeout)
            else:
                self.serial.reset_input_buffer()

    def write(self, data: bytes):
        """发送原始字节"""
        with self.lock:
            if self.is_tcp:
                self.socket.sendall(data)
            else:
                self.serial.write(data)

    def read(self, size: int) -> bytes:
        """读取指定长度字节"""
        if self.is_tcp:
            data = b''
            start = time.time()
            while len(data) < size:
                if time.time() - start > self.timeout: break
                try:
                    chunk = self.socket.recv(size - len(data))
                    if not chunk: break
                    data += chunk
                except socket.timeout: break
            return data
        else:
            return self.serial.read(size)

    def send_ascii_command(self, command: str) -> str:
        """
        发送 ASCII 字符串命令 (如注射泵指令)，读取直到 '\r'。
        """
        with self.lock:
            data = command.encode('ascii') if isinstance(command, str) else command
            self.clear_buffer()
            self.write(data)

            # Read until \r
            if self.is_tcp:
                resp = b''
                start = time.time()
                while True:
                    if time.time() - start > self.timeout: break
                    try:
                        char = self.socket.recv(1)
                        if not char: break
                        resp += char
                        if char == b'\r': break
                    except: break
                return resp.decode('ascii', errors='ignore').strip()
            else:
                return self.serial.read_until(b'\r').decode('ascii', errors='ignore').strip()

# ==============================================================================
# 2. Syringe Pump Driver (注射泵)
# ==============================================================================

class SyringePump:
    """SY-03B 注射泵驱动 (ASCII协议)"""

    CMD_INITIALIZE = "Z{speed},{drain_port},{output_port}R"
    CMD_SWITCH_VALVE = "I{port}R"
    CMD_ASPIRATE = "P{vol}R"
    CMD_DISPENSE = "D{vol}R"
    CMD_DISPENSE_ALL = "A0R"
    CMD_STOP = "TR"
    CMD_QUERY_STATUS = "Q"
    CMD_QUERY_PLUNGER = "?0"

    def __init__(self, device_id: int, transport: TransportManager):
        if not 1 <= device_id <= 15:
            pass # Allow all IDs for now
        self.id = str(device_id)
        self.transport = transport

    def _send(self, template: str, **kwargs) -> str:
        cmd = f"/{self.id}" + template.format(**kwargs) + "\r"
        return self.transport.send_ascii_command(cmd)

    def is_busy(self) -> bool:
        """查询繁忙状态"""
        resp = self._send(self.CMD_QUERY_STATUS)
        # 响应如 /0` (Ready, 0x60) 或 /0@ (Busy, 0x40)
        if len(resp) >= 3:
            status_byte = ord(resp[2])
            # Bit 5: 1=Ready, 0=Busy
            return (status_byte & 0x20) == 0
        return False

    def wait_until_idle(self, timeout=30):
        """阻塞等待直到空闲"""
        start = time.time()
        while time.time() - start < timeout:
            if not self.is_busy(): return
            time.sleep(0.5)
        # raise TimeoutError(f"Pump {self.id} wait idle timeout")
        pass

    def initialize(self, drain_port=0, output_port=0, speed=10):
        """初始化"""
        self._send(self.CMD_INITIALIZE, speed=speed, drain_port=drain_port, output_port=output_port)

    def switch_valve(self, port: int):
        """切换阀门 (1-8)"""
        self._send(self.CMD_SWITCH_VALVE, port=port)

    def aspirate(self, steps: int):
        """吸液 (相对步数)"""
        self._send(self.CMD_ASPIRATE, vol=steps)

    def dispense(self, steps: int):
        """排液 (相对步数)"""
        self._send(self.CMD_DISPENSE, vol=steps)

    def stop(self):
        """停止"""
        self._send(self.CMD_STOP)

    def get_position(self) -> int:
        """获取柱塞位置 (步数)"""
        resp = self._send(self.CMD_QUERY_PLUNGER)
        m = re.search(r'\d+', resp)
        return int(m.group()) if m else -1

# ==============================================================================
# 3. Stepper Motor Driver (步进电机)
# ==============================================================================

class EmmMotor:
    """Emm V5.0 闭环步进电机驱动"""

    def __init__(self, device_id: int, transport: TransportManager):
        self.id = device_id
        self.transport = transport

    def _send(self, func_code: int, payload: list) -> bytes:
        with self.transport.lock:
            self.transport.clear_buffer()
            # 格式: [ID] [Func] [Data...] [Check=0x6B]
            body = [self.id, func_code] + payload
            body.append(0x6B) # Checksum
            self.transport.write(bytes(body))

            # 根据指令不同，读取不同长度响应
            read_len = 10 if func_code in [0x31, 0x32, 0x35, 0x24, 0x27] else 4
            return self.transport.read(read_len)

    def enable(self, on=True):
        """使能 (True=锁轴, False=松轴)"""
        state = 1 if on else 0
        self._send(0xF3, [0xAB, state, 0])

    def run_speed(self, speed_rpm: int, direction=0, acc=10):
        """速度模式运行"""
        sp = struct.pack('>H', int(speed_rpm))
        self._send(0xF6, [direction, sp[0], sp[1], acc, 0])

    def run_position(self, pulses: int, speed_rpm: int, direction=0, acc=10, absolute=False):
        """位置模式运行"""
        sp = struct.pack('>H', int(speed_rpm))
        pl = struct.pack('>I', int(pulses))
        is_abs = 1 if absolute else 0
        self._send(0xFD, [direction, sp[0], sp[1], acc, pl[0], pl[1], pl[2], pl[3], is_abs, 0])

    def stop(self):
        """停止"""
        self._send(0xFE, [0x98, 0])

    def set_zero(self):
        """清零位置"""
        self._send(0x0A, [])

    def get_position(self) -> int:
        """获取当前脉冲位置"""
        resp = self._send(0x32, [])
        if len(resp) >= 8:
            sign = resp[2]
            val = struct.unpack('>I', resp[3:7])[0]
            return -val if sign == 1 else val
        return 0

# ==============================================================================
# 4. Liquid Sensor Driver (液位传感器)
# ==============================================================================

class XKCSensor:
    """XKC RS485 液位传感器 (Modbus RTU)"""

    def __init__(self, device_id: int, transport: TransportManager, threshold: int = 300):
        self.id = device_id
        self.transport = transport
        self.threshold = threshold

    def _crc(self, data: bytes) -> bytes:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001: crc = (crc >> 1) ^ 0xA001
                else: crc >>= 1
        return struct.pack('<H', crc)

    def read_level(self) -> Optional[Dict[str, Any]]:
        """
        读取液位。
        返回: {'level': bool, 'rssi': int}
        """
        with self.transport.lock:
            self.transport.clear_buffer()
            # Modbus Read Registers: 01 03 00 01 00 02 CRC
            payload = struct.pack('>HH', 0x0001, 0x0002)
            msg = struct.pack('BB', self.id, 0x03) + payload
            msg += self._crc(msg)
            self.transport.write(msg)

            # Read header
            h = self.transport.read(3) # Addr, Func, Len
            if len(h) < 3: return None
            length = h[2]

            # Read body + CRC
            body = self.transport.read(length + 2)
            if len(body) < length + 2:
                # Firmware bug fix specific to some modules
                if len(body) == 4 and length == 4:
                    pass
                else:
                    return None

            data = body[:-2]
            if len(data) == 2:
                rssi = data[1]
            elif len(data) >= 4:
                rssi = (data[2] << 8) | data[3]
            else:
                return None

            return {
                'level': rssi > self.threshold,
                'rssi': rssi
            }

# ==============================================================================
# 5. Main Device Class (ChinweDevice)
# ==============================================================================

class ChinweDevice(UniversalDriver):
    """
    ChinWe 工作站主驱动
    继承自 UniversalDriver，管理所有子设备（泵、电机、传感器）
    """

    def __init__(self, port: str = "192.168.1.200:8899", baudrate: int = 9600,
                 pump_ids: List[int] = None, motor_ids: List[int] = None,
                 sensor_id: int = 6, sensor_threshold: int = 300,
                 timeout: float = 10.0):
        """
        初始化 ChinWe 工作站
        :param port: 串口号 或 IP:Port
        :param baudrate: 串口波特率
        :param pump_ids: 注射泵 ID列表 (默认 [1, 2, 3])
        :param motor_ids: 步进电机 ID列表 (默认 [4, 5])
        :param sensor_id: 液位传感器 ID (默认 6)
        :param sensor_threshold: 传感器液位判定阈值
        :param timeout: 通信超时时间 (默认 10秒)
        """
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.mgr = None
        self._is_connected = False

        # 默认配置
        if pump_ids is None: pump_ids = [1, 2, 3]
        if motor_ids is None: motor_ids = [4, 5]

        # 配置信息
        self.pump_ids = pump_ids
        self.motor_ids = motor_ids
        self.sensor_id = sensor_id
        self.sensor_threshold = sensor_threshold

        # 子设备实例容器
        self.pumps: Dict[int, SyringePump] = {}
        self.motors: Dict[int, EmmMotor] = {}
        self.sensor: Optional[XKCSensor] = None

        # 轮询线程控制
        self._stop_event = threading.Event()
        self._poll_thread = None

        # 实时状态缓存
        self.status_cache = {
             "sensor_rssi": 0,
             "sensor_level": False,
             "connected": False
        }

        # 自动连接
        if self.port:
            self.connect()

    def connect(self) -> bool:
        if self._is_connected: return True
        try:
            self.logger.info(f"Connecting to {self.port} (timeout={self.timeout})...")
            self.mgr = TransportManager(self.port, baudrate=self.baudrate, timeout=self.timeout, logger=self.logger)

            # 初始化所有泵
            for pid in self.pump_ids:
                self.pumps[pid] = SyringePump(pid, self.mgr)

            # 初始化所有电机
            for mid in self.motor_ids:
                self.motors[mid] = EmmMotor(mid, self.mgr)

            # 初始化传感器
            self.sensor = XKCSensor(self.sensor_id, self.mgr, self.sensor_threshold)

            self._is_connected = True
            self.status_cache["connected"] = True

            # 启动轮询线程
            self._start_polling()
            return True
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            self._is_connected = False
            self.status_cache["connected"] = False
            return False

    def disconnect(self):
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)

        if self.mgr:
            self.mgr.close()

        self._is_connected = False
        self.status_cache["connected"] = False
        self.logger.info("Disconnected.")

    def _start_polling(self):
        """启动传感器轮询线程"""
        if self._poll_thread and self._poll_thread.is_alive():
            return

        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._polling_loop, daemon=True, name="ChinwePoll")
        self._poll_thread.start()

    def _polling_loop(self):
        """轮询主循环"""
        self.logger.info("Sensor polling started.")
        error_count = 0
        while not self._stop_event.is_set():
            if not self._is_connected or not self.sensor:
                time.sleep(1)
                continue

            try:
                # 获取传感器数据
                data = self.sensor.read_level()
                if data:
                    self.status_cache["sensor_rssi"] = data['rssi']
                    self.status_cache["sensor_level"] = data['level']
                    error_count = 0
                else:
                    error_count += 1

                # 降低轮询频率防止总线拥塞
                time.sleep(0.2)

            except Exception as e:
                error_count += 1
                if error_count > 10: # 连续错误记录日志
                    # self.logger.error(f"Polling error: {e}")
                    error_count = 0
                time.sleep(1)

    # --- 对外暴露属性 (Properties) ---

    @property
    def sensor_level(self) -> bool:
        return self.status_cache["sensor_level"]

    @property
    def sensor_rssi(self) -> int:
        return self.status_cache["sensor_rssi"]

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    # --- 对外功能指令 (Actions) ---

    def pump_initialize(self, pump_id: int, drain_port=0, output_port=0, speed=10):
        """指定泵初始化"""
        pump_id = int(pump_id)
        if pump_id in self.pumps:
            self.pumps[pump_id].initialize(drain_port, output_port, speed)
            self.pumps[pump_id].wait_until_idle()
            return True
        return False

    def pump_aspirate(self, pump_id: int, volume: int, valve_port: int):
        """
        泵吸液 (阻塞)
        :param valve_port: 阀门端口 (1-8)
        """
        pump_id = int(pump_id)
        valve_port = int(valve_port)
        if pump_id in self.pumps:
            pump = self.pumps[pump_id]
            # 1. 切换阀门
            pump.switch_valve(valve_port)
            pump.wait_until_idle()
            # 2. 吸液
            pump.aspirate(volume)
            pump.wait_until_idle()
            return True
        return False

    def pump_dispense(self, pump_id: int, volume: int, valve_port: int):
        """
        泵排液 (阻塞)
        :param valve_port: 阀门端口 (1-8)
        """
        pump_id = int(pump_id)
        valve_port = int(valve_port)
        if pump_id in self.pumps:
            pump = self.pumps[pump_id]
            # 1. 切换阀门
            pump.switch_valve(valve_port)
            pump.wait_until_idle()
            # 2. 排液
            pump.dispense(volume)
            pump.wait_until_idle()
            return True
        return False

    def pump_valve(self, pump_id: int, port: int):
        """泵切换阀门 (阻塞)"""
        pump_id = int(pump_id)
        port = int(port)
        if pump_id in self.pumps:
            pump = self.pumps[pump_id]
            pump.switch_valve(port)
            pump.wait_until_idle()
            return True
        return False

    def motor_run_continuous(self, motor_id: int, speed: int, direction: str = "顺时针"):
        """
        电机一直旋转 (速度模式)
        :param direction: "顺时针" or "逆时针"
        """
        motor_id = int(motor_id)
        if motor_id not in self.motors: return False

        dir_val = 0 if direction == "顺时针" else 1
        self.motors[motor_id].run_speed(speed, dir_val)
        return True

    def motor_rotate_quarter(self, motor_id: int, speed: int = 60, direction: str = "顺时针"):
        """
        电机旋转1/4圈 (阻塞)
        假设电机设置为 3200 脉冲/圈，1/4圈 = 800脉冲
        """
        motor_id = int(motor_id)
        if motor_id not in self.motors: return False

        pulses = 800
        dir_val = 0 if direction == "顺时针" else 1

        self.motors[motor_id].run_position(pulses, speed, dir_val, absolute=False)

        # 预估时间阻塞 (单位: 分钟 -> 秒)
        # Time(s) = revs / (RPM/60). revs = 0.25. time = 15 / RPM.
        estimated_time = 15.0 / max(1, speed)
        time.sleep(estimated_time + 0.5)

        return True

    def motor_stop(self, motor_id: int):
        """电机停止"""
        motor_id = int(motor_id)
        if motor_id in self.motors:
            self.motors[motor_id].stop()
            return True
        return False

    def wait_sensor_level(self, target_state: str = "有液", timeout: int = 30) -> bool:
        """
        等待传感器达到指定电平
        :param target_state: "有液" or "无液"
        """
        target_bool = True if target_state == "有液" else False

        self.logger.info(f"Wait sensor: {target_state} ({target_bool}), timeout: {timeout}")
        start = time.time()
        while time.time() - start < timeout:
            if self.sensor_level == target_bool:
                return True
            time.sleep(0.1)
        self.logger.warning("Wait sensor level timeout")
        return False

    def wait_time(self, duration: int) -> bool:
        """
        等待指定时间 (秒)
        :param duration: 秒
        """
        self.logger.info(f"Waiting for {duration} seconds...")
        time.sleep(duration)
        return True

    def separation_step(self, motor_id: int = 5, speed: int = 60, pulses: int = 700,
                       max_cycles: int = 0, timeout: int = 300) -> bool:
        """
        分液步骤 - 液位传感器与电机联动
        当液位传感器检测到"有液"时,电机顺时针旋转指定脉冲数
        当液位传感器检测到"无液"时,电机逆时针旋转指定脉冲数

        :param motor_id: 电机ID (必须在初始化时配置的motor_ids中)
        :param speed: 电机转速 (RPM)
        :param pulses: 每次旋转的脉冲数 (默认700约为1/4圈,假设3200脉冲/圈)
        :param max_cycles: 最大执行循环次数 (0=无限制,默认0)
        :param timeout: 整体超时时间 (秒)
        :return: 成功返回True,超时或失败返回False
        """
        motor_id = int(motor_id)
        speed = int(speed)
        pulses = int(pulses)
        max_cycles = int(max_cycles)
        timeout = int(timeout)

        # 检查电机是否存在
        if motor_id not in self.motors:
            self.logger.error(f"Motor {motor_id} not found in configured motors: {list(self.motors.keys())}")
            return False

        # 检查传感器是否可用
        if not self.sensor:
            self.logger.error("Sensor not initialized")
            return False

        motor = self.motors[motor_id]

        # 停止轮询线程，避免与 separation_step 同时读取传感器造成串口冲突
        self.logger.info("Stopping polling thread for separation_step...")
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=2.0)

        # 使能电机
        self.logger.info(f"Enabling motor {motor_id}...")
        motor.enable(True)
        time.sleep(0.2)

        self.logger.info(f"Starting separation step: motor_id={motor_id}, speed={speed} RPM, "
                        f"pulses={pulses}, max_cycles={max_cycles}, timeout={timeout}s")

        # 记录上一次的液位状态
        last_level = None
        cycle_count = 0
        start_time = time.time()
        error_count = 0

        try:
            while True:
                # 检查超时
                if time.time() - start_time > timeout:
                    self.logger.warning(f"Separation step timeout after {timeout} seconds")
                    return False

                # 检查循环次数限制
                if max_cycles > 0 and cycle_count >= max_cycles:
                    self.logger.info(f"Separation step completed: reached max_cycles={max_cycles}")
                    return True

                # 读取传感器数据
                data = self.sensor.read_level()

                if data is None:
                    error_count += 1
                    if error_count > 5:
                        self.logger.warning("Sensor read failed multiple times, retrying...")
                        error_count = 0
                    time.sleep(0.5)
                    continue

                error_count = 0
                current_level = data['level']
                rssi = data['rssi']

                # 检测状态变化 (包括首次检测)
                if current_level != last_level:
                    cycle_count += 1

                    if current_level:
                        # 有液 -> 电机顺时针旋转
                        self.logger.info(f"[Cycle {cycle_count}] Liquid detected (RSSI={rssi}), "
                                       f"rotating motor {motor_id} clockwise {pulses} pulses")
                        motor.run_position(pulses=pulses, speed_rpm=speed, direction=0, absolute=False)

                        # 等待电机完成 (预估时间)
                        estimated_time = 15.0 / max(1, speed)
                        time.sleep(estimated_time + 0.5)

                    else:
                        # 无液 -> 电机逆时针旋转
                        self.logger.info(f"[Cycle {cycle_count}] No liquid detected (RSSI={rssi}), "
                                       f"rotating motor {motor_id} counter-clockwise {pulses} pulses")
                        motor.run_position(pulses=pulses, speed_rpm=speed, direction=1, absolute=False)

                        # 等待电机完成 (预估时间)
                        estimated_time = 15.0 / max(1, speed)
                        time.sleep(estimated_time + 0.5)

                # 更新状态
                last_level = current_level

                # 轮询间隔
                time.sleep(0.1)
        finally:
            # 恢复轮询线程
            self.logger.info("Restarting polling thread...")
            self._start_polling()

    def execute_command_from_outer(self, command_dict: Dict[str, Any]) -> bool:
        """支持标准 JSON 指令调用"""
        return super().execute_command_from_outer(command_dict)

if __name__ == "__main__":
    # Test
    logging.basicConfig(level=logging.INFO)
    dev = ChinweDevice(port="192.168.31.201:8899")
    try:
        if dev.is_connected:
            print(f"Status: Level={dev.sensor_level}, RSSI={dev.sensor_rssi}")

            # Test pump 1
            # dev.pump_valve(1, 1)
            # dev.pump_move(1, 1000, "aspirate")

            # Test motor 4
             # dev.motor_run(4, 60, 0, 2)

            for _ in range(5):
                print(f"Level={dev.sensor_level}, RSSI={dev.sensor_rssi}")
                time.sleep(1)
    finally:
        dev.disconnect()
