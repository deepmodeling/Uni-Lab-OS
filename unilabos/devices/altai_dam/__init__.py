"""
阿尔泰科技DAM3000M系列控制仪表设备支持

支持的设备:
- DAM3060V: 4通道模拟输出模块
- DAM3151: 32通道模拟输入模块
"""

from unilabos.devices.altai_dam.dam3060v import DAM3060V
from unilabos.devices.altai_dam.dam3151 import DAM3151

__all__ = ["DAM3060V", "DAM3151"]