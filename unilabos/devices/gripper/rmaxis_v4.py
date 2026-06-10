import time
from asyncio import Event
from enum import Enum, auto
from dataclasses import dataclass
from typing import Dict, Tuple
from pymodbus.client import ModbusSerialClient as ModbusClient
from pymodbus.client import ModbusTcpClient as ModbusTcpClient


class CommandType(Enum):
    COMMAND_NONE = 0
    COMMAND_GO_HOME = 1
    COMMAND_DELAY = 2
    COMMAND_MOVE_ABSOLUTE = 3
    COMMAND_PUSH = 4
    COMMAND_MOVE_RELATIVE = 5
    COMMAND_PRECISE_PUSH = 6


class ParamType(Enum):
    BOOLEAN = 0
    INT32 = 1
    FLOAT = 2
    ENUM = 3


class ParamEdit(Enum):
    NORMAL = 0
    READONLY = 1


@dataclass
class Param:
    type: ParamType
    editability: ParamEdit
    address: int


# 用于存储参数的字典类型
ParamsDict = Dict[str, Param]


# Constants and other required elements can be defined as needed
IO_GAP_TIME = 0.01
EXECUTE_COMMAND_INDEX = 15  # Example index
COMMAND_REACH_SIGNAL = "reach_15"
# Define other constants or configurations as needed


def REVERSE(x):
    return ((x << 16) & 0xFFFF0000) | ((x >> 16) & 0x0000FFFF)


def int32_to_uint16_list(int32_list):
    uint16_list = []
    for num in int32_list:
        lower_half = num & 0xFFFF
        upper_half = (num >> 16) & 0xFFFF
        uint16_list.extend([upper_half, lower_half])
    return uint16_list


def uint16_list_to_int32_list(uint16_list):
    if len(uint16_list) % 2 != 0:
        raise ValueError("Input list must have even number of uint16 elements.")
    int32_list = []
    for i in range(0, len(uint16_list), 2):
        # Combine two uint16 values into one int32 value
        high = uint16_list[i + 1]
        low = uint16_list[i]
        # Assuming the uint16_list is in big-endian order
        int32_value = (high << 16) | low
        int32_list.append(int(int32_value))
    return int32_list


