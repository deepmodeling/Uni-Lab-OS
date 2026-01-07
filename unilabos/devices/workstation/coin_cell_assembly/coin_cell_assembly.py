import csv
import inspect
import json
import os
import threading
import time
import types
from datetime import datetime
from typing import Any, Dict, Optional
from functools import wraps
from pylabrobot.resources import Deck, Resource as PLRResource
from unilabos_msgs.msg import Resource
from unilabos.device_comms.modbus_plc.client import ModbusTcpClient
from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.device_comms.modbus_plc.client import TCPClient, ModbusNode, PLCWorkflow, ModbusWorkflow, WorkflowAction, BaseClient
from unilabos.device_comms.modbus_plc.modbus import DeviceType, Base as ModbusNodeBase, DataType, WorderOrder
from unilabos.devices.workstation.coin_cell_assembly.YB_YH_materials import *
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode, BaseROS2DeviceNode
from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode
from unilabos.devices.workstation.coin_cell_assembly.YB_YH_materials import CoincellDeck
from unilabos.resources.graphio import convert_resources_to_type
from unilabos.utils.log import logger
from pymodbus.payload import BinaryPayloadDecoder
from pymodbus.constants import Endian


def _decode_float32_correct(registers):
    """
    正确解码FLOAT32类型的Modbus寄存器
    
    Args:
        registers: 从Modbus读取的原始寄存器值列表
        
    Returns:
        正确解码的浮点数值
        
    Note:
        根据test_glove_box_pressure.py验证的配置:
        - Byte Order: Big (Modbus标准)
        - Word Order: Little (PLC配置)
    """
    if not registers or len(registers) < 2:
        return 0.0
    
    try:
        # 使用正确的字节序配置
        decoder = BinaryPayloadDecoder.fromRegisters(
            registers,
            byteorder=Endian.Big,    # 字节序始终为Big
            wordorder=Endian.Little  # 字序为Little (根据PLC配置)
        )
        return decoder.decode_32bit_float()
    except Exception as e:
        logger.error(f"解码FLOAT32失败: {e}, registers: {registers}")
        return 0.0


def _ensure_modbus_slave_kw_alias(modbus_client):
    if modbus_client is None:
        return

    method_names = [
        "read_coils",
        "write_coils",
        "write_coil",
        "read_discrete_inputs",
        "read_holding_registers",
        "write_register",
        "write_registers",
    ]

    def _wrap(func):
        signature = inspect.signature(func)
        has_var_kwargs = any(param.kind == param.VAR_KEYWORD for param in signature.parameters.values())
        accepts_unit = has_var_kwargs or "unit" in signature.parameters
        accepts_slave = has_var_kwargs or "slave" in signature.parameters

        @wraps(func)
        def _wrapped(self, *args, **kwargs):
            if "slave" in kwargs and not accepts_slave:
                slave_value = kwargs.pop("slave")
                if accepts_unit and "unit" not in kwargs:
                    kwargs["unit"] = slave_value
            if "unit" in kwargs and not accepts_unit:
                unit_value = kwargs.pop("unit")
                if accepts_slave and "slave" not in kwargs:
                    kwargs["slave"] = unit_value
            return func(self, *args, **kwargs)

        _wrapped._has_slave_alias = True
        return _wrapped

    for name in method_names:
        if not hasattr(modbus_client, name):
            continue
        bound_method = getattr(modbus_client, name)
        func = getattr(bound_method, "__func__", None)
        if func is None:
            continue
        if getattr(func, "_has_slave_alias", False):
            continue
        wrapped = _wrap(func)
        setattr(modbus_client, name, types.MethodType(wrapped, modbus_client))


def _coerce_deck_input(deck: Any) -> Optional[Deck]:
    if deck is None:
        return None

    if isinstance(deck, Deck):
        return deck

    if isinstance(deck, PLRResource):
        return deck if isinstance(deck, Deck) else None

    candidates = None
    if isinstance(deck, dict):
        if "nodes" in deck and isinstance(deck["nodes"], list):
            candidates = deck["nodes"]
        else:
            candidates = [deck]
    elif isinstance(deck, list):
        candidates = deck

    if candidates is None:
        return None

    try:
        converted = convert_resources_to_type(resources_list=candidates, resource_type=Deck)
        if isinstance(converted, Deck):
            return converted
        if isinstance(converted, list):
            for item in converted:
                if isinstance(item, Deck):
                    return item
    except Exception as exc:
        logger.warning(f"deck 转换 Deck 失败: {exc}")
    return None


#构建物料系统

class CoinCellAssemblyWorkstation(WorkstationBase):
    def __init__(self, 
        config: dict = None, 
        deck=None, 
        address: str = "172.16.28.102",
        port: str = "502",
        debug_mode: bool = False,
        *args,
        **kwargs):

        if deck is None and config:
            deck = config.get('deck')
        if deck is None:
            logger.info("没有传入依华deck，检查启动json文件")
        super().__init__(deck=deck, *args, **kwargs,)
        self.debug_mode = debug_mode
 
        """ 连接初始化 """
        modbus_client = TCPClient(addr=address, port=port)
        logger.debug(f"创建 Modbus 客户端: {modbus_client}")
        _ensure_modbus_slave_kw_alias(modbus_client.client)
        if not debug_mode:
            modbus_client.client.connect()
            count = 100
            while count >0:
                count -=1
                if modbus_client.client.is_socket_open():
                    break
                time.sleep(2)
            if not modbus_client.client.is_socket_open():
                raise ValueError('modbus tcp connection failed')
            self.nodes = BaseClient.load_csv(os.path.join(os.path.dirname(__file__), 'coin_cell_assembly_b.csv'))                            
            self.client = modbus_client.register_node_list(self.nodes)
        else:
            print("测试模式，跳过连接")
            self.nodes, self.client = None, None

        """ 工站的配置 """

        self.success = False
        self.allow_data_read = False  #允许读取函数运行标志位
        self.csv_export_thread = None
        self.csv_export_running = False
        self.csv_export_file = None
        self.coin_num_N = 0  #已组装电池数量

    def post_init(self, ros_node: ROS2WorkstationNode):
        self._ros_node = ros_node
        #self.deck = create_a_coin_cell_deck()
        ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
            "resources": [self.deck]
        })

    # 批量操作在这里写
    async def change_hole_sheet_to_2(self, hole: MaterialHole):
        hole._unilabos_state["max_sheets"] = 2
        return await self._ros_node.update_resource(hole)

    
    async def fill_plate(self):
        plate_1: MaterialPlate = self.deck.children[0].children[0]
        #plate_1
        return await self._ros_node.update_resource(plate_1)

    #def run_assembly(self, wf_name: str, resource: PLRResource, params: str = "\{\}"):
    #    """启动工作流"""
    #    self.current_workflow_status = WorkflowStatus.RUNNING
    #    logger.info(f"工作站 {self.device_id} 启动工作流: {wf_name}")
#
    #    # TODO: 实现工作流逻辑
