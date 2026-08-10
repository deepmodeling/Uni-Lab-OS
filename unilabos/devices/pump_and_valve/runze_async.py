import asyncio
from asyncio import Event, Future, Lock, Task
from enum import Enum
from dataclasses import dataclass
from typing import Any, Union, Optional, overload

import serial.tools.list_ports
from serial import Serial
from serial.serialutil import SerialException

from unilabos.device_runtime.node import DeviceNode


class RunzeSyringePumpMode(Enum):
    Normal = 0
    AccuratePos = 1
    AccuratePosVel = 2


pulse_freq_grades = {
    6000: "0" ,
    5600: "1" ,
    5000: "2" ,
    4400: "3" ,
    3800: "4" ,
    3200: "5" ,
    2600: "6" ,
    2200: "7" ,
    2000: "8" ,
    1800: "9" ,
    1600: "10",
    1400: "11",
    1200: "12",
    1000: "13",
    800 : "14",
    600 : "15",
    400 : "16",
    200 : "17",
    190 : "18",
    180 : "19",
    170 : "20",
    160 : "21",
    150 : "22",
    140 : "23",
    130 : "24",
    120 : "25",
    110 : "26",
    100 : "27",
    90  : "28",
    80  : "29",
    70  : "30",
    60  : "31",
    50  : "32",
    40  : "33",
    30  : "34",
    20  : "35",
    18  : "36",
    16  : "37",
    14  : "38",
    12  : "39",
    10  : "40",
}


class RunzeSyringePumpConnectionError(Exception):
    pass


@dataclass(frozen=True, kw_only=True)
class RunzeSyringePumpInfo:
    port: str
    address: str = "1"
    
    volume: float = 25000
    mode: RunzeSyringePumpMode = RunzeSyringePumpMode.Normal

    def create(self):
        return RunzeSyringePumpAsync(self.port, self.address, self.volume, self.mode)