class RMAxis:
    modbus_device = {}

    def __init__(self, port, is_modbus_rtu, baudrate: int = 115200, address: str = "", slave_id: int = 1):
        self.device = port
        self.is_modbus_rtu = is_modbus_rtu
        if is_modbus_rtu:
            self.client = ModbusClient(port=port, baudrate=baudrate, parity='N', stopbits=1, bytesize=8, timeout=3)
        else:
            self.client = ModbusTcpClient(address, port)
        if not self.client.connect():
            raise Exception(f"Modbus Connection failed")
        self.slave_id = slave_id

        self._error_event = Event()
        self.device_params = {}  # Assuming some initialization for parameters
        self.command_edited = {}
        self.init_parameters(self.device_params)

    def init_parameters(self, params):
        params["current_command_position"] = Param(ParamType.FLOAT, ParamEdit.NORMAL, 4902)
        params["current_command_velocity"] = Param(ParamType.FLOAT, ParamEdit.NORMAL, 4904)
        params["current_command_acceleration"] = Param(ParamType.FLOAT, ParamEdit.NORMAL, 4906)
        params["current_command_deacceleration"] = Param(ParamType.FLOAT, ParamEdit.NORMAL, 4908)
        params["current_position"] = Param(ParamType.FLOAT, ParamEdit.READONLY, 0)
        params["current_velocity"] = Param(ParamType.FLOAT, ParamEdit.READONLY, 2)
        params["control_torque"] = Param(ParamType.FLOAT, ParamEdit.NORMAL, 4)
        params["error"] = Param(ParamType.INT32, ParamEdit.READONLY, 6)
        params["current_force_sensor"] = Param(ParamType.FLOAT, ParamEdit.READONLY, 18)
        params["io_in_go_home"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1401)
        params["io_in_error_reset"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1402)
        params["io_in_start"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1403)
        params["io_in_servo"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1404)
        params["io_in_stop"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1405)
        params["io_in_force_reset"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1424)
        params["io_in_save_parameters"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1440)
        params["io_in_load_parameters"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1441)
        params["io_in_save_positions"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1442)
        params["io_in_load_positions"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1443)
        params["io_out_gone_home"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1501)
        params["command_address"] = Param(ParamType.INT32, ParamEdit.NORMAL, 5000)
        params["selected_command_index"] = Param(ParamType.INT32, ParamEdit.NORMAL, 4001)
        params["io_out_reach_15"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1521)
        params["io_out_moving"] = Param(ParamType.BOOLEAN, ParamEdit.NORMAL, 1505)
        params["limit_pos"] = Param(ParamType.FLOAT, ParamEdit.NORMAL, 74)
        params["limit_neg"] = Param(ParamType.FLOAT, ParamEdit.NORMAL, 72)
        params["hardware_direct_output"] = Param(ParamType.INT32, ParamEdit.NORMAL, 158)
        params["hardware_direct_input"] = Param(ParamType.INT32, ParamEdit.NORMAL, 160)

    def get_version(self):
        version_major = self.client.read_input_registers(8, 1, unit=self.slave_id).registers[0]
        version_minor = self.client.read_input_registers(10, 1, unit=self.slave_id).registers[0]
        version_build = self.client.read_input_registers(12, 1, unit=self.slave_id).registers[0]
        version_type = self.client.read_input_registers(14, 1, unit=self.slave_id).registers[0]
        return (version_major, version_minor, version_build, version_type)

    def set_input_signal(self, signal, level):
        param_name = f"io_in_{signal}"
        self.set_parameter(param_name, level)

    def get_output_signal(self, signal):
        param_name = f"io_out_{signal}"
        return self.get_device_parameter(param_name)

    def config_motion(self, velocity, acceleration, deacceleration):
        self.set_parameter("current_command_velocity", velocity)
        self.set_parameter("current_command_acceleration", acceleration)
        self.set_parameter("current_command_deacceleration", deacceleration)

    def move_to(self, position):
        self.set_parameter("current_command_position", position)

    def go_home(self):
        self.set_input_signal("go_home", False)
        time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds
        self.set_input_signal("go_home", True)

    def move_absolute(self, position, velocity, acceleration, deacceleration, band):
        command = {
            'type': CommandType.COMMAND_MOVE_ABSOLUTE.value,
            'position': position,
            'velocity': velocity,
            'acceleration': acceleration,
            'deacceleration': deacceleration,
            'band': band,
            'push_force': 0,
            'push_distance': 0,
            'delay': 0,
            'next_command_index': -1
        }
        self.execute_command(command)

    def move_relative(self, position, velocity, acceleration, deacceleration, band):
        command = {
            'type': CommandType.COMMAND_MOVE_RELATIVE.value,
            'position': position,
            'velocity': velocity,
            'acceleration': acceleration,
            'deacceleration': deacceleration,
            'band': band,
            'push_force': 0,
            'push_distance': 0,
            'delay': 0,
            'next_command_index': -1
        }
        self.execute_command(command)

    def push(self, force, distance, velocity):
        command = {
            'type': CommandType.COMMAND_PUSH.value,
            'position': 0,
            'velocity': velocity,
            'acceleration': 0,
            'deacceleration': 0,
            'band': 0,
            'push_force': force,
            'push_distance': distance,
            'delay': 0,
            'next_command_index': -1
        }
        self.execute_command(command)

    def precise_push(self, force, distance, velocity, force_band, force_check_time):
        command = {
            'type': CommandType.COMMAND_PRECISE_PUSH.value,
            'position': 0,
            'velocity': velocity,
            'acceleration': 0,
            'deacceleration': 0,
            'band': force_band,
            'push_force': force,
            'push_distance': distance,
            'delay': force_check_time,
            'next_command_index': -1
        }
        self.execute_command(command)

    def is_moving(self):
        return self.get_output_signal("moving")

    def is_reached(self):
        return self.get_output_signal(COMMAND_REACH_SIGNAL)

    def is_push_empty(self):
        return not self.is_moving()

    def set_command(self, index, command):
        print("Setting command", command)
        self.command_edited[index] = True
        command_buffer = [
            command['type'],
            int(command['position'] * 1000),
            int(command['velocity'] * 1000),
            int(command['acceleration'] * 1000),
            int(command['deacceleration'] * 1000),
            int(command['band'] * 1000),
            int(command['push_force'] * 1000),
            int(command['push_distance'] * 1000),
            int(command['delay']),
            int(command['next_command_index'])
        ]
        buffer = int32_to_uint16_list(command_buffer)
        response = self.client.write_registers(
            self.device_params["command_address"].address + index * 20, buffer, self.slave_id)

    def get_command(self, index):
        response = self.client.read_holding_registers(
            self.device_params["command_address"].address + index * 20, 20, self.slave_id)
        print(response)
        buffer = response.registers
        command_buffer = uint16_list_to_int32_list(buffer)
        command = {
            'type': command_buffer[0],
            'position': command_buffer[1] / 1000.0,
            'velocity': command_buffer[2] / 1000.0,
            'acceleration': command_buffer[3] / 1000.0,
            'deacceleration': command_buffer[4] / 1000.0,
            'band': command_buffer[5] / 1000.0,
            'push_force': command_buffer[6] / 1000.0,
            'push_distance': command_buffer[7] / 1000.0,
            'delay': command_buffer[8],
            'next_command_index': command_buffer[9]
        }
        return command

    def execute_command(self, command):
        self.set_command(EXECUTE_COMMAND_INDEX, command)
        self.save_commands()
        self.trig_command(EXECUTE_COMMAND_INDEX)
        time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds

    def trig_command(self, index):
        print("Triggering command", index)
        self.set_parameter("selected_command_index", index)
        self.set_input_signal("start", False)
        time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds
        self.set_input_signal("start", True)

    def load_commands(self):
        self.set_input_signal("load_positions", False)
        time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds
        self.set_input_signal("load_positions", True)

    def save_commands(self):
        for index, edited in self.command_edited.items():
            if edited:
                self.set_parameter("selected_command_index", index)
                self.set_input_signal("save_positions", False)
                time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds
                self.set_input_signal("save_positions", True)
                time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds
                self.command_edited[index] = False

    @property
    def position(self) -> float:
        return self.get_device_parameter("current_position")

    def get_position(self) -> float:
        return self.get_device_parameter("current_position")

    @property
    def velocity(self) -> float:
        return self.get_device_parameter("current_velocity")

    @property
    def torque(self) -> float:
        return self.get_device_parameter("control_torque")

    @property
    def force_sensor(self) -> float:
        return self.get_device_parameter("current_force_sensor")

    def error_code(self):
        return self.get_device_parameter("error")

    def get_device_parameter(self, name):
        # Assuming self.device_params is a dictionary with address and type
        param = self.device_params.get(name)
        if not param:
            self._error_event.set()
            raise Exception(f"parameter {name} does not exist")

        address = param.address
        if param.editability == ParamEdit.READONLY:
            if param.type == ParamType.BOOLEAN:
                return self.client.read_input_discretes(address, 1).bits[0]
            elif param.type == ParamType.ENUM:
                return self.client.read_input_registers(address, 1).registers[0]
            elif param.type == ParamType.INT32:
                return self.client.read_input_registers(address, 2).registers[0]  # Handle as needed
            elif param.type == ParamType.FLOAT:
                return self.client.read_input_registers(address, 2).registers[0]  # Handle as needed
            else:
                self._error_event.set()
                raise Exception(f"parameter {name} has unknown data type {param.type}")
        else:
            if param.type == ParamType.BOOLEAN:
                return self.client.read_holding_registers(address, 1).registers[0]
            elif param.type == ParamType.ENUM:
                return self.client.read_holding_registers(address, 1).registers[0]
            elif param.type == ParamType.INT32:
                return self.client.read_holding_registers(address, 2).registers[0]  # Handle as needed
            elif param.type == ParamType.FLOAT:
                return self.client.read_holding_registers(address, 2).registers[0]  # Handle as needed
            else:
                self._error_event.set()
                raise Exception(f"parameter {name} has unknown data type {param['type']}")

    def set_parameter(self, name, value):
        param = self.device_params.get(name)
        if not param:
            self._error_event.set()
            raise Exception(f"parameter {name} does not exist")

        address = param.address
        if param.editability == ParamEdit.READONLY:
            raise Exception(f"parameter {name} is read only")
        else:
            if param.type == ParamType.BOOLEAN:
                self.client.write_coil(address, bool(value))
            elif param.type == ParamType.ENUM:
                self.client.write_register(address, value)
            elif param.type == ParamType.INT32:
                self.client.write_register(address, int(value))
            elif param.type == ParamType.FLOAT:
                self.client.write_register(address, float(value))
            else:
                self._error_event.set()
                raise Exception(f"parameter {name} has unknown data type {param['type']}")

    def load_parameters(self):
        self.set_input_signal("load_parameters", False)
        time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds
        self.set_input_signal("load_parameters", True)

    def save_parameters(self):
        self.set_input_signal("save_parameters", False)
        time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds
        self.set_input_signal("save_parameters", True)

    def reset_error(self):
        self.set_input_signal("error_reset", False)
        time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds
        self.set_input_signal("error_reset", True)

    def set_servo_on_off(self, on_off):
        self.set_input_signal("servo", on_off)

    def stop(self):
        self.set_input_signal("stop", False)
        time.sleep(IO_GAP_TIME)  # Assuming IO_GAP_TIME is 0.1 seconds
        self.set_input_signal("stop", True)

    def soft_reset(self):
        self.client.write_register(186, 0x22205682)

    async def wait_error(self):
        await self._error_event.wait()

    def close(self):
        self.client.close()
        del self.client


if __name__ == "__main__":
    # gripper = RMAxis.create_rmaxis_modbus_rtu("COM7", 115200, 1)
    gripper = RMAxis.create_rmaxis_modbus_rtu('/dev/tty.usbserial-B002YGXY', 115200, 0)
    gripper.go_home()
    # gripper.move_to(20)
    # print("Moving abs...")
    # gripper.move_absolute(20, 5, 100, 100, 0.1)
    # print(gripper.get_command(EXECUTE_COMMAND_INDEX))
    # gripper.go_home()
    # print("Pushing...")
    # gripper.push(0.7, 10, 20)
