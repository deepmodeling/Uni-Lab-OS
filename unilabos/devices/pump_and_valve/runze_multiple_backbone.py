"""
Runze Syringe Pump Controller (SY-03B-T08)

本模块用于控制润泽注射泵 SY-03B-T08 型号的多泵系统。
支持通过串口同时控制多个具有不同地址的泵。
泵每次连接前要先进行初始化。

基础用法:
    # 创建控制器实例
    pump_controller = RunzeMultiplePump("COM3")  # 或 "/dev/ttyUSB0" (Linux)

    # 初始化特定地址的泵
    pump_controller.initialize("1")

    # 设置阀门位置
    pump_controller.set_valve_position("1", 1)  # 设置到位置1

    # 移动到绝对位置
    pump_controller.set_position("1", 10.0)  # 移动到10ml位置

    # 推拉柱塞操作
    pump_controller.pull_plunger("1", 5.0)  # 吸取5ml
    pump_controller.push_plunger("1", 5.0)  # 推出5ml

    # 关闭连接
    pump_controller.close()

支持的泵地址: 1-8 (字符串格式，如 "1", "2", "3" 等)
默认最大容量: 25.0 ml
通信协议: RS485, 9600波特率
"""

from threading import Lock, Event
import time
from dataclasses import dataclass
from enum import Enum
from typing import Union, Optional, List, Dict

import serial.tools.list_ports
from serial import Serial
from serial.serialutil import SerialException


class RunzeSyringePumpMode(Enum):
    Normal = 0
    AccuratePos = 1
    AccuratePosVel = 2


pulse_freq_grades = {
    6000: "0",
    5600: "1",
    5000: "2",
    4400: "3",
    3800: "4",
    3200: "5",
    2600: "6",
    2200: "7",
    2000: "8",
    1800: "9",
    1600: "10",
    1400: "11",
    1200: "12",
    1000: "13",
    800: "14",
    600: "15",
    400: "16",
    200: "17",
    190: "18",
    180: "19",
    170: "20",
    160: "21",
    150: "22",
    140: "23",
    130: "24",
    120: "25",
    110: "26",
    100: "27",
    90: "28",
    80: "29",
    70: "30",
    60: "31",
    50: "32",
    40: "33",
    30: "34",
    20: "35",
    18: "36",
    16: "37",
    14: "38",
    12: "39",
    10: "40",
}


class RunzeSyringePumpConnectionError(Exception):
    pass


@dataclass
class PumpConfig:
    address: str
    max_volume: float = 25.0
    mode: RunzeSyringePumpMode = RunzeSyringePumpMode.Normal


