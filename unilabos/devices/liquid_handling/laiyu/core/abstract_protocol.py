"""
LaiYu_Liquid 抽象协议实现

该模块提供了液体资源管理和转移的抽象协议，包括：
- MaterialResource: 液体资源管理类
- transfer_liquid: 液体转移函数
- 相关的辅助类和函数

主要功能：
- 管理多孔位的液体资源
- 计算和跟踪液体体积
- 处理液体转移操作
- 提供资源状态查询
"""

import logging
from typing import Dict, List, Optional, Union, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import uuid
import time

# pylabrobot 导入
from pylabrobot.resources import Resource, Well, Plate

logger = logging.getLogger(__name__)


class LiquidType(Enum):
    """液体类型枚举"""
    WATER = "water"
    ETHANOL = "ethanol"
    DMSO = "dmso"
    BUFFER = "buffer"
    SAMPLE = "sample"
    REAGENT = "reagent"
    WASTE = "waste"
    UNKNOWN = "unknown"


@dataclass
class LiquidInfo:
    """液体信息类"""
    liquid_type: LiquidType = LiquidType.UNKNOWN
    volume: float = 0.0  # 体积 (μL)
    concentration: Optional[float] = None  # 浓度 (mg/ml, M等)
    ph: Optional[float] = None  # pH值
    temperature: Optional[float] = None  # 温度 (°C)
    viscosity: Optional[float] = None  # 粘度 (cP)
    density: Optional[float] = None  # 密度 (g/ml)
    description: str = ""  # 描述信息
    
    def __str__(self) -> str:
        return f"{self.liquid_type.value}({self.description})"


@dataclass
class WellContent:
    """孔位内容类"""
    volume: float = 0.0  # 当前体积 (ul)
    max_volume: float = 1000.0  # 最大容量 (ul)
    liquid_info: LiquidInfo = field(default_factory=LiquidInfo)
    last_updated: float = field(default_factory=time.time)
    
    @property
    def is_empty(self) -> bool:
        """检查是否为空"""
        return self.volume <= 0.0
    
    @property
    def is_full(self) -> bool:
        """检查是否已满"""
        return self.volume >= self.max_volume
    
    @property
    def available_volume(self) -> float:
        """可用体积"""
        return max(0.0, self.max_volume - self.volume)
    
    @property
    def fill_percentage(self) -> float:
        """填充百分比"""
        return (self.volume / self.max_volume) * 100.0 if self.max_volume > 0 else 0.0
    
    def can_add_volume(self, volume: float) -> bool:
        """检查是否可以添加指定体积"""
        return (self.volume + volume) <= self.max_volume
    
    def can_remove_volume(self, volume: float) -> bool:
        """检查是否可以移除指定体积"""
        return self.volume >= volume
    
    def add_volume(self, volume: float, liquid_info: Optional[LiquidInfo] = None) -> bool:
        """
        添加液体体积
        
        Args:
            volume: 要添加的体积 (ul)
            liquid_info: 液体信息
            
        Returns:
            bool: 是否成功添加
        """
        if not self.can_add_volume(volume):
            return False
        
        self.volume += volume
        if liquid_info:
            self.liquid_info = liquid_info
        self.last_updated = time.time()
        return True
    
    def remove_volume(self, volume: float) -> bool:
        """
        移除液体体积
        
        Args:
            volume: 要移除的体积 (ul)
            
        Returns:
            bool: 是否成功移除
        """
        if not self.can_remove_volume(volume):
            return False
        
        self.volume -= volume
        self.last_updated = time.time()
        
        # 如果完全清空，重置液体信息
        if self.volume <= 0.0:
            self.volume = 0.0
            self.liquid_info = LiquidInfo()
        
        return True


