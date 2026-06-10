import os
import asyncio
from threading import Event, Lock
from enum import Enum
from dataclasses import dataclass
import re
import time
from typing import Any, Union, Optional, overload

import serial.tools.list_ports
from serial import Serial
from serial.serialutil import SerialException

from unilabos.messages import Point3D


class GrblCNCConnectionError(Exception):
    pass


@dataclass(frozen=True, kw_only=True)
class GrblCNCInfo:
    port: str
    address: str = "1"

    limits: tuple[int, int, int, int, int, int] = (-150, 150, -200, 0, -80, 0)

    def create(self):
        return GrblCNC(self.port, self.address, self.limits)


class GrblCNC:
    _status: str = "Offline"
    _position: Point3D = Point3D(x=0.0, y=0.0, z=0.0)
    _spindle_speed: float = 0.0

    def __init__(self, port: str, address: str = "1", limits: tuple[int, int, int, int, int, int] = (-150, 150, -200, 0, -80, 0)):
        self.port = port
        self.address = address

        self.limits = limits

        try:
            self._serial = Serial(
                baudrate=115200,
                port=port
            )
        except (OSError, SerialException) as e:
            raise GrblCNCConnectionError from e

        self._busy = False
        self._closing = False
        self._pose_number = self.pose_number_remaining = -1

        self._query_lock = Lock()
        self._run_lock = Lock()
        self._error_event = Event()

    def _read_all(self):
        data = self._serial.read_until(b"\n")
        data_decoded = data.decode()
        while not "ok" in data_decoded and not "Grbl" in data_decoded:
            data += self._serial.read_until(b"\n")
            data_decoded = data.decode()
        return data

    @overload
    def _query(self, command: str, dtype: type[bool]) -> bool:
        pass

    @overload
    def _query(self, command: str, dtype: type[int]) -> int:
        pass

    @overload
    def _query(self, command: str, dtype=None) -> str:
        pass

    def _query(self, command: str, dtype: Optional[type] = None):
        with self._query_lock:
            if self._closing or self._error_event.is_set():
                raise GrblCNCConnectionError

            self._read_extra_line = command.startswith("?")
            run = ''
            full_command = f"{command}{run}\n"
            full_command_data = bytearray(full_command, 'ascii')

            try:
                # await asyncio.to_thread(lambda: self._serial.write(full_command_data))
                self._serial.write(full_command_data)
                time.sleep(0.1)
                return self._receive(self._read_all())
            except (SerialException, asyncio.TimeoutError) as e:
                self._error_event.set()
                raise GrblCNCConnectionError from e

    def _receive(self, data: bytes):
        ascii_string = "".join(chr(byte) for byte in data)
        was_busy = self._busy
        self._busy = "Idle" not in ascii_string
        return ascii_string

    def _run(self, command: str):
        with self._run_lock:
            try:
                self._query(command)
                while True:
                    time.sleep(0.2)  # Wait for 0.5 seconds before polling again

                    status = self.get_status()
                    if "Idle" in status:
                        break
            except:
                self._error_event.set()

    def initialize(self):
        time.sleep(0.5)
        self._run("G0X0Y0Z0")
        status = self.get_status()
        return status

    # Operations

    # Status Queries

    @property
    def status(self) -> str:
        return self._status

    def get_status(self):
        __pos_pattern__ = re.compile('.Pos:(\-?\d+\.\d+),(\-?\d+\.\d+),(\-?\d+\.\d+)')
        __status_pattern__ = re.compile('<([a-zA-Z]+),')

        response = self._query("?")
        pat = re.search(__pos_pattern__, response)
        if pat is not None:
            pos = pat.group().split(":")[1].split(",")
            self._status = re.search(__status_pattern__, response).group(1).lstrip("<").rstrip(",")
            self._position = Point3D(x=float(pos[0]), y=float(pos[1]), z=float(pos[2]))

        return self.status

    # Position Setpoint and Queries

    @property
    def position(self) -> Point3D:
        # 由于此时一定调用过 get_status，所以 position 一定是被更新过的
        return self._position

    def get_position(self):
        return self.position

    def set_position(self, position: Point3D):
        """
        Move to absolute position (unit: mm)

        Args:
            x, y, z: float

        Returns:
            None
        """
        x = max(self.limits[0], min(self.limits[1], position.x))
        y = max(self.limits[2], min(self.limits[3], position.y))
        z = max(self.limits[4], min(self.limits[5], position.z))
        return self._run(f"G0X{x:.3f}Y{y:.3f}Z{z:.3f}")

    def move_through_points(self, positions: list[Point3D]):
        for i, point in enumerate(positions):
            self._pose_number = i
            self.pose_number_remaining = len(positions) - i
            self.set_position(point)
            time.sleep(0.5)
        self._pose_number = -1

    @property
    def spindle_speed(self) -> float:
        return self._spindle_speed

    # def get_spindle_speed(self):
    #     self._spindle_speed = float(self._query("M3?"))
    #     return self.spindle_speed

    def set_spindle_speed(self, spindle_speed: float, max_velocity: float = 500):
        if spindle_speed < 0:
            spindle_speed = 0
            self._run("M5")
        else:
            spindle_speed = min(max_velocity, spindle_speed)
            self._run(f"M3 S{spindle_speed}")
        self._spindle_speed = spindle_speed

    def stop_operation(self):
        return self._run("T")

    # Queries

    async def wait_error(self):
        await self._error_event.wait()

    @staticmethod
    def list():
        for item in serial.tools.list_ports.comports():
            yield GrblCNCInfo(port=item.device)
