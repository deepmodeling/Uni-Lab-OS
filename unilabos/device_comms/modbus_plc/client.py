import json
import time
import traceback
from typing import Any, Union, List, Dict, Callable, Optional, Tuple
from pydantic import BaseModel

from pymodbus.client import ModbusSerialClient, ModbusTcpClient
from pymodbus.framer import FramerType
from typing import TypedDict

from unilabos.device_comms.modbus_plc.modbus import DeviceType, HoldRegister, Coil, InputRegister, DiscreteInputs, DataType, WorderOrder
from unilabos.device_comms.modbus_plc.modbus import Base as ModbusNodeBase
from unilabos.device_comms.universal_driver import UniversalDriver
from unilabos.utils.log import logger
import pandas as pd


class ModbusNode(BaseModel):
    name: str
    device_type: DeviceType
    address: int
    data_type: DataType = DataType.INT16
    slave: int = 1


class PLCWorkflow(BaseModel):
    name: str
    actions: List[
        Union[
            "PLCWorkflow",
            Callable[
                [Callable[[str], ModbusNodeBase]],
                None
            ]]
    ]

class Action(BaseModel):
    name: str
    rw: bool  # read是0 write是1

class WorkflowAction(BaseModel):
    init: Optional[Callable[[Callable[[str], ModbusNodeBase]], bool]] = None
    start: Optional[Callable[[Callable[[str], ModbusNodeBase]], bool]] = None
    stop: Optional[Callable[[Callable[[str], ModbusNodeBase]], bool]] = None
    cleanup: Optional[Callable[[Callable[[str], ModbusNodeBase]], None]] = None


class ModbusWorkflow(BaseModel):
    name: str
    actions: List[Union["ModbusWorkflow", WorkflowAction]]


""" 前后端Json解析用 """
class AddressFunctionJson(TypedDict):
    func_name: str
    node_name: str
    mode: str
    value: Any

class InitFunctionJson(AddressFunctionJson):
    pass

class StartFunctionJson(AddressFunctionJson):
    pass

class StopFunctionJson(AddressFunctionJson):
    pass

class CleanupFunctionJson(AddressFunctionJson):
    pass

class ActionJson(TypedDict):
    address_function_to_create: list[AddressFunctionJson]
    create_init_function: Optional[InitFunctionJson]
    create_start_function: Optional[StartFunctionJson]
    create_stop_function: Optional[StopFunctionJson]
    create_cleanup_function: Optional[CleanupFunctionJson]

class WorkflowCreateJson(TypedDict):
    name: str
    action: list[Union[ActionJson, 'WorkflowCreateJson'] | str]

class ExecuteProcedureJson(TypedDict):
    register_node_list_from_csv_path: Optional[dict[str, Any]]
    create_flow: list[WorkflowCreateJson]
    execute_flow: list[str]



