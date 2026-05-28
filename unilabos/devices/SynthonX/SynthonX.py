"""
XYZ平台和移液枪的控制脚本
所有设备共用一个 RS485/串口。
XYZ 步进驱动器的 RS485 地址：X=1，Y=2，Z=3。
SOPA 移液器的 RS485 地址：4。
默认串口：COM3，波特率：115200。
忽略CRC报错选项：在XYZ运动过程中启用，以防止偶发串扰导致的CRC校验失败中断运动。
可交互式运行
SynthonX 团队
"""

import sys
import time
import json
import threading
import logging
from dataclasses import dataclass, asdict
from enum import Enum, IntEnum
from typing import Optional, Dict, List

try:
    import serial
except Exception as e:
    raise RuntimeError("Please install pyserial: pip install pyserial") from e


# ------------------------------- Logging -------------------------------
logger = logging.getLogger("unified_xyz_yyq")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


# =========================== Shared RS485 Bus ==========================
class SharedRS485Bus:
    """One serial port for everything + a global lock."""

    def __init__(self, port: str = "COM3", baudrate: int = 115200, timeout: float = 0.2):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial = None
        self.lock = threading.Lock()

    def open(self):
        if self.serial and self.serial.is_open:
            return True
        self.serial = serial.Serial(
            port=self.port, baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=self.timeout
        )
        logger.info(f"Opened RS485 bus on {self.port}")
        return True

    def close(self):
        if self.serial and self.serial.is_open:
            self.serial.close()
            logger.info("Closed RS485 bus")

    def reset_input(self):
        if self.serial:
            self.serial.reset_input_buffer()

    def write(self, data: bytes):
        self.serial.write(data)

    def read(self, n: int = 256) -> bytes:
        return self.serial.read(n)

    def read_exact(self, n: int, overall_timeout: float = 0.3) -> bytes:
        """Read exactly n bytes within overall_timeout; return b'' if timeout."""
        if n <= 0:
            return b""
        buf = b""
        deadline = time.time() + overall_timeout
        while len(buf) < n:
            if time.time() > deadline:
                break
            need = n - len(buf)
            chunk = self.read(need)
            if chunk:
                buf += chunk
            else:
                time.sleep(0.001)
        return buf


# ======================= XYZ: Low-level Modbus ========================
class MotorAxis(Enum):
    X = 1
    Y = 2
    Z = 3


class MotorStatus(Enum):
    STANDBY = 0x0000
    RUNNING = 0x0001
    COLLISION_STOP = 0x0002
    FORWARD_LIMIT_STOP = 0x0003
    REVERSE_LIMIT_STOP = 0x0004


class ModbusException(Exception):
    pass


@dataclass
class MotorPosition:
    steps: int
    speed: int
    current: int
    status: MotorStatus


