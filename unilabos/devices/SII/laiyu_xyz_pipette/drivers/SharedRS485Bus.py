# bus_rs485.py
import threading
import time
try:
    import serial
except Exception as e:
    raise RuntimeError("Please install pyserial: pip install pyserial") from e
import logging

logger = logging.getLogger("liquid_station.rs485")


class SharedRS485Bus:
    """
    所有设备（XYZ 轴 + 移液枪）使用同一个 485 接口，
    加入线程锁保证同一时刻只有一个请求在进行。
    """

    def __init__(self, port: str = "COM3", baudrate: int = 115200, timeout: float = 0.2):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: serial.Serial | None = None
        self.lock = threading.Lock()

    def open(self) -> bool:
        """开启串口，8N1，无校验。"""
        if self.serial and self.serial.is_open:
            return True
        self.serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
        )
        logger.info(f"Opened RS485 bus on {self.port}")
        return True

    def close(self):
        if self.serial and self.serial.is_open:
            self.serial.close()
            logger.info("Closed RS485 bus")

    def reset_input(self):
        """清空接收缓冲。"""
        if self.serial:
            self.serial.reset_input_buffer()

    def write(self, data: bytes):
        """发送数据。"""
        if not self.serial or not self.serial.is_open:
            raise RuntimeError("RS485 bus not open")
        self.serial.write(data)

    def read(self, n: int = 256) -> bytes:
        """读取最多 n 字节（受 timeout 影响）。"""
        if not self.serial or not self.serial.is_open:
            raise RuntimeError("RS485 bus not open")
        return self.serial.read(n)

    def read_exact(self, n: int, overall_timeout: float = 0.3) -> bytes:
        """
        精确读取 n 字节，带整体超时。
        超时返回已读到的内容（可能 < n）。
        """
        if n <= 0:
            return b""
        buf = b""
        deadline = time.time() + overall_timeout
        while len(buf) < n:
            if time.time() > deadline:
                break
            need = n - len(buf)
            chunk = self.read(need)
            if chunk:
                buf += chunk
            else:
                time.sleep(0.001)
        return buf