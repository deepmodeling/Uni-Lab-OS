"""
LaiYu_Liquid 资源定义模块

该模块提供了 LaiYu_Liquid 工作站专用的资源定义函数，包括：
- 各种规格的枪头架
- 不同类型的板和容器
- 特殊功能位置
- 资源创建的便捷函数

所有资源都基于 deck.json 中的配置参数创建。
"""

import json
import os
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

# PyLabRobot 资源导入
try:
    from pylabrobot.resources import (
        Resource, Deck, Plate, TipRack, Container, Tip,
        Coordinate
    )
    from pylabrobot.resources.tip_rack import TipSpot
    from pylabrobot.resources.well import Well as PlateWell
    PYLABROBOT_AVAILABLE = True
except ImportError:
    # 如果 PyLabRobot 不可用，创建模拟类
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
    
    class Container(Resource):
        pass
    
    class Tip(Resource):
        pass
    
    class TipSpot(Resource):
        def __init__(self, name: str, **kwargs):
            super().__init__(name)
            # 忽略其他参数
    
    class PlateWell(Resource):
        pass
    
    class Coordinate:
        def __init__(self, x: float, y: float, z: float):
            self.x = x
            self.y = y
            self.z = z

# 本地导入
from .LaiYu_Liquid import LaiYuLiquidDeck, LaiYuLiquidContainer, LaiYuLiquidTipRack


def load_deck_config() -> Dict[str, Any]:
    """
    加载工作台配置文件
    
    Returns:
        Dict[str, Any]: 配置字典
    """
    # 优先使用最新的deckconfig.json文件
    config_path = Path(__file__).parent / "controllers" / "deckconfig.json"
    
    # 如果最新配置文件不存在，回退到旧配置文件
    if not config_path.exists():
        config_path = Path(__file__).parent / "config" / "deck.json"
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        # 如果找不到配置文件，返回默认配置
        return {
            "name": "LaiYu_Liquid_Deck",
            "size_x": 340.0,  
            "size_y": 250.0,
            "size_z": 160.0
        }


# 加载配置
DECK_CONFIG = load_deck_config()


