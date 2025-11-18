#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYZ三轴步进电机B系列驱动程序
支持RS485通信，Modbus协议
"""

import serial
import struct
import time
import logging
from typing import Optional, Tuple, Dict, Any
from enum import Enum
from dataclasses import dataclass

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MotorAxis(Enum):
    """电机轴枚举"""
    X = 1
    Y = 2
    Z = 3


class MotorStatus(Enum):
    """电机状态枚举"""
    STANDBY = 0x0000  # 待机/到位
    RUNNING = 0x0001  # 运行中
    COLLISION_STOP = 0x0002  # 碰撞停
    FORWARD_LIMIT_STOP = 0x0003  # 正光电停
    REVERSE_LIMIT_STOP = 0x0004  # 反光电停


class ModbusFunction(Enum):
    """Modbus功能码"""
    READ_HOLDING_REGISTERS = 0x03
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_REGISTERS = 0x10


@dataclass
class MotorPosition:
    """电机位置信息"""
    steps: int
    speed: int
    current: int
    status: MotorStatus


class ModbusException(Exception):
    """Modbus通信异常"""
    pass


class StepperMotorDriver:
    """步进电机驱动器基类"""
    
    # 寄存器地址常量
    REG_STATUS = 0x00
    REG_POSITION_HIGH = 0x01
    REG_POSITION_LOW = 0x02
    REG_ACTUAL_SPEED = 0x03
    REG_EMERGENCY_STOP = 0x04
    REG_CURRENT = 0x05
    REG_ENABLE = 0x06
    REG_PWM_OUTPUT = 0x07
    REG_ZERO_SINGLE = 0x0E
    REG_ZERO_COMMAND = 0x0F
    
    # 位置模式寄存器
    REG_TARGET_POSITION_HIGH = 0x10
    REG_TARGET_POSITION_LOW = 0x11
    REG_POSITION_SPEED = 0x13
    REG_POSITION_ACCELERATION = 0x14
    REG_POSITION_PRECISION = 0x15
    
    # 速度模式寄存器
    REG_SPEED_MODE_SPEED = 0x61
    REG_SPEED_MODE_ACCELERATION = 0x62
    
    # 设备参数寄存器
    REG_DEVICE_ADDRESS = 0xE0
    REG_DEFAULT_SPEED = 0xE7
    REG_DEFAULT_ACCELERATION = 0xE8
    
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        """
        初始化步进电机驱动器
        
        Args:
            port: 串口端口名
            baudrate: 波特率
            timeout: 通信超时时间
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial_conn: Optional[serial.Serial] = None
        
    def connect(self) -> bool:
        """
        建立串口连接
        
        Returns:
            连接是否成功
        """
        try:
            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout
            )
            logger.info(f"已连接到串口: {self.port}")
            return True
        except Exception as e:
            logger.error(f"串口连接失败: {e}")
            return False
    
    def disconnect(self) -> None:
        """关闭串口连接"""
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
            logger.info("串口连接已关闭")
    
    def __enter__(self):
        """上下文管理器入口"""
        if self.connect():
            return self
        raise ModbusException("无法建立串口连接")
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.disconnect()
    
    @staticmethod
    def calculate_crc(data: bytes) -> bytes:
        """
        计算Modbus CRC校验码
        
        Args:
            data: 待校验的数据
            
        Returns:
            CRC校验码 (2字节)
        """
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc >>= 1
                    crc ^= 0xA001
                else:
                    crc >>= 1
        return struct.pack('<H', crc)
    
    def _send_command(self, slave_addr: int, data: bytes) -> bytes:
        """
        发送Modbus命令并接收响应
        
        Args:
            slave_addr: 从站地址
            data: 命令数据
            
        Returns:
            响应数据
            
        Raises:
            ModbusException: 通信异常
        """
        if not self.serial_conn or not self.serial_conn.is_open:
            raise ModbusException("串口未连接")
        
        # 构建完整命令
        command = bytes([slave_addr]) + data
        crc = self.calculate_crc(command)
        full_command = command + crc
        
        # 清空接收缓冲区
        self.serial_conn.reset_input_buffer()
        
        # 发送命令
        self.serial_conn.write(full_command)
        logger.debug(f"发送命令: {' '.join(f'{b:02X}' for b in full_command)}")
        
        # 等待响应
        time.sleep(0.01)  # 短暂延时
        
        # 读取响应
        response = self.serial_conn.read(256)  # 最大读取256字节
        if not response:
            raise ModbusException("未收到响应")
        
        logger.debug(f"接收响应: {' '.join(f'{b:02X}' for b in response)}")
        
        # 验证CRC
        if len(response) < 3:
            raise ModbusException("响应数据长度不足")
        
        data_part = response[:-2]
        received_crc = response[-2:]
        calculated_crc = self.calculate_crc(data_part)
        
        if received_crc != calculated_crc:
            raise ModbusException(f"CRC校验失败{response}")
        
        return response
    
    def read_registers(self, slave_addr: int, start_addr: int, count: int) -> list:
        """
        读取保持寄存器
        
        Args:
            slave_addr: 从站地址
            start_addr: 起始地址
            count: 寄存器数量
            
        Returns:
            寄存器值列表
        """
        data = struct.pack('>BHH', ModbusFunction.READ_HOLDING_REGISTERS.value, start_addr, count)
        response = self._send_command(slave_addr, data)
        
        if len(response) < 5:
            raise ModbusException("响应长度不足")
        
        if response[1] != ModbusFunction.READ_HOLDING_REGISTERS.value:
            raise ModbusException(f"功能码错误: {response[1]:02X}")
        
        byte_count = response[2]
        values = []
        for i in range(0, byte_count, 2):
            value = struct.unpack('>H', response[3+i:5+i])[0]
            values.append(value)
        
        return values
    
    def write_single_register(self, slave_addr: int, addr: int, value: int) -> bool:
        """
        写入单个寄存器
        
        Args:
            slave_addr: 从站地址
            addr: 寄存器地址
            value: 寄存器值
            
        Returns:
            写入是否成功
        """
        data = struct.pack('>BHH', ModbusFunction.WRITE_SINGLE_REGISTER.value, addr, value)
        response = self._send_command(slave_addr, data)
        
        return len(response) >= 8 and response[1] == ModbusFunction.WRITE_SINGLE_REGISTER.value
    
    def write_multiple_registers(self, slave_addr: int, start_addr: int, values: list) -> bool:
        """
        写入多个寄存器
        
        Args:
            slave_addr: 从站地址
            start_addr: 起始地址
            values: 寄存器值列表
            
        Returns:
            写入是否成功
        """
        byte_count = len(values) * 2
        data = struct.pack('>BHHB', ModbusFunction.WRITE_MULTIPLE_REGISTERS.value, 
                          start_addr, len(values), byte_count)
        
        for value in values:
            data += struct.pack('>H', value)
        
        response = self._send_command(slave_addr, data)
        
        return len(response) >= 8 and response[1] == ModbusFunction.WRITE_MULTIPLE_REGISTERS.value


