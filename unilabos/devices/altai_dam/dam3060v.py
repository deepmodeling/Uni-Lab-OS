"""
DAM3060V - 4通道模拟输出模块

输出范围:
- Mode 0: -10V ~ 10V (range_code=9)
- Mode 1: -5V ~ 5V (range_code=8)
- Mode 2: 0V ~ 10V (range_code=14)
- Mode 3: 0V ~ 5V (range_code=13)
"""

from math import ceil
from typing import Tuple, Dict
import ctypes
from ctypes import wintypes

from unilabos.devices.altai_dam.dam_base import DAMDeviceBase


class DAM3060V(DAMDeviceBase):
    """
    DAM3060V - 4通道模拟输出模块
    
    每个通道可独立设置输出范围和输出电压
    """
    
    # 设备参数
    AOAddr = 352  # 模拟输出寄存器起始地址
    RangeAddr = 272  # 量程设置寄存器起始地址
    ChannelNum = 4  # 通道数
    RangeNum = 4  # 量程模式数
    ChannelAddr = 2  # 通道地址间隔
    
    # 量程模式映射 (range_code: (最小值, 最大值))
    RangeModes: Dict[int, Tuple[float, float]] = {
        9: (-10.0, 10.0),   # Mode 0: -10V to 10V
        8: (-5.0, 5.0),     # Mode 1: -5V to 5V
        14: (0.0, 10.0),    # Mode 2: 0V to 10V
        13: (0.0, 5.0)      # Mode 3: 0V to 5V
    }
    
    # 默认量程模式 (每个通道)
    DefaultModeList = [8, 8, 8, 8]  # 默认全部为 -5V ~ 5V
    
    def __init__(self, com_id: int, baud_rate: int, device_id: int, 
                 mode_list: list = None, dll_path: str = None):
        """
        初始化DAM3060V模块
        
        Args:
            com_id: 串口号
            baud_rate: 波特率代码
            device_id: 设备ID
            mode_list: 各通道量程模式列表 (长度为4，元素为range_code)
            dll_path: DLL文件路径
        """
        super().__init__(com_id, baud_rate, device_id, dll_path)
        
        # 初始化通道量程模式
        self.channel_modes = {}
        mode_list = mode_list or self.DefaultModeList
        
        # 设置各通道量程
        for channel in range(self.ChannelNum):
            self.set_output_range_mode(channel, mode_list[channel])
    
    def set_output_range_mode(self, channel: int, range_code: int):
        """
        设置通道输出范围模式
        
        Args:
            channel: 通道号 (0-3)
            range_code: 量程代码 (8, 9, 13, 14)
        
        Raises:
            ValueError: 参数错误
            RuntimeError: 设置失败
        """
        if channel not in range(self.ChannelNum):
            raise ValueError(f"通道号必须在 0-{self.ChannelNum-1} 之间")
        
        if range_code not in self.RangeModes:
            raise ValueError(f"量程代码必须是 {list(self.RangeModes.keys())} 之一")
        
        reg_addr = self.RangeAddr + channel
        if not self.write_single_reg(reg_addr, range_code):
            raise RuntimeError(
                f"设置量程失败: handle={self.handle}, device_id={self.device_id}, "
                f"addr={reg_addr}, mode={range_code}"
            )
        
        self.channel_modes[channel] = range_code
    
    def set_analog_output(self, channel: int, voltage: float):
        """
        设置模拟输出电压
        
        Args:
            channel: 通道号 (0-3)
            voltage: 输出电压 (V)
        
        Raises:
            ValueError: 参数错误或电压超出范围
            RuntimeError: 设置失败
        """
        if channel not in range(self.ChannelNum):
            raise ValueError(f"通道号必须在 0-{self.ChannelNum-1} 之间")
        
        if channel not in self.channel_modes:
            raise ValueError(f"通道 {channel} 尚未设置量程模式")
        
        range_code = self.channel_modes[channel]
        range_bottom, range_top = self.RangeModes[range_code]
        
        # 检查电压范围
        if not (range_bottom <= voltage <= range_top):
            raise ValueError(
                f"电压 {voltage}V 超出范围 [{range_bottom}V, {range_top}V]"
            )
        
        # 计算数字值 (12位精度)
        dal_lsb = ceil((voltage - range_bottom) * 0xFFF / (range_top - range_bottom))
        
        # 写入输出值
        reg_addr = self.AOAddr + channel * self.ChannelAddr
        
        # 使用写多寄存器 (UInt32)
        self._dam3000m.DAM3000M_WriteMultiRegsUInt32.argtypes = [
            wintypes.HANDLE, wintypes.LONG, wintypes.LONG,
            wintypes.LONG, ctypes.POINTER(wintypes.ULONG)
        ]
        self._dam3000m.DAM3000M_WriteMultiRegsUInt32.restype = wintypes.BOOL
        
        if not self._dam3000m.DAM3000M_WriteMultiRegsUInt32(
            self.handle, self.device_id, reg_addr, 1,
            ctypes.byref(ctypes.c_ulong(dal_lsb))
        ):
            raise RuntimeError(f"设置模拟输出失败: 通道 {channel}, 电压 {voltage}V")
    
    def get_range_mode(self, channel: int) -> Tuple[float, float]:
        """
        获取通道的当前量程范围
        
        Args:
            channel: 通道号 (0-3)
        
        Returns:
            (最小电压, 最大电压) 元组
        """
        if channel not in self.channel_modes:
            raise ValueError(f"通道 {channel} 尚未设置量程模式")
        
        return self.RangeModes[self.channel_modes[channel]]
    
    def get_all_channels_range(self) -> Dict[int, Tuple[float, float]]:
        """获取所有通道的量程范围"""
        return {
            channel: self.RangeModes[mode]
            for channel, mode in self.channel_modes.items()
        }