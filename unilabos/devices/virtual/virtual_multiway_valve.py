import time
import logging
from typing import Union, Dict, Optional

from unilabos.registry.decorators import topic_config


class VirtualMultiwayValve:
    """
    虚拟九通阀门 - 0号位连接transfer pump，1-8号位连接其他设备 🔄
    """
    def __init__(self, port: str = "VIRTUAL", positions: int = 8, **kwargs):
        self.port = port
        self.max_positions = positions  # 1-8号位
        self.total_positions = positions + 1  # 0-8号位，共9个位置
        
        # 添加日志记录器
        self.logger = logging.getLogger(f"VirtualMultiwayValve.{port}")
        
        # 状态属性
        self._status = "Idle"
        self._valve_state = "Ready"
        self._current_position = 0  # 默认在0号位（transfer pump位置）
        self._target_position = 0
        
        print(f"🔄 === 虚拟多通阀门已创建 === ✨")
        print(f"🎯 端口: {port} | 📊 位置范围: 0-{self.max_positions} | 🏠 初始位置: 0 (transfer_pump)")
        self.logger.info(f"🔧 多通阀门初始化: 端口={port}, 最大位置={self.max_positions}")

    @property
    def status(self) -> str:
        return self._status

    @property
    def valve_state(self) -> str:
        return self._valve_state

    @property
    def current_position(self) -> int:
        return self._current_position

    @property
    def target_position(self) -> int:
        return self._target_position

    @property
    @topic_config()
    def current_port(self) -> str:
        """当前连接的端口名称 🔌"""
        return self.port

    def set_position(self, command: Union[int, str]):
        """
        设置阀门位置 - 支持0-8位置 🎯
        
        Args:
            command: 目标位置 (0-8) 或位置字符串
                    0: transfer pump位置
                    1-8: 其他设备位置
                    'default': 默认位置（0号位）
        """
        try:
            # 🔧 处理特殊字符串命令
            if isinstance(command, str):
                command_lower = command.lower().strip()
                
                # 处理特殊命令
                if command_lower in ['default', 'pump', 'transfer_pump', 'home']:
                    pos = 0  # 默认位置为0号位（transfer pump）
                    self.logger.info(f"🔧 特殊命令 '{command}' 映射到位置 {pos}")
                elif command_lower in ['open']:
                    pos = 0  # open命令也映射到0号位
                    self.logger.info(f"🔧 OPEN命令映射到位置 {pos}")
                elif command_lower in ['close', 'closed']:
                    # 关闭命令保持当前位置
                    pos = self._current_position
                    self.logger.info(f"🔧 CLOSE命令保持当前位置 {pos}")
                else:
                    # 尝试转换为数字
                    try:
                        pos = int(command)
                    except ValueError:
                        error_msg = f"无法识别的命令: '{command}'"
                        self.logger.error(f"❌ {error_msg}")
                        raise ValueError(error_msg)
            else:
                pos = int(command)
                
            if pos < 0 or pos > self.max_positions:
                error_msg = f"位置必须在 0-{self.max_positions} 范围内"
                self.logger.error(f"❌ {error_msg}: 请求位置={pos}")
                raise ValueError(error_msg)
            
            # 获取位置描述emoji
            if pos == 0:
                pos_emoji = "🚰"
                pos_desc = "泵位置"
            else:
                pos_emoji = "🔌"
                pos_desc = f"端口{pos}"
            
            old_position = self._current_position
            old_port = self.get_current_port()
            
            self.logger.info(f"🔄 阀门切换: {old_position}({old_port}) → {pos} {pos_emoji}")
            
            self._status = "Busy"
            self._valve_state = "Moving"
            self._target_position = pos
            
            # 模拟阀门切换时间
            switch_time = abs(self._current_position - pos) * 0.5  # 每个位置0.5秒

            if switch_time > 0:
                self.logger.info(f"⏱️ 阀门移动中... 预计用时: {switch_time:.1f}秒 🔄")
                time.sleep(switch_time)
            
            self._current_position = pos
            self._status = "Idle"
            self._valve_state = "Ready"
            
            current_port = self.get_current_port()
            success_msg = f"✅ 阀门已切换到位置 {pos} ({current_port}) {pos_emoji}"
            
            self.logger.info(success_msg)
            return success_msg
            
        except ValueError as e:
            error_msg = f"❌ 阀门切换失败: {str(e)}"
            self._status = "Error"
            self._valve_state = "Error"
            self.logger.error(error_msg)
            return error_msg

    def set_to_pump_position(self):
        """切换到transfer pump位置（0号位）🚰"""
        self.logger.info(f"🚰 切换到泵位置...")
        return self.set_position(0)

    def set_to_port(self, port_number: int):
        """
        切换到指定端口位置 🔌
        
        Args:
            port_number: 端口号 (1-8)
        """
        if port_number < 1 or port_number > self.max_positions:
            error_msg = f"端口号必须在 1-{self.max_positions} 范围内"
            self.logger.error(f"❌ {error_msg}: 请求端口={port_number}")
            raise ValueError(error_msg)
        
        self.logger.info(f"🔌 切换到端口 {port_number}...")
        return self.set_position(port_number)

    def open(self):
        """打开阀门 - 设置到transfer pump位置（0号位）🔓"""
        self.logger.info(f"🔓 打开阀门，设置到泵位置...")
        return self.set_to_pump_position()

    def close(self):
        """关闭阀门 - 对于多通阀门，设置到一个"关闭"状态 🔒"""
        self.logger.info(f"🔒 关闭阀门...")
        
        self._status = "Busy"
        self._valve_state = "Closing"
        time.sleep(0.5)

        # 可以选择保持当前位置或设置特殊关闭状态
        self._status = "Idle"
        self._valve_state = "Closed"
        
        close_msg = f"🔒 阀门已关闭，保持在位置 {self._current_position} ({self.port})"
        self.logger.info(close_msg)
        return close_msg

    @property
    @topic_config()
    def valve_position(self) -> int:
        """阀门位置 📍"""
        return self._current_position

    def set_valve_position(self, command: Union[int, str]):
        """
        设置阀门位置 - 兼容pump_protocol调用 🎯
        这是set_position的别名方法，用于兼容pump_protocol.py

        Args:
            command: 目标位置 (0-8) 或位置字符串
        """
        # 删除debug日志：self.logger.debug(f"🎯 兼容性调用: set_valve_position({command})")
        return self.set_position(command)

    def is_at_position(self, position: int) -> bool:
        """检查是否在指定位置 🎯"""
        result = self._current_position == position
        # 删除debug日志：self.logger.debug(f"🎯 位置检查: 当前={self._current_position}, 目标={position}, 匹配={result}")
        return result

    def is_at_pump_position(self) -> bool:
        """检查是否在transfer pump位置 🚰"""
        result = self._current_position == 0
        # 删除debug日志：pump_status = "是" if result else "否"
        # 删除debug日志：self.logger.debug(f"🚰 泵位置检查: {pump_status} (当前位置: {self._current_position})")
        return result

    def is_at_port(self, port_number: int) -> bool:
        """检查是否在指定端口位置 🔌"""
        result = self._current_position == port_number
        # 删除debug日志：port_status = "是" if result else "否"
        # 删除debug日志：self.logger.debug(f"🔌 端口{port_number}检查: {port_status} (当前位置: {self._current_position})")
        return result

    def reset(self):
        """重置阀门到transfer pump位置（0号位）🔄"""
        self.logger.info(f"🔄 重置阀门到泵位置...")
        return self.set_position(0)

    def switch_between_pump_and_port(self, port_number: int):
        """
        在transfer pump位置和指定端口之间切换 🔄
        
        Args:
            port_number: 目标端口号 (1-8)
        """
        if self._current_position == 0:
            # 当前在pump位置，切换到指定端口
            self.logger.info(f"🔄 从泵位置切换到端口 {port_number}...")
            return self.set_to_port(port_number)
        else:
            # 当前在某个端口，切换到pump位置
            self.logger.info(f"🔄 从端口 {self._current_position} 切换到泵位置...")
            return self.set_to_pump_position()

    @property
    @topic_config()
    def flow_path(self) -> str:
        """当前流路路径描述 🌊"""
        if self._current_position == 0:
            return f"🚰 转移泵已连接 (位置 {self._current_position})"
        return f"🔌 端口 {self._current_position} 已连接 ({self.current_port})"

    def __str__(self):
        current_port = self.current_port
        status_emoji = "✅" if self._status == "Idle" else "🔄" if self._status == "Busy" else "❌"
        
        return f"🔄 VirtualMultiwayValve({status_emoji} 位置: {self._current_position}/{self.max_positions}, 端口: {current_port}, 状态: {self._status})"


# 使用示例
if __name__ == "__main__":
    valve = VirtualMultiwayValve()
    
    print("🔄 === 虚拟九通阀门测试 === ✨")
    print(f"🏠 初始状态: {valve}")
    print(f"🌊 当前流路: {valve.flow_path}")
    
    # 切换到试剂瓶1（1号位）
    print(f"\n🔌 切换到1号位: {valve.set_position(1)}")
    print(f"📍 当前状态: {valve}")
    
    # 切换到transfer pump位置（0号位）
    print(f"\n🚰 切换到pump位置: {valve.set_to_pump_position()}")
    print(f"📍 当前状态: {valve}")
    
    # 切换到试剂瓶2（2号位）
    print(f"\n🔌 切换到2号位: {valve.set_to_port(2)}")
    print(f"📍 当前状态: {valve}")
    
    # 测试切换功能
    print(f"\n🔄 智能切换测试:")
    print(f"当前位置: {valve._current_position}")
    print(f"切换结果: {valve.switch_between_pump_and_port(3)}")
    print(f"新位置: {valve._current_position}")
    
    # 重置测试
    print(f"\n🔄 重置测试: {valve.reset()}")
    print(f"📍 重置后状态: {valve}")