class LaiYuTipRack1000(LaiYuLiquidTipRack):
    """1000μL 枪头架"""
    
    def __init__(self, name: str):
        """
        初始化1000μL枪头架
        
        Args:
            name: 枪头架名称
        """
        super().__init__(
            name=name,
            size_x=127.76,
            size_y=85.48,
            size_z=30.0,
            tip_count=96,
            tip_volume=1000.0
        )
        
        # 创建枪头位置
        self._create_tip_spots(
            tip_count=96,
            tip_spacing=9.0,
            tip_type="1000ul"
        )
    
    def _create_tip_spots(self, tip_count: int, tip_spacing: float, tip_type: str):
        """
        创建枪头位置 - 从配置文件中读取绝对坐标
        
        Args:
            tip_count: 枪头数量
            tip_spacing: 枪头间距
            tip_type: 枪头类型
        """
        # 从配置文件中获取枪头架的孔位信息
        config = DECK_CONFIG
        tip_module = None
        
        # 查找枪头架模块
        for module in config.get("children", []):
            if module.get("type") == "tip_rack":
                tip_module = module
                break
        
        if not tip_module:
            # 如果配置文件中没有找到，使用默认的相对坐标计算
            rows = 8
            cols = 12
            
            for row in range(rows):
                for col in range(cols):
                    spot_name = f"{chr(65 + row)}{col + 1:02d}"
                    x = col * tip_spacing + tip_spacing / 2
                    y = row * tip_spacing + tip_spacing / 2
                    
                    # 创建枪头 - 根据PyLabRobot或模拟类使用不同参数
                    if PYLABROBOT_AVAILABLE:
                        # PyLabRobot的Tip需要特定参数
                        tip = Tip(
                            has_filter=False,
                            total_tip_length=95.0,  # 1000ul枪头长度
                            maximal_volume=1000.0,  # 最大体积
                            fitting_depth=8.0       # 安装深度
                        )
                    else:
                        # 模拟类只需要name
                        tip = Tip(name=f"tip_{spot_name}")
                    
                    # 创建枪头位置
                    if PYLABROBOT_AVAILABLE:
                        # PyLabRobot的TipSpot需要特定参数
                        tip_spot = TipSpot(
                            name=spot_name,
                            size_x=9.0,  # 枪头位置宽度
                            size_y=9.0,  # 枪头位置深度
                            size_z=95.0,  # 枪头位置高度
                            make_tip=lambda: tip  # 创建枪头的函数
                        )
                    else:
                        # 模拟类只需要name
                        tip_spot = TipSpot(name=spot_name)
                    
                    # 将吸头位置分配到吸头架
                    self.assign_child_resource(
                        tip_spot,
                        location=Coordinate(x, y, 0)
                    )
            return
        
        # 使用配置文件中的绝对坐标
        module_position = tip_module.get("position", {"x": 0, "y": 0, "z": 0})
        
        for well_config in tip_module.get("wells", []):
            spot_name = well_config["id"]
            well_pos = well_config["position"]
            
            # 计算相对于模块的坐标（绝对坐标减去模块位置）
            relative_x = well_pos["x"] - module_position["x"]
            relative_y = well_pos["y"] - module_position["y"]
            relative_z = well_pos["z"] - module_position["z"]
            
            # 创建枪头 - 根据PyLabRobot或模拟类使用不同参数
            if PYLABROBOT_AVAILABLE:
                # PyLabRobot的Tip需要特定参数
                tip = Tip(
                    has_filter=False,
                    total_tip_length=95.0,  # 1000ul枪头长度
                    maximal_volume=1000.0,  # 最大体积
                    fitting_depth=8.0       # 安装深度
                )
            else:
                # 模拟类只需要name
                tip = Tip(name=f"tip_{spot_name}")
            
            # 创建枪头位置
            if PYLABROBOT_AVAILABLE:
                # PyLabRobot的TipSpot需要特定参数
                tip_spot = TipSpot(
                    name=spot_name,
                    size_x=well_config.get("diameter", 9.0),  # 使用配置中的直径
                    size_y=well_config.get("diameter", 9.0),
                    size_z=well_config.get("depth", 95.0),    # 使用配置中的深度
                    make_tip=lambda: tip  # 创建枪头的函数
                )
            else:
                # 模拟类只需要name
                tip_spot = TipSpot(name=spot_name)
            
            # 将吸头位置分配到吸头架
            self.assign_child_resource(
                tip_spot,
                location=Coordinate(relative_x, relative_y, relative_z)
            )
            
            # 注意：在PyLabRobot中，Tip不是Resource，不需要分配给TipSpot
            # TipSpot的make_tip函数会在需要时创建Tip


class LaiYuTipRack200(LaiYuLiquidTipRack):
    """200μL 枪头架"""
    
    def __init__(self, name: str):
        """
        初始化200μL枪头架
        
        Args:
            name: 枪头架名称
        """
        super().__init__(
            name=name,
            size_x=127.76,
            size_y=85.48,
            size_z=30.0,
            tip_count=96,
            tip_volume=200.0
        )
        
        # 创建枪头位置
        self._create_tip_spots(
            tip_count=96,
            tip_spacing=9.0,
            tip_type="200ul"
        )
    
    def _create_tip_spots(self, tip_count: int, tip_spacing: float, tip_type: str):
        """
        创建枪头位置
        
        Args:
            tip_count: 枪头数量
            tip_spacing: 枪头间距
            tip_type: 枪头类型
        """
        rows = 8
        cols = 12
        
        for row in range(rows):
            for col in range(cols):
                spot_name = f"{chr(65 + row)}{col + 1:02d}"
                x = col * tip_spacing + tip_spacing / 2
                y = row * tip_spacing + tip_spacing / 2
                
                # 创建枪头 - 根据PyLabRobot或模拟类使用不同参数
                if PYLABROBOT_AVAILABLE:
                    # PyLabRobot的Tip需要特定参数
                    tip = Tip(
                        has_filter=False,
                        total_tip_length=72.0,  # 200ul枪头长度
                        maximal_volume=200.0,   # 最大体积
                        fitting_depth=8.0       # 安装深度
                    )
                else:
                    # 模拟类只需要name
                    tip = Tip(name=f"tip_{spot_name}")
                
                # 创建枪头位置
                if PYLABROBOT_AVAILABLE:
                    # PyLabRobot的TipSpot需要特定参数
                    tip_spot = TipSpot(
                        name=spot_name,
                        size_x=9.0,  # 枪头位置宽度
                        size_y=9.0,  # 枪头位置深度
                        size_z=72.0,  # 枪头位置高度
                        make_tip=lambda: tip  # 创建枪头的函数
                    )
                else:
                    # 模拟类只需要name
                    tip_spot = TipSpot(name=spot_name)
                
                # 将吸头位置分配到吸头架
                self.assign_child_resource(
                    tip_spot,
                    location=Coordinate(x, y, 0)
                )
                
                # 注意：在PyLabRobot中，Tip不是Resource，不需要分配给TipSpot
                # TipSpot的make_tip函数会在需要时创建Tip