class XYZModbus:
    """Minimal Modbus RTU helper bound to the shared bus.

    简化容错：ignore_crc_error=True 时，CRC 重试耗尽后直接忽略继续，不再统计次数。
    """

    REG_STATUS = 0x00
    REG_POSITION_HIGH = 0x01
    REG_POSITION_LOW = 0x02
    REG_ACTUAL_SPEED = 0x03
    REG_EMERGENCY_STOP = 0x04
    REG_CURRENT = 0x05
    REG_ENABLE = 0x06

    # position mode
    REG_TARGET_POSITION_HIGH = 0x10
    REG_TARGET_POSITION_LOW = 0x11
    REG_POSITION_SPEED = 0x13
    REG_POSITION_ACCELERATION = 0x14
    REG_POSITION_PRECISION = 0x15

    # speed mode
    REG_SPEED_MODE_SPEED = 0x61
    REG_SPEED_MODE_ACCELERATION = 0x62

    def __init__(self, bus: SharedRS485Bus, ignore_crc_error: bool = False):
        self.bus = bus
        self.ignore_crc_error = ignore_crc_error

    def set_ignore_crc(self, flag: bool):
        self.ignore_crc_error = bool(flag)

    @staticmethod
    def _crc16(data: bytes) -> bytes:
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 0x0001:
                    crc >>= 1
                    crc ^= 0xA001
                else:
                    crc >>= 1
        return crc.to_bytes(2, "little")

    def _xfer(self, slave: int, payload: bytes, retries: int = 3) -> bytes:
        req = bytes([slave]) + payload
        frame = req + self._crc16(req)
        fn_req = payload[0]

    # 不做统计，只在最终失败时可选择忽略返回
        for attempt in range(1, retries + 1):
            with self.bus.lock:
                if not self.bus.serial or not self.bus.serial.is_open:
                    raise ModbusException("Bus not open")

                self.bus.reset_input()
                self.bus.write(frame)

            time.sleep(0.010)

            try:
                base = 0.30 + 0.15*(attempt-1)
                header = self.bus.read_exact(2, overall_timeout=base)
                if len(header) < 2:
                    raise ModbusException("No response")

                addr, fn = header[0], header[1]
                if addr != slave:
                    # 把这一帧当成串扰/回波，丢弃后继续本次尝试
                    time.sleep(0.005)
                    continue

                if (fn & 0x80) != 0:
                    rest = self.bus.read_exact(3, overall_timeout=base)
                    resp = header + rest
                    if len(rest) < 3:
                        raise ModbusException("Short exception response")
                    if resp[-2:] != self._crc16(resp[:-2]):
                        logger.warning(f"CRC mismatch (exception response) attempt {attempt}/{retries} slave={slave} fn=0x{fn_req:02X}")
                        if attempt >= retries:
                            if self.ignore_crc_error:
                                logger.error("CRC mismatch(异常帧)重试耗尽已忽略 (风险：异常码可能失真)")
                                return resp  # 返回未校验异常帧
                            raise ModbusException("CRC mismatch (exception)")
                        time.sleep(0.005)
                        continue
                    ex_code = resp[2]
                    raise ModbusException(f"Modbus exception: 0x{ex_code:02X}")

                if fn == 0x03:
                    bc_b = self.bus.read_exact(1, overall_timeout=base)
                    if len(bc_b) < 1:
                        raise ModbusException("Short response (no byte count)")
                    bc = bc_b[0]
                    data_crc = self.bus.read_exact(bc + 2, overall_timeout=base + 0.20)
                    resp = header + bc_b + data_crc
                    if len(data_crc) < bc + 2:
                        raise ModbusException("Short response (payload)")
                elif fn in (0x06, 0x10):
                    rest = self.bus.read_exact(6, overall_timeout=base + 0.20)
                    resp = header + rest
                    if len(rest) < 6:
                        raise ModbusException("Short response")
                else:
                    tail = self.bus.read_exact(254, overall_timeout=base + 0.30)
                    resp = header + tail
                    if len(resp) < 3:
                        raise ModbusException("Short response")

                if resp[-2:] != self._crc16(resp[:-2]):
                    logger.warning(f"CRC mismatch (attempt {attempt}/{retries}) slave={slave} fn=0x{fn_req:02X}")
                    if attempt >= retries:
                        if self.ignore_crc_error:
                            logger.error("CRC mismatch 重试耗尽已忽略 (风险：数据未校验)")
                            return resp  # 直接返回未校验帧
                        raise ModbusException("CRC mismatch")
                    time.sleep(0.005)
                    continue

                if resp[1] != fn_req:
                    raise ModbusException(f"Unexpected function: {resp[1]:02X} (!={fn_req:02X})")

                return resp  # 成功

            except ModbusException:
                if attempt >= retries:
                    # 已在 CRC 分支处理 ignore 情况；这里直接抛出其他类型异常
                    raise
                time.sleep(0.01)

    def read_regs(self, slave: int, addr: int, count: int) -> List[int]:
        fn = 0x03
        payload = bytes([fn]) + addr.to_bytes(2, "big") + count.to_bytes(2, "big")
        resp = self._xfer(slave, payload)
        byte_count = resp[2]
        vals = []
        for i in range(0, byte_count, 2):
            vals.append(int.from_bytes(resp[3 + i:5 + i], "big"))
        return vals

    def write_reg(self, slave: int, addr: int, val: int) -> bool:
        fn = 0x06
        payload = bytes([fn]) + addr.to_bytes(2, "big") + val.to_bytes(2, "big")
        try:
            resp = self._xfer(slave, payload)
        except ModbusException as e:
            logger.warning(f"write_reg: ModbusException slave={slave} addr={addr} val={val}: {e}")
            return False
        except Exception as e:
            logger.error(f"write_reg: unexpected error: {e}")
            return False

        if not resp:
            logger.warning(f"write_reg: no response slave={slave} addr={addr}")
            return False
        return len(resp) >= 8 and resp[1] == fn

    def write_regs(self, slave: int, start: int, values: List[int]) -> bool:
        """
        写多个寄存器（含保护）：当底层无响应或异常时返回 False，不再返回 None。
        """
        fn = 0x10
        bc = len(values) * 2
        payload = bytes([fn]) + start.to_bytes(2, "big") + len(values).to_bytes(2, "big") + bytes([bc])
        for v in values:
            payload += v.to_bytes(2, "big")

        try:
            resp = self._xfer(slave, payload)
        except ModbusException as e:
            logger.warning(f"write_regs: ModbusException slave={slave} start={start} vals={values}: {e}")
            return False
        except Exception as e:
            logger.error(f"write_regs: unexpected error: {e}")
            return False

        if not resp:
            logger.warning(f"write_regs: no response slave={slave} start={start}")
            return False
        return len(resp) >= 8 and resp[1] == fn


