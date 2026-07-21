import asyncio
import logging
import time as time_module
from typing import Dict, Any, TYPE_CHECKING

from unilabos.registry.decorators import topic_config
if TYPE_CHECKING:
    from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode

class VirtualStirrer:
    """Virtual stirrer device for StirProtocol testing - 功能完整版 🌪️"""
    
    _ros_node: "BaseROS2DeviceNode"
    
    def __init__(self, device_id: str = None, config: Dict[str, Any] = None, **kwargs):
        # 处理可能的不同调用方式
        if device_id is None and 'id' in kwargs:
            device_id = kwargs.pop('id')
        if config is None and 'config' in kwargs:
            config = kwargs.pop('config')
        
        # 设置默认值
        self.device_id = device_id or "unknown_stirrer"
        self.config = config or {}
        
        self.logger = logging.getLogger(f"VirtualStirrer.{self.device_id}")
        self.data = {}
        
        # 从config或kwargs中获取配置参数
        self.port = self.config.get('port') or kwargs.get('port', 'VIRTUAL')
        self._max_speed = self.config.get('max_speed') or kwargs.get('max_speed', 1500.0)
        self._min_speed = self.config.get('min_speed') or kwargs.get('min_speed', 50.0)
        
        # 处理其他kwargs参数
        skip_keys = {'port', 'max_speed', 'min_speed'}
        for key, value in kwargs.items():
            if key not in skip_keys and not hasattr(self, key):
                setattr(self, key, value)
        
        print(f"🌪️ === 虚拟搅拌器 {self.device_id} 已创建 === ✨")
        print(f"🔧 速度范围: {self._min_speed} ~ {self._max_speed} RPM | 📱 端口: {self.port}")
    
    def post_init(self, ros_node: "BaseROS2DeviceNode"):
        self._ros_node = ros_node
    
    async def initialize(self) -> bool:
        """Initialize virtual stirrer 🚀"""
        self.logger.info(f"🔧 初始化虚拟搅拌器 {self.device_id} ✨")
        
        # 初始化状态信息
        self.data.update({
            "status": "🏠 待机中",
            "operation_mode": "Idle",          # 操作模式: Idle, Stirring, Settling, Completed, Error
            "current_vessel": "",              # 当前搅拌的容器
            "current_speed": 0.0,              # 当前搅拌速度
            "is_stirring": False,              # 是否正在搅拌
            "remaining_time": 0.0,             # 剩余时间
        })
        
        self.logger.info(f"✅ 搅拌器 {self.device_id} 初始化完成 🌪️")
        self.logger.info(f"📊 设备规格: 速度范围 {self._min_speed} ~ {self._max_speed} RPM")
        return True
    
    async def cleanup(self) -> bool:
        """Cleanup virtual stirrer 🧹"""
        self.logger.info(f"🧹 清理虚拟搅拌器 {self.device_id} 🔚")
        
        self.data.update({
            "status": "💤 离线",
            "operation_mode": "Offline",
            "current_vessel": "",
            "current_speed": 0.0,
            "is_stirring": False,
            "remaining_time": 0.0,
        })
        
        self.logger.info(f"✅ 搅拌器 {self.device_id} 清理完成 💤")
        return True
    
    async def stir(self, stir_time: float, stir_speed: float, settling_time: float, **kwargs) -> bool:
        """Execute stir action - 定时搅拌 + 沉降 🌪️"""
        
        # 🔧 类型转换 - 确保所有参数都是数字类型
        try:
            stir_time = float(stir_time)
            stir_speed = float(stir_speed)
            settling_time = float(settling_time)
        except (ValueError, TypeError) as e:
            error_msg = f"参数类型转换失败: stir_time={stir_time}, stir_speed={stir_speed}, settling_time={settling_time}, error={e}"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: {error_msg}",
                "operation_mode": "Error"
            })
            return False
        
        self.logger.info(f"🌪️ 开始搅拌操作: 速度 {stir_speed} RPM | 时间 {stir_time}s | 沉降 {settling_time}s")
        
        # 验证参数
        if stir_speed > self._max_speed or stir_speed < self._min_speed:
            error_msg = f"🌪️ 搅拌速度 {stir_speed} RPM 超出范围 ({self._min_speed} - {self._max_speed} RPM) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: 速度超出范围",
                "operation_mode": "Error"
            })
            return False
        
        # === 第一阶段：搅拌 ===
        start_time = time_module.time()
        total_stir_time = stir_time
        
        self.logger.info(f"🚀 开始搅拌阶段: {stir_speed} RPM × {total_stir_time}s ⏱️")
        
        self.data.update({
            "status": f"🌪️ 搅拌中: {stir_speed} RPM | ⏰ 剩余: {total_stir_time:.0f}s",
            "operation_mode": "Stirring",
            "current_speed": stir_speed,
            "is_stirring": True,
            "remaining_time": total_stir_time,
        })
        
        # 搅拌过程 - 实时更新剩余时间
        last_logged_time = 0
        while True:
            current_time = time_module.time()
            elapsed = current_time - start_time
            remaining = max(0, total_stir_time - elapsed)
            progress = (elapsed / total_stir_time) * 100 if total_stir_time > 0 else 100
            
            # 更新状态
            self.data.update({
                "remaining_time": remaining,
                "status": f"🌪️ 搅拌中: {stir_speed} RPM | ⏰ 剩余: {remaining:.0f}s"
            })
            
            # 进度日志（每25%打印一次）
            if progress >= 25 and int(progress) % 25 == 0 and int(progress) != last_logged_time:
                self.logger.info(f"📊 搅拌进度: {progress:.0f}% | 🌪️ {stir_speed} RPM | ⏰ 剩余: {remaining:.0f}s ✨")
                last_logged_time = int(progress)
            
            # 搅拌时间到了
            if remaining <= 0:
                break
            
            await self._ros_node.sleep(1.0)
        
        self.logger.info(f"✅ 搅拌阶段完成! 🌪️ {stir_speed} RPM × {stir_time}s")
        
        # === 第二阶段：沉降（如果需要）===
        if settling_time > 0:
            start_settling_time = time_module.time()
            total_settling_time = settling_time
            
            self.logger.info(f"🛑 开始沉降阶段: 停止搅拌 × {total_settling_time}s ⏱️")
            
            self.data.update({
                "status": f"🛑 沉降中: 停止搅拌 | ⏰ 剩余: {total_settling_time:.0f}s",
                "operation_mode": "Settling",
                "current_speed": 0.0,
                "is_stirring": False,
                "remaining_time": total_settling_time,
            })
            
            # 沉降过程 - 实时更新剩余时间
            last_logged_settling = 0
            while True:
                current_time = time_module.time()
                elapsed = current_time - start_settling_time
                remaining = max(0, total_settling_time - elapsed)
                progress = (elapsed / total_settling_time) * 100 if total_settling_time > 0 else 100
                
                # 更新状态
                self.data.update({
                    "remaining_time": remaining,
                    "status": f"🛑 沉降中: 停止搅拌 | ⏰ 剩余: {remaining:.0f}s"
                })
                
                # 进度日志（每25%打印一次）
                if progress >= 25 and int(progress) % 25 == 0 and int(progress) != last_logged_settling:
                    self.logger.info(f"📊 沉降进度: {progress:.0f}% | 🛑 静置中 | ⏰ 剩余: {remaining:.0f}s ✨")
                    last_logged_settling = int(progress)
                
                # 沉降时间到了
                if remaining <= 0:
                    break
                
                await self._ros_node.sleep(1.0)
            
            self.logger.info(f"✅ 沉降阶段完成! 🛑 静置 {settling_time}s")
        
        # === 操作完成 ===
        settling_info = f" | 🛑 沉降: {settling_time:.0f}s" if settling_time > 0 else ""
        
        self.data.update({
            "status": f"✅ 完成: 🌪️ 搅拌 {stir_speed} RPM × {stir_time:.0f}s{settling_info}",
            "operation_mode": "Completed",
            "current_speed": 0.0,
            "is_stirring": False,
            "remaining_time": 0.0,
        })
        
        self.logger.info(f"🎉 搅拌操作完成! ✨")
        self.logger.info(f"📊 操作总结:")
        self.logger.info(f"  🌪️ 搅拌: {stir_speed} RPM × {stir_time}s")
        if settling_time > 0:
            self.logger.info(f"  🛑 沉降: {settling_time}s")
        self.logger.info(f"  ⏱️ 总用时: {(stir_time + settling_time):.0f}s 🏁")
        
        return True
    
    async def start_stir(self, vessel: str, stir_speed: float, purpose: str = "") -> bool:
        """Start stir action - 开始持续搅拌 🔄"""
        
        # 🔧 类型转换
        try:
            stir_speed = float(stir_speed)
            vessel = str(vessel)
            purpose = str(purpose)
        except (ValueError, TypeError) as e:
            error_msg = f"参数类型转换错误: {str(e)}"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: {error_msg}",
                "operation_mode": "Error"
            })
            return False
        
        self.logger.info(f"🔄 启动持续搅拌: {vessel} | 🌪️ {stir_speed} RPM")
        if purpose:
            self.logger.info(f"📝 搅拌目的: {purpose}")
        
        # 验证参数
        if stir_speed > self._max_speed or stir_speed < self._min_speed:
            error_msg = f"🌪️ 搅拌速度 {stir_speed} RPM 超出范围 ({self._min_speed} - {self._max_speed} RPM) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: 速度超出范围",
                "operation_mode": "Error"
            })
            return False
        
        purpose_info = f" | 📝 {purpose}" if purpose else ""
        
        self.data.update({
            "status": f"🔄 启动: 持续搅拌 {vessel} | 🌪️ {stir_speed} RPM{purpose_info}",
            "operation_mode": "Stirring",
            "current_vessel": vessel,
            "current_speed": stir_speed,
            "is_stirring": True,
            "remaining_time": -1.0,  # -1 表示持续运行
        })
        
        self.logger.info(f"✅ 持续搅拌已启动! 🌪️ {stir_speed} RPM × ♾️ 🚀")
        return True
    
    async def stop_stir(self, vessel: str) -> bool:
        """Stop stir action - 停止搅拌 🛑"""
        
        # 🔧 类型转换
        try:
            vessel = str(vessel)
        except (ValueError, TypeError) as e:
            error_msg = f"参数类型转换错误: {str(e)}"
            self.logger.error(f"❌ {error_msg}")
            return False
        
        current_speed = self.data.get("current_speed", 0.0)
        
        self.logger.info(f"🛑 停止搅拌: {vessel}")
        if current_speed > 0:
            self.logger.info(f"🌪️ 之前搅拌速度: {current_speed} RPM")
        
        self.data.update({
            "status": f"🛑 已停止: {vessel} 搅拌停止 | 之前速度: {current_speed} RPM",
            "operation_mode": "Stopped",
            "current_vessel": "",
            "current_speed": 0.0,
            "is_stirring": False,
            "remaining_time": 0.0,
        })
        
        self.logger.info(f"✅ 搅拌器已停止 {vessel} 的搅拌操作 🏁")
        return True
    
    # 状态属性
    @property
    def status(self) -> str:
        return self.data.get("status", "🏠 待机中")
    
    @property
    def operation_mode(self) -> str:
        return self.data.get("operation_mode", "Idle")
    
    @property
    def current_vessel(self) -> str:
        return self.data.get("current_vessel", "")
    
    @property
    def current_speed(self) -> float:
        return self.data.get("current_speed", 0.0)
    
    @property
    def is_stirring(self) -> bool:
        return self.data.get("is_stirring", False)
    
    @property
    def remaining_time(self) -> float:
        return self.data.get("remaining_time", 0.0)
    
    @property
    def max_speed(self) -> float:
        return self._max_speed
    
    @property
    def min_speed(self) -> float:
        return self._min_speed
    
    @property
    @topic_config()
    def device_info(self) -> Dict[str, Any]:
        """设备状态快照信息 📊"""
        return {
            "device_id": self.device_id,
            "status": self.status,
            "operation_mode": self.operation_mode,
            "current_vessel": self.current_vessel,
            "current_speed": self.current_speed,
            "is_stirring": self.is_stirring,
            "remaining_time": self.remaining_time,
            "max_speed": self._max_speed,
            "min_speed": self._min_speed,
        }

    def __str__(self):
        status_emoji = "✅" if self.operation_mode == "Idle" else "🌪️" if self.operation_mode == "Stirring" else "🛑" if self.operation_mode == "Settling" else "❌"
        return f"🌪️ VirtualStirrer({status_emoji} {self.device_id}: {self.operation_mode}, {self.current_speed} RPM)"