class LaiYu96WellPlate(LaiYuLiquidContainer):
    """96孔板"""
    
    def __init__(self, name: str, lid_height: float = 0.0):
        """
        初始化96孔板
        
        Args:
            name: 板名称
            lid_height: 盖子高度
        """
        super().__init__(
            name=name,
            size_x=127.76,
            size_y=85.48,
            size_z=14.22,
            container_type="96_well_plate",
            volume=0.0,
            max_volume=200.0,
            lid_height=lid_height
        )
        
        # 创建孔位
        self._create_wells(
            well_count=96,
            well_volume=200.0,
            well_spacing=9.0
        )
    
    def get_size_z(self) -> float:
        """获取孔位深度"""
        return 10.0  # 96孔板孔位深度
    
    def _create_wells(self, well_count: int, well_volume: float, well_spacing: float):
        """
        创建孔位 - 从配置文件中读取绝对坐标
        
        Args:
            well_count: 孔位数量
            well_volume: 孔位体积
            well_spacing: 孔位间距
        """
        # 从配置文件中获取96孔板的孔位信息
        config = DECK_CONFIG
        plate_module = None
        
        # 查找96孔板模块
        for module in config.get("children", []):
            if module.get("type") == "96_well_plate":
                plate_module = module
                break
        
        if not plate_module:
            # 如果配置文件中没有找到，使用默认的相对坐标计算
            rows = 8
            cols = 12
            
            for row in range(rows):
                for col in range(cols):
                    well_name = f"{chr(65 + row)}{col + 1:02d}"
                    x = col * well_spacing + well_spacing / 2
                    y = row * well_spacing + well_spacing / 2
                    
                    # 创建孔位
                    well = PlateWell(
                        name=well_name,
                        size_x=well_spacing * 0.8,
                        size_y=well_spacing * 0.8,
                        size_z=self.get_size_z(),
                        max_volume=well_volume
                    )
                    
                    # 添加到板
                    self.assign_child_resource(
                        well,
                        location=Coordinate(x, y, 0)
                    )
            return
        
        # 使用配置文件中的绝对坐标
        module_position = plate_module.get("position", {"x": 0, "y": 0, "z": 0})
        
        for well_config in plate_module.get("wells", []):
            well_name = well_config["id"]
            well_pos = well_config["position"]
            
            # 计算相对于模块的坐标（绝对坐标减去模块位置）
            relative_x = well_pos["x"] - module_position["x"]
            relative_y = well_pos["y"] - module_position["y"]
            relative_z = well_pos["z"] - module_position["z"]
            
            # 创建孔位
            well = PlateWell(
                name=well_name,
                size_x=well_config.get("diameter", 8.2) * 0.8,  # 使用配置中的直径
                size_y=well_config.get("diameter", 8.2) * 0.8,
                size_z=well_config.get("depth", self.get_size_z()),
                max_volume=well_config.get("volume", well_volume)
            )
            
            # 添加到板
            self.assign_child_resource(
                well,
                location=Coordinate(relative_x, relative_y, relative_z)
            )


