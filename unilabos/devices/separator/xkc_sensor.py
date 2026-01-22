# -*- coding: utf-8 -*-
"""
XKC RS485 液位传感器 (Modbus RTU)

说明:
    1. 遵循 Modbus-RTU 协议。
    2. 数据寄存器: 0x0001 (液位状态, 1=有液, 0=无液), 0x0002 (RSSI 信号强度)。
    3. 地址寄存器: 0x0004 (可读写, 范围 1-254)。
    4. 波特率寄存器: 0x0005 (可写, 代码表见 change_baudrate 方法)。
"""

import struct
import threading
import time
import logging
import serial
from typing import Optional, Dict, Any, List

from unilabos.device_comms.universal_driver import UniversalDriver

class TransportManager:
    """
    统一通信管理类。
    仅支持 串口 (Serial/有线) 连接。
    """
    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 3.0, logger=None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.logger = logger
        self.lock = threading.RLock() # 线程锁，确保多设备共用一个连接时不冲突

        self.serial = None
        self._connect_serial()

    def _connect_serial(self):
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout
            )
        except Exception as e:
            raise ConnectionError(f"Serial open failed: {e}")

    def close(self):
        """关闭连接"""
        if self.serial and self.serial.is_open:
            self.serial.close()

    def clear_buffer(self):
        """清空缓冲区 (Thread-safe)"""
        with self.lock:
            if self.serial:
                self.serial.reset_input_buffer()

    def write(self, data: bytes):
        """发送原始字节"""
        with self.lock:
            if self.serial:
                self.serial.write(data)

    def read(self, size: int) -> bytes:
        """读取指定长度字节"""
        if self.serial:
            return self.serial.read(size)
        return b''

