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

import os
import sys
import socket
import xml.etree.ElementTree as ET
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict

from pylabrobot.resources import ResourceHolder, Coordinate, create_ordered_items_2d, Deck, Plate
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode

# ========================
# OSS 上传工具函数
# ========================

import requests

# 服务器地址和OSS配置
OSS_PUBLIC_HOST = "uni-lab-test.oss-cn-zhangjiakou.aliyuncs.com"

def get_upload_token(base_url, auth_token, scene, filename):
    """
    获取文件上传的预签名URL
    
    Args:
        base_url: API服务器地址
        auth_token: 认证Token (JWT)，需要包含 "Bearer " 前缀
        scene: 上传场景 (例如: "job")
        filename: 文件名
        
    Returns:
        dict: 包含上传URL和路径的字典，失败返回None
    """
    url = f"{base_url}/api/v1/lab/storage/token"
    params = {
        "scene": scene,
        "filename": filename,
        "path": "neware_backup",  # 添加 path 参数
    }
    headers = {
        "Authorization": auth_token,
    }

    print(f"正在从 {url} 获取上传凭证...")
    try:
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()

        data = response.json()
        if data.get("code") == 0 and "data" in data and "url" in data["data"]:
            print("成功获取上传凭证!")
            return data["data"]
        else:
            print(f"获取凭证失败: {data.get('msg', '未知错误')}")
            return None

    except requests.exceptions.RequestException as e:
        print(f"请求上传凭证时发生错误: {e}")
        return None


