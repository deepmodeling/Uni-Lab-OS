import time
import logging
from dataclasses import dataclass
from enum import Enum
from typing import List

from .SharedRS485Bus import SharedRS485Bus

logger = logging.getLogger("liquid_station.xyz_modbus")


class ModbusException(Exception):
    pass


class MotorStatus(Enum):
    STANDBY = 0x0000
    RUNNING = 0x0001
    COLLISION_STOP = 0x0002
    FORWARD_LIMIT_STOP = 0x0003
    REVERSE_LIMIT_STOP = 0x0004


@dataclass
class MotorPosition:
    steps: int
    speed: int
    current: int
    status: MotorStatus


class XYZModbus:
    """
    基于 SharedRS485Bus 的 Modbus RTU 客户端。
    """

    def __init__(self, bus: SharedRS485Bus, ignore_crc_error: bool = False):
        self.bus = bus
        self.ignore_crc_error = ignore_crc_error

    def set_ignore_crc(self, flag: bool):
        self.ignore_crc_error = bool(flag)

    @staticmethod
    def _crc16(data: bytes) -> bytes:
        """
        使用你原始代码那套 CRC 算法，但这里直接返回2字节小端形式。
        """
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 0x0001:
                    crc >>= 1
                    crc ^= 0xA001
                else:
                    crc >>= 1
        return crc.to_bytes(2, "little")

    def _xfer(
        self,
        slave: int,
        payload: bytes,
        retries: int = 3,
        delay_before_read: float = 0.02,
    ) -> bytes:
        """
        发送一帧 Modbus 请求并接收响应。
        采用“先读一整块，再在其中寻找匹配帧 + CRC 校验”的策略。
        如果在所有重试中都没有找到合法帧，则抛出 ModbusException。
        """
        def hx(data: bytes) -> str:
            return " ".join(f"{b:02X}" for b in data)

        req = bytes([slave]) + payload
        frame = req + self._crc16(req)
        fn_req = payload[0]
        logger.debug("TX slave=%d fn=0x%02X frame=%s", slave, fn_req, hx(frame))

        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                with self.bus.lock:
                    if not self.bus.serial or not self.bus.serial.is_open:
                        raise ModbusException("RS485 bus not open")

                    # 清空旧数据，发送新帧
                    self.bus.reset_input()
                    self.bus.write(frame)
                    time.sleep(delay_before_read)

                    # 一次性读一大块原始数据
                    raw = self.bus.read(256)
                    if not raw:
                        raise ModbusException("No response")

                    logger.debug("RAW RX attempt %d/%d: %s", attempt, retries, hx(raw))

                    # 在 raw 里搜索对齐的帧头
                    for i in range(0, len(raw) - 4):
                        if raw[i] != slave:
                            continue
                        fn = raw[i + 1]

                        # 只接受期望功能码或其异常码
                        if fn != fn_req and fn != (fn_req | 0x80):
                            continue

                        # 根据功能码推断长度
                        if fn == 0x03:
                            # 读多个寄存器： addr fn bc data... crc
                            if i + 3 >= len(raw):
                                continue
                            bc = raw[i + 2]
                            total_len = 3 + bc + 2
                        elif fn in (0x06, 0x10):
                            # 写单/多寄存器：固定 8 字节响应
                            total_len = 8
                        else:
                            # 其他功能码，保守一些：至少 5 字节
                            total_len = 5

                        if i + total_len > len(raw):
                            continue

                        frame_candidate = raw[i : i + total_len]

                        # CRC 校验
                        frame_wo_crc = frame_candidate[:-2]
                        crc_recv = frame_candidate[-2:]
                        crc_calc = self._crc16(frame_wo_crc)
                        if crc_recv != crc_calc:
                            # CRC 不一致，跳过这个 candidate
                            continue

                        # 异常响应
                        if (fn & 0x80) != 0:
                            logger.warning("Modbus exception frame: %s", hx(frame_candidate))
                            raise ModbusException("Modbus exception response")

                        logger.debug(
                            "Parsed frame OK attempt %d/%d: %s",
                            attempt,
                            retries,
                            hx(frame_candidate),
                        )
                        return frame_candidate

                    # 如果这一轮没有找到任何合法帧
                    raise ModbusException("No valid frame found in raw data")

            except Exception as e:
                last_error = e
                logger.warning(
                    "Modbus xfer attempt %d/%d failed (slave=%s fn=0x%02X): %s",
                    attempt,
                    retries,
                    slave,
                    fn_req,
                    e,
                )
                # 下一轮重试
                continue

        # 所有重试都失败
        if last_error is not None:
            raise ModbusException(
                f"Modbus transfer failed after {retries} retries: {last_error}"
            )
        else:
            raise ModbusException(f"Modbus transfer failed after {retries} retries")

    def read_regs(self, slave: int, addr: int, count: int) -> List[int]:
        fn = 0x03
        payload = bytes([fn]) + addr.to_bytes(2, "big") + count.to_bytes(2, "big")
        resp = self._xfer(slave, payload)
        # resp: [addr, fn, byte_count, data..., crc_lo, crc_hi]
        if len(resp) < 5:
            raise ModbusException(f"read_regs: response too short: {resp!r}")

        byte_count = resp[2]
        if len(resp) != 3 + byte_count + 2:
            raise ModbusException(
                f"read_regs: byte_count mismatch: byte_count={byte_count}, resp_len={len(resp)}"
            )

        vals: List[int] = []
        for i in range(0, byte_count, 2):
            hi = resp[3 + i]
            lo = resp[3 + i + 1]
            vals.append((hi << 8) | lo)
        return vals

    def write_reg(self, slave: int, addr: int, val: int) -> bool:
        fn = 0x06
        payload = bytes([fn]) + addr.to_bytes(2, "big") + val.to_bytes(2, "big")
        try:
            resp = self._xfer(slave, payload)
        except Exception as e:
            logger.warning("write_reg error (slave=%s, addr=%s, val=%s): %s",
                           slave, addr, val, e)
            return False
        # 正常响应: addr fn hi(lo_addr) lo(lo_addr) hi(val) lo(val) crc_lo crc_hi
        return len(resp) >= 6 and resp[1] == fn

    def write_regs(self, slave: int, start: int, values: List[int]) -> bool:
        fn = 0x10
        bc = len(values) * 2
        payload = (
            bytes([fn])
            + start.to_bytes(2, "big")
            + len(values).to_bytes(2, "big")
            + bytes([bc])
        )
        for v in values:
            payload += v.to_bytes(2, "big")
        try:
            resp = self._xfer(slave, payload)
        except Exception as e:
            logger.warning(
                "write_regs error (slave=%s, start=%s, len=%s): %s",
                slave,
                start,
                len(values),
                e,
            )
            return False
        # 正常响应: addr fn start_hi start_lo qty_hi qty_lo crc_lo crc_hi
        return len(resp) >= 6 and resp[1] == fn