# ===================== XYZ: High-level Controller =====================
@dataclass
class MachineConfig:
    steps_per_mm_x: float = 204.8
    steps_per_mm_y: float = 204.8
    steps_per_mm_z: float = 3276.8
    max_travel_x: float = 340.0
    max_travel_y: float = 250.0
    max_travel_z: float = 250.0
    safe_z_height: float = 5.0
    z_approach_height: float = 5.0
    homing_speed: int = 100
    homing_timeout: float = 30.0
    safe_clearance: float = 1.0
    position_stable_time: float = 3.0
    position_check_interval: float = 0.2
    default_speed: int = 100
    default_acceleration: int = 500
    homing_speed_x: Optional[int] = None
    homing_speed_y: Optional[int] = None
    homing_speed_z: Optional[int] = None
    homing_accel_x: Optional[int] = None
    homing_accel_y: Optional[int] = None
    homing_accel_z: Optional[int] = None


@dataclass
class CoordinateOrigin:
    machine_origin_steps: Dict[str, int] = None
    work_origin_steps: Dict[str, int] = None
    is_homed: bool = False

    def __post_init__(self):
        if self.machine_origin_steps is None:
            self.machine_origin_steps = {"x": 0, "y": 0, "z": 0}
        if self.work_origin_steps is None:
            self.work_origin_steps = {"x": 0, "y": 0, "z": 0}


class CoordinateSystemError(Exception):
    pass