class LaiYuDeepWellPlate(LaiYuLiquidContainer):
    """深孔板"""
    
    def __init__(self, name: str, lid_height: float = 0.0):
        """
        初始化深孔板
        
        Args:
            name: 板名称
            lid_height: 盖子高度
        """
        super().__init__(
            name=name,
            size_x=127.76,
            size_y=85.48,
            size_z=41.3,
            container_type="deep_well_plate",
            volume=0.0,
            max_volume=2000.0,
            lid_height=lid_height
        )
        
        # 创建孔位
        self._create_wells(
            well_count=96,
            well_volume=2000.0,
            well_spacing=9.0
        )
    
    def get_size_z(self) -> float:
        """获取孔位深度"""
        return 35.0  # 深孔板孔位深度
    
    def _create_wells(self, well_count: int, well_volume: float, well_spacing: float):
        """
        创建孔位 - 从配置文件中读取绝对坐标
        
        Args:
            well_count: 孔位数量
            well_volume: 孔位体积
            well_spacing: 孔位间距
        """
        # 从配置文件中获取深孔板的孔位信息
        config = DECK_CONFIG
        plate_module = None
        
        # 查找深孔板模块（通常是第二个96孔板模块）
        plate_modules = []
        for module in config.get("children", []):
            if module.get("type") == "96_well_plate":
                plate_modules.append(module)
        
        # 如果有多个96孔板模块，选择第二个作为深孔板
        if len(plate_modules) > 1:
            plate_module = plate_modules[1]
        elif len(plate_modules) == 1:
            plate_module = plate_modules[0]
        
        if not plate_module:
            # 如果配置文件中没有找到，使用默认的相对坐标计算
            rows = 8
            cols = 12
            
            for row in range(rows):
                for col in range(cols):
                    well_name = f"{chr(65 + row)}{col + 1:02d}"
                    x = col * well_spacing + well_spacing / 2
                    y = row * well_spacing + well_spacing / 2
                    
                    # 创建孔位
                    well = PlateWell(
                        name=well_name,
                        size_x=well_spacing * 0.8,
                        size_y=well_spacing * 0.8,
                        size_z=self.get_size_z(),
                        max_volume=well_volume
                    )
                    
                    # 添加到板
                    self.assign_child_resource(
                        well,
                        location=Coordinate(x, y, 0)
                    )
            return
        
        # 使用配置文件中的绝对坐标
        module_position = plate_module.get("position", {"x": 0, "y": 0, "z": 0})
        
        for well_config in plate_module.get("wells", []):
            well_name = well_config["id"]
            well_pos = well_config["position"]
            
            # 计算相对于模块的坐标（绝对坐标减去模块位置）
            relative_x = well_pos["x"] - module_position["x"]
            relative_y = well_pos["y"] - module_position["y"]
            relative_z = well_pos["z"] - module_position["z"]
            
            # 创建孔位
            well = PlateWell(
                name=well_name,
                size_x=well_config.get("diameter", 8.2) * 0.8,  # 使用配置中的直径
                size_y=well_config.get("diameter", 8.2) * 0.8,
                size_z=well_config.get("depth", self.get_size_z()),
                max_volume=well_config.get("volume", well_volume)
            )
            
            # 添加到板
            self.assign_child_resource(
                well,
                location=Coordinate(relative_x, relative_y, relative_z)
            )


class LaiYuWasteContainer(Container):
    """废液容器"""
    
    def __init__(self, name: str):
        """
        初始化废液容器
        
        Args:
            name: 容器名称
        """
        super().__init__(
            name=name,
            size_x=100.0,
            size_y=100.0,
            size_z=50.0,
            max_volume=5000.0
        )


class LaiYuWashContainer(Container):
    """清洗容器"""
    
    def __init__(self, name: str):
        """
        初始化清洗容器
        
        Args:
            name: 容器名称
        """
        super().__init__(
            name=name,
            size_x=100.0,
            size_y=100.0,
            size_z=50.0,
            max_volume=5000.0
        )


class LaiYuReagentContainer(Container):
    """试剂容器"""
    
    def __init__(self, name: str):
        """
        初始化试剂容器
        
        Args:
            name: 容器名称
        """
        super().__init__(
            name=name,
            size_x=50.0,
            size_y=50.0,
            size_z=100.0,
            max_volume=2000.0
        )