class XYZStepperController(StepperMotorDriver):
    """XYZ三轴步进电机控制器"""
    
    # 电机配置常量
    STEPS_PER_REVOLUTION = 16384  # 每圈步数
    
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        """
        初始化XYZ三轴步进电机控制器
        
        Args:
            port: 串口端口名
            baudrate: 波特率
            timeout: 通信超时时间
        """
        super().__init__(port, baudrate, timeout)
        self.axis_addresses = {
            MotorAxis.X: 1,
            MotorAxis.Y: 2,
            MotorAxis.Z: 3
        }
    
    def degrees_to_steps(self, degrees: float) -> int:
        """
        将角度转换为步数
        
        Args:
            degrees: 角度值
            
        Returns:
            对应的步数
        """
        return int(degrees * self.STEPS_PER_REVOLUTION / 360.0)
    
    def steps_to_degrees(self, steps: int) -> float:
        """
        将步数转换为角度
        
        Args:
            steps: 步数
            
        Returns:
            对应的角度值
        """
        return steps * 360.0 / self.STEPS_PER_REVOLUTION
    
    def revolutions_to_steps(self, revolutions: float) -> int:
        """
        将圈数转换为步数
        
        Args:
            revolutions: 圈数
            
        Returns:
            对应的步数
        """
        return int(revolutions * self.STEPS_PER_REVOLUTION)
    
    def steps_to_revolutions(self, steps: int) -> float:
        """
        将步数转换为圈数
        
        Args:
            steps: 步数
            
        Returns:
            对应的圈数
        """
        return steps / self.STEPS_PER_REVOLUTION
    
    def get_motor_status(self, axis: MotorAxis) -> MotorPosition:
        """
        获取电机状态信息
        
        Args:
            axis: 电机轴
            
        Returns:
            电机位置信息
        """
        addr = self.axis_addresses[axis]
        
        # 读取状态、位置、速度、电流
        values = self.read_registers(addr, self.REG_STATUS, 6)
        
        status = MotorStatus(values[0])
        position_high = values[1]
        position_low = values[2]
        speed = values[3]
        current = values[5]
        
        # 合并32位位置
        position = (position_high << 16) | position_low
        # 处理有符号数
        if position > 0x7FFFFFFF:
            position -= 0x100000000
        
        return MotorPosition(position, speed, current, status)
    
    def emergency_stop(self, axis: MotorAxis) -> bool:
        """
        紧急停止电机
        
        Args:
            axis: 电机轴
            
        Returns:
            操作是否成功
        """
        addr = self.axis_addresses[axis]
        return self.write_single_register(addr, self.REG_EMERGENCY_STOP, 0x0000)
    
    def enable_motor(self, axis: MotorAxis, enable: bool = True) -> bool:
        """
        使能/失能电机
        
        Args:
            axis: 电机轴
            enable: True为使能，False为失能
            
        Returns:
            操作是否成功
        """
        addr = self.axis_addresses[axis]
        value = 0x0001 if enable else 0x0000
        return self.write_single_register(addr, self.REG_ENABLE, value)
    
    def move_to_position(self, axis: MotorAxis, position: int, speed: int = 5000, 
                        acceleration: int = 1000, precision: int = 100) -> bool:
        """
        移动到指定位置
        
        Args:
            axis: 电机轴
            position: 目标位置(步数)
            speed: 运行速度(rpm)
            acceleration: 加速度(rpm/s)
            precision: 到位精度
            
        Returns:
            操作是否成功
        """
        addr = self.axis_addresses[axis]
        
        # 处理32位位置
        if position < 0:
            position += 0x100000000
        
        position_high = (position >> 16) & 0xFFFF
        position_low = position & 0xFFFF
        
        values = [
            position_high,     # 目标位置高位
            position_low,      # 目标位置低位
            0x0000,           # 保留
            speed,            # 速度
            acceleration,     # 加速度
            precision         # 精度
        ]
        
        return self.write_multiple_registers(addr, self.REG_TARGET_POSITION_HIGH, values)
    
    def set_speed_mode(self, axis: MotorAxis, speed: int, acceleration: int = 1000) -> bool:
        """
        设置速度模式运行
        
        Args:
            axis: 电机轴
            speed: 运行速度(rpm)，正值正转，负值反转
            acceleration: 加速度(rpm/s)
            
        Returns:
            操作是否成功
        """
        addr = self.axis_addresses[axis]
        
        # 处理负数
        if speed < 0:
            speed = 0x10000 + speed  # 补码表示
        
        values = [0x0000, speed, acceleration, 0x0000]
        
        return self.write_multiple_registers(addr, 0x60, values)
    
    def home_axis(self, axis: MotorAxis) -> bool:
        """
        轴归零操作
        
        Args:
            axis: 电机轴
            
        Returns:
            操作是否成功
        """
        addr = self.axis_addresses[axis]
        return self.write_single_register(addr, self.REG_ZERO_SINGLE, 0x0001)
    
    def wait_for_completion(self, axis: MotorAxis, timeout: float = 30.0) -> bool:
        """
        等待电机运动完成
        
        Args:
            axis: 电机轴
            timeout: 超时时间(秒)
            
        Returns:
            是否在超时前完成
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            status = self.get_motor_status(axis)
            if status.status == MotorStatus.STANDBY:
                return True
            time.sleep(0.1)
        
        logger.warning(f"{axis.name}轴运动超时")
        return False
    
    def move_xyz(self, x: Optional[int] = None, y: Optional[int] = None, z: Optional[int] = None,
                speed: int = 5000, acceleration: int = 1000) -> Dict[MotorAxis, bool]:
        """
        同时控制XYZ轴移动
        
        Args:
            x: X轴目标位置
            y: Y轴目标位置
            z: Z轴目标位置
            speed: 运行速度
            acceleration: 加速度
            
        Returns:
            各轴操作结果字典
        """
        results = {}
        
        if x is not None:
            results[MotorAxis.X] = self.move_to_position(MotorAxis.X, x, speed, acceleration)
        
        if y is not None:
            results[MotorAxis.Y] = self.move_to_position(MotorAxis.Y, y, speed, acceleration)
        
        if z is not None:
            results[MotorAxis.Z] = self.move_to_position(MotorAxis.Z, z, speed, acceleration)
        
        return results
    
    def move_xyz_degrees(self, x_deg: Optional[float] = None, y_deg: Optional[float] = None, 
                        z_deg: Optional[float] = None, speed: int = 5000, 
                        acceleration: int = 1000) -> Dict[MotorAxis, bool]:
        """
        使用角度值同时移动多个轴到指定位置
        
        Args:
            x_deg: X轴目标角度（度）
            y_deg: Y轴目标角度（度）
            z_deg: Z轴目标角度（度）
            speed: 移动速度
            acceleration: 加速度
            
        Returns:
            各轴移动操作结果
        """
        # 将角度转换为步数
        x_steps = self.degrees_to_steps(x_deg) if x_deg is not None else None
        y_steps = self.degrees_to_steps(y_deg) if y_deg is not None else None
        z_steps = self.degrees_to_steps(z_deg) if z_deg is not None else None
        
        return self.move_xyz(x_steps, y_steps, z_steps, speed, acceleration)
    
    def move_xyz_revolutions(self, x_rev: Optional[float] = None, y_rev: Optional[float] = None, 
                           z_rev: Optional[float] = None, speed: int = 5000, 
                           acceleration: int = 1000) -> Dict[MotorAxis, bool]:
        """
        使用圈数值同时移动多个轴到指定位置
        
        Args:
            x_rev: X轴目标圈数
            y_rev: Y轴目标圈数
            z_rev: Z轴目标圈数
            speed: 移动速度
            acceleration: 加速度
            
        Returns:
            各轴移动操作结果
        """
        # 将圈数转换为步数
        x_steps = self.revolutions_to_steps(x_rev) if x_rev is not None else None
        y_steps = self.revolutions_to_steps(y_rev) if y_rev is not None else None
        z_steps = self.revolutions_to_steps(z_rev) if z_rev is not None else None
        
        return self.move_xyz(x_steps, y_steps, z_steps, speed, acceleration)
    
    def move_to_position_degrees(self, axis: MotorAxis, degrees: float, speed: int = 5000, 
                               acceleration: int = 1000, precision: int = 100) -> bool:
        """
        使用角度值移动单个轴到指定位置
        
        Args:
            axis: 电机轴
            degrees: 目标角度（度）
            speed: 移动速度
            acceleration: 加速度
            precision: 精度
            
        Returns:
            移动操作是否成功
        """
        steps = self.degrees_to_steps(degrees)
        return self.move_to_position(axis, steps, speed, acceleration, precision)
    
    def move_to_position_revolutions(self, axis: MotorAxis, revolutions: float, speed: int = 5000, 
                                   acceleration: int = 1000, precision: int = 100) -> bool:
        """
        使用圈数值移动单个轴到指定位置
        
        Args:
            axis: 电机轴
            revolutions: 目标圈数
            speed: 移动速度
            acceleration: 加速度
            precision: 精度
            
        Returns:
            移动操作是否成功
        """
        steps = self.revolutions_to_steps(revolutions)
        return self.move_to_position(axis, steps, speed, acceleration, precision)
    
    def stop_all_axes(self) -> Dict[MotorAxis, bool]:
        """
        紧急停止所有轴
        
        Returns:
            各轴停止结果字典
        """
        results = {}
        for axis in MotorAxis:
            results[axis] = self.emergency_stop(axis)
        return results
    
    def enable_all_axes(self, enable: bool = True) -> Dict[MotorAxis, bool]:
        """
        使能/失能所有轴
        
        Args:
            enable: True为使能，False为失能
            
        Returns:
            各轴操作结果字典
        """
        results = {}
        for axis in MotorAxis:
            results[axis] = self.enable_motor(axis, enable)
        return results
    
    def get_all_positions(self) -> Dict[MotorAxis, MotorPosition]:
        """
        获取所有轴的位置信息
        
        Returns:
            各轴位置信息字典
        """
        positions = {}
        for axis in MotorAxis:
            positions[axis] = self.get_motor_status(axis)
        return positions
    
    def home_all_axes(self) -> Dict[MotorAxis, bool]:
        """
        所有轴归零
        
        Returns:
            各轴归零结果字典
        """
        results = {}
        for axis in MotorAxis:
            results[axis] = self.home_axis(axis)
        return results