def upload_file_with_presigned_url(upload_info, file_path):
    """
    使用预签名URL上传文件到OSS
    
    Args:
        upload_info: 包含上传URL的字典
        file_path: 本地文件路径
        
    Returns:
        bool: 上传是否成功
    """
    upload_url = upload_info['url']

    print(f"开始上传文件: {file_path} 到 {upload_url}")
    try:
        with open(file_path, 'rb') as f:
            file_data = f.read()
        response = requests.put(upload_url, data=file_data)
        response.raise_for_status()

        print("文件上传成功!")
        return True

    except FileNotFoundError:
        print(f"错误: 文件未找到 {file_path}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"文件上传失败: {e}")
        print(f"服务器响应: {e.response.text if e.response else '无响应'}")
        return False


def upload_file_to_oss(local_file_path, oss_object_name=None):
    """
    上传文件到阿里云OSS (使用统一API方式)
    
    Args:
        local_file_path: 本地文件路径
        oss_object_name: OSS对象名称 (暂时未使用，保留接口兼容性)
        
    Returns:
        bool or str: 上传成功返回文件访问URL，失败返回False
    """
    # 从环境变量获取配置
    base_url = os.getenv('UNI_LAB_BASE_URL', 'https://uni-lab.test.bohrium.com')
    auth_token = os.getenv('UNI_LAB_AUTH_TOKEN')
    upload_scene = os.getenv('UNI_LAB_UPLOAD_SCENE', 'job')  # 必须使用 job，其他值会被改成 default

    # 检查环境变量是否设置
    if not auth_token:
        raise ValueError("请设置环境变量: UNI_LAB_AUTH_TOKEN")

    # 确保 auth_token 包含正确的前缀
    # 支持两种格式: "Bearer xxx" (JWT) 或 "Api xxx" (API Key)
    if not auth_token.startswith("Bearer ") and not auth_token.startswith("Api "):
        # 默认使用 Api 格式
        auth_token = f"Api {auth_token}"

    # 检查文件是否存在
    if not os.path.exists(local_file_path):
        print(f"错误: 无法找到要上传的文件 '{local_file_path}'")
        return False

    filename = os.path.basename(local_file_path)

    # 1. 获取上传信息
    upload_info = get_upload_token(base_url, auth_token, upload_scene, filename)

    if not upload_info:
        print("无法继续上传，因为没有获取到有效的上传信息。")
        return False

    # 2. 上传文件
    success = upload_file_with_presigned_url(upload_info, local_file_path)

    if success:
        access_url = f"https://{OSS_PUBLIC_HOST}/{upload_info['path']}"
        print(f"文件访问URL: {access_url}")
        return access_url
    else:
        return False


def upload_files_to_oss(file_paths, oss_prefix=""):
    """
    批量上传文件到OSS
    
    Args:
        file_paths: 本地文件路径列表
        oss_prefix: OSS对象前缀 (暂时未使用，保留接口兼容性)
        
    Returns:
        int: 成功上传的文件数量
    """
    success_count = 0
    print(f"开始批量上传 {len(file_paths)} 个文件到OSS...")
    for i, fp in enumerate(file_paths, 1):
        print(f"[{i}/{len(file_paths)}] 上传文件: {fp}")
        try:
            result = upload_file_to_oss(fp)
            if result:
                success_count += 1
                print(f"[{i}/{len(file_paths)}] 上传成功")
            else:
                print(f"[{i}/{len(file_paths)}] 上传失败")
        except ValueError as e:
            print(f"[{i}/{len(file_paths)}] 环境变量错误: {e}")
            break
        except Exception as e:
            print(f"[{i}/{len(file_paths)}] 上传异常: {e}")
    print(f"批量上传完成: {success_count}/{len(file_paths)} 个文件成功")
    return success_count


def upload_directory_to_oss(local_dir, oss_prefix=""):
    """
    上传整个目录到OSS
    
    Args:
        local_dir: 本地目录路径
        oss_prefix: OSS对象前缀 (暂时未使用，保留接口兼容性)
    """
    for root, dirs, files in os.walk(local_dir):
        for file in files:
            local_file_path = os.path.join(root, file)
            upload_file_to_oss(local_file_path)


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
        
        size_x: float = 50,
        size_y: float = 50,
        size_z: float = 20,
        
        oss_upload_enabled: bool = False,
        oss_prefix: str = "neware_backup",
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
            oss_upload_enabled: 是否启用OSS上传功能，默认False
            oss_prefix: OSS对象路径前缀，默认"neware_backup"
        """
        self.ip = ip or self.BTS_IP
        self.port = port or self.BTS_PORT
        self.machine_id = machine_id
        self.devtype = devtype or self.DEVTYPE
        self.timeout = timeout or self.TIMEOUT
        
        # 存储设备物理尺寸
        self.size_x = size_x
        self.size_y = size_y
        self.size_z = size_z
        
        # OSS 上传配置
        self.oss_upload_enabled = oss_upload_enabled
        self.oss_prefix = oss_prefix
        
        self._last_status_update = None
        self._cached_status = {}
        self._last_backup_dir = None  # 记录最近一次的 backup_dir，供上传使用
        self._ros_node: Optional[ROS2WorkstationNode] = None  # ROS节点引用，由框架设置


    def post_init(self, ros_node):
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
        # 先给物料设置一个最大的Deck，并设置其在空间中的位置
        
        deck_main = Deck("ADeckName", 2000, 1800, 100, origin=Coordinate(2000,2000,0))

        plate1_resources: Dict[str, BatteryTestPosition] = create_ordered_items_2d(
            BatteryTestPosition,
            num_items_x=8,  # 8列（对应chlid 1-8）
            num_items_y=5,  # 5行（对应subdevid 1-5，即A-E）
            dx=10,
            dy=10,
            dz=0,
            item_dx=65,
            item_dy=65
        )
        plate1 = Plate("P1", 400, 300, 50, ordered_items=plate1_resources)
        deck_main.assign_child_resource(plate1, location=Coordinate(0, 0, 0))
        
        # 只有在真实ROS环境下才调用update_resource
        if hasattr(self._ros_node, 'update_resource') and callable(getattr(self._ros_node, 'update_resource')):
            try:
                ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
                    "resources": [deck_main]
                })
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
            dz=0,  
            item_dx=65,
            item_dy=65
        )

        plate2 = Plate("P2", 400, 300, 50, ordered_items=plate2_resources)
        deck_main.assign_child_resource(plate2, location=Coordinate(0, 350, 0))


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
        # 第1盘：subdevid 1-5 映射到 8列5行网格 (列0-7, 行0-4)
        for subdev_id in range(1, 6):  # subdevid 1-5
            status_row = subunits.get(subdev_id, {})
            
            for chl_id in range(1, 9):  # chlid 1-8
                try:
                    # 根据用户描述：第一个是(0,0)，最后一个是(7,4)
                    # 说明是8列5行，列从0开始，行从0开始
                    col_idx = (chl_id - 1)      # 0-7 (chlid 1-8 -> 列0-7)
                    row_idx = (subdev_id - 1)   # 0-4 (subdevid 1-5 -> 行0-4)
                    
                    # 尝试多种可能的资源命名格式
                    possible_names = [
                        f"P1_batterytestposition_{col_idx}_{row_idx}",  # 用户提到的格式
                        f"P1_{self.LETTERS[row_idx]}{col_idx + 1}",     # 原有的A1-E8格式
                        f"P1_{self.LETTERS[row_idx].lower()}{col_idx + 1}",  # 小写字母格式
                    ]
                    
                    r = None
                    resource_name = None
                    for name in possible_names:
                        if name in self.station_resources:
                            r = self.station_resources[name]
                            resource_name = name
                            break
                    
                    if r:
                        status_channel = status_row.get(chl_id, {})
                        metrics = status_channel.get("metrics", {})
                        # 构建BatteryTestPosition状态数据（移除capacity和energy）
                        channel_state = {
                            # 基本测量数据
                            "voltage": metrics.get("voltage_V", 0.0),
                            "current": metrics.get("current_A", 0.0),
                            "time": metrics.get("totaltime_s", 0.0),
                            
                            # 状态信息
                            "status": status_channel.get("state", "unknown"),
                            "color": status_channel.get("color", self.STATUS_COLOR["unknown"]),
                            
                            # 通道名称标识
                            "Channel_Name": f"{self.machine_id}-{subdev_id}-{chl_id}",

                        }
                        r.load_state(channel_state)
                  
                        # 调试信息
                        if self._ros_node and hasattr(self._ros_node, 'lab_logger'):
                            self._ros_node.lab_logger().debug(
                                f"更新P1资源状态: {resource_name} <- subdev{subdev_id}/chl{chl_id} "
                                f"状态:{channel_state['status']}"
                            )
                    else:
                        # 如果找不到资源，记录调试信息
                        if self._ros_node and hasattr(self._ros_node, 'lab_logger'):
                            self._ros_node.lab_logger().debug(
                                f"P1未找到资源: subdev{subdev_id}/chl{chl_id} -> 尝试的名称: {possible_names}"
                            )
                except (KeyError, IndexError) as e:
                    if self._ros_node and hasattr(self._ros_node, 'lab_logger'):
                        self._ros_node.lab_logger().debug(f"P1映射错误: subdev{subdev_id}/chl{chl_id} - {e}")
                    continue
        
        # 第2盘：subdevid 6-10 映射到 8列5行网格 (列0-7, 行0-4)
        for subdev_id in range(6, 11):  # subdevid 6-10
            status_row = subunits.get(subdev_id, {})
            
            for chl_id in range(1, 9):  # chlid 1-8
                try:
                    col_idx = (chl_id - 1)          # 0-7 (chlid 1-8 -> 列0-7)
                    row_idx = (subdev_id - 6)       # 0-4 (subdevid 6-10 -> 行0-4)
                    
                    # 尝试多种可能的资源命名格式
                    possible_names = [
                        f"P2_batterytestposition_{col_idx}_{row_idx}",  # 用户提到的格式
                        f"P2_{self.LETTERS[row_idx]}{col_idx + 1}",     # 原有的A1-E8格式
                        f"P2_{self.LETTERS[row_idx].lower()}{col_idx + 1}",  # 小写字母格式
                    ]
                    
                    r = None
                    resource_name = None
                    for name in possible_names:
                        if name in self.station_resources:
                            r = self.station_resources[name]
                            resource_name = name
                            break
                    
                    if r:
                        status_channel = status_row.get(chl_id, {})
                        metrics = status_channel.get("metrics", {})
                        # 构建BatteryTestPosition状态数据（移除capacity和energy）
                        channel_state = {
                            # 基本测量数据
                            "voltage": metrics.get("voltage_V", 0.0),
                            "current": metrics.get("current_A", 0.0),
                            "time": metrics.get("totaltime_s", 0.0),
                            
                            # 状态信息
                            "status": status_channel.get("state", "unknown"),
                            "color": status_channel.get("color", self.STATUS_COLOR["unknown"]),
                            
                            # 通道名称标识
                            "Channel_Name": f"{self.machine_id}-{subdev_id}-{chl_id}",
                            
                        }
                        r.load_state(channel_state)
                        
                        # 调试信息
                        if self._ros_node and hasattr(self._ros_node, 'lab_logger'):
                            self._ros_node.lab_logger().debug(
                                f"更新P2资源状态: {resource_name} <- subdev{subdev_id}/chl{chl_id} "
                                f"状态:{channel_state['status']}"
                            )
                    else:
                        # 如果找不到资源，记录调试信息
                        if self._ros_node and hasattr(self._ros_node, 'lab_logger'):
                            self._ros_node.lab_logger().debug(
                                f"P2未找到资源: subdev{subdev_id}/chl{chl_id} -> 尝试的名称: {possible_names}"
                            )
                except (KeyError, IndexError) as e:
                    if self._ros_node and hasattr(self._ros_node, 'lab_logger'):
                        self._ros_node.lab_logger().debug(f"P2映射错误: subdev{subdev_id}/chl{chl_id} - {e}")
                    continue
        ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
                    "resources": list(self.station_resources.values())
                })

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

    def _plate_status(self) -> Dict[str, Any]:
        """
        获取所有盘的状态信息（内部方法）
        
        Returns:
            包含所有盘状态信息的字典
        """
        try:
            # 确保先更新所有资源的状态数据
            _ = self.channel_status  # 这会触发状态更新并调用load_state
            
            # 手动计算两盘的状态
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






    def debug_resource_names(self) -> dict:
        """
        调试方法：显示所有资源的实际名称（ROS2动作）
        
        Returns:
            dict: ROS2动作结果格式，包含所有资源名称信息
        """
        try:
            debug_info = {
                "total_resources": len(self.station_resources),
                "plate1_resources": len(self.station_resources_plate1),
                "plate2_resources": len(self.station_resources_plate2),
                "plate1_names": list(self.station_resources_plate1.keys())[:10],  # 显示前10个
                "plate2_names": list(self.station_resources_plate2.keys())[:10],  # 显示前10个
                "all_resource_names": list(self.station_resources.keys())[:20],   # 显示前20个
            }
            
            # 检查是否有用户提到的命名格式
            batterytestposition_names = [name for name in self.station_resources.keys() 
                                       if "batterytestposition" in name]
            debug_info["batterytestposition_names"] = batterytestposition_names[:10]
            
            success_msg = f"资源调试信息获取成功，共{debug_info['total_resources']}个资源"
            if self._ros_node:
                self._ros_node.lab_logger().info(success_msg)
                self._ros_node.lab_logger().info(f"调试信息: {debug_info}")
            
            return {
                "return_info": success_msg,
                "success": True,
                "debug_data": debug_info
            }
            
        except Exception as e:
            error_msg = f"获取资源调试信息失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {"return_info": error_msg, "success": False}

    def get_plate_status(self, plate_num: int = None) -> dict:
        """
        获取指定盘或所有盘的状态信息（ROS2动作）
        
        Args:
            plate_num: 盘号 (1 或 2)，如果为None则返回所有盘的状态
            
        Returns:
            dict: ROS2动作结果格式 {"return_info": str, "success": bool, "plate_data": dict}
        """
        try:
            # 获取所有盘的状态
            all_plates_data = self._plate_status()
            
            # 如果指定了盘号，只返回该盘的数据
            if plate_num is not None:
                if plate_num not in [1, 2]:
                    error_msg = f"无效的盘号: {plate_num}，必须是 1 或 2"
                    if self._ros_node:
                        self._ros_node.lab_logger().error(error_msg)
                    return {
                        "return_info": error_msg,
                        "success": False,
                        "plate_data": {}
                    }
                
                plate_key = f"plate{plate_num}"
                plate_data = all_plates_data.get(plate_key, {})
                
                success_msg = f"成功获取盘 {plate_num} 的状态信息"
                if self._ros_node:
                    self._ros_node.lab_logger().info(success_msg)
                
                return {
                    "return_info": success_msg,
                    "success": True,
                    "plate_data": plate_data
                }
            else:
                # 返回所有盘的状态
                success_msg = "成功获取所有盘的状态信息"
                if self._ros_node:
                    self._ros_node.lab_logger().info(success_msg)
                
                return {
                    "return_info": success_msg,
                    "success": True,
                    "plate_data": all_plates_data
                }
                
        except Exception as e:
            error_msg = f"获取盘状态失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {
                "return_info": error_msg,
                "success": False,
                "plate_data": {}
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

    # ========================
    # CSV批量提交功能（新增）
    # ========================
    
    def _ensure_local_import_path(self):
        """确保本地模块导入路径"""
        base_dir = os.path.dirname(__file__)
        if base_dir not in sys.path:
            sys.path.insert(0, base_dir)
    
    def _canon(self, bs: str) -> str:
        """规范化电池体系名称"""
        return str(bs).strip().replace('-', '_').upper()
    
    def _compute_values(self, row):
        """
        计算活性物质质量和容量
        
        Args:
            row: DataFrame行数据
            
        Returns:
            tuple: (活性物质质量mg, 容量mAh)
        """
        pw = float(row['Pole_Weight'])
        cm = float(row['集流体质量'])
        am = row['活性物质含量']
        if isinstance(am, str) and am.endswith('%'):
            amv = float(am.rstrip('%')) / 100.0
        else:
            amv = float(am)
        act_mass = (pw - cm) * amv
        sc = float(row['克容量mah/g'])
        cap = act_mass * sc / 1000.0
        return round(act_mass, 2), round(cap, 3)
    
    def _get_xml_builder(self, gen_mod, key: str):
        """
        获取对应电池体系的XML生成函数
        
        Args:
            gen_mod: generate_xml_content模块
            key: 电池体系标识
            
        Returns:
            callable: XML生成函数
        """
        fmap = {
            'LB6': gen_mod.xml_LB6,
            'GR_LI': gen_mod.xml_Gr_Li,
            'LFP_LI': gen_mod.xml_LFP_Li,
            'LFP_GR': gen_mod.xml_LFP_Gr,
            '811_LI_002': gen_mod.xml_811_Li_002,
            '811_LI_005': gen_mod.xml_811_Li_005,
            'SIGR_LI_STEP': gen_mod.xml_SiGr_Li_Step,
            'SIGR_LI': gen_mod.xml_SiGr_Li_Step,
            '811_SIGR': gen_mod.xml_811_SiGr,
            '811_CU_AGING': gen_mod.xml_811_Cu_aging,
        }
        if key not in fmap:
            raise ValueError(f"未定义电池体系映射: {key}")
        return fmap[key]
    
    def _save_xml(self, xml: str, path: str):
        """
        保存XML文件
        
        Args:
            xml: XML内容
            path: 文件路径
        """
        with open(path, 'w', encoding='utf-8') as f:
            f.write(xml)
    
    def submit_from_csv(self, csv_path: str, output_dir: str = ".") -> dict:
        """
        从CSV文件批量提交Neware测试任务（设备动作）
        
        Args:
            csv_path (str): 输入CSV文件路径
            output_dir (str): 输出目录，用于存储XML文件和备份，默认当前目录
            
        Returns:
            dict: 执行结果 {"return_info": str, "success": bool, "submitted_count": int}
        """
        try:
            # 确保可以导入本地模块
            self._ensure_local_import_path()
            import pandas as pd
            import generate_xml_content as gen_mod
            from neware_driver import start_test
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"开始从CSV文件提交任务: {csv_path}")
            
            # 读取CSV文件
            if not os.path.exists(csv_path):
                error_msg = f"CSV文件不存在: {csv_path}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False, "submitted_count": 0, "total_count": 0}
            
            df = pd.read_csv(csv_path, encoding='gbk')
            
            # 验证必需列
            required = [
                'Battery_Code', 'Electrolyte_Code', 'Pole_Weight', '集流体质量', '活性物质含量', 
                '克容量mah/g', '电池体系', '设备号', '排号', '通道号'
            ]
            missing = [c for c in required if c not in df.columns]
            if missing:
                error_msg = f"CSV缺少必需列: {missing}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False, "submitted_count": 0, "total_count": 0}
            
            # 创建输出目录
            xml_dir = os.path.join(output_dir, 'xml_dir')
            backup_dir = os.path.join(output_dir, 'backup_dir')
            os.makedirs(xml_dir, exist_ok=True)
            os.makedirs(backup_dir, exist_ok=True)
            
            # 记录备份目录供后续 OSS 上传使用
            self._last_backup_dir = backup_dir
            
            if self._ros_node:
                self._ros_node.lab_logger().info(
                    f"输出目录: XML={xml_dir}, 备份={backup_dir}"
                )
            
            # 逐行处理CSV数据
            submitted_count = 0
            results = []
            
            for idx, row in df.iterrows():
                try:
                    coin_id = f"{row['Battery_Code']}-{row['Electrolyte_Code']}"
                    
                    # 计算活性物质质量和容量
                    act_mass, cap_mAh = self._compute_values(row)
                    
                    if cap_mAh < 0:
                        error_msg = (
                            f"容量为负数: Battery_Code={coin_id}, "
                            f"活性物质质量mg={act_mass}, 容量mah={cap_mAh}"
                        )
                        if self._ros_node:
                            self._ros_node.lab_logger().warning(error_msg)
                        results.append(f"行{idx+1} 失败: {error_msg}")
                        continue
                    
                    # 获取电池体系对应的XML生成函数
                    key = self._canon(row['电池体系'])
                    builder = self._get_xml_builder(gen_mod, key)
                    
                    # 生成XML内容
                    xml_content = builder(act_mass, cap_mAh)
                    
                    # 获取设备信息
                    devid = int(row['设备号'])
                    subdevid = int(row['排号'])
                    chlid = int(row['通道号'])
                    
                    # 保存XML文件
                    recipe_path = os.path.join(
                        xml_dir, 
                        f"{coin_id}_{devid}_{subdevid}_{chlid}.xml"
                    )
                    self._save_xml(xml_content, recipe_path)
                    
                    # 提交测试任务
                    resp = start_test(
                        ip=self.ip, 
                        port=self.port, 
                        devid=devid, 
                        subdevid=subdevid, 
                        chlid=chlid, 
                        CoinID=coin_id, 
                        recipe_path=recipe_path, 
                        backup_dir=backup_dir
                    )
                    
                    submitted_count += 1
                    results.append(f"行{idx+1} {coin_id}: {resp}")
                    
                    if self._ros_node:
                        self._ros_node.lab_logger().info(
                            f"已提交 {coin_id} (设备{devid}-{subdevid}-{chlid}): {resp}"
                        )
                
                except Exception as e:
                    error_msg = f"行{idx+1} 处理失败: {str(e)}"
                    results.append(error_msg)
                    if self._ros_node:
                        self._ros_node.lab_logger().error(error_msg)
            
            # 汇总结果
            success_msg = (
                f"批量提交完成: 成功{submitted_count}个，共{len(df)}行。"
                f"\n详细结果:\n" + "\n".join(results)
            )
            
            if self._ros_node:
                self._ros_node.lab_logger().info(
                    f"批量提交完成: 成功{submitted_count}/{len(df)}"
                )
            
            return {
                "return_info": success_msg,
                "success": True,
                "submitted_count": submitted_count,
                "total_count": len(df),
                "results": results
            }
        
        except Exception as e:
            error_msg = f"批量提交失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {
                "return_info": error_msg, 
                "success": False, 
                "submitted_count": 0,
                "total_count": 0
            }


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

    def upload_backup_to_oss(
        self, 
        backup_dir: str = None,
        file_pattern: str = "*",
        oss_prefix: str = None
    ) -> dict:
        """
        上传备份目录中的文件到 OSS（ROS2 动作）
        
        Args:
            backup_dir: 备份目录路径，默认使用最近一次 submit_from_csv 的 backup_dir
            file_pattern: 文件通配符模式，默认 "*" 上传所有文件（例如 "*.csv" 仅上传 CSV 文件）
            oss_prefix: OSS 对象前缀，默认使用类初始化时的配置
            
        Returns:
            dict: {
                "return_info": str,
                "success": bool,
                "uploaded_count": int,
                "total_count": int,
                "failed_files": List[str]
            }
        """
        try:
            # 确定备份目录
            target_backup_dir = backup_dir if backup_dir else self._last_backup_dir
            
            if not target_backup_dir:
                error_msg = "未指定 backup_dir 且没有可用的最近备份目录"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {
                    "return_info": error_msg,
                    "success": False,
                    "uploaded_count": 0,
                    "total_count": 0,
                    "failed_files": [],
                    "uploaded_files": []
                }
            
            # 检查目录是否存在
            if not os.path.exists(target_backup_dir):
                error_msg = f"备份目录不存在: {target_backup_dir}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {
                    "return_info": error_msg,
                    "success": False,
                    "uploaded_count": 0,
                    "total_count": 0,
                    "failed_files": [],
                    "uploaded_files": []
                }
            
            # 检查是否启用 OSS 上传
            if not self.oss_upload_enabled:
                warning_msg = f"OSS 上传未启用 (oss_upload_enabled=False)，跳过上传。备份目录: {target_backup_dir}"
                if self._ros_node:
                    self._ros_node.lab_logger().warning(warning_msg)
                return {                    "return_info": warning_msg,
                    "success": False,
                    "uploaded_count": 0,
                    "total_count": 0,
                    "failed_files": [],
                    "uploaded_files": []
                }
            
            # 确定 OSS 前缀
            target_oss_prefix = oss_prefix if oss_prefix else self.oss_prefix
            
            if self._ros_node:
                self._ros_node.lab_logger().info(
                    f"开始上传备份文件到 OSS: {target_backup_dir} -> {target_oss_prefix}"
                )
            
            # 扫描匹配的文件
            import glob
            pattern_path = os.path.join(target_backup_dir, file_pattern)
            matched_files = glob.glob(pattern_path)
            
            if not matched_files:
                warning_msg = f"备份目录中没有匹配 '{file_pattern}' 的文件: {target_backup_dir}"
                if self._ros_node:
                    self._ros_node.lab_logger().warning(warning_msg)
                return {
                    "return_info": warning_msg,
                    "success": True,  # 没有文件也算成功
                    "uploaded_count": 0,
                    "total_count": 0,
                    "failed_files": [],
                    "uploaded_files": []
                }
            
            total_count = len(matched_files)
            if self._ros_node:
                self._ros_node.lab_logger().info(
                    f"找到 {total_count} 个匹配文件，开始上传..."
                )
            
            # 批量上传文件
            uploaded_count = 0
            failed_files = []
            uploaded_files = []  # 记录成功上传的文件信息（文件名和URL）
            
            for i, file_path in enumerate(matched_files, 1):
                try:
                    basename = os.path.basename(file_path)
                    oss_object_name = f"{target_oss_prefix}/{basename}" if target_oss_prefix else basename
                    oss_object_name = oss_object_name.replace('\\', '/')
                    
                    if self._ros_node:
                        self._ros_node.lab_logger().info(
                            f"[{i}/{total_count}] 上传: {file_path} -> {oss_object_name}"
                        )
                    
                        # upload_file_to_oss 成功时返回 URL
                        result = upload_file_to_oss(file_path, oss_object_name)
                        if result:
                            uploaded_count += 1
                            # 解析文件名获取 Battery_Code 和 Electrolyte_Code
                            name_without_ext = os.path.splitext(basename)[0]
                            parts = name_without_ext.split('-', 1)
                            battery_code = parts[0]
                            electrolyte_code = parts[1] if len(parts) > 1 else ""
                            
                            # 记录成功上传的文件信息
                            uploaded_files.append({
                                "filename": basename,
                                "url": result if isinstance(result, str) else f"https://uni-lab-test.oss-cn-zhangjiakou.aliyuncs.com/{oss_object_name}",
                                "Battery_Code": battery_code,
                                "Electrolyte_Code": electrolyte_code
                            })
                        if self._ros_node:
                            self._ros_node.lab_logger().info(
                                f"[{i}/{total_count}] 上传成功: {result if isinstance(result, str) else oss_object_name}"
                            )
                    else:
                        failed_files.append(basename)
                        if self._ros_node:
                            self._ros_node.lab_logger().error(
                                f"[{i}/{total_count}] 上传失败: {basename}"
                            )
                
                except ValueError as e:
                    # OSS 环境变量错误，停止上传
                    error_msg = f"OSS 环境变量配置错误: {e}"
                    if self._ros_node:
                        self._ros_node.lab_logger().error(error_msg)
                    return {
                        "return_info": error_msg,
                        "success": False,
                        "uploaded_count": uploaded_count,
                        "total_count": total_count,
                        "failed_files": failed_files,
                        "uploaded_files": uploaded_files
                    }
                
                except Exception as e:
                    failed_files.append(os.path.basename(file_path))
                    if self._ros_node:
                        self._ros_node.lab_logger().error(
                            f"[{i}/{total_count}] 上传异常: {e}"
                        )
            
            # 汇总结果
            if uploaded_count == total_count:
                success_msg = f"全部上传成功: {uploaded_count}/{total_count} 个文件"
                success = True
            elif uploaded_count > 0:
                success_msg = f"部分上传成功: {uploaded_count}/{total_count} 个文件，失败 {len(failed_files)} 个"
                success = True  # 部分成功也算成功
            else:
                success_msg = f"全部上传失败: 0/{total_count} 个文件"
                success = False
            
            if self._ros_node:
                self._ros_node.lab_logger().info(success_msg)
            
            return {
                "return_info": success_msg,
                "success": success,
                "uploaded_count": uploaded_count,
                "total_count": total_count,
                "failed_files": failed_files,
                "uploaded_files": uploaded_files  # 添加成功上传的文件 URL 列表
            }
        
        except Exception as e:
            error_msg = f"上传备份文件到 OSS 失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {
                "return_info": error_msg,
                "success": False,
                "uploaded_count": 0,
                "total_count": 0,
                "failed_files": [],
                "uploaded_files": []
            }


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