class SharedXYZController:
    """XYZ controller using the shared bus and Modbus helper."""

    def __init__(self, bus: SharedRS485Bus, cfg: Optional[MachineConfig] = None):
        self.bus = bus
        self.mb = XYZModbus(bus)
        self.cfg = cfg or MachineConfig()
        self.origin = CoordinateOrigin()
        self.addr = {MotorAxis.X: 1, MotorAxis.Y: 2, MotorAxis.Z: 3}  # keep 1/2/3

    def mm_to_steps(self, axis: MotorAxis, mm: float) -> int:
        if axis == MotorAxis.X:
            return int(mm * self.cfg.steps_per_mm_x)
        if axis == MotorAxis.Y:
            return int(mm * self.cfg.steps_per_mm_y)
        if axis == MotorAxis.Z:
            return int(mm * self.cfg.steps_per_mm_z)
        raise ValueError(axis)

    def steps_to_mm(self, axis: MotorAxis, steps: int) -> float:
        if axis == MotorAxis.X:
            return steps / self.cfg.steps_per_mm_x
        if axis == MotorAxis.Y:
            return steps / self.cfg.steps_per_mm_y
        if axis == MotorAxis.Z:
            return steps / self.cfg.steps_per_mm_z
        raise ValueError(axis)

    def enable(self, axis: MotorAxis, on: bool = True) -> bool:
        return self.mb.write_reg(self.addr[axis], XYZModbus.REG_ENABLE, 0x0001 if on else 0x0000)

    def emergency_stop(self, axis: MotorAxis) -> bool:
        return self.mb.write_reg(self.addr[axis], XYZModbus.REG_EMERGENCY_STOP, 0x0000)

    def get_motor_status(self, axis: MotorAxis) -> MotorPosition:
        a = self.addr[axis]
        v = self.mb.read_regs(a, XYZModbus.REG_STATUS, 6)
        status = MotorStatus(v[0])
        pos = (v[1] << 16) | v[2]
        if pos > 0x7FFFFFFF:
            pos -= 0x100000000
        speed = v[3]
        current = v[5]
        return MotorPosition(pos, speed, current, status)

    def move_to_steps(self, axis: MotorAxis, steps: int, speed_rpm: int = 1000,
                      accel: int = 1000, precision: int = 100) -> bool:
        a = self.addr[axis]
        if steps < 0:
            steps = (steps + 0x100000000) & 0xFFFFFFFF
        hi = (steps >> 16) & 0xFFFF
        lo = steps & 0xFFFF
        ok = self.mb.write_regs(a, XYZModbus.REG_TARGET_POSITION_HIGH, [
            hi, lo, speed_rpm, accel, precision
        ])
        return ok

    def wait_for_completion(self, axis: MotorAxis, timeout: float = 20.0) -> bool:
        t0 = time.time()
        misses = 0
        while time.time() - t0 < timeout:
            try:
                st = self.get_motor_status(axis)
                misses = 0
                if st.status == MotorStatus.STANDBY:
                    return True
            except ModbusException:
                misses += 1
                if misses >= 10:
                    raise
            time.sleep(0.05)
        return False

    def get_homing_speed(self, axis: MotorAxis) -> int:
        if axis == MotorAxis.X and self.cfg.homing_speed_x is not None:
            return self.cfg.homing_speed_x
        if axis == MotorAxis.Y and self.cfg.homing_speed_y is not None:
            return self.cfg.homing_speed_y
        if axis == MotorAxis.Z and self.cfg.homing_speed_z is not None:
            return self.cfg.homing_speed_z
        return self.cfg.homing_speed

    def get_homing_accel(self, axis: MotorAxis) -> int:
        if axis == MotorAxis.X and self.cfg.homing_accel_x is not None:
            return self.cfg.homing_accel_x
        if axis == MotorAxis.Y and self.cfg.homing_accel_y is not None:
            return self.cfg.homing_accel_y
        if axis == MotorAxis.Z and self.cfg.homing_accel_z is not None:
            return self.cfg.homing_accel_z
        return 500

    def home_axis(self, axis: MotorAxis, direction: int = -1) -> bool:
        a = self.addr[axis]
        self.enable(axis, True)
        speed = self.get_homing_speed(axis) * direction
        accel = self.get_homing_accel(axis)
        if not self.mb.write_reg(a, XYZModbus.REG_SPEED_MODE_ACCELERATION, accel & 0xFFFF):
            return False
        if not self.mb.write_reg(a, XYZModbus.REG_SPEED_MODE_SPEED, speed & 0xFFFF):
            return False
        last = None
        stable_since = None
        t0 = time.time()
        while time.time() - t0 < self.cfg.homing_timeout:
            st = self.get_motor_status(axis)
            pos = st.steps
            if (direction < 0 and st.status == MotorStatus.REVERSE_LIMIT_STOP) or \
               (direction > 0 and st.status == MotorStatus.FORWARD_LIMIT_STOP):
                self.emergency_stop(axis)
                final = pos
                break
            if last is not None:
                if abs(pos - last) <= 1:
                    stable_since = stable_since or time.time()
                    if time.time() - stable_since >= self.cfg.position_stable_time:
                        self.emergency_stop(axis)
                        final = pos
                        break
                else:
                    stable_since = None
            last = pos
            time.sleep(self.cfg.position_check_interval)
        else:
            self.emergency_stop(axis)
            final = self.get_motor_status(axis).steps

        clear_steps = self.mm_to_steps(axis, self.cfg.safe_clearance)
        safe_pos = final + (-direction) * clear_steps
        self.move_to_steps(axis, safe_pos, self.cfg.default_speed, self.cfg.default_acceleration)
        self.wait_for_completion(axis, 10.0)
        self.origin.machine_origin_steps[axis.name.lower()] = final
        return True

    def home_all(self) -> bool:
        for ax in (MotorAxis.Z, MotorAxis.X, MotorAxis.Y):
            if not self.home_axis(ax, -1):
                return False
            time.sleep(0.3)
        self.origin.is_homed = True
        # 自动将当前回零后位置作为工作坐标系原点。
        # 这样后续工作系(0,0,0)即对应回零后当前位置。
        try:
            self.set_work_origin_here()
        except Exception:
            # 若失败不影响 homing 结果，只在需要时手动再设。
            pass
        return True

    def set_work_origin_here(self) -> bool:
        pos = {
            'x': self.get_motor_status(MotorAxis.X).steps,
            'y': self.get_motor_status(MotorAxis.Y).steps,
            'z': self.get_motor_status(MotorAxis.Z).steps,
        }
        self.origin.work_origin_steps = pos
        return True

    def work_to_machine_steps(self, x=None, y=None, z=None) -> Dict[str, int]:
        out = {}
        if x is not None:
            out['x'] = self.origin.work_origin_steps['x'] + self.mm_to_steps(MotorAxis.X, x)
        if y is not None:
            out['y'] = self.origin.work_origin_steps['y'] + self.mm_to_steps(MotorAxis.Y, y)
        if z is not None:
            out['z'] = self.origin.work_origin_steps['z'] + self.mm_to_steps(MotorAxis.Z, z)
        return out

    def check_limits(self, x=None, y=None, z=None):
        if x is not None and (x < 0 or x > self.cfg.max_travel_x):
            raise CoordinateSystemError(f"X out of range: {x}")
        if y is not None and (y < 0 or y > self.cfg.max_travel_y):
            raise CoordinateSystemError(f"Y out of range: {y}")
        if z is not None and (z < 0 or z > self.cfg.max_travel_z):
            raise CoordinateSystemError(f"Z out of range: {z}")

    def move_to_work_safe(self, x=None, y=None, z=None, speed=None, accel=None) -> bool:
        self.check_limits(x, y, z)
        speed = speed or self.cfg.default_speed
        accel = accel or self.cfg.default_acceleration
        if z is not None:
            safe_steps = self.work_to_machine_steps(z=self.cfg.safe_z_height)['z']
            self.move_to_steps(MotorAxis.Z, safe_steps, speed, accel)
            self.wait_for_completion(MotorAxis.Z, 10.0)
        if x is not None:
            self.move_to_steps(MotorAxis.X, self.work_to_machine_steps(x=x)['x'], speed, accel)
        if y is not None:
            self.move_to_steps(MotorAxis.Y, self.work_to_machine_steps(y=y)['y'], speed, accel)
        if x is not None:
            self.wait_for_completion(MotorAxis.X, 20.0)
        if y is not None:
            self.wait_for_completion(MotorAxis.Y, 20.0)
        if z is not None:
            self.move_to_steps(MotorAxis.Z, self.work_to_machine_steps(z=z)['z'], speed, accel)
            self.wait_for_completion(MotorAxis.Z, 20.0)
        return True
    
    def move_to_work_safe(self, x=None, y=None, z=None, speed=None, accel=None) -> bool:
        """
        安全移动到工作坐标 (X/Y/Z, mm)。在本次运动过程中临时忽略 Modbus 的 CRC mismatch，
        以避免因为偶发串扰导致的 CRC 校验失败而中断运动；运动结束后恢复原有设置。
        """
        # 1) 限位与默认参数
        self.check_limits(x, y, z)
        speed = speed or self.cfg.default_speed
        accel = accel or self.cfg.default_acceleration

        # 2) 临时开启“忽略 CRC 错误”
        prev_ignore = getattr(self.mb, "ignore_crc_error", False)
        try:
            self.mb.set_ignore_crc(True)

            # 3) 先抬到安全 Z（若给了 z 目标）
            if z is not None:
                safe_steps = self.work_to_machine_steps(z=self.cfg.safe_z_height)['z']
                self.move_to_steps(MotorAxis.Z, safe_steps, speed, accel)
                self.wait_for_completion(MotorAxis.Z, 10.0)

            # 4) 下发 XY 目标
            if x is not None:
                self.move_to_steps(MotorAxis.X, self.work_to_machine_steps(x=x)['x'], speed, accel)
            if y is not None:
                self.move_to_steps(MotorAxis.Y, self.work_to_machine_steps(y=y)['y'], speed, accel)

            # 5) 等待 XY 完成
            if x is not None:
                self.wait_for_completion(MotorAxis.X, 20.0)
            if y is not None:
                self.wait_for_completion(MotorAxis.Y, 20.0)

            # 6) 最后降到目标 Z
            if z is not None:
                self.move_to_steps(MotorAxis.Z, self.work_to_machine_steps(z=z)['z'], speed, accel)
                self.wait_for_completion(MotorAxis.Z, 20.0)

            return True
        finally:
            # 7) 恢复之前的 CRC 忽略开关
            try:
                self.mb.set_ignore_crc(prev_ignore)
            except Exception:
                pass



    def move_rel_z_mm(self, dz: float, speed=1000, accel=1000) -> bool:
        cur = self.get_motor_status(MotorAxis.Z).steps
        tgt = cur + self.mm_to_steps(MotorAxis.Z, dz)
        self.move_to_steps(MotorAxis.Z, tgt, speed, accel, 50)
        return self.wait_for_completion(MotorAxis.Z, 10.0)
    
    def machine_steps_to_work_mm(self, x=None, y=None, z=None):
        out = {}
        if x is not None:
            dx = int(x) - int(self.origin.work_origin_steps['x'])
            out['x'] = self.steps_to_mm(MotorAxis.X, dx)
        if y is not None:
            dy = int(y) - int(self.origin.work_origin_steps['y'])
            out['y'] = self.steps_to_mm(MotorAxis.Y, dy)
        if z is not None:
            dz = int(z) - int(self.origin.work_origin_steps['z'])
            out['z'] = self.steps_to_mm(MotorAxis.Z, dz)
        return out

    def machine_to_work_mm(self, x=None, y=None, z=None):
        out = {}
        if x is not None:
            xs = self.mm_to_steps(MotorAxis.X, x) - self.origin.work_origin_steps['x']
            out['x'] = self.steps_to_mm(MotorAxis.X, xs)
        if y is not None:
            ys = self.mm_to_steps(MotorAxis.Y, y) - self.origin.work_origin_steps['y']
            out['y'] = self.steps_to_mm(MotorAxis.Y, ys)
        if z is not None:
            zs = self.mm_to_steps(MotorAxis.Z, z) - self.origin.work_origin_steps['z']
            out['z'] = self.steps_to_mm(MotorAxis.Z, zs)
        return out

    def get_work_position_mm(self):
        sx = self.get_motor_status(MotorAxis.X).steps
        sy = self.get_motor_status(MotorAxis.Y).steps
        sz = self.get_motor_status(MotorAxis.Z).steps
        return self.machine_steps_to_work_mm(x=sx, y=sy, z=sz)


