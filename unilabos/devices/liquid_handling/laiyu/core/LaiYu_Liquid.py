"""
LaiYu_Liquid 液体处理工作站主要集成文件

该模块实现了 LaiYu_Liquid 与 UniLabOS 系统的集成，提供标准化的液体处理接口。
主要包含：
- LaiYuLiquidBackend: 硬件通信后端
- LaiYuLiquid: 主要接口类
- 相关的异常类和容器类
"""

import asyncio
import logging
import time
from typing import List, Optional, Dict, Any, Union, Tuple
from dataclasses import dataclass
from abc import ABC, abstractmethod

# 基础导入
try:
    from pylabrobot.resources import Deck, Plate, TipRack, Tip, Resource, Well
    PYLABROBOT_AVAILABLE = True
except ImportError:
    # 如果 pylabrobot 不可用，创建基础的模拟类
    PYLABROBOT_AVAILABLE = False
    
    class Resource:
        def __init__(self, name: str):
            self.name = name
    
    class Deck(Resource):
        pass
    
    class Plate(Resource):
        pass
    
    class TipRack(Resource):
        pass
    
    class Tip(Resource):
        pass
    
    class Well(Resource):
        pass

# LaiYu_Liquid 控制器导入
try:
    from .controllers.pipette_controller import (
        PipetteController, TipStatus, LiquidClass, LiquidParameters
    )
    from .controllers.xyz_controller import (
        XYZController, MachineConfig, CoordinateOrigin, MotorAxis
    )
    CONTROLLERS_AVAILABLE = True
except ImportError:
    CONTROLLERS_AVAILABLE = False
    # 创建模拟的控制器类
    class PipetteController:
        def __init__(self, *args, **kwargs):
            pass
        
        def connect(self):
            return True
        
        def initialize(self):
            return True
    
    class XYZController:
        def __init__(self, *args, **kwargs):
            pass
        
        def connect_device(self):
            return True

logger = logging.getLogger(__name__)


class LaiYuLiquidError(RuntimeError):
    """LaiYu_Liquid 设备异常"""
    pass


@dataclass
class LaiYuLiquidConfig:
    """LaiYu_Liquid 设备配置"""
    port: str = "/dev/cu.usbserial-3130"  # RS485转USB端口
    address: int = 1  # 设备地址
    baudrate: int = 9600  # 波特率
    timeout: float = 5.0  # 通信超时时间
    
    # 工作台尺寸
    deck_width: float = 340.0  # 工作台宽度 (mm)
    deck_height: float = 250.0  # 工作台高度 (mm)
    deck_depth: float = 160.0  # 工作台深度 (mm)
    
    # 移液参数
    max_volume: float = 1000.0  # 最大体积 (μL)
    min_volume: float = 0.1  # 最小体积 (μL)
    
    # 运动参数
    max_speed: float = 100.0  # 最大速度 (mm/s)
    acceleration: float = 50.0  # 加速度 (mm/s²)
    
    # 安全参数
    safe_height: float = 50.0  # 安全高度 (mm)
    tip_pickup_depth: float = 10.0  # 吸头拾取深度 (mm)
    liquid_detection: bool = True  # 液面检测
    
    # 取枪头相关参数
    tip_pickup_speed: int = 30  # 取枪头时的移动速度 (rpm)
    tip_pickup_acceleration: int = 500  # 取枪头时的加速度 (rpm/s)
    tip_approach_height: float = 5.0  # 接近枪头时的高度 (mm)
    tip_pickup_force_depth: float = 2.0  # 强制插入深度 (mm)
    tip_pickup_retract_height: float = 20.0  # 取枪头后的回退高度 (mm)
    
    # 丢弃枪头相关参数
    tip_drop_height: float = 10.0  # 丢弃枪头时的高度 (mm)
    tip_drop_speed: int = 50  # 丢弃枪头时的移动速度 (rpm)
    trash_position: Tuple[float, float, float] = (300.0, 200.0, 0.0)  # 垃圾桶位置 (mm)
    
    # 安全范围配置
    deck_width: float = 300.0  # 工作台宽度 (mm)
    deck_height: float = 200.0  # 工作台高度 (mm)
    deck_depth: float = 100.0  # 工作台深度 (mm)
    safe_height: float = 50.0  # 安全高度 (mm)
    position_validation: bool = True  # 启用位置验证
    emergency_stop_enabled: bool = True  # 启用紧急停止