class BaseClient(UniversalDriver):
    client: Optional[Union[ModbusSerialClient, ModbusTcpClient]] = None
    _node_registry: Dict[str, ModbusNodeBase] = {}
    DEFAULT_ADDRESS_PATH = ""

    def __init__(self):
        super().__init__()
        # self.register_node_list_from_csv_path()

    def _set_client(self, client: Optional[Union[ModbusSerialClient, ModbusTcpClient]]) -> None:
        if client is None:
            raise ValueError('client is not valid')
        # if not isinstance(client, TCPClient ) or not isinstance(client, RTUClient):
        #     raise ValueError('client is not valid')
        self.client = client

    def _connect(self) -> None:
        logger.info('try to connect client...')
        if self.client:
            if self.client.connect():
                logger.info('client connected!')
            else:
                logger.error('client connect failed')
        else:
            raise ValueError('client is not initialized')

    @classmethod
    def load_csv(cls, file_path: str):
        df = pd.read_csv(file_path)
        df = df.drop_duplicates(subset='Name', keep='first') # FIXME: 重复的数据应该报错
        data_dict = df.set_index('Name').to_dict(orient='index')
        nodes = []
        for k, v in data_dict.items():
            deviceType = v.get('DeviceType', None)
            addr = v.get('Address', 0)
            dataType = v.get('DataType', 'BOOL')
            if not deviceType or not addr:
                continue

            if deviceType == DeviceType.COIL.value:
                byteAddr = int(addr / 10)
                bitAddr = addr % 10
                addr = byteAddr * 8 + bitAddr

            if dataType == 'BOOL':
                # noinspection PyTypeChecker
                dataType = 'INT16'
            # noinspection PyTypeChecker
            if pd.isna(dataType):
                print(v, "没有注册成功！")
                continue
            dataType: DataType = DataType[dataType]
            nodes.append(ModbusNode(name=k, device_type=DeviceType(deviceType), address=addr, data_type=dataType))
        return nodes

    def use_node(self, name: str) -> ModbusNodeBase:
        if name not in self._node_registry:
            raise ValueError(f'node {name} is not registered')

        return self._node_registry[name]

    def get_node_registry(self) -> Dict[str, ModbusNodeBase]:
        return self._node_registry

    def register_node_list_from_csv_path(self, path: str = None) -> "BaseClient":
        if path is None:
            path = self.DEFAULT_ADDRESS_PATH
        nodes = self.load_csv(path)
        return self.register_node_list(nodes)

    def register_node_list(self, node_list: List[ModbusNode]) -> "BaseClient":
        if not self.client:
            raise ValueError('client is not connected')

        if not node_list or len(node_list) == 0:
            logger.warning('node list is empty')
            return self

        logger.info(f'start to register {len(node_list)} nodes...')
        for node in node_list:
            if node is None:
                continue
            if node.name in self._node_registry:
                logger.info(f'node {node.name} already exists')
                exist = self._node_registry[node.name]
                if exist.type != node.device_type:
                    raise ValueError(f'node {node.name} type {node.device_type} is diplicated with {exist.type}')
                if exist.address != node.address:
                    raise ValueError(f'node {node.name} address is duplicated with {exist.address}')
                continue
            if not isinstance(node.device_type, DeviceType):
                raise ValueError(f'node {node.name} type is not valid')

            if node.device_type == DeviceType.HOLD_REGISTER:
                self._node_registry[node.name] = HoldRegister(self.client, node.name, node.address, node.data_type)
            elif node.device_type == DeviceType.COIL:
                self._node_registry[node.name] = Coil(self.client, node.name, node.address, node.data_type)
            elif node.device_type == DeviceType.INPUT_REGISTER:
                self._node_registry[node.name] = InputRegister(self.client, node.name, node.address, node.data_type)
            elif node.device_type == DeviceType.DISCRETE_INPUTS:
                self._node_registry[node.name] = DiscreteInputs(self.client, node.name, node.address, node.data_type)
            else:
                raise ValueError(f'node {node.name} type {node.device_type} is not valid')

        logger.info('register nodes done.')
        return self

    def run_plc_workflow(self, workflow: PLCWorkflow) -> None:
        if not self.client:
            raise ValueError('client is not connected')

        logger.info(f'start to run workflow {workflow.name}...')

        for action in workflow.actions:
            if isinstance(action, PLCWorkflow):
                self.run_plc_workflow(action)
            elif isinstance(action, Callable):
                action(self.use_node)
            else:
                raise ValueError(f'invalid action {action}')

    def call_lifecycle_fn(
            self,
            workflow: ModbusWorkflow,
            fn: Optional[Callable[[Callable], bool]],
    ) -> bool:
        if not fn:
            raise ValueError('fn is not valid in call_lifecycle_fn')
        try:
            return fn(self.use_node)
        except Exception as e:
            traceback.print_exc()
            logger.error(f'execute {workflow.name} lifecycle failed, err: {e}')
            return False

    def run_modbus_workflow(self, workflow: ModbusWorkflow) -> bool:
        if not self.client:
            raise ValueError('client is not connected')

        logger.info(f'start to run workflow {workflow.name}...')

        for action in workflow.actions:
            if isinstance(action, ModbusWorkflow):
                if self.run_modbus_workflow(action):
                    logger.info(f"{action.name} workflow done.")
                    continue
                else:
                    logger.error(f"{action.name} workflow failed")
                    return False
            elif isinstance(action, WorkflowAction):
                init = action.init
                start = action.start
                stop = action.stop
                cleanup = action.cleanup
                if not init and not start and not stop:
                    raise ValueError(f'invalid action {action}')

                is_err = False
                try:
                    if init and not self.call_lifecycle_fn(workflow, init):
                        raise ValueError(f"{workflow.name} init action failed")
                    if not self.call_lifecycle_fn(workflow, start):
                        raise ValueError(f"{workflow.name} start action failed")
                    if not self.call_lifecycle_fn(workflow, stop):
                        raise ValueError(f"{workflow.name} stop action failed")
                    logger.info(f"{workflow.name} action done.")
                except Exception as e:
                    is_err = True
                    traceback.print_exc()
                    logger.error(f"{workflow.name} action failed, err: {e}")
                finally:
                    logger.info(f"{workflow.name} try to run cleanup")
                    if cleanup:
                        self.call_lifecycle_fn(workflow, cleanup)
                    else:
                        logger.info(f"{workflow.name} cleanup is not defined")
                    if is_err:
                        return False
                    return True
            else:
                raise ValueError(f'invalid action type {type(action)}')

        return True

    function_name: dict[str, Callable[[Callable[[str], ModbusNodeBase]], bool]] = {}

    @classmethod
    def pack_func(cls, func, value="UNDEFINED"):
        def execute_pack_func(use_node: Callable[[str], ModbusNodeBase]):
            if value == "UNDEFINED":
                func()
            else:
                func(use_node, value)
        return execute_pack_func

    def create_address_function(self, func_name: str = None, node_name: str = None, mode: str = None, value: Any = None, data_type: Optional[DataType] = None, word_order: WorderOrder = None, slave: Optional[int] = None) -> Callable[[Callable[[str], ModbusNodeBase]], bool]:
        def execute_address_function(use_node: Callable[[str], ModbusNodeBase]) -> Union[bool, Tuple[Union[int, float, str, list[bool], list[int], list[float]], bool]]:
            param = {"value": value}
            if data_type is not None:
                param["data_type"] = data_type
            if word_order is not None:
                param["word_order"] = word_order
            if slave is not None:
                param["slave"] = slave
            target_node = use_node(node_name)
            print("执行", node_name, type(target_node).__name__, target_node.address, mode, value)
            if mode == 'read':
                return use_node(node_name).read(**param)
            elif mode == 'write':
                return not use_node(node_name).write(**param)
            return False
        if func_name is None:
            func_name = node_name + '_' + mode + '_' + str(value)
        print("创建 address function", mode, func_name)
        self.function_name[func_name] = execute_address_function
        return execute_address_function
    
    def create_init_function(self, func_name: str = None, node_name: str = None, mode: str = None, value: Any = None, data_type: Optional[DataType] = None, word_order: WorderOrder = None, slave: Optional[int] = None):
        return self.create_address_function(func_name, node_name, mode, value, data_type, word_order, slave)

    def create_stop_function(self, func_name: str = None, node_name: str = None, mode: str = None, value: Any = None, data_type: Optional[DataType] = None, word_order: WorderOrder = None, slave: Optional[int] = None):
        return self.create_address_function(func_name, node_name, mode, value, data_type, word_order, slave)

    def create_cleanup_function(self, func_name: str = None, node_name: str = None, mode: str = None, value: Any = None, data_type: Optional[DataType] = None, word_order: WorderOrder = None, slave: Optional[int] = None):
        return self.create_address_function(func_name, node_name, mode, value, data_type, word_order, slave)

    def create_start_function(self, func_name: str, write_functions: list[str], condition_functions: list[str], stop_condition_expression: str):
        def execute_start_function(use_node: Callable[[str], ModbusNodeBase]) -> bool:
            for write_function in write_functions:
                self.function_name[write_function](use_node)
            while True:
                next_loop = False
                condition_source = {}
                for condition_function in condition_functions:
                    read_res, read_err = self.function_name[condition_function](use_node)
                    if read_err:
                        next_loop = True
                        break
                    condition_source[condition_function] = read_res
                if not next_loop:
                    if stop_condition_expression:
                        condition_source["__RESULT"] = None
                        exec(f"__RESULT = {stop_condition_expression}", {}, condition_source)  # todo: safety check
                        res = condition_source["__RESULT"]
                        print("取得计算结果；", res)
                        if res:
                            break
                else:
                    time.sleep(0.3)
            return True
        return execute_start_function

    def create_action_from_json(self, data: ActionJson):
        for i in data["address_function_to_create"]:
            self.create_address_function(**i)
        init = None
        start = None
        stop = None
        cleanup = None
        if data["create_init_function"]:
            print("创建 init function")
            init = self.create_init_function(**data["create_init_function"])
        if data["create_start_function"]:
            print("创建 start function")
            start = self.create_start_function(**data["create_start_function"])
        if data["create_stop_function"]:
            print("创建 stop function")
            stop = self.create_stop_function(**data["create_stop_function"])
        if data["create_cleanup_function"]:
            print("创建 cleanup function")
            cleanup = self.create_cleanup_function(**data["create_cleanup_function"])
        return WorkflowAction(init=init, start=start, stop=stop, cleanup=cleanup)
    
    workflow_name = {}

    def create_workflow_from_json(self, data: list[WorkflowCreateJson]):
        for ind, flow in enumerate(data):
            print("正在创建 workflow", ind, flow["name"])
            actions = []
            for i in flow["action"]:
                if isinstance(i, str):
                    print("沿用 已有workflow 作为action", i)
                    action = self.workflow_name[i]
                else:
                    print("创建 action")
                    action = self.create_action_from_json(i)
                actions.append(action)
            flow_instance = ModbusWorkflow(name=flow["name"], actions=actions)
            print("创建完成 workflow", flow["name"])
            self.workflow_name[flow["name"]] = flow_instance

    def execute_workflow_from_json(self, data: list[str]):
        for i in data:
            print("正在执行 workflow", i)
            self.run_modbus_workflow(self.workflow_name[i])

    def execute_procedure_from_json(self, data: ExecuteProcedureJson):
        if data["register_node_list_from_csv_path"]:
            print("注册节点 csv", data["register_node_list_from_csv_path"])
            self.register_node_list_from_csv_path(**data["register_node_list_from_csv_path"])
        print("创建工作流")
        self.create_workflow_from_json(data["create_flow"])
        print("执行工作流")
        self.execute_workflow_from_json(data["execute_flow"])


