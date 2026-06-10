import time
from typing import Callable
from unilabos.device_comms.modbus_plc.client import TCPClient, ModbusWorkflow, WorkflowAction, load_csv
from unilabos.device_comms.modbus_plc.modbus import Base as ModbusNodeBase

############ 第一种写法 ##############


# modbus_tcp_client_test1 = TCPClient('192.168.3.2', 502)
#
#
# node_list = [
#     ModbusNode(name="left_move_coli", device_type=DeviceType.COIL, address=7003 * 8),
#     ModbusNode(name="right_move_coli", device_type=DeviceType.COIL, address=7002 * 8),
#     ModbusNode(name="position_register", device_type=DeviceType.HOLD_REGISTER, address=7040),
# ]
#
# def judge_position(node: ModbusNodeBase):
#     idx = 0
#     while idx <= 5:
#         result, is_err = node.read(2)
#         if is_err:
#             print("读取失败")
#         else:
#             print("读取成功:", result)
#         idx+=1
#         time.sleep(1)
#
# workflow_move_2_right = PLCWorkflow(name="测试水平向右移动", actions=[
#     lambda use_node: use_node('left_move_coli').write(False),
#     lambda use_node: use_node('right_move_coli').write(True),
#     lambda use_node: judge_position(use_node('position_register')),
#     lambda use_node: use_node('right_move_coli').write(False),
# ])
#
#
# workflow_move_2_left = PLCWorkflow(name="测试水平向左移动", actions=[
#     lambda use_node: use_node('right_move_coli').write(False),
#     lambda use_node: use_node('left_move_coli').write(True),
#     lambda use_node: judge_position(use_node('position_register')),
#     lambda use_node: use_node('left_move_coli').write(False),
# ])
#
#
# workflow_test_1 = PLCWorkflow(name="测试水平移动并停止", actions=[
#     workflow_move_2_right,
#     workflow_move_2_left,
# ])
#
# modbus_tcp_client_test1 \
#     .register_node_list(node_list) \
#     .run_plc_workflow(workflow_test_1)
#


############ 第二种写法 ##############

modbus_tcp_client_test2 = TCPClient('192.168.3.2', 502)

# def judge_position(node: ModbusNodeBase):
#     idx = 0
#     while idx <= 5:
#         result, is_err = node.read(2)
#         if is_err:
#             print("读取失败")
#         else:
#             print("读取成功:", result)
#         idx+=1
#         time.sleep(1)


# def move_2_right_init(use_node: Callable[[str], ModbusNodeBase]) -> bool:
#     use_node('left_move_coli').write(False)
#     use_node('right_move_coli').write(True)
#     return True

# def move_2_right_start(use_node: Callable[[str], ModbusNodeBase]) -> bool:
#     judge_position(use_node('position_register'))
#     return True

# def move_2_right_stop(use_node: Callable[[str], ModbusNodeBase]) -> bool:
#     use_node('right_move_coli').write(False)
#     return True

# move_2_right_workflow = ModbusWorkflow(name="测试水平向右移动", actions=[WorkflowAction(
#     init=move_2_right_init,
#     start=move_2_right_start,
#     stop=move_2_right_stop,
# )])

# move_2_right_workflow = ModbusWorkflow(name="测试水平向右移动", actions=[WorkflowAction(
#     init=move_2_right_init,
#     start= None,
#     stop= None,
#     cleanup=None,
#     )])


def idel_init(use_node: Callable[[str], ModbusNodeBase]) -> bool:
    # 修改速度
    use_node('M01_idlepos_velocity_rw').write(20.0)
    # 修改位置
    # use_node('M01_idlepos_position_rw').write(35.22)
    return True


def idel_position(use_node: Callable[[str], ModbusNodeBase]) -> bool:
    use_node('M01_idlepos_coil_w').write(True)
    while True:
        pos_idel, idel_err = use_node('M01_idlepos_coil_r').read(1)
        pos_stop, stop_err = use_node('M01_manual_stop_coil_r').read(1)
        time.sleep(0.5)
        if not idel_err and not stop_err and pos_idel[0] and pos_stop[0]:
            break

    return True


def idel_stop(use_node: Callable[[str], ModbusNodeBase]) -> bool:
    use_node('M01_idlepos_coil_w').write(False)
    return True


move_idel = ModbusWorkflow(name="测试待机位置", actions=[WorkflowAction(
    init=idel_init,
    start=idel_position,
    stop=idel_stop,
)])


def pipetter_init(use_node: Callable[[str], ModbusNodeBase]) -> bool:
    # 修改速度
    # use_node('M01_idlepos_velocity_rw').write(10.0)
    # 修改位置
    # use_node('M01_idlepos_position_rw').write(35.22)
    return True


def pipetter_position(use_node: Callable[[str], ModbusNodeBase]) -> bool:
    use_node('M01_pipette0_coil_w').write(True)
    while True:
        pos_idel, isError = use_node('M01_pipette0_coil_r').read(1)
        pos_stop, isError = use_node('M01_manual_stop_coil_r').read(1)
        time.sleep(0.5)
        if pos_idel[0] and pos_stop[0]:
            break

    return True


def pipetter_stop(use_node: Callable[[str], ModbusNodeBase]) -> bool:
    use_node('M01_pipette0_coil_w').write(False)
    return True


move_pipetter = ModbusWorkflow(name="测试待机位置", actions=[WorkflowAction(
    init=None,
    start=pipetter_position,
    stop=pipetter_stop,
)])


workflow_test_2 = ModbusWorkflow(name="测试水平移动并停止", actions=[
    move_idel,
    move_pipetter,
])

nodes = load_csv('/Users/dingshinn/Desktop/lbg/uni-lab/M01.csv')

modbus_tcp_client_test2 \
    .register_node_list(nodes) \
    .run_modbus_workflow(workflow_test_2)
# .run_modbus_workflow(move_2_left_workflow)