class LaiYu8TubeRack(LaiYuLiquidContainer):
    """8管试管架"""
    
    def __init__(self, name: str):
        """
        初始化8管试管架
        
        Args:
            name: 试管架名称
        """
        super().__init__(
            name=name,
            size_x=151.0,
            size_y=75.0,
            size_z=75.0,
            container_type="tube_rack",
            volume=0.0,
            max_volume=77000.0
        )
        
        # 创建孔位
        self._create_wells(
            well_count=8,
            well_volume=77000.0,
            well_spacing=35.0
        )
    
    def get_size_z(self) -> float:
        """获取孔位深度"""
        return 117.0  # 试管深度
    
    def _create_wells(self, well_count: int, well_volume: float, well_spacing: float):
        """
        创建孔位 - 从配置文件中读取绝对坐标
        
        Args:
            well_count: 孔位数量
            well_volume: 孔位体积
            well_spacing: 孔位间距
        """
        # 从配置文件中获取8管试管架的孔位信息
        config = DECK_CONFIG
        tube_module = None
        
        # 查找8管试管架模块
        for module in config.get("children", []):
            if module.get("type") == "tube_rack":
                tube_module = module
                break
        
        if not tube_module:
            # 如果配置文件中没有找到，使用默认的相对坐标计算
            rows = 2
            cols = 4
            
            for row in range(rows):
                for col in range(cols):
                    well_name = f"{chr(65 + row)}{col + 1}"
                    x = col * well_spacing + well_spacing / 2
                    y = row * well_spacing + well_spacing / 2
                    
                    # 创建孔位
                    well = PlateWell(
                        name=well_name,
                        size_x=29.0,
                        size_y=29.0,
                        size_z=self.get_size_z(),
                        max_volume=well_volume
                    )
                    
                    # 添加到试管架
                    self.assign_child_resource(
                        well,
                        location=Coordinate(x, y, 0)
                    )
            return
        
        # 使用配置文件中的绝对坐标
        module_position = tube_module.get("position", {"x": 0, "y": 0, "z": 0})
        
        for well_config in tube_module.get("wells", []):
            well_name = well_config["id"]
            well_pos = well_config["position"]
            
            # 计算相对于模块的坐标（绝对坐标减去模块位置）
            relative_x = well_pos["x"] - module_position["x"]
            relative_y = well_pos["y"] - module_position["y"]
            relative_z = well_pos["z"] - module_position["z"]
            
            # 创建孔位
            well = PlateWell(
                name=well_name,
                size_x=well_config.get("diameter", 29.0),
                size_y=well_config.get("diameter", 29.0),
                size_z=well_config.get("depth", self.get_size_z()),
                max_volume=well_config.get("volume", well_volume)
            )
            
            # 添加到试管架
            self.assign_child_resource(
                well,
                location=Coordinate(relative_x, relative_y, relative_z)
            )


class LaiYuTipDisposal(Resource):
    """枪头废料位置"""
    
    def __init__(self, name: str):
        """
        初始化枪头废料位置
        
        Args:
            name: 位置名称
        """
        super().__init__(
            name=name,
            size_x=100.0,
            size_y=100.0,
            size_z=50.0
        )


class LaiYuMaintenancePosition(Resource):
    """维护位置"""
    
    def __init__(self, name: str):
        """
        初始化维护位置
        
        Args:
            name: 位置名称
        """
        super().__init__(
            name=name,
            size_x=50.0,
            size_y=50.0,
            size_z=100.0
        )


# 资源创建函数
def create_tip_rack_1000ul(name: str = "tip_rack_1000ul") -> LaiYuTipRack1000:
    """
    创建1000μL枪头架
    
    Args:
        name: 枪头架名称
        
    Returns:
        LaiYuTipRack1000: 1000μL枪头架实例
    """
    return LaiYuTipRack1000(name)


def create_tip_rack_200ul(name: str = "tip_rack_200ul") -> LaiYuTipRack200:
    """
    创建200μL枪头架
    
    Args:
        name: 枪头架名称
        
    Returns:
        LaiYuTipRack200: 200μL枪头架实例
    """
    return LaiYuTipRack200(name)


def create_96_well_plate(name: str = "96_well_plate", lid_height: float = 0.0) -> LaiYu96WellPlate:
    """
    创建96孔板
    
    Args:
        name: 板名称
        lid_height: 盖子高度
        
    Returns:
        LaiYu96WellPlate: 96孔板实例
    """
    return LaiYu96WellPlate(name, lid_height)


def create_deep_well_plate(name: str = "deep_well_plate", lid_height: float = 0.0) -> LaiYuDeepWellPlate:
    """
    创建深孔板
    
    Args:
        name: 板名称
        lid_height: 盖子高度
        
    Returns:
        LaiYuDeepWellPlate: 深孔板实例
    """
    return LaiYuDeepWellPlate(name, lid_height)


