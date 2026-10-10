import csv
import inspect
import json
import os
import threading
import time
import types
import urllib.error
import urllib.request
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
from unilabos.devices.workstation.coin_cell_assembly.YB_YH_materials import (
    YihuaCoinCellDeck,
    yihua_coin_cell_deck,
    COIN_CELL_STOCK_PROPERTIES,
    apply_coin_cell_stock_to_deck,
)
from unilabos.resources.graphio import convert_resources_to_type
from unilabos.utils.log import logger
import struct


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
        # Word Order: Little - 交换两个寄存器的顺序
        # Byte Order: Big - 每个寄存器内部使用大端字节序
        low_word = registers[0]
        high_word = registers[1]
        
        # 将两个16位寄存器组合成一个32位值 (Little Word Order)
        # 使用大端字节序 ('>') 将每个寄存器转换为字节
        byte_string = struct.pack('>HH', high_word, low_word)
        
        # 将字节字符串解码为浮点数 (大端)
        value = struct.unpack('>f', byte_string)[0]
        
        return value
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

# 旁观者采集：组装记录 CSV 表头（与 func_pack_get_msg_cmd 的表头一致，去掉配方相关列）
_OBSERVER_ASSEMBLY_HEADER = [
    'Time', 'open_circuit_voltage', 'pole_weight',
    'assembly_time', 'target_assembly_pressure', 'real_assembly_pressure', 'electrolyte_volume',
    'data_coin_type', 'electrolyte_code', 'coin_cell_code',
]

# 旁观者采集：Neware 提交 CSV 表头，必须与 submit_from_csv_export_ndax 校验的列名逐字一致
_OBSERVER_NEWARE_HEADER = [
    'coin_cell_code', 'electrolyte_code', '电池体系', '设备号', '排号', '通道号',
    'pole_weight', '集流体质量', '活性物质含量', '克容量mah/g',
]


def _observer_cast_float(value) -> float:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else 0.0
    return float(value)


def _observer_cast_int(value) -> int:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else 0
    return int(float(value))


def _observer_cast_code(value) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip() or "N/A"


