#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LaiYu液体处理设备核心模块

该模块包含LaiYu液体处理设备的核心功能组件：
- LaiYu_Liquid.py: 主设备类和配置管理
- abstract_protocol.py: 抽象协议定义
- laiyu_liquid_res.py: 设备资源管理

作者: UniLab团队
版本: 2.0.0
"""

from .LaiYu_Liquid import (
    LaiYuLiquid,
    LaiYuLiquidConfig,
    LaiYuLiquidDeck,
    LaiYuLiquidContainer,
    LaiYuLiquidTipRack,
    create_quick_setup
)

from .laiyu_liquid_res import (
    LaiYuLiquidDeck,
    LaiYuLiquidContainer, 
    LaiYuLiquidTipRack
)

__all__ = [
    # 主设备类
    'LaiYuLiquid',
    'LaiYuLiquidConfig',
    
    # 设备资源
    'LaiYuLiquidDeck',
    'LaiYuLiquidContainer',
    'LaiYuLiquidTipRack',
    
    # 工具函数
    'create_quick_setup'
]