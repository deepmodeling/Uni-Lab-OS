# station_simple.py
import logging
import json
import os
from typing import Optional

from .drivers.SharedRS485Bus import SharedRS485Bus
from .XYZStepperController import XYZStepperController
from .drivers.SOPAPipetteYYQ import SOPAPipetteYYQ

logger = logging.getLogger("liquid_station.simple_station")


class Station:
    """
    简化版 Station：
    - 共享 RS485 总线
    - XYZ 三轴控制器 (XYZStepperController)
    - SOPA 移液枪 (SOPAPipetteYYQ)
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        points_file: str = "unilabos/devices/SII/laiyu_xyz_pipette/points.json",
        origin_file: str = "unilabos/devices/SII/laiyu_xyz_pipette/work_origin.json",
    ):
        self.port = port
        self.baudrate = baudrate

        self.bus: SharedRS485Bus = SharedRS485Bus(port=self.port, baudrate=self.baudrate)
        self.xyz: Optional[XYZStepperController] = None
        self.pip: Optional[SOPAPipetteYYQ] = None

        self.points_file = points_file
        self.points: dict[str, dict[str, float]] = {}
        self._load_points()

        self.origin_file = origin_file

        # 新增：记录最近一次 move_xyz 的目标工件坐标
        # 注意：这里的 x/y/z 是“工件坐标系的 mm”，和 move_xyz / move_to_point 使用的坐标体系一致
        self._last_target_work: dict[str, float] = {"x": 0.0, "y": 0.0, "z": 0.0}

    # ===== 点位加载 =====
    def _load_points(self):
        """从 JSON 文件加载预定义点位"""
        if not os.path.exists(self.points_file):
            logger.warning("点位文件不存在: %s", self.points_file)
            self.points = {}
            return

        try:
            with open(self.points_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error("加载点位文件失败 %s: %s", self.points_file, e)
            self.points = {}
            return

        # 简单校验一下结构
        ok_points: dict[str, dict[str, float]] = {}
        for name, p in data.items():
            try:
                x = float(p["x"])
                y = float(p["y"])
                z = float(p["z"])
                ok_points[name] = {"x": x, "y": y, "z": z}
            except Exception:
                logger.warning("点位 %s 格式不正确，已忽略: %s", name, p)

        self.points = ok_points
        logger.info("已加载点位 %d 个", len(self.points))

    # ===== 连接与断开 =====
    def connect(self):
        logger.info("Connecting station on %s ...", self.port)
        self.bus.open()
        # 把 origin_file 传进去，这样 XYZStepperController 会用这个路径加载/保存软零点
        self.xyz = XYZStepperController(self.bus, origin_path=self.origin_file)
        self.pip = SOPAPipetteYYQ(self.bus)
        self.pip.initialize()
        logger.info("Station connected.")

    def disconnect(self):
        self.bus.close()

    def _ensure_connected(self):
        """如果尚未连接设备，则自动连接"""
        if self.xyz is None or self.pip is None:
            self.connect()

    # ===== 对外简单 API =====
    def move_xyz(
        self,
        x: float | None,
        y: float | None,
        z: float | None,
        speed: int = 50,
        acc: int = 60,
    ):
        self._ensure_connected()

        def _to_float_or_none(v):
            if v is None:
                return None
            return float(v)

        x_f = _to_float_or_none(x)
        y_f = _to_float_or_none(y)
        z_f = _to_float_or_none(z)

        # 记录这次的目标工件坐标（None 时保持上一次的值）
        # 这样 save_current_position_as_point 可以“保存最后一次 move_xyz 的目标”
        if x_f is not None:
            self._last_target_work["x"] = x_f
        if y_f is not None:
            self._last_target_work["y"] = y_f
        if z_f is not None:
            self._last_target_work["z"] = z_f

        # 仍然调用底层的工件坐标运动函数
        self.xyz.move_xyz_work(x_f, y_f, z_f, speed, acc)

    def aspirate(self, volume_uL: float):
        self._ensure_connected()
        if not self.pip:
            raise RuntimeError("Pipette not initialized")
        self.pip.aspirate(volume_uL)

    def dispense(self, volume_uL: float):
        self._ensure_connected()
        if not self.pip:
            raise RuntimeError("Pipette not initialized")
        self.pip.dispense(volume_uL)

    def eject_tip(self):
        self._ensure_connected()
        if not self.pip:
            raise RuntimeError("Pipette not initialized")
        self.pip.eject_tip()

    # ===== 新增：按点位名移动 =====
    def move_to_point(
        self,
        name: str,
        speed: int = 50,
        acc: int = 60,
        z_offset: float = 0.0,
    ):
        """
        根据点位名，从 self.points 中取出工件坐标 (x, y, z)，
        然后直接调用 move_xyz 来完成运动并更新 _last_target_work。
        """
        self._ensure_connected()

        if name not in self.points:
            raise ValueError(f"未知点位: {name}，已加载点位数量={len(self.points)}")

        p = self.points[name]
        x = float(p["x"])
        y = float(p["y"])
        z = float(p["z"]) + float(z_offset)

        logger.info(
            "Move to point %s: x=%.3f, y=%.3f, z=%.3f (z_offset=%.3f)",
            name, x, y, z, z_offset,
        )
        # 关键：改为通过 move_xyz，而不是直接调 xyz.move_xyz_work
        self.move_xyz(x, y, z, speed, acc)

    def save_point(self, name: str, x: float, y: float, z: float) -> None:
        """
        将一个点位保存/更新到 points_file 对应的 JSON 文件，并更新内存中的 self.points。

        这里假定 x, y, z 已经是“工件坐标系下的目标坐标（单位 mm）”，
        即直接保存 move_xyz/move_to_point 所用的坐标。
        """
        x = float(x)
        y = float(y)
        z = float(z)

        if self.points is None:
            self.points = {}
        self.points[name] = {"x": x, "y": y, "z": z}

        points_dir = os.path.dirname(self.points_file)
        if points_dir and not os.path.exists(points_dir):
            os.makedirs(points_dir, exist_ok=True)

        try:
            with open(self.points_file, "w", encoding="utf-8") as f:
                json.dump(self.points, f, indent=4, ensure_ascii=False)
            logger.info(
                "点位已保存到 %s: %s -> x=%.6f, y=%.6f, z=%.6f",
                self.points_file, name, x, y, z,
            )
        except Exception as e:
            logger.error("保存点位到文件失败 %s: %s", self.points_file, e)
            raise

    def save_current_position_as_point(self, name: str) -> None:
        """
        不再从电机读“当前位置”，而是把最近一次 move_xyz / move_to_point
        的目标工件坐标 (x, y, z) 直接保存为点位。

        这样即使某个轴通讯不正常，也能保证点位文件里的坐标和
        上层调用 move_xyz 时的目标坐标一致。
        """
        self._ensure_connected()

        x_work = float(self._last_target_work.get("x", 0.0))
        y_work = float(self._last_target_work.get("y", 0.0))
        z_work = float(self._last_target_work.get("z", 0.0))

        self.save_point(name, x_work, y_work, z_work)
        logger.info(
            "最近一次目标坐标已保存为点位 %s: x=%.6f, y=%.6f, z=%.6f (工件坐标 mm)",
            name, x_work, y_work, z_work,
        )

    # ===== 新增：重置工件软零点 =====
    def define_current_as_zero(self):
        """
        以当前 XYZ 三轴的位置为新的工件软零点，并写入 origin_file 指定的 JSON。

        等价于直接调用 XYZStepperController.define_current_as_zero(origin_file)。
        """
        self._ensure_connected()
        if not self.xyz:
            raise RuntimeError("XYZ controller not initialized")

        logger.info("正在以当前步数重置工件软零点到: %s", self.origin_file)
        self.xyz.define_current_as_zero(save_path=self.origin_file)
        logger.info("软零点重置完成: %s", self.xyz.work_origin_steps)