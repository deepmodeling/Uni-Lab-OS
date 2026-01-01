"""
示例设备类文件，用于测试注册表编辑器
"""

import asyncio
from typing import Dict, Any, List
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class AnyDevice:
    @property
    def status(self) -> str:
        return "Idle"

    async def action(self, addr: str) -> bool:
        return True





    def disconnect_device(self) -> bool:
        """
        断开设备连接

        Returns:
            bool: 断开连接是否成功
        """
        self.is_connected = False
        self.current_flow_rate = 0.0
        return True

    def set_flow_rate(self, flow_rate: float, units: str = "ml/min") -> bool:
        """
        设置泵流速

        Args:
            flow_rate: 流速值
            units: 流速单位

        Returns:
            bool: 设置是否成功
        """
        if not self.is_connected:
            return False

        self.current_flow_rate = flow_rate
        return True

    async def pump_volume_async(self, volume: float, flow_rate: float) -> Dict[str, Any]:
        """
        异步泵送指定体积的液体

        Args:
            volume: 目标体积 (mL)
            flow_rate: 泵送流速 (mL/min)

        Returns:
            Dict: 包含操作结果的字典
        """
        if not self.is_connected:
            return {"success": False, "error": "设备未连接"}

        # 计算泵送时间
        pump_time = (volume / flow_rate) * 60  # 转换为秒

        self.current_flow_rate = flow_rate
        await self._ros_node.sleep(min(pump_time, 3.0))  # 模拟泵送过程

        self.total_volume_pumped += volume
        self.current_flow_rate = 0.0

        return {
            "success": True,
            "pumped_volume": volume,
            "actual_time": min(pump_time, 3.0),
            "total_volume": self.total_volume_pumped,
        }

    def emergency_stop(self) -> bool:
        """
        紧急停止泵

        Returns:
            bool: 停止是否成功
        """
        self.current_flow_rate = 0.0
        return True

    def perform_calibration(self, reference_volume: float, measured_volume: float) -> bool:
        """
        执行泵校准

        Args:
            reference_volume: 参考体积
            measured_volume: 实际测量体积

        Returns:
            bool: 校准是否成功
        """
        if measured_volume > 0:
            self.calibration_factor = reference_volume / measured_volume
            return True
        return False

    # 状态查询方法
    def get_connection_status(self) -> str:
        """获取连接状态"""
        return "connected" if self.is_connected else "disconnected"

    def get_current_flow_rate(self) -> float:
        """获取当前流速 (mL/min)"""
        return self.current_flow_rate

    def get_total_volume(self) -> float:
        """获取累计泵送体积 (mL)"""
        return self.total_volume_pumped

    def get_calibration_factor(self) -> float:
        """获取校准因子"""
        return self.calibration_factor

    def get_pump_mode(self) -> str:
        """获取泵送模式"""
        return self.pump_mode

    def get_device_status(self) -> Dict[str, Any]:
        """获取设备完整状态信息"""
        return {
            "device_id": self.device_id,
            "connected": self.is_connected,
            "flow_rate": self.current_flow_rate,
            "total_volume": self.total_volume_pumped,
            "calibration_factor": self.calibration_factor,
            "mode": self.pump_mode,
            "running": self.current_flow_rate > 0,
        }