# ====================== YYQ-style SOPA Pipette ========================
@dataclass
class SOPAConfig:
    address: int = 4          # 固定为 4
    timeout: float = 2.0


class SOPAPipetteYYQ:
    """
    A minimal SOPA pipette driver adapted from YYQ.py to use the shared bus.
    Kept functions: initialize, eject_tip, aspirate, dispense.
    Command packing follows YYQ:  '/{addr}{CMD}E' + checksum(sum&0xFF).
    NOTE: We intentionally keep the 'HE' and 'RE' forms for compatibility.
    """

    def __init__(self, bus: SharedRS485Bus, config: SOPAConfig = SOPAConfig()):
        self.bus = bus
        self.config = config
        self.is_initialized = False

    # ---- low-level helpers
    def _send_command(self, cmd: str):
        address = str(self.config.address)
        full_cmd = f"/{address}{cmd}E".encode("ascii")
        checksum = bytes([sum(full_cmd) & 0xFF])
        payload = full_cmd + checksum
        with self.bus.lock:
            self.bus.reset_input()
            self.bus.write(payload)
        logger.debug(f"[YYQ] TX: {payload!r}")
        # simple pacing, keep same semantics as YYQ example
        time.sleep(0.1)

    def _read_response(self) -> str:
        # very permissive read, mirroring YYQ approach
        time.sleep(0.2)
        data = b""
        with self.bus.lock:
            if self.bus.serial.in_waiting:
                data = self.bus.serial.read_all()
        txt = data.decode(errors="ignore")
        if txt:
            logger.debug(f"[YYQ] RX: {txt!r}")
        return txt

    # ---- the four functions
    def initialize(self) -> bool:
        try:
            logger.info("🚀 初始化移液枪中(YYQ样式)...")
            # YYQ used "HE" (so final becomes '/4HEE' + checksum). Keep it as-is.
            self._send_command("HE")
            time.sleep(10)
            self.is_initialized = True
            logger.info("✅ 初始化完成")
            return True
        except Exception as e:
            logger.error(f"初始化失败: {e}")
            return False

    def eject_tip(self):
        try:
            # YYQ used "RE"
            self._send_command("RE")
            time.sleep(1)
            logger.info("🗑️ 枪头已弹出")
        except Exception as e:
            logger.error(f"弹出枪头失败: {e}")

    def aspirate(self, volume_uL: float):
        try:
            vol = int(volume_uL)
            logger.info(f"💧 吸液 {vol} µL...")
            self._send_command(f"P{vol}")
            time.sleep(max(0.2, vol / 200.0))
            logger.info("✅ 吸液完成")
        except Exception as e:
            logger.error(f"吸液失败: {e}")

    def dispense(self, volume_uL: float):
        try:
            vol = int(volume_uL)
            logger.info(f"💦 排液 {vol} µL...")
            self._send_command(f"D{vol}")
            time.sleep(max(0.2, vol / 200.0))
            logger.info("✅ 排液完成")
        except Exception as e:
            logger.error(f"排液失败: {e}")