def create_8_tube_rack(name: str = "8_tube_rack") -> LaiYu8TubeRack:
    """
    创建8管试管架
    
    Args:
        name: 试管架名称
        
    Returns:
        LaiYu8TubeRack: 8管试管架实例
    """
    return LaiYu8TubeRack(name)


def create_waste_container(name: str = "waste_container") -> LaiYuWasteContainer:
    """
    创建废液容器
    
    Args:
        name: 容器名称
        
    Returns:
        LaiYuWasteContainer: 废液容器实例
    """
    return LaiYuWasteContainer(name)


def create_wash_container(name: str = "wash_container") -> LaiYuWashContainer:
    """
    创建清洗容器
    
    Args:
        name: 容器名称
        
    Returns:
        LaiYuWashContainer: 清洗容器实例
    """
    return LaiYuWashContainer(name)


def create_reagent_container(name: str = "reagent_container") -> LaiYuReagentContainer:
    """
    创建试剂容器
    
    Args:
        name: 容器名称
        
    Returns:
        LaiYuReagentContainer: 试剂容器实例
    """
    return LaiYuReagentContainer(name)


def create_tip_disposal(name: str = "tip_disposal") -> LaiYuTipDisposal:
    """
    创建枪头废料位置
    
    Args:
        name: 位置名称
        
    Returns:
        LaiYuTipDisposal: 枪头废料位置实例
    """
    return LaiYuTipDisposal(name)


def create_maintenance_position(name: str = "maintenance_position") -> LaiYuMaintenancePosition:
    """
    创建维护位置
    
    Args:
        name: 位置名称
        
    Returns:
        LaiYuMaintenancePosition: 维护位置实例
    """
    return LaiYuMaintenancePosition(name)


def create_standard_deck() -> LaiYuLiquidDeck:
    """
    创建标准工作台配置
    
    Returns:
        LaiYuLiquidDeck: 配置好的工作台实例
    """
    # 从配置文件创建工作台
    deck = LaiYuLiquidDeck(config=DECK_CONFIG)
    
    return deck


def get_resource_by_name(deck: LaiYuLiquidDeck, name: str) -> Optional[Resource]:
    """
    根据名称获取资源
    
    Args:
        deck: 工作台实例
        name: 资源名称
        
    Returns:
        Optional[Resource]: 找到的资源，如果不存在则返回None
    """
    for child in deck.children:
        if child.name == name:
            return child
    return None


def get_resources_by_type(deck: LaiYuLiquidDeck, resource_type: type) -> List[Resource]:
    """
    根据类型获取资源列表
    
    Args:
        deck: 工作台实例
        resource_type: 资源类型
        
    Returns:
        List[Resource]: 匹配类型的资源列表
    """
    return [child for child in deck.children if isinstance(child, resource_type)]


def list_all_resources(deck: LaiYuLiquidDeck) -> Dict[str, List[str]]:
    """
    列出所有资源
    
    Args:
        deck: 工作台实例
        
    Returns:
        Dict[str, List[str]]: 按类型分组的资源名称字典
    """
    resources = {
        "tip_racks": [],
        "plates": [],
        "containers": [],
        "positions": []
    }
    
    for child in deck.children:
        if isinstance(child, (LaiYuTipRack1000, LaiYuTipRack200)):
            resources["tip_racks"].append(child.name)
        elif isinstance(child, (LaiYu96WellPlate, LaiYuDeepWellPlate)):
            resources["plates"].append(child.name)
        elif isinstance(child, (LaiYuWasteContainer, LaiYuWashContainer, LaiYuReagentContainer)):
            resources["containers"].append(child.name)
        elif isinstance(child, (LaiYuTipDisposal, LaiYuMaintenancePosition)):
            resources["positions"].append(child.name)
    
    return resources


# 导出的类别名（向后兼容）
TipRack1000ul = LaiYuTipRack1000
TipRack200ul = LaiYuTipRack200
Plate96Well = LaiYu96WellPlate
Plate96DeepWell = LaiYuDeepWellPlate
TubeRack8 = LaiYu8TubeRack
WasteContainer = LaiYuWasteContainer
WashContainer = LaiYuWashContainer
ReagentContainer = LaiYuReagentContainer
TipDisposal = LaiYuTipDisposal
MaintenancePosition = LaiYuMaintenancePosition