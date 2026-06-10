import time
import serial


class SolenoidValve:
    def __init__(self, io_device_port: str):
        self._status = "Idle"
        self._valve_position = "OPEN"
        self.io_device_port = io_device_port

        try:
            self.hardware_interface = serial.Serial(str(io_device_port), 9600, timeout=1)
        except serial.SerialException:
            # raise Exception(f"Failed to connect to the device at {io_device_port}")
            self.hardware_interface = str(io_device_port)

    @property
    def status(self) -> str:
        return self._status

    @property
    def valve_position(self) -> str:
        return self._valve_position

    def send_command(self, command):
        self.hardware_interface.write(command)

    def read_data(self):
        return self.hardware_interface.read()

    def get_valve_position(self) -> str:
        self._valve_position = "OPEN" if self.read_data() else "CLOSED"
        return self._valve_position

    def set_valve_position(self, position):
        self._status = "Busy"
        self.send_command(1 if position == "OPEN" else 0)
        time.sleep(5)
        self._status = "Idle"

    def open(self):
        self._valve_position = "OPEN"

    def close(self):
        self._valve_position = "CLOSED"

    def is_open(self):
        return self._valve_position

    def is_closed(self):
        return not self._valve_position
