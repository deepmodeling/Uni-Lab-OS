import asyncio
import logging
import time as time_module
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


def debug_print(message):
    """调试输出 🔍"""
    print(f"🌪️ [ROTAVAP] {message}", flush=True)


class VirtualRotavap:
    """Virtual rotary evaporator device - 简化版，只保留核心功能 🌪️"""

    _ros_node: "BaseROS2DeviceNode"

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        # 处理可能的不同调用方式
        if device_id is None and "id" in kwargs:
            device_id = kwargs.pop("id")
        if config is None and "config" in kwargs:
            config = kwargs.pop("config")

        # 设置默认值
        self.device_id = device_id or "unknown_rotavap"
        self.config = config or {}

        self.logger = logging.getLogger(f"VirtualRotavap.{self.device_id}")
        self.data = {}

        # 从config或kwargs中获取配置参数
        self.port = self.config.get("port") or kwargs.get("port", "VIRTUAL")
        self._max_temp = self.config.get("max_temp") or kwargs.get("max_temp", 180.0)
        self._max_rotation_speed = self.config.get("max_rotation_speed") or kwargs.get("max_rotation_speed", 280.0)

        # 处理其他kwargs参数
        skip_keys = {"port", "max_temp", "max_rotation_speed"}
        for key, value in kwargs.items():
            if key not in skip_keys and not hasattr(self, key):
                setattr(self, key, value)

        print(f"🌪️ === 虚拟旋转蒸发仪 {self.device_id} 已创建 === ✨")
        print(f"🔥 温度范围: 10°C ~ {self._max_temp}°C | 🌀 转速范围: 10 ~ {self._max_rotation_speed} RPM")

    def post_init(self, ros_node: "BaseROS2DeviceNode"):
        self._ros_node = ros_node

    async def initialize(self) -> bool:
        """Initialize virtual rotary evaporator 🚀"""
        self.logger.info(f"🔧 初始化虚拟旋转蒸发仪 {self.device_id} ✨")

        # 只保留核心状态
        self.data.update(
            {
                "status": "🏠 待机中",
                "rotavap_state": "Ready",  # Ready, Evaporating, Completed, Error
                "current_temp": 25.0,
                "target_temp": 25.0,
                "rotation_speed": 0.0,
                "vacuum_pressure": 1.0,  # 大气压
                "evaporated_volume": 0.0,
                "progress": 0.0,
                "remaining_time": 0.0,
                "message": "🌪️ Ready for evaporation",
            }
        )

        self.logger.info(f"✅ 旋转蒸发仪 {self.device_id} 初始化完成 🌪️")
        self.logger.info(
            f"📊 设备规格: 温度范围 10°C ~ {self._max_temp}°C | 转速范围 10 ~ {self._max_rotation_speed} RPM"
        )
        return True

    async def cleanup(self) -> bool:
        """Cleanup virtual rotary evaporator 🧹"""
        self.logger.info(f"🧹 清理虚拟旋转蒸发仪 {self.device_id} 🔚")

        self.data.update(
            {
                "status": "💤 离线",
                "rotavap_state": "Offline",
                "current_temp": 25.0,
                "rotation_speed": 0.0,
                "vacuum_pressure": 1.0,
                "message": "💤 System offline",
            }
        )

        self.logger.info(f"✅ 旋转蒸发仪 {self.device_id} 清理完成 💤")
        return True

    async def evaporate(
        self,
        vessel: str,
        pressure: float = 0.1,
        temp: float = 60.0,
        time: float = 180.0,
        stir_speed: float = 100.0,
        solvent: str = "",
        **kwargs,
    ) -> bool:
        """Execute evaporate action - 简化版 🌪️"""

        # 🔧 新增：确保time参数是数值类型
        if isinstance(time, str):
            try:
                time = float(time)
            except ValueError:
                self.logger.error(f"❌ 无法转换时间参数 '{time}' 为数值，使用默认值180.0秒")
                time = 180.0
        elif not isinstance(time, (int, float)):
            self.logger.error(f"❌ 时间参数类型无效: {type(time)}，使用默认值180.0秒")
            time = 180.0

        # 确保time是float类型; 并加速
        time = float(time) / 10.0

        # 🔧 简化处理：如果vessel就是设备自己，直接操作
        if vessel == self.device_id:
            debug_print(f"🎯 在设备 {self.device_id} 上直接执行蒸发操作")
            actual_vessel = self.device_id
        else:
            actual_vessel = vessel

        # 参数预处理
        if solvent:
            self.logger.info(f"🧪 识别到溶剂: {solvent}")
            # 根据溶剂调整参数
            solvent_lower = solvent.lower()
            if any(s in solvent_lower for s in ["water", "aqueous"]):
                temp = max(temp, 80.0)
                pressure = max(pressure, 0.2)
                self.logger.info(f"💧 水系溶剂：调整参数 → 温度 {temp}°C, 压力 {pressure} bar")
            elif any(s in solvent_lower for s in ["ethanol", "methanol", "acetone"]):
                temp = min(temp, 50.0)
                pressure = min(pressure, 0.05)
                self.logger.info(f"⚡ 易挥发溶剂：调整参数 → 温度 {temp}°C, 压力 {pressure} bar")

        self.logger.info(f"🌪️ 开始蒸发操作: {actual_vessel}")
        self.logger.info(f"  🥽 容器: {actual_vessel}")
        self.logger.info(f"  🌡️ 温度: {temp}°C")
        self.logger.info(f"  💨 真空度: {pressure} bar")
        self.logger.info(f"  ⏰ 时间: {time}s")
        self.logger.info(f"  🌀 转速: {stir_speed} RPM")
        if solvent:
            self.logger.info(f"  🧪 溶剂: {solvent}")

        # 验证参数
        if temp > self._max_temp or temp < 10.0:
            error_msg = f"🌡️ 温度 {temp}°C 超出范围 (10-{self._max_temp}°C) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update(
                {
                    "status": f"❌ 错误: 温度超出范围",
                    "rotavap_state": "Error",
                    "current_temp": 25.0,
                    "progress": 0.0,
                    "evaporated_volume": 0.0,
                    "message": error_msg,
                }
            )
            return False

        if stir_speed > self._max_rotation_speed or stir_speed < 10.0:
            error_msg = f"🌀 旋转速度 {stir_speed} RPM 超出范围 (10-{self._max_rotation_speed} RPM) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update(
                {
                    "status": f"❌ 错误: 转速超出范围",
                    "rotavap_state": "Error",
                    "current_temp": 25.0,
                    "progress": 0.0,
                    "evaporated_volume": 0.0,
                    "message": error_msg,
                }
            )
            return False

        if pressure < 0.01 or pressure > 1.0:
            error_msg = f"💨 真空度 {pressure} bar 超出范围 (0.01-1.0 bar) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update(
                {
                    "status": f"❌ 错误: 压力超出范围",
                    "rotavap_state": "Error",
                    "current_temp": 25.0,
                    "progress": 0.0,
                    "evaporated_volume": 0.0,
                    "message": error_msg,
                }
            )
            return False

        # 开始蒸发 - 🔧 现在time已经确保是float类型
        self.logger.info(f"🚀 启动蒸发程序! 预计用时 {time/60:.1f}分钟 ⏱️")

        self.data.update(
            {
                "status": f"🌪️ 蒸发中: {actual_vessel}",
                "rotavap_state": "Evaporating",
                "current_temp": temp,
                "target_temp": temp,
                "rotation_speed": stir_speed,
                "vacuum_pressure": pressure,
                "remaining_time": time,
                "progress": 0.0,
                "evaporated_volume": 0.0,
                "message": f"🌪️ Evaporating {actual_vessel} at {temp}°C, {pressure} bar, {stir_speed} RPM",
            }
        )

        try:
            # 蒸发过程 - 实时更新进度
            start_time = time_module.time()
            total_time = time
            last_logged_progress = 0

            while True:
                current_time = time_module.time()
                elapsed = current_time - start_time
                remaining = max(0, total_time - elapsed)
                progress = min(100.0, (elapsed / total_time) * 100)

                # 模拟蒸发体积 - 根据溶剂类型调整
                if solvent and any(s in solvent.lower() for s in ["water", "aqueous"]):
                    evaporated_vol = progress * 0.6  # 水系溶剂蒸发慢
                elif solvent and any(s in solvent.lower() for s in ["ethanol", "methanol", "acetone"]):
                    evaporated_vol = progress * 1.0  # 易挥发溶剂蒸发快
                else:
                    evaporated_vol = progress * 0.8  # 默认蒸发量

                # 🔧 更新状态 - 确保包含所有必需字段
                status_msg = f"🌪️ 蒸发中: {actual_vessel} | 🌡️ {temp}°C | 💨 {pressure} bar | 🌀 {stir_speed} RPM | 📊 {progress:.1f}% | ⏰ 剩余: {remaining:.0f}s"

                self.data.update(
                    {
                        "remaining_time": remaining,
                        "progress": progress,
                        "evaporated_volume": evaporated_vol,
                        "current_temp": temp,
                        "status": status_msg,
                        "message": f"🌪️ Evaporating: {progress:.1f}% complete, 💧 {evaporated_vol:.1f}mL evaporated, ⏰ {remaining:.0f}s remaining",
                    }
                )

                # 进度日志（每25%打印一次）
                if progress >= 25 and int(progress) % 25 == 0 and int(progress) != last_logged_progress:
                    self.logger.info(
                        f"📊 蒸发进度: {progress:.0f}% | 💧 已蒸发: {evaporated_vol:.1f}mL | ⏰ 剩余: {remaining:.0f}s ✨"
                    )
                    last_logged_progress = int(progress)

                # 时间到了，退出循环
                if remaining <= 0:
                    break

                # 每秒更新一次
                await self._ros_node.sleep(1.0)

            # 蒸发完成
            if solvent and any(s in solvent.lower() for s in ["water", "aqueous"]):
                final_evaporated = 60.0  # 水系溶剂
            elif solvent and any(s in solvent.lower() for s in ["ethanol", "methanol", "acetone"]):
                final_evaporated = 100.0  # 易挥发溶剂
            else:
                final_evaporated = 80.0  # 默认

            self.data.update(
                {
                    "status": f"✅ 蒸发完成: {actual_vessel} | 💧 蒸发量: {final_evaporated:.1f}mL",
                    "rotavap_state": "Completed",
                    "evaporated_volume": final_evaporated,
                    "progress": 100.0,
                    "current_temp": temp,
                    "remaining_time": 0.0,
                    "rotation_speed": 0.0,
                    "vacuum_pressure": 1.0,
                    "message": f"✅ Evaporation completed: {final_evaporated}mL evaporated from {actual_vessel}",
                }
            )

            self.logger.info(f"🎉 蒸发操作完成! ✨")
            self.logger.info(f"📊 蒸发结果:")
            self.logger.info(f"  🥽 容器: {actual_vessel}")
            self.logger.info(f"  💧 蒸发量: {final_evaporated:.1f}mL")
            self.logger.info(f"  🌡️ 蒸发温度: {temp}°C")
            self.logger.info(f"  💨 真空度: {pressure} bar")
            self.logger.info(f"  🌀 旋转速度: {stir_speed} RPM")
            self.logger.info(f"  ⏱️ 总用时: {total_time:.0f}s")
            if solvent:
                self.logger.info(f"  🧪 处理溶剂: {solvent} 🏁")

            return True

        except Exception as e:
            # 出错处理
            error_msg = f"蒸发过程中发生错误: {str(e)} 💥"
            self.logger.error(f"❌ {error_msg}")

            self.data.update(
                {
                    "status": f"❌ 蒸发错误: {str(e)}",
                    "rotavap_state": "Error",
                    "current_temp": 25.0,
                    "progress": 0.0,
                    "evaporated_volume": 0.0,
                    "rotation_speed": 0.0,
                    "vacuum_pressure": 1.0,
                    "message": f"❌ Evaporation failed: {str(e)}",
                }
            )
            return False

    # === 核心状态属性 ===
    @property
    def status(self) -> str:
        return self.data.get("status", "❓ Unknown")

    @property
    def rotavap_state(self) -> str:
        return self.data.get("rotavap_state", "Unknown")

    @property
    def current_temp(self) -> float:
        return self.data.get("current_temp", 25.0)

    @property
    def rotation_speed(self) -> float:
        return self.data.get("rotation_speed", 0.0)

    @property
    def vacuum_pressure(self) -> float:
        return self.data.get("vacuum_pressure", 1.0)

    @property
    def evaporated_volume(self) -> float:
        return self.data.get("evaporated_volume", 0.0)

    @property
    def progress(self) -> float:
        return self.data.get("progress", 0.0)

    @property
    def message(self) -> str:
        return self.data.get("message", "")

    @property
    def max_temp(self) -> float:
        return self._max_temp

    @property
    def max_rotation_speed(self) -> float:
        return self._max_rotation_speed

    @property
    def remaining_time(self) -> float:
        return self.data.get("remaining_time", 0.0)