class LaiYuLiquidDeck:
    """LaiYu_Liquid 工作台管理"""
    
    def __init__(self, config: LaiYuLiquidConfig):
        self.config = config
        self.resources: Dict[str, Resource] = {}
        self.positions: Dict[str, Tuple[float, float, float]] = {}
        
    def add_resource(self, name: str, resource: Resource, position: Tuple[float, float, float]):
        """添加资源到工作台"""
        self.resources[name] = resource
        self.positions[name] = position
        
    def get_resource(self, name: str) -> Optional[Resource]:
        """获取资源"""
        return self.resources.get(name)
        
    def get_position(self, name: str) -> Optional[Tuple[float, float, float]]:
        """获取资源位置"""
        return self.positions.get(name)
        
    def list_resources(self) -> List[str]:
        """列出所有资源"""
        return list(self.resources.keys())


class LaiYuLiquidContainer:
    """LaiYu_Liquid 容器类"""
    
    def __init__(self, name: str, size_x: float = 0, size_y: float = 0, size_z: float = 0, container_type: str = "", volume: float = 0.0, max_volume: float = 1000.0, lid_height: float = 0.0):
        self.name = name
        self.size_x = size_x
        self.size_y = size_y
        self.size_z = size_z
        self.lid_height = lid_height
        self.container_type = container_type
        self.volume = volume
        self.max_volume = max_volume
        self.last_updated = time.time()
        self.child_resources = {}  # 存储子资源
        
    @property
    def is_empty(self) -> bool:
        return self.volume <= 0.0
        
    @property
    def is_full(self) -> bool:
        return self.volume >= self.max_volume
        
    @property
    def available_volume(self) -> float:
        return max(0.0, self.max_volume - self.volume)
        
    def add_volume(self, volume: float) -> bool:
        """添加体积"""
        if self.volume + volume <= self.max_volume:
            self.volume += volume
            self.last_updated = time.time()
            return True
        return False
        
    def remove_volume(self, volume: float) -> bool:
        """移除体积"""
        if self.volume >= volume:
            self.volume -= volume
            self.last_updated = time.time()
            return True
        return False
    
    def assign_child_resource(self, resource, location=None):
        """分配子资源 - 与 PyLabRobot 资源管理系统兼容"""
        if hasattr(resource, 'name'):
            self.child_resources[resource.name] = {
                'resource': resource,
                'location': location
            }


class LaiYuLiquidTipRack:
    """LaiYu_Liquid 吸头架类"""
    
    def __init__(self, name: str, size_x: float = 0, size_y: float = 0, size_z: float = 0, tip_count: int = 96, tip_volume: float = 1000.0):
        self.name = name
        self.size_x = size_x
        self.size_y = size_y
        self.size_z = size_z
        self.tip_count = tip_count
        self.tip_volume = tip_volume
        self.tips_available = [True] * tip_count
        self.child_resources = {}  # 存储子资源
        
    @property
    def available_tips(self) -> int:
        return sum(self.tips_available)
        
    @property
    def is_empty(self) -> bool:
        return self.available_tips == 0
        
    def pick_tip(self, position: int) -> bool:
        """拾取吸头"""
        if 0 <= position < self.tip_count and self.tips_available[position]:
            self.tips_available[position] = False
            return True
        return False
        
    def has_tip(self, position: int) -> bool:
        """检查位置是否有吸头"""
        if 0 <= position < self.tip_count:
            return self.tips_available[position]
        return False
    
    def assign_child_resource(self, resource, location=None):
        """分配子资源到指定位置"""
        self.child_resources[resource.name] = {
            'resource': resource,
            'location': location
        }


def get_module_info():
    """获取模块信息"""
    return {
        "name": "LaiYu_Liquid",
        "version": "1.0.0",
        "description": "LaiYu液体处理工作站模块，提供移液器控制、XYZ轴控制和资源管理功能",
        "author": "UniLabOS Team",
        "capabilities": [
            "移液器控制",
            "XYZ轴运动控制", 
            "吸头架管理",
            "板和容器管理",
            "资源位置管理"
        ],
        "dependencies": {
            "required": ["serial"],
            "optional": ["pylabrobot"]
        }
    }


