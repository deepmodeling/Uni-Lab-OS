"""
LaiYu_Liquid 控制器模块

该模块包含了LaiYu_Liquid液体处理工作站的高级控制器：
- 移液器控制器：提供液体处理的高级接口
- XYZ运动控制器：提供三轴运动的高级接口
"""

# 移液器控制器导入
from .pipette_controller import PipetteController

# XYZ运动控制器导入
from .xyz_controller import XYZController

__all__ = [
    # 移液器控制器
    "PipetteController",
    
    # XYZ运动控制器
    "XYZController",
]

__version__ = "1.0.0"
__author__ = "LaiYu_Liquid Controller Team"
__description__ = "LaiYu_Liquid 高级控制器集合"