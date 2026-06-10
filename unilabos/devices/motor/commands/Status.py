import time
from ..Utils import create_command, send_command
from .PositionControl import run_get_motor_position


def run_get_status(ser, DEVICE_ADDRESS, print_status=True) -> list[list[bytes]]:
    # Bit0 故障
    # Bit1 使能
    # Bit2 运行
    # Bit4 指令完成
    # Bit5 路径完成
    # Bit6 回零完成
    ret = send_command(ser, create_command(DEVICE_ADDRESS, 0x03, 0x1003, 0x0001))
    status = bin(int(ret.hex()[6:10], 16))[2:]
    # 用0左位补齐
    status = status.zfill(8)
    status_info_list = []
    if status[-1] == "1":
        status_info_list.append("故障")
    if status[-2] == "1":
        status_info_list.append("使能")
    if status[-3] == "1":
        status_info_list.append("运行")
    if status[-5] == "1":
        status_info_list.append("指令完成")
    if status[-6] == "1":
        status_info_list.append("路径完成")
    if status[-7] == "1":
        status_info_list.append("回零完成")
    if print_status:
        print(f"获取状态: {' '.join(status_info_list)}")
    return status_info_list


def run_until_status(ser, DEVICE_ADDRESS, status_info: str, max_time=10):
    start_time = time.time()
    while True:
        ret = run_get_status(ser, DEVICE_ADDRESS)
        if status_info in ret:
            break
        if time.time() - start_time > max_time:
            print(f"状态未达到 {status_info} 超时")
            return False
        time.sleep(0.05)
    return True
