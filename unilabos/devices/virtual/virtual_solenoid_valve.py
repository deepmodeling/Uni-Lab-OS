import time
import asyncio
from typing import Union, TYPE_CHECKING

if TYPE_CHECKING:
    from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class VirtualSolenoidValve:
    """
    虚拟电磁阀门 - 简单的开关型阀门，只有开启和关闭两个状态
    """
    
    _ros_node: "BaseROS2DeviceNode"
    
    def __init__(self, device_id: str = None, config: dict = None, **kwargs):
        # 从配置中获取参数，提供默认值
        if config is None:
            config = {}
        
        self.device_id = device_id
        self.port = config.get("port", "VIRTUAL")
        self.voltage = config.get("voltage", 12.0)
        self.response_time = config.get("response_time", 0.1)
        
        # 状态属性
        self._status = "Idle"
        self._valve_state = "Closed"  # "Open" or "Closed"
        self._is_open = False
    
    def post_init(self, ros_node: "BaseROS2DeviceNode"):
        self._ros_node = ros_node

    async def initialize(self) -> bool:
        """初始化设备"""
        self._status = "Idle"
        return True

    async def cleanup(self) -> bool:
        """清理资源"""
        return True

    @property
    def status(self) -> str:
        return self._status

    @property
    def valve_state(self) -> str:
        return self._valve_state

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def valve_position(self) -> str:
        """获取阀门位置状态"""
        return "OPEN" if self._is_open else "CLOSED"

    async def set_valve_position(self, command: str = None, **kwargs):
        """
        设置阀门位置 - ROS动作接口
        
        Args:
            command: "OPEN"/"CLOSED" 或其他控制命令
        """
        if command is None:
            return {"success": False, "message": "Missing command parameter"}
        
        print(f"SOLENOID_VALVE: {self.device_id} 接收到命令: {command}")
        
        self._status = "Busy"
        
        # 模拟阀门响应时间
        await self._ros_node.sleep(self.response_time)
        
        # 处理不同的命令格式
        if isinstance(command, str):
            cmd_upper = command.upper()
            if cmd_upper in ["OPEN", "ON", "TRUE", "1"]:
                self._is_open = True
                self._valve_state = "Open"
                result_msg = f"Valve {self.device_id} opened"
            elif cmd_upper in ["CLOSED", "CLOSE", "OFF", "FALSE", "0"]:
                self._is_open = False
                self._valve_state = "Closed"
                result_msg = f"Valve {self.device_id} closed"
            else:
                # 可能是端口名称，处理路径设置
                # 对于简单电磁阀，任何非关闭命令都视为开启
                self._is_open = True
                self._valve_state = "Open"
                result_msg = f"Valve {self.device_id} set to position: {command}"
        else:
            self._status = "Error"
            return {"success": False, "message": "Invalid command type"}
        
        self._status = "Idle"
        print(f"SOLENOID_VALVE: {result_msg}")
        
        return {
            "success": True, 
            "message": result_msg,
            "valve_position": self.valve_position
        }

    async def open(self, **kwargs):
        """打开电磁阀 - ROS动作接口"""
        return await self.set_valve_position(command="OPEN")

    async def close(self, **kwargs):
        """关闭电磁阀 - ROS动作接口"""
        return await self.set_valve_position(command="CLOSED")

    async def set_status(self, string: str = None, **kwargs):
        """
        设置阀门状态 - 兼容 StrSingleInput 类型
    
        Args:
            string: "ON"/"OFF" 或 "OPEN"/"CLOSED"
        """
        if string is None:
            return {"success": False, "message": "Missing string parameter"}
        
        # 将 string 参数转换为 command 参数
        if string.upper() in ["ON", "OPEN"]:
            command = "OPEN"
        elif string.upper() in ["OFF", "CLOSED"]:
            command = "CLOSED"
        else:
            command = string
        
        return await self.set_valve_position(command=command)

    def toggle(self):
        """切换阀门状态"""
        if self._is_open:
            return self.close()
        else:
            return self.open()

    def is_closed(self) -> bool:
        """检查阀门是否关闭"""
        return not self._is_open

    async def reset(self):
        """重置阀门到关闭状态"""
        return await self.close()