class LaiYuLiquidBackend:
    """LaiYu_Liquid 硬件通信后端"""
    
    def __init__(self, config: LaiYuLiquidConfig, deck: Optional['LaiYuLiquidDeck'] = None):
        self.config = config
        self.deck = deck  # 工作台引用，用于获取资源位置信息
        self.pipette_controller = None
        self.xyz_controller = None
        self.is_connected = False
        self.is_initialized = False
        
        # 状态跟踪
        self.current_position = (0.0, 0.0, 0.0)
        self.tip_attached = False
        self.current_volume = 0.0
        
    def _validate_position(self, x: float, y: float, z: float) -> bool:
        """验证位置是否在安全范围内"""
        try:
            # 检查X轴范围
            if not (0 <= x <= self.config.deck_width):
                logger.error(f"X轴位置 {x:.2f}mm 超出范围 [0, {self.config.deck_width}]")
                return False
            
            # 检查Y轴范围
            if not (0 <= y <= self.config.deck_height):
                logger.error(f"Y轴位置 {y:.2f}mm 超出范围 [0, {self.config.deck_height}]")
                return False
            
            # 检查Z轴范围（负值表示向下，0为工作台表面）
            if not (-self.config.deck_depth <= z <= self.config.safe_height):
                logger.error(f"Z轴位置 {z:.2f}mm 超出安全范围 [{-self.config.deck_depth}, {self.config.safe_height}]")
                return False
            
            return True
        except Exception as e:
            logger.error(f"位置验证失败: {e}")
            return False
    
    def _check_hardware_ready(self) -> bool:
        """检查硬件是否准备就绪"""
        if not self.is_connected:
            logger.error("设备未连接")
            return False
        
        if CONTROLLERS_AVAILABLE:
            if self.xyz_controller is None:
                logger.error("XYZ控制器未初始化")
                return False
        
        return True
    
    async def emergency_stop(self) -> bool:
        """紧急停止所有运动"""
        try:
            logger.warning("执行紧急停止")
            
            if CONTROLLERS_AVAILABLE and self.xyz_controller:
                # 停止XYZ控制器
                await self.xyz_controller.stop_all_motion()
                logger.info("XYZ控制器已停止")
            
            if self.pipette_controller:
                # 停止移液器控制器
                await self.pipette_controller.stop()
                logger.info("移液器控制器已停止")
            
            return True
        except Exception as e:
            logger.error(f"紧急停止失败: {e}")
            return False
    
    async def move_to_safe_position(self) -> bool:
        """移动到安全位置"""
        try:
            if not self._check_hardware_ready():
                return False
            
            safe_position = (
                self.config.deck_width / 2,  # 工作台中心X
                self.config.deck_height / 2,  # 工作台中心Y
                self.config.safe_height  # 安全高度Z
            )
            
            if not self._validate_position(*safe_position):
                logger.error("安全位置无效")
                return False
            
            if CONTROLLERS_AVAILABLE and self.xyz_controller:
                await self.xyz_controller.move_to_work_coord(*safe_position)
                self.current_position = safe_position
                logger.info(f"已移动到安全位置: {safe_position}")
                return True
            else:
                # 模拟模式
                self.current_position = safe_position
                logger.info("模拟移动到安全位置")
                return True
                
        except Exception as e:
            logger.error(f"移动到安全位置失败: {e}")
            return False
        
    async def setup(self) -> bool:
        """设置硬件连接"""
        try:
            if CONTROLLERS_AVAILABLE:
                # 初始化移液器控制器
                self.pipette_controller = PipetteController(
                    port=self.config.port,
                    address=self.config.address
                )
                
                # 初始化XYZ控制器
                machine_config = MachineConfig()
                self.xyz_controller = XYZController(
                    port=self.config.port,
                    baudrate=self.config.baudrate,
                    machine_config=machine_config
                )
                
                # 连接设备
                pipette_connected = await asyncio.to_thread(self.pipette_controller.connect)
                xyz_connected = await asyncio.to_thread(self.xyz_controller.connect_device)
                
                if pipette_connected and xyz_connected:
                    self.is_connected = True
                    logger.info("LaiYu_Liquid 硬件连接成功")
                    return True
                else:
                    logger.error("LaiYu_Liquid 硬件连接失败")
                    return False
            else:
                # 模拟模式
                logger.info("LaiYu_Liquid 运行在模拟模式")
                self.is_connected = True
                return True
                
        except Exception as e:
            logger.error(f"LaiYu_Liquid 设置失败: {e}")
            return False
    
    async def stop(self):
        """停止设备"""
        try:
            if self.pipette_controller and hasattr(self.pipette_controller, 'disconnect'):
                await asyncio.to_thread(self.pipette_controller.disconnect)
            
            if self.xyz_controller and hasattr(self.xyz_controller, 'disconnect'):
                await asyncio.to_thread(self.xyz_controller.disconnect)
                
            self.is_connected = False
            self.is_initialized = False
            logger.info("LaiYu_Liquid 已停止")
            
        except Exception as e:
            logger.error(f"LaiYu_Liquid 停止失败: {e}")
    
    async def move_to(self, x: float, y: float, z: float) -> bool:
        """移动到指定位置"""
        try:
            if not self.is_connected:
                raise LaiYuLiquidError("设备未连接")
            
            # 模拟移动
            await asyncio.sleep(0.1)  # 模拟移动时间
            self.current_position = (x, y, z)
            logger.debug(f"移动到位置: ({x}, {y}, {z})")
            return True
            
        except Exception as e:
            logger.error(f"移动失败: {e}")
            return False
    
    async def pick_up_tip(self, tip_rack: str, position: int) -> bool:
        """拾取吸头 - 包含真正的Z轴下降控制"""
        try:
            # 硬件准备检查
            if not self._check_hardware_ready():
                return False
                
            if self.tip_attached:
                logger.warning("已有吸头附着，无法拾取新吸头")
                return False
                
            logger.info(f"开始从 {tip_rack} 位置 {position} 拾取吸头")
            
            # 获取枪头架位置信息
            if self.deck is None:
                logger.error("工作台未初始化")
                return False
                
            tip_position = self.deck.get_position(tip_rack)
            if tip_position is None:
                logger.error(f"未找到枪头架 {tip_rack} 的位置信息")
                return False
                
            # 计算具体枪头位置（这里简化处理，实际应根据position计算偏移）
            tip_x, tip_y, tip_z = tip_position
            
            # 验证所有关键位置的安全性
            safe_z = tip_z + self.config.tip_approach_height
            pickup_z = tip_z - self.config.tip_pickup_force_depth
            retract_z = tip_z + self.config.tip_pickup_retract_height
            
            if not (self._validate_position(tip_x, tip_y, safe_z) and 
                    self._validate_position(tip_x, tip_y, pickup_z) and 
                    self._validate_position(tip_x, tip_y, retract_z)):
                logger.error("枪头拾取位置超出安全范围")
                return False
            
            if CONTROLLERS_AVAILABLE and self.xyz_controller:
                # 真实硬件控制流程
                logger.info("使用真实XYZ控制器进行枪头拾取")
                
                try:
                    # 1. 移动到枪头上方的安全位置
                    safe_z = tip_z + self.config.tip_approach_height
                    logger.info(f"移动到枪头上方安全位置: ({tip_x:.2f}, {tip_y:.2f}, {safe_z:.2f})")
                    move_success = await asyncio.to_thread(
                        self.xyz_controller.move_to_work_coord, 
                        tip_x, tip_y, safe_z
                    )
                    if not move_success:
                        logger.error("移动到枪头上方失败")
                        return False
                    
                    # 2. Z轴下降到枪头位置
                    pickup_z = tip_z - self.config.tip_pickup_force_depth
                    logger.info(f"Z轴下降到枪头拾取位置: {pickup_z:.2f}mm")
                    z_down_success = await asyncio.to_thread(
                        self.xyz_controller.move_to_work_coord,
                        tip_x, tip_y, pickup_z
                    )
                    if not z_down_success:
                        logger.error("Z轴下降到枪头位置失败")
                        return False
                    
                    # 3. 等待一小段时间确保枪头牢固附着
                    await asyncio.sleep(0.2)
                    
                    # 4. Z轴上升到回退高度
                    retract_z = tip_z + self.config.tip_pickup_retract_height
                    logger.info(f"Z轴上升到回退高度: {retract_z:.2f}mm")
                    z_up_success = await asyncio.to_thread(
                        self.xyz_controller.move_to_work_coord,
                        tip_x, tip_y, retract_z
                    )
                    if not z_up_success:
                        logger.error("Z轴上升失败")
                        return False
                    
                    # 5. 更新当前位置
                    self.current_position = (tip_x, tip_y, retract_z)
                    
                except Exception as move_error:
                    logger.error(f"枪头拾取过程中发生错误: {move_error}")
                    # 尝试移动到安全位置
                    if self.config.emergency_stop_enabled:
                        await self.emergency_stop()
                        await self.move_to_safe_position()
                    return False
                
            else:
                # 模拟模式
                logger.info("模拟模式：执行枪头拾取动作")
                await asyncio.sleep(1.0)  # 模拟整个拾取过程的时间
                self.current_position = (tip_x, tip_y, tip_z + self.config.tip_pickup_retract_height)
            
            # 6. 标记枪头已附着
            self.tip_attached = True
            logger.info("吸头拾取成功")
            return True
            
        except Exception as e:
            logger.error(f"拾取吸头失败: {e}")
            return False
    
    async def drop_tip(self, location: str = "trash") -> bool:
        """丢弃吸头 - 包含真正的Z轴控制"""
        try:
            # 硬件准备检查
            if not self._check_hardware_ready():
                return False
            
            if not self.tip_attached:
                logger.warning("没有吸头附着，无需丢弃")
                return True
            
            logger.info(f"开始丢弃吸头到 {location}")
            
            # 确定丢弃位置
            if location == "trash":
                # 使用配置中的垃圾桶位置
                drop_x, drop_y, drop_z = self.config.trash_position
            else:
                # 尝试从deck获取指定位置
                if self.deck is None:
                    logger.error("工作台未初始化")
                    return False
                    
                drop_position = self.deck.get_position(location)
                if drop_position is None:
                    logger.error(f"未找到丢弃位置 {location} 的信息")
                    return False
                drop_x, drop_y, drop_z = drop_position
            
            # 验证丢弃位置的安全性
            safe_z = drop_z + self.config.safe_height
            drop_height_z = drop_z + self.config.tip_drop_height
            
            if not (self._validate_position(drop_x, drop_y, safe_z) and 
                    self._validate_position(drop_x, drop_y, drop_height_z)):
                logger.error("枪头丢弃位置超出安全范围")
                return False
            
            if CONTROLLERS_AVAILABLE and self.xyz_controller:
                # 真实硬件控制流程
                logger.info("使用真实XYZ控制器进行枪头丢弃")
                
                try:
                    # 1. 移动到丢弃位置上方的安全高度
                    safe_z = drop_z + self.config.tip_drop_height
                    logger.info(f"移动到丢弃位置上方: ({drop_x:.2f}, {drop_y:.2f}, {safe_z:.2f})")
                    move_success = await asyncio.to_thread(
                        self.xyz_controller.move_to_work_coord,
                        drop_x, drop_y, safe_z
                    )
                    if not move_success:
                        logger.error("移动到丢弃位置上方失败")
                        return False
                    
                    # 2. Z轴下降到丢弃高度
                    logger.info(f"Z轴下降到丢弃高度: {drop_z:.2f}mm")
                    z_down_success = await asyncio.to_thread(
                        self.xyz_controller.move_to_work_coord,
                        drop_x, drop_y, drop_z
                    )
                    if not z_down_success:
                        logger.error("Z轴下降到丢弃位置失败")
                        return False
                    
                    # 3. 执行枪头弹出动作（如果有移液器控制器）
                    if self.pipette_controller:
                        try:
                            # 发送弹出枪头命令
                            await asyncio.to_thread(self.pipette_controller.eject_tip)
                            logger.info("执行枪头弹出命令")
                        except Exception as e:
                            logger.warning(f"枪头弹出命令失败: {e}")
                    
                    # 4. 等待一小段时间确保枪头完全脱离
                    await asyncio.sleep(0.3)
                    
                    # 5. Z轴上升到安全高度
                    logger.info(f"Z轴上升到安全高度: {safe_z:.2f}mm")
                    z_up_success = await asyncio.to_thread(
                        self.xyz_controller.move_to_work_coord,
                        drop_x, drop_y, safe_z
                    )
                    if not z_up_success:
                        logger.error("Z轴上升失败")
                        return False
                    
                    # 6. 更新当前位置
                    self.current_position = (drop_x, drop_y, safe_z)
                    
                except Exception as drop_error:
                    logger.error(f"枪头丢弃过程中发生错误: {drop_error}")
                    # 尝试移动到安全位置
                    if self.config.emergency_stop_enabled:
                        await self.emergency_stop()
                        await self.move_to_safe_position()
                    return False
                
            else:
                # 模拟模式
                logger.info("模拟模式：执行枪头丢弃动作")
                await asyncio.sleep(0.8)  # 模拟整个丢弃过程的时间
                self.current_position = (drop_x, drop_y, drop_z + self.config.tip_drop_height)
            
            # 7. 标记枪头已脱离，清空体积
            self.tip_attached = False
            self.current_volume = 0.0
            logger.info("吸头丢弃成功")
            return True
            
        except Exception as e:
            logger.error(f"丢弃吸头失败: {e}")
            return False
    
    async def aspirate(self, volume: float, location: str) -> bool:
        """吸取液体"""
        try:
            if not self.is_connected:
                raise LaiYuLiquidError("设备未连接")
            
            if not self.tip_attached:
                raise LaiYuLiquidError("没有吸头附着")
            
            if volume <= 0 or volume > self.config.max_volume:
                raise LaiYuLiquidError(f"体积超出范围: {volume}")
            
            # 模拟吸取
            await asyncio.sleep(0.3)
            self.current_volume += volume
            logger.debug(f"从 {location} 吸取 {volume} μL")
            return True
            
        except Exception as e:
            logger.error(f"吸取失败: {e}")
            return False
    
    async def dispense(self, volume: float, location: str) -> bool:
        """分配液体"""
        try:
            if not self.is_connected:
                raise LaiYuLiquidError("设备未连接")
            
            if not self.tip_attached:
                raise LaiYuLiquidError("没有吸头附着")
            
            if volume <= 0 or volume > self.current_volume:
                raise LaiYuLiquidError(f"分配体积无效: {volume}")
            
            # 模拟分配
            await asyncio.sleep(0.3)
            self.current_volume -= volume
            logger.debug(f"向 {location} 分配 {volume} μL")
            return True
            
        except Exception as e:
            logger.error(f"分配失败: {e}")
            return False


