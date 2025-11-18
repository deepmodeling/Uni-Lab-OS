#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SOPA气动式移液器RS485控制驱动程序

基于SOPA气动式移液器RS485控制指令合集编写的Python驱动程序，
支持完整的移液器控制功能，包括移液、检测、配置等操作。

仅支持SC-STxxx-00-13型号的RS485通信。
"""

import serial
import time
import logging
import threading
from typing import Optional, Union, Dict, Any, Tuple, List
from enum import Enum, IntEnum
from dataclasses import dataclass
from contextlib import contextmanager

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SOPAError(Exception):
    """SOPA移液器异常基类"""
    pass


class SOPACommunicationError(SOPAError):
    """通信异常"""
    pass


class SOPADeviceError(SOPAError):
    """设备异常"""
    pass


class SOPAStatusCode(IntEnum):
    """状态码枚举"""
    NO_ERROR = 0x00          # 无错误
    ACTION_INCOMPLETE = 0x01  # 上次动作未完成
    NOT_INITIALIZED = 0x02    # 设备未初始化
    DEVICE_OVERLOAD = 0x03    # 设备过载
    INVALID_COMMAND = 0x04    # 无效指令
    LLD_FAULT = 0x05         # 液位探测故障
    AIR_ASPIRATE = 0x0D      # 空吸
    NEEDLE_BLOCK = 0x0E      # 堵针
    FOAM_DETECT = 0x10       # 泡沫
    EXCEED_TIP_VOLUME = 0x11 # 吸液超过吸头容量


class CommunicationType(Enum):
    """通信类型"""
    TERMINAL_DEBUG = "/"     # 终端调试，头码为0x2F
    OEM_COMMUNICATION = "["  # OEM通信，头码为0x5B


class DetectionMode(IntEnum):
    """液位检测模式"""
    PRESSURE = 0  # 压力式检测(pLLD)
    CAPACITIVE = 1  # 电容式检测(cLLD)


@dataclass
class SOPAConfig:
    """SOPA移液器配置参数"""
    # 通信参数
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200
    address: int = 1
    timeout: float = 5.0
    comm_type: CommunicationType = CommunicationType.TERMINAL_DEBUG

    # 运动参数 (单位: 0.1ul/秒)
    max_speed: int = 2000       # 最高速度 200ul/秒
    start_speed: int = 200      # 启动速度 20ul/秒
    cutoff_speed: int = 200     # 断流速度 20ul/秒
    acceleration: int = 30000   # 加速度

    # 检测参数
    empty_threshold: int = 4    # 空吸门限
    foam_threshold: int = 20    # 泡沫门限
    block_threshold: int = 350  # 堵塞门限

    # 液位检测参数
    lld_speed: int = 200        # 检测速度 (100~2000)
    lld_sensitivity: int = 5    # 检测灵敏度 (3~40)
    detection_mode: DetectionMode = DetectionMode.PRESSURE

    # 吸头参数
    tip_volume: int = 1000      # 吸头容量 (ul)
    calibration_factor: float = 1.0  # 校准系数
    compensation_offset: float = 0.0  # 补偿偏差

    def __post_init__(self):
        """初始化后验证参数"""
        self._validate_address()

    def _validate_address(self):
        """
        验证设备地址是否符合协议要求
        
        协议要求：
        - 地址范围：1~254
        - 禁用地址：47, 69, 91 (对应ASCII字符 '/', 'E', '[')
        """
        if not (1 <= self.address <= 254):
            raise ValueError(f"设备地址必须在1-254范围内，当前地址: {self.address}")
        
        forbidden_addresses = [47, 69, 91]  # '/', 'E', '['
        if self.address in forbidden_addresses:
            forbidden_chars = {47: "'/' (0x2F)", 69: "'E' (0x45)", 91: "'[' (0x5B)"}
            char_desc = forbidden_chars[self.address]
            raise ValueError(
                f"地址 {self.address} 不可用，因为它对应协议字符 {char_desc}。"
                f"请选择其他地址（1-254，排除47、69、91）"
            )


class SOPAPipette:
    """SOPA气动式移液器驱动类"""

    def __init__(self, config: SOPAConfig):
        """
        初始化SOPA移液器

        Args:
            config: 移液器配置参数
        """
        self.config = config
        self.serial_port: Optional[serial.Serial] = None
        self.is_connected = False
        self.is_initialized = False
        self.lock = threading.Lock()

        # 状态缓存
        self._last_status = SOPAStatusCode.NOT_INITIALIZED
        self._current_position = 0
        self._tip_present = False

    def connect(self) -> bool:
        """
        连接移液器

        Returns:
            bool: 连接是否成功
        """
        try:
            self.serial_port = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.config.timeout
            )

            if self.serial_port.is_open:
                self.is_connected = True
                logger.info(f"已连接到SOPA移液器，端口: {self.config.port}, 地址: {self.config.address}")

                # 查询设备信息
                version = self.get_firmware_version()
                if version:
                    logger.info(f"固件版本: {version}")

                return True
            else:
                raise SOPACommunicationError("串口打开失败")

        except Exception as e:
            logger.error(f"连接失败: {str(e)}")
            self.is_connected = False
            return False

    def disconnect(self):
        """断开连接"""
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
        self.is_connected = False
        self.is_initialized = False
        logger.info("已断开SOPA移液器连接")

    def _calculate_checksum(self, data: bytes) -> int:
        """计算校验和"""
        return sum(data) & 0xFF

    def _build_command(self, command: str) -> bytes:
        """
        构建完整命令字节串
        
        根据协议格式：头码 + 地址 + 命令/数据 + 尾码 + 校验和

        Args:
            command: 命令字符串

        Returns:
            bytes: 完整的命令字节串
        """
        header = self.config.comm_type.value  # '/' 或 '['
        address = str(self.config.address)    # 设备地址
        tail = "E"                           # 尾码固定为 'E'

        # 构建基础命令字符串：头码 + 地址 + 命令 + 尾码
        cmd_str = f"{header}{address}{command}{tail}"
        
        # 转换为字节串
        cmd_bytes = cmd_str.encode('ascii')
        
        # 计算校验和（所有字节的累加值）
        checksum = self._calculate_checksum(cmd_bytes)
        
        # 返回完整命令：基础命令字节 + 校验和字节
        return cmd_bytes + bytes([checksum])

    def _send_command(self, command: str) -> bool:
        """
        发送命令到移液器

        Args:
            command: 要发送的命令

        Returns:
            bool: 命令是否发送成功
        """
        if not self.is_connected or not self.serial_port:
            raise SOPACommunicationError("设备未连接")

        with self.lock:
            try:
                full_command_bytes = self._build_command(command)
                # 转换为可读字符串用于日志显示
                readable_cmd = ''.join(chr(b) if 32 <= b <= 126 else f'\\x{b:02X}' for b in full_command_bytes)
                logger.debug(f"发送命令: {readable_cmd}")

                self.serial_port.write(full_command_bytes)
                self.serial_port.flush()

                # 等待响应
                time.sleep(0.1)
                return True

            except Exception as e:
                logger.error(f"发送命令失败: {str(e)}")
                raise SOPACommunicationError(f"发送命令失败: {str(e)}")

    def _read_response(self, timeout: float = None) -> Optional[str]:
        """
        读取设备响应

        Args:
            timeout: 超时时间

        Returns:
            Optional[str]: 设备响应字符串
        """
        if not self.is_connected or not self.serial_port:
            return None

        timeout = timeout or self.config.timeout

        try:
            # 设置读取超时
            self.serial_port.timeout = timeout

            response = b''
            start_time = time.time()

            while time.time() - start_time < timeout:
                if self.serial_port.in_waiting > 0:
                    chunk = self.serial_port.read(self.serial_port.in_waiting)
                    response += chunk

                    # 检查是否收到完整响应（以'E'结尾）
                    if response.endswith(b'E') or len(response) >= 20:
                        break

                time.sleep(0.01)

            if response:
                decoded_response = response.decode('ascii', errors='ignore')
                logger.debug(f"收到响应: {decoded_response}")
                return decoded_response

        except Exception as e:
            logger.error(f"读取响应失败: {str(e)}")

        return None

    def _send_query(self, query: str) -> Optional[str]:
        """
        发送查询命令并获取响应

        Args:
            query: 查询命令

        Returns:
            Optional[str]: 查询结果
        """
        try:
            self._send_command(query)
            return self._read_response()
        except Exception as e:
            logger.error(f"查询失败: {str(e)}")
            return None

    # ==================== 基础控制方法 ====================

    def initialize(self) -> bool:
        """
        初始化移液器

        Returns:
            bool: 初始化是否成功
        """
        try:
            logger.info("初始化SOPA移液器...")

            # 发送初始化命令
            self._send_command("HE")

            # 等待初始化完成
            time.sleep(2.0)

            # 检查状态
            status = self.get_status()
            if status == SOPAStatusCode.NO_ERROR:
                self.is_initialized = True
                logger.info("移液器初始化成功")

                # 应用配置参数
                self._apply_configuration()
                return True
            else:
                logger.error(f"初始化失败，状态码: {status}")
                return False

        except Exception as e:
            logger.error(f"初始化异常: {str(e)}")
            return False

    def _apply_configuration(self):
        """应用配置参数"""
        try:
            # 设置运动参数
            self.set_acceleration(self.config.acceleration)
            self.set_start_speed(self.config.start_speed)
            self.set_cutoff_speed(self.config.cutoff_speed)
            self.set_max_speed(self.config.max_speed)

            # 设置检测参数
            self.set_empty_threshold(self.config.empty_threshold)
            self.set_foam_threshold(self.config.foam_threshold)
            self.set_block_threshold(self.config.block_threshold)

            # 设置吸头参数
            self.set_tip_volume(self.config.tip_volume)
            self.set_calibration_factor(self.config.calibration_factor)

            # 设置液位检测参数
            self.set_detection_mode(self.config.detection_mode)
            self.set_lld_speed(self.config.lld_speed)

            logger.info("配置参数应用完成")

        except Exception as e:
            logger.warning(f"应用配置参数失败: {str(e)}")

    def eject_tip(self) -> bool:
        """
        顶出枪头

        Returns:
            bool: 操作是否成功
        """
        try:
            logger.info("顶出枪头")
            self._send_command("RE")
            time.sleep(1.0)
            return True
        except Exception as e:
            logger.error(f"顶出枪头失败: {str(e)}")
            return False

    def get_tip_status(self) -> bool:
        """
        获取枪头状态

        Returns:
            bool: True表示有枪头，False表示无枪头
        """
        try:
            response = self._send_query("Q28")
            if response and len(response) > 10:
                # 解析响应中的枪头状态
                if "T1" in response:
                    self._tip_present = True
                    return True
                elif "T0" in response:
                    self._tip_present = False
                    return False
                else:
                    logger.error(f"获取枪头状态失败: {response}")
                    return False
        except Exception as e:
            logger.error(f"获取枪头状态失败: {str(e)}")

        return False

    # ==================== 移液控制方法 ====================

    def move_absolute(self, position: float) -> bool:
        """
        绝对位置移动

        Args:
            position: 目标位置(微升)

        Returns:
            bool: 移动是否成功
        """
        try:
            if not self.is_initialized:
                raise SOPADeviceError("设备未初始化")

            pos_int = int(position)
            logger.debug(f"绝对移动到位置: {pos_int}ul")

            self._send_command(f"A{pos_int}E")
            time.sleep(0.5)

            self._current_position = pos_int
            return True

        except Exception as e:
            logger.error(f"绝对移动失败: {str(e)}")
            return False

    def aspirate(self, volume: float, detection: bool = False) -> bool:
        """
        抽吸液体

        Args:
            volume: 抽吸体积(微升)
            detection: 是否开启液体检测

        Returns:
            bool: 抽吸是否成功
        """
        try:
            if not self.is_initialized:
                raise SOPADeviceError("设备未初始化")

            vol_int = int(volume)
            logger.info(f"抽吸液体: {vol_int}ul, 检测: {detection}")

            # 构建命令
            cmd_parts = []
            cmd_parts.append(f"a{self.config.acceleration}")
            cmd_parts.append(f"b{self.config.start_speed}")
            cmd_parts.append(f"c{self.config.cutoff_speed}")
            cmd_parts.append(f"s{self.config.max_speed}")

            if detection:
                cmd_parts.append("f1")  # 开启检测

            cmd_parts.append(f"P{vol_int}")

            if detection:
                cmd_parts.append("f0")  # 关闭检测

            cmd_parts.append("E")

            command = "".join(cmd_parts)
            self._send_command(command)

            # 等待操作完成
            time.sleep(max(1.0, vol_int / 100.0))

            # 检查状态
            status = self.get_status()
            if status == SOPAStatusCode.NO_ERROR:
                self._current_position += vol_int
                logger.info(f"抽吸成功: {vol_int}ul")
                return True
            elif status == SOPAStatusCode.AIR_ASPIRATE:
                logger.warning("检测到空吸")
                return False
            elif status == SOPAStatusCode.NEEDLE_BLOCK:
                logger.error("检测到堵针")
                return False
            else:
                logger.error(f"抽吸失败，状态码: {status}")
                return False

        except Exception as e:
            logger.error(f"抽吸失败: {str(e)}")
            return False

    def dispense(self, volume: float, detection: bool = False) -> bool:
        """
        分配液体

        Args:
            volume: 分配体积(微升)
            detection: 是否开启液体检测

        Returns:
            bool: 分配是否成功
        """
        try:
            if not self.is_initialized:
                raise SOPADeviceError("设备未初始化")

            vol_int = int(volume)
            logger.info(f"分配液体: {vol_int}ul, 检测: {detection}")

            # 构建命令
            cmd_parts = []
            cmd_parts.append(f"a{self.config.acceleration}")
            cmd_parts.append(f"b{self.config.start_speed}")
            cmd_parts.append(f"c{self.config.cutoff_speed}")
            cmd_parts.append(f"s{self.config.max_speed}")

            if detection:
                cmd_parts.append("f1")  # 开启检测

            cmd_parts.append(f"D{vol_int}")

            if detection:
                cmd_parts.append("f0")  # 关闭检测

            cmd_parts.append("E")

            command = "".join(cmd_parts)
            self._send_command(command)

            # 等待操作完成
            time.sleep(max(1.0, vol_int / 200.0))

            # 检查状态
            status = self.get_status()
            if status == SOPAStatusCode.NO_ERROR:
                self._current_position -= vol_int
                logger.info(f"分配成功: {vol_int}ul")
                return True
            else:
                logger.error(f"分配失败，状态码: {status}")
                return False

        except Exception as e:
            logger.error(f"分配失败: {str(e)}")
            return False

    # ==================== 液位检测方法 ====================

    def liquid_level_detection(self, sensitivity: int = None) -> bool:
        """
        执行液位检测

        Args:
            sensitivity: 检测灵敏度 (3~40)

        Returns:
            bool: 检测是否成功
        """
        try:
            if not self.is_initialized:
                raise SOPADeviceError("设备未初始化")

            sens = sensitivity or self.config.lld_sensitivity

            if self.config.detection_mode == DetectionMode.PRESSURE:
                # 压力式液面检测
                command = f"m0k{self.config.lld_speed}L{sens}E"
            else:
                # 电容式液面检测
                command = f"m1L{sens}E"

            logger.info(f"执行液位检测, 模式: {self.config.detection_mode.name}, 灵敏度: {sens}")

            self._send_command(command)
            time.sleep(2.0)

            # 检查检测结果
            status = self.get_status()
            if status == SOPAStatusCode.NO_ERROR:
                logger.info("液位检测成功")
                return True
            elif status == SOPAStatusCode.LLD_FAULT:
                logger.error("液位检测故障")
                return False
            else:
                logger.warning(f"液位检测异常，状态码: {status}")
                return False

        except Exception as e:
            logger.error(f"液位检测失败: {str(e)}")
            return False

    # ==================== 参数设置方法 ====================

    def set_max_speed(self, speed: int) -> bool:
        """设置最高速度 (0.1ul/秒为单位)"""
        try:
            self._send_command(f"s{speed}E")
            self.config.max_speed = speed
            logger.debug(f"设置最高速度: {speed} (0.1ul/秒)")
            return True
        except Exception as e:
            logger.error(f"设置最高速度失败: {str(e)}")
            return False

    def set_start_speed(self, speed: int) -> bool:
        """设置启动速度 (0.1ul/秒为单位)"""
        try:
            self._send_command(f"b{speed}E")
            self.config.start_speed = speed
            logger.debug(f"设置启动速度: {speed} (0.1ul/秒)")
            return True
        except Exception as e:
            logger.error(f"设置启动速度失败: {str(e)}")
            return False

    def set_cutoff_speed(self, speed: int) -> bool:
        """设置断流速度 (0.1ul/秒为单位)"""
        try:
            self._send_command(f"c{speed}E")
            self.config.cutoff_speed = speed
            logger.debug(f"设置断流速度: {speed} (0.1ul/秒)")
            return True
        except Exception as e:
            logger.error(f"设置断流速度失败: {str(e)}")
            return False

    def set_acceleration(self, accel: int) -> bool:
        """设置加速度"""
        try:
            self._send_command(f"a{accel}E")
            self.config.acceleration = accel
            logger.debug(f"设置加速度: {accel}")
            return True
        except Exception as e:
            logger.error(f"设置加速度失败: {str(e)}")
            return False

    def set_empty_threshold(self, threshold: int) -> bool:
        """设置空吸门限"""
        try:
            self._send_command(f"${threshold}E")
            self.config.empty_threshold = threshold
            logger.debug(f"设置空吸门限: {threshold}")
            return True
        except Exception as e:
            logger.error(f"设置空吸门限失败: {str(e)}")
            return False

    def set_foam_threshold(self, threshold: int) -> bool:
        """设置泡沫门限"""
        try:
            self._send_command(f"!{threshold}E")
            self.config.foam_threshold = threshold
            logger.debug(f"设置泡沫门限: {threshold}")
            return True
        except Exception as e:
            logger.error(f"设置泡沫门限失败: {str(e)}")
            return False

    def set_block_threshold(self, threshold: int) -> bool:
        """设置堵塞门限"""
        try:
            self._send_command(f"%{threshold}E")
            self.config.block_threshold = threshold
            logger.debug(f"设置堵塞门限: {threshold}")
            return True
        except Exception as e:
            logger.error(f"设置堵塞门限失败: {str(e)}")
            return False

    def set_tip_volume(self, volume: int) -> bool:
        """设置吸头容量"""
        try:
            self._send_command(f"C{volume}E")
            self.config.tip_volume = volume
            logger.debug(f"设置吸头容量: {volume}ul")
            return True
        except Exception as e:
            logger.error(f"设置吸头容量失败: {str(e)}")
            return False

    def set_calibration_factor(self, factor: float) -> bool:
        """设置校准系数"""
        try:
            self._send_command(f"j{factor}E")
            self.config.calibration_factor = factor
            logger.debug(f"设置校准系数: {factor}")
            return True
        except Exception as e:
            logger.error(f"设置校准系数失败: {str(e)}")
            return False

    def set_detection_mode(self, mode: DetectionMode) -> bool:
        """设置液位检测模式"""
        try:
            self._send_command(f"m{mode.value}E")
            self.config.detection_mode = mode
            logger.debug(f"设置检测模式: {mode.name}")
            return True
        except Exception as e:
            logger.error(f"设置检测模式失败: {str(e)}")
            return False

    def set_lld_speed(self, speed: int) -> bool:
        """设置液位检测速度"""
        try:
            if 100 <= speed <= 2000:
                self._send_command(f"k{speed}E")
                self.config.lld_speed = speed
                logger.debug(f"设置检测速度: {speed}")
                return True
            else:
                logger.error("检测速度超出范围 (100~2000)")
                return False
        except Exception as e:
            logger.error(f"设置检测速度失败: {str(e)}")
            return False

    # ==================== 状态查询方法 ====================

    def get_status(self) -> SOPAStatusCode:
        """
        获取设备状态

        Returns:
            SOPAStatusCode: 当前状态码
        """
        try:
            response = self._send_query("Q")
            if response and len(response) > 8:
                # 解析状态字节
                status_char = response[8] if len(response) > 8 else '0'
                try:
                    status_code = int(status_char, 16) if status_char.isdigit() or status_char.lower() in 'abcdef' else 0
                    self._last_status = SOPAStatusCode(status_code)
                except ValueError:
                    self._last_status = SOPAStatusCode.NO_ERROR

                return self._last_status
        except Exception as e:
            logger.error(f"获取状态失败: {str(e)}")

        return SOPAStatusCode.NO_ERROR

    def get_firmware_version(self) -> Optional[str]:
        """
        获取固件版本信息
        处理SOPA移液器的双响应帧格式

        Returns:
            Optional[str]: 固件版本字符串，获取失败返回None
        """
        try:
            if not self.is_connected:
                logger.debug("设备未连接，无法查询版本")
                return "设备未连接"

            # 清空串口缓冲区，避免残留数据干扰
            if self.serial_port and self.serial_port.in_waiting > 0:
                logger.debug(f"清空缓冲区中的 {self.serial_port.in_waiting} 字节数据")
                self.serial_port.reset_input_buffer()

            # 发送版本查询命令 - 使用VE命令
            command = self._build_command("VE")
            logger.debug(f"发送版本查询命令: {command}")
            self.serial_port.write(command)
            
            # 等待响应
            time.sleep(0.3)  # 增加等待时间
            
            # 读取所有可用数据
            all_data = b''
            timeout_count = 0
            max_timeout = 15  # 增加最大等待时间到1.5秒
            
            while timeout_count < max_timeout:
                if self.serial_port.in_waiting > 0:
                    data = self.serial_port.read(self.serial_port.in_waiting)
                    all_data += data
                    logger.debug(f"接收到 {len(data)} 字节数据: {data.hex().upper()}")
                    timeout_count = 0  # 重置超时计数
                else:
                    time.sleep(0.1)
                    timeout_count += 1
                    
                # 检查是否收到完整的双响应帧
                if len(all_data) >= 26:  # 两个13字节的响应帧
                    logger.debug("收到完整的双响应帧")
                    break
                elif len(all_data) >= 13:  # 至少一个响应帧
                    # 继续等待一段时间看是否有第二个帧
                    if timeout_count > 5:  # 等待0.5秒后如果没有更多数据就停止
                        logger.debug("只收到单响应帧")
                        break
            
            logger.debug(f"总共接收到 {len(all_data)} 字节数据: {all_data.hex().upper()}")
            
            if len(all_data) < 13:
                logger.warning("接收到的数据不足一个完整响应帧")
                return "版本信息不可用"
            
            # 解析响应数据
            version_info = self._parse_version_response(all_data)
            logger.info(f"解析得到版本信息: {version_info}")
            return version_info

        except Exception as e:
            logger.error(f"获取固件版本失败: {str(e)}")
            return "版本信息不可用"

    def _parse_version_response(self, data: bytes) -> str:
        """
        解析版本响应数据
        
        Args:
            data: 原始响应数据
            
        Returns:
            str: 解析后的版本信息
        """
        try:
            # 将数据转换为十六进制字符串用于调试
            hex_data = data.hex().upper()
            logger.debug(f"收到版本响应数据: {hex_data}")
            
            # 查找响应帧的起始位置
            responses = []
            i = 0
            while i < len(data) - 12:
                # 查找帧头 0x2F (/)
                if data[i] == 0x2F:
                    # 检查是否是完整的13字节帧
                    if i + 12 < len(data) and data[i + 11] == 0x45:  # 尾码 E
                        frame = data[i:i+13]
                        responses.append(frame)
                        i += 13
                    else:
                        i += 1
                else:
                    i += 1
            
            if len(responses) < 2:
                # 如果只有一个响应帧，尝试解析
                if len(responses) == 1:
                    return self._extract_version_from_frame(responses[0])
                else:
                    return f"响应格式异常: {hex_data}"
            
            # 解析第二个响应帧（通常包含版本信息）
            version_frame = responses[1]
            return self._extract_version_from_frame(version_frame)
            
        except Exception as e:
            logger.error(f"解析版本响应失败: {str(e)}")
            return f"解析失败: {data.hex().upper()}"

    def _extract_version_from_frame(self, frame: bytes) -> str:
        """
        从响应帧中提取版本信息
        
        Args:
            frame: 13字节的响应帧
            
        Returns:
            str: 版本信息字符串
        """
        try:
            # 帧格式: 头码(1) + 地址(1) + 数据(9) + 尾码(1) + 校验和(1)
            if len(frame) != 13:
                return f"帧长度错误: {frame.hex().upper()}"
            
            # 提取数据部分 (索引2-10，共9字节)
            data_part = frame[2:11]
            
            # 尝试不同的解析方法
            version_candidates = []
            
            # 方法1: 查找可打印的ASCII字符
            ascii_chars = []
            for byte in data_part:
                if 32 <= byte <= 126:  # 可打印ASCII范围
                    ascii_chars.append(chr(byte))
            
            if ascii_chars:
                version_candidates.append(''.join(ascii_chars))
            
            # 方法2: 解析为版本号格式 (如果前几个字节是版本信息)
            if len(data_part) >= 3:
                # 检查是否是 V.x.y 格式
                if data_part[0] == 0x56:  # 'V'
                    version_str = f"V{data_part[1]}.{data_part[2]}"
                    version_candidates.append(version_str)
            
            # 方法3: 十六进制表示
            hex_version = ' '.join(f'{b:02X}' for b in data_part)
            version_candidates.append(f"HEX: {hex_version}")
            
            # 返回最合理的版本信息
            for candidate in version_candidates:
                if candidate and len(candidate.strip()) > 1:
                    return candidate.strip()
            
            return f"原始数据: {frame.hex().upper()}"
            
        except Exception as e:
            logger.error(f"提取版本信息失败: {str(e)}")
            return f"提取失败: {frame.hex().upper()}"

    def get_current_position(self) -> float:
        """
        获取当前位置

        Returns:
            float: 当前位置 (微升)
        """
        try:
            response = self._send_query("Q18")
            if response and len(response) > 10:
                # 解析位置信息
                pos_str = response[8:14].strip()
                try:
                    self._current_position = int(pos_str)
                except ValueError:
                    pass
        except Exception as e:
            logger.error(f"获取位置失败: {str(e)}")

        return self._current_position

    def get_device_info(self) -> Dict[str, Any]:
        """
        获取设备完整信息

        Returns:
            Dict[str, Any]: 设备信息字典
        """
        info = {
            'firmware_version': self.get_firmware_version(),
            'current_position': self.get_current_position(),
            'tip_present': self.get_tip_status(),
            'status': self.get_status(),
            'is_connected': self.is_connected,
            'is_initialized': self.is_initialized,
            'config': {
                'address': self.config.address,
                'baudrate': self.config.baudrate,
                'max_speed': self.config.max_speed,
                'tip_volume': self.config.tip_volume,
                'detection_mode': self.config.detection_mode.name
            }
        }

        return info

    # ==================== 高级操作方法 ====================

    def transfer_liquid(self, source_volume: float, dispense_volume: float = None,
                       with_detection: bool = True, pre_wet: bool = False) -> bool:
        """
        完整的液体转移操作

        Args:
            source_volume: 从源容器抽吸的体积
            dispense_volume: 分配到目标容器的体积（默认等于抽吸体积）
            with_detection: 是否使用液体检测
            pre_wet: 是否进行预润湿

        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_initialized:
                raise SOPADeviceError("设备未初始化")

            dispense_volume = dispense_volume or source_volume

            logger.info(f"开始液体转移: 抽吸{source_volume}ul -> 分配{dispense_volume}ul")

            # 预润湿（如果需要）
            if pre_wet:
                logger.info("执行预润湿操作")
                if not self.aspirate(source_volume * 0.1, with_detection):
                    return False
                if not self.dispense(source_volume * 0.1):
                    return False

            # 执行液位检测（如果启用）
            if with_detection:
                if not self.liquid_level_detection():
                    logger.warning("液位检测失败，继续执行")

            # 抽吸液体
            if not self.aspirate(source_volume, with_detection):
                logger.error("抽吸失败")
                return False

            # 可选的延时
            time.sleep(0.5)

            # 分配液体
            if not self.dispense(dispense_volume, with_detection):
                logger.error("分配失败")
                return False

            logger.info("液体转移完成")
            return True

        except Exception as e:
            logger.error(f"液体转移失败: {str(e)}")
            return False

    @contextmanager
    def batch_operation(self):
        """批量操作上下文管理器"""
        logger.info("开始批量操作")
        try:
            yield self
        finally:
            logger.info("批量操作完成")

    def reset_to_home(self) -> bool:
        """回到初始位置"""
        return self.move_absolute(0)

    def emergency_stop(self):
        """紧急停止"""
        try:
            if self.serial_port and self.serial_port.is_open:
                # 发送停止命令（如果协议支持）
                self.serial_port.write(b'\x03')  # Ctrl+C
                logger.warning("执行紧急停止")
        except Exception as e:
            logger.error(f"紧急停止失败: {str(e)}")

    def __enter__(self):
        """上下文管理器入口"""
        if not self.is_connected:
            self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.disconnect()

    def __del__(self):
        """析构函数"""
        self.disconnect()


# ==================== 工厂函数和便利方法 ====================

def create_sopa_pipette(port: str = "/dev/ttyUSB0", address: int = 1,
                       baudrate: int = 115200, **kwargs) -> SOPAPipette:
    """
    创建SOPA移液器实例的便利函数

    Args:
        port: 串口端口
        address: RS485地址
        baudrate: 波特率
        **kwargs: 其他配置参数

    Returns:
        SOPAPipette: 移液器实例
    """
    config = SOPAConfig(
        port=port,
        address=address,
        baudrate=baudrate,
        **kwargs
    )

    return SOPAPipette(config)
