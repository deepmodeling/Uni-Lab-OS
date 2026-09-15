"""
DAM3151 - 32通道模拟输入模块

测量范围:
电压:
- -10V ~ 10V (range_code=9)
- -5V ~ 5V (range_code=8)
- -1V ~ 1V (range_code=6)
- -500mV ~ 500mV (range_code=5)
- -150mV ~ 150mV (range_code=4)
- 0V ~ 10V (range_code=14)
- 0V ~ 5V (range_code=13)
- 1V ~ 5V (range_code=130)

电流:
- -20mA ~ 20mA (range_code=10)
- 0mA ~ 20mA (range_code=11)
- 4mA ~ 20mA (range_code=12)
- 0mA ~ 22mA (range_code=128)
"""

from typing import Tuple, Dict, List
import ctypes
from ctypes import wintypes

from unilabos.devices.altai_dam.dam_base import DAMDeviceBase


class DAM3151(DAMDeviceBase):
    """
    DAM3151 - 32通道模拟输入模块
    
    支持32通道同步采集，可测量电压或电流信号
    """
    
    # 设备参数
    ChannelNum = 32  # 通道数
    RangeNum = 12  # 量程模式数
    RangeAddr = 136  # 量程设置寄存器起始地址 (137-1)
    CHEnableAddr = 221  # 通道使能寄存器地址
    ADAddr = 0  # A/D转换数据起始地址
    fLsbType = 65535.0  # LSB转换系数
    
    # 量程模式映射 (range_code: (最小值, 最大值, 单位))
    RangeModes: Dict[int, Tuple[float, float, str]] = {
        9: (-10.0, 10.0, "V"),      # -10V to 10V
        8: (-5.0, 5.0, "V"),        # -5V to 5V
        6: (-1.0, 1.0, "V"),        # -1V to 1V
        5: (-0.5, 0.5, "V"),        # -500mV to 500mV
        4: (-0.15, 0.15, "V"),      # -150mV to 150mV
        14: (0.0, 10.0, "V"),       # 0V to 10V
        13: (0.0, 5.0, "V"),        # 0V to 5V
        130: (1.0, 5.0, "V"),       # 1V to 5V
        10: (-20.0, 20.0, "mA"),    # -20mA to 20mA
        11: (0.0, 20.0, "mA"),      # 0mA to 20mA
        12: (4.0, 20.0, "mA"),      # 4mA to 20mA
        128: (0.0, 22.0, "mA")      # 0mA to 22mA
    }
    
    # 默认量程模式 (交替使用电流和电压模式)
    DefaultModeList = [10 if i % 2 == 0 else 8 for i in range(ChannelNum)]
    
    def __init__(self, com_id: int, baud_rate: int, device_id: int,
                 mode_list: list = None, dll_path: str = None):
        """
        初始化DAM3151模块
        
        Args:
            com_id: 串口号
            baud_rate: 波特率代码
            device_id: 设备ID
            mode_list: 各通道量程模式列表 (长度为32，元素为range_code)
            dll_path: DLL文件路径
        """
        super().__init__(com_id, baud_rate, device_id, dll_path)
        
        # 初始化通道量程模式
        self.channel_modes = {}
        mode_list = mode_list or self.DefaultModeList
        
        # 使能所有通道
        self._enable_all_channels()
        
        # 设置各通道量程
        for channel in range(self.ChannelNum):
            self.set_measurement_range_mode(channel, mode_list[channel])
    
    def _enable_all_channels(self):
        """使能所有32个通道"""
        mask = (1 << self.ChannelNum) - 1  # 0xFFFFFFFF
        if not self.write_single_reg(self.CHEnableAddr, mask):
            raise RuntimeError(
                f"使能通道失败: handle={self.handle}, device_id={self.device_id}, "
                f"addr={self.CHEnableAddr}"
            )
    
    def set_measurement_range_mode(self, channel: int, range_code: int):
        """
        设置通道测量范围模式
        
        Args:
            channel: 通道号 (0-31)
            range_code: 量程代码
        
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
    
    def _data_converter(self, raw_data: int, range_top: float, range_bottom: float) -> float:
        """
        将原始数据转换为实际测量值
        
        Args:
            raw_data: 原始A/D转换值
            range_top: 量程上限
            range_bottom: 量程下限
        
        Returns:
            实际测量值 (V 或 mA)
        """
        return raw_data / self.fLsbType * (range_top - range_bottom) + range_bottom
    
    def measure_all_channels(self) -> List[float]:
        """
        测量所有32个通道
        
        Returns:
            测量值列表 (长度为32，单位为 V 或 mA)
        """
        # 读取所有通道数据
        raw_data = self.read_input_regs_uint16(self.ADAddr, self.ChannelNum)
        
        # 转换为实际值
        measurements = []
        for channel in range(self.ChannelNum):
            if channel not in self.channel_modes:
                measurements.append(None)
                continue
            
            range_code = self.channel_modes[channel]
            range_bottom, range_top, unit = self.RangeModes[range_code]
            
            value = self._data_converter(raw_data[channel], range_top, range_bottom)
            measurements.append(value)
        
        return measurements
    
    def measure_channel(self, channel: int) -> float:
        """
        测量单个通道
        
        Args:
            channel: 通道号 (0-31)
        
        Returns:
            测量值 (V 或 mA)
        """
        if channel not in range(self.ChannelNum):
            raise ValueError(f"通道号必须在 0-{self.ChannelNum-1} 之间")
        
        if channel not in self.channel_modes:
            raise ValueError(f"通道 {channel} 尚未设置量程模式")
        
        # 读取单个通道数据
        raw_data = self.read_input_regs_uint16(self.ADAddr + channel, 1)[0]
        
        # 转换为实际值
        range_code = self.channel_modes[channel]
        range_bottom, range_top, unit = self.RangeModes[range_code]
        
        return self._data_converter(raw_data, range_top, range_bottom)
    
    def get_range_mode(self, channel: int) -> Tuple[float, float, str]:
        """
        获取通道的当前量程范围
        
        Args:
            channel: 通道号 (0-31)
        
        Returns:
            (最小值, 最大值, 单位) 元组
        """
        if channel not in self.channel_modes:
            raise ValueError(f"通道 {channel} 尚未设置量程模式")
        
        return self.RangeModes[self.channel_modes[channel]]
    
    def get_all_channels_range(self) -> Dict[int, Tuple[float, float, str]]:
        """获取所有通道的量程范围"""
        return {
            channel: self.RangeModes[mode]
            for channel, mode in self.channel_modes.items()
        }
    
    def measure_current_channels(self) -> List[float]:
        """
        测量所有电流模式通道 (偶数通道)
        
        Returns:
            电流值列表 (mA)
        """
        all_data = self.measure_all_channels()
        return [all_data[i] for i in range(0, self.ChannelNum, 2)]
    
    def measure_voltage_channels(self) -> List[float]:
        """
        测量所有电压模式通道 (奇数通道)
        
        Returns:
            电压值列表 (V)
        """
        all_data = self.measure_all_channels()
        return [all_data[i] for i in range(1, self.ChannelNum, 2)]