import os
import asyncio
from asyncio import Event, Future, Lock, Task
from enum import Enum
from dataclasses import dataclass
import re
import time
from typing import Any, Union, Optional, overload

import serial.tools.list_ports
from serial import Serial
from serial.serialutil import SerialException

from unilabos.messages import Point3D
from unilabos.device_runtime.node import DeviceNode


class GrblCNCConnectionError(Exception):
    pass


@dataclass(frozen=True, kw_only=True)
class GrblCNCInfo:
    port: str
    address: str = "1"
    
    limits: tuple[int, int, int, int, int, int] = (-150, 150, -200, 0, 0, 60)

    def create(self):
        return GrblCNCAsync(self.port, self.address, self.limits)


class GrblCNCAsync:
    _status: str = "Offline"
    _position: Point3D = Point3D(x=0.0, y=0.0, z=0.0)
    _ros_node: DeviceNode
    
    def __init__(self, port: str, address: str = "1", limits: tuple[int, int, int, int, int, int] = (-150, 150, -200, 0, 0, 60)):
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
        self._error_event = Event()
        self._query_future = Future[Any]()
        self._query_lock = Lock()
        self._read_task: Optional[Task[None]] = None
        self._read_extra_line = False
        self._run_future: Optional[Future[Any]] = None
        self._run_lock = Lock()
    
    def post_init(self, ros_node: DeviceNode):
        self._ros_node = ros_node
    
    def _read_all(self):
        data = self._serial.read_until(b"\n")
        data_decoded = data.decode()
        while not "ok" in data_decoded and not "Grbl" in data_decoded:
            data += self._serial.read_until(b"\n")
            data_decoded = data.decode()
        return data

    async def _read_loop(self):
        try:
            while True:
                self._receive((await asyncio.to_thread(lambda: self._read_all())))
        except SerialException as e:
            raise GrblCNCConnectionError from e
        finally:
            if not self._closing:
                self._error_event.set()

            if self._query_future and not self._query_future.done():
                self._query_future.set_exception(GrblCNCConnectionError())
            if self._run_future and not self._run_future.done():
                self._run_future.set_exception(GrblCNCConnectionError())

    @overload
    async def _query(self, command: str, dtype: type[bool]) -> bool:
        pass

    @overload
    async def _query(self, command: str, dtype: type[int]) -> int:
        pass

    @overload
    async def _query(self, command: str, dtype = None) -> str:
        pass

    async def _query(self, command: str, dtype: Optional[type] = None):
        async with self._query_lock:
            if self._closing or self._error_event.is_set():
                raise GrblCNCConnectionError

            self._query_future = Future[Any]()

            self._read_extra_line = command.startswith("?")
            run = ''
            full_command = f"{command}{run}\n"
            full_command_data = bytearray(full_command, 'ascii')
            
            try:
                # await asyncio.to_thread(lambda: self._serial.write(full_command_data))
                self._serial.write(full_command_data)
                return self._parse(await asyncio.wait_for(asyncio.shield(self._query_future), timeout=5.0), dtype=dtype)
            except (SerialException, asyncio.TimeoutError) as e:
                self._error_event.set()
                raise GrblCNCConnectionError from e
            finally:
                self._query_future = None

    def _parse(self, data: bytes, dtype: Optional[type] = None):
        response = data.decode()

        if dtype == bool:
            return response == "1"
        elif dtype == int:
            return int(response)
        else:
            return response

    def _receive(self, data: bytes):
        ascii_string = "".join(chr(byte) for byte in data)
        was_busy = self._busy
        self._busy = "Idle" not in ascii_string

        # if self._read_extra_line and ascii_string.startswith("ok"):
        #     self._read_extra_line = False
        #     return
        if self._run_future and was_busy and not self._busy:
            self._run_future.set_result(data)
        if self._query_future:
            self._query_future.set_result(data)
        else:
            raise Exception("Dropping data")

    async def _run(self, command: str):
        async with self._run_lock:
            self._run_future = Future[Any]()
            # self._busy = True

            try:
                await self._query(command)
                while True:
                    await self._ros_node.sleep(0.2)  # Wait for 0.5 seconds before polling again
                    
                    status = await self.get_status()
                    if "Idle" in status:
                        break
                await asyncio.shield(self._run_future)
            finally:
                self._run_future = None

    async def initialize(self):
        time.sleep(0.5)
        await self._run("G0X0Y0Z0")
        status = await self.get_status()
        return status
    
    # Operations

    # Status Queries
    
    @property
    def status(self) -> str:
        return self._status

    async def get_status(self):
        __pos_pattern__ = re.compile('.Pos:(\-?\d+\.\d+),(\-?\d+\.\d+),(\-?\d+\.\d+)')
        __status_pattern__ = re.compile('<([a-zA-Z]+),')
        
        response = await self._query("?")
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

    async def set_position(self, position: Point3D):
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
        return await self._run(f"G0X{x:.3f}Y{y:.3f}Z{z:.3f}")
    
    async def move_through_points(self, points: list[Point3D]):
        for i, point in enumerate(points):
            self._pose_number = i
            self.pose_number_remaining = len(points) - i
            await self.set_position(point)
            await self._ros_node.sleep(0.5)
        self._step_number = -1

    async def stop_operation(self):
        return await self._run("T")
    
    # Queries

    async def wait_error(self):
        await self._error_event.wait()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def open(self):
        if self._read_task:
            raise GrblCNCConnectionError
        self._read_task = self._ros_node.create_task(self._read_loop())

        try:
            await self.get_status()
        except Exception:
            await self.close()
            raise

    async def close(self):
        if self._closing or not self._read_task:
            raise GrblCNCConnectionError

        self._closing = True
        self._read_task.cancel()

        try:
            await self._read_task
        except asyncio.CancelledError:
            pass
        finally:
            del self._read_task

        self._serial.close()

    @staticmethod
    def list():
        for item in serial.tools.list_ports.comports():
            yield GrblCNCInfo(port=item.device)
