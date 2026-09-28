"""
阿尔泰科技DAM3000M系列设备基类
"""

import ctypes
from ctypes import wintypes
from typing import Dict, Optional
import platform


class DeviceInfo(ctypes.Structure):
    """设备信息结构体"""
    _fields_ = [
        ("DeviceType", wintypes.LONG),
        ("TypeSuffix", wintypes.LONG),
        ("ModusType", wintypes.LONG),
        ("VesionID", wintypes.LONG),
        ("DeviceID", wintypes.LONG),
        ("BaudRate", wintypes.LONG),
        ("bParity", wintypes.LONG)
    ]


class DAMDeviceBase:
    """
    阿尔泰DAM3000M系列设备基类
    
    提供基本的设备连接、初始化和通信功能
    """
    
    # 类级别的句柄缓存，支持同一串口共享句柄
    _handles: Dict[int, wintypes.HANDLE] = {}
    
    def __init__(self, com_id: int, baud_rate: int, device_id: int, dll_path: Optional[str] = None):
        """
        初始化DAM设备
        
        Args:
            com_id: 串口号 (例如: 4 表示 COM4)
            baud_rate: 波特率代码 (0-7)
                0: 1200 bps
                1: 2400 bps
                2: 4800 bps
                3: 9600 bps
                4: 19200 bps
                5: 38400 bps
                6: 57600 bps
                7: 115200 bps
            device_id: 设备ID (Modbus地址)
            dll_path: DLL文件路径 (可选，默认为当前目录下的DAM3000M_64.dll)
        """
        if platform.system() != "Windows":
            raise RuntimeError("阿尔泰DAM3000M设备仅支持Windows系统")
        
        self.com_id = com_id
        self.baud_rate = baud_rate
        self.device_id = device_id
        self.dll_path = dll_path or "./DAM3000M_64.dll"
        
        # 加载DLL
        self._dam3000m = ctypes.WinDLL(self.dll_path)
        self._setup_dll_functions()
        
        # 获取设备句柄
        self.handle = self._get_handle()
        
        # 获取设备信息
        self.device_info = self._get_device_info(device_id)
    
    def _setup_dll_functions(self):
        """设置DLL函数原型"""
        # 创建设备
        self._dam3000m.DAM3000M_CreateDevice.argtypes = [wintypes.LONG]
        self._dam3000m.DAM3000M_CreateDevice.restype = wintypes.HANDLE
        
        # 初始化设备
        self._dam3000m.DAM3000M_InitDevice.argtypes = [
            wintypes.HANDLE, wintypes.LONG, wintypes.LONG, 
            wintypes.LONG, wintypes.LONG, wintypes.LONG, wintypes.LONG
        ]
        self._dam3000m.DAM3000M_InitDevice.restype = wintypes.BOOL
        
        # 释放设备
        self._dam3000m.DAM3000M_ReleaseDevice.argtypes = [wintypes.HANDLE]
        self._dam3000m.DAM3000M_ReleaseDevice.restype = wintypes.BOOL
        
        # 获取设备信息
        self._dam3000m.DAM3000M_GetDeviceInfo.argtypes = [
            wintypes.HANDLE, wintypes.LONG, ctypes.POINTER(DeviceInfo)
        ]
        self._dam3000m.DAM3000M_GetDeviceInfo.restype = wintypes.BOOL
        
        # 写单个寄存器
        self._dam3000m.DAM3000M_WriteSingleReg.argtypes = [
            wintypes.HANDLE, wintypes.LONG, wintypes.LONG, wintypes.ULONG
        ]
        self._dam3000m.DAM3000M_WriteSingleReg.restype = wintypes.BOOL
        
        # 读输入寄存器 (UInt16)
        self._dam3000m.DAM3000M_ReadInputRegsUInt16.argtypes = [
            wintypes.HANDLE, wintypes.LONG, wintypes.INT, 
            wintypes.INT, ctypes.POINTER(wintypes.USHORT)
        ]
        self._dam3000m.DAM3000M_ReadInputRegsUInt16.restype = wintypes.BOOL
    
    def _get_handle(self) -> wintypes.HANDLE:
        """获取设备句柄（支持句柄共享）"""
        if self.com_id not in self._handles:
            handle = self._dam3000m.DAM3000M_CreateDevice(self.com_id)
            if handle in (-1, None, 0):
                raise RuntimeError(f"创建设备失败: COM{self.com_id}")
            
            # 初始化设备: baud_rate, 8位数据位, 无校验, 1位停止位
            if not self._dam3000m.DAM3000M_InitDevice(handle, self.baud_rate, 8, 0, 0x00, 200, 0):
                raise RuntimeError(f"初始化设备失败: COM{self.com_id}")
            
            self._handles[self.com_id] = handle
        
        return self._handles[self.com_id]
    
    def _get_device_info(self, device_id: int) -> DeviceInfo:
        """获取设备信息"""
        device_info = DeviceInfo()
        if not self._dam3000m.DAM3000M_GetDeviceInfo(self.handle, device_id, ctypes.byref(device_info)):
            raise RuntimeError(f"获取设备信息失败: 设备ID {device_id}")
        return device_info
    
    @property
    def device_name(self) -> str:
        """获取设备名称"""
        name = f"DAM-{self.device_info.DeviceType:02X}{chr(self.device_info.TypeSuffix >> 8 & 0xFF)}"
        return name.rstrip(' ')
    
    def write_single_reg(self, reg_addr: int, value: int) -> bool:
        """写单个寄存器"""
        return self._dam3000m.DAM3000M_WriteSingleReg(
            self.handle, self.device_id, reg_addr, value
        )
    
    def read_input_regs_uint16(self, start_addr: int, count: int) -> list:
        """读输入寄存器 (16位无符号)"""
        buffer_type = ctypes.c_ushort * count
        data_buffer = buffer_type()
        
        if not self._dam3000m.DAM3000M_ReadInputRegsUInt16(
            self.handle, self.device_id, start_addr, count, data_buffer
        ):
            raise RuntimeError(f"读输入寄存器失败: 起始地址 {start_addr}, 数量 {count}")
        
        return list(data_buffer)
    
    def close(self):
        """关闭设备连接"""
        if self.com_id in self._handles:
            self._dam3000m.DAM3000M_ReleaseDevice(self._handles[self.com_id])
            del self._handles[self.com_id]
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False