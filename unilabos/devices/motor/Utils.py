import struct
import time
from typing import Union

from .Consts import Config


def calculate_modbus_crc16(data: bytes) -> tuple[int, int]:
    """
    计算 Modbus RTU 的 CRC16 校验码，返回 (low_byte, high_byte)。
    data 可以是 bytes 或者 bytearray
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if (crc & 0x0001):
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1

    # 低字节在前、高字节在后
    low_byte = crc & 0xFF
    high_byte = (crc >> 8) & 0xFF
    return low_byte, high_byte


def create_command(slave_id, func_code, address, data):
    """
    生成完整的 Modbus 通信指令：
    - 第1字节: 从站地址
    - 第2字节: 功能码
    - 第3~4字节: 寄存器地址 (大端)
    - 第5~6字节: 数据(或读寄存器个数) (大端)
    - 第7~8字节: CRC校验, 先低后高
    """
    # 按照大端格式打包：B(1字节), B(1字节), H(2字节), H(2字节)
    # 例如: 0x03, 0x03, 0x0191, 0x0001
    # 生成的命令将是: 03 03 01 91 00 01 (不含 CRC)
    command = struct.pack(">B B H H", slave_id, func_code, address, data)

    # 计算CRC，并将低字节、后高字节拼到末尾
    low_byte, high_byte = calculate_modbus_crc16(command)
    return command + bytes([low_byte, high_byte])


def send_command(ser, command) -> Union[bytes, str]:
    """通过串口发送指令并打印响应"""
    # Modbus RTU 半双工，发送前拉高 RTS
    ser.setRTS(True)
    time.sleep(0.02)
    ser.write(command)  # 发送指令
    if Config.OPEN_DEVICE:
        # 如果是实际串口，就打印16进制的发送内容
        print(f"发送的数据: ", end="")
        for ind, c in enumerate(command.hex().upper()):
            if ind % 2 == 0 and ind != 0:
                print(" ", end="")
            print(c, end="")

    # 发送完成后，切换到接收模式
    ser.setRTS(False)

    # 读取响应，具体长度要看从站返回，有时多字节
    response = ser.read(8)  # 假设响应是8字节
    print(f"接收到的数据: ", end="")
    for ind, c in enumerate(response.hex().upper()):
        if ind % 2 == 0 and ind != 0:
            print(" ", end="")
        print(c, end="")
    print()
    return response


def get_result_byte_int(data: bytes, byte_start: int = 6, byte_end: int = 10) -> int:
    return int(data.hex()[byte_start:byte_end], 16)


def get_result_byte_str(data: bytes, byte_start: int = 6, byte_end: int = 10) -> str:
    return data.hex()[byte_start:byte_end]


def run_commands(ser, duration=0.1, *commands):
    for cmd in commands:
        if isinstance(cmd, list):
            for c in cmd:
                send_command(ser, c)
                time.sleep(duration)
        else:
            send_command(ser, cmd)
            time.sleep(duration)