# ======================== Liquid Station (ALL) ========================
@dataclass
class LiquidParams:
    delay_after_aspirate: float = 0.5
    delay_after_dispense: float = 0.5


class LiquidStation:
    """Bring XYZ and Pipette together on one port, with a CLI."""

    def __init__(self, port: str = "COM3", baudrate: int = 115200):
        self.bus = SharedRS485Bus(port, baudrate)
        self.points_file = "points.json"
        self.params = LiquidParams()
        self._points = {}
        self.xyz = None
        self.pip = None

    def connect(self):
        self.bus.open()
        self.xyz = SharedXYZController(self.bus)
        self.pip = SOPAPipetteYYQ(self.bus)
        logger.info("Controllers are ready (shared bus).")

    def disconnect(self):
        self.bus.close()

    # ---- points DB
    def load_points(self):
        try:
            with open(self.points_file, "r", encoding="utf-8") as f:
                self._points = json.load(f)
        except Exception:
            self._points = {}

    def save_points(self):
        with open(self.points_file, "w", encoding="utf-8") as f:
            json.dump(self._points, f, indent=2, ensure_ascii=False)

    # ---- station ops
    def home_all(self):
        return self.xyz.home_all()

    def set_work_origin_here(self):
        return self.xyz.set_work_origin_here()

    def move_to(self, x=None, y=None, z=None, speed=None, accel=None):
        return self.xyz.move_to_work_safe(x, y, z, speed, accel)

    def move_to_direct(self, x=None, y=None, z=None, speed=None, accel=None, z_order: str = "auto"):
        """不抬Z直接移动到工作坐标。z_order可为 first/last/auto"""
        return self.xyz.move_to_work_direct(x=x, y=y, z=z, speed=speed, accel=accel, z_order=z_order)

    def move_rel_z(self, dz_mm: float):
        return self.xyz.move_rel_z_mm(dz_mm, 1000, 1000)

    # ---- pipette (only 4 functions)
    def pipette_init(self):
        return self.pip.initialize()

    def eject_tip(self):
        return self.pip.eject_tip()

    def aspirate(self, vol_ul: float):
        return self.pip.aspirate(vol_ul)

    def dispense(self, vol_ul: float):
        return self.pip.dispense(vol_ul)

    def estop_all(self):
        for ax in (MotorAxis.X, MotorAxis.Y, MotorAxis.Z):
            try:
                self.xyz.emergency_stop(ax)
            except Exception:
                pass
        logger.warning("Emergency stop requested")