class XKCSensorDriver(UniversalDriver):
    """XKC RS485 液位传感器 (Modbus RTU)"""

    def __init__(self, port: str, baudrate: int = 9600, device_id: int = 6,
                 threshold: int = 300, timeout: float = 3.0, debug: bool = False):
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.device_id = device_id
        self.threshold = threshold
        self.timeout = timeout
        self.debug = debug
        self.level = False
        self.rssi = 0
        self.status = {"level": self.level, "rssi": self.rssi}

        try:
            self.transport = TransportManager(port, baudrate, timeout, logger=self.logger)
            self.logger.info(f"XKCSensorDriver connected to {port} (ID: {device_id})")
        except Exception as e:
            self.logger.error(f"Failed to connect XKCSensorDriver: {e}")
            self.transport = None

        # 启动背景轮询线程，确保 status 实时刷新
        self._stop_event = threading.Event()
        self._polling_thread = threading.Thread(
            target=self._update_loop,
            name=f"XKCPolling_{port}",
            daemon=True
        )
        if self.transport:
            self._polling_thread.start()

    def _update_loop(self):
        """背景循环读取传感器数据"""
        while not self._stop_event.is_set():
            try:
                self.read_level()
            except Exception as e:
                if self.debug:
                    self.logger.error(f"Polling error: {e}")
            time.sleep(2.0) # 每2秒刷新一次数据

    def _crc(self, data: bytes) -> bytes:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001: crc = (crc >> 1) ^ 0xA001
                else: crc >>= 1
        return struct.pack('<H', crc)

    def read_level(self) -> Optional[Dict[str, Any]]:
        """
        读取液位。
        返回: {'level': bool, 'rssi': int}
        """
        if not self.transport:
            return None

        with self.transport.lock:
            self.transport.clear_buffer()
            # Modbus Read Registers: 01 03 00 01 00 02 CRC
            payload = struct.pack('>HH', 0x0001, 0x0002)
            msg = struct.pack('BB', self.device_id, 0x03) + payload
            msg += self._crc(msg)

            if self.debug:
                self.logger.info(f"TX (ID {self.device_id}): {msg.hex().upper()}")

            self.transport.write(msg)

            # Read header
            h = self.transport.read(3) # Addr, Func, Len
            if self.debug:
                self.logger.info(f"RX Header: {h.hex().upper()}")

            if len(h) < 3: return None
            length = h[2]

            # Read body + CRC
            body = self.transport.read(length + 2)
            if self.debug:
                self.logger.info(f"RX Body+CRC: {body.hex().upper()}")
            if len(body) < length + 2:
                # Firmware bug fix specific to some modules
                if len(body) == 4 and length == 4:
                    pass
                else:
                    return None

            data = body[:-2]
            # 根据手册说明:
            # 寄存器 0x0001 (data[0:2]): 液位状态 (00 01 为有液, 00 00 为无液)
            # 寄存器 0x0002 (data[2:4]): 信号强度 RSSI

            hw_level = False
            rssi = 0

            if len(data) >= 4:
                hw_level = ((data[0] << 8) | data[1]) == 1
                rssi = (data[2] << 8) | data[3]
            elif len(data) == 2:
                # 兼容模式: 某些老固件可能只返回 1 个寄存器
                rssi = (data[0] << 8) | data[1]
                hw_level = rssi > self.threshold
            else:
                return None

            # 最终判定: 优先使用硬件层级的 level 判定，但 RSSI 阈值逻辑作为补充/校验
            # 注意: 如果用户显式设置了 THRESHOLD，我们可以在逻辑中做权衡
            self.level = hw_level or (rssi > self.threshold)
            self.rssi = rssi
            result = {
                'level': self.level,
                'rssi': self.rssi
            }
            self.status = result
            return result

    def wait_level(self, target_state: bool, timeout: float = 60.0) -> bool:
        """
        等待液位达到目标状态 (阻塞式)
        """
        self.logger.info(f"Waiting for level: {target_state}")
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            res = self.read_level()
            if res and res.get('level') == target_state:
                return True
            time.sleep(0.5)
        self.logger.warning(f"Wait level timeout ({timeout}s)")
        return False

    def wait_for_liquid(self, target_state: bool, timeout: float = 120.0) -> bool:
        """
        实时检测电导率(RSSI)并等待用户指定的“有液”或“无液”状态。
        一旦检测到符合目标状态，立即返回。

        Args:
            target_state: True 为“有液”, False 为“无液”
            timeout: 最大等待时间(秒)
        """
        state_str = "有液" if target_state else "无液"
        self.logger.info(f"开始实时检测电导率，等待状态: {state_str} (超时: {timeout}s)")

        start_time = time.time()
        while (time.time() - start_time) < timeout:
            res = self.read_level() # 内部已更新 self.level 和 self.rssi
            if res:
                current_level = res.get('level')
                current_rssi = res.get('rssi')
                if current_level == target_state:
                    self.logger.info(f"✅ 检测到目标状态: {state_str} (当前电导率/RSSI: {current_rssi})")
                    return True

                if self.debug:
                    self.logger.debug(f"当前状态: {'有液' if current_level else '无液'}, RSSI: {current_rssi}")

            time.sleep(0.2) # 高频采样

        self.logger.warning(f"❌ 等待 {state_str} 状态超时 ({timeout}s)")
        return False

    def set_threshold(self, threshold: int):
        """设置液位判定阈值"""
        self.threshold = int(threshold)
        self.logger.info(f"Threshold updated to: {self.threshold}")

    def change_device_id(self, new_id: int) -> bool:
        """
        修改设备的 Modbus 从站地址。
        寄存器: 0x0004, 功能码: 0x06
        """
        if not (1 <= new_id <= 254):
            self.logger.error(f"Invalid device ID: {new_id}. Must be 1-254.")
            return False

        self.logger.info(f"Changing device ID from {self.device_id} to {new_id}")
        success = self._write_single_register(0x0004, new_id)
        if success:
            self.device_id = new_id # 更新内存中的地址
            self.logger.info(f"Device ID update command sent successfully (target {new_id}).")
        return success

    def change_baudrate(self, baud_code: int) -> bool:
        """
        更改通讯波特率 (寄存器: 0x0005)。
        设置成功后传感器 LED 会闪烁，通常无数据返回。

        波特率代码对照表 (16进制):
        05: 2400
        06: 4800
        07: 9600 (默认)
        08: 14400
        09: 19200
        0A: 28800
        0C: 57600
        0D: 115200
        0E: 128000
        0F: 256000
        """
        self.logger.info(f"Sending baudrate change command (Code: {baud_code:02X})")
        # 写入寄存器 0x0005
        self._write_single_register(0x0005, baud_code)
        self.logger.info("Baudrate change command executed. Device LED should flash. Please update connection settings.")
        return True

    def factory_reset(self) -> bool:
        """
        恢复出厂设置 (通过广播地址 FF)。
        设置地址为 01，逻辑为向 0x0004 写入 0x0002
        """
        self.logger.info("Sending factory reset command via broadcast address FF...")
        # 广播指令通常无回显
        self._write_single_register(0x0004, 0x0002, slave_id=0xFF)
        self.logger.info("Factory reset command sent. Device address should be 01 now.")
        return True

    def _write_single_register(self, reg_addr: int, value: int, slave_id: Optional[int] = None) -> bool:
        """内部辅助函数: Modbus 功能码 06 写单个寄存器"""
        if not self.transport: return False

        target_id = slave_id if slave_id is not None else self.device_id
        msg = struct.pack('BBHH', target_id, 0x06, reg_addr, value)
        msg += self._crc(msg)

        with self.transport.lock:
            self.transport.clear_buffer()
            if self.debug:
                self.logger.info(f"TX Write (Reg {reg_addr:#06x}): {msg.hex().upper()}")

            self.transport.write(msg)

            # 广播地址、波特率修改或厂家特定指令可能无回显
            if target_id == 0xFF or reg_addr == 0x0005:
                time.sleep(0.5)
                return True

            # 等待返回 (正常应返回相同报文)
            resp = self.transport.read(len(msg))
            if self.debug:
                self.logger.info(f"RX Write Response: {resp.hex().upper()}")

            return resp == msg

    def close(self):
        if self.transport:
            self.transport.close()