class AdvancedTemperatureController:
    """
    高级温度控制器

    支持PID控制、多点温度监控和程序化温度曲线。
    适用于需要精确温度控制的化学反应和材料处理过程。
    """

    _ros_node: BaseROS2DeviceNode

    def __init__(self, controller_id: str = "temp_controller_01"):
        """
        初始化温度控制器

        Args:
            controller_id: 控制器ID
        """
        self.controller_id = controller_id
        self.current_temperature = 25.0
        self.target_temperature = 25.0
        self.is_heating = False
        self.is_cooling = False
        self.pid_enabled = True
        self.temperature_history: List[Dict] = []

    def post_init(self, ros_node: BaseROS2DeviceNode):
        self._ros_node = ros_node

    def set_target_temperature(self, temperature: float, rate: float = 10.0) -> bool:
        """
        设置目标温度

        Args:
            temperature: 目标温度 (°C)
            rate: 升温/降温速率 (°C/min)

        Returns:
            bool: 设置是否成功
        """
        self.target_temperature = temperature
        return True

    async def heat_to_temperature_async(
        self, temperature: float, tolerance: float = 0.5, timeout: int = 600
    ) -> Dict[str, Any]:
        """
        异步加热到指定温度

        Args:
            temperature: 目标温度 (°C)
            tolerance: 温度容差 (°C)
            timeout: 最大等待时间 (秒)

        Returns:
            Dict: 操作结果
        """
        self.target_temperature = temperature
        start_temp = self.current_temperature

        if temperature > start_temp:
            self.is_heating = True
        elif temperature < start_temp:
            self.is_cooling = True

        # 模拟温度变化过程
        steps = min(abs(temperature - start_temp) * 2, 20)  # 计算步数
        step_time = min(timeout / steps if steps > 0 else 1, 2.0)  # 每步最多2秒

        for step in range(int(steps)):
            progress = (step + 1) / steps
            self.current_temperature = start_temp + (temperature - start_temp) * progress

            # 记录温度历史
            self.temperature_history.append(
                {
                    "timestamp": asyncio.get_event_loop().time(),
                    "temperature": self.current_temperature,
                    "target": self.target_temperature,
                }
            )

            await self._ros_node.sleep(step_time)

            # 保持历史记录不超过100条
            if len(self.temperature_history) > 100:
                self.temperature_history.pop(0)

        # 最终设置为目标温度
        self.current_temperature = temperature
        self.is_heating = False
        self.is_cooling = False

        return {
            "success": True,
            "final_temperature": self.current_temperature,
            "start_temperature": start_temp,
            "time_taken": steps * step_time,
        }

    def enable_pid_control(self, kp: float = 1.0, ki: float = 0.1, kd: float = 0.05) -> bool:
        """
        启用PID控制

        Args:
            kp: 比例增益
            ki: 积分增益
            kd: 微分增益

        Returns:
            bool: 启用是否成功
        """
        self.pid_enabled = True
        return True

    def run_temperature_program(self, program: List[Dict]) -> bool:
        """
        运行温度程序

        Args:
            program: 温度程序列表，每个元素包含温度和持续时间

        Returns:
            bool: 程序启动是否成功
        """
        # 模拟程序启动
        return True

    # 状态查询方法
    def get_current_temperature(self) -> float:
        """获取当前温度 (°C)"""
        return round(self.current_temperature, 2)

    def get_target_temperature(self) -> float:
        """获取目标温度 (°C)"""
        return self.target_temperature

    def get_heating_status(self) -> bool:
        """获取加热状态"""
        return self.is_heating

    def get_cooling_status(self) -> bool:
        """获取制冷状态"""
        return self.is_cooling

    def get_pid_status(self) -> bool:
        """获取PID控制状态"""
        return self.pid_enabled

    def get_temperature_history(self) -> List[Dict]:
        """获取温度历史记录"""
        return self.temperature_history[-10:]  # 返回最近10条记录

    def get_controller_status(self) -> Dict[str, Any]:
        """获取控制器完整状态"""
        return {
            "controller_id": self.controller_id,
            "current_temp": self.current_temperature,
            "target_temp": self.target_temperature,
            "is_heating": self.is_heating,
            "is_cooling": self.is_cooling,
            "pid_enabled": self.pid_enabled,
            "history_count": len(self.temperature_history),
        }


class MultiChannelAnalyzer:
    """
    多通道分析仪

    支持同时监测多个通道的信号，提供实时数据采集和分析功能。
    常用于光谱分析、电化学测量等应用场景。
    """

    _ros_node: BaseROS2DeviceNode

    def __init__(self, analyzer_id: str = "analyzer_01", channels: int = 8):
        """
        初始化多通道分析仪

        Args:
            analyzer_id: 分析仪ID
            channels: 通道数量
        """
        self.analyzer_id = analyzer_id
        self.channel_count = channels
        self.channel_data = {i: {"value": 0.0, "unit": "V", "enabled": True} for i in range(channels)}
        self.is_measuring = False
        self.sample_rate = 1000  # Hz

    def post_init(self, ros_node: BaseROS2DeviceNode):
        self._ros_node = ros_node

    def configure_channel(self, channel: int, enabled: bool = True, unit: str = "V") -> bool:
        """
        配置通道

        Args:
            channel: 通道编号
            enabled: 是否启用
            unit: 测量单位

        Returns:
            bool: 配置是否成功
        """
        if 0 <= channel < self.channel_count:
            self.channel_data[channel]["enabled"] = enabled
            self.channel_data[channel]["unit"] = unit
            return True
        return False

    async def start_measurement_async(self, duration: int = 10) -> Dict[str, Any]:
        """
        开始异步测量

        Args:
            duration: 测量持续时间（秒）

        Returns:
            Dict: 测量结果
        """
        self.is_measuring = True

        # 模拟数据采集
        measurements = []
        for _ in range(duration):
            timestamp = asyncio.get_event_loop().time()
            frame_data = {}

            for channel in range(self.channel_count):
                if self.channel_data[channel]["enabled"]:
                    # 模拟传感器数据
                    import random

                    value = random.uniform(-5.0, 5.0)
                    frame_data[f"channel_{channel}"] = value
                    self.channel_data[channel]["value"] = value

            measurements.append({"timestamp": timestamp, "data": frame_data})

            await self._ros_node.sleep(1.0)  # 每秒采集一次

        self.is_measuring = False

        return {
            "success": True,
            "duration": duration,
            "samples_count": len(measurements),
            "measurements": measurements[-5:],  # 只返回最后5个样本
            "channels_active": len([ch for ch in self.channel_data.values() if ch["enabled"]]),
        }

    def stop_measurement(self) -> bool:
        """
        停止测量

        Returns:
            bool: 停止是否成功
        """
        self.is_measuring = False
        return True

    def reset_channels(self) -> bool:
        """
        重置所有通道

        Returns:
            bool: 重置是否成功
        """
        for channel in self.channel_data:
            self.channel_data[channel]["value"] = 0.0
        return True

    # 状态查询方法
    def get_measurement_status(self) -> bool:
        """获取测量状态"""
        return self.is_measuring

    def get_channel_count(self) -> int:
        """获取通道数量"""
        return self.channel_count

    def get_sample_rate(self) -> float:
        """获取采样率 (Hz)"""
        return self.sample_rate

    def get_channel_values(self) -> Dict[int, float]:
        """获取所有通道的当前值"""
        return {ch: data["value"] for ch, data in self.channel_data.items() if data["enabled"]}

    def get_enabled_channels(self) -> List[int]:
        """获取已启用的通道列表"""
        return [ch for ch, data in self.channel_data.items() if data["enabled"]]

    def get_analyzer_status(self) -> Dict[str, Any]:
        """获取分析仪完整状态"""
        return {
            "analyzer_id": self.analyzer_id,
            "channel_count": self.channel_count,
            "is_measuring": self.is_measuring,
            "sample_rate": self.sample_rate,
            "active_channels": len(self.get_enabled_channels()),
            "channel_data": self.channel_data,
        }


