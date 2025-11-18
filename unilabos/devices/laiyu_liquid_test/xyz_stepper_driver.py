
"""
XYZ 三轴步进电机驱动（统一字符串参数版）
基于 Modbus RTU 协议
Author: Xiuyu Chen (Modified by Assistant)
"""

import serial  # type: ignore
import struct
import time
import logging
from enum import Enum
from dataclasses import dataclass
from typing import Optional, List, Dict

# ========== 日志配置 ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("XYZStepper")


# ========== 层 1：Modbus RTU ==========
class ModbusException(Exception):
    pass


class ModbusRTUTransport:
    """底层串口通信层"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.2):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None

    def open(self):
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.02,
                write_timeout=0.5,
            )
            logger.info(f"[RTU] 串口连接成功: {self.port}")
        except Exception as e:
            raise ModbusException(f"无法打开串口 {self.port}: {e}")

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            logger.info("[RTU] 串口已关闭")

    def send(self, frame: bytes):
        if not self.ser or not self.ser.is_open:
            raise ModbusException("串口未连接")

        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        logger.debug(f"[TX] {frame.hex(' ').upper()}")

    def receive(self, expected_len: int) -> bytes:
        if not self.ser or not self.ser.is_open:
            raise ModbusException("串口未连接")

        start = time.time()
        buf = bytearray()
        while len(buf) < expected_len and (time.time() - start) < self.timeout:
            chunk = self.ser.read(expected_len - len(buf))
            if chunk:
                buf.extend(chunk)
            else:
                time.sleep(0.01)
        return bytes(buf)


# ========== 层 2：Modbus 协议 ==========
class ModbusFunction(Enum):
    READ_HOLDING_REGISTERS = 0x03
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_REGISTERS = 0x10


class ModbusClient:
    """Modbus RTU 客户端"""

    def __init__(self, transport: ModbusRTUTransport):
        self.transport = transport

    @staticmethod
    def calc_crc(data: bytes) -> bytes:
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        return struct.pack("<H", crc)

    def send_request(self, addr: int, func: int, payload: bytes) -> bytes:
        frame = bytes([addr, func]) + payload
        full = frame + self.calc_crc(frame)
        self.transport.send(full)
        time.sleep(0.01)
        resp = self.transport.ser.read(256)
        if not resp:
            raise ModbusException("未收到响应")

        start = resp.find(bytes([addr, func]))
        if start > 0:
            resp = resp[start:]
        if len(resp) < 5:
            raise ModbusException(f"响应长度不足: {resp.hex(' ').upper()}")
        if self.calc_crc(resp[:-2]) != resp[-2:]:
            raise ModbusException("CRC 校验失败")
        return resp

    def read_registers(self, addr: int, start: int, count: int) -> List[int]:
        payload = struct.pack(">HH", start, count)
        resp = self.send_request(addr, ModbusFunction.READ_HOLDING_REGISTERS.value, payload)
        byte_count = resp[2]
        regs = [struct.unpack(">H", resp[3 + i:5 + i])[0] for i in range(0, byte_count, 2)]
        return regs

    def write_single_register(self, addr: int, reg: int, val: int) -> bool:
        payload = struct.pack(">HH", reg, val)
        resp = self.send_request(addr, ModbusFunction.WRITE_SINGLE_REGISTER.value, payload)
        return resp[1] == ModbusFunction.WRITE_SINGLE_REGISTER.value

    def write_multiple_registers(self, addr: int, start: int, values: List[int]) -> bool:
        byte_count = len(values) * 2
        payload = struct.pack(">HHB", start, len(values), byte_count)
        payload += b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
        resp = self.send_request(addr, ModbusFunction.WRITE_MULTIPLE_REGISTERS.value, payload)
        return resp[1] == ModbusFunction.WRITE_MULTIPLE_REGISTERS.value


# ========== 层 3：业务逻辑 ==========
class MotorAxis(Enum):
    X = 1
    Y = 2
    Z = 3


class MotorStatus(Enum):
    STANDBY = 0
    RUNNING = 1
    COLLISION_STOP = 2
    FORWARD_LIMIT_STOP = 3
    REVERSE_LIMIT_STOP = 4


@dataclass
class MotorPosition:
    steps: int
    speed: int
    current: int
    status: MotorStatus


class XYZStepperController:
    """XYZ 三轴步进控制器（字符串接口版）"""

    STEPS_PER_REV = 16384
    LEAD_MM_X, LEAD_MM_Y, LEAD_MM_Z = 80.0, 80.0, 5.0
    STEPS_PER_MM_X = STEPS_PER_REV / LEAD_MM_X
    STEPS_PER_MM_Y = STEPS_PER_REV / LEAD_MM_Y
    STEPS_PER_MM_Z = STEPS_PER_REV / LEAD_MM_Z

    REG_STATUS, REG_POS_HIGH, REG_POS_LOW = 0x00, 0x01, 0x02
    REG_ACTUAL_SPEED, REG_CURRENT, REG_ENABLE = 0x03, 0x05, 0x06
    REG_ZERO_CMD, REG_TARGET_HIGH, REG_TARGET_LOW = 0x0F, 0x10, 0x11
    REG_SPEED, REG_ACCEL, REG_PRECISION, REG_START = 0x13, 0x14, 0x15, 0x16
    REG_COMMAND = 0x60

    def __init__(self, client: Optional[ModbusClient] = None, 
                port="/dev/ttyUSB0", baudrate=115200, 
                origin_path="unilabos/devices/laiyu_liquid_test/work_origin.json"):
        if client is None:
            transport = ModbusRTUTransport(port, baudrate)
            transport.open()
            self.client = ModbusClient(transport)
        else:
            self.client = client

        self.axis_addr = {MotorAxis.X: 1, MotorAxis.Y: 2, MotorAxis.Z: 3}
        self.work_origin_steps = {"x": 0, "y": 0, "z": 0}
        self.is_homed = False
        self._load_work_origin(origin_path)

    # ========== 基础工具 ==========
    @staticmethod
    def s16(v: int) -> int:
        return v - 0x10000 if v & 0x8000 else v

    @staticmethod
    def s32(h: int, l: int) -> int:
        v = (h << 16) | l
        return v - 0x100000000 if v & 0x80000000 else v

    @classmethod
    def mm_to_steps(cls, axis: str, mm: float = 0.0) -> int:
        axis = axis.upper()
        if axis == "X":
            return int(mm * cls.STEPS_PER_MM_X)
        elif axis == "Y":
            return int(mm * cls.STEPS_PER_MM_Y)
        elif axis == "Z":
            return int(mm * cls.STEPS_PER_MM_Z)
        raise ValueError(f"未知轴: {axis}")

    @classmethod
    def steps_to_mm(cls, axis: str, steps: int) -> float:
        axis = axis.upper()
        if axis == "X":
            return steps / cls.STEPS_PER_MM_X
        elif axis == "Y":
            return steps / cls.STEPS_PER_MM_Y
        elif axis == "Z":
            return steps / cls.STEPS_PER_MM_Z
        raise ValueError(f"未知轴: {axis}")

    # ========== 状态与控制 ==========
    def get_status(self, axis: str = "Z") -> list:
        """返回简化数组格式: [steps, speed, current, status_value]"""
        if isinstance(axis, MotorAxis):
            axis_enum = axis
        elif isinstance(axis, str):
            axis_enum = MotorAxis[axis.upper()]
        else:
            raise TypeError("axis 参数必须为 str 或 MotorAxis")

        vals = self.client.read_registers(self.axis_addr[axis_enum], self.REG_STATUS, 6)
        return [
            self.s32(vals[1], vals[2]),
            self.s16(vals[3]),
            vals[4],
            int(MotorStatus(vals[0]).value)
        ]

    def enable(self, axis: str, state: bool) -> bool:
        a = MotorAxis[axis.upper()]
        return self.client.write_single_register(self.axis_addr[a], self.REG_ENABLE, 1 if state else 0)

    def wait_complete(self, axis: str, timeout=30.0) -> bool:
        a = axis.upper()
        start = time.time()
        while time.time() - start < timeout:
            vals = self.get_status(a)
            st = MotorStatus(vals[3])  # 第4个元素是状态值
            if st == MotorStatus.STANDBY:
                return True
            if st in (MotorStatus.COLLISION_STOP, MotorStatus.FORWARD_LIMIT_STOP, MotorStatus.REVERSE_LIMIT_STOP):
                logger.warning(f"{a} 轴异常停止: {st.name}")
                return False
            time.sleep(0.1)
        logger.warning(f"{a} 轴运动超时")
        return False

    # ========== 控制命令 ==========
    def move_to(self, axis: str, steps: int, speed: int = 2000, acc: int = 500, precision: int = 50) -> bool:
        a = MotorAxis[axis.upper()]
        addr = self.axis_addr[a]
        hi, lo = (steps >> 16) & 0xFFFF, steps & 0xFFFF
        values = [hi, lo, speed, acc, precision]
        ok = self.client.write_multiple_registers(addr, self.REG_TARGET_HIGH, values)
        if ok:
            self.client.write_single_register(addr, self.REG_START, 1)
        return ok

    def move_xyz_work(self, x: float = 0.0, y: float = 0.0, z: float = 0.0, speed: int = 100, acc: int = 1500):
        logger.info("🧭 执行安全多轴运动：Z→XY→Z")
        if z is not None:
            safe_z = self._to_machine_steps("Z", 0.0)
            self.move_to("Z", safe_z, speed, acc)
            self.wait_complete("Z")

        if x is not None or y is not None:
            if x is not None:
                self.move_to("X", self._to_machine_steps("X", x), speed, acc)
            if y is not None:
                self.move_to("Y", self._to_machine_steps("Y", y), speed, acc)
            if x is not None:
                self.wait_complete("X")
            if y is not None:
                self.wait_complete("Y")

        if z is not None:
            self.move_to("Z", self._to_machine_steps("Z", z), speed, acc)
            self.wait_complete("Z")
        logger.info("✅ 多轴顺序运动完成")

    # ========== 坐标与零点 ==========
    def _to_machine_steps(self, axis: str, mm: float) -> int:
        base = self.work_origin_steps.get(axis.lower(), 0)
        return base + self.mm_to_steps(axis, mm)

    def define_current_as_zero(self, save_path="work_origin.json"):
        import json
        from datetime import datetime

        origin = {}
        for axis in ["X", "Y", "Z"]:
            vals = self.get_status(axis)
            origin[axis.lower()] = int(vals[0])  # 第1个是步数
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump({"work_origin_steps": origin, "timestamp": datetime.now().isoformat()}, f, indent=2)
        self.work_origin_steps = origin
        self.is_homed = True
        logger.info(f"✅ 零点已定义并保存至 {save_path}")

    def _load_work_origin(self, path: str) -> bool:
        import json, os

        if not os.path.exists(path):
            logger.warning("⚠️ 未找到软零点文件")
            return False
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.work_origin_steps = data.get("work_origin_steps", {"x": 0, "y": 0, "z": 0})
        self.is_homed = True
        logger.info(f"📂 软零点已加载: {self.work_origin_steps}")
        return True

    def return_to_work_origin(self, speed: int = 200, acc: int = 800):
        logger.info("🏁 回工件软零点")
        self.move_to("Z", self._to_machine_steps("Z", 0.0), speed, acc)
        self.wait_complete("Z")
        self.move_to("X", self.work_origin_steps.get("x", 0), speed, acc)
        self.move_to("Y", self.work_origin_steps.get("y", 0), speed, acc)
        self.wait_complete("X")
        self.wait_complete("Y")
        self.move_to("Z", self.work_origin_steps.get("z", 0), speed, acc)
        self.wait_complete("Z")
        logger.info("🎯 回软零点完成 ✅")