if __name__ == "__main__":
    # 快速实例化测试
    import logging
    # 减少冗余日志，仅显示重要信息
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    # 硬件配置 (根据实际情况修改)
    TEST_PORT = "/dev/tty.usbserial-3110"
    SLAVE_ID = 1
    THRESHOLD = 300

    print("\n" + "="*50)
    print(f"  XKC RS485 传感器独立测试程序")
    print(f"  端口: {TEST_PORT} | 地址: {SLAVE_ID} | 阈值: {THRESHOLD}")
    print("="*50)

    sensor = XKCSensorDriver(port=TEST_PORT, device_id=SLAVE_ID, threshold=THRESHOLD, debug=False)

    try:
        if sensor.transport:
            print(f"\n开始实时连续采样测试 (持续 15 秒)...")
            print(f"按 Ctrl+C 可提前停止\n")

            start_time = time.time()
            duration = 15
            count = 0

            while time.time() - start_time < duration:
                count += 1
                res = sensor.read_level()
                if res:
                    rssi = res['rssi']
                    level = res['level']
                    status_str = "【有液】" if level else "【无液】"
                    # 使用 \r 实现单行刷新显示 (或者不刷，直接打印历史)
                    # 为了方便查看变化，我们直接打印
                    elapsed = time.time() - start_time
                    print(f" [{elapsed:4.1f}s] 采样 {count:<3}: 电导率/RSSI = {rssi:<5} | 判定结果: {status_str}")
                else:
                    print(f" [{time.time()-start_time:4.1f}s] 采样 {count:<3}: 通信失败 (无响应)")

                time.sleep(0.5) # 每秒采样 2 次

            print(f"\n--- 15 秒采样测试完成 (总计 {count} 次) ---")

            # [3] 测试动态修改阈值
            print(f"\n[3] 动态修改阈值演示...")
            new_threshold = 400
            sensor.set_threshold(new_threshold)
            res = sensor.read_level()
            if res:
                print(f"  采样 (当前阈值={new_threshold}): 电导率/RSSI = {res['rssi']:<5} | 判定结果: {'【有液】' if res['level'] else '【无液】'}")
            sensor.set_threshold(THRESHOLD) # 还原

    except KeyboardInterrupt:
        print("\n[!] 用户中断测试")
    except Exception as e:
        print(f"\n[!] 测试运行出错: {e}")
    finally:
        sensor.close()
        print("\n--- 测试程序已退出 ---\n")
