import os
import sys
from abc import abstractmethod
from typing import Optional

import serial

from .Consts import Config
from .FakeSerial import FakeSerial
from .Utils import run_commands
from .base_types.PrPath import PR_PATH
from .commands.PositionControl import run_get_command_position, run_set_forward_run, create_position_commands, \
    create_position_run_command
from .commands.Warning import run_elimate_warning

try:
    from unilabos.utils.pywinauto_util import connect_application, get_process_pid_by_name, get_ui_path_with_window_specification, print_wrapper_identifiers
    from unilabos.device_comms.universal_driver import UniversalDriver, SingleRunningExecutor
    from unilabos.device_comms import universal_driver as ud
except Exception as e:
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
    from unilabos.utils.pywinauto_util import connect_application, get_process_pid_by_name, get_ui_path_with_window_specification, print_wrapper_identifiers
    from unilabos.device_comms.universal_driver import UniversalDriver, SingleRunningExecutor
    from unilabos.device_comms import universal_driver as ud
    print(f"使用文件DEBUG运行: {e}")


class iCL42Driver(UniversalDriver):
    _ser: Optional[serial.Serial | FakeSerial] = None
    _motor_position: Optional[int] = None
    _DEVICE_COM: Optional[str] = None
    _DEVICE_ADDRESS: Optional[int] = None
    # 运行状态
    _is_executing_run: bool = False
    _success: bool = False

    @property
    def motor_position(self) -> int:
        return self._motor_position

    @property
    def is_executing_run(self) -> bool:
        return self._is_executing_run

    @property
    def success(self) -> bool:
        return self._success

    def init_device(self):
        # 配置串口参数
        if Config.OPEN_DEVICE:
            self._ser = serial.Serial(
                port=self._DEVICE_COM,               # 串口号
                baudrate=38400,            # 波特率
                bytesize=serial.EIGHTBITS,  # 数据位
                parity=serial.PARITY_NONE,  # 校验位 N-无校验
                stopbits=serial.STOPBITS_TWO,  # 停止位
                timeout=1                  # 超时时间
            )
        else:
            self._ser = FakeSerial()

    def run_motor(self, mode: str, position: float, velocity: int):
        if self._ser is None:
            print("Device is not initialized")
            self._success = False
            return False

        def post_func(res, _):
            self._success = res
            if not res:
                self._is_executing_run = False
        ins: SingleRunningExecutor = SingleRunningExecutor.get_instance(
            self.execute_run_motor, post_func
        )
        # if not ins.is_ended and ins.is_started:
        #     print("Function is running")
        #     self._success = False
        #     return False
        # elif not ins.is_started:
        #     print("Function started")
        #     ins.start() # 开始执行
        # else:
        #     print("Function reset and started")
        ins.reset()
        ins.start(mode=mode, position=position, velocity=velocity)

    def execute_run_motor(self, mode: str, position: float, velocity: int):
        position = int(position * 1000)
        PR = 0
        run_elimate_warning(self._ser, self._DEVICE_ADDRESS)
        run_set_forward_run(self._ser, self._DEVICE_ADDRESS)
        run_commands(
            self._ser, 0.1,
            create_position_commands(self._DEVICE_ADDRESS, PR, PR_PATH[mode], position, velocity),  # 41.8cm   21.8cm
            create_position_run_command(self._DEVICE_ADDRESS, PR),
        )
        # if run_until_status(self._ser, self._DEVICE_ADDRESS, "路径完成"):
        #     pass

    def __init__(self, device_com: str = "COM9", device_address: int = 0x01):
        self._DEVICE_COM = device_com
        self._DEVICE_ADDRESS = device_address
        self.init_device()
        # 启动所有监控器
        self.checkers = [
            # PositionChecker(self, 1),
        ]
        for checker in self.checkers:
            checker.start_monitoring()


@abstractmethod
class DriverChecker(ud.DriverChecker):
    driver: iCL42Driver


class PositionChecker(DriverChecker):
    def check(self):
        # noinspection PyProtectedMember
        if self.driver._ser is not None:
            # noinspection PyProtectedMember
            self.driver._motor_position = run_get_command_position(self.driver._ser, self.driver._DEVICE_ADDRESS)


# 示例用法
if __name__ == "__main__":
    driver = iCL42Driver("COM3")
    driver._is_executing_run = True
