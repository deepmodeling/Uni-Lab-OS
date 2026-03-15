# xyz_stepper_controller.py
import time
import logging
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, List

from .drivers.XYZModbus import XYZModbus, MotorStatus, ModbusException
from .drivers.SharedRS485Bus import SharedRS485Bus

logger = logging.getLogger("XYZStepper")


class MotorAxis(Enum):
    X = 1
    Y = 2
    Z = 3


@dataclass
class MotorPosition:
    steps: int
    speed: int
    current: int
    status: MotorStatus


class XYZStepperController:
    """
    XYZ 三轴步进控制器（基于 SharedRS485Bus + XYZModbus）
    保持和你原版大体一致的接口。
    """

    STEPS_PER_REV = 16384
    LEAD_MM_X, LEAD_MM_Y, LEAD_MM_Z = 80.0, 80.0, 5.0
    STEPS_PER_MM_X = STEPS_PER_REV / LEAD_MM_X
    STEPS_PER_MM_Y = STEPS_PER_REV / LEAD_MM_Y
    STEPS_PER_MM_Z = STEPS_PER_REV / LEAD_MM_Z

    # 寄存器映射沿用你原来的
    REG_STATUS, REG_POS_HIGH, REG_POS_LOW = 0x00, 0x01, 0x02
    REG_ACTUAL_SPEED, REG_CURRENT, REG_ENABLE = 0x03, 0x05, 0x06
    REG_ZERO_CMD, REG_TARGET_HIGH, REG_TARGET_LOW = 0x0F, 0x10, 0x11
    REG_SPEED, REG_ACCEL, REG_PRECISION, REG_START = 0x13, 0x14, 0x15, 0x16
    REG_COMMAND = 0x60

    def __init__(
        self,
        bus: SharedRS485Bus,
        origin_path: str = "unilabos/devices/laiyu_xyz_pipette/work_origin.json",
    ):
        """
        这里不再自己开串口，而是复用上层传入的 SharedRS485Bus。
        """
        self.bus = bus
        self.modbus = XYZModbus(bus)
        self.axis_addr: Dict[MotorAxis, int] = {
            MotorAxis.X: 1,
            MotorAxis.Y: 2,
            MotorAxis.Z: 3,
        }
        self.work_origin_steps = {"x": 0, "y": 0, "z": 0}
        self.is_homed = False
        self._load_work_origin(origin_path)

    # --------- 工具函数 ----------
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

    # --------- 状态与控制 ----------
    def get_status(self, axis: str = "Z") -> List[int]:
        """
        返回简化数组格式: [steps, speed, current, status_value]
        读 6 个寄存器：status, posH, posL, speed, current, enable
        """
        if isinstance(axis, MotorAxis):
            axis_enum = axis
        elif isinstance(axis, str):
            axis_enum = MotorAxis[axis.upper()]
        else:
            raise TypeError("axis 参数必须为 str 或 MotorAxis")

        slave = self.axis_addr[axis_enum]
        try:
            vals = self.modbus.read_regs(slave, self.REG_STATUS, 6)
        except Exception as e:
            logger.warning("get_status fail axis=%s: %s", axis, e)
            return [0, 0, 0, MotorStatus.STANDBY.value]

        if vals is None or len(vals) < 5:
            return [0, 0, 0, MotorStatus.STANDBY.value]

        steps = self.s32(vals[1], vals[2])
        speed = self.s16(vals[3])
        current = vals[4]
        st_val = int(MotorStatus(vals[0]).value)
        return [steps, speed, current, st_val]

    def enable(self, axis: str, state: bool) -> bool:
        a = MotorAxis[axis.upper()]
        return self.modbus.write_reg(self.axis_addr[a], self.REG_ENABLE, 1 if state else 0)

    def wait_complete(self, axis: str, timeout: float = 30.0) -> bool:
        a = axis.upper()
        start = time.time()
        while time.time() - start < timeout:
            try:
                vals = self.get_status(a)
            except Exception:
                time.sleep(0.1)
                continue

            if len(vals) <= 3:
                time.sleep(0.1)
                continue

            try:
                st = MotorStatus(vals[3])
            except Exception:
                time.sleep(0.1)
                continue

            if st == MotorStatus.STANDBY:
                return True

            if st in (
                MotorStatus.COLLISION_STOP,
                MotorStatus.FORWARD_LIMIT_STOP,
                MotorStatus.REVERSE_LIMIT_STOP,
            ):
                logger.warning("%s 轴异常停止: %s", a, st.name)
                return False

            time.sleep(0.1)
        return False

    # --------- 控制命令 ----------
    def move_to(self, axis: str, steps: int,
                speed: int = 2000, acc: int = 500, precision: int = 50) -> bool:
        """
        单轴绝对运动（机器坐标系步数）。
        """
        a = MotorAxis[axis.upper()]
        addr = self.axis_addr[a]
        hi, lo = (steps >> 16) & 0xFFFF, steps & 0xFFFF
        values = [hi, lo, speed, acc, precision]
        ok = self.modbus.write_regs(addr, self.REG_TARGET_HIGH, values)
        if ok:
            self.modbus.write_reg(addr, self.REG_START, 1)
        return ok

    def move_xyz_work(
        self,
        x: Optional[float] = 0.0,
        y: Optional[float] = 0.0,
        z: Optional[float] = 0.0,
        speed: int = 100,
        acc: int = 1500,
    ):
        """
        按你原来的安全顺序：Z 抬起 -> XY 平面移动 -> Z 下探。
        x/y/z 为 “工作坐标” mm。
        """
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

    # --------- 坐标与零点 ----------
    def _to_machine_steps(self, axis: str, mm: float) -> int:
        """
        工作坐标 (mm) -> 机器坐标步数（= 工件软零点 + 位移）。
        """
        base = self.work_origin_steps.get(axis.lower(), 0)
        return base + self.mm_to_steps(axis, mm)

    def define_current_as_zero(self, save_path: str = "work_origin.json"):
        import json
        from datetime import datetime

        origin = {}
        for axis in ["X", "Y", "Z"]:
            vals = self.get_status(axis)
            origin[axis.lower()] = int(vals[0])
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "work_origin_steps": origin,
                    "timestamp": datetime.now().isoformat(),
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        self.work_origin_steps = origin
        self.is_homed = True
        logger.info("软零点已定义并保存到 %s", save_path)

    def _load_work_origin(self, path: str) -> bool:
        import json, os

        if not os.path.exists(path):
            logger.warning("未找到软零点文件: %s", path)
            return False
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.work_origin_steps = data.get("work_origin_steps", {"x": 0, "y": 0, "z": 0})
        self.is_homed = True
        logger.info("软零点已加载: %s", self.work_origin_steps)
        return True

    def return_to_work_origin(self, speed: int = 200, acc: int = 800):
        """
        回工件软零点：Z 抬起 -> X/Y -> Z。
        """
        self.move_to("Z", self._to_machine_steps("Z", 0.0), speed, acc)
        self.wait_complete("Z")

        self.move_to("X", self.work_origin_steps.get("x", 0), speed, acc)
        self.move_to("Y", self.work_origin_steps.get("y", 0), speed, acc)
        self.wait_complete("X")
        self.wait_complete("Y")

        self.move_to("Z", self.work_origin_steps.get("z", 0), speed, acc)
        self.wait_complete("Z")