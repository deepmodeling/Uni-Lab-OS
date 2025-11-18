#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYZ三轴步进电机控制器
支持坐标系管理、限位开关回零、工作原点设定等功能

主要功能：
- 坐标系转换层（步数↔毫米）
- 限位开关回零功能
- 工作原点示教和保存
- 安全限位检查
- 运动控制接口

"""

import json
import os
from re import X
import time
from typing import Optional, Dict, Tuple, Union
from dataclasses import dataclass, field, asdict
from pathlib import Path
import logging

# 添加项目根目录到Python路径以解决模块导入问题
import sys
import os

# 无论如何都添加项目根目录到路径
current_file = os.path.abspath(__file__)
# 从 .../Uni-Lab-OS/unilabos/devices/LaiYu_Liquid/controllers/xyz_controller.py
# 向上5级到 .../Uni-Lab-OS
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_file)))))
# 强制添加项目根目录到sys.path的开头
sys.path.insert(0, project_root)

# 导入原有的驱动
from unilabos.devices.liquid_handling.laiyu.drivers.xyz_stepper_driver import XYZStepperController, MotorAxis, MotorStatus

logger = logging.getLogger(__name__)


@dataclass
class MachineConfig:
    """机械配置参数"""
    # 步距配置 (基于16384步/圈的步进电机)
    steps_per_mm_x: float = 204.8    # X轴步距 (16384步/圈 ÷ 80mm导程)
    steps_per_mm_y: float = 204.8    # Y轴步距 (16384步/圈 ÷ 80mm导程)  
    steps_per_mm_z: float = 3276.8   # Z轴步距 (16384步/圈 ÷ 5mm导程)
    
    # 行程限制
    max_travel_x: float = 340.0     # X轴最大行程
    max_travel_y: float = 250.0     # Y轴最大行程
    max_travel_z: float = 200.0     # Z轴最大行程
    reference_distance: Dict[str, float] = field(default_factory=lambda: {
        "x": 29,
        "y": -13,
        "z": -75.5
    })
    # 安全移动参数
    safe_z_height: float = 0.0      # Z轴安全移动高度 (mm) - 液体处理工作站安全高度
    z_approach_height: float = 5.0  # Z轴接近高度 (mm) - 在目标位置上方的预备高度
    
    # 回零参数
    homing_speed: int = 50          # 回零速度 (mm/s)
    homing_timeout: float = 30.0    # 回零超时时间
    safe_clearance: float = 10.0     # 安全间隙 (mm)
    position_stable_time: float = 1.0  # 位置稳定检测时间（秒）
    position_check_interval: float = 0.2  # 位置检查间隔（秒）
    
    # 运动参数
    default_speed: int = 50         # 默认运动速度 (mm/s)
    default_acceleration: int = 1000 # 默认加速度


@dataclass 
class CoordinateOrigin:
    """坐标原点信息"""
    machine_origin_steps: Dict[str, int] = None    # 机械原点步数位置
    work_origin_steps: Dict[str, int] = None       # 工作原点步数位置
    is_homed: bool = False                         # 是否已回零
    timestamp: str = ""                            # 设定时间戳
    
    def __post_init__(self):
        if self.machine_origin_steps is None:
            self.machine_origin_steps = {"x": 0, "y": 0, "z": 0}
        if self.work_origin_steps is None:
            self.work_origin_steps = {"x": 0, "y": 0, "z": 0}


class CoordinateSystemError(Exception):
    """坐标系统异常"""
    pass


class XYZController(XYZStepperController):
    """XYZ三轴控制器"""
    
    def __init__(self, port: str, baudrate: int = 115200, 
                 machine_config: Optional[MachineConfig] = None,
                 config_file: str = "machine_config.json",
                 auto_connect: bool = True):
        """
        初始化XYZ控制器
        
        Args:
            port: 串口端口
            baudrate: 波特率
            machine_config: 机械配置参数
            config_file: 配置文件路径
            auto_connect: 是否自动连接设备
        """
        super().__init__(port, baudrate)
        
        # 机械配置
        self.machine_config = machine_config or MachineConfig()
        self.config_file = config_file
        
        # 坐标系统
        self.coordinate_origin = CoordinateOrigin()
        import os
        self.origin_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinate_origin.json")
        
        # 连接状态
        self.is_connected = False
        
        # 加载配置
        self._load_config()
        self._load_coordinate_origin()
        
        # 自动连接设备
        if auto_connect:
            self.connect_device()
    
    def connect_device(self) -> bool:
        """
        连接设备并初始化
        
        Returns:
            bool: 连接是否成功
        """
        try:
            logger.info(f"正在连接设备: {self.port}")
            
            # 连接硬件
            if not self.connect():
                logger.error("硬件连接失败")
                return False
            
            self.is_connected = True
            logger.info("设备连接成功")
            
            # 使能所有轴
            enable_results = self.enable_all_axes(True)
            success_count = sum(1 for result in enable_results.values() if result)
            logger.info(f"轴使能结果: {success_count}/{len(enable_results)} 成功")
            
            # 获取系统状态
            try:
                status = self.get_system_status()
                logger.info(f"系统状态获取成功: {len(status)} 项信息")
            except Exception as e:
                logger.warning(f"获取系统状态失败: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"设备连接失败: {e}")
            self.is_connected = False
            return False
    
    def disconnect_device(self):
        """断开设备连接"""
        try:
            if self.is_connected:
                self.disconnect()  # 使用父类的disconnect方法
                self.is_connected = False
                logger.info("设备连接已断开")
        except Exception as e:
            logger.error(f"断开连接失败: {e}")
    
    def _load_config(self):
        """加载机械配置"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    # 更新配置参数
                    for key, value in config_data.items():
                        if hasattr(self.machine_config, key):
                            setattr(self.machine_config, key, value)
                logger.info("机械配置加载完成")
        except Exception as e:
            logger.warning(f"加载机械配置失败: {e}，使用默认配置")
    
    def _save_config(self):
        """保存机械配置"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(asdict(self.machine_config), f, indent=2, ensure_ascii=False)
            logger.info("机械配置保存完成")
        except Exception as e:
            logger.error(f"保存机械配置失败: {e}")
    
    def _load_coordinate_origin(self):
        """加载坐标原点信息"""
        try:
            if os.path.exists(self.origin_file):
                with open(self.origin_file, 'r', encoding='utf-8') as f:
                    origin_data = json.load(f)

                    for key, value in origin_data["machine_origin_steps"].items():
                        origin_data["machine_origin_steps"][key] = round(self.mm_to_steps(MotorAxis[key.upper()], value), 2)

                    for key, value in origin_data["work_origin_steps"].items():
                        origin_data["work_origin_steps"][key] = round(self.mm_to_steps(MotorAxis[key.upper()], value), 2)

                    self.coordinate_origin = CoordinateOrigin(**origin_data)
                logger.info("坐标原点信息加载完成")
        except Exception as e:
            logger.warning(f"加载坐标原点失败: {e}，使用默认设置")
    
    def _save_coordinate_origin(self):
        """保存坐标原点信息"""
        try:
            # 更新时间戳
            from datetime import datetime
            self.coordinate_origin.timestamp = datetime.now().isoformat()
            
            with open(self.origin_file, 'w', encoding='utf-8') as f:
                json_data = asdict(self.coordinate_origin)
                for key, value in json_data["machine_origin_steps"].items():
                    json_data["machine_origin_steps"][key] = round(self.steps_to_mm(MotorAxis[key.upper()], value), 2)

                for key, value in json_data["work_origin_steps"].items():
                    json_data["work_origin_steps"][key] = round(self.steps_to_mm(MotorAxis[key.upper()], value), 2)

                json.dump(json_data, f, indent=2, ensure_ascii=False)
            logger.info("坐标原点信息保存完成")
        except Exception as e:
            logger.error(f"保存坐标原点失败: {e}")
    
    # ==================== 坐标转换方法 ====================
    
    def mm_to_steps(self, axis: MotorAxis, mm: float) -> int:
        """毫米转步数"""
        if axis == MotorAxis.X:
            return int(mm * self.machine_config.steps_per_mm_x)
        elif axis == MotorAxis.Y:
            return int(mm * self.machine_config.steps_per_mm_y)
        elif axis == MotorAxis.Z:
            return int(mm * self.machine_config.steps_per_mm_z)
        else:
            raise ValueError(f"未知轴: {axis}")
    
    def steps_to_mm(self, axis: MotorAxis, steps: int) -> float:
        """步数转毫米"""
        if axis == MotorAxis.X:
            return steps / self.machine_config.steps_per_mm_x
        elif axis == MotorAxis.Y:
            return steps / self.machine_config.steps_per_mm_y
        elif axis == MotorAxis.Z:
            return steps / self.machine_config.steps_per_mm_z
        else:
            raise ValueError(f"未知轴: {axis}")
    
    def work_to_machine_steps(self, x: float = None, y: float = None, z: float = None) -> Dict[str, int]:
        """工作坐标转机械坐标步数"""
        machine_steps = {}
        
        if x is not None:
            work_steps = self.mm_to_steps(MotorAxis.X, x)
            machine_steps['x'] = self.coordinate_origin.work_origin_steps['x'] + work_steps + self.coordinate_origin.machine_origin_steps['x']
        
        if y is not None:
            work_steps = self.mm_to_steps(MotorAxis.Y, y)
            machine_steps['y'] = self.coordinate_origin.work_origin_steps['y'] + work_steps + self.coordinate_origin.machine_origin_steps['y']

        if z is not None:
            work_steps = self.mm_to_steps(MotorAxis.Z, z)
            machine_steps['z'] = self.coordinate_origin.work_origin_steps['z'] + work_steps + self.coordinate_origin.machine_origin_steps['z']
            
        return machine_steps
    
    def machine_to_work_coords(self, machine_steps: Dict[str, int]) -> Dict[str, float]:
        """机械坐标步数转工作坐标"""
        work_coords = {}
        
        for axis_name, steps in machine_steps.items():
            axis = MotorAxis[axis_name.upper()]
            work_origin_steps = self.coordinate_origin.work_origin_steps[axis_name]
            relative_steps = steps - work_origin_steps - self.coordinate_origin.machine_origin_steps[axis_name]
            work_coords[axis_name] = self.steps_to_mm(axis, relative_steps)
            
        return work_coords
    
    def check_travel_limits(self, x: float = None, y: float = None, z: float = None) -> bool:
        """检查行程限制"""
        min_x = min(0, -50)
        min_y = min(0, -50)
        min_z = min(0, -50)
        
        if x is not None and (x < min_x or x > self.machine_config.max_travel_x):
            raise CoordinateSystemError(f"X轴超出行程范围: {x}mm ({min_x} ~ {self.machine_config.max_travel_x}mm)")
        
        if y is not None and (y < min_y or y > self.machine_config.max_travel_y):
            raise CoordinateSystemError(f"Y轴超出行程范围: {y}mm ({min_y} ~ {self.machine_config.max_travel_y}mm)")
            
        if z is not None and (z < min_z or z > self.machine_config.max_travel_z):
            raise CoordinateSystemError(f"Z轴超出行程范围: {z}mm ({min_z} ~ {self.machine_config.max_travel_z}mm)")
            
        return True
    
    # ==================== 回零和原点设定方法 ====================
    
    def home_axis(self, axis: MotorAxis, direction: int = -1, speed: float = None) -> bool:
        """
        单轴回零到限位开关 - 使用步数变化检测
        
        Args:
            axis: 要回零的轴
            direction: 回零方向 (-1负方向, 1正方向)
            
        Returns:
            bool: 回零是否成功
        """
        if not self.is_connected:
            logger.error("设备未连接，无法执行回零操作")
            return False
            
        try:
            logger.info(f"开始{axis.name}轴回零")
            
            # 使能电机
            if not self.enable_motor(axis, True):
                raise CoordinateSystemError(f"{axis.name}轴使能失败")
            
            # 设置回零速度模式，根据方向设置正负
            if speed is None:
                speed_ = self.machine_config.homing_speed 
            else:
                speed_ = speed

            speed_ = min(max(speed_, 0), 500) 
            if not self.set_speed_mode(axis, self.ms_to_rpm(axis, speed_) * direction):
                raise CoordinateSystemError(f"{axis.name}轴设置回零速度失败")
            

            
            # 智能回零检测 - 基于步数变化
            start_time = time.time()
            limit_detected = False
            final_position = None
            
            # 步数变化检测参数（从配置获取）
            position_stable_time = self.machine_config.position_stable_time
            check_interval = self.machine_config.position_check_interval
            last_position = None
            stable_start_time = None
            
            logger.info(f"{axis.name}轴开始移动，监测步数变化...")
            
            while time.time() - start_time < self.machine_config.homing_timeout:
                status = self.get_motor_status(axis)
                current_position = status.steps
                
                # 检查是否明确触碰限位开关
                if (direction < 0 and status.status == MotorStatus.REVERSE_LIMIT_STOP) or \
                   (direction > 0 and status.status == MotorStatus.FORWARD_LIMIT_STOP):
                    # 停止运动
                    self.emergency_stop(axis)
                    time.sleep(0.5)
                    
                    # 记录机械原点位置
                    final_position = current_position
                    limit_detected = True
                    logger.info(f"{axis.name}轴检测到限位开关信号，位置: {final_position}步")
                    break
                
                # 检查是否发生碰撞
                if status.status == MotorStatus.COLLISION_STOP:
                    raise CoordinateSystemError(f"{axis.name}轴回零时发生碰撞")
                
                # 步数变化检测逻辑
                if last_position is not None:
                    # 检查位置是否发生变化
                    if abs(current_position - last_position) <= 1:  # 允许1步的误差
                        # 位置基本没有变化
                        if stable_start_time is None:
                            stable_start_time = time.time()
                            logger.debug(f"{axis.name}轴位置开始稳定在 {current_position}步")
                        elif time.time() - stable_start_time >= position_stable_time:
                            # 位置稳定超过指定时间，认为已到达限位
                            self.emergency_stop(axis)
                            time.sleep(0.5)
                            
                            final_position = current_position
                            limit_detected = True
                            logger.info(f"{axis.name}轴位置稳定{position_stable_time}秒，假设已到达限位开关，位置: {final_position}步")
                            break
                    else:
                        # 位置发生变化，重置稳定计时
                        stable_start_time = None
                        logger.debug(f"{axis.name}轴位置变化: {last_position} -> {current_position}")
                
                last_position = current_position
                time.sleep(check_interval)
            
            # 超时处理
            if not limit_detected:
                logger.warning(f"{axis.name}轴回零超时({self.machine_config.homing_timeout}秒)，强制停止")
                self.emergency_stop(axis)
                time.sleep(0.5)
                
                # 获取当前位置作为机械原点
                try:
                    status = self.get_motor_status(axis)
                    final_position = status.steps
                    logger.info(f"{axis.name}轴超时后位置: {final_position}步")
                except Exception as e:
                    logger.error(f"获取{axis.name}轴位置失败: {e}")
                    return False
            
            # 记录机械原点位置
            self.coordinate_origin.machine_origin_steps[axis.name.lower()] = final_position
            
            # 从限位开关退出安全距离
            try:
                clearance_steps = self.mm_to_steps(axis, self.machine_config.safe_clearance)
                safe_position = final_position + (clearance_steps * -direction)  # 反方向退出
                
                if not self.move_to_position(axis, safe_position, 
                                            self.ms_to_rpm(axis, speed_)):
                    logger.warning(f"{axis.name}轴无法退出到安全位置")
                else:
                    self.wait_for_completion(axis, 10.0)
                    logger.info(f"{axis.name}轴已退出到安全位置: {safe_position}步")
            except Exception as e:
                logger.warning(f"{axis.name}轴退出安全位置时出错: {e}")
            
            status_msg = "限位检测成功" if limit_detected else "超时假设成功"
            logger.info(f"{axis.name}轴回零完成 ({status_msg})，机械原点: {final_position}步")
            return True
            
        except Exception as e:
            logger.error(f"{axis.name}轴回零失败: {e}")
            self.emergency_stop(axis)
            return False
    
    def home_all_axes(self, sequence: list = None) -> bool:
        """
        全轴回零 (液体处理工作站安全回零)
        
        液体处理工作站回零策略：
        1. Z轴必须首先回零，避免与容器、试管架等碰撞
        2. 然后XY轴回零，确保移动路径安全
        3. 严格按照Z->X->Y顺序执行，不允许更改
        
        Args:
            sequence: 回零顺序，液体处理工作站固定为Z->X->Y，不建议修改
            
        Returns:
            bool: 全轴回零是否成功
        """
        if not self.is_connected:
            logger.error("设备未连接，无法执行回零操作")
            return False
        
        # 液体处理工作站安全回零序列：Z轴绝对优先
        safe_sequence = [MotorAxis.Z, MotorAxis.X, MotorAxis.Y]
        
        if sequence is not None and sequence != safe_sequence:
            logger.warning(f"液体处理工作站不建议修改回零序列，使用安全序列: {[axis.name for axis in safe_sequence]}")
            
        sequence = safe_sequence  # 强制使用安全序列
        
        logger.info("开始全轴回零")
        
        try:
            for axis in sequence:
                if not self.home_axis(axis):
                    logger.error(f"全轴回零失败，停止在{axis.name}轴")
                    return False
                # 轴间等待时间
                time.sleep(0.5)

                            # 标记为已回零
            self.coordinate_origin.is_homed = True
            self._save_coordinate_origin()
            
            logger.info("全轴回零完成")
            return True
            
        except Exception as e:
            logger.error(f"全轴回零异常: {e}")
            return False
    
    def set_work_origin_here(self) -> bool:
        """将当前位置设置为工作原点"""
        if not self.is_connected:
            logger.error("设备未连接，无法设置工作原点")
            return False
            
        try:
            if not self.coordinate_origin.is_homed:
                logger.warning("建议先执行回零操作再设置工作原点")
            
            # 获取当前各轴位置
            positions = self.get_all_positions()

            machine_steps = {
                'x': self.mm_to_steps(MotorAxis.X, self.machine_config.reference_distance['x']),
                'y': self.mm_to_steps(MotorAxis.Y, self.machine_config.reference_distance['y']),
                'z': self.mm_to_steps(MotorAxis.Z, self.machine_config.reference_distance['z'])
            }
            
            for axis in MotorAxis:
                axis_name = axis.name.lower()
                current_steps = positions[axis].steps
                self.coordinate_origin.work_origin_steps[axis_name] = current_steps + machine_steps[axis_name] - self.coordinate_origin.machine_origin_steps[axis_name]
                
                logger.info(f"{axis.name}轴工作原点设置为: {current_steps + machine_steps[axis_name] - self.coordinate_origin.machine_origin_steps[axis_name]}步 "
                          f"({self.steps_to_mm(axis, current_steps + machine_steps[axis_name] - self.coordinate_origin.machine_origin_steps[axis_name]):.2f}mm)")
            
            self._save_coordinate_origin()
            logger.info("工作原点设置完成")
            return True
            
        except Exception as e:
            logger.error(f"设置工作原点失败: {e}")
            return False
    
    def ms_to_rpm(self, axis: MotorAxis, velocity_mms: float) -> int:
        """
        将速度从米/秒(m/s)转换为转速(rpm)
        
        Args:
            axis: 电机轴
            velocity_ms: 速度（米/秒）
        
        Returns:
            转速（rpm）
        """
        # 获取每转的行程（mm）
        if axis == MotorAxis.X:
            lead_mm = 80.0
        elif axis == MotorAxis.Y:
            lead_mm = 80.0
        elif axis == MotorAxis.Z:
            lead_mm = 5.0
        else:
            raise ValueError(f"未知轴: {axis}")

        # mm/s -> rps
        rps = velocity_mms / lead_mm
        # rps -> rpm
        rpm = int(rps * 60.0)
        return min(max(rpm, 0), 150)

        
    # ==================== 高级运动控制方法 ====================
    
    def move_to_work_coord_safe(self, x: float = None, y: float = None, z: float = None,
                               speed: float = None, acceleration: int = None) -> bool:
        """
        安全移动到工作坐标系指定位置 (液体处理工作站专用)
        移动策略：Z轴先上升到安全高度 -> XY轴移动到目标位置 -> Z轴下降到目标位置
        
        Args:
            x, y, z: 工作坐标系下的目标位置 (mm)
            speed: 运动速度 (m/s)
            acceleration: 加速度 (rpm/s)
            
        Returns:
            bool: 移动是否成功
        """
        if not self.is_connected:
            logger.error("设备未连接，无法执行移动操作")
            return False
            
        try:
            # 检查坐标系是否已设置
            if not self.coordinate_origin.work_origin_steps:
                raise CoordinateSystemError("工作原点未设置，请先调用set_work_origin_here()")
            
            # 检查行程限制
            # self.check_travel_limits(x, y, z)
            
            # 设置运动参数
            speed = speed or self.machine_config.default_speed
            acceleration = acceleration or self.machine_config.default_acceleration
            
            xy_success = True
            # 步骤1: Z轴先上升到安全高度
            
            machine_steps = self.work_to_machine_steps(x, y, z)

            if z is not None and (x is not None or y is not None):
                safe_z_steps = self.work_to_machine_steps(None, None, self.machine_config.safe_z_height)
                if not self.move_to_position(MotorAxis.Z, safe_z_steps['z'], self.ms_to_rpm(MotorAxis.Z, speed), acceleration):
                    logger.error("Z轴上升到安全高度失败")
                    return False
                logger.info(f"Z轴上升到安全高度: {self.machine_config.safe_z_height} mm")
                
                # 等待Z轴移动完成
                self.wait_for_completion(MotorAxis.Z, 10.0)
            
            # 步骤2: XY轴移动到目标位置
            if x is not None:
                if not self.move_to_position(MotorAxis.X, machine_steps['x'], self.ms_to_rpm(MotorAxis.X, speed), acceleration):
                    xy_success = False
            if y is not None:
                if not self.move_to_position(MotorAxis.Y, machine_steps['y'], self.ms_to_rpm(MotorAxis.Y, speed), acceleration):
                    xy_success = False

            if not xy_success:
                logger.error("XY轴移动失败")
                return False
                
            
            if x is not None or y is not None:
                logger.info(f"XY轴移动到目标位置: X:{x} Y:{y} mm")
                # 等待XY轴移动完成
                if x is not None:
                    self.wait_for_completion(MotorAxis.X, 10.0)
                if y is not None:
                    self.wait_for_completion(MotorAxis.Y, 10.0)
            
            # 步骤3: Z轴下降到目标位置
            if z is not None:   
                if not self.move_to_position(MotorAxis.Z, machine_steps['z'], self.ms_to_rpm(MotorAxis.Z, speed), acceleration):
                    logger.error("Z轴下降到目标位置失败")
                    return False
            logger.info(f"Z轴下降到目标位置: {z} mm")
            self.wait_for_completion(MotorAxis.Z, 10.0)
            
            logger.info(f"安全移动到工作坐标 X:{x} Y:{y} Z:{z} (mm) 完成")
            return True
            
        except Exception as e:
            logger.error(f"安全移动失败: {e}")
            return False

    def move_to_work_coord(self, x: float = None, y: float = None, z: float = None,
                          speed: int = None, acceleration: int = None) -> bool:
        """
        移动到工作坐标 (已禁用)
        
        此方法已被禁用，请使用 move_to_work_coord_safe() 方法。
        
        Raises:
            RuntimeError: 方法已禁用
        """
        error_msg = "Method disabled, use move_to_work_coord_safe instead"
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    def move_relative_work_coord(self, dx: float = 0, dy: float = 0, dz: float = 0,
                               speed: float = None, acceleration: int = None) -> bool:
        """
        相对当前位置移动
        
        Args:
            dx, dy, dz: 相对移动距离 (mm)
            speed: 运动速度 (m/s)  
            acceleration: 加速度 (rpm/s)
            
        Returns:
            bool: 移动是否成功
        """
        if not self.is_connected:
            logger.error("设备未连接，无法执行移动操作")
            return False
            
        try:
            # 获取当前工作坐标
            current_work = self.get_current_work_coords()
            
            # 计算目标坐标
            target_x = current_work['x'] + dx if dx != 0 else None
            target_y = current_work['y'] + dy if dy != 0 else None  
            target_z = current_work['z'] + dz if dz != 0 else None
            
            return self.move_to_work_coord_safe(x=target_x, y=target_y, z=target_z, speed=speed, acceleration=acceleration)
            
        except Exception as e:
            logger.error(f"相对移动失败: {e}")
            return False
    
    def get_current_work_coords(self) -> Dict[str, float]:
        """获取当前工作坐标"""
        if not self.is_connected:
            logger.error("设备未连接，无法获取当前坐标")
            return {'x': 0.0, 'y': 0.0, 'z': 0.0}
            
        try:
            # 获取当前机械坐标
            positions = self.get_all_positions()
            machine_steps = {axis.name.lower(): pos.steps for axis, pos in positions.items()}
            
            # 转换为工作坐标
            return self.machine_to_work_coords(machine_steps)
            
        except Exception as e:
            logger.error(f"获取工作坐标失败: {e}")
            return {'x': 0.0, 'y': 0.0, 'z': 0.0}
    
    def get_current_position_mm(self) -> Dict[str, float]:
        """获取当前位置坐标（毫米单位）"""
        return self.get_current_work_coords()
    
    def wait_for_move_completion(self, timeout: float = 30.0) -> bool:
        """等待所有轴运动完成"""
        if not self.is_connected:
            return False
            
        for axis in MotorAxis:
            if not self.wait_for_completion(axis, timeout):
                return False
        return True
    
    # ==================== 系统状态和配置方法 ====================
    
    def get_system_status(self) -> Dict:
        """获取系统状态信息"""
        status = {
            "connection": {
                "is_connected": self.is_connected,
                "port": self.port,
                "baudrate": self.baudrate
            },
            "coordinate_system": {
                "is_homed": self.coordinate_origin.is_homed,
                "machine_origin": self.coordinate_origin.machine_origin_steps,
                "work_origin": self.coordinate_origin.work_origin_steps,
                "timestamp": self.coordinate_origin.timestamp
            },
            "machine_config": asdict(self.machine_config),
            "current_position": {}
        }
        
        if self.is_connected:
            try:
                # 获取当前位置
                positions = self.get_all_positions()
                for axis, pos in positions.items():
                    axis_name = axis.name.lower()
                    status["current_position"][axis_name] = {
                        "steps": pos.steps,
                        "mm": self.steps_to_mm(axis, pos.steps),
                        "status": pos.status.name if hasattr(pos.status, 'name') else str(pos.status)
                    }
                    
                # 获取工作坐标
                work_coords = self.get_current_work_coords()
                status["current_work_coords"] = work_coords
                
            except Exception as e:
                status["position_error"] = str(e)
        
        return status
    
    def update_machine_config(self, **kwargs):
        """更新机械配置参数"""
        for key, value in kwargs.items():
            if hasattr(self.machine_config, key):
                setattr(self.machine_config, key, value)
                logger.info(f"更新配置参数 {key}: {value}")
            else:
                logger.warning(f"未知配置参数: {key}")
        
        # 保存配置
        self._save_config()
    
    def reset_coordinate_system(self):
        """重置坐标系统"""
        self.coordinate_origin = CoordinateOrigin()
        self._save_coordinate_origin()
        logger.info("坐标系统已重置")
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.disconnect_device()


def interactive_control(controller: XYZController):
    """
    交互式控制模式
    
    Args:
        controller: 已连接的控制器实例
    """
    print("\n" + "="*60)
    print("进入交互式控制模式")
    print("="*60)
    
    # 显示当前状态
    def show_status():
        try:
            current_pos = controller.get_current_position_mm()
            print(f"\n当前位置: X={current_pos['x']:.2f}mm, Y={current_pos['y']:.2f}mm, Z={current_pos['z']:.2f}mm")
        except Exception as e:
            print(f"获取位置失败: {e}")
    
    # 显示帮助信息
    def show_help():
        print("\n可用命令:")
        print("  move <轴> <距离>     - 相对移动，例: move x 10.5")
        print("  goto <x> <y> <z>     - 绝对移动到指定坐标，例: goto 10 20 5")
        print("  home [轴]            - 回零操作，例: home 或 home x")
        print("  origin               - 设置当前位置为工作原点")
        print("  status               - 显示当前状态")
        print("  speed <速度>         - 设置运动速度(mm/s)，例: speed 2000")
        print("  limits               - 显示行程限制")
        print("  config               - 显示机械配置")
        print("  help                 - 显示此帮助信息")
        print("  quit/exit            - 退出交互模式")
        print("\n提示:")
        print("  - 轴名称: x, y, z")
        print("  - 距离单位: 毫米(mm)")
        print("  - 正数向正方向移动，负数向负方向移动")
    

    
    # 安全回零操作
    def safe_homing():
        print("\n系统安全初始化...")
        print("为确保操作安全，系统将执行回零操作")
        print("提示: 已安装限位开关，超时后将假设回零成功")
        
        # 询问用户是否继续
        while True:
            user_choice = input("是否继续执行回零操作? (y/n/skip): ").strip().lower()
            if user_choice in ['y', 'yes', '是']:
                print("\n开始执行全轴回零...")
                print("回零过程可能需要一些时间，请耐心等待...")
                
                # 执行回零操作
                homing_success = controller.home_all_axes()
                
                if homing_success:
                    print("回零操作完成，系统已就绪")
                    # 设置当前位置为工作原点

                    # if controller.set_work_origin_here():
                    #     print("工作原点已设置为回零位置")
                    # else:
                    #     print("工作原点设置失败，但可以继续操作")
                    return True
                else:
                    print("回零操作失败")
                    print("这可能是由于通信问题，但限位开关应该已经起作用")
                    
                    # 询问是否继续
                    retry_choice = input("是否仍要继续操作? (y/n): ").strip().lower()
                    if retry_choice in ['y', 'yes', '是']:
                        print("继续操作，请手动确认设备位置安全")
                        return True
                    else:
                        return False
                        
            elif user_choice in ['n', 'no', '否']:
                print("用户取消回零操作，退出交互模式")
                return False
            elif user_choice in ['skip', 's', '跳过']:
                print("跳过回零操作，请注意安全！")
                print("建议在开始操作前手动执行 'home' 命令")
                return True
            else:
                print("请输入 y(继续)/n(取消)/skip(跳过)")
    
    # 安全回原点操作
    def safe_return_home():
        print("\n系统安全关闭...")
        print("正在将所有轴移动到安全位置...")
        
        try:
            # 移动到工作原点 (0,0,0) - 使用安全移动方法
            if controller.move_to_work_coord_safe(0, 0, 0, speed=50):
                print("已安全返回工作原点")
                show_status()
            else:
                print("返回原点失败，请手动检查设备位置")
        except Exception as e:
            print(f"返回原点时出错: {e}")
    
    # 当前运动速度
    current_speed = controller.machine_config.default_speed
    
    try:
        # 1. 首先执行安全回零
        if not safe_homing():
            return
        
        # 2. 显示初始状态和帮助
        show_status()
        show_help()
        
        while True:
            try:
                # 获取用户输入
                user_input = input("\n请输入命令 (输入 help 查看帮助): ").strip().lower()
                
                if not user_input:
                    continue
                
                # 解析命令
                parts = user_input.split()
                command = parts[0]
                
                if command in ['quit', 'exit', 'q']:
                    print("准备退出交互模式...")
                    # 执行安全回原点操作
                    safe_return_home()
                    print("退出交互模式")
                    break
                
                elif command == 'help' or command == 'h':
                    show_help()
                
                elif command == 'status' or command == 's':
                    show_status()
                    print(f"当前速度: {current_speed} m/s")
                    print(f"是否已回零: {controller.coordinate_origin.is_homed}")
                
                elif command == 'move' or command == 'm':
                    if len(parts) != 3:
                        print("格式错误，正确格式: move <轴> <距离>")
                        print("   例如: move x 10.5")
                        continue
                    
                    axis = parts[1].lower()
                    try:
                        distance = float(parts[2])
                    except ValueError:
                        print("距离必须是数字")
                        continue
                    
                    if axis not in ['x', 'y', 'z']:
                        print("轴名称必须是 x, y 或 z")
                        continue
                    
                    print(f"{axis.upper()}轴移动 {distance:+.2f}mm...")
                    
                    # 执行移动
                    kwargs = {f'd{axis}': distance, 'speed': current_speed}
                    if controller.move_relative_work_coord(**kwargs):
                        print(f"{axis.upper()}轴移动完成")
                        show_status()
                    else:
                        print(f"{axis.upper()}轴移动失败")
                
                elif command == 'goto' or command == 'g':
                    if len(parts) != 4:
                        print("格式错误，正确格式: goto <x> <y> <z>")
                        print("   例如: goto 10 20 5")
                        continue
                    
                    try:
                        x = float(parts[1])
                        y = float(parts[2])
                        z = float(parts[3])
                    except ValueError:
                        print("坐标必须是数字")
                        continue
                    
                    print(f"移动到坐标 ({x}, {y}, {z})...")
                    print("使用安全移动策略: Z轴先上升 → XY移动 → Z轴下降")
                    
                    if controller.move_to_work_coord_safe(x, y, z, speed=current_speed):
                        print("安全移动到目标位置完成")
                        show_status()
                    else:
                        print("移动失败")
                
                elif command == 'home':
                    if len(parts) == 1:
                        # 全轴回零
                        print("开始全轴回零...")
                        if controller.home_all_axes():
                            print("全轴回零完成")
                            show_status()
                        else:
                            print("回零失败")
                    elif len(parts) == 2:
                        # 单轴回零
                        axis_name = parts[1].lower()
                        if axis_name not in ['x', 'y', 'z']:
                            print("轴名称必须是 x, y 或 z")
                            continue
                        
                        axis = MotorAxis[axis_name.upper()]
                        print(f"{axis_name.upper()}轴回零...")
                        
                        if controller.home_axis(axis):
                            print(f"{axis_name.upper()}轴回零完成")
                            show_status()
                        else:
                            print(f"{axis_name.upper()}轴回零失败")
                    else:
                        print("格式错误，正确格式: home 或 home <轴>")
                
                elif command == 'origin' or command == 'o':
                    print("设置当前位置为工作原点...")
                    if controller.set_work_origin_here():
                        print("工作原点设置完成")
                        show_status()
                    else:
                        print("工作原点设置失败")
                
                elif command == 'speed':
                    if len(parts) != 2:
                        print("格式错误，正确格式: speed <速度>")
                        print("   例如: speed 200")
                        continue
                    
                    try:
                        new_speed = int(parts[1])
                        if new_speed <= 0:
                            print("速度必须大于0")
                            continue
                        if new_speed > 500:
                            print("速度不能超过500 mm/s")
                            continue
                        
                        current_speed = new_speed
                        print(f"运动速度设置为: {current_speed} mm/s")
                        
                    except ValueError:
                        print("速度必须是整数")
                
                elif command == 'limits' or command == 'l':
                    config = controller.machine_config
                    print("\n行程限制:")
                    print(f"  X轴: 0 ~ {config.max_travel_x} mm")
                    print(f"  Y轴: 0 ~ {config.max_travel_y} mm")
                    print(f"  Z轴: 0 ~ {config.max_travel_z} mm")
                
                elif command == 'config' or command == 'c':
                    config = controller.machine_config
                    print("\n机械配置:")
                    print(f"  X轴步距: {config.steps_per_mm_x:.1f} 步/mm")
                    print(f"  Y轴步距: {config.steps_per_mm_y:.1f} 步/mm")
                    print(f"  Z轴步距: {config.steps_per_mm_z:.1f} 步/mm")
                    print(f"  回零速度: {config.homing_speed} mm/s")
                    print(f"  默认速度: {config.default_speed} mm/s")
                    print(f"  安全间隙: {config.safe_clearance} mm")
                
                else:
                    print(f"未知命令: {command}")
                    print("输入 help 查看可用命令")
            
            except KeyboardInterrupt:
                print("\n\n用户中断，退出交互模式")
                break
            except Exception as e:
                print(f"命令执行错误: {e}")
                print("输入 help 查看正确的命令格式")
    
    finally:
        # 确保正确断开连接
        try:
            controller.disconnect_device()
            print("设备连接已断开")
        except Exception as e:
            print(f"断开连接时出错: {e}")


def run_tests():
    """运行测试函数"""
    print("=== XYZ控制器测试 ===")
    
    # 1. 测试机械配置
    print("\n1. 测试机械配置")
    config = MachineConfig(
        steps_per_mm_x=204.8,   # 16384步/圈 ÷ 80mm导程
        steps_per_mm_y=204.8,   # 16384步/圈 ÷ 80mm导程
        steps_per_mm_z=3276.8,  # 16384步/圈 ÷ 5mm导程
        max_travel_x=340.0,
        max_travel_y=250.0,
        max_travel_z=160.0,
        homing_speed=50,
        default_speed=50
    )
    print(f"X轴步距: {config.steps_per_mm_x} 步/mm")
    print(f"Y轴步距: {config.steps_per_mm_y} 步/mm")
    print(f"Z轴步距: {config.steps_per_mm_z} 步/mm")
    print(f"行程限制: X={config.max_travel_x}mm, Y={config.max_travel_y}mm, Z={config.max_travel_z}mm")
    
    # 2. 测试坐标原点数据结构
    print("\n2. 测试坐标原点数据结构")
    origin = CoordinateOrigin()
    print(f"初始状态: 已回零={origin.is_homed}")
    print(f"机械原点: {origin.machine_origin_steps}")
    print(f"工作原点: {origin.work_origin_steps}")
    
    # 设置示例数据
    origin.machine_origin_steps = {'x': 0, 'y': 0, 'z': 0}
    origin.work_origin_steps = {'x': 16384, 'y': 16384, 'z': 13107}  # 5mm, 5mm, 2mm (基于16384步/圈)
    origin.is_homed = True
    origin.timestamp = "2024-09-26 12:00:00"
    print(f"设置后: 已回零={origin.is_homed}")
    print(f"机械原点: {origin.machine_origin_steps}")
    print(f"工作原点: {origin.work_origin_steps}")
    
    # 3. 测试离线功能
    print("\n3. 测试离线功能")
    
    # 创建离线控制器（不自动连接）
    offline_controller = XYZController(
        port='/dev/ttyUSB_CH340',
        machine_config=config,
        auto_connect=False
    )
    
    # 测试单位转换
    print("\n单位转换测试:")
    test_distances = [1.0, 5.0, 10.0, 25.5]
    for distance in test_distances:
        x_steps = offline_controller.mm_to_steps(MotorAxis.X, distance)
        y_steps = offline_controller.mm_to_steps(MotorAxis.Y, distance)
        z_steps = offline_controller.mm_to_steps(MotorAxis.Z, distance)
        print(f"{distance}mm -> X:{x_steps}步, Y:{y_steps}步, Z:{z_steps}步")
        
        # 反向转换验证
        x_mm = offline_controller.steps_to_mm(MotorAxis.X, x_steps)
        y_mm = offline_controller.steps_to_mm(MotorAxis.Y, y_steps)
        z_mm = offline_controller.steps_to_mm(MotorAxis.Z, z_steps)
        print(f"反向转换: X:{x_mm:.2f}mm, Y:{y_mm:.2f}mm, Z:{z_mm:.2f}mm")
    
    # 测试坐标系转换
    print("\n坐标系转换测试:")
    offline_controller.coordinate_origin = origin  # 使用示例原点
    work_coords = [(0, 0, 0), (10, 15, 5), (50, 30, 20)]
    
    for x, y, z in work_coords:
        try:
            machine_steps = offline_controller.work_to_machine_steps(x, y, z)
            print(f"工作坐标 ({x}, {y}, {z}) -> 机械步数 {machine_steps}")
            
            # 反向转换验证
            work_coords_back = offline_controller.machine_to_work_coords(machine_steps)
            print(f"反向转换: ({work_coords_back['x']:.2f}, {work_coords_back['y']:.2f}, {work_coords_back['z']:.2f})")
        except Exception as e:
            print(f"转换失败: {e}")
    
    # 测试行程限制检查
    print("\n行程限制检查测试:")
    test_positions = [
        (50, 50, 25, "正常位置"),
        (250, 50, 25, "X轴超限"),
        (50, 350, 25, "Y轴超限"),
        (50, 50, 150, "Z轴超限"),
        (-10, 50, 25, "X轴负超限"),
        (50, -10, 25, "Y轴负超限"),
        (50, 50, -5, "Z轴负超限")
    ]
    
    # for x, y, z, desc in test_positions:
    #     try:
    #         offline_controller.check_travel_limits(x, y, z)
    #         print(f"{desc} ({x}, {y}, {z}): 有效")
    #     except CoordinateSystemError as e:
    #         print(f"{desc} ({x}, {y}, {z}): 超限 - {e}")
    
    print("\n=== 离线功能测试完成 ===")
    
    # 4. 硬件连接测试
    print("\n4. 硬件连接测试")
    print("尝试连接真实设备...")
    
    # 可能的串口列表
    possible_ports = [
        '/dev/ttyUSB_CH340'
    ]
    
    connected_controller = None
    
    for port in possible_ports:
        try:
            print(f"尝试连接端口: {port}")
            controller = XYZController(
                port=port,
                machine_config=config,
                auto_connect=True
            )
            
            if controller.is_connected:
                print(f"成功连接到 {port}")
                connected_controller = controller
                
                # 获取系统状态
                status = controller.get_system_status()
                print("\n系统状态:")
                print(f"  连接状态: {status['connection']['is_connected']}")
                print(f"  是否已回零: {status['coordinate_system']['is_homed']}")
                
                if 'current_position' in status:
                    print("  当前位置:")
                    for axis, pos_info in status['current_position'].items():
                        print(f"    {axis.upper()}轴: {pos_info['steps']}步 ({pos_info['mm']:.2f}mm)")
                
                # 测试基本移动功能
                print("\n测试基本移动功能:")
                try:
                    # 获取当前位置
                    current_pos = controller.get_current_position_mm()
                    print(f"当前工作坐标: {current_pos}")
                    
                    # 小幅移动测试
                    print("执行小幅移动测试 (X+1mm)...")
                    if controller.move_relative_work_coord(dx=1.0, speed=500):
                        print("移动成功")
                        time.sleep(1)
                        new_pos = controller.get_current_position_mm()
                        print(f"移动后坐标: {new_pos}")
                    else:
                        print("移动失败")
                        
                except Exception as e:
                    print(f"移动测试失败: {e}")
                
                break
                
        except Exception as e:
            print(f"连接 {port} 失败: {e}")
            continue
    
    if not connected_controller:
        print("未找到可用的设备端口")
        print("请检查:")
        print("  1. 设备是否正确连接")
        print("  2. 串口端口是否正确")
        print("  3. 设备驱动是否安装")
    else:
        # 进入交互式控制模式
        interactive_control(connected_controller)
    
    print("\n=== XYZ控制器测试完成 ===")


# ==================== 测试和示例代码 ====================
if __name__ == "__main__":
    run_tests()
    # xyz_controller = XYZController(port='/dev/ttyUSB_CH340', auto_connect=True)
    # # xyz_controller.stop_all_axes()
    # xyz_controller.connect_device()
    # time.sleep(1)
    # xyz_controller.home_all_axes()