class AutomatedDispenser:
    """
    自动分配器

    精确控制固体和液体材料的分配，支持多种分配模式和容器管理。
    集成称重功能，确保分配精度和重现性。
    """

    _ros_node: BaseROS2DeviceNode

    def __init__(self, dispenser_id: str = "dispenser_01"):
        """
        初始化自动分配器

        Args:
            dispenser_id: 分配器ID
        """
        self.dispenser_id = dispenser_id
        self.is_ready = True
        self.current_position = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.dispensed_total = 0.0
        self.container_capacity = 1000.0  # mL
        self.precision_mode = True

    def post_init(self, ros_node: BaseROS2DeviceNode):
        self._ros_node = ros_node

    def move_to_position(self, x: float, y: float, z: float) -> bool:
        """
        移动到指定位置

        Args:
            x: X坐标 (mm)
            y: Y坐标 (mm)
            z: Z坐标 (mm)

        Returns:
            bool: 移动是否成功
        """
        self.current_position = {"x": x, "y": y, "z": z}
        return True

    async def dispense_liquid_async(self, volume: float, container_id: str, viscosity: str = "low") -> Dict[str, Any]:
        """
        异步分配液体

        Args:
            volume: 分配体积 (mL)
            container_id: 容器ID
            viscosity: 液体粘度等级

        Returns:
            Dict: 分配结果
        """
        if not self.is_ready:
            return {"success": False, "error": "设备未就绪"}

        if volume <= 0:
            return {"success": False, "error": "体积必须大于0"}

        # 模拟分配过程
        dispense_time = volume * 0.1  # 每mL需要0.1秒
        if viscosity == "high":
            dispense_time *= 2  # 高粘度液体需要更长时间

        await self._ros_node.sleep(min(dispense_time, 5.0))  # 最多等待5秒

        self.dispensed_total += volume

        return {
            "success": True,
            "dispensed_volume": volume,
            "container_id": container_id,
            "actual_time": min(dispense_time, 5.0),
            "total_dispensed": self.dispensed_total,
        }

    def clean_dispenser(self, wash_volume: float = 5.0) -> bool:
        """
        清洗分配器

        Args:
            wash_volume: 清洗液体积 (mL)

        Returns:
            bool: 清洗是否成功
        """
        # 模拟清洗过程
        return True

    def calibrate_volume(self, target_volume: float) -> bool:
        """
        校准分配体积

        Args:
            target_volume: 校准目标体积 (mL)

        Returns:
            bool: 校准是否成功
        """
        # 模拟校准过程
        return True

    # 状态查询方法
    def get_ready_status(self) -> bool:
        """获取就绪状态"""
        return self.is_ready

    def get_current_position(self) -> Dict[str, float]:
        """获取当前位置坐标"""
        return self.current_position.copy()

    def get_dispensed_total(self) -> float:
        """获取累计分配体积 (mL)"""
        return self.dispensed_total

    def get_container_capacity(self) -> float:
        """获取容器容量 (mL)"""
        return self.container_capacity

    def get_precision_mode(self) -> bool:
        """获取精密模式状态"""
        return self.precision_mode

    def get_dispenser_status(self) -> Dict[str, Any]:
        """获取分配器完整状态"""
        return {
            "dispenser_id": self.dispenser_id,
            "ready": self.is_ready,
            "position": self.current_position,
            "dispensed_total": self.dispensed_total,
            "capacity": self.container_capacity,
            "precision_mode": self.precision_mode,
        }