class RunzeSyringePumpAsync:
    _ros_node: DeviceNode
    
    def __init__(self, port: str, address: str = "1", volume: float = 25000, mode: RunzeSyringePumpMode = None):
        self.port = port
        self.address = address
        
        self.volume = volume
        self.mode = mode
        self.total_steps = self.total_steps_vel = 6000
        
        try:
            self._serial = Serial(
                baudrate=9600,
                port=port
            )
        except (OSError, SerialException) as e:
            raise RunzeSyringePumpConnectionError from e

        self._busy = False
        self._closing = False
        self._error_event = Event()
        self._query_future = Future[Any]()
        self._query_lock = Lock()
        self._read_task: Optional[Task[None]] = None
        self._run_future: Optional[Future[Any]] = None
        self._run_lock = Lock()
    
    def post_init(self, ros_node: DeviceNode):
        self._ros_node = ros_node
    
    def _adjust_total_steps(self):
        self.total_steps = 6000 if self.mode == RunzeSyringePumpMode.Normal else 48000
        self.total_steps_vel = 48000 if self.mode == RunzeSyringePumpMode.AccuratePosVel else 6000

    async def _read_loop(self):
        try:
            while True:
                self._receive((await asyncio.to_thread(lambda: self._serial.read_until(b"\n")))[3:-3])
        except SerialException as e:
            raise RunzeSyringePumpConnectionError from e
        finally:
            if not self._closing:
                self._error_event.set()

            if self._query_future and not self._query_future.done():
                self._query_future.set_exception(RunzeSyringePumpConnectionError())
            if self._run_future and not self._run_future.done():
                self._run_future.set_exception(RunzeSyringePumpConnectionError())

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
                raise RunzeSyringePumpConnectionError

            self._query_future = Future[Any]()

            run = 'R' if not command.startswith("?") else ''
            full_command = f"/{self.address}{command}{run}\r\n"
            full_command_data = bytearray(full_command, 'ascii')

            try:
                await asyncio.to_thread(lambda: self._serial.write(full_command_data))
                return self._parse(await asyncio.wait_for(asyncio.shield(self._query_future), timeout=2.0), dtype=dtype)
            except (SerialException, asyncio.TimeoutError) as e:
                self._error_event.set()
                raise RunzeSyringePumpConnectionError from e
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
        self._busy = ((data[0] & (1 << 5)) < 1) or ascii_string.startswith("@")

        if self._run_future and was_busy and not self._busy:
            self._run_future.set_result(data)
        if self._query_future:
            self._query_future.set_result(data)
        else:
            raise Exception("Dropping data")

    async def _run(self, command: str):
        async with self._run_lock:
            self._run_future = Future[Any]()

            try:
                await self._query(command)
                while True:
                    await self._ros_node.sleep(0.5)  # Wait for 0.5 seconds before polling again
                    
                    status = await self.query_device_status()
                    if status == '`':
                        break
                await asyncio.shield(self._run_future)
            finally:
                self._run_future = None

    async def initialize(self):
        response = await self._run("Z")
        if self.mode:
            self.set_step_mode(self.mode)
        else:
            # self.mode = RunzeSyringePumpMode.Normal
            # # self.set_step_mode(self.mode)
            self.mode = await self.query_step_mode()
        return response
    
    # Settings

    async def set_baudrate(self, baudrate):
        if baudrate == 9600:
            return await self._run("U41")
        elif baudrate == 38400:
            return await self._run("U47")
        else:
            raise ValueError("Unsupported baudrate")
    
    # Mode Settings and Queries
    
    async def set_step_mode(self, mode: RunzeSyringePumpMode):
        self.mode = mode
        self._adjust_total_steps()
        command = f"N{mode.value}"
        return await self._run(command)
    
    async def query_step_mode(self):
        response = await self._query("?28")
        status, mode = response[0], int(response[1])
        self.mode = RunzeSyringePumpMode._value2member_map_[mode]
        self._adjust_total_steps()
        return self.mode

    # Speed Settings and Queries
    
    async def set_speed_grade(self, speed: Union[int, str]):
        return await self._run(f"S{speed}")
    
    async def set_speed_max(self, speed: float):
        pulse_freq = int(speed / self.volume * self.total_steps_vel)
        pulse_freq = min(6000, pulse_freq)
        return await self._run(f"V{speed}")
    
    async def query_speed_grade(self):
        pulse_freq, speed = await self.query_speed_max()
        g = "-1"
        for freq, grade in pulse_freq_grades.items():
            if pulse_freq >= freq:
                g = grade
                break
        return g

    async def query_speed_init(self):
        response = await self._query("?1")
        status, pulse_freq = response[0], int(response[1:])
        speed = pulse_freq / self.total_steps_vel * self.volume
        return pulse_freq, speed

    async def query_speed_max(self):
        response = await self._query("?2")
        status, pulse_freq = response[0], int(response[1:])
        speed = pulse_freq / self.total_steps_vel * self.volume
        return pulse_freq, speed

    async def query_speed_end(self):
        response = await self._query("?3")
        status, pulse_freq = response[0], int(response[1:])
        speed = pulse_freq / self.total_steps_vel * self.volume
        return pulse_freq, speed
    
    # Operations
    
    # Valve Setpoint and Queries

    async def set_valve_position(self, position: Union[int, str]):
        command = f"I{position}" if type(position) == int or ord(position) <= 57 else position.upper() 
        return await self._run(command)

    async def query_valve_position(self):
        response = await self._query("?6")
        status, pos_valve = response[0], response[1].upper()
        return pos_valve
    
    # Plunger Setpoint and Queries

    async def move_plunger_to(self, volume: float):
        """
        Move to absolute volume (unit: μL)

        Args:
            volume (float): absolute position of the plunger, unit: μL

        Returns:
            None
        """
        pos_step = int(volume / self.volume * self.total_steps)
        return await self._run(f"A{pos_step}")
    
    async def pull_plunger(self, volume: float):
        """
        Pull a fixed volume (unit: μL)

        Args:
            volume (float): absolute position of the plunger, unit: μL

        Returns:
            None
        """
        pos_step = int(volume / self.volume * self.total_steps)
        return await self._run(f"P{pos_step}")
    
    async def push_plunger(self, volume: float):
        """
        Push a fixed volume (unit: μL)

        Args:
            volume (float): absolute position of the plunger, unit: μL

        Returns:
            None
        """
        pos_step = int(volume / self.volume * self.total_steps)
        return await self._run(f"D{pos_step}")

    async def report_position(self):
        response = await self._query("?0")
        status, pos_step = response[0], int(response[1:])
        return pos_step / self.total_steps * self.volume

    async def query_plunger_position(self):
        response = await self._query("?4")
        status, pos_step = response[0], int(response[1:])
        return pos_step / self.total_steps * self.volume

    async def stop_operation(self):
        return await self._run("T")
    
    # Queries

    async def query_device_status(self):
        return await self._query("Q")

    async def query_command_buffer_status(self):
        return await self._query("?10")

    async def query_backlash_position(self):
        return await self._query("?12")

    async def query_aux_input_status_1(self):
        return await self._query("?13")

    async def query_aux_input_status_2(self):
        return await self._query("?14")

    async def query_software_version(self):
        return await self._query("?23")

    async def wait_error(self):
        await self._error_event.wait()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def open(self):
        if self._read_task:
            raise RunzeSyringePumpConnectionError

        self._read_task = self._ros_node.create_task(self._read_loop())

        try:
            await self.query_device_status()
        except Exception:
            await self.close()
            raise

    async def close(self):
        if self._closing or not self._read_task:
            raise RunzeSyringePumpConnectionError

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
            yield RunzeSyringePumpInfo(port=item.device)