# ================================ CLI =================================
def main():
    print("\n=== Unified XYZ + YYQ SOPA (Single-Port) ===")
    port = input("串口端口 (默认 COM3): ").strip() or "COM3"
    station = LiquidStation(port)
    station.connect()
    station.load_points()

    init_pip = input("是否初始化移液器? (y/N): ").strip().lower() in ("y", "yes")
    if init_pip:
        if station.pipette_init():
            print("移液器初始化完成。")
        else:
            print("移液器初始化失败。")

    while True:
        print("\n" + "=" * 50)
        print("1) 全轴回零（Z→X→Y）")
        print("2) 设定当前位置为工作原点")
        print("3) 安全移动到点 (X/Y/Z，mm)")
        print("4) Z 轴相对移动 (mm)")
        print("5) 保存/前往点位")
        print("6) 移液：初始化 / 弹出枪头 / 吸液 / 排液")
        print("7) 直接移动(不抬Z) X/Y/Z + 顺序(first/last/auto)")
        print("99) 紧急停止")
        print("0) 退出")
        choice = input("选择: ").strip()

        if choice == "0":
            break

        elif choice == "1":
            print("回零中…")
            print("成功" if station.home_all() else "失败")

        elif choice == "2":
            print("设定工作原点…")
            print("成功" if station.set_work_origin_here() else "失败")

        elif choice == "3":
            x = input("X(mm, 空=跳过): ").strip()
            y = input("Y(mm, 空=跳过): ").strip()
            z = input("Z(mm, 空=跳过): ").strip()
            x = float(x) if x else None
            y = float(y) if y else None
            z = float(z) if z else None
            ok = station.move_to(x, y, z)
            print("到位" if ok else "失败")

        elif choice == "4":
            dz = float(input("Z 相对位移(mm，正=下降): ").strip())
            ok = station.move_rel_z(dz)
            print("完成" if ok else "失败")

        elif choice == "5":
            sub = input("(a)保存点  (b)前往点: ").strip().lower()
            if sub == "a":
                name = input("点名: ").strip()
                x = float(input("X(mm): ").strip())
                y = float(input("Y(mm): ").strip())
                z = float(input("Z(mm): ").strip())
                station._points[name] = {"x": x, "y": y, "z": z}
                station.save_points()
                print("已保存")
            else:
                name = input("点名: ").strip()
                pt = station._points.get(name)
                if not pt:
                    print("未找到该点")
                else:
                    ok = station.move_to(pt["x"], pt["y"], pt["z"])
                    print("到位" if ok else "失败")

        elif choice == "6":
            sub = input("(a)初始化  (b)弹出枪头  (c)吸液  (d)排液: ").strip().lower()
            if sub == "a":
                print("初始化…")
                print("完成" if station.pipette_init() else "失败")
            elif sub == "b":
                print("弹出枪头…")
                station.eject_tip()
                print("完成")
            elif sub == "c":
                vol = float(input("吸液体积(µL): ").strip())
                station.aspirate(vol)
            elif sub == "d":
                vol = float(input("排液体积(µL): ").strip())
                station.dispense(vol)
            else:
                print("无效子选项")

        elif choice == "7":
            x = input("X(mm, 空=跳过): ").strip()
            y = input("Y(mm, 空=跳过): ").strip()
            z = input("Z(mm, 空=跳过): ").strip()
            z_order = input("Z顺序(first/last/auto, 默认auto): ").strip().lower() or "auto"
            x = float(x) if x else None
            y = float(y) if y else None
            z = float(z) if z else None
            ok = station.move_to_direct(x=x, y=y, z=z, z_order=z_order)
            print("到位" if ok else "失败")

        elif choice == "99":
            station.estop_all()
            print("已急停")

        else:
            print("无效选项")

    station.disconnect()
    print("Bye.")


if __name__ == "__main__":
    main()
