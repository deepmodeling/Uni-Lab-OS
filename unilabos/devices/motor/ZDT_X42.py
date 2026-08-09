# -*- coding: utf-8 -*-
"""
ZDT X42 Closed-Loop Stepper Motor Driver
RS485 Serial Communication via USB-Serial Converter

- Baudrate: 115200
"""

import serial
import time
import threading
import struct
import logging
from typing import Optional, Any

try:
    from unilabos.device_comms.universal_driver import UniversalDriver
except ImportError:
    class UniversalDriver:
        def __init__(self, *args, **kwargs):
            self.logger = logging.getLogger(self.__class__.__name__)
        def execute_command_from_outer(self, command: Any): pass

from serial.rs485 import RS485Settings


class ZDTX42Driver(UniversalDriver):
    """
    ZDT X42 闭环步进电机驱动器

    支持功能:
    - 速度模式运行
    - 位置模式运行 (相对/绝对)
    - 位置读取和清零
    - 使能/禁用控制

    通信协议:
    - 帧格式: [设备ID] [功能码] [数据...] [校验位=0x6B]
    - 响应长度根据功能码决定
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        device_id: int = 1,
        timeout: float = 0.5,
        debug: bool = False
    ):
        """
        初始化 ZDT X42 电机驱动

        Args:
            port: 串口设备路径
            baudrate: 波特率 (默认 115200)
            device_id: 设备地址 (1-255)
            timeout: 通信超时时间(秒)
            debug: 是否启用调试输出
        """
        super().__init__()
        self.id = device_id
        self.debug = debug
        self.lock = threading.RLock()
        self.status = "idle"         # 对应注册表中的 status (str)
        self.position = 0             # 对应注册表中的 position (int)

        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )

            # 启用 RS485 模式
            try:
                self.ser.rs485_mode = RS485Settings(
                    rts_level_for_tx=True,
                    rts_level_for_rx=False
                )
            except Exception:
                pass  # RS485 模式是可选的

            self.logger.info(
                f"ZDT X42 Motor connected: {port} "
                f"(Baud: {baudrate}, ID: {device_id})"
            )
            # 自动使能电机，确保初始状态可运动
            self.enable(True)

            # 启动背景轮询线程，确保 position 实时刷新
            self._stop_event = threading.Event()
            self._polling_thread = threading.Thread(
                target=self._update_loop,
                name=f"ZDTPolling_{port}",
                daemon=True
            )
            self._polling_thread.start()
        except Exception as e:
            self.logger.error(f"Failed to open serial port {port}: {e}")
            self.ser = None

    def _update_loop(self):
        """背景循环读取电机位置"""
        while not self._stop_event.is_set():
            try:
                self.get_position()
            except Exception as e:
                if self.debug:
                    self.logger.error(f"Polling error: {e}")
            time.sleep(1.0) # 每1秒刷新一次位置数据

    def _send(self, func_code: int, payload: list) -> bytes:
        """
        发送指令并接收响应

        Args:
            func_code: 功能码
            payload: 数据负载 (list of bytes)

        Returns:
            响应数据 (bytes)
        """
        if not self.ser:
            self.logger.error("Serial port not available")
            return b""

        with self.lock:
            # 清空输入缓冲区
            self.ser.reset_input_buffer()

            # 构建消息: [ID] [功能码] [数据...] [校验位=0x6B]
            message = bytes([self.id, func_code] + payload + [0x6B])

            # 发送
            self.ser.write(message)

            # 根据功能码决定响应长度
            # 查询类指令返回 10 字节，控制类指令返回 4 字节
            read_len = 10 if func_code in [0x31, 0x32, 0x35, 0x24, 0x27] else 4
            response = self.ser.read(read_len)

            # 调试输出
            if self.debug:
                sent_hex = message.hex().upper()
                recv_hex = response.hex().upper() if response else 'TIMEOUT'
                print(f"[ID {self.id}] TX: {sent_hex} → RX: {recv_hex}")

            return response

    def enable(self, on: bool = True) -> bool:
        """
        使能/禁用电机

        Args:
            on: True=使能(锁轴), False=禁用(松轴)

        Returns:
            是否成功
        """
        state = 1 if on else 0
        resp = self._send(0xF3, [0xAB, state, 0])
        return len(resp) >= 4

    def move_speed(
        self,
        speed_rpm: int,
        direction: str = "CW",
        acceleration: int = 10
    ) -> bool:
        """
        速度模式运行

        Args:
            speed_rpm: 转速 (RPM)
            direction: 方向 ("CW"=顺时针, "CCW"=逆时针)
            acceleration: 加速度 (0-255)

        Returns:
            是否成功
        """
        dir_val = 0 if direction.upper() in ["CW", "顺时针"] else 1
        speed_bytes = struct.pack('>H', int(speed_rpm))
        self.status = f"moving@{speed_rpm}rpm"
        resp = self._send(0xF6, [dir_val, speed_bytes[0], speed_bytes[1], acceleration, 0])
        return len(resp) >= 4

    def move_position(
        self,
        pulses: int,
        speed_rpm: int,
        direction: str = "CW",
        acceleration: int = 10,
        absolute: bool = False
    ) -> bool:
        """
        位置模式运行

        Args:
            pulses: 脉冲数
            speed_rpm: 转速 (RPM)
            direction: 方向 ("CW"=顺时针, "CCW"=逆时针)
            acceleration: 加速度 (0-255)
            absolute: True=绝对位置, False=相对位置

        Returns:
            是否成功
        """
        dir_val = 0 if direction.upper() in ["CW", "顺时针"] else 1
        speed_bytes = struct.pack('>H', int(speed_rpm))
        self.status = f"moving_to_{pulses}"
        pulse_bytes = struct.pack('>I', int(pulses))
        abs_flag = 1 if absolute else 0

        payload = [
            dir_val,
            speed_bytes[0], speed_bytes[1],
            acceleration,
            pulse_bytes[0], pulse_bytes[1], pulse_bytes[2], pulse_bytes[3],
            abs_flag,
            0
        ]

        resp = self._send(0xFD, payload)
        return len(resp) >= 4

    def stop(self) -> bool:
        """
        停止电机

        Returns:
            是否成功
        """
        self.status = "idle"
        resp = self._send(0xFE, [0x98, 0])
        return len(resp) >= 4

    def rotate_quarter(self, speed_rpm: int = 60, direction: str = "CW") -> bool:
        """
        电机旋转 1/4 圈 (阻塞式)
        假设电机细分为 3200 脉冲/圈，1/4 圈 = 800 脉冲
        """
        pulses = 800
        success = self.move_position(pulses=pulses, speed_rpm=speed_rpm, direction=direction, absolute=False)

        if success:
            # 计算预估旋转时间并进行阻塞等待 (Time = revolutions / (RPM/60))
            # 1/4 rev / (RPM/60) = 15.0 / RPM
            estimated_time = 15.0 / max(1, speed_rpm)
            time.sleep(estimated_time + 0.5)  # 额外给 0.5 秒缓冲
            self.status = "idle"

        return success

    def wait_time(self, duration_s: float) -> bool:
        """
        等待指定时间 (秒)
        """
        self.logger.info(f"Waiting for {duration_s} seconds...")
        time.sleep(duration_s)
        return True

    def set_zero(self) -> bool:
        """
        清零当前位置

        Returns:
            是否成功
        """
        resp = self._send(0x0A, [])
        return len(resp) >= 4

    def get_position(self) -> Optional[int]:
        """
        读取当前位置 (脉冲数)

        Returns:
            当前位置脉冲数，失败返回 None
        """
        resp = self._send(0x32, [])

        if len(resp) >= 8:
            # 响应格式: [ID] [Func] [符号位] [数值4字节] [校验]
            sign = resp[2]  # 0=正, 1=负
            value = struct.unpack('>I', resp[3:7])[0]
            self.position = -value if sign == 1 else value

            if self.debug:
                print(f"[Position] Raw: {resp.hex().upper()}, Parsed: {self.position}")

            return self.position

        self.logger.warning("Failed to read position")
        return None

    def close(self):
        """关闭串口连接并停止线程"""
        if hasattr(self, '_stop_event'):
            self._stop_event.set()

        if self.ser and self.ser.is_open:
            self.ser.close()
            self.logger.info("Serial port closed")


# ============================================================
# 测试和调试代码
# ============================================================

def test_motor():
    """基础功能测试"""
    logging.basicConfig(level=logging.INFO)

    print("="*60)
    print("ZDT X42 电机驱动测试")
    print("="*60)

    driver = ZDTX42Driver(
        port="/dev/tty.usbserial-3110",
        baudrate=115200,
        device_id=2,
        debug=True
    )

    if not driver.ser:
        print("❌ 串口打开失败")
        return

    try:
        # 测试 1: 读取位置
        print("\n[1] 读取当前位置")
        pos = driver.get_position()
        print(f"✓ 当前位置: {pos} 脉冲")

        # 测试 2: 使能
        print("\n[2] 使能电机")
        driver.enable(True)
        time.sleep(0.3)
        print("✓ 电机已锁定")

        # 测试 3: 相对位置运动
        print("\n[3] 相对位置运动 (1000脉冲)")
        driver.move_position(pulses=1000, speed_rpm=60, direction="CW")
        time.sleep(2)
        pos = driver.get_position()
        print(f"✓ 新位置: {pos}")

        # 测试 4: 速度运动
        print("\n[4] 速度模式 (30RPM, 3秒)")
        driver.move_speed(speed_rpm=30, direction="CW")
        time.sleep(3)
        driver.stop()
        pos = driver.get_position()
        print(f"✓ 停止后位置: {pos}")

        # 测试 5: 禁用
        print("\n[5] 禁用电机")
        driver.enable(False)
        print("✓ 电机已松开")

        print("\n" + "="*60)
        print("✅ 测试完成")
        print("="*60)

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        driver.close()


if __name__ == "__main__":
    test_motor()