class LaiYuLiquid:
    """LaiYu_Liquid 主要接口类"""
    
    def __init__(self, config: Optional[LaiYuLiquidConfig] = None, **kwargs):
        # 如果传入了关键字参数，创建配置对象
        if kwargs and config is None:
            # 从kwargs中提取配置参数
            config_params = {}
            for key, value in kwargs.items():
                if hasattr(LaiYuLiquidConfig, key):
                    config_params[key] = value
            self.config = LaiYuLiquidConfig(**config_params)
        else:
            self.config = config or LaiYuLiquidConfig()
        
        # 先创建deck，然后传递给backend
        self.deck = LaiYuLiquidDeck(self.config)
        self.backend = LaiYuLiquidBackend(self.config, self.deck)
        self.is_setup = False
    
    @property
    def current_position(self) -> Tuple[float, float, float]:
        """获取当前位置"""
        return self.backend.current_position
    
    @property
    def current_volume(self) -> float:
        """获取当前体积"""
        return self.backend.current_volume
    
    @property
    def is_connected(self) -> bool:
        """获取连接状态"""
        return self.backend.is_connected
    
    @property
    def is_initialized(self) -> bool:
        """获取初始化状态"""
        return self.backend.is_initialized
    
    @property
    def tip_attached(self) -> bool:
        """获取吸头附着状态"""
        return self.backend.tip_attached
        
    async def setup(self) -> bool:
        """设置液体处理器"""
        try:
            success = await self.backend.setup()
            if success:
                self.is_setup = True
                logger.info("LaiYu_Liquid 设置完成")
            return success
        except Exception as e:
            logger.error(f"LaiYu_Liquid 设置失败: {e}")
            return False
    
    async def stop(self):
        """停止液体处理器"""
        await self.backend.stop()
        self.is_setup = False
    
    async def transfer(self, source: str, target: str, volume: float, 
                      tip_rack: str = "tip_rack_1", tip_position: int = 0) -> bool:
        """液体转移"""
        try:
            if not self.is_setup:
                raise LaiYuLiquidError("设备未设置")
            
            # 获取源和目标位置
            source_pos = self.deck.get_position(source)
            target_pos = self.deck.get_position(target)
            tip_pos = self.deck.get_position(tip_rack)
            
            if not all([source_pos, target_pos, tip_pos]):
                raise LaiYuLiquidError("位置信息不完整")
            
            # 执行转移步骤
            steps = [
                ("移动到吸头架", self.backend.move_to(*tip_pos)),
                ("拾取吸头", self.backend.pick_up_tip(tip_rack, tip_position)),
                ("移动到源位置", self.backend.move_to(*source_pos)),
                ("吸取液体", self.backend.aspirate(volume, source)),
                ("移动到目标位置", self.backend.move_to(*target_pos)),
                ("分配液体", self.backend.dispense(volume, target)),
                ("丢弃吸头", self.backend.drop_tip())
            ]
            
            for step_name, step_coro in steps:
                logger.debug(f"执行步骤: {step_name}")
                success = await step_coro
                if not success:
                    raise LaiYuLiquidError(f"步骤失败: {step_name}")
            
            logger.info(f"液体转移完成: {source} -> {target}, {volume} μL")
            return True
            
        except Exception as e:
            logger.error(f"液体转移失败: {e}")
            return False
    
    def add_resource(self, name: str, resource_type: str, position: Tuple[float, float, float]):
        """添加资源到工作台"""
        if resource_type == "plate":
            resource = Plate(name)
        elif resource_type == "tip_rack":
            resource = TipRack(name)
        else:
            resource = Resource(name)
        
        self.deck.add_resource(name, resource, position)
    
    def get_status(self) -> Dict[str, Any]:
        """获取设备状态"""
        return {
            "connected": self.backend.is_connected,
            "setup": self.is_setup,
            "current_position": self.backend.current_position,
            "tip_attached": self.backend.tip_attached,
            "current_volume": self.backend.current_volume,
            "resources": self.deck.list_resources()
        }