def _log_coin_cell_record(
    mode: str,
    record_index: int,
    device_count: int,
    snapshot: Dict[str, Any],
    csv_file: str,
) -> None:
    """按统一格式记录一颗已成功写入 CSV 的扣电数据。"""
    def _cast_or_default(caster, value, default):
        try:
            return caster(value)
        except Exception:
            return default

    payload = {
        "mode": mode,
        "record_index": _cast_or_default(_observer_cast_int, record_index, 0),
        "device_count": _cast_or_default(_observer_cast_int, device_count, 0),
        "time": str(snapshot.get("time", snapshot.get("Time", ""))),
        "coin_cell_code": _cast_or_default(
            _observer_cast_code, snapshot.get("coin_cell_code"), "N/A"
        ),
        "electrolyte_code": _cast_or_default(
            _observer_cast_code, snapshot.get("electrolyte_code"), "N/A"
        ),
        "open_circuit_voltage": _cast_or_default(
            _observer_cast_float, snapshot.get("open_circuit_voltage"), 0.0
        ),
        "pole_weight": _cast_or_default(
            _observer_cast_float, snapshot.get("pole_weight"), 0.0
        ),
        "assembly_time": _cast_or_default(
            _observer_cast_float, snapshot.get("assembly_time"), 0.0
        ),
        "target_assembly_pressure": _cast_or_default(
            _observer_cast_int, snapshot.get("target_assembly_pressure"), 0
        ),
        "real_assembly_pressure": _cast_or_default(
            _observer_cast_int, snapshot.get("real_assembly_pressure"), 0
        ),
        "electrolyte_volume": _cast_or_default(
            _observer_cast_int, snapshot.get("electrolyte_volume"), 0
        ),
        "data_coin_type": _cast_or_default(
            _observer_cast_int, snapshot.get("data_coin_type"), 0
        ),
        "csv_file": str(csv_file),
    }
    logger.info(
        f"[扣电完成][mode={mode}] "
        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


class CoinCellAssemblyWorkstation(WorkstationBase):
    # ===================== DUCO 机械臂状态采集配置 ======================
    # 现场机 172.16.28.102 上常驻的 HTTP 服务已采集新松 DUCO 六轴协作机器人状态，本类只读不写。
    # 采集周期与 ROS 发布周期统一为同一常量，避免"发布快于采集导致重复发同一帧"。
    _DUCO_STATE_URL = "http://172.16.28.102:8000/api/state"
    _DUCO_PERIOD_S = 0.5  # 后台轮询间隔 = ROS topic 发布周期（秒）
    _DUCO_HTTP_TIMEOUT_S = 1.5  # 单次 HTTP 请求超时（秒），与周期独立
    _DUCO_MAX_FAIL = 3  # 连续失败次数达到该阈值后丢弃缓存（置 None）

    def __init__(self, 
        config: dict = None, 
        deck=None, 
        address: str = "172.16.28.102",
        port: str = "502",
        debug_mode: bool = False,
        stock_refresh_interval: float = 10.0,
        *args,
        **kwargs):

        if deck is None and config:
            deck = config.get('deck')
        if deck is None:
            logger.info("没有传入依华deck，检查启动json文件")
        super().__init__(deck=deck, *args, **kwargs,)
        self.debug_mode = debug_mode
        self._modbus_address = address
        self._modbus_port = port

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
            self._modbus_client_raw = modbus_client
        else:
            print("测试模式，跳过连接")
            self.nodes, self.client = None, None
            self._modbus_client_raw = None

        """ 工站的配置 """

        self.success = False
        self.allow_data_read = False  #允许读取函数运行标志位
        self.csv_export_thread = None
        self.csv_export_running = False
        self.csv_export_file = None
        self.coin_num_N = 0  #已组装电池数量
        self._elec_use_num = 0  # 每瓶电池数（下单后由 coin_cell_start 更新）
        self._elec_bottle_num = 0  # 电解液瓶数（下单后由 coin_cell_start 更新）
        self._observer_running = False  # 旁观者采集运行标志（与 csv_export_running 分开，避免互相干扰）

        """ 物料余量采集（把 PLC 余量寄存器写到 deck 弹夹上供前端展示） """
        try:
            self._stock_refresh_interval = float(stock_refresh_interval or 0)
        except (TypeError, ValueError):
            logger.warning(f"stock_refresh_interval={stock_refresh_interval!r} 非法，按 0（不刷新）处理")
            self._stock_refresh_interval = 0.0
        self._stock_last_readings: Optional[Dict[str, float]] = None  # 上一轮读数，用于抑制无变化推送
        self._stock_thread = None

        """ DUCO 机械臂状态采集（独立于 Modbus 业务，debug_mode 下同样轮询以便本地验证） """
        self._duco_lock = threading.Lock()  # 保护 _duco_state 的读写
        self._duco_state = None  # 最近一帧 /api/state 原始 JSON，无有效数据时为 None
        self._duco_fail_count = 0  # 连续请求失败次数
        self._duco_thread = threading.Thread(
            target=self._duco_poll_loop, daemon=True, name="duco-state-poller"
        )
        self._duco_thread.start()

    # ===================== DUCO 机械臂状态采集 ======================

    def _duco_poll_loop(self) -> None:
        """后台守护线程：定期拉取 102 上的 DUCO 状态并写入本地缓存。

        任何网络/解析异常只记 warning 并保留上一次缓存，绝不向外抛出，
        以免影响工站主流程（Modbus 业务）。连续失败达到 _DUCO_MAX_FAIL 次后
        认为数据已不可信，将缓存置 None。
        """
        while True:
            try:
                request = urllib.request.Request(self._DUCO_STATE_URL, method="GET")
                with urllib.request.urlopen(request, timeout=self._DUCO_HTTP_TIMEOUT_S) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError(f"返回体不是 JSON 对象: {type(payload).__name__}")
                with self._duco_lock:
                    self._duco_state = payload
                    self._duco_fail_count = 0
            except Exception as e:
                with self._duco_lock:
                    self._duco_fail_count += 1
                    fail_count = self._duco_fail_count
                    # 连续失败超过阈值则丢弃陈旧缓存，避免对外发布过期位置
                    if fail_count >= self._DUCO_MAX_FAIL:
                        self._duco_state = None
                logger.warning(f"[DUCO] 读取机械臂状态失败（连续第 {fail_count} 次）: {e}")
            time.sleep(self._DUCO_PERIOD_S)

    def _duco_snapshot(self) -> Optional[Dict[str, Any]]:
        """取一份 DUCO 状态缓存的浅拷贝（加锁），无有效数据时返回 None"""
        with self._duco_lock:
            if self._duco_state is None:
                return None
            return dict(self._duco_state)

    def _ensure_modbus_connected(self) -> None:
        """检查 Modbus TCP 连接是否存活，若已断开则自动重连（防止长时间空闲后连接超时）"""
        if self.debug_mode or self._modbus_client_raw is None:
            return
        raw_client = self._modbus_client_raw.client
        if raw_client.is_socket_open():
            return
        logger.warning("[Modbus] 检测到连接已断开，尝试重连...")
        try:
            raw_client.close()
        except Exception:
            pass
        count = 10
        while count > 0:
            count -= 1
            try:
                raw_client.connect()
            except Exception:
                pass
            if raw_client.is_socket_open():
                break
            time.sleep(2)
        if not raw_client.is_socket_open():
            raise RuntimeError(f"Modbus TCP 重连失败（{self._modbus_address}:{self._modbus_port}），请检查设备连接")
        logger.info("[Modbus] 重连成功")

    # ===================== 物料余量采集 ======================

    def _stock_bus_ready(self) -> bool:
        """连接断开时读寄存器一律返回 0，会被当成「余量耗尽」推到前端，故先确认链路可用。"""
        if self.debug_mode:
            return True
        raw_client = getattr(self._modbus_client_raw, "client", None)
        return bool(raw_client is not None and raw_client.is_socket_open())

    def _read_stock_readings(self) -> Dict[str, float]:
        """取一轮余量读数。直接复用已有的寄存器属性，不另开一路 Modbus 访问。"""
        return {prop: float(getattr(self, prop)) for prop in COIN_CELL_STOCK_PROPERTIES}

    def _apply_stock_to_deck(self) -> Optional[Dict[str, float]]:
        """读一轮余量写进各弹夹，返回本轮读数；deck 或 Modbus 链路不可用时返回 None。"""
        if self.deck is None or not self._stock_bus_ready():
            return None
        readings = self._read_stock_readings()
        failed, dropped = apply_coin_cell_stock_to_deck(self.deck, readings)
        if failed:
            logger.warning(f"[物料余量] 台面上未找到弹夹，已跳过: {'、'.join(failed)}")
        if dropped:
            detail = "、".join(f"{k}={v:g}" for k, v in dropped.items())
            logger.warning(
                f"[物料余量] 以下计数为零或负，多半是上料时未录初值、计数器已减穿零"
                f"（负值绝对值即上次复位以来的消耗量）: {detail}"
            )
        return readings

    def _refresh_stock_to_deck(self) -> None:
        """周期刷新：读数有变化才推 deck，避免每轮都上传整棵资源树。"""
        readings = self._apply_stock_to_deck()
        if readings is None or readings == self._stock_last_readings:
            return
        self._stock_last_readings = readings
        if getattr(self, "_ros_node", None) is None:
            return
        ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
            "resources": [self.deck]
        })
        logger.info(f"[物料余量] 读数变化，已推送 deck: {json.dumps(readings, ensure_ascii=False)}")

    def _stock_poll_loop(self) -> None:
        """后台守护线程：定期把 PLC 余量寄存器同步到 deck。异常只记 warning，不影响主流程。"""
        while True:
            time.sleep(self._stock_refresh_interval)
            try:
                self._refresh_stock_to_deck()
            except Exception as e:
                logger.warning(f"[物料余量] 周期刷新异常: {e}")

    def post_init(self, ros_node: ROS2WorkstationNode):
        self._ros_node = ros_node

        # Deck 为空时（反序列化未恢复子节点），主动调用 setup() 初始化子物料
        if self.deck and not self.deck.children and hasattr(self.deck, "setup") and callable(self.deck.setup):
            logger.info("YihuaCoinCellDeck 无子节点，调用 setup() 初始化")
            self.deck.setup()

        # 先同步刷一轮余量，让下面这次 deck 上传就带上弹夹余量
        try:
            self._stock_last_readings = self._apply_stock_to_deck()
        except Exception as e:
            logger.warning(f"[物料余量] 首轮采集失败，待周期刷新重试: {e}")

        ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
            "resources": [self.deck]
        })

        if self._stock_refresh_interval > 0:
            self._stock_thread = threading.Thread(
                target=self._stock_poll_loop, daemon=True, name="coin-cell-stock-poller"
            )
            self._stock_thread.start()
            logger.info(f"[物料余量] 采集线程已启动，间隔 {self._stock_refresh_interval}s")
        else:
            logger.info("[物料余量] stock_refresh_interval=0，不启动周期采集")

        # DUCO 机械臂位置：把发布周期从框架默认 5 秒调到与 HTTP 轮询一致的 _DUCO_PERIOD_S
        for name in ("duco_joint_positions", "duco_tcp_pose"):
            pub = ros_node._property_publishers.get(name)
            if pub is not None:
                pub.change_frequency(self._DUCO_PERIOD_S)
            else:
                logger.warning(f"[DUCO] 未找到属性发布者 {name}，请检查注册表 status_types 是否已声明")

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

    # ================== DUCO 机械臂位置（只读 HTTP 缓存） ==================
    # 返回类型必须是 str：Host 状态回调只接受标量，list/dict 会被框架强转为 String，语义不可靠。

    @property
    def duco_joint_positions(self) -> str:
        """DUCO 六轴实际关节角，JSON 字符串，单位 rad；无数据时返回 "{}" """
        state = self._duco_snapshot()
        if state is None:
            return "{}"
        joints = state.get("joint_actual_position")
        if not isinstance(joints, list) or len(joints) != 6:
            return "{}"
        return json.dumps({"joints_rad": [float(v) for v in joints], "ts": state.get("timestamp")})

    @property
    def duco_tcp_pose(self) -> str:
        """DUCO TCP 实际位姿，JSON 字符串，位置单位 m、姿态单位 rad；无数据时返回 "{}" """
        state = self._duco_snapshot()
        if state is None:
            return "{}"
        pose = state.get("tcp_pose")
        if not isinstance(pose, list) or len(pose) != 6:
            return "{}"
        x, y, z, rx, ry, rz = (float(v) for v in pose)
        return json.dumps({
            "x": x, "y": y, "z": z,
            "rx": rx, "ry": ry, "rz": rz,
            "ts": state.get("timestamp"),
        })

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
    def data_target_assembly_pressure(self) -> int:
        """设备侧设定的目标压制力 MW490 (INT16)，旁观模式下压制力非 Uni-Lab 下发，只能从此寄存器读"""
        if self.debug_mode:
            return 0
        try:
            pressure, read_err = self.client.use_node('REG_DATA_TARGET_ASSEMBLY_PRESSURE').read(1)
            return pressure or 0
        except Exception as e:
            logger.warning(f"读取目标压制力失败，返回 0: {e}")
            return 0

    @property
    def data_electrolyte_volume(self) -> int:
        """当前电解液加注量 (INT16)"""
        if self.debug_mode:
            return 0
        try:
            vol, read_err = self.client.use_node('REG_DATA_ELECTROLYTE_VOLUME').read(1)
            return vol or 0
        except Exception as e:
            logger.warning(f"读取电解液加注量失败，返回 0: {e}")
            return 0

    @property
    def data_coin_type(self) -> int:
        """电池类型 - 7种或8种组装物料 (INT16)"""
        if self.debug_mode:
            return 7
        coin_type, read_err = self.client.use_node('REG_DATA_COIN_TYPE').read(1)
        return coin_type
    
    @property
    def data_current_assembling_count(self) -> int:
        """当前进行组装的电池数量 - Current assembling battery count (INT16)"""
        if self.debug_mode:
            return 0
        count, read_err = self.client.use_node('REG_DATA_CURRENT_ASSEMBLING_COUNT').read(1)
        return count
    
    @property
    def data_current_completed_count(self) -> int:
        """当前完成组装的电池数量 - Current completed battery count (INT16)"""
        if self.debug_mode:
            return 0
        count, read_err = self.client.use_node('REG_DATA_CURRENT_COMPLETED_COUNT').read(1)
        return count

    @property
    def data_order_completion_percentage(self) -> float:
        """电池订单任务完成百分比 (%)

        分子: 当前完成组装的电池数量 (data_current_completed_count)
        分母: 电池总数 = 电解液瓶数 (REG_MSG_ELECTROLYTE_NUM)
              × 每瓶电池数 (REG_MSG_ELECTROLYTE_USE_NUM)
        订单未下发（分母为 0）时返回 0.0
        """
        if self.debug_mode:
            return 0.0
        try:
            completed = self.data_current_completed_count
            bottle_num, _ = self.client.use_node('REG_MSG_ELECTROLYTE_NUM').read(1)
            use_num, _ = self.client.use_node('REG_MSG_ELECTROLYTE_USE_NUM').read(1)
            total = (bottle_num or 0) * (use_num or 0)
            if total <= 0:
                return 0.0
            return round((completed or 0) / total * 100.0, 2)
        except Exception as e:
            logger.warning(f"读取订单完成进度失败，返回 0.0: {e}")
            return 0.0

    @property
    def data_coin_cell_code(self) -> str:
        """电池二维码序列号 (STRING)"""
        try:
            # 读取 STRING 类型数据
            code_little, read_err = self.client.use_node('REG_DATA_COIN_CELL_CODE').read(10, word_order=WorderOrder.LITTLE)
            
            # PyModbus 3.x 返回 string 类型
            if not isinstance(code_little, str):
                logger.warning(f"电池二维码返回的类型不支持: {type(code_little)}, 值: {repr(code_little)}")
                return "N/A"
            
            # 从字符串末尾查找连续的字母数字字符（反转字符串）
            import re
            reversed_str = code_little[::-1]
            match = re.match(r'^([A-Za-z0-9]+)', reversed_str)
            
            if not match:
                logger.warning(f"未找到有效的电池二维码数据，原始字符串: {repr(code_little)}")
                return "N/A"
            
            # 提取匹配到的字符串（已经是正确顺序）
            decoded = match.group(1)[:8]  # 只取前8个字符
            
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
            
            # PyModbus 3.x 返回 string 类型
            if not isinstance(code_little, str):
                logger.warning(f"电解液二维码返回的类型不支持: {type(code_little)}, 值: {repr(code_little)}")
                return "N/A"
            
            # 从字符串末尾查找连续的字母数字字符（反转字符串）
            import re
            reversed_str = code_little[::-1]
            match = re.match(r'^([A-Za-z0-9]+)', reversed_str)
            
            if not match:
                logger.warning(f"未找到有效的电解液二维码数据，原始字符串: {repr(code_little)}")
                return "N/A"
            
            # 提取匹配到的字符串（已经是正确顺序）
            decoded = match.group(1)[:8]  # 只取前8个字符
            
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

    @property
    def data_10mm_positive_plate_remaining(self) -> float:
        """10mm正极片剩余物料数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_10MM_POSITIVE_PLATE_REMAINING_COUNT').address, count=2)
        if result.isError():
            logger.error("读取10mm正极片余量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_12mm_positive_plate_remaining(self) -> float:
        """12mm正极片剩余物料数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_12MM_POSITIVE_PLATE_REMAINING_COUNT').address, count=2)
        if result.isError():
            logger.error("读取12mm正极片余量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_16mm_positive_plate_remaining(self) -> float:
        """16mm正极片剩余物料数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_16MM_POSITIVE_PLATE_REMAINING_COUNT').address, count=2)
        if result.isError():
            logger.error("读取16mm正极片余量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_aluminum_foil_remaining(self) -> float:
        """铝箔剩余物料数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_ALUMINUM_FOIL_REMAINING_COUNT').address, count=2)
        if result.isError():
            logger.error("读取铝箔余量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_positive_shell_remaining(self) -> float:
        """正极壳剩余物料数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_POSITIVE_SHELL_REMAINING_COUNT').address, count=2)
        if result.isError():
            logger.error("读取正极壳余量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_flat_washer_remaining(self) -> float:
        """平垫剩余物料数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_FLAT_WASHER_REMAINING_COUNT').address, count=2)
        if result.isError():
            logger.error("读取平垫余量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_negative_shell_remaining(self) -> float:
        """负极壳剩余物料数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_NEGATIVE_SHELL_REMAINING_COUNT').address, count=2)
        if result.isError():
            logger.error("读取负极壳余量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_spring_washer_remaining(self) -> float:
        """弹垫剩余物料数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_SPRING_WASHER_REMAINING_COUNT').address, count=2)
        if result.isError():
            logger.error("读取弹垫余量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_finished_battery_remaining_capacity(self) -> float:
        """成品电池剩余可容纳数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_FINISHED_BATTERY_REMAINING_CAPACITY').address, count=2)
        if result.isError():
            logger.error("读取成品电池余量失败")
            return 0.0
        return _decode_float32_correct(result.registers)

    @property
    def data_finished_battery_ng_remaining_capacity(self) -> float:
        """成品电池NG槽剩余可容纳数量 (FLOAT32)"""
        if self.debug_mode:
            return 0.0
        result = self.client.client.read_holding_registers(address=self.client.use_node('REG_DATA_FINISHED_BATTERY_NG_REMAINING_CAPACITY').address, count=2)
        if result.isError():
            logger.error("读取成品电池NG槽余量失败")
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

    def _handle_material_search_dialog(self, enable_search: bool, timeout: int = 30) -> None:
        """处理物料搜寻确认弹窗
        
        监测弹窗是否出现，并根据参数自动点击"是"或"否"按钮（脉冲模式）
        
        Args:
            enable_search: True=点击"是"启用物料搜寻, False=点击"否"不启用物料搜寻
            timeout: 等待弹窗出现的最大时间（秒），默认30秒
            
        Raises:
            RuntimeError: 超时未检测到弹窗，或操作失败
        """
        logger.info(f"开始监测物料搜寻确认弹窗（超时: {timeout}秒）...")
        logger.info(f"用户选择: {'启用物料搜寻（点击是）' if enable_search else '不启用物料搜寻（点击否）'}")
        
        start_time = time.time()
        dialog_appeared = False
        
        # 步骤1: 监测弹窗是否出现
        while time.time() - start_time < timeout:
            try:
                dialog_node = self.client.use_node('COIL_MATERIAL_SEARCH_DIALOG_APPEAR')
                dialog_state, read_err = dialog_node.read(1)
                
                if read_err:
                    logger.warning("读取弹窗状态时出错，继续监测...")
                    time.sleep(0.5)
                    continue
                
                # 提取实际值
                if isinstance(dialog_state, (list, tuple)):
                    dialog_actual = dialog_state[0] if len(dialog_state) > 0 else False
                else:
                    dialog_actual = dialog_state
                
                if dialog_actual:
                    logger.info("✓ 检测到物料搜寻确认弹窗出现！")
                    dialog_appeared = True
                    break
                    
            except Exception as e:
                logger.warning(f"读取弹窗状态异常: {e}，继续监测...")
            
            time.sleep(0.5)  # 每0.5秒检查一次
        
        if not dialog_appeared:
            error_msg = f"❌ 超时未检测到物料搜寻确认弹窗（等待了 {timeout} 秒）"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        
        # 步骤2: 执行脉冲按钮点击
        button_name = "是" if enable_search else "否"
        coil_name = "COIL_MATERIAL_SEARCH_CONFIRM_YES" if enable_search else "COIL_MATERIAL_SEARCH_CONFIRM_NO"
        
        logger.info(f"执行脉冲按钮点击: '{button_name}'")
        
        try:
            button_node = self.client.use_node(coil_name)
            
            # 读取初始状态
            initial_state, _ = button_node.read(1)
            logger.debug(f"按钮'{button_name}'初始状态: {initial_state}")
            
            # 脉冲步骤1: 设置为 True
            logger.info(f"  → 按下按钮 '{button_name}' (设置为 True)")
            button_node.write(True)
            time.sleep(0.5)  # 保持0.5秒
            
            # 验证已按下
            pressed_state, _ = button_node.read(1)
            if isinstance(pressed_state, (list, tuple)):
                pressed_actual = pressed_state[0] if len(pressed_state) > 0 else False
            else:
                pressed_actual = pressed_state
            
            if pressed_actual:
                logger.info(f"  ✓ 按钮 '{button_name}' 已按下")
            else:
                logger.warning(f"  ⚠ 按钮 '{button_name}' 状态未变为 True，当前值: {pressed_actual}")
            
            # 脉冲步骤2: 释放按钮 (设置为 False)
            logger.info(f"  → 释放按钮 '{button_name}' (设置为 False)")
            button_node.write(False)
            time.sleep(0.3)
            
            # 验证已释放
            released_state, _ = button_node.read(1)
            if isinstance(released_state, (list, tuple)):
                released_actual = released_state[0] if len(released_state) > 0 else False
            else:
                released_actual = released_state
            
            if not released_actual:
                logger.info(f"  ✓ 按钮 '{button_name}' 已释放（脉冲完成）")
            else:
                logger.warning(f"  ⚠ 按钮 '{button_name}' 未正确释放，当前值: {released_actual}")
            
            logger.info(f"✓ 成功处理物料搜寻确认弹窗（选择: {button_name}）")
            
        except Exception as e:
            error_msg = f"❌ 执行按钮'{button_name}'脉冲操作时失败: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)


    # 长时延模拟的推荐档位（对应测试方案要跑的三种时长），不是硬约束
    SIM_DELAY_PRESETS = (3, 5, 10)
    # 上限不是「只能这几档」，而是拦住打错字：delay_minutes=1000 会让工站空睡 16 小时，
    # 而这个动作除了睡觉什么都不做，堵住纯属浪费
    SIM_DELAY_MAX_MINUTES = 60

    def coin_cell_sim_delay(
        self,
        delay_minutes: float = 3,
        report_interval: float = 15.0,
    ) -> Dict[str, Any]:
        """长时延模拟：空转指定时长，用来模拟扣电组装的完整流程耗时。

        用于配液↔扣电联动的时序验证：把它替换掉真实的
        `coin_cell_init` + `coin_cell_start`，就能在不动硬件的情况下
        观察配液工站的互锁门控是否按预期阻塞、放行。

        Args:
            delay_minutes: 模拟耗时（分钟）。推荐档位 3 / 5 / 10，但接受任意正值，
                上限 60 分钟。不做白名单是因为快速联调时常需要 30 秒级的时长
            report_interval: 进度上报间隔（秒）。分段 sleep 是必要的——一句
                time.sleep(600) 会让前端十分钟毫无变化、看起来像卡死

        Returns:
            {"status": "ok", "delay_minutes": float, "delay_seconds": float,
             "start_time": str, "start_time_ms": int,
             "end_time": str, "end_time_ms": int, "actual_seconds": float}

        Raises:
            ValueError: delay_minutes 非正或超过 60 分钟
        """
        try:
            minutes = float(delay_minutes)
        except (TypeError, ValueError):
            raise ValueError(f"delay_minutes 必须是数字，收到: {delay_minutes!r}")

        if minutes <= 0:
            raise ValueError(f"delay_minutes 必须为正值，收到: {minutes}")
        if minutes > self.SIM_DELAY_MAX_MINUTES:
            raise ValueError(
                f"delay_minutes={minutes} 超过上限 {self.SIM_DELAY_MAX_MINUTES} 分钟。"
                f"这个动作只是空转，睡这么久多半是填错了；"
                f"推荐档位 {'/'.join(map(str, self.SIM_DELAY_PRESETS))} 分钟。"
            )

        total_seconds = minutes * 60.0
        start_dt = datetime.now()
        start_ms = int(start_dt.timestamp() * 1000)
        logger.info(
            f"[coin_cell_sim_delay] 开始模拟扣电组装耗时 {minutes} 分钟"
            f"（{total_seconds:.0f}s），起始 {start_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}"
        )

        deadline = time.monotonic() + total_seconds
        step = max(float(report_interval), 1.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(step, remaining))
            remaining = max(deadline - time.monotonic(), 0.0)
            logger.info(
                f"[coin_cell_sim_delay] 模拟中… 剩余 {remaining:.0f}s / 共 {total_seconds:.0f}s"
            )

        end_dt = datetime.now()
        end_ms = int(end_dt.timestamp() * 1000)
        actual = (end_ms - start_ms) / 1000.0
        result = {
            "status": "ok",
            "delay_minutes": minutes,
            "delay_seconds": round(total_seconds, 3),
            "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "start_time_ms": start_ms,
            "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "end_time_ms": end_ms,
            "actual_seconds": round(actual, 3),
        }
        logger.info(f"[coin_cell_sim_delay] ✅ 模拟结束，实际耗时 {actual:.1f}s: {result}")
        return result

    def coin_cell_init(self, material_search_enable: bool = False) -> bool:
        """
        组合函数：设备初始化 + 切换自动模式 + 启动
        
        整合了原有的三个独立函数：
        1. func_pack_device_init()  - 设备初始化
        2. func_pack_device_auto()  - 切换自动模式
        3. func_pack_device_start() - 启动设备
        
        Args:
            material_search_enable: 是否启用物料搜寻功能。
                                    设备初始化后会弹出物料搜寻确认弹窗，
                                    此参数控制自动点击'是'（启用）或'否'（不启用）。
                                    默认为False（不启用物料搜寻）。
        
        Returns:
            bool: 操作成功返回 True，失败返回 False
        """
        logger.info("=" * 60)
        logger.info("开始组合操作：设备初始化 → 物料搜寻确认 → 自动模式 → 启动")
        logger.info("=" * 60)
        
        # 步骤0: 前置条件检查
        logger.info("\n【步骤 0/4】前置条件检查...")
        self._ensure_modbus_connected()
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
            
            # 握手/命令线圈残留自动复位（init 前正常状态均应为 False）
            # 说明：以下线圈物理上均可写，若上次运行意外中断残留为 True 会导致流程卡死；
            #       此处统一尽力写回 False（best-effort，不阻断初始化）。
            #       注意方向：8700/8720/8730/8060/8050/8040 为 PC→PLC（PC 写，复位安全）；
            #       8500/8510/8710 为 PLC→PC（PLC 写），若 PLC 仍在主动驱动，写 False 可能被覆盖，仅告警继续。
            coils_to_reset = [
                ("COIL_UNILAB_SEND_MSG_SUCC_CMD",       "PC→PLC 配方发送完毕 (8700)"),
                ("UNILAB_SEND_ELECTROLYTE_BOTTLE_NUM",  "PC→PLC 瓶数发送完毕 (8720)"),
                ("UNILAB_SEND_FINISHED_CMD",            "PC→PLC 一组完成确认 (8730)"),
                ("COIL_SYS_INIT_CMD",                   "PC→PLC 系统初始化命令 (8060)"),
                ("COIL_SYS_AUTO_CMD",                   "PC→PLC 系统自动模式命令 (8050)"),
                ("COIL_SYS_HAND_CMD",                   "PC→PLC 系统手动模式命令 (8040)"),
                ("COIL_REQUEST_REC_MSG_STATUS",         "PLC→PC 请求接收配方 (8500)"),
                ("COIL_REQUEST_SEND_MSG_STATUS",        "PLC→PC 请求发送测试数据 (8510)"),
                ("COIL_UNILAB_REC_MSG_SUCC_CMD",        "PLC→PC 数据接收完毕 (8710)"),
            ]
            for coil_name, coil_desc in coils_to_reset:
                try:
                    cmd_node = self.client.use_node(coil_name)
                    cmd_value, cmd_err = cmd_node.read(1)
                    if cmd_err:
                        logger.warning(f"  ⚠ 无法读取 {coil_name}({coil_desc})，跳过此项")
                        continue
                    cmd_actual = cmd_value[0] if isinstance(cmd_value, (list, tuple)) else cmd_value
                    logger.info(f"  {coil_name} 当前值: {cmd_actual}")
                    if cmd_actual:
                        logger.warning(f"  ⚠ {coil_name}({coil_desc}) = True，期望 False，正在自动复位...")
                        cmd_node.write(False)
                        time.sleep(0.2)
                        # 回读确认是否复位成功
                        verify_value, verify_err = cmd_node.read(1)
                        verify_actual = verify_value[0] if isinstance(verify_value, (list, tuple)) else verify_value
                        if verify_err or verify_actual:
                            # 写不进去（多为 PLC 仍在主动驱动该状态位）：仅告警，不阻断初始化
                            logger.warning(
                                f"  ⚠ {coil_name} 自动复位未生效，当前值仍为 {verify_actual}"
                                f"（{coil_desc}）。若为 PLC 主动驱动属正常，继续初始化；"
                                "如初始化异常请检查上次运行是否意外中断或在HMI手动复位。"
                            )
                        else:
                            logger.info(f"  ✓ {coil_name} 已自动复位为 False")
                    else:
                        logger.info(f"  ✓ {coil_name} 检查通过 (值为False)")
                except Exception as cmd_e:
                    logger.warning(f"  ⚠ 处理 {coil_name} 时发生异常: {cmd_e}，跳过此项")

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
        
        # 步骤1: 设备初始化（包含弹窗检测）
        logger.info("\n【步骤 1/4】设备初始化...")
        try:
            # 切换手动模式
            logger.info("切换手动模式...")
            self._sys_hand_cmd(True)
            time.sleep(1)
            while (self._sys_hand_status()) == False:
                logger.debug("waiting for hand_cmd")
                time.sleep(1)
            
            # 设备初始化命令
            logger.info("发送初始化命令...")
            self._sys_init_cmd(True)
            time.sleep(1)
            
            # 等待初始化完成，同时检测物料搜寻弹窗
            logger.info("等待初始化完成（同时监测物料搜寻弹窗）...")
            dialog_handled = False
            max_wait_time = 240 #最多等待240s
            start_wait = time.time()
            
            while (self._sys_init_status()) == False:
                # 检查是否超时
                if time.time() - start_wait > max_wait_time:
                    raise RuntimeError(f"初始化超时（超过 {max_wait_time} 秒）")
                
                # 如果还没处理弹窗，检测弹窗是否出现
                if not dialog_handled:
                    try:
                        dialog_node = self.client.use_node('COIL_MATERIAL_SEARCH_DIALOG_APPEAR')
                        dialog_state, read_err = dialog_node.read(1)
                        
                        if not read_err:
                            # 提取实际值
                            if isinstance(dialog_state, (list, tuple)):
                                dialog_actual = dialog_state[0] if len(dialog_state) > 0 else False
                            else:
                                dialog_actual = dialog_state
                            
                            # 如果弹窗出现，立即处理
                            if dialog_actual:
                                logger.info("✓ 在初始化过程中检测到物料搜寻确认弹窗！")
                                logger.info(f"用户选择: {'启用物料搜寻（点击是）' if material_search_enable else '不启用物料搜寻（点击否）'}")
                                
                                # 执行脉冲按钮点击
                                button_name = "是" if material_search_enable else "否"
                                coil_name = "COIL_MATERIAL_SEARCH_CONFIRM_YES" if material_search_enable else "COIL_MATERIAL_SEARCH_CONFIRM_NO"
                                
                                button_node = self.client.use_node(coil_name)
                                
                                # 脉冲：True -> 等待 -> False
                                logger.info(f"  → 按下按钮 '{button_name}'")
                                button_node.write(True)
                                time.sleep(0.5)
                                logger.info(f"  → 释放按钮 '{button_name}'")
                                button_node.write(False)
                                time.sleep(0.3)
                                
                                logger.info(f"✓ 成功处理物料搜寻确认弹窗（选择: {button_name}）")
                                dialog_handled = True
                                
                    except Exception as e:
                        logger.debug(f"检测弹窗时出错: {e}")
                
                logger.debug("waiting for init_cmd")
                time.sleep(1)
            
            logger.info("✓ 初始化状态完成")
            
            # 手动按钮置回False
            self._sys_hand_cmd(False)
            time.sleep(1)
            while (self._sys_hand_cmd()) == True:
                logger.debug("waiting for hand_cmd to False")
                time.sleep(1)
            
            # 初始化命令置回False
            self._sys_init_cmd(False)
            time.sleep(1)
            while (self._sys_init_cmd()) == True:
                logger.debug("waiting for init_cmd to False")
                time.sleep(1)
            
            logger.info("✓ 设备初始化完成")
        except Exception as e:
            logger.error(f"❌ 设备初始化失败: {e}")
            return False
        
        # 步骤1.5已经在步骤1中处理，跳过
        logger.info("\n【步骤 1.5/4】物料搜寻确认已在初始化过程中完成")
        
        # 步骤2: 切换自动模式
        logger.info("\n【步骤 2/4】切换自动模式...")
        try:
            self.func_pack_device_auto()
            logger.info("✓ 切换自动模式完成")
        except Exception as e:
            logger.error(f"❌ 切换自动模式失败: {e}")
            return False
        
        # 步骤3: 启动设备
        logger.info("\n【步骤 3/4】启动设备...")
        try:
            self.func_pack_device_start()
            logger.info("✓ 启动设备完成")
        except Exception as e:
            logger.error(f"❌ 启动设备失败: {e}")
            return False
        
        logger.info("\n" + "=" * 60)
        logger.info("组合操作完成：设备已成功初始化、确认物料搜寻、切换自动模式并启动")
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

    def coin_cell_start(
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
        # 装配参数（负极片/正极片/隔膜/枪头盒/铝箔垫）
        ne_plate_num: int = 0,
        ne_plate_matrix: int = 0,
        pe_plate_num: int = 0,
        pe_plate_matrix: int = 0,
        sep_plate_num: int = 0,
        sep_plate_matrix: int = 0,
        tip_box_matrix: int = 0,
        aluminum_foil: bool = True,
        battery_pressure_mode: bool = True,
        battery_clean_ignore: bool = False,
        # 正负极片取料/负极片吹气参数
        positive_plate_pick_mode: bool = False,
        negative_plate_thickness_mode: bool = False,
        thick_negative_plate_blow_time: int = 0,
        thin_negative_plate_blow_time: int = 0,
        file_path: str = "/Users/sml/work",
        formulations: List[Dict] = None
    ) -> Dict[str, Any]:
        """
        发送瓶数+简化组装函数（适用于第二批次及后续批次）
        
        合并了发送瓶数和简化组装流程，用于连续批次生产。
        适用场景：设备已完成初始化和启动，仍在自动模式下运行。
        
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
            ne_plate_num: 负极片盘数
            ne_plate_matrix: 负极片矩阵点位
            pe_plate_num: 正极片盘数（仅水平取料时需要填写）
            pe_plate_matrix: 正极片矩阵点位（仅水平取料时需要填写）
            sep_plate_num: 隔膜盘数
            sep_plate_matrix: 隔膜矩阵点位
            tip_box_matrix: 枪头盒矩阵点位
            aluminum_foil: 是否使用铝箔垫片
            battery_pressure_mode: 是否启用压力模式
            battery_clean_ignore: 是否忽略电池清洁
            positive_plate_pick_mode: 正极片取料模式 (False=弹夹取料, True=水平取料)
            negative_plate_thickness_mode: 负极片厚度模式 (False=厚负极片, True=薄负极片)
            thick_negative_plate_blow_time: 厚负极片吹气时间
            thin_negative_plate_blow_time: 薄负极片吹气时间
            file_path: 实验记录保存路径
            formulations: 配方信息列表（从 create_orders.mass_ratios 获取）
                         包含 orderCode, target_mass_ratio, real_mass_ratio 等
                         用于CSV数据追溯，可选参数
            
        Returns:
            dict: 包含组装结果的字典
            
        注意:
            - 第一次启动需先调用 coin_cell_init()
            - 后续批次直接调用此函数即可
        """
        logger.info("=" * 60)
        logger.info("开始发送瓶数+简化组装流程...")
        logger.info(f"电解液瓶数: {elec_num}, 每瓶电池数: {elec_use_num}")
        
        # 本次组装运行的时间戳（用于 CSV 文件命名，整批电池共用同一个文件）
        self._csv_export_run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logger.info(f"本次组装 CSV 文件时间戳: {self._csv_export_run_timestamp}")
        
        # 存储配方信息到设备状态（供 CSV 写入使用）
        if formulations:
            logger.info(f"接收到配方信息: {len(formulations)} 条")
            # 将配方信息按 orderCode 索引，方便后续查找
            self._formulations_map = {
                f["orderCode"]: f for f in formulations
            } if formulations else {}
            # ✅ 新增：存储配方列表（按接收顺序），用于索引访问
            self._formulations_list = formulations
        else:
            logger.warning("未接收到配方信息，CSV将不包含配方字段")
            self._formulations_map = {}
            self._formulations_list = []
        
        # ✅ 新增：存储每瓶电池数，用于计算当前使用的瓶号
        # ⚠️ 确保转换为整数（前端可能传递字符串）
        self._elec_use_num = int(elec_use_num) if elec_use_num else 0
        # ✅ 新增：存储电解液瓶数，用于计算订单完成百分比（分母 = 瓶数 × 每瓶电池数）
        self._elec_bottle_num = int(elec_num) if elec_num else 0
        logger.info(f"已存储参数: 电解液瓶数={self._elec_bottle_num}, 每瓶电池数={self._elec_use_num}, 配方数={len(self._formulations_list)}")
        
        # ✅ 新增：软件层电池计数器（防止硬件计数器不准确）
        self._software_battery_counter = 0  # 从0开始，每写入一次CSV递增
        logger.info("软件层电池计数器已初始化")
        
        logger.info("=" * 60)
        
        # 步骤1: 发送电解液瓶数（触发物料搬运）
        logger.info("步骤1/2: 发送电解液瓶数，触发物料搬运...")
        try:
            self.func_pack_send_bottle_num(elec_num)
            logger.info("✓ 瓶数发送完成，物料搬运中...")
        except Exception as e:
            logger.error(f"发送瓶数失败: {e}")
            return {
                "success": False,
                "error": f"发送瓶数失败: {e}",
                "total_batteries": 0,
                "batteries": []
            }
        
        # 步骤2: 执行简化组装流程
        logger.info("步骤2/2: 开始简化组装流程...")
        result = self.func_allpack_cmd_simp(
            elec_num=elec_num,
            elec_use_num=elec_use_num,
            elec_vol=elec_vol,
            dual_drop_mode=dual_drop_mode,
            dual_drop_first_volume=dual_drop_first_volume,
            dual_drop_suction_timing=dual_drop_suction_timing,
            dual_drop_start_timing=dual_drop_start_timing,
            assembly_type=assembly_type,
            assembly_pressure=assembly_pressure,
            ne_plate_num=ne_plate_num,
            ne_plate_matrix=ne_plate_matrix,
            pe_plate_num=pe_plate_num,
            pe_plate_matrix=pe_plate_matrix,
            sep_plate_num=sep_plate_num,
            sep_plate_matrix=sep_plate_matrix,
            tip_box_matrix=tip_box_matrix,
            aluminum_foil=aluminum_foil,
            battery_pressure_mode=battery_pressure_mode,
            battery_clean_ignore=battery_clean_ignore,
            positive_plate_pick_mode=positive_plate_pick_mode,
            negative_plate_thickness_mode=negative_plate_thickness_mode,
            thick_negative_plate_blow_time=thick_negative_plate_blow_time,
            thin_negative_plate_blow_time=thin_negative_plate_blow_time,
            file_path=file_path
        )
        
        logger.info("=" * 60)
        logger.info("发送瓶数+简化组装流程完成")
        logger.info(f"总组装电池数: {result.get('total_batteries', 0)}")
        logger.info("=" * 60)
        
        return result


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
        self._target_assembly_pressure = assembly_pressure
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
        data_coin_type = self.data_coin_type  # 电池类型（7或8种物料）
        data_battery_number = self.data_current_assembling_count  # ✅ 真正的电池编号
        
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
        logger.debug(f"data_coin_type: {data_coin_type}")  # 电池类型
        logger.debug(f"data_battery_number: {data_battery_number}")  # ✅ 电池编号
        logger.debug(f"data_electrolyte_code: {data_electrolyte_code}")
        logger.debug(f"data_coin_cell_code: {data_coin_cell_code}")
        #接收完信息后，读取完毕标志位置True
        finished_battery_magazine = self.deck.get_resource("成品弹夹")        
        
        # 计算电池应该放在哪个洞，以及洞内的堆叠位置
        # 成品弹夹有6个洞，每个洞可堆叠20颗电池
        # 前5个洞（索引0-4）放正常电池，第6个洞（索引5）放NG电池
        BATTERIES_PER_HOLE = 20
        MAX_NORMAL_BATTERIES = 100  # 5个洞 × 20颗/洞
        
        hole_index = self.coin_num_N // BATTERIES_PER_HOLE  # 第几个洞（0-4为正常电池）
        in_hole_position = self.coin_num_N % BATTERIES_PER_HOLE  # 洞内的堆叠序号
        
        if hole_index >= 5:
            logger.error(f"电池数量超出正常容量范围: {self.coin_num_N + 1} > {MAX_NORMAL_BATTERIES}")
            raise ValueError(f"成品弹夹正常洞位已满（最多{MAX_NORMAL_BATTERIES}颗），当前尝试放置第{self.coin_num_N + 1}颗")
        
        target_hole = finished_battery_magazine.children[hole_index]  # 获取目标洞
        
        # 生成唯一的电池名称（使用时间戳确保唯一性）
        timestamp_suffix = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        battery_name = f"battery_{self.coin_num_N}_{timestamp_suffix}"
        
        # 创建新的电池资源
        battery = ElectrodeSheet(name=battery_name, size_x=14, size_y=14, size_z=2)
        battery._unilabos_state = {
                            "electrolyte_name": data_coin_cell_code,
                            "data_electrolyte_code": data_electrolyte_code,
                            "open_circuit_voltage": data_open_circuit_voltage,
                            "assembly_pressure": data_assembly_pressure,
                            "electrolyte_volume": data_electrolyte_volume
                            }
        
        # 将电池堆叠到目标洞中
        try:
            target_hole.assign_child_resource(battery, location=None)
            logger.info(f"成功放置电池 {battery_name} 到弹夹洞{hole_index}的第{in_hole_position + 1}层 (总计第{self.coin_num_N + 1}颗)")
        except Exception as e:
            logger.error(f"放置电池资源失败: {e}")
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
            #秒级时间戳用于标记每一行电池数据
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._last_assembly_timestamp = timestamp
            #生成输出文件的变量（整批组装共用同一文件，文件名带运行时间戳）
            #若未经 coin_cell_start 设置运行时间戳则回退到当前时间
        run_timestamp = getattr(self, "_csv_export_run_timestamp", None) or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_export_file = os.path.join(file_path, f"coin_cell_assembly_{run_timestamp}.csv")   
        # 导出目录不存在则跳过 CSV 记录：仅告警，不创建目录、不中断组装任务
        if not os.path.isdir(file_path):
            logger.warning(f"[CSV写入] 导出目录不存在，跳过本次CSV记录（不中断任务）: {file_path}")
            self.success = True
            return self.success
        #将数据存入csv文件
        if not os.path.exists(self.csv_export_file):
            #创建一个表头
            with open(self.csv_export_file, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow([
                    'Time', 'open_circuit_voltage', 'pole_weight', 
                    'assembly_time', 'target_assembly_pressure', 'real_assembly_pressure', 'electrolyte_volume', 
                    'data_coin_type', 'electrolyte_code', 'coin_cell_code',
                    'orderName', 'prep_bottle_barcode', 'vial_bottle_barcodes',
                    'target_mass_ratio', 'real_mass_ratio'
                ])
                #立刻写入磁盘
                csvfile.flush()
        #开始追加电池信息
        with open(self.csv_export_file, 'a', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            
            # ========== 提取配方信息 ==========
            formulation_order_name = ""
            prep_bottle_barcode = ""
            vial_bottle_barcodes = ""
            target_ratio_str = ""
            real_ratio_str = ""
            
            # 从 self._formulations_list 获取配方信息
            if hasattr(self, '_formulations_list') and self._formulations_list:
                # ✅ 新方案：根据电池编号和每瓶电池数计算当前瓶号
                # 例如：elec_use_num=2时，电池1-2用瓶0，电池3-4用瓶1
                if hasattr(self, '_elec_use_num') and self._elec_use_num:
                    # ⚠️ 确保转换为整数（防御性编程）
                    elec_use_num_int = int(self._elec_use_num) if self._elec_use_num else 1
                    if elec_use_num_int > 0:
                        current_bottle_index = (data_battery_number - 1) // elec_use_num_int
                    else:
                        current_bottle_index = 0
                    
                    logger.debug(
                        f"[CSV写入] 电池 {data_battery_number}: 计算瓶号索引={current_bottle_index} "
                        f"(每瓶{self._elec_use_num}颗电池)"
                    )
                else:
                    # 降级方案：尝试从二维码解析（仅当参数未设置时）
                    current_bottle_index = int(data_electrolyte_code.split('-')[-1]) if '-' in str(data_electrolyte_code) else 0
                    logger.debug(
                        f"[CSV写入] 电池 {data_battery_number}: 从二维码解析瓶号索引={current_bottle_index}"
                    )
                
                # 从配方列表中获取对应配方
                if 0 <= current_bottle_index < len(self._formulations_list):
                    formulation = self._formulations_list[current_bottle_index]
                    formulation_order_name = formulation.get("orderName", "")
                    prep_bottle_barcode = formulation.get("prep_bottle_barcode", "")
                    vial_bottle_barcodes = formulation.get("vial_bottle_barcodes", "")
                    
                    real_ratio = formulation.get("real_mass_ratio", {})
                    target_ratio = formulation.get("target_mass_ratio", {})
                    
                    # 将配方比例转为JSON字符串
                    import json
                    target_ratio_str = json.dumps(target_ratio, ensure_ascii=False) if target_ratio else ""
                    real_ratio_str = json.dumps(real_ratio, ensure_ascii=False) if real_ratio else ""
                    
                    logger.info(
                        f"[CSV写入] 电池 {data_battery_number}: 使用配方[{current_bottle_index}] "
                        f"orderName={formulation_order_name}, 配液瓶={prep_bottle_barcode}, 分液瓶={vial_bottle_barcodes}"
                    )
                else:
                    logger.warning(
                        f"[CSV写入] 电池 {data_battery_number}: 瓶号索引 {current_bottle_index} "
                        f"超出配方列表范围 (共{len(self._formulations_list)}个配方)"
                    )
            else:
                logger.debug(f"[CSV写入] 电池 {data_battery_number}: 未找到配方信息数据")
            
            target_assembly_pressure = getattr(self, '_target_assembly_pressure', '')
            writer.writerow([
                timestamp, data_open_circuit_voltage, data_pole_weight,
                data_assembly_time, target_assembly_pressure, data_assembly_pressure, data_electrolyte_volume,
                data_coin_type, data_electrolyte_code, data_coin_cell_code,
                formulation_order_name, prep_bottle_barcode, vial_bottle_barcodes,
                target_ratio_str, real_ratio_str
            ])
            #立刻写入磁盘
            csvfile.flush()
        self._software_battery_counter = getattr(self, "_software_battery_counter", 0) + 1
        _log_coin_cell_record(
            mode="coin_cell_start",
            record_index=self._software_battery_counter,
            device_count=data_battery_number,
            snapshot={
                "time": timestamp,
                "coin_cell_code": data_coin_cell_code,
                "electrolyte_code": data_electrolyte_code,
                "open_circuit_voltage": data_open_circuit_voltage,
                "pole_weight": data_pole_weight,
                "assembly_time": data_assembly_time,
                "target_assembly_pressure": target_assembly_pressure,
                "real_assembly_pressure": data_assembly_pressure,
                "electrolyte_volume": data_electrolyte_volume,
                "data_coin_type": data_coin_type,
            },
            csv_file=self.csv_export_file,
        )
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
        # 装配参数（负极片/正极片/隔膜/枪头盒/铝箔垫）
        ne_plate_num: int = 0,
        ne_plate_matrix: int = 0,
        pe_plate_num: int = 0,
        pe_plate_matrix: int = 0,
        sep_plate_num: int = 0,
        sep_plate_matrix: int = 0,
        tip_box_matrix: int = 0,
        aluminum_foil: bool = True,
        battery_pressure_mode: bool = True,
        battery_clean_ignore: bool = False,
        # 正负极片取料/负极片吹气参数
        positive_plate_pick_mode: bool = False,
        negative_plate_thickness_mode: bool = False,
        thick_negative_plate_blow_time: int = 0,
        thin_negative_plate_blow_time: int = 0,
        file_path: str = "/Users/sml/work"
    ) -> Dict[str, Any]:
        """
    
        简化版组装函数，自动处理以下配置：
        - 负极片、正极片（仅水平取料）和隔膜的盘数及矩阵点位
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
            ne_plate_num: 负极片盘数
            ne_plate_matrix: 负极片矩阵点位
            pe_plate_num: 正极片盘数（仅水平取料时需要填写）
            pe_plate_matrix: 正极片矩阵点位（仅水平取料时需要填写）
            sep_plate_num: 隔膜盘数
            sep_plate_matrix: 隔膜矩阵点位
            tip_box_matrix: 枪头盒矩阵点位
            aluminum_foil: 是否使用铝箔垫片
            battery_pressure_mode: 是否启用压力模式
            battery_clean_ignore: 是否忽略电池清洁
            positive_plate_pick_mode: 正极片取料模式 (False=弹夹取料, True=水平取料)
            negative_plate_thickness_mode: 负极片厚度模式 (False=厚负极片, True=薄负极片)
            thick_negative_plate_blow_time: 厚负极片吹气时间
            thin_negative_plate_blow_time: 薄负极片吹气时间
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
        ne_plate_num = int(ne_plate_num)
        ne_plate_matrix = int(ne_plate_matrix)
        pe_plate_num = int(pe_plate_num)
        pe_plate_matrix = int(pe_plate_matrix)
        sep_plate_num = int(sep_plate_num)
        sep_plate_matrix = int(sep_plate_matrix)
        tip_box_matrix = int(tip_box_matrix)
        thick_negative_plate_blow_time = int(thick_negative_plate_blow_time)
        thin_negative_plate_blow_time = int(thin_negative_plate_blow_time)
        
        # 步骤1: 设置设备参数（负极片/隔膜/枪头盒/铝箔垫/压力模式等）
        logger.info("=" * 60)
        logger.info("设置设备参数...")
        logger.info(f"  负极片盘数: {ne_plate_num}, 矩阵点位: {ne_plate_matrix}")
        logger.info(f"  正极片盘数: {pe_plate_num}, 矩阵点位: {pe_plate_matrix}")
        logger.info(f"  隔膜盘数: {sep_plate_num}, 矩阵点位: {sep_plate_matrix}")
        logger.info(f"  枪头盒矩阵点位: {tip_box_matrix}")
        logger.info(f"  铝箔垫片: {aluminum_foil}, 压力模式: {battery_pressure_mode}")
        logger.info(f"  压制力: {assembly_pressure}")
        logger.info(f"  忽略电池清洁: {battery_clean_ignore}")
        logger.info(f"  正极片取料模式: {'水平取料' if positive_plate_pick_mode else '弹夹取料'}")
        logger.info(f"  负极片厚度模式: {'薄负极片' if negative_plate_thickness_mode else '厚负极片'}")
        logger.info(f"  厚负极片吹气时间: {thick_negative_plate_blow_time}, 薄负极片吹气时间: {thin_negative_plate_blow_time}")
        logger.info("=" * 60)
        
        # 写入基础参数到PLC
        self.client.use_node('REG_MSG_NE_PLATE_NUM').write(ne_plate_num)
        self.client.use_node('REG_MSG_NE_PLATE_MATRIX').write(ne_plate_matrix)
        self.client.use_node('REG_MSG_PE_PLATE_NUM').write(pe_plate_num)
        self.client.use_node('REG_MSG_PE_PLATE_MATRIX').write(pe_plate_matrix)
        self.client.use_node('REG_MSG_SEPARATOR_PLATE_NUM').write(sep_plate_num)
        self.client.use_node('REG_MSG_SEPARATOR_PLATE_MATRIX').write(sep_plate_matrix)
        self.client.use_node('REG_MSG_TIP_BOX_MATRIX').write(tip_box_matrix)
        self.client.use_node('COIL_ALUMINUM_FOIL').write(not aluminum_foil)
        self.client.use_node('REG_MSG_PRESS_MODE').write(not battery_pressure_mode)
        self.client.use_node('REG_MSG_BATTERY_CLEAN_IGNORE').write(battery_clean_ignore)
        
        # 正极片取料模式 / 负极片厚度模式 / 负极片吹气时间
        self.client.use_node('COIL_POSITIVE_PLATE_PICK_MODE').write(positive_plate_pick_mode)
        self.client.use_node('COIL_NEGATIVE_PLATE_THICKNESS_MODE').write(negative_plate_thickness_mode)
        self.client.use_node('REG_MSG_THICK_NEGATIVE_PLATE_BLOW_TIME').write(thick_negative_plate_blow_time)
        self.client.use_node('REG_MSG_THIN_NEGATIVE_PLATE_BLOW_TIME').write(thin_negative_plate_blow_time)
        
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
        
        # 步骤2: 执行组装流程（含断点续传）
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
                    "Time": getattr(self, "_last_assembly_timestamp", datetime.now().strftime("%Y%m%d_%H%M%S")),
                    "open_circuit_voltage": open_circuit_voltage,
                    "pole_weight": pole_weight,
                    "assembly_time": self.data_assembly_time,
                    "assembly_pressure": self.data_assembly_pressure,
                    "target_assembly_pressure": getattr(self, "_target_assembly_pressure", ""),
                    "electrolyte_volume": self.data_electrolyte_volume,
                    "data_coin_type": getattr(self, "data_coin_type", 0),
                    "electrolyte_code": electrolyte_qr_code,
                    "coin_cell_code": battery_qr_code,
                }
                battery_data_list.append(battery_info)
                print(f"已收集第 {coin_num_N + 1} 个电池数据: 电池码={battery_info['coin_cell_code']}, 电解液码={battery_info['electrolyte_code']}")
                
                time.sleep(1)

                # 生成断点文件
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                try:
                    with open(summary_csv_file, 'w', newline='', encoding='utf-8') as csvfile:
                        writer = csv.writer(csvfile)
                        writer.writerow(['elec_num','elec_use_num', 'elec_num_N', 'elec_use_num_N', 'coin_num_N', 'timestamp'])
                        writer.writerow([elec_num, elec_use_num, elec_num_N, elec_use_num_N, coin_num_N, timestamp])
                        csvfile.flush()
                except Exception as e:
                    logger.warning(f"[断点文件] 写入失败，跳过（不中断任务）: {e}")
                coin_num_N += 1
                self.coin_num_N = coin_num_N
                elec_use_num_N += 1
            elec_num_N += 1
            elec_use_num_N = 0

        # 循环正常结束，则删除断点文件
        if os.path.exists(summary_csv_file):
            os.remove(summary_csv_file)
        # 全部完成后等待依华发送完成信号
        self.func_pack_send_finished_cmd()
        
        # 返回JSON格式数据
        result = {
            "success": True,
            "total_batteries": len(battery_data_list),
            "batteries": battery_data_list,
            "assembly_data": battery_data_list,
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
        test_battery_plate = self.deck.get_resource("\u7535\u6c60\u6599\u76d8")
        for i in range(16):            
            battery = ElectrodeSheet(name=f"battery_{i}", size_x=16, size_y=16, size_z=2)
            battery._unilabos_state = {
                                "diameter": 20.0,
                                "height": 20.0,
                                "assembly_pressure": i,
                                "electrolyte_volume": 20.0,
                                "electrolyte_name": f"DP{i}"
                                }
            test_battery_plate.children[i].assign_child_resource(battery, location=None)
            
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

        #本次读取会话的时间戳（用于 CSV 文件命名，整个会话共用同一个文件）
        run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 只要允许读取标志位为true，就持续运行该函数，直到触发停止条件
        while self.allow_data_read:

            #函数运行标志位，确保只同时启动一个导出函数
            self.csv_export_running = True

            #等待接收结果标志位置True
            while self.request_send_msg_status == False:
                print("waiting for send_msg_status to True")
                time.sleep(1)
            #秒级时间戳用于标记每一行电池数据
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            #生成输出文件的变量（整个读取会话共用同一文件，文件名带运行时间戳）
            self.csv_export_file = os.path.join(file_path, f"coin_cell_assembly_{run_timestamp}.csv")   
            
            #接收信息
            data_open_circuit_voltage = self.data_open_circuit_voltage
            data_pole_weight = self.data_pole_weight
            data_assembly_time = self.data_assembly_time
            data_assembly_pressure = self.data_assembly_pressure
            data_electrolyte_volume = self.data_electrolyte_volume
            data_coin_type = self.data_coin_type  # 电池类型（7或8种物料）
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
                    data_coin_type, data_electrolyte_code, data_coin_cell_code  # ✅ 已修正
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

    # ===================== 旁观者采集（全程只读） ======================

    def _observer_read_snapshot(self) -> Dict[str, Any]:
        """只读方式采集一份当前设备数据快照（旁观者模式专用，不写任何线圈）

        Returns:
            dict: 单颗电池的数据快照，读取失败的字段回退为 0 / "N/A"
        """
        def _read(attr_name: str, caster, default):
            try:
                return caster(getattr(self, attr_name))
            except Exception as e:
                logger.warning(f"[旁观采集] 读取 {attr_name} 失败，使用默认值 {default}: {e}")
                return default

        return {
            "time": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "open_circuit_voltage": _read("data_open_circuit_voltage", _observer_cast_float, 0.0),
            "pole_weight": _read("data_pole_weight", _observer_cast_float, 0.0),
            "assembly_time": _read("data_assembly_time", _observer_cast_float, 0.0),
            "target_assembly_pressure": _read("data_target_assembly_pressure", _observer_cast_int, 0),
            "real_assembly_pressure": _read("data_assembly_pressure", _observer_cast_int, 0),
            "electrolyte_volume": _read("data_electrolyte_volume", _observer_cast_int, 0),
            "data_coin_type": _read("data_coin_type", _observer_cast_int, 0),
            "electrolyte_code": _read("data_electrolyte_code", _observer_cast_code, "N/A"),
            "coin_cell_code": _read("data_coin_cell_code", _observer_cast_code, "N/A"),
        }

    def _observer_prepare_files(self, file_path: str, run_timestamp: str):
        """创建旁观者采集的两个 CSV 并写入表头

        Args:
            file_path: 导出目录
            run_timestamp: 本次采集的运行时间戳，两个文件共用

        Returns:
            tuple: (组装记录CSV路径, Neware提交CSV路径)
        """
        assembly_file = os.path.join(file_path, f"coin_cell_observe_{run_timestamp}.csv")
        neware_file = os.path.join(file_path, f"neware_submit_{run_timestamp}.csv")

        if not os.path.exists(assembly_file):
            with open(assembly_file, 'w', newline='', encoding='utf-8') as csvfile:
                csv.writer(csvfile).writerow(_OBSERVER_ASSEMBLY_HEADER)
                csvfile.flush()

        # Neware 的 submit_from_csv_export_ndax 以 gbk 读取，此处必须同样用 gbk 写
        if not os.path.exists(neware_file):
            with open(neware_file, 'w', newline='', encoding='gbk', errors='replace') as csvfile:
                csv.writer(csvfile).writerow(_OBSERVER_NEWARE_HEADER)
                csvfile.flush()

        return assembly_file, neware_file

    def _observer_write_row(self, snapshot: Dict[str, Any], assembly_file: str, neware_file: str,
                            battery_system: str = "") -> None:
        """把一颗电池的快照同时追加到组装记录 CSV 和 Neware 提交 CSV

        Args:
            snapshot: _observer_read_snapshot 返回的快照
            assembly_file: 组装记录CSV路径
            neware_file: Neware提交CSV路径
            battery_system: 预填 Neware CSV 的「电池体系」列
        """
        with open(assembly_file, 'a', newline='', encoding='utf-8') as csvfile:
            csv.writer(csvfile).writerow([
                snapshot["time"], snapshot["open_circuit_voltage"], snapshot["pole_weight"],
                snapshot["assembly_time"], snapshot["target_assembly_pressure"], snapshot["real_assembly_pressure"],
                snapshot["electrolyte_volume"], snapshot["data_coin_type"],
                snapshot["electrolyte_code"], snapshot["coin_cell_code"],
            ])
            csvfile.flush()

        # 除三个可采集字段外其余列留空，由人工补充后再喂给 Neware
        with open(neware_file, 'a', newline='', encoding='gbk', errors='replace') as csvfile:
            csv.writer(csvfile).writerow([
                snapshot["coin_cell_code"], snapshot["electrolyte_code"], battery_system,
                '', '', '', snapshot["pole_weight"], '', '', '',
            ])
            csvfile.flush()

    def coin_cell_observe_export(
        self,
        file_path: str = r"D:\Uni-Lab-OS-Data\observe",
        max_duration: float = 3600,
        target_count: int = 0,
        idle_timeout: float = 0,
        stop_on_device_idle: bool = False,
        device_idle_grace: float = 120,
        poll_interval: float = 0.5,
        battery_system: str = "",
    ) -> Dict[str, Any]:
        """旁观者采集：不下发任务、不写任何线圈，只轮询依华工站的已完成电池计数。计数跳变时记录跳变前一拍的快照，同时追加写入组装记录 CSV 和 Neware 提交 CSV。

        Args:
            file_path: 导出目录，两个 CSV 的输出目录，不存在会自动创建
            max_duration: 监控时长上限（秒），到点即停；填 0 或负数表示不限
            target_count: 目标电池数，采满该颗数即停；0 表示不限
            idle_timeout: 空闲超时（秒），连续该秒数没有新电池即停；0 表示关闭
            stop_on_device_idle: 设备停机即停，设备运行/自动状态掉线并持续超过宽限时间后停止采集，默认关闭以免误停
            device_idle_grace: 停机判定宽限（秒），仅在“设备停机即停”开启时生效
            poll_interval: 轮询间隔（秒），越小越不容易错过跳变前的数据，但 Modbus 读取压力越大
            battery_system: 电池体系，预填 Neware 提交 CSV 的「电池体系」列，如 HC_LFP；留空则由人工补

        Returns:
            dict: 采集汇总，含停止原因、采集颗数、两个 CSV 路径
        """
        if self._observer_running:
            logger.warning("[旁观采集] 已有采集在运行中，忽略本次请求")
            return {
                "success": False,
                "stop_reason": "already_running",
                "collected": 0,
                "missed": 0,
                "csv_assembly_file": "",
                "csv_neware_file": "",
                "start_time": "",
                "end_time": "",
            }

        max_duration = float(max_duration or 0)
        target_count = int(target_count or 0)
        idle_timeout = float(idle_timeout or 0)
        device_idle_grace = float(device_idle_grace or 0)
        poll_interval = max(float(poll_interval or 0.5), 0.05)
        battery_system = str(battery_system or "").strip()

        os.makedirs(file_path, exist_ok=True)
        run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        assembly_file, neware_file = self._observer_prepare_files(file_path, run_timestamp)

        logger.info("=" * 60)
        logger.info("[旁观采集] 启动，只读监控依华工站已完成电池计数")
        logger.info(f"[旁观采集] 组装记录: {assembly_file}")
        logger.info(f"[旁观采集] Neware提交: {neware_file}")
        logger.info(
            f"[旁观采集] 停止条件: 时长上限={max_duration or '不限'}s, 目标颗数={target_count or '不限'}, "
            f"空闲超时={idle_timeout or '关闭'}s, 停机即停={stop_on_device_idle}"
        )
        logger.info("=" * 60)
        if self.debug_mode:
            logger.warning("[旁观采集] 当前为 debug_mode，寄存器恒返回 0，采集将空转到停止条件命中")

        start_ts = time.time()
        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        collected = 0
        missed = 0
        last_count = None
        prev_snapshot = None
        last_new_ts = start_ts
        device_idle_since = None
        stop_reason = "max_duration"

        self._observer_running = True
        try:
            while True:
                now = time.time()
                if max_duration > 0 and now - start_ts >= max_duration:
                    stop_reason = "max_duration"
                    break
                if target_count > 0 and collected >= target_count:
                    stop_reason = "target_count"
                    break
                if idle_timeout > 0 and now - last_new_ts >= idle_timeout:
                    stop_reason = "idle_timeout"
                    break

                try:
                    self._ensure_modbus_connected()
                    # 先读数据后读计数，保证拿到的是跳变前的那一拍
                    snapshot = self._observer_read_snapshot()
                    count = _observer_cast_int(self.data_current_completed_count)
                except Exception as e:
                    logger.warning(f"[旁观采集] 本轮读取失败，{poll_interval}s 后重试: {e}")
                    time.sleep(poll_interval)
                    continue

                if stop_on_device_idle:
                    try:
                        device_running = bool(self._sys_start_status()) and bool(self._sys_auto_status())
                    except Exception as e:
                        logger.warning(f"[旁观采集] 读取设备运行状态失败，本轮不做停机判定: {e}")
                        device_running = True
                    if device_running:
                        device_idle_since = None
                    else:
                        device_idle_since = device_idle_since or now
                        if now - device_idle_since >= device_idle_grace:
                            stop_reason = "device_idle"
                            break

                if last_count is None:
                    last_count = count
                    logger.info(f"[旁观采集] 基线计数={count}，从下一次跳变开始记录")
                elif count != last_count:
                    if count < last_count:
                        logger.warning(f"[旁观采集] 计数回退 {last_count} → {count}，视为设备复位，仅重置基线")
                    else:
                        step = count - last_count
                        if step > 1:
                            missed += step - 1
                            logger.warning(f"[旁观采集] 计数跳变 {last_count} → {count}，漏采 {step - 1} 颗")
                        row_snapshot = prev_snapshot or snapshot
                        try:
                            self._observer_write_row(row_snapshot, assembly_file, neware_file, battery_system)
                            collected += 1
                            _log_coin_cell_record(
                                mode="observer",
                                record_index=collected,
                                device_count=count,
                                snapshot=row_snapshot,
                                csv_file=assembly_file,
                            )
                        except Exception as e:
                            logger.error(f"[旁观采集] 写入 CSV 失败（跳过本颗，不中断采集）: {e}")
                    last_count = count
                    last_new_ts = now

                prev_snapshot = snapshot
                time.sleep(poll_interval)
        finally:
            self._observer_running = False

        end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("=" * 60)
        logger.info(f"[旁观采集] 结束，停止原因={stop_reason}，共采集 {collected} 颗，漏采 {missed} 颗")
        logger.info("=" * 60)

        return {
            "success": True,
            "stop_reason": stop_reason,
            "collected": collected,
            "missed": missed,
            "csv_assembly_file": assembly_file,
            "csv_neware_file": neware_file,
            "start_time": start_time,
            "end_time": end_time,
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
    workstation = CoinCellAssemblyWorkstation(deck=yihua_coin_cell_deck(name="coin_cell_deck"))
    # print(f"工作站创建成功: {workstation.deck.name}")
    # print(f"料盘数量: {len(workstation.deck.children)}")
    workstation.coin_cell_init()
    workstation.coin_cell_start(elec_num=16, elec_use_num=16, elec_vol=50, assembly_type=7, assembly_pressure=4200, file_path="/Users/calvincao/Desktop/work/Uni-Lab-OS-hhm")
    