import csv
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional
from pylabrobot.resources import Resource as PLRResource
from unilabos_msgs.msg import Resource
from unilabos.device_comms.modbus_plc.client import ModbusTcpClient
from unilabos.devices.workstation.coin_cell_assembly.button_battery_station import MaterialHole, MaterialPlate
from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.device_comms.modbus_plc.client import TCPClient, ModbusNode, PLCWorkflow, ModbusWorkflow, WorkflowAction, BaseClient
from unilabos.device_comms.modbus_plc.modbus import DeviceType, Base as ModbusNodeBase, DataType, WorderOrder
from unilabos.devices.workstation.coin_cell_assembly.button_battery_station import *
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode, BaseROS2DeviceNode
from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode

#构建物料系统

class CoinCellAssemblyWorkstation(WorkstationBase):
    def __init__(
        self,
        deck: CoincellDeck,
        address: str = "192.168.1.20",
        port: str = "502",
        debug_mode: bool = True,
        *args,
        **kwargs,
    ):
        super().__init__(
            #桌子
            deck=deck,
            *args,
            **kwargs,
        )
        self.debug_mode = debug_mode
        self.deck = deck
        """ 连接初始化 """
        modbus_client = TCPClient(addr=address, port=port)
        print("modbus_client", modbus_client)
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
        else:
            print("测试模式，跳过连接")

        """ 工站的配置 """
        self.nodes = BaseClient.load_csv(os.path.join(os.path.dirname(__file__), 'coin_cell_assembly_a.csv'))
        self.client  = modbus_client.register_node_list(self.nodes)
        self.success = False
        self.allow_data_read = False  #允许读取函数运行标志位
        self.csv_export_thread = None
        self.csv_export_running = False
        self.csv_export_file = None
        #创建一个物料台面，包含两个极片板
        #self.deck = create_a_coin_cell_deck()
        
        #self._ros_node.update_resource(self.deck)
        
        #ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
        #    "resources": [self.deck]
        #})

    
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
        time, read_err =  self.client.use_node('REG_DATA_ASSEMBLY_PER_TIME').read(2, word_order=WorderOrder.LITTLE)
        return time

    @property
    def data_open_circuit_voltage(self) -> float:
        """开路电压值 (FLOAT32)"""
        if self.debug_mode:
            return 0
        vol, read_err =  self.client.use_node('REG_DATA_OPEN_CIRCUIT_VOLTAGE').read(2, word_order=WorderOrder.LITTLE)
        return vol

    @property
    def data_axis_x_pos(self) -> float:
        """分液X轴当前位置 (FLOAT32)"""
        if self.debug_mode:
            return 0
        pos, read_err =  self.client.use_node('REG_DATA_AXIS_X_POS').read(2, word_order=WorderOrder.LITTLE)
        return pos

    @property
    def data_axis_y_pos(self) -> float:
        """分液Y轴当前位置 (FLOAT32)"""
        if self.debug_mode:
            return 0
        pos, read_err =  self.client.use_node('REG_DATA_AXIS_Y_POS').read(2, word_order=WorderOrder.LITTLE)
        return pos

    @property
    def data_axis_z_pos(self) -> float:
        """分液Z轴当前位置 (FLOAT32)"""
        if self.debug_mode:
            return 0
        pos, read_err =  self.client.use_node('REG_DATA_AXIS_Z_POS').read(2, word_order=WorderOrder.LITTLE)
        return pos

    @property
    def data_pole_weight(self) -> float:
        """当前电池正极片称重数据 (FLOAT32)"""
        if self.debug_mode:
            return 0
        weight, read_err =  self.client.use_node('REG_DATA_POLE_WEIGHT').read(2, word_order=WorderOrder.LITTLE)
        return weight

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
            # 尝试不同的字节序读取
            code_little, read_err = self.client.use_node('REG_DATA_COIN_CELL_CODE').read(10, word_order=WorderOrder.LITTLE)
            print(code_little)
            clean_code = code_little[-8:][::-1]
            return clean_code
        except Exception as e:
            print(f"读取电池二维码失败: {e}")
            return "N/A"


    @property
    def data_electrolyte_code(self) -> str:
        try:
            # 尝试不同的字节序读取
            code_little, read_err = self.client.use_node('REG_DATA_ELECTROLYTE_CODE').read(10, word_order=WorderOrder.LITTLE)
            print(code_little)
            clean_code = code_little[-8:][::-1]
            return clean_code
        except Exception as e:
            print(f"读取电解液二维码失败: {e}")
            return "N/A"

    # ===================== 环境监控区 ======================
    @property
    def data_glove_box_pressure(self) -> float:
        """手套箱压力 (bar, FLOAT32)"""
        if self.debug_mode:
            return 0
        status, read_err = self.client.use_node('REG_DATA_GLOVE_BOX_PRESSURE').read(2, word_order=WorderOrder.LITTLE)
        return status

    @property
    def data_glove_box_o2_content(self) -> float:
        """手套箱氧含量 (ppm, FLOAT32)"""
        if self.debug_mode:
            return 0
        value, read_err = self.client.use_node('REG_DATA_GLOVE_BOX_O2_CONTENT').read(2, word_order=WorderOrder.LITTLE)
        return value

    @property
    def data_glove_box_water_content(self) -> float:
        """手套箱水含量 (ppm, FLOAT32)"""
        if self.debug_mode:
            return 0
        value, read_err = self.client.use_node('REG_DATA_GLOVE_BOX_WATER_CONTENT').read(2, word_order=WorderOrder.LITTLE)
        return value

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

    def func_pack_send_bottle_num(self, bottle_num: int):
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

    def func_pack_send_msg_cmd(self, elec_use_num) -> bool:
        """UNILAB写参数"""    
        while (self.request_rec_msg_status) == False: 
            print("wait for request_rec_msg_status to True")
            time.sleep(1)
        self.success = False
        #self._unilab_send_msg_electrolyte_num(elec_num)
        time.sleep(1)
        self._unilab_send_msg_electrolyte_use_num(elec_use_num)
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
        data_open_circuit_voltage = self.data_open_circuit_voltage
        data_pole_weight = self.data_pole_weight
        data_assembly_time = self.data_assembly_time
        data_assembly_pressure = self.data_assembly_pressure
        data_electrolyte_volume = self.data_electrolyte_volume
        data_coin_num = self.data_coin_num
        data_electrolyte_code = self.data_electrolyte_code
        data_coin_cell_code = self.data_coin_cell_code
        print("data_open_circuit_voltage", data_open_circuit_voltage)
        print("data_pole_weight", data_pole_weight)
        print("data_assembly_time", data_assembly_time)
        print("data_assembly_pressure", data_assembly_pressure)
        print("data_electrolyte_volume", data_electrolyte_volume)
        print("data_coin_num", data_coin_num)
        print("data_electrolyte_code", data_electrolyte_code)
        print("data_coin_cell_code", data_coin_cell_code)
        #接收完信息后，读取完毕标志位置True
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



    def func_allpack_cmd(self, elec_num, elec_use_num, file_path: str="D:\\coin_cell_data") -> bool:
        summary_csv_file = os.path.join(file_path, "duandian.csv")
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
                        return False
                    print(f"从断点文件读取进度: elec_num_N={elec_num_N}, elec_use_num_N={elec_use_num_N}, coin_num_N={coin_num_N}")
                     
        else:
            read_status_flag = False
            print("未找到断点文件，从头开始")
            elec_num_N = 0
            elec_use_num_N = 0
            coin_num_N = 0

        print(f"剩余电解液瓶数: {elec_num}, 已组装电池数: {elec_use_num}")

        
        #如果是第一次运行，则进行初始化、切换自动、启动, 如果是断点重启则跳过。
        if read_status_flag == False:
            #初始化
            self.func_pack_device_init()
            #切换自动
            self.func_pack_device_auto()
            #启动，小车收回
            self.func_pack_device_start()
            #发送电解液瓶数量，启动搬运,多搬运没事
            self.func_pack_send_bottle_num(elec_num)
        last_i = elec_num_N
        last_j = elec_use_num_N
        for i in range(last_i, elec_num):
            print(f"开始第{last_i+i+1}瓶电解液的组装")
            #第一个循环从上次断点继续，后续循环从0开始
            j_start = last_j if i == last_i else 0
            self.func_pack_send_msg_cmd(elec_use_num-j_start)

            for j in range(j_start, elec_use_num):
                print(f"开始第{last_i+i+1}瓶电解液的第{j+j_start+1}个电池组装")
                #读取电池组装数据并存入csv
                self.func_pack_get_msg_cmd(file_path)
                time.sleep(1)

                #这里定义物料系统
                # TODO:读完再将电池数加一还是进入循环就将电池数加一需要考虑
                liaopan1 = self.deck.get_resource("liaopan1")
                liaopan4 = self.deck.get_resource("liaopan4")
                jipian1 = liaopan1.children[coin_num_N].children[0]
                jipian4 = liaopan4.children[coin_num_N].children[0]
                #print(jipian1)
                #从料盘上去物料解绑后放到另一盘上
                jipian1.parent.unassign_child_resource(jipian1)
                jipian4.parent.unassign_child_resource(jipian4)
                
                #print(jipian2.parent)
                battery = Battery(name = f"battery_{coin_num_N}")
                battery.assign_child_resource(jipian1, location=None)
                battery.assign_child_resource(jipian4, location=None)
                
                zidanjia6 = self.deck.get_resource("zi_dan_jia6")

                zidanjia6.children[0].assign_child_resource(battery, location=None)
               

                # 生成断点文件
                # 生成包含elec_num_N、coin_num_N、timestamp的CSV文件
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                with open(summary_csv_file, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(['elec_num','elec_use_num', 'elec_num_N', 'elec_use_num_N', 'coin_num_N', 'timestamp'])
                    writer.writerow([elec_num, elec_use_num, elec_num_N, elec_use_num_N, coin_num_N, timestamp])
                    csvfile.flush()
                coin_num_N += 1
                elec_use_num_N += 1
            elec_num_N += 1
            elec_use_num_N = 0

        #循环正常结束，则删除断点文件
        os.remove(summary_csv_file)
        #全部完成后等待依华发送完成信号
        self.func_pack_send_finished_cmd()


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
        #liaopan1 = self.deck.get_resource("liaopan1")
        #liaopan4 = self.deck.get_resource("liaopan4")
        #for coin_num_N in range(16):
        #    liaopan1 = self.deck.get_resource("liaopan1")
        #    liaopan4 = self.deck.get_resource("liaopan4")
        #    jipian1 = liaopan1.children[coin_num_N].children[0]
        #    jipian4 = liaopan4.children[coin_num_N].children[0]
        #    #print(jipian1)
        #    #从料盘上去物料解绑后放到另一盘上
        #    jipian1.parent.unassign_child_resource(jipian1)
        #    jipian4.parent.unassign_child_resource(jipian4)
        #    
        #    #print(jipian2.parent)
        #    battery = Battery(name = f"battery_{coin_num_N}")
        #    battery.assign_child_resource(jipian1, location=None)
        #    battery.assign_child_resource(jipian4, location=None)
        #    
        #    zidanjia6 = self.deck.get_resource("zi_dan_jia6")
        #    zidanjia6.children[0].assign_child_resource(battery, location=None)
        #    ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
        #        "resources": [self.deck]
        #    })
        #    time.sleep(2)
        for i in range(20):
            print(f"输出{i}")
            time.sleep(2)

            
    # 数据读取与输出
    def func_read_data_and_output(self, file_path: str="D:\\coin_cell_data"):
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
        #    #把电解液从瓶中取到电池夹子中
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
    from pylabrobot.resources import Resource
    Coin_Cell = CoinCellAssemblyWorkstation(Resource("1", 1, 1, 1), debug_mode=True)
    #Coin_Cell.func_pack_device_init()  
    #Coin_Cell.func_pack_device_auto()
    #Coin_Cell.func_pack_device_start()
    #Coin_Cell.func_pack_send_bottle_num(2)
    #Coin_Cell.func_pack_send_msg_cmd(2)
    #Coin_Cell.func_pack_get_msg_cmd()
    #Coin_Cell.func_pack_get_msg_cmd()
    #Coin_Cell.func_pack_send_finished_cmd()
#
    #Coin_Cell.func_allpack_cmd(3, 2)
    #print(Coin_Cell.data_stack_vision_code)
    #print("success")
    #创建一个物料台面

    #deck = create_a_coin_cell_deck()

    ##在台面上找到料盘和极片
    #liaopan1 = deck.get_resource("liaopan1")
    #liaopan2 = deck.get_resource("liaopan2")
    #jipian1 = liaopan1.children[1].children[0]
#
    ##print(jipian1)
    ##把物料解绑后放到另一盘上
    #jipian1.parent.unassign_child_resource(jipian1)
    #liaopan2.children[1].assign_child_resource(jipian1, location=None)
    ##print(jipian2.parent)
    from unilabos.resources.graphio import resource_ulab_to_plr, convert_resources_to_type

    with open("./button_battery_decks_unilab.json", "r", encoding="utf-8") as f:
        bioyond_resources_unilab = json.load(f)
    print(f"成功读取 JSON 文件，包含 {len(bioyond_resources_unilab)} 个资源")
    ulab_resources = convert_resources_to_type(bioyond_resources_unilab, List[PLRResource])
    print(f"转换结果类型: {type(ulab_resources)}")
    print(ulab_resources)