def create_quick_setup() -> LaiYuLiquidDeck:
    """
    创建快速设置的LaiYu液体处理工作站
    
    Returns:
        LaiYuLiquidDeck: 配置好的工作台实例
    """
    # 创建默认配置
    config = LaiYuLiquidConfig()
    
    # 创建工作台
    deck = LaiYuLiquidDeck(config)
    
    # 导入资源创建函数
    try:
        from .laiyu_liquid_res import (
            create_tip_rack_1000ul,
            create_tip_rack_200ul,
            create_96_well_plate,
            create_waste_container
        )
        
        # 添加基本资源
        tip_rack_1000 = create_tip_rack_1000ul("tip_rack_1000")
        tip_rack_200 = create_tip_rack_200ul("tip_rack_200")
        plate_96 = create_96_well_plate("plate_96")
        waste = create_waste_container("waste")
        
        # 添加到工作台
        deck.add_resource("tip_rack_1000", tip_rack_1000, (50, 50, 0))
        deck.add_resource("tip_rack_200", tip_rack_200, (150, 50, 0))
        deck.add_resource("plate_96", plate_96, (250, 50, 0))
        deck.add_resource("waste", waste, (50, 150, 0))
        
    except ImportError:
        # 如果资源模块不可用，创建空的工作台
        logger.warning("资源模块不可用，创建空的工作台")
    
    return deck


__all__ = [
    "LaiYuLiquid",
    "LaiYuLiquidBackend", 
    "LaiYuLiquidConfig",
    "LaiYuLiquidDeck",
    "LaiYuLiquidContainer",
    "LaiYuLiquidTipRack",
    "LaiYuLiquidError",
    "create_quick_setup",
    "get_module_info"
]