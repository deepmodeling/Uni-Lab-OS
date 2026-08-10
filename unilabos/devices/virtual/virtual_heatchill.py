import asyncio
import logging
import time as time_module  # 重命名time模块，避免与参数冲突
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from unilabos.device_runtime.node import DeviceNode

class VirtualHeatChill:
    """Virtual heat chill device for HeatChillProtocol testing 🌡️"""
    
    _ros_node: "DeviceNode"
    
    def __init__(self, device_id: str = None, config: Dict[str, Any] = None, **kwargs):
        # 处理可能的不同调用方式
        if device_id is None and 'id' in kwargs:
            device_id = kwargs.pop('id')
        if config is None and 'config' in kwargs:
            config = kwargs.pop('config')
        
        # 设置默认值
        self.device_id = device_id or "unknown_heatchill"
        self.config = config or {}
        
        self.logger = logging.getLogger(f"VirtualHeatChill.{self.device_id}")
        self.data = {}
        
        # 从config或kwargs中获取配置参数
        self.port = self.config.get('port') or kwargs.get('port', 'VIRTUAL')
        self._max_temp = self.config.get('max_temp') or kwargs.get('max_temp', 200.0)
        self._min_temp = self.config.get('min_temp') or kwargs.get('min_temp', -80.0)
        self._max_stir_speed = self.config.get('max_stir_speed') or kwargs.get('max_stir_speed', 1000.0)
        
        # 处理其他kwargs参数
        skip_keys = {'port', 'max_temp', 'min_temp', 'max_stir_speed'}
        for key, value in kwargs.items():
            if key not in skip_keys and not hasattr(self, key):
                setattr(self, key, value)
        
        print(f"🌡️ === 虚拟温控设备 {self.device_id} 已创建 === ✨")
        print(f"🔥 温度范围: {self._min_temp}°C ~ {self._max_temp}°C | 🌪️ 最大搅拌: {self._max_stir_speed} RPM")
    
    def post_init(self, ros_node: "DeviceNode"):
        self._ros_node = ros_node
    
    async def initialize(self) -> bool:
        """Initialize virtual heat chill 🚀"""
        self.logger.info(f"🔧 初始化虚拟温控设备 {self.device_id} ✨")
        
        # 初始化状态信息
        self.data.update({
            "status": "🏠 待机中",
            "operation_mode": "Idle",
            "is_stirring": False,
            "stir_speed": 0.0,
            "remaining_time": 0.0,
        })
        
        self.logger.info(f"✅ 温控设备 {self.device_id} 初始化完成 🌡️")
        self.logger.info(f"📊 设备规格: 温度范围 {self._min_temp}°C ~ {self._max_temp}°C | 搅拌范围 0 ~ {self._max_stir_speed} RPM")
        return True
    
    async def cleanup(self) -> bool:
        """Cleanup virtual heat chill 🧹"""
        self.logger.info(f"🧹 清理虚拟温控设备 {self.device_id} 🔚")
        
        self.data.update({
            "status": "💤 离线",
            "operation_mode": "Offline",
            "is_stirring": False,
            "stir_speed": 0.0,
            "remaining_time": 0.0
        })
        
        self.logger.info(f"✅ 温控设备 {self.device_id} 清理完成 💤")
        return True
    
    async def heat_chill(self, temp: float, time, stir: bool,
                        stir_speed: float, purpose: str, vessel: dict = {}) -> bool:
        """Execute heat chill action - 🔧 修复：确保参数类型正确"""
        
        # 🔧 关键修复：确保所有参数类型正确
        try:
            temp = float(temp)
            time_value = float(time)  # 强制转换为浮点数
            stir_speed = float(stir_speed)
            stir = bool(stir)
            purpose = str(purpose)
        except (ValueError, TypeError) as e:
            error_msg = f"参数类型转换错误: temp={temp}({type(temp)}), time={time}({type(time)}), error={str(e)}"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: {error_msg}",
                "operation_mode": "Error"
            })
            return False
        
        # 确定温度操作emoji
        if temp > 25.0:
            temp_emoji = "🔥"
            operation_mode = "Heating"
            status_action = "加热"
        elif temp < 25.0:
            temp_emoji = "❄️"
            operation_mode = "Cooling"
            status_action = "冷却"
        else:
            temp_emoji = "🌡️"
            operation_mode = "Maintaining"
            status_action = "保温"
        
        self.logger.info(f"🌡️ 开始温控操作: {temp}°C {temp_emoji}")
        self.logger.info(f"  🎯 目标温度: {temp}°C {temp_emoji}")
        self.logger.info(f"  ⏰ 持续时间: {time_value}s")
        self.logger.info(f"  🌪️ 搅拌: {stir} ({stir_speed} RPM)")
        self.logger.info(f"  📝 目的: {purpose}")
        
        # 验证参数范围
        if temp > self._max_temp or temp < self._min_temp:
            error_msg = f"🌡️ 温度 {temp}°C 超出范围 ({self._min_temp}°C - {self._max_temp}°C) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: 温度超出范围 ⚠️",
                "operation_mode": "Error"
            })
            return False
        
        if stir and stir_speed > self._max_stir_speed:
            error_msg = f"🌪️ 搅拌速度 {stir_speed} RPM 超出最大值 {self._max_stir_speed} RPM ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: 搅拌速度超出范围 ⚠️",
                "operation_mode": "Error"
            })
            return False
        
        if time_value <= 0:
            error_msg = f"⏰ 时间 {time_value}s 必须大于0 ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: 时间参数无效 ⚠️",
                "operation_mode": "Error"
            })
            return False
        
        # 🔧 修复：使用转换后的时间值
        start_time = time_module.time()
        total_time = time_value  # 使用转换后的浮点数
        
        self.logger.info(f"🚀 开始{status_action}程序! 预计用时 {total_time:.1f}秒 ⏱️")
        
        # 开始操作
        stir_info = f" | 🌪️ 搅拌: {stir_speed} RPM" if stir else ""
        
        self.data.update({
            "status": f"{temp_emoji} 运行中: {status_action} 至 {temp}°C | ⏰ 剩余: {total_time:.0f}s{stir_info}",
            "operation_mode": operation_mode,
            "is_stirring": stir,
            "stir_speed": stir_speed if stir else 0.0,
            "remaining_time": total_time,
        })
        
        # 在等待过程中每秒更新剩余时间
        last_logged_time = 0
        while True:
            current_time = time_module.time()
            elapsed = current_time - start_time
            remaining = max(0, total_time - elapsed)
            progress = (elapsed / total_time) * 100 if total_time > 0 else 100
            
            # 更新剩余时间和状态
            self.data.update({
                "remaining_time": remaining,
                "status": f"{temp_emoji} 运行中: {status_action} 至 {temp}°C | ⏰ 剩余: {remaining:.0f}s{stir_info}",
                "progress": progress
            })
            
            # 进度日志（每25%打印一次）
            if progress >= 25 and int(progress) % 25 == 0 and int(progress) != last_logged_time:
                self.logger.info(f"📊 {status_action}进度: {progress:.0f}% | ⏰ 剩余: {remaining:.0f}s | {temp_emoji} 目标: {temp}°C ✨")
                last_logged_time = int(progress)
            
            # 如果时间到了，退出循环
            if remaining <= 0:
                break
            
            # 等待1秒后再次检查
            await self._ros_node.sleep(1.0)
        
        # 操作完成
        final_stir_info = f" | 🌪️ 搅拌: {stir_speed} RPM" if stir else ""
        
        self.data.update({
            "status": f"✅ 完成: 已达到 {temp}°C {temp_emoji} | ⏱️ 用时: {total_time:.0f}s{final_stir_info}",
            "operation_mode": "Completed",
            "remaining_time": 0.0,
            "is_stirring": False,
            "stir_speed": 0.0,
            "progress": 100.0
        })
        
        self.logger.info(f"🎉 温控操作完成! ✨")
        self.logger.info(f"📊 操作结果:")
        self.logger.info(f"  🌡️ 达到温度: {temp}°C {temp_emoji}")
        self.logger.info(f"  ⏱️ 总用时: {total_time:.0f}s")
        if stir:
            self.logger.info(f"  🌪️ 搅拌速度: {stir_speed} RPM")
        self.logger.info(f"  📝 操作目的: {purpose} 🏁")
        
        return True
    
    async def heat_chill_start(self, temp: float, purpose: str, vessel: dict = {}) -> bool:
        """Start continuous heat chill 🔄"""
        
        # 🔧 添加类型转换
        try:
            temp = float(temp)
            purpose = str(purpose)
        except (ValueError, TypeError) as e:
            error_msg = f"参数类型转换错误: {str(e)}"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: {error_msg}",
                "operation_mode": "Error"
            })
            return False
        
        # 确定温度操作emoji
        if temp > 25.0:
            temp_emoji = "🔥"
            operation_mode = "Heating"
            status_action = "持续加热"
        elif temp < 25.0:
            temp_emoji = "❄️"
            operation_mode = "Cooling"
            status_action = "持续冷却"
        else:
            temp_emoji = "🌡️"
            operation_mode = "Maintaining"
            status_action = "恒温保持"
        
        self.logger.info(f"🔄 启动持续温控: {temp}°C {temp_emoji}")
        self.logger.info(f"  🎯 目标温度: {temp}°C {temp_emoji}")
        self.logger.info(f"  🔄 模式: {status_action}")
        self.logger.info(f"  📝 目的: {purpose}")
        
        # 验证参数
        if temp > self._max_temp or temp < self._min_temp:
            error_msg = f"🌡️ 温度 {temp}°C 超出范围 ({self._min_temp}°C - {self._max_temp}°C) ⚠️"
            self.logger.error(f"❌ {error_msg}")
            self.data.update({
                "status": f"❌ 错误: 温度超出范围 ⚠️",
                "operation_mode": "Error"
            })
            return False
        
        self.data.update({
            "status": f"🔄 启动: {status_action} 至 {temp}°C {temp_emoji} | ♾️ 持续运行",
            "operation_mode": operation_mode,
            "is_stirring": False,
            "stir_speed": 0.0,
            "remaining_time": -1.0,  # -1 表示持续运行
        })
        
        self.logger.info(f"✅ 持续温控已启动! {temp_emoji} {status_action}模式 🚀")
        return True
    
    async def heat_chill_stop(self, vessel: dict = {}) -> bool:
        """Stop heat chill 🛑"""
        
        self.logger.info(f"🛑 停止温控:")
        
        self.data.update({
            "status": f"🛑 {self.device_id} 温控停止",
            "operation_mode": "Stopped",
            "is_stirring": False,
            "stir_speed": 0.0,
            "remaining_time": 0.0,
        })
        
        self.logger.info(f"✅ 温控设备已停止 {self.device_id} 温度控制 🏁")
        return True
    
    # 状态属性
    @property
    def status(self) -> str:
        return self.data.get("status", "🏠 待机中")
    
    @property
    def operation_mode(self) -> str:
        return self.data.get("operation_mode", "Idle")
    
    @property
    def is_stirring(self) -> bool:
        return self.data.get("is_stirring", False)
    
    @property
    def stir_speed(self) -> float:
        return self.data.get("stir_speed", 0.0)
    
    @property
    def remaining_time(self) -> float:
        return self.data.get("remaining_time", 0.0)
    
    @property
    def progress(self) -> float:
        return self.data.get("progress", 0.0)
    
    @property
    def max_temp(self) -> float:
        return self._max_temp
    
    @property
    def min_temp(self) -> float:
        return self._min_temp
    
    @property
    def max_stir_speed(self) -> float:
        return self._max_stir_speed