#
    #    anode_sheet = self.deck.get_resource("anode_sheet")
        
    """ Action逻辑代码 """
    def _sys_start_cmd(self, cmd=None):
        """设备启动命令 (可读写)"""
        if cmd is not None:  # 写入模式
            self.success = False
            node = self.client.use_node('COIL_SYS_START_CMD')
            ret = node.write(cmd)
            print(ret)
            self.success = True
            return self.success
        else:  # 读取模式
            cmd_feedback, read_err =  self.client.use_node('COIL_SYS_START_CMD').read(1)
            return cmd_feedback[0]

    def _sys_stop_cmd(self, cmd=None):
        """设备停止命令 (可读写)"""
        if cmd is not None:  # 写入模式
            self.success = False
            node = self.client.use_node('COIL_SYS_STOP_CMD')
            node.write(cmd)
            self.success = True
            return self.success
        else:  # 读取模式
            cmd_feedback, read_err = self.client.use_node('COIL_SYS_STOP_CMD').read(1)
            return cmd_feedback[0]

    def _sys_reset_cmd(self, cmd=None):
        """设备复位命令 (可读写)"""
        if cmd is not None:
            self.success = False
            self.client.use_node('COIL_SYS_RESET_CMD').write(cmd)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('COIL_SYS_RESET_CMD').read(1)
            return cmd_feedback[0]

    def _sys_hand_cmd(self, cmd=None):
        """手动模式命令 (可读写)"""
        if cmd is not None:
            self.success = False
            self.client.use_node('COIL_SYS_HAND_CMD').write(cmd)
            self.success = True
            print("步骤0")
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('COIL_SYS_HAND_CMD').read(1)
            return cmd_feedback[0]

    def _sys_auto_cmd(self, cmd=None):
        """自动模式命令 (可读写)"""
        if cmd is not None:
            self.success = False
            self.client.use_node('COIL_SYS_AUTO_CMD').write(cmd)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('COIL_SYS_AUTO_CMD').read(1)
            return cmd_feedback[0]

    def _sys_init_cmd(self, cmd=None):
        """初始化命令 (可读写)"""
        if cmd is not None:
            self.success = False
            self.client.use_node('COIL_SYS_INIT_CMD').write(cmd)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('COIL_SYS_INIT_CMD').read(1)
            return cmd_feedback[0]

    def _unilab_send_msg_succ_cmd(self, cmd=None):
        """UNILAB发送配方完毕 (可读写)"""
        if cmd is not None:
            self.success = False
            self.client.use_node('COIL_UNILAB_SEND_MSG_SUCC_CMD').write(cmd)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('COIL_UNILAB_SEND_MSG_SUCC_CMD').read(1)
            return cmd_feedback[0]

    def _unilab_rec_msg_succ_cmd(self, cmd=None):
        """UNILAB接收测试电池数据完毕 (可读写)"""
        if cmd is not None:
            self.success = False
            self.client.use_node('COIL_UNILAB_REC_MSG_SUCC_CMD').write(cmd)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('COIL_UNILAB_REC_MSG_SUCC_CMD').read(1)
            return cmd_feedback


  # ====================== 命令类指令（REG_x_） ======================
    def _unilab_send_msg_electrolyte_num(self, num=None):
        """UNILAB写电解液使用瓶数(可读写)"""
        if num is not None:
            self.success = False
            ret = self.client.use_node('REG_MSG_ELECTROLYTE_NUM').write(num)
            print(ret)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('REG_MSG_ELECTROLYTE_NUM').read(1)
            return cmd_feedback[0]

    def _unilab_send_msg_electrolyte_use_num(self, use_num=None):
        """UNILAB写单次电解液使用瓶数(可读写)"""
        if use_num is not None:
            self.success = False
            self.client.use_node('REG_MSG_ELECTROLYTE_USE_NUM').write(use_num)
            self.success = True
            return self.success
        else:
            return False

    def _unilab_send_msg_assembly_type(self, num=None):
        """UNILAB写组装参数"""
        if num is not None:
            self.success = False
            self.client.use_node('REG_MSG_ASSEMBLY_TYPE').write(num)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('REG_MSG_ASSEMBLY_TYPE').read(1)
            return cmd_feedback[0]

    def _unilab_send_msg_electrolyte_vol(self, vol=None):
        """UNILAB写电解液吸取量参数"""
        if vol is not None:
            self.success = False
            self.client.use_node('REG_MSG_ELECTROLYTE_VOLUME').write(vol, data_type=DataType.FLOAT32, word_order=WorderOrder.LITTLE)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('REG_MSG_ELECTROLYTE_VOLUME').read(2, word_order=WorderOrder.LITTLE)
            return cmd_feedback[0]

    def _unilab_send_msg_assembly_pressure(self, vol=None):
        """UNILAB写电池压制力"""
        if vol is not None:
            self.success = False
            self.client.use_node('REG_MSG_ASSEMBLY_PRESSURE').write(vol, data_type=DataType.FLOAT32, word_order=WorderOrder.LITTLE)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('REG_MSG_ASSEMBLY_PRESSURE').read(2, word_order=WorderOrder.LITTLE)
            return cmd_feedback[0]
        
    # ==================== 0905新增内容（COIL_x_STATUS） ====================
    def _unilab_send_electrolyte_bottle_num(self, num=None):
        """UNILAB发送电解液瓶数完毕"""
        if num is not None:
            self.success = False
            self.client.use_node('UNILAB_SEND_ELECTROLYTE_BOTTLE_NUM').write(num)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('UNILAB_SEND_ELECTROLYTE_BOTTLE_NUM').read(1)
            return cmd_feedback[0]
        
    def _unilab_rece_electrolyte_bottle_num(self, num=None):
        """设备请求接受电解液瓶数"""
        if num is not None:
            self.success = False
            self.client.use_node('UNILAB_RECE_ELECTROLYTE_BOTTLE_NUM').write(num)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('UNILAB_RECE_ELECTROLYTE_BOTTLE_NUM').read(1)
            return cmd_feedback[0]

    def _reg_msg_electrolyte_num(self, num=None):
        """电解液已使用瓶数"""
        if num is not None:
            self.success = False
            self.client.use_node('REG_MSG_ELECTROLYTE_NUM').write(num)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('REG_MSG_ELECTROLYTE_NUM').read(1)
            return cmd_feedback[0]

    def _reg_data_electrolyte_use_num(self, num=None):
        """单瓶电解液完成组装数"""
        if num is not None:
            self.success = False
            self.client.use_node('REG_DATA_ELECTROLYTE_USE_NUM').write(num)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('REG_DATA_ELECTROLYTE_USE_NUM').read(1)
            return cmd_feedback[0]
        
    def _unilab_send_finished_cmd(self, num=None):
        """Unilab发送已知一组组装完成信号"""
        if num is not None:
            self.success = False
            self.client.use_node('UNILAB_SEND_FINISHED_CMD').write(num)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('UNILAB_SEND_FINISHED_CMD').read(1)
            return cmd_feedback[0]

    def _unilab_rece_finished_cmd(self, num=None):
        """Unilab接收已知一组组装完成信号"""
        if num is not None:
            self.success = False
            self.client.use_node('UNILAB_RECE_FINISHED_CMD').write(num)
            self.success = True
            return self.success
        else:
            cmd_feedback, read_err = self.client.use_node('UNILAB_RECE_FINISHED_CMD').read(1)
            return cmd_feedback[0]



    # ==================== 状态类属性（COIL_x_STATUS） ====================
    def _sys_start_status(self) -> bool:
        """设备启动中( BOOL)"""
        status, read_err = self.client.use_node('COIL_SYS_START_STATUS').read(1)
        return status[0]

    def _sys_stop_status(self) -> bool:
        """设备停止中( BOOL)"""
        status, read_err = self.client.use_node('COIL_SYS_STOP_STATUS').read(1)
        return status[0]

    def _sys_reset_status(self) -> bool:
        """设备复位中( BOOL)"""
        status, read_err = self.client.use_node('COIL_SYS_RESET_STATUS').read(1)
        return status[0]

    def _sys_init_status(self) -> bool:
        """设备初始化完成( BOOL)"""
        status, read_err = self.client.use_node('COIL_SYS_INIT_STATUS').read(1)
        return status[0]
    
    # 查找资源
    def modify_deck_name(self, resource_name: str):
        # figure_res = self._ros_node.resource_tracker.figure_resource({"name": resource_name})
        # print(f"!!! figure_res: {type(figure_res)}")
        self.deck.children[1]
        return

    @property
    def sys_status(self) -> str:
        if self.debug_mode:
            return "设备调试模式"
        if self._sys_start_status():
            return "设备启动中"
        elif self._sys_stop_status():
            return "设备停止中"
        elif self._sys_reset_status():
            return "设备复位中"
        elif self._sys_init_status():
            return "设备初始化中"
        else:
            return "未知状态"

    def _sys_hand_status(self) -> bool:
        """设备手动模式( BOOL)"""
        status, read_err = self.client.use_node('COIL_SYS_HAND_STATUS').read(1)
        return status[0]

    def _sys_auto_status(self) -> bool:
        """设备自动模式( BOOL)"""
        status, read_err = self.client.use_node('COIL_SYS_AUTO_STATUS').read(1)
        return status[0]

    @property
    def sys_mode(self) -> str:
        if self.debug_mode:
            return "设备调试模式"
        if self._sys_hand_status():
            return "设备手动模式"
        elif self._sys_auto_status():
            return "设备自动模式"
        else:
            return "未知模式"

    @property
    def request_rec_msg_status(self) -> bool:
        """设备请求接受配方( BOOL)"""
        if self.debug_mode:
            return True
        status, read_err = self.client.use_node('COIL_REQUEST_REC_MSG_STATUS').read(1)
        return status[0]

    @property
    def request_send_msg_status(self) -> bool:
        """设备请求发送测试数据( BOOL)"""
        if self.debug_mode:
            return True
        status, read_err = self.client.use_node('COIL_REQUEST_SEND_MSG_STATUS').read(1)
        return status[0]

    # ======================= 其他属性（特殊功能） ========================
    '''
    @property
    def warning_1(self) -> bool:
        status, read_err = self.client.use_node('COIL_WARNING_1').read(1)
        return status[0]
    '''
    # ===================== 生产数据区 ======================
    
    @property
    def data_assembly_coin_cell_num(self) -> int:
        """已完成电池数量 (INT16)"""
        if self.debug_mode:
            return 0
        num, read_err = self.client.use_node('REG_DATA_ASSEMBLY_COIN_CELL_NUM').read(1)
        return num

    @property
    def data_assembly_time(self) -> float:
        """单颗电池组装时间 (秒, REAL/FLOAT32)"""
        if self.debug_mode:
            return 0
        # 读取原始寄存器并正确解码FLOAT32
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_ASSEMBLY_PER_TIME').address, count=2)
        if result.isError():
            logger.error(f"读取组装时间失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_open_circuit_voltage(self) -> float:
        """开路电压值 (FLOAT32)"""
        if self.debug_mode:
            return 0
        # 读取原始寄存器并正确解码FLOAT32
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_OPEN_CIRCUIT_VOLTAGE').address, count=2)
        if result.isError():
            logger.error(f"读取开路电压失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_axis_x_pos(self) -> float:
        """分液X轴当前位置 (FLOAT32)"""
        if self.debug_mode:
            return 0
        # 读取原始寄存器并正确解码FLOAT32
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_AXIS_X_POS').address, count=2)
        if result.isError():
            logger.error(f"读取X轴位置失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_axis_y_pos(self) -> float:
        """分液Y轴当前位置 (FLOAT32)"""
        if self.debug_mode:
            return 0
        # 读取原始寄存器并正确解码FLOAT32
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_AXIS_Y_POS').address, count=2)
        if result.isError():
            logger.error(f"读变Y轴位置失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_axis_z_pos(self) -> float:
        """分液Z轴当前位置 (FLOAT32)"""
        if self.debug_mode:
            return 0
        # 读取原始寄存器并正确解码FLOAT32
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_AXIS_Z_POS').address, count=2)
        if result.isError():
            logger.error(f"读取Z轴位置失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_pole_weight(self) -> float:
        """当前电池正极片称重数据 (FLOAT32)"""
        if self.debug_mode:
            return 0
        # 读取原始寄存器并正确解码FLOAT32
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_POLE_WEIGHT').address, count=2)
        if result.isError():
            logger.error(f"读取极片质量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_assembly_pressure(self) -> int:
        """当前电池压制力 (INT16)"""
        if self.debug_mode:
            return 0
        pressure, read_err = self.client.use_node('REG_DATA_ASSEMBLY_PRESSURE').read(1)
        return pressure

    @property
    def data_electrolyte_volume(self) -> int:
        """当前电解液加注量 (INT16)"""
        if self.debug_mode:
            return 0
        vol, read_err = self.client.use_node('REG_DATA_ELECTROLYTE_VOLUME').read(1)
        return vol

    @property
    def data_coin_num(self) -> int:
        """当前电池数量 (INT16)"""
        if self.debug_mode:
            return 0
        num, read_err = self.client.use_node('REG_DATA_COIN_NUM').read(1)
        return num

    @property
    def data_coin_cell_code(self) -> str:
        """电池二维码序列号 (STRING)"""
        try:
            # 读取 STRING 类型数据
            code_little, read_err = self.client.use_node('REG_DATA_COIN_CELL_CODE').read(10, word_order=WorderOrder.LITTLE)
            
            # 处理 bytes 或 string 类型
            if isinstance(code_little, bytes):
                code_str = code_little.decode('utf-8', errors='ignore')
            elif isinstance(code_little, str):
                code_str = code_little
            else:
                logger.warning(f"电池二维码返回的类型不支持: {type(code_little)}")
                return "N/A"
            
            # 取前8个字符
            raw_code = code_str[:8]
            
            # LITTLE字节序需要每2个字符交换位置
            clean_code = ''.join([raw_code[i+1] + raw_code[i] for i in range(0, len(raw_code), 2)])
            
            # 去除空字符和空格
            decoded = clean_code.replace('\x00', '').replace('\r', '').replace('\n', '').strip()
            
            return decoded if decoded else "N/A"
        except Exception as e:
            logger.error(f"读取电池二维码失败: {e}")
            return "N/A"


    @property
    def data_electrolyte_code(self) -> str:
        """电解液二维码序列号 (STRING)"""
        try:
            # 读取 STRING 类型数据
            code_little, read_err = self.client.use_node('REG_DATA_ELECTROLYTE_CODE').read(10, word_order=WorderOrder.LITTLE)
            
            # 处理 bytes 或 string 类型
            if isinstance(code_little, bytes):
                code_str = code_little.decode('utf-8', errors='ignore')
            elif isinstance(code_little, str):
                code_str = code_little
            else:
                logger.warning(f"电解液二维码返回的类型不支持: {type(code_little)}")
                return "N/A"
            
            # 取前8个字符
            raw_code = code_str[:8]
            
            # LITTLE字节序需要每2个字符交换位置
            clean_code = ''.join([raw_code[i+1] + raw_code[i] for i in range(0, len(raw_code), 2)])
            
            # 去除空字符和空格
            decoded = clean_code.replace('\x00', '').replace('\r', '').replace('\n', '').strip()
            
            return decoded if decoded else "N/A"
        except Exception as e:
            logger.error(f"读取电解液二维码失败: {e}")
            return "N/A"

    # ===================== 环境监控区 ======================
    @property
    def data_glove_box_pressure(self) -> float:
        """手套箱压力 (mbar, FLOAT32)"""
        if self.debug_mode:
            return 0
        # 读取原始寄存器并正确解码FLOAT32
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_GLOVE_BOX_PRESSURE').address, count=2)
        if result.isError():
            logger.error(f"读取手套箱压力失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_glove_box_o2_content(self) -> float:
        """手套箱氧含量 (ppm, FLOAT32)"""
        if self.debug_mode:
            return 0
        # 读取原始寄存器并正确解码FLOAT32
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_GLOVE_BOX_O2_CONTENT').address, count=2)
        if result.isError():
            logger.error(f"读取手套箱氧含量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_glove_box_water_content(self) -> float:
        """手套箱水含量 (ppm, FLOAT32)"""
        if self.debug_mode:
            return 0
        # 读取原始寄存器并正确解码FLOAT32
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_GLOVE_BOX_WATER_CONTENT').address, count=2)
        if result.isError():
            logger.error(f"读取手套箱水含量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

#    @property
#    def data_stack_vision_code(self) -> int:
#        """物料堆叠复检图片编码 (INT16)"""
#        if self.debug_mode:
#            return 0
#        code, read_err =  self.client.use_node('REG_DATA_STACK_VISON_CODE').read(1)
#        #code, _ =  self.client.use_node('REG_DATA_STACK_VISON_CODE').read(1).type
#        print(f"读取物料堆叠复检图片编码", {code}, "error", type(code))
#        #print(code.type)
#        # print(read_err)
#        return int(code)

    def func_pack_device_init(self):
        #切换手动模式
        print("切换手动模式")
        self._sys_hand_cmd(True)
        time.sleep(1)
        while (self._sys_hand_status()) == False:
            print("waiting for hand_cmd")
            time.sleep(1)
        #设备初始化
        self._sys_init_cmd(True)
        time.sleep(1)
        #sys_init_status为bool值，不加括号
        while (self._sys_init_status())== False:
            print("waiting for init_cmd")
            time.sleep(1)
        #手动按钮置回False
        self._sys_hand_cmd(False)
        time.sleep(1)
        while (self._sys_hand_cmd()) == True:
            print("waiting for hand_cmd to False")
            time.sleep(1)
        #初始化命令置回False
        self._sys_init_cmd(False)
        time.sleep(1)
        while (self._sys_init_cmd()) == True:
            print("waiting for init_cmd to False")
            time.sleep(1)

    def func_pack_device_auto(self):
        #切换自动
        print("切换自动模式")
        self._sys_auto_cmd(True)
        time.sleep(1)
        while (self._sys_auto_status()) == False:
            print("waiting for auto_status")
            time.sleep(1)
        #自动按钮置False
        self._sys_auto_cmd(False)
        time.sleep(1)
        while (self._sys_auto_cmd()) == True:
            print("waiting for auto_cmd")
            time.sleep(1)

    def func_pack_device_start(self):
        #切换自动
        print("启动")
        self._sys_start_cmd(True)
        time.sleep(1)
        while (self._sys_start_status()) == False:
            print("waiting for start_status")
            time.sleep(1)
        #自动按钮置False
        self._sys_start_cmd(False)
        time.sleep(1)
        while (self._sys_start_cmd()) == True:
            print("waiting for start_cmd")
            time.sleep(1)      

    def func_pack_device_init_auto_start_combined(self) -> bool:
        """
        组合函数：设备初始化 + 切换自动模式 + 启动
        
        整合了原有的三个独立函数：
        1. func_pack_device_init()  - 设备初始化
        2. func_pack_device_auto()  - 切换自动模式
        3. func_pack_device_start() - 启动设备
        
        Returns:
            bool: 操作成功返回 True，失败返回 False
        """
        logger.info("=" * 60)
        logger.info("开始组合操作：设备初始化 → 自动模式 → 启动")
        logger.info("=" * 60)
        
        # 步骤0: 前置条件检查
        logger.info("\n【步骤 0/3】前置条件检查...")
        try:
            # 检查 REG_UNILAB_INTERACT (应该为False，表示使用Unilab交互)
            unilab_interact_node = self.client.use_node('REG_UNILAB_INTERACT')
            unilab_interact_value, read_err = unilab_interact_node.read(1)
            
            if read_err:
                error_msg = "❌ 无法读取 REG_UNILAB_INTERACT 状态！请检查设备连接。"
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            
            # 提取实际值（处理可能的列表或单值）
            if isinstance(unilab_interact_value, (list, tuple)):
                unilab_interact_actual = unilab_interact_value[0] if len(unilab_interact_value) > 0 else None
            else:
                unilab_interact_actual = unilab_interact_value
            
            logger.info(f"  REG_UNILAB_INTERACT 当前值: {unilab_interact_actual}")
            
            if unilab_interact_actual != False:
                error_msg = (
                    "❌ 前置条件检查失败！\n"
                    f"  REG_UNILAB_INTERACT = {unilab_interact_actual} (期望值: False)\n"
                    "  说明: 当前设备设置为'忽略Unilab交互'模式\n"
                    "  操作: 请在HMI上确认并切换为'使用Unilab交互'模式\n"
                    "  提示: REG_UNILAB_INTERACT应该为False才能继续"
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            
            logger.info("  ✓ REG_UNILAB_INTERACT 检查通过 (值为False，使用Unilab交互)")
            
            # 检查 COIL_GB_L_IGNORE_CMD (应该为False，表示使用左手套箱)
            gb_l_ignore_node = self.client.use_node('COIL_GB_L_IGNORE_CMD')
            gb_l_ignore_value, read_err = gb_l_ignore_node.read(1)
            
            if read_err:
                error_msg = "❌ 无法读取 COIL_GB_L_IGNORE_CMD 状态！请检查设备连接。"
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            
            # 提取实际值
            if isinstance(gb_l_ignore_value, (list, tuple)):
                gb_l_ignore_actual = gb_l_ignore_value[0] if len(gb_l_ignore_value) > 0 else None
            else:
                gb_l_ignore_actual = gb_l_ignore_value
            
            logger.info(f"  COIL_GB_L_IGNORE_CMD 当前值: {gb_l_ignore_actual}")
            
            if gb_l_ignore_actual != False:
                error_msg = (
                    "❌ 前置条件检查失败！\n"
                    f"  COIL_GB_L_IGNORE_CMD = {gb_l_ignore_actual} (期望值: False)\n"
                    "  说明: 当前设备设置为'忽略左手套箱'模式\n"
                    "  操作: 请在HMI上确认并切换为'使用左手套箱'模式\n"
                    "  提示: COIL_GB_L_IGNORE_CMD应该为False才能继续"
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            
            logger.info("  ✓ COIL_GB_L_IGNORE_CMD 检查通过 (值为False，使用左手套箱)")
            logger.info("✓ 所有前置条件检查通过！")
            
        except ValueError as e:
            # 节点未找到
            error_msg = f"❌ 配置错误：{str(e)}\n请检查CSV配置文件是否包含必要的节点。"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        except Exception as e:
            # 其他异常
            error_msg = f"❌ 前置条件检查异常：{str(e)}"
            logger.error(error_msg)
            raise
        
        # 步骤1: 设备初始化
        logger.info("\n【步骤 1/3】设备初始化...")
        try:
            self.func_pack_device_init()
            logger.info("✓ 设备初始化完成")
        except Exception as e:
            logger.error(f"❌ 设备初始化失败: {e}")
            return False
        
        # 步骤2: 切换自动模式
        logger.info("\n【步骤 2/3】切换自动模式...")
        try:
            self.func_pack_device_auto()
            logger.info("✓ 切换自动模式完成")
        except Exception as e:
            logger.error(f"❌ 切换自动模式失败: {e}")
            return False
        
        # 步骤3: 启动设备
        logger.info("\n【步骤 3/3】启动设备...")
        try:
            self.func_pack_device_start()
            logger.info("✓ 启动设备完成")
        except Exception as e:
            logger.error(f"❌ 启动设备失败: {e}")
            return False
        
        logger.info("\n" + "=" * 60)
        logger.info("组合操作完成：设备已成功初始化、切换自动模式并启动")
        logger.info("=" * 60)
        
        return True

    def func_pack_send_bottle_num(self, bottle_num):
        bottle_num = int(bottle_num)
        #发送电解液平台数
        print("启动")
        while (self._unilab_rece_electrolyte_bottle_num()) == False:
            print("waiting for rece_electrolyte_bottle_num to True")
            # self.client.use_node('8520').write(True)
            time.sleep(1)     
        #发送电解液瓶数为2
        self._reg_msg_electrolyte_num(bottle_num)
        time.sleep(1)
        #完成信号置True
        self._unilab_send_electrolyte_bottle_num(True)
        time.sleep(1)
        #检测到依华已接收
        while (self._unilab_rece_electrolyte_bottle_num()) == True:
            print("waiting for rece_electrolyte_bottle_num to False")
            time.sleep(1)    
        #完成信号置False
        self._unilab_send_electrolyte_bottle_num(False) 
        time.sleep(1) 
        #自动按钮置False


    # 下发参数
    #def func_pack_send_msg_cmd(self, elec_num: int, elec_use_num: int, elec_vol: float, assembly_type: int, assembly_pressure: int) -> bool:
    #    """UNILAB写参数"""
    #    while (self.request_rec_msg_status) == False: 
    #        print("wait for res_msg")
    #        time.sleep(1)
    #    self.success = False
    #    self._unilab_send_msg_electrolyte_num(elec_num)
    #    time.sleep(1)
    #    self._unilab_send_msg_electrolyte_use_num(elec_use_num)
    #    time.sleep(1)
    #    self._unilab_send_msg_electrolyte_vol(elec_vol)
    #    time.sleep(1)
    #    self._unilab_send_msg_assembly_type(assembly_type)
    #    time.sleep(1)
    #    self._unilab_send_msg_assembly_pressure(assembly_pressure)
    #    time.sleep(1)
    #    self._unilab_send_msg_succ_cmd(True)
    #    time.sleep(1)
    #    self._unilab_send_msg_succ_cmd(False)
    #    #将允许读取标志位置True
    #    self.allow_data_read = True
    #    self.success = True
    #    return self.success

    def func_pack_send_msg_cmd(self, elec_use_num, elec_vol, assembly_type, assembly_pressure) -> bool:
        """UNILAB写参数"""    
        while (self.request_rec_msg_status) == False: 
            print("wait for request_rec_msg_status to True")
            time.sleep(1)
        self.success = False
        #self._unilab_send_msg_electrolyte_num(elec_num)
        #设置平行样数目
        self._unilab_send_msg_electrolyte_use_num(elec_use_num)
        time.sleep(1)
        #发送电解液加注量
        self._unilab_send_msg_electrolyte_vol(elec_vol)
        time.sleep(1)
        #发送电解液组装类型
        self._unilab_send_msg_assembly_type(assembly_type)
        time.sleep(1)
        #发送电池压制力
        self._unilab_send_msg_assembly_pressure(assembly_pressure)
        time.sleep(1)
        self._unilab_send_msg_succ_cmd(True)
        time.sleep(1)
        while (self.request_rec_msg_status) == True: 
            print("wait for request_rec_msg_status to False")
            time.sleep(1)
        self._unilab_send_msg_succ_cmd(False)
        #将允许读取标志位置True
        self.allow_data_read = True
        self.success = True
        return self.success

    def func_pack_get_msg_cmd(self, file_path: str="D:\\coin_cell_data") -> bool:
        """UNILAB读参数"""    
        while self.request_send_msg_status == False:
            print("waiting for send_read_msg_status to True")
            time.sleep(1)
        
        # 处理开路电压 - 确保是数值类型
        try:
            data_open_circuit_voltage = self.data_open_circuit_voltage
            if isinstance(data_open_circuit_voltage, (list, tuple)) and len(data_open_circuit_voltage) > 0:
                data_open_circuit_voltage = float(data_open_circuit_voltage[0])
            else:
                data_open_circuit_voltage = float(data_open_circuit_voltage)
        except Exception as e:
            print(f"读取开路电压失败: {e}")
            logger.error(f"读取开路电压失败: {e}")
            data_open_circuit_voltage = 0.0
        
        # 处理极片质量 - 确保是数值类型
        try:
            data_pole_weight = self.data_pole_weight
            if isinstance(data_pole_weight, (list, tuple)) and len(data_pole_weight) > 0:
                data_pole_weight = float(data_pole_weight[0])
            else:
                data_pole_weight = float(data_pole_weight)
        except Exception as e:
            print(f"读取正极片重量失败: {e}")
            logger.error(f"读取正极片重量失败: {e}")
            data_pole_weight = 0.0
        
        data_assembly_time = self.data_assembly_time
        data_assembly_pressure = self.data_assembly_pressure
        data_electrolyte_volume = self.data_electrolyte_volume
        data_coin_num = self.data_coin_num
        
        # 处理电解液二维码 - 确保是字符串类型
        try:
            data_electrolyte_code = self.data_electrolyte_code
            if isinstance(data_electrolyte_code, str):
                data_electrolyte_code = data_electrolyte_code.strip()
            else:
                data_electrolyte_code = str(data_electrolyte_code)
        except Exception as e:
            print(f"读取电解液二维码失败: {e}")
            logger.error(f"读取电解液二维码失败: {e}")
            data_electrolyte_code = "N/A"
        
        # 处理电池二维码 - 确保是字符串类型
        try:
            data_coin_cell_code = self.data_coin_cell_code
            if isinstance(data_coin_cell_code, str):
                data_coin_cell_code = data_coin_cell_code.strip()
            else:
                data_coin_cell_code = str(data_coin_cell_code)
        except Exception as e:
            print(f"读取电池二维码失败: {e}")
            logger.error(f"读取电池二维码失败: {e}")
            data_coin_cell_code = "N/A"
        logger.debug(f"data_open_circuit_voltage: {data_open_circuit_voltage}")
        logger.debug(f"data_pole_weight: {data_pole_weight}")
        logger.debug(f"data_assembly_time: {data_assembly_time}")
        logger.debug(f"data_assembly_pressure: {data_assembly_pressure}")
        logger.debug(f"data_electrolyte_volume: {data_electrolyte_volume}")
        logger.debug(f"data_coin_num: {data_coin_num}")
        logger.debug(f"data_electrolyte_code: {data_electrolyte_code}")
        logger.debug(f"data_coin_cell_code: {data_coin_cell_code}")
        #接收完信息后，读取完毕标志位置True
        liaopan3 = self.deck.get_resource("成品弹夹")        
        
        # 生成唯一的电池名称（使用时间戳确保唯一性）
        timestamp_suffix = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        battery_name = f"battery_{self.coin_num_N}_{timestamp_suffix}"
        
        # 检查目标位置是否已有资源，如果有则先卸载
        target_slot = liaopan3.children[self.coin_num_N]
        if target_slot.children:
            logger.warning(f"位置 {self.coin_num_N} 已有资源，将先卸载旧资源")
            try:
                # 卸载所有现有子资源
                for child in list(target_slot.children):
                    target_slot.unassign_child_resource(child)
                    logger.info(f"已卸载旧资源: {child.name}")
            except Exception as e:
                logger.error(f"卸载旧资源时出错: {e}")
        
        # 创建新的电池资源
        battery = ElectrodeSheet(name=battery_name, size_x=14, size_y=14, size_z=2)
        battery._unilabos_state = {
                            "electrolyte_name": data_coin_cell_code,
                            "data_electrolyte_code": data_electrolyte_code,
                            "open_circuit_voltage": data_open_circuit_voltage,
                            "assembly_pressure": data_assembly_pressure,
                            "electrolyte_volume": data_electrolyte_volume
                            }
        
        # 分配新资源到目标位置
        try:
            target_slot.assign_child_resource(battery, location=None)
            logger.info(f"成功分配电池 {battery_name} 到位置 {self.coin_num_N}")
        except Exception as e:
            logger.error(f"分配电池资源失败: {e}")
            # 如果分配失败，尝试使用更简单的方法
            raise
        
        #print(jipian2.parent)
        ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
            "resources": [self.deck]
        })


        self._unilab_rec_msg_succ_cmd(True)
        time.sleep(1)
        #等待允许读取标志位置False
        while self.request_send_msg_status == True:
            print("waiting for send_msg_status to False")
            time.sleep(1)
        self._unilab_rec_msg_succ_cmd(False)
        time.sleep(1)
        #将允许读取标志位置True
        time_date = datetime.now().strftime("%Y%m%d")
            #秒级时间戳用于标记每一行电池数据
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            #生成输出文件的变量
        self.csv_export_file = os.path.join(file_path, f"date_{time_date}.csv")   
        #将数据存入csv文件
        if not os.path.exists(self.csv_export_file):
            #创建一个表头
            with open(self.csv_export_file, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow([
                    'Time', 'open_circuit_voltage', 'pole_weight', 
                    'assembly_time', 'assembly_pressure', 'electrolyte_volume', 
                    'coin_num', 'electrolyte_code', 'coin_cell_code'
                ])
                #立刻写入磁盘
                csvfile.flush()
        #开始追加电池信息
        with open(self.csv_export_file, 'a', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                timestamp, data_open_circuit_voltage, data_pole_weight,
                data_assembly_time, data_assembly_pressure, data_electrolyte_volume,
                data_coin_num, data_electrolyte_code, data_coin_cell_code
            ])
            #立刻写入磁盘
            csvfile.flush()
        self.success = True
        return self.success



    def func_pack_send_finished_cmd(self) -> bool:
        """UNILAB写参数"""    
        while (self._unilab_rece_finished_cmd()) == False: 
            print("wait for rece_finished_cmd to True")
            time.sleep(1)
        self.success = False
        self._unilab_send_finished_cmd(True)
        time.sleep(1)
        while (self._unilab_rece_finished_cmd()) == True: 
            print("wait for rece_finished_cmd to False")
            time.sleep(1)
        self._unilab_send_finished_cmd(False)
        #将允许读取标志位置True
        self.success = True
        return self.success

    def qiming_coin_cell_code(self, fujipian_panshu:int, fujipian_juzhendianwei:int=0, gemopanshu:int=0, gemo_juzhendianwei:int=0, lvbodian:bool=True, battery_pressure_mode:bool=True, battery_pressure:int=4000, battery_clean_ignore:bool=False) -> bool:
        self.success = False
        self.client.use_node('REG_MSG_NE_PLATE_NUM').write(fujipian_panshu)
        self.client.use_node('REG_MSG_NE_PLATE_MATRIX').write(fujipian_juzhendianwei)
        self.client.use_node('REG_MSG_SEPARATOR_PLATE_NUM').write(gemopanshu)
        self.client.use_node('REG_MSG_SEPARATOR_PLATE_MATRIX').write(gemo_juzhendianwei)
        self.client.use_node('COIL_ALUMINUM_FOIL').write(not lvbodian)
        self.client.use_node('REG_MSG_PRESS_MODE').write(not battery_pressure_mode)
        # self.client.use_node('REG_MSG_ASSEMBLY_PRESSURE').write(battery_pressure)
        self.client.use_node('REG_MSG_BATTERY_CLEAN_IGNORE').write(battery_clean_ignore)
        self.success = True
        
        return self.success

    def func_allpack_cmd(self, elec_num, elec_use_num, elec_vol:int=50, assembly_type:int=7, assembly_pressure:int=4200, file_path: str="/Users/sml/work") -> Dict[str, Any]:
        elec_num, elec_use_num, elec_vol, assembly_type, assembly_pressure = int(elec_num), int(elec_use_num), int(elec_vol), int(assembly_type), int(assembly_pressure)
        summary_csv_file = os.path.join(file_path, "duandian.csv")
        
        # 用于收集所有电池的数据
        battery_data_list = []
        
        # 如果断点文件存在，先读取之前的进度
        if os.path.exists(summary_csv_file):
            read_status_flag = True
            with open(summary_csv_file, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.reader(csvfile)
                header = next(reader)  # 跳过标题行
                data_row = next(reader)  # 读取数据行
                if len(data_row) >= 2:
                    elec_num_r = int(data_row[0])
                    elec_use_num_r = int(data_row[1])
                    elec_num_N = int(data_row[2])
                    elec_use_num_N = int(data_row[3])
                    coin_num_N = int(data_row[4])
                    if elec_num_r == elec_num and elec_use_num_r == elec_use_num:
                        print("断点文件与当前任务匹配，继续")
                    else:
                        print("断点文件中elec_num、elec_use_num与当前任务不匹配，请检查任务下发参数或修改断点文件")
                        return {
                            "success": False,
                            "error": "断点文件参数不匹配",
                            "total_batteries": 0,
                            "batteries": []
                        }
                    print(f"从断点文件读取进度: elec_num_N={elec_num_N}, elec_use_num_N={elec_use_num_N}, coin_num_N={coin_num_N}")
                     
        else:
            read_status_flag = False
            print("未找到断点文件，从头开始")
            elec_num_N = 0
            elec_use_num_N = 0
            coin_num_N = 0
        for i in range(20):
            print(f"剩余电解液瓶数: {elec_num}, 已组装电池数: {elec_use_num}")
            print(f"剩余电解液瓶数: {type(elec_num)}, 已组装电池数: {type(elec_use_num)}")
            print(f"剩余电解液瓶数: {type(int(elec_num))}, 已组装电池数: {type(int(elec_use_num))}")
        
        #如果是第一次运行，则进行初始化、切换自动、启动, 如果是断点重启则跳过。
        if read_status_flag == False:
            pass
            #初始化
            #self.func_pack_device_init()
            #切换自动
            #self.func_pack_device_auto()
            #启动，小车收回
            #self.func_pack_device_start()
            #发送电解液瓶数量，启动搬运,多搬运没事
            #self.func_pack_send_bottle_num(elec_num)
        last_i = elec_num_N
        last_j = elec_use_num_N
        for i in range(last_i, elec_num):
            print(f"开始第{last_i+i+1}瓶电解液的组装")
            #第一个循环从上次断点继续，后续循环从0开始
            j_start = last_j if i == last_i else 0
            self.func_pack_send_msg_cmd(elec_use_num-j_start, elec_vol, assembly_type, assembly_pressure)

            for j in range(j_start, elec_use_num):
                print(f"开始第{last_i+i+1}瓶电解液的第{j+j_start+1}个电池组装")
                
                #读取电池组装数据并存入csv
                self.func_pack_get_msg_cmd(file_path)
                
                # 收集当前电池的数据
                # 处理电池二维码
                try:
                    battery_qr_code = self.data_coin_cell_code
                except Exception as e:
                    print(f"读取电池二维码失败: {e}")
                    battery_qr_code = "N/A"
                
                # 处理电解液二维码
                try:
                    electrolyte_qr_code = self.data_electrolyte_code
                except Exception as e:
                    print(f"读取电解液二维码失败: {e}")
                    electrolyte_qr_code = "N/A"
                
                # 处理开路电压 - 确保是数值类型
                try:
                    open_circuit_voltage = self.data_open_circuit_voltage
                    if isinstance(open_circuit_voltage, (list, tuple)) and len(open_circuit_voltage) > 0:
                        open_circuit_voltage = float(open_circuit_voltage[0])
                    else:
                        open_circuit_voltage = float(open_circuit_voltage)
                except Exception as e:
                    print(f"读取开路电压失败: {e}")
                    open_circuit_voltage = 0.0
                
                # 处理极片质量 - 确保是数值类型
                try:
                    pole_weight = self.data_pole_weight
                    if isinstance(pole_weight, (list, tuple)) and len(pole_weight) > 0:
                        pole_weight = float(pole_weight[0])
                    else:
                        pole_weight = float(pole_weight)
                except Exception as e:
                    print(f"读取正极片重量失败: {e}")
                    pole_weight = 0.0
                
                battery_info = {
                    "battery_index": coin_num_N + 1,
                    "battery_barcode": battery_qr_code,
                    "electrolyte_barcode": electrolyte_qr_code,
                    "open_circuit_voltage": open_circuit_voltage,
                    "pole_weight": pole_weight,
                    "assembly_time": self.data_assembly_time,
                    "assembly_pressure": self.data_assembly_pressure,
                    "electrolyte_volume": self.data_electrolyte_volume
                }
                battery_data_list.append(battery_info)
                print(f"已收集第 {coin_num_N + 1} 个电池数据: 电池码={battery_info['battery_barcode']}, 电解液码={battery_info['electrolyte_barcode']}")
                
                time.sleep(1)
                # TODO:读完再将电池数加一还是进入循环就将电池数加一需要考虑

                # 生成断点文件
                # 生成包含elec_num_N、coin_num_N、timestamp的CSV文件
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                with open(summary_csv_file, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(['elec_num','elec_use_num', 'elec_num_N', 'elec_use_num_N', 'coin_num_N', 'timestamp'])
                    writer.writerow([elec_num, elec_use_num, elec_num_N, elec_use_num_N, coin_num_N, timestamp])
                    csvfile.flush()
                coin_num_N += 1
                self.coin_num_N = coin_num_N
                elec_use_num_N += 1
            elec_num_N += 1
            elec_use_num_N = 0

        #循环正常结束，则删除断点文件
        os.remove(summary_csv_file)
        #全部完成后等待依华发送完成信号
        self.func_pack_send_finished_cmd()
        
        # 返回JSON格式数据
        result = {
            "success": True,
            "total_batteries": len(battery_data_list),
            "batteries": battery_data_list,
            "summary": {
                "electrolyte_bottles_used": elec_num,
                "batteries_per_bottle": elec_use_num,
                "electrolyte_volume": elec_vol,
                "assembly_type": assembly_type,
                "assembly_pressure": assembly_pressure
            }
        }
        
        print(f"\n{'='*60}")
        print(f"组装完成统计:")
        print(f"  总组装电池数: {result['total_batteries']}")
        print(f"  使用电解液瓶数: {elec_num}")
        print(f"  每瓶电池数: {elec_use_num}")
        print(f"{'='*60}\n")
        
        return result


    def func_allpack_cmd_simp(
        self, 
        elec_num, 
        elec_use_num, 
        elec_vol: int = 50,
        # 电解液双滴模式参数
        dual_drop_mode: bool = False,
        dual_drop_first_volume: int = 25,
        dual_drop_suction_timing: bool = False,
        dual_drop_start_timing: bool = False,
        assembly_type: int = 7, 
        assembly_pressure: int = 4200,
        # 来自原 qiming_coin_cell_code 的参数
        fujipian_panshu: int = 0,
        fujipian_juzhendianwei: int = 0,
        gemopanshu: int = 0,
        gemo_juzhendianwei: int = 0,
        qiangtou_juzhendianwei: int = 0,
        lvbodian: bool = True,
        battery_pressure_mode: bool = True,
        battery_clean_ignore: bool = False,
        file_path: str = "/Users/sml/work"
    ) -> Dict[str, Any]:
        """
        简化版电池组装函数，整合了原 qiming_coin_cell_code 的参数设置和双滴模式
        
        此函数是 func_allpack_cmd 的增强版本，自动处理以下配置：
        - 负极片和隔膜的盘数及矩阵点位
        - 枪头盒矩阵点位
        - 铝箔垫片使用设置
        - 压力模式和清洁忽略选项
        - 电解液双滴模式（分两次滴液）
        
        Args:
            elec_num: 电解液瓶数
            elec_use_num: 每瓶电解液组装的电池数
            elec_vol: 电解液吸液量 (μL)
            dual_drop_mode: 电解液添加模式 (False=单次滴液, True=二次滴液)
            dual_drop_first_volume: 二次滴液第一次排液体积 (μL)
            dual_drop_suction_timing: 二次滴液吸液时机 (False=正常吸液, True=先吸液)
            dual_drop_start_timing: 二次滴液开始滴液时机 (False=正极片前, True=正极片后)
            assembly_type: 组装类型 (7=不用铝箔垫, 8=使用铝箔垫)
            assembly_pressure: 电池压制力 (N)
            fujipian_panshu: 负极片盘数
            fujipian_juzhendianwei: 负极片矩阵点位
            gemopanshu: 隔膜盘数
            gemo_juzhendianwei: 隔膜矩阵点位
            qiangtou_juzhendianwei: 枪头盒矩阵点位
            lvbodian: 是否使用铝箔垫片
            battery_pressure_mode: 是否启用压力模式
            battery_clean_ignore: 是否忽略电池清洁
            file_path: 实验记录保存路径
        
        Returns:
            dict: 包含组装结果的字典
        """
        # 参数类型转换
        elec_num = int(elec_num)
        elec_use_num = int(elec_use_num)
        elec_vol = int(elec_vol)
        dual_drop_first_volume = int(dual_drop_first_volume)
        assembly_type = int(assembly_type)
        assembly_pressure = int(assembly_pressure)
        fujipian_panshu = int(fujipian_panshu)
        fujipian_juzhendianwei = int(fujipian_juzhendianwei)
        gemopanshu = int(gemopanshu)
        gemo_juzhendianwei = int(gemo_juzhendianwei)
        qiangtou_juzhendianwei = int(qiangtou_juzhendianwei)
        
        # 步骤1: 设置设备参数（原 qiming_coin_cell_code 的功能）
        logger.info("=" * 60)
        logger.info("设置设备参数...")
        logger.info(f"  负极片盘数: {fujipian_panshu}, 矩阵点位: {fujipian_juzhendianwei}")
        logger.info(f"  隔膜盘数: {gemopanshu}, 矩阵点位: {gemo_juzhendianwei}")
        logger.info(f"  枪头盒矩阵点位: {qiangtou_juzhendianwei}")
        logger.info(f"  铝箔垫片: {lvbodian}, 压力模式: {battery_pressure_mode}")
        logger.info(f"  压制力: {assembly_pressure}")
        logger.info(f"  忽略电池清洁: {battery_clean_ignore}")
        logger.info("=" * 60)
        
        # 写入基础参数到PLC
        self.client.use_node('REG_MSG_NE_PLATE_NUM').write(fujipian_panshu)
        self.client.use_node('REG_MSG_NE_PLATE_MATRIX').write(fujipian_juzhendianwei)
        self.client.use_node('REG_MSG_SEPARATOR_PLATE_NUM').write(gemopanshu)
        self.client.use_node('REG_MSG_SEPARATOR_PLATE_MATRIX').write(gemo_juzhendianwei)
        self.client.use_node('REG_MSG_TIP_BOX_MATRIX').write(qiangtou_juzhendianwei)
        self.client.use_node('COIL_ALUMINUM_FOIL').write(not lvbodian)
        self.client.use_node('REG_MSG_PRESS_MODE').write(not battery_pressure_mode)
        self.client.use_node('REG_MSG_BATTERY_CLEAN_IGNORE').write(battery_clean_ignore)
        
        # 设置电解液双滴模式参数
        self.client.use_node('COIL_ELECTROLYTE_DUAL_DROP_MODE').write(dual_drop_mode)
        self.client.use_node('REG_MSG_DUAL_DROP_FIRST_VOLUME').write(dual_drop_first_volume)
        self.client.use_node('COIL_DUAL_DROP_SUCTION_TIMING').write(dual_drop_suction_timing)
        self.client.use_node('COIL_DUAL_DROP_START_TIMING').write(dual_drop_start_timing)
        
        if dual_drop_mode:
            logger.info(f"✓ 双滴模式已启用: 第一次排液={dual_drop_first_volume}μL, "
                        f"吸液时机={'先吸液' if dual_drop_suction_timing else '正常吸液'}, "
                        f"滴液时机={'正极片后' if dual_drop_start_timing else '正极片前'}")
        else:
            logger.info("✓ 单次滴液模式")
        
        logger.info("✓ 设备参数设置完成")
        
        # 步骤2: 执行组装流程（复用 func_allpack_cmd 的主体逻辑）
        summary_csv_file = os.path.join(file_path, "duandian.csv")
        
        # 用于收集所有电池的数据
        battery_data_list = []
        
        # 如果断点文件存在，先读取之前的进度
        if os.path.exists(summary_csv_file):
            read_status_flag = True
            with open(summary_csv_file, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.reader(csvfile)
                header = next(reader)  # 跳过标题行
                data_row = next(reader)  # 读取数据行
                if len(data_row) >= 2:
                    elec_num_r = int(data_row[0])
                    elec_use_num_r = int(data_row[1])
                    elec_num_N = int(data_row[2])
                    elec_use_num_N = int(data_row[3])
                    coin_num_N = int(data_row[4])
                    if elec_num_r == elec_num and elec_use_num_r == elec_use_num:
                        print("断点文件与当前任务匹配，继续")
                    else:
                        print("断点文件中elec_num、elec_use_num与当前任务不匹配，请检查任务下发参数或修改断点文件")
                        return {
                            "success": False,
                            "error": "断点文件参数不匹配",
                            "total_batteries": 0,
                            "batteries": []
                        }
                    print(f"从断点文件读取进度: elec_num_N={elec_num_N}, elec_use_num_N={elec_use_num_N}, coin_num_N={coin_num_N}")
                     
        else:
            read_status_flag = False
            print("未找到断点文件，从头开始")
            elec_num_N = 0
            elec_use_num_N = 0
            coin_num_N = 0
        
        for i in range(20):
            print(f"剩余电解液瓶数: {elec_num}, 已组装电池数: {elec_use_num}")
            print(f"剩余电解液瓶数: {type(elec_num)}, 已组装电池数: {type(elec_use_num)}")
            print(f"剩余电解液瓶数: {type(int(elec_num))}, 已组装电池数: {type(int(elec_use_num))}")
        
        last_i = elec_num_N
        last_j = elec_use_num_N
        for i in range(last_i, elec_num):
            print(f"开始第{last_i+i+1}瓶电解液的组装")
            # 第一个循环从上次断点继续，后续循环从0开始
            j_start = last_j if i == last_i else 0
            self.func_pack_send_msg_cmd(elec_use_num-j_start, elec_vol, assembly_type, assembly_pressure)

            for j in range(j_start, elec_use_num):
                print(f"开始第{last_i+i+1}瓶电解液的第{j+j_start+1}个电池组装")
                
                # 读取电池组装数据并存入csv
                self.func_pack_get_msg_cmd(file_path)
                
                # 收集当前电池的数据
                try:
                    battery_qr_code = self.data_coin_cell_code
                except Exception as e:
                    print(f"读取电池二维码失败: {e}")
                    battery_qr_code = "N/A"
                
                try:
                    electrolyte_qr_code = self.data_electrolyte_code
                except Exception as e:
                    print(f"读取电解液二维码失败: {e}")
                    electrolyte_qr_code = "N/A"
                
                try:
                    open_circuit_voltage = self.data_open_circuit_voltage
                    if isinstance(open_circuit_voltage, (list, tuple)) and len(open_circuit_voltage) > 0:
                        open_circuit_voltage = float(open_circuit_voltage[0])
                    else:
                        open_circuit_voltage = float(open_circuit_voltage)
                except Exception as e:
                    print(f"读取开路电压失败: {e}")
                    open_circuit_voltage = 0.0
                
                try:
                    pole_weight = self.data_pole_weight
                    if isinstance(pole_weight, (list, tuple)) and len(pole_weight) > 0:
                        pole_weight = float(pole_weight[0])
                    else:
                        pole_weight = float(pole_weight)
                except Exception as e:
                    print(f"读取正极片重量失败: {e}")
                    pole_weight = 0.0
                
                battery_info = {
                    "battery_index": coin_num_N + 1,
                    "battery_barcode": battery_qr_code,
                    "electrolyte_barcode": electrolyte_qr_code,
                    "open_circuit_voltage": open_circuit_voltage,
                    "pole_weight": pole_weight,
                    "assembly_time": self.data_assembly_time,
                    "assembly_pressure": self.data_assembly_pressure,
                    "electrolyte_volume": self.data_electrolyte_volume
                }
                battery_data_list.append(battery_info)
                print(f"已收集第 {coin_num_N + 1} 个电池数据: 电池码={battery_info['battery_barcode']}, 电解液码={battery_info['electrolyte_barcode']}")
                
                time.sleep(1)

                # 生成断点文件
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                with open(summary_csv_file, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(['elec_num','elec_use_num', 'elec_num_N', 'elec_use_num_N', 'coin_num_N', 'timestamp'])
                    writer.writerow([elec_num, elec_use_num, elec_num_N, elec_use_num_N, coin_num_N, timestamp])
                    csvfile.flush()
                coin_num_N += 1
                self.coin_num_N = coin_num_N
                elec_use_num_N += 1
            elec_num_N += 1
            elec_use_num_N = 0

        # 循环正常结束，则删除断点文件
        os.remove(summary_csv_file)
        # 全部完成后等待依华发送完成信号
        self.func_pack_send_finished_cmd()
        
        # 返回JSON格式数据
        result = {
            "success": True,
            "total_batteries": len(battery_data_list),
            "batteries": battery_data_list,
            "summary": {
                "electrolyte_bottles_used": elec_num,
                "batteries_per_bottle": elec_use_num,
                "electrolyte_volume": elec_vol,
                "assembly_type": assembly_type,
                "assembly_pressure": assembly_pressure,
                "dual_drop_mode": dual_drop_mode
            }
        }
        
        print(f"\n{'='*60}")
        print(f"组装完成统计:")
        print(f"  总组装电池数: {result['total_batteries']}")
        print(f"  使用电解液瓶数: {elec_num}")
        print(f"  每瓶电池数: {elec_use_num}")
        print(f"  双滴模式: {'启用' if dual_drop_mode else '禁用'}")
        print(f"{'='*60}\n")
        
        return result

    def func_pack_device_stop(self) -> bool:
        """打包指令：设备停止"""
        for i in range(3):
            time.sleep(2)
            print(f"输出{i}")
        #print("_sys_hand_cmd", self._sys_hand_cmd())
        #time.sleep(1)  
        #print("_sys_hand_status", self._sys_hand_status())
        #time.sleep(1)  
        #print("_sys_init_cmd", self._sys_init_cmd())
        #time.sleep(1)  
        #print("_sys_init_status", self._sys_init_status())
        #time.sleep(1)  
        #print("_sys_auto_status", self._sys_auto_status())
        #time.sleep(1)  
        #print("data_axis_y_pos", self.data_axis_y_pos)
        #time.sleep(1)  
        #self.success = False
        #with open('action_device_stop.json', 'r', encoding='utf-8') as f:
        #    action_json = json.load(f)
        #self.client.execute_procedure_from_json(action_json)
        #self.success = True
        #return self.success
    
    def fun_wuliao_test(self) -> bool: 
        #找到data_init中构建的2个物料盘
        liaopan3 = self.deck.get_resource("\u7535\u6c60\u6599\u76d8")
        for i in range(16):            
            battery = ElectrodeSheet(name=f"battery_{i}", size_x=16, size_y=16, size_z=2)
            battery._unilabos_state = {
                                "diameter": 20.0,
                                "height": 20.0,
                                "assembly_pressure": i,
                                "electrolyte_volume": 20.0,
                                "electrolyte_name": f"DP{i}"
                                }
            liaopan3.children[i].assign_child_resource(battery, location=None)
            
            ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
                "resources": [self.deck]
            })
            # for i in range(40):
            #     print(f"fun_wuliao_test 运行结束{i}")
            #     time.sleep(1)
            # time.sleep(40)
    # 数据读取与输出
    def func_read_data_and_output(self, file_path: str="/Users/sml/work"):
        # 检查CSV导出是否正在运行，已运行则跳出，防止同时启动两个while循环
        if self.csv_export_running:
            return False, "读取已在运行中"
        
        #若不存在该目录则创建
        if not os.path.exists(file_path):
            os.makedirs(file_path)
            print(f"创建目录: {file_path}")

        # 只要允许读取标志位为true，就持续运行该函数，直到触发停止条件
        while self.allow_data_read:

            #函数运行标志位，确保只同时启动一个导出函数
            self.csv_export_running = True

            #等待接收结果标志位置True
            while self.request_send_msg_status == False:
                print("waiting for send_msg_status to True")
                time.sleep(1)
            #日期时间戳用于按天存放csv文件
            time_date = datetime.now().strftime("%Y%m%d")
            #秒级时间戳用于标记每一行电池数据
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            #生成输出文件的变量
            self.csv_export_file = os.path.join(file_path, f"date_{time_date}.csv")   
            
            #接收信息
            data_open_circuit_voltage = self.data_open_circuit_voltage
            data_pole_weight = self.data_pole_weight
            data_assembly_time = self.data_assembly_time
            data_assembly_pressure = self.data_assembly_pressure
            data_electrolyte_volume = self.data_electrolyte_volume
            data_coin_num = self.data_coin_num
            data_electrolyte_code = self.data_electrolyte_code
            data_coin_cell_code = self.data_coin_cell_code
            # 电解液瓶位置
            elec_bottle_site = 2
            # 极片夹取位置(应当通过寄存器读光标)
            Pos_elec_site = 0
            Al_elec_site = 0
            Gasket_site = 0

            #接收完信息后，读取完毕标志位置True
            self._unilab_rec_msg_succ_cmd()# = True
            #等待允许读取标志位置False
            while self.request_send_msg_status == True:
                print("waiting for send_msg_status to False")
                time.sleep(1)
            self._unilab_rec_msg_succ_cmd()# = False

            #此处操作物料信息（如果中途报错停止，如何）
            #报错怎么办（加个判断标志位，如果发生错误，则根据停止位置扣除物料）
            #根据物料光标判断取哪个物料（人工摆盘，电解液瓶，移液枪头都有光标位置，寄存器读即可）
            
            #物料读取操作写在这里
            #在这里进行物料调取
            #转移物料瓶，elec_bottle_site对应第几瓶电解液（从依华寄存器读取）
        #    transfer_bottle(deck, elec_bottle_site)
        #    #找到电解液瓶的对象
        #    electrolyte_rack = deck.get_resource("electrolyte_rack")
        #    pending_positions = electrolyte_rack.get_pending_positions()[elec_bottle_site]
        #    # TODO: 瓶子取液体操作需要加入
#
#
        #    #找到压制工站对应的对象
        #    battery_press_slot = deck.get_resource("battery_press_1")
        #    #创建一个新电池
        #    test_battery = Battery(
        #        name=f"test_battery_{data_coin_num}",
        #        diameter=20.0,  # 与压制槽直径匹配
        #        height=3.0,     # 电池高度
        #        max_volume=100.0,  # 100μL容量
        #        barcode=data_coin_cell_code,  # 电池条码
        #    )
        #    if battery_press_slot.has_battery():
        #        return False, "压制工站已有电池，无法放置新电池"
        #    #在压制位放置电池
        #    battery_press_slot.place_battery(test_battery)
        #    #从第一个子弹夹中取料
        #    clip_magazine_1_hole = self.deck.get_resource("clip_magazine_1").get_item(Pos_elec_site)
        #    clip_magazine_2_hole = self.deck.get_resource("clip_magazine_2").get_item(Al_elec_site)
        #    clip_magazine_3_hole = self.deck.get_resource("clip_magazine_3").get_item(Gasket_site)
        #    
        #    if clip_magazine_1_hole.get_sheet_count() > 0:   # 检查洞位是否有极片
        #        electrode_sheet_1 = clip_magazine_1_hole.take_sheet()  # 从洞位取出极片
        #        test_battery.add_electrode_sheet(electrode_sheet_1)  # 添加到电池中
        #        print(f"已将极片 {electrode_sheet_1.name} 从子弹夹转移到电池")
        #    else:
        #        print("子弹夹洞位0没有极片")
#
        #    if clip_magazine_2_hole.get_sheet_count() > 0:   # 检查洞位是否有极片
        #        electrode_sheet_2 = clip_magazine_2_hole.take_sheet()  # 从洞位取出极片
        #        test_battery.add_electrode_sheet(electrode_sheet_2)  # 添加到电池中
        #        print(f"已将极片 {electrode_sheet_2.name} 从子弹夹转移到电池")
        #    else:
        #        print("子弹夹洞位0没有极片")
#
        #    if clip_magazine_3_hole.get_sheet_count() > 0:   # 检查洞位是否有极片
        #        electrode_sheet_3 = clip_magazine_3_hole.take_sheet()  # 从洞位取出极片
        #        test_battery.add_electrode_sheet(electrode_sheet_3)  # 添加到电池中
        #        print(f"已将极片 {electrode_sheet_3.name} 从子弹夹转移到电池")
        #    else:
        #        print("子弹夹洞位0没有极片")
        #  
        #    # TODO:#把电解液从瓶中取到电池夹子中
        #    battery_site = deck.get_resource("battery_press_1")
        #    clip_magazine_battery = deck.get_resource("clip_magazine_battery")
        #    if battery_site.has_battery():
        #        battery = battery_site.take_battery() #从压制槽取出电池
        #        clip_magazine_battery.add_battery(battery) #从压制槽取出电池
#
#
#
#
        #    # 保存配置到文件
        #    self.deck.save("button_battery_station_layout.json", indent=2)
        #    print("\n台面配置已保存到: button_battery_station_layout.json")
        #
        #    # 保存状态到文件
        #    self.deck.save_state_to_file("button_battery_station_state.json", indent=2)
        #    print("台面状态已保存到: button_battery_station_state.json")






            #将数据写入csv中
            #如当前目录下无同名文件则新建一个csv用于存放数据
            if not os.path.exists(self.csv_export_file):
                #创建一个表头
                with open(self.csv_export_file, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow([
                        'Time', 'open_circuit_voltage', 'pole_weight', 
                        'assembly_time', 'assembly_pressure', 'electrolyte_volume', 
                        'coin_num', 'electrolyte_code', 'coin_cell_code'
                    ])
                    #立刻写入磁盘
                    csvfile.flush()
            #开始追加电池信息
            with open(self.csv_export_file, 'a', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow([
                    timestamp, data_open_circuit_voltage, data_pole_weight,
                    data_assembly_time, data_assembly_pressure, data_electrolyte_volume,
                    data_coin_num, data_electrolyte_code, data_coin_cell_code
                ])
                #立刻写入磁盘
                csvfile.flush()

            # 只要不在自动模式运行中，就将允许标志位置False
            if self.sys_auto_status  == False or self.sys_start_status == False:
                self.allow_data_read = False
                self.csv_export_running = False
            time.sleep(1)

    def func_stop_read_data(self):
        """停止CSV导出"""
        if not self.csv_export_running:
            return False, "read data未在运行"
        
        self.csv_export_running = False
        self.allow_data_read = False
        
        if self.csv_export_thread and self.csv_export_thread.is_alive():
            self.csv_export_thread.join(timeout=5)

    def func_get_csv_export_status(self):
        """获取CSV导出状态"""
        return {
            'allow_read': self.allow_data_read,
            'running': self.csv_export_running,
            'thread_alive': self.csv_export_thread.is_alive() if self.csv_export_thread else False
        }

    
    '''
    # ===================== 物料管理区 ======================
    @property
    def data_material_inventory(self) -> int:
        """主物料库存 (数量, INT16)"""
        inventory, read_err =  self.client.use_node('REG_DATA_MATERIAL_INVENTORY').read(1)
        return inventory

    @property
    def data_tips_inventory(self) -> int:
        """移液枪头库存 (数量, INT16)"""
        inventory, read_err = self.client.register_node_list(self.nodes).use_node('REG_DATA_TIPS_INVENTORY').read(1)
        return inventory
        
    '''



if __name__ == "__main__":
    # 简单测试
    workstation = CoinCellAssemblyWorkstation(deck=CoincellDeck(setup=True, name="coin_cell_deck"))
    # workstation.qiming_coin_cell_code(fujipian_panshu=1, fujipian_juzhendianwei=2, gemopanshu=3, gemo_juzhendianwei=4, lvbodian=False, battery_pressure_mode=False, battery_pressure=4200, battery_clean_ignore=False)
    # print(f"工作站创建成功: {workstation.deck.name}")
    # print(f"料盘数量: {len(workstation.deck.children)}")
    workstation.func_pack_device_init()
    workstation.func_pack_device_auto()
    workstation.func_pack_device_start()
    workstation.func_pack_send_bottle_num(16)
    workstation.func_allpack_cmd(elec_num=16, elec_use_num=16, elec_vol=50, assembly_type=7, assembly_pressure=4200, file_path="/Users/calvincao/Desktop/work/Uni-Lab-OS-hhm")
    