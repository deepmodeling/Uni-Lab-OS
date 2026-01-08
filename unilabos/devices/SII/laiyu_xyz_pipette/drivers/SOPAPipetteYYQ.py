# pipette.py
import time
import logging
from dataclasses import dataclass

from .SharedRS485Bus import SharedRS485Bus

logger = logging.getLogger("liquid_station.pipette_driver")


@dataclass
class SOPAConfig:
    address: int = 4      # 固定设备地址
    timeout: float = 2.0


class SOPAPipetteYYQ:
    """SOPA 移液枪 YYQ 驱动（共享 RS485 总线，文本协议）。"""

    def __init__(self, bus: SharedRS485Bus, config: SOPAConfig = SOPAConfig()):
        self.bus = bus
        self.config = config

    def _send_and_read(self, cmd: str, delay_before_read: float = 0.2) -> str:
        """
        在一把锁内发送一条命令并读取响应，保证事务原子。
        cmd: 不含地址/结束符的正文，如 "HE" / "P100"
        """
        address = str(self.config.address)
        full_cmd = f"/{address}{cmd}E".encode("ascii")
        checksum = bytes([sum(full_cmd) & 0xFF])
        payload = full_cmd + checksum

        with self.bus.lock:
            self.bus.reset_input()
            self.bus.write(payload)
            logger.debug("[YYQ] TX: %r", payload)
            time.sleep(delay_before_read)
            data = b""
            if self.bus.serial and self.bus.serial.in_waiting:
                data = self.bus.serial.read_all()
        txt = data.decode(errors="ignore")
        if txt:
            logger.debug("[YYQ] RX: %r", txt)
        return txt

    def initialize(self) -> bool:
        try:
            logger.info("初始化移液枪中...")
            self._send_and_read("HE")
            time.sleep(10)
            self.is_initialized = True
            logger.info("移液枪初始化完成")
            return True
        except Exception as e:
            logger.error("初始化失败: %s", e)
            return False

    def eject_tip(self) -> bool:
        try:
            self._send_and_read("RE")
            time.sleep(1)
            logger.info("枪头已弹出")
            return True
        except Exception as e:
            logger.error("弹出枪头失败: %s", e)
            return False

    def aspirate(self, volume_uL: float):
        try:
            vol = int(volume_uL)
            logger.info("吸液 %d µL...", vol)
            self._send_and_read(f"P{vol}")
            time.sleep(max(0.2, vol / 200.0))
            logger.info("吸液完成")
        except Exception as e:
            logger.error("吸液失败: %s", e)

    def dispense(self, volume_uL: float):
        try:
            vol = int(volume_uL)
            logger.info("排液 %d µL...", vol)
            self._send_and_read(f"D{vol}")
            time.sleep(max(0.2, vol / 200.0))
            logger.info("排液完成")
        except Exception as e:
            logger.error("排液失败: %s", e)

    def set_max_speed(self, speed: int):
        try:
            self._send_and_read(f"s{speed}")
            time.sleep(1)
            logger.info("设置最高速度完成")
        except Exception as e:
            logger.error("设置最高速度失败: %s", e)

    def set_start_speed(self, speed: int):
        try:
            self._send_and_read(f"b{speed}")
            time.sleep(1)
            logger.info("设置启动速度完成")
        except Exception as e:
            logger.error("设置启动速度失败: %s", e)

    def set_cutoff_speed(self, speed: int):
        try:
            self._send_and_read(f"c{speed}")
            time.sleep(1)
            logger.info("设置断流速度完成")
        except Exception as e:
            logger.error("设置断流速度失败: %s", e)

    def set_acceleration(self, accel: int):
        try:
            self._send_and_read(f"a{accel}")
            time.sleep(1)
            logger.info("设置加速度完成")
        except Exception as e:
            logger.error("设置加速度失败: %s", e)

    def get_status(self) -> str:
        return self._send_and_read("Q", delay_before_read=0.1)

    def get_tip_status(self) -> bool:
        """
        True: 有枪头；False: 无枪头或通信失败。
        """
        resp = self._send_and_read("Q28", delay_before_read=0.1)
        if resp:
            if "T1" in resp:
                return True
            if "T0" in resp:
                return False
        return False