class MaterialResource:
    """
    液体资源管理类
    
    该类用于管理液体处理过程中的资源状态，包括：
    - 跟踪多个孔位的液体体积和类型
    - 计算总体积和可用体积
    - 处理液体的添加和移除
    - 提供资源状态查询
    """
    
    def __init__(
        self,
        resource: Resource,
        wells: Optional[List[Well]] = None,
        default_max_volume: float = 1000.0
    ):
        """
        初始化材料资源
        
        Args:
            resource: pylabrobot 资源对象
            wells: 孔位列表，如果为None则自动获取
            default_max_volume: 默认最大体积 (ul)
        """
        self.resource = resource
        self.resource_id = str(uuid.uuid4())
        self.default_max_volume = default_max_volume
        
        # 获取孔位列表
        if wells is None:
            if hasattr(resource, 'get_wells'):
                self.wells = resource.get_wells()
            elif hasattr(resource, 'wells'):
                self.wells = resource.wells
            else:
                # 如果没有孔位，创建一个虚拟孔位
                self.wells = [resource]
        else:
            self.wells = wells
        
        # 初始化孔位内容
        self.well_contents: Dict[str, WellContent] = {}
        for well in self.wells:
            well_id = self._get_well_id(well)
            self.well_contents[well_id] = WellContent(
                max_volume=default_max_volume
            )
        
        logger.info(f"初始化材料资源: {resource.name}, 孔位数: {len(self.wells)}")
    
    def _get_well_id(self, well: Union[Well, Resource]) -> str:
        """获取孔位ID"""
        if hasattr(well, 'name'):
            return well.name
        else:
            return str(id(well))
    
    @property
    def name(self) -> str:
        """资源名称"""
        return self.resource.name
    
    @property
    def total_volume(self) -> float:
        """总液体体积"""
        return sum(content.volume for content in self.well_contents.values())
    
    @property
    def total_max_volume(self) -> float:
        """总最大容量"""
        return sum(content.max_volume for content in self.well_contents.values())
    
    @property
    def available_volume(self) -> float:
        """总可用体积"""
        return sum(content.available_volume for content in self.well_contents.values())
    
    @property
    def well_count(self) -> int:
        """孔位数量"""
        return len(self.wells)
    
    @property
    def empty_wells(self) -> List[str]:
        """空孔位列表"""
        return [well_id for well_id, content in self.well_contents.items() 
                if content.is_empty]
    
    @property
    def full_wells(self) -> List[str]:
        """满孔位列表"""
        return [well_id for well_id, content in self.well_contents.items() 
                if content.is_full]
    
    @property
    def occupied_wells(self) -> List[str]:
        """有液体的孔位列表"""
        return [well_id for well_id, content in self.well_contents.items() 
                if not content.is_empty]
    
    def get_well_content(self, well_id: str) -> Optional[WellContent]:
        """获取指定孔位的内容"""
        return self.well_contents.get(well_id)
    
    def get_well_volume(self, well_id: str) -> float:
        """获取指定孔位的体积"""
        content = self.get_well_content(well_id)
        return content.volume if content else 0.0
    
    def set_well_volume(
        self,
        well_id: str,
        volume: float,
        liquid_info: Optional[LiquidInfo] = None
    ) -> bool:
        """
        设置指定孔位的体积
        
        Args:
            well_id: 孔位ID
            volume: 体积 (ul)
            liquid_info: 液体信息
            
        Returns:
            bool: 是否成功设置
        """
        if well_id not in self.well_contents:
            logger.error(f"孔位 {well_id} 不存在")
            return False
        
        content = self.well_contents[well_id]
        if volume > content.max_volume:
            logger.error(f"体积 {volume} 超过最大容量 {content.max_volume}")
            return False
        
        content.volume = max(0.0, volume)
        if liquid_info:
            content.liquid_info = liquid_info
        content.last_updated = time.time()
        
        logger.info(f"设置孔位 {well_id} 体积: {volume}ul")
        return True
    
    def add_liquid(
        self,
        well_id: str,
        volume: float,
        liquid_info: Optional[LiquidInfo] = None
    ) -> bool:
        """
        向指定孔位添加液体
        
        Args:
            well_id: 孔位ID
            volume: 添加的体积 (ul)
            liquid_info: 液体信息
            
        Returns:
            bool: 是否成功添加
        """
        if well_id not in self.well_contents:
            logger.error(f"孔位 {well_id} 不存在")
            return False
        
        content = self.well_contents[well_id]
        success = content.add_volume(volume, liquid_info)
        
        if success:
            logger.info(f"向孔位 {well_id} 添加 {volume}ul 液体")
        else:
            logger.error(f"无法向孔位 {well_id} 添加 {volume}ul 液体")
        
        return success
    
    def remove_liquid(self, well_id: str, volume: float) -> bool:
        """
        从指定孔位移除液体
        
        Args:
            well_id: 孔位ID
            volume: 移除的体积 (ul)
            
        Returns:
            bool: 是否成功移除
        """
        if well_id not in self.well_contents:
            logger.error(f"孔位 {well_id} 不存在")
            return False
        
        content = self.well_contents[well_id]
        success = content.remove_volume(volume)
        
        if success:
            logger.info(f"从孔位 {well_id} 移除 {volume}ul 液体")
        else:
            logger.error(f"无法从孔位 {well_id} 移除 {volume}ul 液体")
        
        return success
    
    def find_wells_with_volume(self, min_volume: float) -> List[str]:
        """
        查找具有指定最小体积的孔位
        
        Args:
            min_volume: 最小体积 (ul)
            
        Returns:
            List[str]: 符合条件的孔位ID列表
        """
        return [well_id for well_id, content in self.well_contents.items()
                if content.volume >= min_volume]
    
    def find_wells_with_space(self, min_space: float) -> List[str]:
        """
        查找具有指定最小空间的孔位
        
        Args:
            min_space: 最小空间 (ul)
            
        Returns:
            List[str]: 符合条件的孔位ID列表
        """
        return [well_id for well_id, content in self.well_contents.items()
                if content.available_volume >= min_space]
    
    def get_status_summary(self) -> Dict[str, Any]:
        """获取资源状态摘要"""
        return {
            "resource_name": self.name,
            "resource_id": self.resource_id,
            "well_count": self.well_count,
            "total_volume": self.total_volume,
            "total_max_volume": self.total_max_volume,
            "available_volume": self.available_volume,
            "fill_percentage": (self.total_volume / self.total_max_volume) * 100.0,
            "empty_wells": len(self.empty_wells),
            "full_wells": len(self.full_wells),
            "occupied_wells": len(self.occupied_wells)
        }
    
    def get_detailed_status(self) -> Dict[str, Any]:
        """获取详细状态信息"""
        well_details = {}
        for well_id, content in self.well_contents.items():
            well_details[well_id] = {
                "volume": content.volume,
                "max_volume": content.max_volume,
                "available_volume": content.available_volume,
                "fill_percentage": content.fill_percentage,
                "liquid_type": content.liquid_info.liquid_type.value,
                "description": content.liquid_info.description,
                "last_updated": content.last_updated
            }
        
        return {
            "summary": self.get_status_summary(),
            "wells": well_details
        }