class TCPClient(BaseClient):
    def __init__(self, addr: str, port: int):
        super().__init__()
        self._set_client(ModbusTcpClient(host=addr, port=port))
        # self._connect()


class RTUClient(BaseClient):
    def __init__(self, port: str, baudrate: int, timeout: int):
        super().__init__()
        self._set_client(ModbusSerialClient(framer=FramerType.RTU, port=port, baudrate=baudrate, timeout=timeout))
        self._connect()

if __name__ == '__main__':
    """ 代码写法① """
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

    # .run_modbus_workflow(move_2_left_workflow)

    """ 代码写法② """
    # if False:
    #     modbus_tcp_client_test2 = TCPClient('192.168.3.2', 502)
    #     modbus_tcp_client_test2.register_node_list_from_csv_path('M01.csv')
    #     init = modbus_tcp_client_test2.create_init_function('idel_init', 'M01_idlepos_velocity_rw', 'write', 20.0)
    #
    #     modbus_tcp_client_test2.create_address_function('pos_tip', 'M01_idlepos_coil_w', 'write', True)
    #     modbus_tcp_client_test2.create_address_function('pos_tip_read', 'M01_idlepos_coil_r', 'read', 1)
    #     modbus_tcp_client_test2.create_address_function('manual_stop', 'M01_manual_stop_coil_r', 'read', 1)
    #     start = modbus_tcp_client_test2.create_start_function(
    #         'idel_position',
    #         write_functions=[
    #             'pos_tip'
    #         ],
    #         condition_functions=[
    #             'pos_tip_read',
    #             'manual_stop'
    #         ],
    #         stop_condition_expression='pos_tip_read[0] and manual_stop[0]'
    #     )
    #     stop = modbus_tcp_client_test2.create_stop_function('idel_stop', 'M01_idlepos_coil_w', 'write', False)
    #
    #     move_idel = ModbusWorkflow(name="归位", actions=[WorkflowAction(
    #         init=init,
    #         start=start,
    #         stop=stop,
    #     )])
    #
    #     modbus_tcp_client_test2.create_address_function('pipetter_position', 'M01_pipette0_coil_w', 'write', True)
    #     modbus_tcp_client_test2.create_address_function('pipetter_position_read', 'M01_pipette0_coil_r', 'read', 1)
    #     modbus_tcp_client_test2.create_address_function('pipetter_stop_read', 'M01_manual_stop_coil_r', 'read', 1)
    #     pipetter_position = modbus_tcp_client_test2.create_start_function(
    #         'pipetter_start',
    #         write_functions=[
    #             'pipetter_position'
    #         ],
    #         condition_functions=[
    #             'pipetter_position_read',
    #             'pipetter_stop_read'
    #         ],
    #         stop_condition_expression='pipetter_position[0] and pipetter_stop_read[0]'
    #     )
    #     pipetter_stop = modbus_tcp_client_test2.create_stop_function('pipetter_stop', 'M01_pipette0_coil_w', 'write', False)
    #
    #     move_pipetter = ModbusWorkflow(name="测试待机位置", actions=[WorkflowAction(
    #         init=None,
    #         start=pipetter_position,
    #         stop=pipetter_stop,
    #     )])
    #
    #     workflow_test_2 = ModbusWorkflow(name="测试水平移动并停止", actions=[
    #         move_idel,
    #         move_pipetter,
    #     ])
    #
    #     workflow_test_2.run_modbus_workflow()

    """ 代码写法③ """
    with open('example_json.json', 'r', encoding='utf-8') as f:
        example_json = json.load(f)
    modbus_tcp_client_test2 = TCPClient('127.0.0.1', 5021)
    modbus_tcp_client_test2.execute_procedure_from_json(example_json)
    # .run_modbus_workflow(move_2_left_workflow)
#     init_client(FramerType.SOCKET, "", '192.168.3.2', 502)
