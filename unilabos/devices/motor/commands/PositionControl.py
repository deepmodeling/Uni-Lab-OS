
from ..base_types.PrPath import PR_PATH
from ..Utils import create_command, get_result_byte_int, get_result_byte_str, send_command


def create_position_commands(slave_id: int, which: int, how: PR_PATH,
                             position: float, velocity: int = 300,
                             acc_time: int = 50, dec_time: int = 50,
                             special: int = 0) -> list[list[bytes]]:
    """
    创建位置相关的Modbus命令列表

    Args:
        slave_id: 从站地址
        which: PR路径号(0-7)
        how: PR_PATH枚举，定义运动方式
        position: 目标位置(Pulse)
        velocity: 运行速度(rpm)
        acc_time: 加速时间(ms/Krpm)，默认50
        dec_time: 减速时间(ms/Krpm)，默认50
        special: 特殊参数，默认0

    Returns:
        包含所有设置命令的列表
    """
    if not 0 <= which <= 7:
        raise ValueError("which必须在0到7之间")

    base_addr = 0x6200 + which * 8

    # 位置值保持原样（Pulse）
    position = int(position)
    print(f"路径方式 {' '.join(bin(how.value)[2:])} 位置 {position} 速度 {velocity}")
    position_high = (position >> 16) & 0xFFFF  # 获取高16位
    position_low = position & 0xFFFF           # 获取低16位

    # 速度值（rpm）转换为0x0000格式
    velocity_value = 0x0000 + velocity

    # 加减速时间（ms/Krpm）转换为0x0000格式
    acc_time_value = 0x0000 + int(acc_time)
    dec_time_value = 0x0000 + int(dec_time)

    # 特殊参数转换为0x0000格式
    special_value = 0x0000 + special
    return [
        create_command(slave_id, 0x06, base_addr + 0, how.value),      # 路径方式
        create_command(slave_id, 0x06, base_addr + 1, position_high),  # 位置高16位
        create_command(slave_id, 0x06, base_addr + 2, position_low),   # 位置低16位
        create_command(slave_id, 0x06, base_addr + 3, velocity_value),  # 运行速度
        create_command(slave_id, 0x06, base_addr + 4, acc_time_value),  # 加速时间
        create_command(slave_id, 0x06, base_addr + 5, dec_time_value),  # 减速时间
        create_command(slave_id, 0x06, base_addr + 6, special_value),  # 特殊参数
    ]


def create_position_run_command(slave_id: int, which: int) -> list[list[bytes]]:
    print(f"运行路径 PR{which}")
    return [create_command(slave_id, 0x06, 0x6002, 0x0010 + which)]


def run_set_position_zero(ser, DEVICE_ADDRESS) -> list[list[bytes]]:
    print(f"手动回零")
    send_command(ser, create_command(DEVICE_ADDRESS, 0x06, 0x6002, 0x0021))


def run_stop(ser, DEVICE_ADDRESS) -> list[list[bytes]]:
    print(f"急停")
    send_command(ser, create_command(DEVICE_ADDRESS, 0x06, 0x6002, 0x0040))


def run_set_forward_run(ser, DEVICE_ADDRESS) -> list[list[bytes]]:
    print(f"设定正方向运动")
    send_command(ser, create_command(DEVICE_ADDRESS, 0x06, 0x0007, 0x0000))


def run_set_backward_run(ser, DEVICE_ADDRESS) -> list[list[bytes]]:
    print(f"设定反方向运动")
    send_command(ser, create_command(DEVICE_ADDRESS, 0x06, 0x0007, 0x0001))


def run_get_command_position(ser, DEVICE_ADDRESS, print_pos=True) -> int:
    retH = send_command(ser, create_command(DEVICE_ADDRESS, 0x03, 0x602A, 0x0001))  # 命令位置H
    retL = send_command(ser, create_command(DEVICE_ADDRESS, 0x03, 0x602B, 0x0001))  # 命令位置L
    value = get_result_byte_str(retH) + get_result_byte_str(retL)
    value = int(value, 16)
    if print_pos:
        print(f"命令位置: {value}")
    return value


def run_get_motor_position(ser, DEVICE_ADDRESS, print_pos=True) -> int:
    retH = send_command(ser, create_command(DEVICE_ADDRESS, 0x03, 0x602C, 0x0001))  # 电机位置H
    retL = send_command(ser, create_command(DEVICE_ADDRESS, 0x03, 0x602D, 0x0001))  # 电机位置L
    value = get_result_byte_str(retH) + get_result_byte_str(retL)
    value = int(value, 16)
    if print_pos:
        print(f"电机位置: {value}")
    return value