def transfer_liquid(
    source: MaterialResource,
    target: MaterialResource,
    volume: float,
    source_well_id: Optional[str] = None,
    target_well_id: Optional[str] = None,
    liquid_info: Optional[LiquidInfo] = None
) -> bool:
    """
    在两个材料资源之间转移液体
    
    Args:
        source: 源资源
        target: 目标资源
        volume: 转移体积 (ul)
        source_well_id: 源孔位ID，如果为None则自动选择
        target_well_id: 目标孔位ID，如果为None则自动选择
        liquid_info: 液体信息
        
    Returns:
        bool: 转移是否成功
    """
    try:
        # 自动选择源孔位
        if source_well_id is None:
            available_wells = source.find_wells_with_volume(volume)
            if not available_wells:
                logger.error(f"源资源 {source.name} 没有足够体积的孔位")
                return False
            source_well_id = available_wells[0]
        
        # 自动选择目标孔位
        if target_well_id is None:
            available_wells = target.find_wells_with_space(volume)
            if not available_wells:
                logger.error(f"目标资源 {target.name} 没有足够空间的孔位")
                return False
            target_well_id = available_wells[0]
        
        # 检查源孔位是否有足够液体
        if not source.get_well_content(source_well_id).can_remove_volume(volume):
            logger.error(f"源孔位 {source_well_id} 液体不足")
            return False
        
        # 检查目标孔位是否有足够空间
        if not target.get_well_content(target_well_id).can_add_volume(volume):
            logger.error(f"目标孔位 {target_well_id} 空间不足")
            return False
        
        # 获取源液体信息
        source_content = source.get_well_content(source_well_id)
        transfer_liquid_info = liquid_info or source_content.liquid_info
        
        # 执行转移
        if source.remove_liquid(source_well_id, volume):
            if target.add_liquid(target_well_id, volume, transfer_liquid_info):
                logger.info(f"成功转移 {volume}ul 液体: {source.name}[{source_well_id}] -> {target.name}[{target_well_id}]")
                return True
            else:
                # 如果目标添加失败，回滚源操作
                source.add_liquid(source_well_id, volume, source_content.liquid_info)
                logger.error("目标添加失败，已回滚源操作")
                return False
        else:
            logger.error("源移除失败")
            return False
    
    except Exception as e:
        logger.error(f"液体转移失败: {e}")
        return False


def create_material_resource(
    name: str,
    resource: Resource,
    initial_volumes: Optional[Dict[str, float]] = None,
    liquid_info: Optional[LiquidInfo] = None,
    max_volume: float = 1000.0
) -> MaterialResource:
    """
    创建材料资源的便捷函数
    
    Args:
        name: 资源名称
        resource: pylabrobot 资源对象
        initial_volumes: 初始体积字典 {well_id: volume}
        liquid_info: 液体信息
        max_volume: 最大体积
        
    Returns:
        MaterialResource: 创建的材料资源
    """
    material_resource = MaterialResource(
        resource=resource,
        default_max_volume=max_volume
    )
    
    # 设置初始体积
    if initial_volumes:
        for well_id, volume in initial_volumes.items():
            material_resource.set_well_volume(well_id, volume, liquid_info)
    
    return material_resource


def batch_transfer_liquid(
    transfers: List[Tuple[MaterialResource, MaterialResource, float]],
    liquid_info: Optional[LiquidInfo] = None
) -> List[bool]:
    """
    批量液体转移
    
    Args:
        transfers: 转移列表 [(source, target, volume), ...]
        liquid_info: 液体信息
        
    Returns:
        List[bool]: 每个转移操作的结果
    """
    results = []
    
    for source, target, volume in transfers:
        result = transfer_liquid(source, target, volume, liquid_info=liquid_info)
        results.append(result)
        
        if not result:
            logger.warning(f"批量转移中的操作失败: {source.name} -> {target.name}")
    
    success_count = sum(results)
    logger.info(f"批量转移完成: {success_count}/{len(transfers)} 成功")
    
    return results