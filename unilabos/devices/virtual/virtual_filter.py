import asyncio
import logging
import time as time_module
from typing import Dict, Any, Optional, TYPE_CHECKING

from unilabos.compile.utils.vessel_parser import get_vessel
if TYPE_CHECKING:
    from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class VirtualFilter:
    """Virtual filter device - 完全按照 Filter.action 规范 🌊"""

    _ros_node: "BaseROS2DeviceNode"

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        if device_id is None and "id" in kwargs:
            device_id = kwargs.pop("id")
        if config is None and "config" in kwargs:
            config = kwargs.pop("config")

        self.device_id = device_id or "unknown_filter"
        self.config = config or {}
        self.logger = logging.getLogger(f"VirtualFilter.{self.device_id}")
        self.data = {}

        # 从config或kwargs中获取配置参数
        self.port = self.config.get("port") or kwargs.get("port", "VIRTUAL")
        self._max_temp = self.config.get("max_temp") or kwargs.get("max_temp", 100.0)
        self._max_stir_speed = self.config.get("max_stir_speed") or kwargs.get("max_stir_speed", 1000.0)
        self._max_volume = self.config.get("max_volume") or kwargs.get("max_volume", 500.0)

        # 处理其他kwargs参数
        skip_keys = {"port", "max_temp", "max_stir_speed", "max_volume"}
        for key, value in kwargs.items():
            if key not in skip_keys and not hasattr(self, key):
                setattr(self, key, value)

    def post_init(self, ros_node: "BaseROS2DeviceNode"):
        self._ros_node = ros_node

    async def initialize(self) -> bool:
        """Initialize virtual filter 🚀"""
        self.logger.info(f"🔧 初始化虚拟过滤器 {self.device_id} ✨")

        # 按照 Filter.action 的 feedback 字段初始化
        self.data.update(
            {
                "status": "Idle",
                "progress": 0.0,  # Filter.action feedback
                "current_temp": 25.0,  # Filter.action feedback
                "filtered_volume": 0.0,  # Filter.action feedback
                "message": "Ready for filtration",
            }
        )

        self.logger.info(f"✅ 过滤器 {self.device_id} 初始化完成 🌊")
        return True

    async def cleanup(self) -> bool:
        """Cleanup virtual filter 🧹"""
        self.logger.info(f"🧹 清理虚拟过滤器 {self.device_id} 🔚")

        self.data.update({"status": "Offline"})

        self.logger.info(f"✅ 过滤器 {self.device_id} 清理完成 💤")
        return True

    async def filter(
        self,
        vessel: dict,
        filtrate_vessel: dict = {},
        stir: bool = False,
        stir_speed: float = 300.0,
        temp: float = 25.0,
        continue_heatchill: bool = False,
        volume: float = 0.0,
    ) -> bool:
        """Execute filter action - 完全按照 Filter.action 参数 🌊"""
        vessel_id, _ = get_vessel(vessel)
        filtrate_vessel_id, _ = get_vessel(filtrate_vessel) if filtrate_vessel else (f"{vessel_id}_filtrate", {})

        # 🔧 新增：温度自动调整
        original_temp = temp
        if temp == 0.0:
            temp = 25.0  # 0度自动设置为室温
            self.logger.info(f"🌡️ 温度自动调整: {original_temp}°C → {temp}°C (室温) 🏠")
        elif temp < 4.0:
            temp = 4.0  # 小于4度自动设置为4度
            self.logger.info(f"🌡️ 温度自动调整: {original_temp}°C → {temp}°C (最低温度) ❄️")

        self.logger.info(f"🌊 开始过滤操作: {vessel_id} → {filtrate_vessel_id} 🚰")
        self.logger.info(f"  🌪️ 搅拌: {stir} ({stir_speed} RPM)")
        self.logger.info(f"  🌡️ 温度: {temp}°C")
        self.logger.info(f"  💧 体积: {volume}mL")
        self.logger.info(f"  🔥 保持加热: {continue_heatchill}")

        # 验证参数
        if temp > self._max_temp or temp < 4.0:
            error_msg = f"🌡️ 温度 {temp}°C 超出范围 (4-{self._max_temp}°C) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({"status": f"Error: 温度超出范围 ⚠️", "message": error_msg})
            return False

        if stir and stir_speed > self._max_stir_speed:
            error_msg = f"🌪️ 搅拌速度 {stir_speed} RPM 超出范围 (0-{self._max_stir_speed} RPM) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({"status": f"Error: 搅拌速度超出范围 ⚠️", "message": error_msg})
            return False

        if volume > self._max_volume:
            error_msg = f"💧 过滤体积 {volume} mL 超出范围 (0-{self._max_volume} mL) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({"status": f"Error", "message": error_msg})
            return False

        # 开始过滤
        filter_volume = volume if volume > 0 else 50.0
        self.logger.info(f"🚀 开始过滤 {filter_volume}mL 液体 💧")

        self.data.update(
            {
                "status": f"Running",
                "current_temp": temp,
                "filtered_volume": 0.0,
                "progress": 0.0,
                "message": f"🚀 Starting filtration: {vessel_id} → {filtrate_vessel_id}",
            }
        )

        try:
            # 过滤过程 - 实时更新进度
            start_time = time_module.time()

            # 根据体积和搅拌估算过滤时间
            base_time = filter_volume / 5.0  # 5mL/s 基础速度
            if stir:
                base_time *= 0.8  # 搅拌加速过滤
                self.logger.info(f"🌪️ 搅拌加速过滤，预计时间减少20% ⚡")
            if temp > 50.0:
                base_time *= 0.7  # 高温加速过滤
                self.logger.info(f"🔥 高温加速过滤，预计时间减少30% ⚡")

            filter_time = max(base_time, 10.0)  # 最少10秒
            self.logger.info(f"⏱️ 预计过滤时间: {filter_time:.1f}秒 ⌛")

            while True:
                current_time = time_module.time()
                elapsed = current_time - start_time
                remaining = max(0, filter_time - elapsed)
                progress = min(100.0, (elapsed / filter_time) * 100)
                current_filtered = (progress / 100.0) * filter_volume

                # 更新状态 - 按照 Filter.action feedback 字段
                status_msg = f"🌊 过滤中: {vessel}"
                if stir:
                    status_msg += f" | 🌪️ 搅拌: {stir_speed} RPM"
                status_msg += f" | 🌡️ {temp}°C | 📊 {progress:.1f}% | 💧 已过滤: {current_filtered:.1f}mL"

                self.data.update(
                    {
                        "progress": progress,  # Filter.action feedback
                        "current_temp": temp,  # Filter.action feedback
                        "filtered_volume": current_filtered,  # Filter.action feedback
                        "status": "Running",
                        "message": f"🌊 Filtering: {progress:.1f}% complete, {current_filtered:.1f}mL filtered",
                    }
                )

                # 进度日志（每25%打印一次）
                if progress >= 25 and progress % 25 < 1:
                    self.logger.info(f"📊 过滤进度: {progress:.0f}% | 💧 {current_filtered:.1f}mL 完成 ✨")

                if remaining <= 0:
                    break

                await self._ros_node.sleep(1.0)

            # 过滤完成
            final_temp = temp if continue_heatchill else 25.0
            final_status = f"✅ 过滤完成: {vessel} | 💧 {filter_volume}mL → {filtrate_vessel}"
            if continue_heatchill:
                final_status += " | 🔥 继续加热搅拌"
                self.logger.info(f"🔥 继续保持加热搅拌状态 🌪️")

            self.data.update(
                {
                    "status": final_status,
                    "progress": 100.0,  # Filter.action feedback
                    "current_temp": final_temp,  # Filter.action feedback
                    "filtered_volume": filter_volume,  # Filter.action feedback
                    "message": f"✅ Filtration completed: {filter_volume}mL filtered from {vessel_id}",
                }
            )

            self.logger.info(f"🎉 过滤完成! 💧 {filter_volume}mL 从 {vessel_id} 过滤到 {filtrate_vessel_id} ✨")
            self.logger.info(f"📊 最终状态: 温度 {final_temp}°C | 进度 100% | 体积 {filter_volume}mL 🏁")
            return True

        except Exception as e:
            error_msg = f"过滤过程中发生错误: {str(e)} 💥"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({"status": f"Error", "message": f"❌ Filtration failed: {str(e)}"})
            return False

    # === 核心状态属性 - 按照 Filter.action feedback 字段 ===
    @property
    def status(self) -> str:
        return self.data.get("status", "❓ Unknown")

    @property
    def progress(self) -> float:
        """Filter.action feedback 字段 📊"""
        return self.data.get("progress", 0.0)

    @property
    def current_temp(self) -> float:
        """Filter.action feedback 字段 🌡️"""
        return self.data.get("current_temp", 25.0)

    @property
    def current_status(self) -> str:
        """Filter.action feedback 字段 📋"""
        return self.data.get("current_status", "")

    @property
    def filtered_volume(self) -> float:
        """Filter.action feedback 字段 💧"""
        return self.data.get("filtered_volume", 0.0)

    @property
    def message(self) -> str:
        return self.data.get("message", "")

    @property
    def max_temp(self) -> float:
        return self._max_temp

    @property
    def max_stir_speed(self) -> float:
        return self._max_stir_speed

    @property
    def max_volume(self) -> float:
        return self._max_volume