class RunzeMultiplePump:
    """
    Multi-address Runze Syringe Pump Controller

    Supports controlling multiple pumps on the same serial port with different addresses.
    """

    def __init__(self, port: str):
        """
        Initialize multiple pump controller

        Args:
            port (str): Serial port path
        """
        self.port = port

        # Default pump parameters
        self.max_volume = 25.0
        self.total_steps = 6000
        self.total_steps_vel = 6000

        # Connection management
        try:
            self.hardware_interface = Serial(baudrate=9600, port=port, timeout=1.0)
            print(f"✓ 成功连接到串口: {port}")
        except (OSError, SerialException) as e:
            print(f"✗ 串口连接失败: {e}")
            raise RunzeSyringePumpConnectionError(f"无法连接到串口 {port}: {e}") from e

        # Thread safety
        self._query_lock = Lock()
        self._run_lock = Lock()
        self._closing = False

        # Pump status tracking
        self._pump_status: Dict[str, str] = {}  # address -> status

    def _adjust_total_steps(self, mode: RunzeSyringePumpMode):
        total_steps = 6000 if mode == RunzeSyringePumpMode.Normal else 48000
        total_steps_vel = 48000 if mode == RunzeSyringePumpMode.AccuratePosVel else 6000
        return total_steps, total_steps_vel

    def _receive(self, data: bytes) -> str:
        """
        Keep this method as original. Always use chr to decode, avoid "/0"
        """
        if not data:
            return ""
        # **Do not use decode method
        ascii_string = "".join(chr(byte) for byte in data)
        return ascii_string

    def send_command(self, full_command: str) -> str:
        """Send command to hardware and get response"""
        full_command_data = bytearray(full_command, "ascii")
        self.hardware_interface.write(full_command_data)
        time.sleep(0.05)
        response = self.hardware_interface.read_until(b"\n")  # \n should direct use, not \\n
        output = self._receive(response)
        return output

    def _query(self, address: str, command: str) -> str:
        """
        Send query command to specific pump

        Args:
            address (str): Pump address (e.g., "1", "2", "3")
            command (str): Command to send

        Returns:
            str: Response from pump
        """
        with self._query_lock:
            if self._closing:
                raise RunzeSyringePumpConnectionError("Connection is closing")

            run = "R" if "?" not in command else ""
            full_command = f"/{address}{command}{run}\r\n"  # \r\n should direct use, not \\r\\n

            output = self.send_command(full_command)[3:-3]
        return output

    def _run(self, address: str, command: str) -> str:
        """
        Run command and wait for completion

        Args:
            address (str): Pump address
            command (str): Command to execute

        Returns:
            str: Command response
        """
        with self._run_lock:
            try:
                print(f"[泵 {address}] 执行命令: {command}")
                response = self._query(address, command)

                # Wait for operation completion
                while True:
                    time.sleep(0.5)
                    status = self.get_status(address)
                    if status == "Idle":
                        break

            except Exception as e:
                print(f"[泵 {address}] 命令执行错误: {e}")
                response = ""

        return response

    def _standardize_status(self, status_raw: str) -> str:
        """Convert raw status to standard format"""
        return "Idle" if status_raw == "`" else "Busy"

    # === Core Operations ===

    def initialize(self, address: str) -> str:
        """Initialize specific pump"""
        print(f"[泵 {address}] 正在初始化...")
        response = self._run(address, "Z")
        print(f"[泵 {address}] 初始化完成")
        return response

    # === Status Queries ===

    def get_status(self, address: str) -> str:
        """Get pump status"""
        try:
            status_raw = self._query(address, "Q")
            status = self._standardize_status(status_raw)
            self._pump_status[address] = status
            return status
        except Exception:
            return "Error"

    # === Velocity Control ===

    def set_max_velocity(self, address: str, velocity: float, max_volume: float = None) -> str:
        """Set maximum velocity for pump"""
        if max_volume is None:
            max_volume = self.max_volume

        pulse_freq = int(velocity / max_volume * self.total_steps_vel)
        pulse_freq = min(6000, pulse_freq)
        return self._run(address, f"V{pulse_freq}")

    def get_max_velocity(self, address: str, max_volume: float = None) -> float:
        """Get maximum velocity of pump"""
        if max_volume is None:
            max_volume = self.max_volume

        response = self._query(address, "?2")
        status_raw, pulse_freq = response[0], int(response[1:])
        velocity = pulse_freq / self.total_steps_vel * max_volume
        return velocity

    def set_velocity_grade(self, address: str, velocity: Union[int, str]) -> str:
        """Set velocity grade"""
        return self._run(address, f"S{velocity}")

    # === Position Control ===

    def get_position(self, address: str, max_volume: float = None) -> float:
        """Get current plunger position in ml"""
        if max_volume is None:
            max_volume = self.max_volume

        response = self._query(address, "?0")
        status_raw, pos_step = response[0], int(response[1:])
        position = pos_step / self.total_steps * max_volume
        return position

    def set_position(self, address: str, position: float, max_velocity: float = None, max_volume: float = None) -> str:
        """
        Move to absolute volume position

        Args:
            address (str): Pump address
            position (float): Target position in ml
            max_velocity (float): Maximum velocity in ml/s
            max_volume (float): Maximum syringe volume in ml
        """
        if max_volume is None:
            max_volume = self.max_volume

        velocity_cmd = ""
        if max_velocity is not None:
            pulse_freq = int(max_velocity / max_volume * self.total_steps_vel)
            pulse_freq = min(6000, pulse_freq)
            velocity_cmd = f"V{pulse_freq}"

        pos_step = int(position / max_volume * self.total_steps)
        return self._run(address, f"{velocity_cmd}A{pos_step}")

    def pull_plunger(self, address: str, volume: float, max_volume: float = None) -> str:
        """Pull plunger by specified volume"""
        if max_volume is None:
            max_volume = self.max_volume

        pos_step = int(volume / max_volume * self.total_steps)
        return self._run(address, f"P{pos_step}")

    def push_plunger(self, address: str, volume: float, max_volume: float = None) -> str:
        """Push plunger by specified volume"""
        if max_volume is None:
            max_volume = self.max_volume

        pos_step = int(volume / max_volume * self.total_steps)
        return self._run(address, f"D{pos_step}")

    # === Valve Control ===

    def set_valve_position(self, address: str, position: Union[int, str, float]) -> str:
        """Set valve position"""
        if isinstance(position, float):
            position = round(position / 120)
        command = f"I{position}" if isinstance(position, int) or ord(str(position)) <= 57 else str(position).upper()
        return self._run(address, command)

    def get_valve_position(self, address: str) -> str:
        """Get current valve position"""
        response = self._query(address, "?6")
        status_raw, pos_valve = response[0], response[1].upper()
        return pos_valve

    # === Utility Functions ===

    def stop_operation(self, address: str) -> str:
        """Stop current operation"""
        return self._run(address, "T")

    def close(self):
        """Close connection"""
        if self._closing:
            raise RunzeSyringePumpConnectionError("Already closing")

        self._closing = True
        self.hardware_interface.close()
        print("✓ 串口连接已关闭")


if __name__ == "__main__":
    """
    示例：初始化3个泵（地址1、2、3），然后断开连接
    """
    try:
        # 请根据实际串口修改端口号
        # Windows: "COM3", "COM4", 等
        # Linux/Mac: "/dev/ttyUSB0", "/dev/ttyACM0", 等
        port = "/dev/cn."  # 修改为实际使用的串口

        print("正在创建泵控制器...")
        pump_controller = RunzeMultiplePump(port)

        # 初始化3个泵 (地址: 1, 2, 3)
        pump_addresses = ["1", "2", "3"]

        for address in pump_addresses:
            try:
                print(f"\n正在初始化泵 {address}...")
                pump_controller.initialize(address)

                # 检查泵状态
                status = pump_controller.get_status(address)
                print(f"泵 {address} 状态: {status}")
            except Exception as e:
                print(f"泵 {address} 初始化失败: {e}")

        print("\n所有泵初始化完成！")

        # 断开连接
        print("\n正在断开连接...")
        pump_controller.close()
        print("程序结束")

    except RunzeSyringePumpConnectionError as e:
        print(f"连接错误: {e}")
        print("请检查:")
        print("1. 串口是否正确")
        print("2. 设备是否已连接")
        print("3. 串口是否被其他程序占用")

    except Exception as e:
        print(f"未知错误: {e}")
