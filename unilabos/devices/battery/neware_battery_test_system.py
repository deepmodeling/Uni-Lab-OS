#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
新威电池测试系统设备类
- 提供TCP通信接口查询电池通道状态
- 支持720个通道（devid 1-7, 8, 86）
- 兼容BTSAPI getchlstatus协议

设备特点：
- TCP连接: 默认127.0.0.1:502
- 通道映射: devid->subdevid->chlid 三级结构
- 状态类型: working/stop/finish/protect/pause/false/unknown
"""

import socket
import xml.etree.ElementTree as ET
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict

from pylabrobot.resources import ResourceHolder, Coordinate, create_ordered_items_2d, Deck, Plate

from unilabos.device_runtime.node import DeviceNode


# ========================
# 内部数据类和结构
# ========================

@dataclass(frozen=True)
class ChannelKey:
    devid: int
    subdevid: int
    chlid: int


@dataclass
class ChannelStatus:
    state: str  # working/stop/finish/protect/pause/false/unknown
    color: str  # 状态对应颜色
    current_A: float  # 电流 (A)
    voltage_V: float  # 电压 (V)
    totaltime_s: float  # 总时间 (s)


class BatteryTestPositionState(TypedDict):
    voltage: float  # 电压 (V)
    current: float  # 电流 (A)
    time: float  # 时间 (s) - 使用totaltime
    capacity: float  # 容量 (Ah)
    energy: float  # 能量 (Wh)

    status: str  # 通道状态
    color: str  # 状态对应颜色

    # 额外的inquire协议字段
    relativetime: float  # 相对时间 (s)
    open_or_close: int  # 0=关闭, 1=打开
    step_type: str  # 步骤类型
    cycle_id: int  # 循环ID
    step_id: int  # 步骤ID
    log_code: str  # 日志代码


class BatteryTestPosition(ResourceHolder):
    def __init__(
            self,
            name,
            size_x=60,
            size_y=60,
            size_z=60,
            rotation=None,
            category="resource_holder",
            model=None,
            child_location: Coordinate = Coordinate.zero(),
    ):
        super().__init__(name, size_x, size_y, size_z, rotation, category, model, child_location=child_location)
        self._unilabos_state: Dict[str, Any] = {}

    def load_state(self, state: Dict[str, Any]) -> None:
        """格式不变"""
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(self._unilabos_state)
        return data


class NewareBatteryTestSystem:
    """
    新威电池测试系统设备类
    
    提供电池测试通道状态查询、控制等功能。
    支持720个通道的状态监控和数据导出。
    包含完整的物料管理系统，支持2盘电池的状态映射。
    
    Attributes:
        ip (str): TCP服务器IP地址，默认127.0.0.1
        port (int): TCP端口，默认502
        devtype (str): 设备类型，默认"27"
        timeout (int): 通信超时时间（秒），默认20
    """
    
    # ========================
    # 基本通信与协议参数
    # ========================
    BTS_IP = "127.0.0.1"
    BTS_PORT = 502
    DEVTYPE = "27"
    TIMEOUT = 20  # 秒
    REQ_END = b"#\r\n"  # 常见实现以 "#\\r\\n" 作为报文结束
    
    # ========================
    # 状态与颜色映射（前端可直接使用）
    # ========================
    STATUS_SET = {"working", "stop", "finish", "protect", "pause", "false"}
    STATUS_COLOR = {
        "working": "#22c55e",  # 绿
        "stop":    "#6b7280",  # 灰
        "finish":  "#3b82f6",  # 蓝
        "protect": "#ef4444",  # 红
        "pause":   "#f59e0b",  # 橙
        "false":   "#9ca3af",  # 不存在/无效
        "unknown": "#a855f7",  # 未知
    }
    
    # 字母常量
    ascii_lowercase = 'abcdefghijklmnopqrstuvwxyz'
    ascii_uppercase = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    LETTERS = ascii_uppercase + ascii_lowercase

    def __init__(self, 
        ip: str = None, 
        port: int = None, 
        machine_id: int = 1,
        devtype: str = None, 
        timeout: int = None,
        
        size_x: float = 500.0,
        size_y: float = 500.0,
        size_z: float = 2000.0,
    ):
        """
        初始化新威电池测试系统
        
        Args:
            ip: TCP服务器IP地址
            port: TCP端口
            devtype: 设备类型标识
            timeout: 通信超时时间（秒）
            machine_id: 机器ID
            size_x, size_y, size_z: 设备物理尺寸
        """
        self.ip = ip or self.BTS_IP
        self.port = port or self.BTS_PORT
        self.machine_id = machine_id
        self.devtype = devtype or self.DEVTYPE
        self.timeout = timeout or self.TIMEOUT
        self._last_status_update = None
        self._cached_status = {}
        self._ros_node: Optional[DeviceNode] = None  # 运行时节点引用，由框架设置


    def post_init(self, ros_node: DeviceNode):
        """
        ROS节点初始化后的回调方法，用于建立设备连接
        
        Args:
            ros_node: ROS节点实例
        """
        self._ros_node = ros_node
        # 创建2盘电池的物料管理系统
        self._setup_material_management()
        # 初始化通道映射
        self._channels = self._build_channel_map()
        try:
            # 测试设备连接
            if self.test_connection():
                ros_node.lab_logger().info(f"新威电池测试系统连接成功: {self.ip}:{self.port}")
            else:
                ros_node.lab_logger().warning(f"新威电池测试系统连接失败: {self.ip}:{self.port}")
        except Exception as e:
            ros_node.lab_logger().error(f"新威电池测试系统初始化失败: {e}")
            # 不抛出异常，允许节点继续运行，后续可以重试连接

    def _setup_material_management(self):
        """设置物料管理系统"""
        # 第1盘：5行8列网格 (A1-E8) - 5行对应subdevid 1-5，8列对应chlid 1-8
        # 先给物料设置一个最大的Deck
        deck_main = Deck("ADeckName", 200, 200, 200)

        plate1_resources: Dict[str, BatteryTestPosition] = create_ordered_items_2d(
            BatteryTestPosition,
            num_items_x=8,  # 8列（对应chlid 1-8）
            num_items_y=5,  # 5行（对应subdevid 1-5，即A-E）
            dx=10,
            dy=10,
            dz=0,
            item_dx=45,
            item_dy=45
        )
        plate1 = Plate("P1", 400, 300, 50, ordered_items=plate1_resources)
        deck_main.assign_child_resource(plate1, location=Coordinate(0, 0, 0))
        
        # 只有在真实ROS环境下才调用update_resource
        if hasattr(self._ros_node, 'update_resource') and callable(getattr(self._ros_node, 'update_resource')):
            try:
                self._ros_node.create_task(
                    self._ros_node.update_resource([deck_main])
                )
            except Exception as e:
                if hasattr(self._ros_node, 'lab_logger'):
                    self._ros_node.lab_logger().warning(f"更新资源失败: {e}")
                # 在非ROS环境下忽略此错误
        
        # 为第1盘资源添加P1_前缀
        self.station_resources_plate1 = {}
        for name, resource in plate1_resources.items():
            new_name = f"P1_{name}"
            self.station_resources_plate1[new_name] = resource

        # 第2盘：5行8列网格 (A1-E8)，在Z轴上偏移 - 5行对应subdevid 6-10，8列对应chlid 1-8
        plate2_resources = create_ordered_items_2d(
            BatteryTestPosition,
            num_items_x=8,  # 8列（对应chlid 1-8）
            num_items_y=5,  # 5行（对应subdevid 6-10，即A-E）
            dx=10,
            dy=10,
            dz=100,  # Z轴偏移100mm
            item_dx=65,
            item_dy=65
        )

        # 为第2盘资源添加P2_前缀
        self.station_resources_plate2 = {}
        for name, resource in plate2_resources.items():
            new_name = f"P2_{name}"
            self.station_resources_plate2[new_name] = resource

        # 合并两盘资源为统一的station_resources
        self.station_resources = {}
        self.station_resources.update(self.station_resources_plate1)
        self.station_resources.update(self.station_resources_plate2)

    # ========================
    # 核心属性（Uni-Lab标准）
    # ========================
    
    @property
    def status(self) -> str:
        """设备状态属性 - 会被自动识别并定时广播"""
        try:
            if self.test_connection():
                return "Connected"
            else:
                return "Disconnected"
        except:
            return "Error"
    
    @property
    def channel_status(self) -> Dict[int, Dict]:
        """
        获取所有通道状态（按设备ID分组）
        
        这个属性会执行实际的TCP查询并返回格式化的状态数据。
        结果按设备ID分组，包含统计信息和详细状态。
        
        Returns:
            Dict[int, Dict]: 按设备ID分组的通道状态统计
        """
        status_map = self._query_all_channels()
        status_processed = {} if not status_map else self._group_by_devid(status_map)
        
        # 修复数据过滤逻辑：如果machine_id对应的数据不存在，尝试使用第一个可用的设备数据
        status_current_machine = status_processed.get(self.machine_id, {})
        
        if not status_current_machine and status_processed:
            # 如果machine_id没有匹配到数据，使用第一个可用的设备数据
            first_devid = next(iter(status_processed.keys()))
            status_current_machine = status_processed[first_devid]
            if self._ros_node:
                self._ros_node.lab_logger().warning(
                    f"machine_id {self.machine_id} 没有匹配到数据，使用设备ID {first_devid} 的数据"
                )
        
        # 确保有默认的数据结构
        if not status_current_machine:
            status_current_machine = {
                "stats": {s: 0 for s in self.STATUS_SET | {"unknown"}},
                "subunits": {}
            }
        
        # 确保subunits存在
        subunits = status_current_machine.get("subunits", {})
        
        # 处理2盘电池的状态映射
        self._update_plate_resources(subunits)
        
        return status_current_machine

    def _update_plate_resources(self, subunits: Dict):
        """更新两盘电池资源的状态"""
        # 第1盘：subdevid 1-5 映射到 P1_A1-P1_E8 (5行8列)
        for subdev_id in range(1, 6):  # subdevid 1-5
            status_row = subunits.get(subdev_id, {})
            
            for chl_id in range(1, 9):  # chlid 1-8
                try:
                    # 计算在5×8网格中的位置
                    row_idx = (subdev_id - 1)  # 0-4 (对应A-E)
                    col_idx = (chl_id - 1)     # 0-7 (对应1-8)
                    resource_name = f"P1_{self.LETTERS[row_idx]}{col_idx + 1}"
                    
                    r = self.station_resources.get(resource_name)
                    if r:
                        status_channel = status_row.get(chl_id, {})
                        channel_state = {
                            "status": status_channel.get("state", "unknown"),
                            "color": status_channel.get("color", self.STATUS_COLOR["unknown"]),
                            "voltage": status_channel.get("voltage_V", 0.0),
                            "current": status_channel.get("current_A", 0.0),
                            "time": status_channel.get("totaltime_s", 0.0),
                        }
                        r.load_state(channel_state)
                except (KeyError, IndexError):
                    continue
        
        # 第2盘：subdevid 6-10 映射到 P2_A1-P2_E8 (5行8列)
        for subdev_id in range(6, 11):  # subdevid 6-10
            status_row = subunits.get(subdev_id, {})
            
            for chl_id in range(1, 9):  # chlid 1-8
                try:
                    # 计算在5×8网格中的位置
                    row_idx = (subdev_id - 6)  # 0-4 (subdevid 6->0, 7->1, ..., 10->4) (对应A-E)
                    col_idx = (chl_id - 1)     # 0-7 (对应1-8)
                    resource_name = f"P2_{self.LETTERS[row_idx]}{col_idx + 1}"
                    
                    r = self.station_resources.get(resource_name)
                    if r:
                        status_channel = status_row.get(chl_id, {})
                        channel_state = {
                            "status": status_channel.get("state", "unknown"),
                            "color": status_channel.get("color", self.STATUS_COLOR["unknown"]),
                            "voltage": status_channel.get("voltage_V", 0.0),
                            "current": status_channel.get("current_A", 0.0),
                            "time": status_channel.get("totaltime_s", 0.0),
                        }
                        r.load_state(channel_state)
                except (KeyError, IndexError):
                    continue

    @property
    def connection_info(self) -> Dict[str, str]:
        """获取连接信息"""
        return {
            "ip": self.ip,
            "port": str(self.port),
            "devtype": self.devtype,
            "timeout": f"{self.timeout}s"
        }
    
    @property
    def total_channels(self) -> int:
        """获取总通道数"""
        return len(self._channels)

    # ========================
    # 设备动作方法（Uni-Lab标准）
    # ========================
    
    def export_status_json(self, filepath: str = "bts_status.json") -> dict:
        """
        导出当前状态到JSON文件（ROS2动作）
        
        Args:
            filepath: 输出文件路径
            
        Returns:
            dict: ROS2动作结果格式 {"return_info": str, "success": bool}
        """
        try:
            grouped_status = self.channel_status
            payload = {
                "timestamp": time.time(),
                "device_info": {
                    "ip": self.ip,
                    "port": self.port,
                    "devtype": self.devtype,
                    "total_channels": self.total_channels
                },
                "data": grouped_status,
                "color_mapping": self.STATUS_COLOR
            }
            
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            
            success_msg = f"状态数据已成功导出到: {filepath}"
            if self._ros_node:
                self._ros_node.lab_logger().info(success_msg)
            return {"return_info": success_msg, "success": True}
            
        except Exception as e:
            error_msg = f"导出JSON失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {"return_info": error_msg, "success": False}

    @property
    def plate_status(self) -> Dict[str, Any]:
        """
        获取所有盘的状态信息（属性）
        
        Returns:
            包含所有盘状态信息的字典
        """
        try:
            # 确保先更新所有资源的状态数据
            _ = self.channel_status  # 这会触发状态更新并调用load_state
            
            # 手动计算两盘的状态，避免调用需要参数的get_plate_status方法
            plate1_stats = {s: 0 for s in self.STATUS_SET | {"unknown"}}
            plate1_active = []
            
            for name, resource in self.station_resources_plate1.items():
                state = getattr(resource, '_unilabos_state', {})
                status = state.get('status', 'unknown')
                plate1_stats[status] += 1
                
                if status != 'unknown':
                    plate1_active.append({
                        'name': name,
                        'status': status,
                        'color': state.get('color', self.STATUS_COLOR['unknown']),
                        'voltage': state.get('voltage', 0.0),
                        'current': state.get('current', 0.0),
                    })
            
            plate2_stats = {s: 0 for s in self.STATUS_SET | {"unknown"}}
            plate2_active = []
            
            for name, resource in self.station_resources_plate2.items():
                state = getattr(resource, '_unilabos_state', {})
                status = state.get('status', 'unknown')
                plate2_stats[status] += 1
                
                if status != 'unknown':
                    plate2_active.append({
                        'name': name,
                        'status': status,
                        'color': state.get('color', self.STATUS_COLOR['unknown']),
                        'voltage': state.get('voltage', 0.0),
                        'current': state.get('current', 0.0),
                    })
            
            return {
                "plate1": {
                    'plate_num': 1,
                    'stats': plate1_stats,
                    'total_positions': len(self.station_resources_plate1),
                    'active_positions': len(plate1_active),
                    'resources': plate1_active
                },
                "plate2": {
                    'plate_num': 2,
                    'stats': plate2_stats,
                    'total_positions': len(self.station_resources_plate2),
                    'active_positions': len(plate2_active),
                    'resources': plate2_active
                },
                "total_plates": 2
            }
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"获取盘状态失败: {e}")
            return {
                "plate1": {"error": str(e)},
                "plate2": {"error": str(e)},
                "total_plates": 2
            }





    # ========================
    # 辅助方法
    # ========================
    
    def test_connection(self) -> bool:
        """
        测试TCP连接是否正常
        
        Returns:
            bool: 连接是否成功
        """
        try:
            with socket.create_connection((self.ip, self.port), timeout=5) as sock:
                return True
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().debug(f"连接测试失败: {e}")
            return False

    def print_status_summary(self) -> None:
        """
        打印通道状态摘要信息（支持2盘电池）
        """
        try:
            status_data = self.channel_status
            if not status_data:
                print("   未获取到状态数据")
                return
                
            print(f"   状态统计:")
            total_channels = 0
            
            # 从channel_status获取stats字段
            stats = status_data.get("stats", {})
            for state, count in stats.items():
                if isinstance(count, int) and count > 0:
                    color = self.STATUS_COLOR.get(state, "#000000")
                    print(f"     {state}: {count} 个通道 ({color})")
                    total_channels += count
            
            print(f"   总计: {total_channels} 个通道")
            print(f"   第1盘资源数: {len(self.station_resources_plate1)}")
            print(f"   第2盘资源数: {len(self.station_resources_plate2)}")
            print(f"   总资源数: {len(self.station_resources)}")
                    
        except Exception as e:
            print(f"   获取状态失败: {e}")

    def get_device_summary(self) -> dict:
        """
        获取设备级别的摘要统计（设备动作）
        
        Returns:
            dict: ROS2动作结果格式 {"return_info": str, "success": bool}
        """
        try:
            # 确保_channels已初始化
            if not hasattr(self, '_channels') or not self._channels:
                self._channels = self._build_channel_map()
            
            summary = {}
            for channel in self._channels:
                devid = channel.devid
                summary[devid] = summary.get(devid, 0) + 1
            
            result_info = json.dumps(summary, ensure_ascii=False)
            success_msg = f"设备摘要统计: {result_info}"
            if self._ros_node:
                self._ros_node.lab_logger().info(success_msg)
            return {"return_info": result_info, "success": True}
            
        except Exception as e:
            error_msg = f"获取设备摘要失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {"return_info": error_msg, "success": False}

    def test_connection_action(self) -> dict:
        """
        测试TCP连接（设备动作）
        
        Returns:
            dict: ROS2动作结果格式 {"return_info": str, "success": bool}
        """
        try:
            is_connected = self.test_connection()
            if is_connected:
                success_msg = f"TCP连接测试成功: {self.ip}:{self.port}"
                if self._ros_node:
                    self._ros_node.lab_logger().info(success_msg)
                return {"return_info": success_msg, "success": True}
            else:
                error_msg = f"TCP连接测试失败: {self.ip}:{self.port}"
                if self._ros_node:
                    self._ros_node.lab_logger().warning(error_msg)
                return {"return_info": error_msg, "success": False}
                
        except Exception as e:
            error_msg = f"连接测试异常: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {"return_info": error_msg, "success": False}

    def print_status_summary_action(self) -> dict:
        """
        打印状态摘要（设备动作）
        
        Returns:
            dict: ROS2动作结果格式 {"return_info": str, "success": bool}
        """
        try:
            self.print_status_summary()
            success_msg = "状态摘要已打印到控制台"
            if self._ros_node:
                self._ros_node.lab_logger().info(success_msg)
            return {"return_info": success_msg, "success": True}
            
        except Exception as e:
            error_msg = f"打印状态摘要失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {"return_info": error_msg, "success": False}

    def query_plate_action(self, plate_id: str = "P1") -> dict:
        """
        查询指定盘的详细信息（设备动作）
        
        Args:
            plate_id: 盘号标识，如"P1"或"P2"
            
        Returns:
            dict: ROS2动作结果格式，包含指定盘的详细通道信息
        """
        try:
            # 解析盘号
            if plate_id.upper() == "P1":
                plate_num = 1
            elif plate_id.upper() == "P2":
                plate_num = 2
            else:
                error_msg = f"无效的盘号: {plate_id}，仅支持P1或P2"
                if self._ros_node:
                    self._ros_node.lab_logger().warning(error_msg)
                return {"return_info": error_msg, "success": False}
            
            # 获取指定盘的详细信息
            plate_detail = self._get_plate_detail_info(plate_num)
            
            success_msg = f"成功获取{plate_id}盘详细信息，包含{len(plate_detail['channels'])}个通道"
            if self._ros_node:
                self._ros_node.lab_logger().info(success_msg)
            
            return {
                "return_info": success_msg,
                "success": True,
                "plate_data": plate_detail
            }
            
        except Exception as e:
            error_msg = f"查询盘{plate_id}详细信息失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {"return_info": error_msg, "success": False}

    def _get_plate_detail_info(self, plate_num: int) -> dict:
        """
        获取指定盘的详细信息，包含设备ID、子设备ID、通道ID映射
        
        Args:
            plate_num: 盘号 (1 或 2)
            
        Returns:
            dict: 包含详细通道信息的字典
        """
        # 获取最新的通道状态数据
        channel_status_data = self.channel_status
        subunits = channel_status_data.get('subunits', {})
        
        if plate_num == 1:
            devid = 1
            subdevid_range = range(1, 6)  # 子设备ID 1-5
        elif plate_num == 2:
            devid = 1
            subdevid_range = range(6, 11)  # 子设备ID 6-10
        else:
            raise ValueError("盘号必须是1或2")
        
        channels = []
        
        # 直接从subunits数据构建通道信息，而不依赖资源状态
        for subdev_id in subdevid_range:
            status_row = subunits.get(subdev_id, {})
            
            for chl_id in range(1, 9):  # chlid 1-8
                try:
                    # 计算在5×8网格中的位置
                    if plate_num == 1:
                        row_idx = (subdev_id - 1)  # 0-4 (对应A-E)
                    else:  # plate_num == 2
                        row_idx = (subdev_id - 6)  # 0-4 (subdevid 6->0, 7->1, ..., 10->4) (对应A-E)
                    
                    col_idx = (chl_id - 1)     # 0-7 (对应1-8)
                    position = f"{self.LETTERS[row_idx]}{col_idx + 1}"
                    name = f"P{plate_num}_{position}"
                    
                    # 从subunits直接获取通道状态数据
                    status_channel = status_row.get(chl_id, {})
                    
                    # 提取metrics数据（如果存在）
                    metrics = status_channel.get('metrics', {})
                    
                    channel_info = {
                        'name': name,
                        'devid': devid,
                        'subdevid': subdev_id,
                        'chlid': chl_id,
                        'position': position,
                        'status': status_channel.get('state', 'unknown'),
                        'color': status_channel.get('color', self.STATUS_COLOR['unknown']),
                        'voltage': metrics.get('voltage_V', 0.0),
                        'current': metrics.get('current_A', 0.0),
                        'time': metrics.get('totaltime_s', 0.0)
                    }
                    
                    channels.append(channel_info)
                    
                except (ValueError, IndexError, KeyError):
                    # 如果解析失败，跳过该通道
                    continue
        
        # 按位置排序（先按行，再按列）
        channels.sort(key=lambda x: (x['subdevid'], x['chlid']))
        
        # 统计状态
        stats = {s: 0 for s in self.STATUS_SET | {"unknown"}}
        for channel in channels:
            stats[channel['status']] += 1
        
        return {
            'plate_id': f"P{plate_num}",
            'plate_num': plate_num,
            'devid': devid,
            'subdevid_range': list(subdevid_range),
            'total_channels': len(channels),
            'stats': stats,
            'channels': channels
        }

    # ========================
    # TCP通信和协议处理
    # ========================
    
    def _build_channel_map(self) -> List['ChannelKey']:
        """构建全量通道映射（720个通道）"""
        channels = []
        
        # devid 1-7: subdevid 1-10, chlid 1-8
        for devid in range(1, 8):
            for sub in range(1, 11):
                for ch in range(1, 9):
                    channels.append(ChannelKey(devid, sub, ch))
        
        # devid 8: subdevid 11-20, chlid 1-8
        for sub in range(11, 21):
            for ch in range(1, 9):
                channels.append(ChannelKey(8, sub, ch))
        
        # devid 86: subdevid 1-10, chlid 1-8
        for sub in range(1, 11):
            for ch in range(1, 9):
                channels.append(ChannelKey(86, sub, ch))
                
        return channels

    def _query_all_channels(self) -> Dict['ChannelKey', dict]:
        """执行TCP查询获取所有通道状态"""
        try:
            req_xml = self._build_inquire_xml()
            
            with socket.create_connection((self.ip, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(req_xml)
                response = self._recv_until(sock)
                
            return self._parse_inquire_resp(response)
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"查询通道状态失败: {e}")
            else:
                print(f"查询通道状态失败: {e}")
            return {}

    def _build_inquire_xml(self) -> bytes:
        """构造inquire请求XML"""
        lines = [
            '<?xml version="1.0" encoding="UTF-8" ?>',
            '<bts version="1.0">',
            '<cmd>inquire</cmd>',
            f'<list count="{len(self._channels)}">'
        ]
        
        for c in self._channels:
            lines.append(
                f'<inquire ip="{self.ip}" devtype="{self.devtype}" '
                f'devid="{c.devid}" subdevid="{c.subdevid}" chlid="{c.chlid}" '
                f'aux="0" barcode="0">true</inquire>'
            )
        
        lines.extend(['</list>', '</bts>'])
        xml_text = "\n".join(lines)
        return xml_text.encode("utf-8") + self.REQ_END

    def _recv_until(self, sock: socket.socket, end_token: bytes = None, 
                   alt_close_tag: bytes = b"</bts>") -> bytes:
        """接收TCP响应数据"""
        if end_token is None:
            end_token = self.REQ_END
            
        buf = bytearray()
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf.extend(chunk)
            if end_token in buf:
                cut = buf.rfind(end_token)
                return bytes(buf[:cut])
            if alt_close_tag in buf:
                cut = buf.rfind(alt_close_tag) + len(alt_close_tag)
                return bytes(buf[:cut])
        return bytes(buf)

    def _parse_inquire_resp(self, xml_bytes: bytes) -> Dict['ChannelKey', dict]:
        """解析inquire_resp响应XML"""
        mapping = {}
        
        try:
            xml_text = xml_bytes.decode("utf-8", errors="ignore").strip()
            if not xml_text:
                return mapping
                
            root = ET.fromstring(xml_text)
            cmd = root.findtext("cmd", default="").strip()
            
            if cmd != "inquire_resp":
                return mapping
                
            list_node = root.find("list")
            if list_node is None:
                return mapping
                
            for node in list_node.findall("inquire"):
                # 解析 dev="27-1-1-1-0"
                dev = node.get("dev", "")
                parts = dev.split("-")
                # 容错：至少需要 5 段
                if len(parts) < 5:
                    continue
                try:
                    devtype = int(parts[0])   # 未使用，但解析以校验正确性
                    devid = int(parts[1])
                    subdevid = int(parts[2])
                    chlid = int(parts[3])
                    aux = int(parts[4])
                except ValueError:
                    continue

                key = ChannelKey(devid, subdevid, chlid)

                # 提取属性，带类型转换与缺省值
                def fget(name: str, cast, default):
                    v = node.get(name)
                    if v is None or v == "":
                        return default
                    try:
                        return cast(v)
                    except Exception:
                        return default

                workstatus = (node.get("workstatus", "") or "").lower()
                if workstatus not in self.STATUS_SET:
                    workstatus = "unknown"

                current = fget("current", float, 0.0)
                voltage = fget("voltage", float, 0.0)
                capacity = fget("capacity", float, 0.0)
                energy = fget("energy", float, 0.0)
                totaltime = fget("totaltime", float, 0.0)
                relativetime = fget("relativetime", float, 0.0)
                open_close = fget("open_or_close", int, 0)
                cycle_id = fget("cycle_id", int, 0)
                step_id = fget("step_id", int, 0)
                step_type = node.get("step_type", "") or ""
                log_code = node.get("log_code", "") or ""
                barcode = node.get("barcode")

                mapping[key] = {
                    "state": workstatus,
                    "color": self.STATUS_COLOR.get(workstatus, self.STATUS_COLOR["unknown"]),
                    "current_A": current,
                    "voltage_V": voltage,
                    "capacity_Ah": capacity,
                    "energy_Wh": energy,
                    "totaltime_s": totaltime,
                    "relativetime_s": relativetime,
                    "open_or_close": open_close,
                    "step_type": step_type,
                    "cycle_id": cycle_id,
                    "step_id": step_id,
                    "log_code": log_code,
                    **({"barcode": barcode} if barcode is not None else {}),
                }
                
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"解析XML响应失败: {e}")
            else:
                print(f"解析XML响应失败: {e}")
            
        return mapping

    def _group_by_devid(self, status_map: Dict['ChannelKey', dict]) -> Dict[int, Dict]:
        """按设备ID分组状态数据"""
        result = {}
        
        for key, val in status_map.items():
            if key.devid not in result:
                result[key.devid] = {
                    "stats": {s: 0 for s in self.STATUS_SET | {"unknown"}},
                    "subunits": {}
                }
            
            dev = result[key.devid]
            state = val.get("state", "unknown")
            dev["stats"][state] = dev["stats"].get(state, 0) + 1
            
            subunits = dev["subunits"]
            if key.subdevid not in subunits:
                subunits[key.subdevid] = {}
            
            subunits[key.subdevid][key.chlid] = {
                "state": state,
                "color": val.get("color", self.STATUS_COLOR["unknown"]),
                "open_or_close": val.get("open_or_close", 0),
                "metrics": {
                    "voltage_V": val.get("voltage_V", 0.0),
                    "current_A": val.get("current_A", 0.0),
                    "capacity_Ah": val.get("capacity_Ah", 0.0),
                    "energy_Wh": val.get("energy_Wh", 0.0),
                    "totaltime_s": val.get("totaltime_s", 0.0),
                    "relativetime_s": val.get("relativetime_s", 0.0)
                },
                "meta": {
                    "step_type": val.get("step_type", ""),
                    "cycle_id": val.get("cycle_id", 0),
                    "step_id": val.get("step_id", 0),
                    "log_code": val.get("log_code", "")
                }
            }
            
        return result


# ========================
# 示例和测试代码
# ========================
def main():
    """测试和演示设备类的使用（支持2盘80颗电池）"""
    print("=== 新威电池测试系统设备类演示（2盘80颗电池） ===")
    
    # 创建设备实例
    bts = NewareBatteryTestSystem()
    
    # 创建一个模拟的ROS节点用于初始化
    class MockRosNode:
        def lab_logger(self):
            import logging
            return logging.getLogger(__name__)
        
        def update_resource(self, *args, **kwargs):
            pass  # 空实现，避免ROS调用错误
    
    # 调用post_init进行正确的初始化
    mock_ros_node = MockRosNode()
    bts.post_init(mock_ros_node)
    
    # 测试连接
    print(f"\n1. 连接测试:")
    print(f"   连接信息: {bts.connection_info}")
    if bts.test_connection():
        print("   ✓ TCP连接正常")
    else:
        print("   ✗ TCP连接失败")
        return
    
    # 获取设备摘要
    print(f"\n2. 设备摘要:")
    print(f"   总通道数: {bts.total_channels}")
    summary_result = bts.get_device_summary()
    if summary_result["success"]:
        # 直接解析return_info，因为它就是JSON字符串
        summary = json.loads(summary_result["return_info"])
        for devid, count in summary.items():
            print(f"   设备ID {devid}: {count} 个通道")
    else:
        print(f"   获取设备摘要失败: {summary_result['return_info']}")
    
    # 显示物料管理系统信息
    print(f"\n3. 物料管理系统:")
    print(f"   第1盘资源数: {len(bts.station_resources_plate1)}")
    print(f"   第2盘资源数: {len(bts.station_resources_plate2)}")
    print(f"   总资源数: {len(bts.station_resources)}")
    
    # 获取实时状态
    print(f"\n4. 获取通道状态:")
    try:
        bts.print_status_summary()
    except Exception as e:
        print(f"   获取状态失败: {e}")
    
    # 分别获取两盘的状态
    print(f"\n5. 分盘状态统计:")
    try:
        plate_status_data = bts.plate_status
        for plate_num in [1, 2]:
            plate_key = f"plate{plate_num}"  # 修正键名格式：plate1, plate2
            if plate_key in plate_status_data:
                plate_info = plate_status_data[plate_key]
                print(f"   第{plate_num}盘:")
                print(f"     总位置数: {plate_info['total_positions']}")
                print(f"     活跃位置数: {plate_info['active_positions']}")
                for state, count in plate_info['stats'].items():
                    if count > 0:
                        print(f"     {state}: {count} 个位置")
            else:
                print(f"   第{plate_num}盘: 无数据")
    except Exception as e:
        print(f"   获取分盘状态失败: {e}")
    
    # 导出JSON
    print(f"\n6. 导出状态数据:")
    result = bts.export_status_json("demo_2plate_status.json")
    if result["success"]:
        print("   ✓ 状态数据已导出到 demo_2plate_status.json")
    else:
        print("   ✗ 导出失败")


if __name__ == "__main__":
    main()
