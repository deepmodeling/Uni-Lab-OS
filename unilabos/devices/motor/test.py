# 使用pyserial进行485的协议通信，端口指定为COM4
import serial
from serial.rs485 import RS485Settings
import traceback


from Consts import Config
from FakeSerial import FakeSerial
from base_types.PrPath import PR_PATH
from Utils import run_commands
from commands.PositionControl import create_position_commands, create_position_run_command, run_get_command_position, run_get_motor_position, run_set_forward_run
from commands.Status import run_get_status, run_until_status
from commands.Warning import run_elimate_warning
from Grasp import EleGripper


DEVICE_ADDRESS = Config.DEVICE_ADDRESS

# 配置串口参数
if Config.OPEN_DEVICE:
    ser = serial.Serial(
        port='COM11',               # 串口号
        baudrate=38400,            # 波特率
        bytesize=serial.EIGHTBITS,  # 数据位
        parity=serial.PARITY_NONE,  # 校验位 N-无校验
        stopbits=serial.STOPBITS_TWO,  # 停止位
        timeout=1                  # 超时时间
    )
else:
    ser = FakeSerial()

# RS485模式支持（如果需要）
try:
    ser.rs485_mode = RS485Settings(
        rts_level_for_tx=True,
        rts_level_for_rx=False,
        # delay_before_tx=0.01,
        # delay_before_rx=0.01
    )
except AttributeError:
    traceback.print_exc()
    print("RS485模式需要支持的硬件和pyserial版本")

# run_set_position_zero(ser, DEVICE_ADDRESS)

# api.get_running_state(ser, DEVICE_ADDRESS)
gripper = EleGripper("COM12")
gripper.init_gripper()
gripper.wait_for_gripper_init()
PR = 0
run_get_status(ser, DEVICE_ADDRESS)
run_elimate_warning(ser, DEVICE_ADDRESS)
run_set_forward_run(ser, DEVICE_ADDRESS)
run_commands(
    ser, 0.1,
    create_position_commands(DEVICE_ADDRESS, PR, PR_PATH.ABS_POS, 20 * 1000, 300),  # 41.8cm   21.8cm
    create_position_run_command(DEVICE_ADDRESS, PR),
)
if run_until_status(ser, DEVICE_ADDRESS, "路径完成"):
    pass
gripper.gripper_move(210, 127, 255)
gripper.wait_for_gripper()
gripper.rotate_move_abs(135, 10, 255)
gripper.data_reader()
print(gripper.rot_data)
run_commands(
    ser, 0.1,
    create_position_commands(DEVICE_ADDRESS, PR, PR_PATH.ABS_POS, 30 * 1000, 300),  # 41.8cm   21.8cm
    create_position_run_command(DEVICE_ADDRESS, PR),
)
gripper.gripper_move(210, 127, 255)
gripper.wait_for_gripper()
gripper.rotate_move_abs(135, 10, 255)
gripper.data_reader()

# run_get_command_position(ser, DEVICE_ADDRESS)
# run_get_motor_position(ser, DEVICE_ADDRESS)

# ser.close()
