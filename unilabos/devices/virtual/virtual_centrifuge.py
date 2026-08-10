import asyncio
import logging
import time as time_module
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from unilabos.device_runtime.node import DeviceNode


class VirtualCentrifuge:
    """Virtual centrifuge device - 简化版，只保留核心功能"""
    
    _ros_node: "DeviceNode"

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        # 处理可能的不同调用方式
        if device_id is None and "id" in kwargs:
            device_id = kwargs.pop("id")
        if config is None and "config" in kwargs:
            config = kwargs.pop("config")

        # 设置默认值
        self.device_id = device_id or "unknown_centrifuge"
        self.config = config or {}

        self.logger = logging.getLogger(f"VirtualCentrifuge.{self.device_id}")
        self.data = {}

        # 从config或kwargs中获取配置参数
        self.port = self.config.get("port") or kwargs.get("port", "VIRTUAL")
        self._max_speed = self.config.get("max_speed") or kwargs.get("max_speed", 15000.0)
        self._max_temp = self.config.get("max_temp") or kwargs.get("max_temp", 40.0)
        self._min_temp = self.config.get("min_temp") or kwargs.get("min_temp", 4.0)

        # 处理其他kwargs参数
        skip_keys = {"port", "max_speed", "max_temp", "min_temp"}
        for key, value in kwargs.items():
            if key not in skip_keys and not hasattr(self, key):
                setattr(self, key, value)
    
    def post_init(self, ros_node: "DeviceNode"):
        self._ros_node = ros_node

    async def initialize(self) -> bool:
        """Initialize virtual centrifuge"""
        self.logger.info(f"Initializing virtual centrifuge {self.device_id}")
        
        # 只保留核心状态
        self.data.update({
            "status": "Idle",
            "centrifuge_state": "Stopped",  # Stopped, Running, Completed, Error
            "current_speed": 0.0,
            "target_speed": 0.0,
            "current_temp": 25.0,
            "target_temp": 25.0,
            "time_remaining": 0.0,
            "progress": 0.0,
            "message": "Ready for centrifugation"
        })
        return True

    async def cleanup(self) -> bool:
        """Cleanup virtual centrifuge"""
        self.logger.info(f"Cleaning up virtual centrifuge {self.device_id}")
        
        self.data.update({
            "status": "Offline",
            "centrifuge_state": "Offline",
            "current_speed": 0.0,
            "current_temp": 25.0,
            "message": "System offline"
        })
        return True

    async def centrifuge(
        self, 
        vessel: str, 
        speed: float, 
        time: float, 
        temp: float = 25.0
    ) -> bool:
        """Execute centrifuge action - 简化的离心流程"""
        self.logger.info(f"Centrifuge: vessel={vessel}, speed={speed} rpm, time={time}s, temp={temp}°C")

        # 验证参数
        if speed > self._max_speed or speed < 100.0:
            error_msg = f"离心速度 {speed} rpm 超出范围 (100-{self._max_speed} rpm)"
            self.logger.error(error_msg)
            self.data.update({
                "status": f"Error: {error_msg}",
                "centrifuge_state": "Error",
                "message": error_msg
            })
            return False

        if temp > self._max_temp or temp < self._min_temp:
            error_msg = f"温度 {temp}°C 超出范围 ({self._min_temp}-{self._max_temp}°C)"
            self.logger.error(error_msg)
            self.data.update({
                "status": f"Error: {error_msg}",
                "centrifuge_state": "Error",
                "message": error_msg
            })
            return False

        # 开始离心
        self.data.update({
            "status": f"离心中: {vessel}",
            "centrifuge_state": "Running",
            "current_speed": speed,
            "target_speed": speed,
            "current_temp": temp,
            "target_temp": temp,
            "time_remaining": time,
            "progress": 0.0,
            "message": f"Centrifuging {vessel} at {speed} rpm, {temp}°C"
        })

        try:
            # 离心过程 - 实时更新进度
            start_time = time_module.time()
            total_time = time
            
            while True:
                current_time = time_module.time()
                elapsed = current_time - start_time
                remaining = max(0, total_time - elapsed)
                progress = min(100.0, (elapsed / total_time) * 100)
                
                # 更新状态
                self.data.update({
                    "time_remaining": remaining,
                    "progress": progress,
                    "status": f"离心中: {vessel} | {speed} rpm | {temp}°C | {progress:.1f}% | 剩余: {remaining:.0f}s",
                    "message": f"Centrifuging: {progress:.1f}% complete, {remaining:.0f}s remaining"
                })
                
                # 时间到了，退出循环
                if remaining <= 0:
                    break
                
                # 每秒更新一次
                await self._ros_node.sleep(1.0)
            
            # 离心完成
            self.data.update({
                "status": f"离心完成: {vessel} | {speed} rpm | {time}s",
                "centrifuge_state": "Completed",
                "progress": 100.0,
                "time_remaining": 0.0,
                "current_speed": 0.0,  # 停止旋转
                "current_temp": 25.0,  # 恢复室温
                "message": f"Centrifugation completed: {vessel} at {speed} rpm for {time}s"
            })

            self.logger.info(f"Centrifugation completed: {vessel} at {speed} rpm for {time}s")
            return True

        except Exception as e:
            # 出错处理
            self.logger.error(f"Error during centrifugation: {str(e)}")
            
            self.data.update({
                "status": f"离心错误: {str(e)}",
                "centrifuge_state": "Error",
                "current_speed": 0.0,
                "current_temp": 25.0,
                "progress": 0.0,
                "time_remaining": 0.0,
                "message": f"Centrifugation failed: {str(e)}"
            })
            return False

    # === 核心状态属性 ===
    @property
    def status(self) -> str:
        return self.data.get("status", "Unknown")

    @property
    def centrifuge_state(self) -> str:
        return self.data.get("centrifuge_state", "Unknown")

    @property
    def current_speed(self) -> float:
        return self.data.get("current_speed", 0.0)

    @property
    def target_speed(self) -> float:
        return self.data.get("target_speed", 0.0)

    @property
    def current_temp(self) -> float:
        return self.data.get("current_temp", 25.0)

    @property
    def target_temp(self) -> float:
        return self.data.get("target_temp", 25.0)

    @property
    def max_speed(self) -> float:
        return self._max_speed

    @property
    def max_temp(self) -> float:
        return self._max_temp

    @property
    def min_temp(self) -> float:
        return self._min_temp

    @property
    def time_remaining(self) -> float:
        return self.data.get("time_remaining", 0.0)

    @property
    def progress(self) -> float:
        return self.data.get("progress", 0.0)

    @property
    def message(self) -> str:
        return self.data.get("message", "")
