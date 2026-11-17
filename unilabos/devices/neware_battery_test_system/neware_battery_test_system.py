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
import csv
import xml.etree.ElementTree as ET
import json
import time
import inspect
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict

from pylabrobot.resources import ResourceHolder, Coordinate, create_ordered_items_2d, Deck, Plate
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode
from unilabos.utils.datacore import push_csv_by_config
from unilabos.devices.workstation.mixing_judgment_query import JUDGMENT_COLUMN

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

    def serialize(self) -> dict:
        d = super().serialize()
        channel_name = self._unilabos_state.get("Channel_Name")
        if channel_name:
            d["name"] = channel_name
        return d

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        """格式不变"""
        data = super().serialize_state()
        data.update(self._unilabos_state)
        return data

    def serialize_all_state(self) -> Dict[str, Dict[str, Any]]:
        states = {}
        channel_name = self._unilabos_state.get("Channel_Name", self.name)
        states[channel_name] = self.serialize_state()
        for child in self.children:
            states.update(child.serialize_all_state())
        return states


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
        "working": "#15803d",  # 深绿
        "stop":    "#4b5563",  # 深灰
        "finish":  "#1d4ed8",  # 深蓝
        "protect": "#b91c1c",  # 深红
        "pause":   "#b45309",  # 深橙
        "false":   "#6b7280",  # 灰
        "unknown": "#7c3aed",  # 深紫
    }
    
    # 字母常量
    ascii_lowercase = 'abcdefghijklmnopqrstuvwxyz'
    ascii_uppercase = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    LETTERS = ascii_uppercase + ascii_lowercase
    DEFAULT_MACHINE_IDS = [1, 2, 3, 4, 5, 6, 86]

    def __init__(self, 
        ip: str = None, 
        port: int = None, 
        machine_ids: Optional[List[int]] = None,
        devtype: str = None, 
        timeout: int = None,
        
        size_x: float = 50,
        size_y: float = 50,
        size_z: float = 20,
        
        oss_upload_enabled: bool = False,
        oss_prefix: str = "neware_backup",

        datacore_config: Optional[Dict[str, Any]] = None,
    ):
        """
        初始化新威电池测试系统
        
        Args:
            ip: TCP服务器IP地址
            port: TCP端口
            machine_ids: 设备ID列表
            devtype: 设备类型标识
            timeout: 通信超时时间（秒）
            size_x, size_y, size_z: 设备物理尺寸
            oss_upload_enabled: 是否启用OSS上传功能，默认False
            oss_prefix: OSS对象路径前缀，默认"neware_backup"
            datacore_config: DataCore 大装置数据接入配置（来自图 JSON），支持字段
                enabled / ingest_url / device_id / device_key / timeout /
                total_timeout / retry_interval；不配置则不推送
        """
        self.ip = ip or self.BTS_IP
        self.port = port or self.BTS_PORT
        self.machine_ids = machine_ids
        self.display_device_ids = self._resolve_display_device_ids()
        self.primary_device_id = self.display_device_ids[0]
        self.devtype = devtype or self.DEVTYPE
        self.timeout = timeout or self.TIMEOUT
        
        # 存储设备物理尺寸
        self.size_x = size_x
        self.size_y = size_y
        self.size_z = size_z
        
        # OSS 上传配置
        self.oss_upload_enabled = oss_upload_enabled
        self.oss_prefix = oss_prefix

        # DataCore 大装置数据接入配置
        self.datacore_config = datacore_config or {}
        
        self._last_status_update = None
        self._cached_status = {}
        self._last_backup_dir = None  # 记录最近一次的 backup_dir，供上传使用
        self._ros_node: Optional[ROS2WorkstationNode] = None  # ROS节点引用，由框架设置
        self._channels = self._build_channel_map()

    def _resolve_display_device_ids(self) -> List[int]:
        if self.machine_ids:
            return [int(devid) for devid in self.machine_ids]
        return self.DEFAULT_MACHINE_IDS.copy()


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

    def _plate_name(self, devid: int, plate_num: int) -> str:
        return f"{devid}_P{plate_num}"

    def _plate_resource_key(self, devid: int, plate_num: int, row_idx: int, col_idx: int) -> str:
        return f"{self._plate_name(devid, plate_num)}_{self.LETTERS[row_idx]}{col_idx + 1}"

    def _get_plate_resource(self, devid: int, plate_num: int, row_idx: int, col_idx: int):
        possible_names = [
            f"{self._plate_name(devid, plate_num)}_batterytestposition_{col_idx}_{row_idx}",
            f"{self._plate_name(devid, plate_num)}_{self.LETTERS[row_idx]}{col_idx + 1}",
            f"{self._plate_name(devid, plate_num)}_{self.LETTERS[row_idx].lower()}{col_idx + 1}",
            f"P{plate_num}_batterytestposition_{col_idx}_{row_idx}",
            f"P{plate_num}_{self.LETTERS[row_idx]}{col_idx + 1}",
            f"P{plate_num}_{self.LETTERS[row_idx].lower()}{col_idx + 1}",
        ]
        for name in possible_names:
            if name in self.station_resources:
                return self.station_resources[name], name, possible_names
        return None, None, possible_names

    def _setup_material_management(self):
        """设置物料管理系统"""
        deck_main = Deck(
            name="ADeckName",
            size_x=1200,
            size_y=2800,
            size_z=100,
            origin=Coordinate(-5500, 0, 0)
        )
        self.station_resources = {}
        self.station_resources_by_plate = {}

        for row_idx, devid in enumerate(self.display_device_ids):
            for plate_num in (1, 2):
                plate_resources: Dict[str, BatteryTestPosition] = create_ordered_items_2d(
                    BatteryTestPosition,
                    num_items_x=8,
                    num_items_y=5,
                    dx=10,
                    dy=10,
                    dz=0,
                    item_dx=65,
                    item_dy=65
                )
                plate_name = self._plate_name(devid, plate_num)
                plate = Plate(
                    name=plate_name,
                    size_x=540,
                    size_y=350,
                    size_z=50,
                    ordered_items=plate_resources
                )
                location_x = 0 if plate_num == 1 else 590
                location_y = row_idx * 400
                deck_main.assign_child_resource(plate, location=Coordinate(location_x, location_y, 0))

                plate_key = (devid, plate_num)
                subdev_start = 1 if plate_num == 1 else 6
                self.station_resources_by_plate[plate_key] = {}
                for name, resource in plate_resources.items():
                    new_name = f"{plate_name}_{name}"
                    # 从名称解析 col/row 索引，设置初始 Channel_Name
                    parts = name.rsplit("_", 2)
                    if len(parts) >= 3:
                        col_idx, row_idx = int(parts[-2]), int(parts[-1])
                        chl_id = col_idx + 1
                        subdev_id = subdev_start + row_idx
                        resource.load_state({
                            "status": "unknown",
                            "color": self.STATUS_COLOR["unknown"],
                            "voltage": 0.0,
                            "current": 0.0,
                            "time": 0.0,
                            "Channel_Name": f"{devid}-{subdev_id}-{chl_id}",
                        })
                    self.station_resources_by_plate[plate_key][new_name] = resource
                    self.station_resources[new_name] = resource

        self.station_resources_plate1 = self.station_resources_by_plate.get((self.primary_device_id, 1), {})
        self.station_resources_plate2 = self.station_resources_by_plate.get((self.primary_device_id, 2), {})

        if hasattr(self._ros_node, 'update_resource') and callable(getattr(self._ros_node, 'update_resource')):
            try:
                ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
                    "resources": [deck_main]
                })
            except Exception as e:
                if hasattr(self._ros_node, 'lab_logger'):
                    self._ros_node.lab_logger().warning(f"更新资源失败: {e}")

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
        
        # 返回主设备数据，如果主设备没有匹配数据则回退到首个可用设备
        status_current_machine = status_processed.get(self.primary_device_id, {})
        
        if not status_current_machine and status_processed:
            # 如果主设备没有匹配到数据，使用第一个可用的设备数据
            first_devid = next(iter(status_processed.keys()))
            status_current_machine = status_processed[first_devid]
            if self._ros_node:
                self._ros_node.lab_logger().warning(
                    f"主设备ID {self.primary_device_id} 没有匹配到数据，使用设备ID {first_devid} 的数据"
                )
        
        # 确保有默认的数据结构
        if not status_current_machine:
            status_current_machine = {
                "stats": {s: 0 for s in self.STATUS_SET | {"unknown"}},
                "subunits": {}
            }
        
        self._update_plate_resources(status_processed)
        
        return status_current_machine

    def _update_plate_resources(self, status_processed: Dict[int, Dict]):
        """更新7台设备共14盘电池资源的状态"""
        for devid in self.display_device_ids:
            machine_data = status_processed.get(devid, {})
            subunits = machine_data.get("subunits", {})
            for plate_num, subdev_start, subdev_end in ((1, 1, 5), (2, 6, 10)):
                for subdev_id in range(subdev_start, subdev_end + 1):
                    status_row = subunits.get(subdev_id, {})
                    for chl_id in range(1, 9):
                        try:
                            col_idx = chl_id - 1
                            row_idx = subdev_id - subdev_start
                            r, resource_name, possible_names = self._get_plate_resource(
                                devid=devid,
                                plate_num=plate_num,
                                row_idx=row_idx,
                                col_idx=col_idx
                            )
                            if r is None:
                                if self._ros_node and hasattr(self._ros_node, 'lab_logger'):
                                    self._ros_node.lab_logger().debug(
                                        f"{devid}_P{plate_num}未找到资源: subdev{subdev_id}/chl{chl_id} -> "
                                        f"尝试的名称: {possible_names}"
                                    )
                                continue
                            status_channel = status_row.get(chl_id, {})
                            metrics = status_channel.get("metrics", {})
                            channel_state = {
                                "voltage": metrics.get("voltage_V", 0.0),
                                "current": metrics.get("current_A", 0.0),
                                "time": metrics.get("totaltime_s", 0.0),
                                "status": status_channel.get("state", "unknown"),
                                "color": status_channel.get("color", self.STATUS_COLOR["unknown"]),
                                "Channel_Name": f"{devid}-{subdev_id}-{chl_id}",
                            }
                            r.load_state(channel_state)
                            if self._ros_node and hasattr(self._ros_node, 'lab_logger'):
                                self._ros_node.lab_logger().debug(
                                    f"更新{devid}_P{plate_num}资源状态: {resource_name} <- "
                                    f"subdev{subdev_id}/chl{chl_id} 状态:{channel_state['status']}"
                                )
                        except (KeyError, IndexError) as e:
                            if self._ros_node and hasattr(self._ros_node, 'lab_logger'):
                                self._ros_node.lab_logger().debug(
                                    f"{devid}_P{plate_num}映射错误: subdev{subdev_id}/chl{chl_id} - {e}"
                                )
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

    def _build_device_summary_dict(self) -> dict:
        if not hasattr(self, '_channels') or not self._channels:
            self._channels = self._build_channel_map()
        channel_count_by_devid = {}
        for channel in self._channels:
            devid = channel.devid
            channel_count_by_devid[devid] = channel_count_by_devid.get(devid, 0) + 1
        return {
            "channel_count_by_devid": channel_count_by_devid,
            "display_device_ids": self.display_device_ids,
            "total_channels": len(self._channels)
        }

    def device_summary(self) -> str:
        return json.dumps(self._build_device_summary_dict(), ensure_ascii=False)

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

    def _get_builder_required_positional_count(self, builder) -> int:
        """返回XML生成函数必填位置参数个数（仅统计无默认值的positional参数）"""
        sig = inspect.signature(builder)
        required = 0
        for p in sig.parameters.values():
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
                if p.default is inspect.Parameter.empty:
                    required += 1
        return required

    def _is_csv_value_empty(self, value) -> bool:
        """判断CSV单元格是否为空（兼容NaN/None/空串/null）"""
        if value is None:
            return True
        if isinstance(value, str):
            return value.strip().lower() in ("", "nan", "none", "null")
        try:
            # NaN 与自身不相等
            return value != value
        except Exception:
            return False
    
    def _compute_values(self, row):
        """
        计算活性物质质量和容量
        
        Args:
            row: DataFrame行数据
            
        Returns:
            tuple: (活性物质质量mg, 容量mAh)
        """
        pw = float(row['pole_weight'])
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
        # 体系标识 -> generate_xml_content 中的函数名（惰性解析：仅解析实际用到的体系，
        # 避免某个函数未实现导致整个映射构建失败，从而拖累所有电池）
        fmap = {
            'LB6': 'xml_LB6',
            'GR_LI': 'xml_Gr_Li',
            'LFP_GR': 'xml_LFP_Gr',
            '811_LI_002': 'xml_811_Li_002',
            '811_LI_005': 'xml_811_Li_005',
            'SIGR_LI_STEP': 'xml_SiGr_Li_Step',
            'SIGR_LI': 'xml_SiGr_Li_Step',
            '811_SIGR': 'xml_811_SiGr',
            '811_CU_AGING': 'xml_811_Cu_aging',
            '811_LI_JY': 'xml_811_Li_JY',
            '811_DATACORETEST': 'xml_811_datacoretest',
            'ZQXNLRMO': 'xml_ZQXNLRMO',
            'HC_LFP': 'xml_HC_LFP',
            'HC_GR': 'xml_HC_Gr',
            'SLG_NCM811': 'xml_SLG_NCM811',
            'WFJ_NCM811': 'xml_model_ncm811',
        }
        if key not in fmap:
            raise ValueError(f"未定义电池体系映射: {key}，可选体系: {sorted(fmap.keys())}")
        func_name = fmap[key]
        builder = getattr(gen_mod, func_name, None)
        if builder is None:
            raise ValueError(
                f"电池体系 {key} 对应的XML生成函数 {func_name} 尚未在 generate_xml_content.py 中实现"
            )
        return builder
    
    def _save_xml(self, xml: str, path: str):
        """
        保存XML文件
        
        Args:
            xml: XML内容
            path: 文件路径
        """
        with open(path, 'w', encoding='utf-8') as f:
            f.write(xml)
    
    def submit_from_csv_export_ndax(self, csv_path: str, output_dir: str = ".") -> dict:
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
                'coin_cell_code', 'electrolyte_code', '电池体系', '设备号', '排号', '通道号'
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
                    coin_id = f"{row['coin_cell_code']}-{row['electrolyte_code']}"

                    # 获取电池体系对应的XML生成函数
                    key = self._canon(row['电池体系'])
                    builder = self._get_xml_builder(gen_mod, key)
                    builder_required_args = self._get_builder_required_positional_count(builder)

                    # 生成XML内容：仅当工步模板需要时才校验并计算 act_mass/cap_mAh
                    if builder_required_args == 0:
                        xml_content = builder()
                    elif builder_required_args == 2:
                        calc_cols = ['pole_weight', '集流体质量', '活性物质含量', '克容量mah/g']
                        missing_calc = [
                            c for c in calc_cols
                            if c not in df.columns or self._is_csv_value_empty(row[c])
                        ]
                        if missing_calc:
                            error_msg = (
                                f"电池体系 {key} 需要 act_mass/Cap_mAh，以下列缺失或为空: {missing_calc}, "
                                f"CoinID={coin_id}"
                            )
                            if self._ros_node:
                                self._ros_node.lab_logger().warning(error_msg)
                            results.append(f"行{idx+1} 失败: {error_msg}")
                            continue

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
                        xml_content = builder(act_mass, cap_mAh)
                    else:
                        raise ValueError(
                            f"XML生成函数参数不支持: {builder.__name__} 需要 {builder_required_args} 个必填位置参数"
                        )
                    
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
                        backup_dir=backup_dir,
                        filetype=0
                    )
                    
                    submitted_count += 1
                    results.append(f"行{idx+1} {coin_id}: {resp}")
                    
                    if self._ros_node:
                        self._ros_node.lab_logger().info(
                            f"已提交 {coin_id} (设备{devid}-{subdevid}-{chlid}, NDAX备份): {resp}"
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


    def submit_from_csv_export_excel(self, csv_path: str, output_dir: str = ".") -> dict:
        """
        从CSV文件批量提交Neware测试任务，备份格式为Excel（设备动作）
        
        与 submit_from_csv_export_ndax 逻辑一致，唯一区别是 BTS 备份文件格式为 Excel 而非 NDA。
        
        Args:
            csv_path (str): 输入CSV文件路径
            output_dir (str): 输出目录，用于存储XML文件和备份，默认当前目录
            
        Returns:
            dict: 执行结果 {"return_info": str, "success": bool, "submitted_count": int}
        """
        try:
            self._ensure_local_import_path()
            import pandas as pd
            import generate_xml_content as gen_mod
            from neware_driver import start_test
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"开始从CSV文件提交任务(Excel备份): {csv_path}")
            
            if not os.path.exists(csv_path):
                error_msg = f"CSV文件不存在: {csv_path}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False, "submitted_count": 0, "total_count": 0}
            
            df = pd.read_csv(csv_path, encoding='gbk')
            
            required = [
                'coin_cell_code', 'electrolyte_code', '电池体系', '设备号', '排号', '通道号'
            ]
            missing = [c for c in required if c not in df.columns]
            if missing:
                error_msg = f"CSV缺少必需列: {missing}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False, "submitted_count": 0, "total_count": 0}
            
            xml_dir = os.path.join(output_dir, 'xml_dir')
            backup_dir = os.path.join(output_dir, 'backup_dir')
            os.makedirs(xml_dir, exist_ok=True)
            os.makedirs(backup_dir, exist_ok=True)
            
            self._last_backup_dir = backup_dir
            
            if self._ros_node:
                self._ros_node.lab_logger().info(
                    f"输出目录: XML={xml_dir}, 备份(Excel)={backup_dir}"
                )
            
            submitted_count = 0
            results = []
            
            for idx, row in df.iterrows():
                try:
                    coin_id = f"{row['coin_cell_code']}-{row['electrolyte_code']}"

                    key = self._canon(row['电池体系'])
                    builder = self._get_xml_builder(gen_mod, key)
                    builder_required_args = self._get_builder_required_positional_count(builder)

                    if builder_required_args == 0:
                        xml_content = builder()
                    elif builder_required_args == 2:
                        calc_cols = ['pole_weight', '集流体质量', '活性物质含量', '克容量mah/g']
                        missing_calc = [
                            c for c in calc_cols
                            if c not in df.columns or self._is_csv_value_empty(row[c])
                        ]
                        if missing_calc:
                            error_msg = (
                                f"电池体系 {key} 需要 act_mass/Cap_mAh，以下列缺失或为空: {missing_calc}, "
                                f"CoinID={coin_id}"
                            )
                            if self._ros_node:
                                self._ros_node.lab_logger().warning(error_msg)
                            results.append(f"行{idx+1} 失败: {error_msg}")
                            continue

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
                        xml_content = builder(act_mass, cap_mAh)
                    else:
                        raise ValueError(
                            f"XML生成函数参数不支持: {builder.__name__} 需要 {builder_required_args} 个必填位置参数"
                        )
                    
                    devid = int(row['设备号'])
                    subdevid = int(row['排号'])
                    chlid = int(row['通道号'])
                    
                    recipe_path = os.path.join(
                        xml_dir, 
                        f"{coin_id}_{devid}_{subdevid}_{chlid}.xml"
                    )
                    self._save_xml(xml_content, recipe_path)
                    
                    resp = start_test(
                        ip=self.ip, 
                        port=self.port, 
                        devid=devid, 
                        subdevid=subdevid, 
                        chlid=chlid, 
                        CoinID=coin_id, 
                        recipe_path=recipe_path, 
                        backup_dir=backup_dir,
                        filetype=1
                    )
                    
                    submitted_count += 1
                    results.append(f"行{idx+1} {coin_id}: {resp}")
                    
                    if self._ros_node:
                        self._ros_node.lab_logger().info(
                            f"已提交 {coin_id} (设备{devid}-{subdevid}-{chlid}, Excel备份): {resp}"
                        )
                
                except Exception as e:
                    error_msg = f"行{idx+1} 处理失败: {str(e)}"
                    results.append(error_msg)
                    if self._ros_node:
                        self._ros_node.lab_logger().error(error_msg)
            
            success_msg = (
                f"批量提交完成(Excel备份): 成功{submitted_count}个，共{len(df)}行。"
                f"\n详细结果:\n" + "\n".join(results)
            )
            
            if self._ros_node:
                self._ros_node.lab_logger().info(
                    f"批量提交完成(Excel备份): 成功{submitted_count}/{len(df)}"
                )
            
            return {
                "return_info": success_msg,
                "success": True,
                "submitted_count": submitted_count,
                "total_count": len(df),
                "results": results
            }
        
        except Exception as e:
            error_msg = f"批量提交失败(Excel备份): {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {
                "return_info": error_msg, 
                "success": False, 
                "submitted_count": 0,
                "total_count": 0
            }

    def submit_lp_csv_export_excel(self, csv_path: str, output_dir: str = ".") -> dict:
        """
        从CSV文件批量提交LP任务，备份格式为Excel（设备动作）

        与 submit_from_csv_export_excel 逻辑一致，但当工步模板需要参数时，
        容量仅由“活性物质质量mg”和“克容量mah/g”计算。

        Args:
            csv_path (str): 输入CSV文件路径
            output_dir (str): 输出目录，用于存储XML文件和备份，默认当前目录

        Returns:
            dict: 执行结果 {"return_info": str, "success": bool, "submitted_count": int}
        """
        try:
            self._ensure_local_import_path()
            import pandas as pd
            import generate_xml_content as gen_mod
            from neware_driver import start_test

            if self._ros_node:
                self._ros_node.lab_logger().info(f"开始从CSV文件提交LP任务(Excel备份): {csv_path}")

            if not os.path.exists(csv_path):
                error_msg = f"CSV文件不存在: {csv_path}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False, "submitted_count": 0, "total_count": 0}

            df = pd.read_csv(csv_path, encoding='gbk')

            required = ['coin_cell_code', 'electrolyte_code', '电池体系', '设备号', '排号', '通道号']
            missing = [c for c in required if c not in df.columns]
            if missing:
                error_msg = f"CSV缺少必需列: {missing}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False, "submitted_count": 0, "total_count": 0}

            xml_dir = os.path.join(output_dir, 'xml_dir')
            backup_dir = os.path.join(output_dir, 'backup_dir')
            os.makedirs(xml_dir, exist_ok=True)
            os.makedirs(backup_dir, exist_ok=True)

            self._last_backup_dir = backup_dir

            if self._ros_node:
                self._ros_node.lab_logger().info(
                    f"输出目录: XML={xml_dir}, 备份(Excel)={backup_dir}"
                )

            submitted_count = 0
            results = []

            for idx, row in df.iterrows():
                try:
                    coin_id = f"{row['coin_cell_code']}-{row['electrolyte_code']}"

                    key = self._canon(row['电池体系'])
                    builder = self._get_xml_builder(gen_mod, key)
                    builder_required_args = self._get_builder_required_positional_count(builder)

                    if builder_required_args == 0:
                        xml_content = builder()
                    elif builder_required_args == 2:
                        calc_cols = ['活性物质质量mg', '克容量mah/g']
                        missing_calc = [
                            c for c in calc_cols
                            if c not in df.columns or self._is_csv_value_empty(row[c])
                        ]
                        if missing_calc:
                            error_msg = (
                                f"电池体系 {key} 需要 act_mass/Cap_mAh，以下列缺失或为空: {missing_calc}, "
                                f"CoinID={coin_id}"
                            )
                            if self._ros_node:
                                self._ros_node.lab_logger().warning(error_msg)
                            results.append(f"行{idx+1} 失败: {error_msg}")
                            continue

                        act_mass = float(row['活性物质质量mg'])
                        specific_capacity = float(row['克容量mah/g'])
                        cap_mAh = round(act_mass * specific_capacity / 1000.0, 3)
                        act_mass = round(act_mass, 2)
                        if cap_mAh < 0:
                            error_msg = (
                                f"容量为负数: Battery_Code={coin_id}, "
                                f"活性物质质量mg={act_mass}, 容量mah={cap_mAh}"
                            )
                            if self._ros_node:
                                self._ros_node.lab_logger().warning(error_msg)
                            results.append(f"行{idx+1} 失败: {error_msg}")
                            continue
                        xml_content = builder(act_mass, cap_mAh)
                    else:
                        raise ValueError(
                            f"XML生成函数参数不支持: {builder.__name__} 需要 {builder_required_args} 个必填位置参数"
                        )

                    devid = int(row['设备号'])
                    subdevid = int(row['排号'])
                    chlid = int(row['通道号'])

                    recipe_path = os.path.join(
                        xml_dir,
                        f"{coin_id}_{devid}_{subdevid}_{chlid}.xml"
                    )
                    self._save_xml(xml_content, recipe_path)

                    resp = start_test(
                        ip=self.ip,
                        port=self.port,
                        devid=devid,
                        subdevid=subdevid,
                        chlid=chlid,
                        CoinID=coin_id,
                        recipe_path=recipe_path,
                        backup_dir=backup_dir,
                        filetype=1
                    )

                    submitted_count += 1
                    results.append(f"行{idx+1} {coin_id}: {resp}")

                    if self._ros_node:
                        self._ros_node.lab_logger().info(
                            f"已提交LP {coin_id} (设备{devid}-{subdevid}-{chlid}, Excel备份): {resp}"
                        )

                except Exception as e:
                    error_msg = f"行{idx+1} 处理失败: {str(e)}"
                    results.append(error_msg)
                    if self._ros_node:
                        self._ros_node.lab_logger().error(error_msg)

            success_msg = (
                f"LP批量提交完成(Excel备份): 成功{submitted_count}个，共{len(df)}行。"
                f"\n详细结果:\n" + "\n".join(results)
            )

            if self._ros_node:
                self._ros_node.lab_logger().info(
                    f"LP批量提交完成(Excel备份): 成功{submitted_count}/{len(df)}"
                )

            return {
                "return_info": success_msg,
                "success": True,
                "submitted_count": submitted_count,
                "total_count": len(df),
                "results": results
            }

        except Exception as e:
            error_msg = f"LP批量提交失败(Excel备份): {str(e)}"
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
            result_info = self.device_summary()
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
            backup_dir: 备份目录路径，默认使用最近一次提交任务的 backup_dir
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

    def mock_assembly_data(self) -> dict:
        """
        模拟扣电组装站 auto-func_sendbottle_allpack_multi 的输出，返回固定的 16 颗电池 assembly_data。
        用于在没有真实扣电组装站的情况下，测试
        mock_assembly_data → manual_confirm → battery_transfer_confirm → submit_auto_export_excel
        的完整参数传递与 TCP 下发链路。

        Returns:
            dict: {
                "assembly_data": list[dict],   # 9 字段 × 16 颗电池
                "success": bool,
                "return_info": str,
            }
        """
        # 用确定性规律生成 16 颗电池，保证每次调用结果一致，便于回归测试
        assembly_data = [
            {
                "Time":                f"20260421_14{30 + i:02d}22",
                "open_circuit_voltage": round(3.700 + (i % 5) * 0.006, 3),
                "pole_weight":          round(97.50 + i * 0.06, 2),
                "assembly_time":        118 + (i % 4),
                "assembly_pressure":    round(5.0 + (i % 4) * 0.1, 1),
                "target_assembly_pressure": 5.0,
                "electrolyte_volume":   round(79.0 + (i % 3) * 0.5, 1),
                "data_coin_type":       2,
                "electrolyte_code":     f"EL-20260421{i:02d}",
                "coin_cell_code":       f"CC-20260421{i:02d}",
                "coaxiality":           f"0.{i:03d}mm",
            }
            for i in range(1, 17)
        ]
        info = f"mock_assembly_data 返回 {len(assembly_data)} 颗电池的模拟组装数据"
        if self._ros_node:
            self._ros_node.lab_logger().info(f"[mock_assembly_data] {info}")
        else:
            print(f"[mock_assembly_data] {info}")
        return {
            "assembly_data": assembly_data,
            "success": True,
            "return_info": info,
        }

    # ─── manual_confirm 辅助：CSV 表头别名 + 三模式展开 ───────────────────────────

    @staticmethod
    def _normalize_csv_headers(df):
        """把 CSV 中文/英文表头统一映射为内部字段名。"""
        alias = {
            "coin_cell_code":  "coin_cell_code",
            "电池条码":        "coin_cell_code",
            "电池编号":        "coin_cell_code",
            "collector_mass":  "collector_mass",
            "集流体质量":      "collector_mass",
            "active_material": "active_material",
            "活性物质含量":    "active_material",
            "活性物质的含量":  "active_material",
            "capacity":        "capacity",
            "克容量":          "capacity",
            "battery_system":  "battery_system",
            "电池体系":        "battery_system",
            "xml工步":         "battery_system",
        }
        rename_map = {c: alias[c.strip()] for c in df.columns if c.strip() in alias}
        return df.rename(columns=rename_map)

    def _expand_battery_params(
        self,
        mount_resource,
        assembly_data,
        collector_mass,
        active_material,
        capacity,
        battery_system,
        default_collector_mass,
        default_active_material,
        default_capacity,
        default_battery_system,
        param_csv_path,
    ):
        """按优先级 B(逐颗数组) > C(CSV) > A(标量默认) 展开为长度 N 的 4 个 list。"""
        N = len(mount_resource) if mount_resource else 0
        FIELDS = ("collector_mass", "active_material", "capacity", "battery_system")

        # 1) 解析 CSV → coin_cell_code 索引表
        csv_map: Dict[str, Dict[str, Any]] = {}
        if param_csv_path:
            if not os.path.isfile(param_csv_path):
                raise FileNotFoundError(f"param_csv_path 不存在: {param_csv_path}")
            import pandas as pd
            try:
                df = pd.read_csv(param_csv_path, encoding="utf-8-sig")
            except UnicodeDecodeError:
                df = pd.read_csv(param_csv_path, encoding="gbk")
            df = self._normalize_csv_headers(df)
            if "coin_cell_code" not in df.columns:
                raise ValueError(
                    f"param_csv_path 解析失败: 缺少必需列 coin_cell_code（或 电池条码 / 电池编号），实际列={list(df.columns)}"
                )
            for _, row in df.iterrows():
                code = str(row["coin_cell_code"]).strip()
                if not code or code.lower() == "nan":
                    continue
                csv_map[code] = {
                    f: (row[f] if f in df.columns and not (isinstance(row[f], float) and row[f] != row[f]) else None)
                    for f in FIELDS
                }
            if self._ros_node:
                self._ros_node.lab_logger().info(
                    f"[manual_confirm] CSV 已加载 {len(csv_map)} 行参数 from {param_csv_path}"
                )

        arr_map = {
            "collector_mass":  collector_mass or [],
            "active_material": active_material or [],
            "capacity":        capacity or [],
            "battery_system":  battery_system or [],
        }
        default_map = {
            "collector_mass":  default_collector_mass,
            "active_material": default_active_material,
            "capacity":        default_capacity,
            "battery_system":  default_battery_system,
        }

        out: Dict[str, list] = {f: [None] * N for f in FIELDS}
        for i in range(N):
            code = ""
            if assembly_data and i < len(assembly_data):
                code = str(assembly_data[i].get("coin_cell_code", "")).strip()

            for f in FIELDS:
                # B：逐颗数组
                arr = arr_map[f]
                if arr and i < len(arr) and arr[i] not in (None, ""):
                    out[f][i] = arr[i]
                    continue
                # C：CSV 按 coin_cell_code
                if code and code in csv_map:
                    cv = csv_map[code].get(f)
                    if cv not in (None, ""):
                        out[f][i] = cv
                        continue
                # A：标量默认
                d = default_map[f]
                if d not in (None, ""):
                    out[f][i] = d
                    continue

        # 缺失校验
        missing = [(i, f) for f in FIELDS for i in range(N) if out[f][i] in (None, "")]
        if missing:
            raise ValueError(
                f"battery_param 展开缺失: {missing}；请检查 A(default_*) / B(逐颗数组) / C(param_csv_path) 三种填法是否覆盖到每颗电池"
            )

        # 数值字段类型规范化（CSV 读出来可能是 numpy 类型，转回 Python 原生）
        out["collector_mass"] = [float(v) for v in out["collector_mass"]]
        out["capacity"]       = [float(v) for v in out["capacity"]]
        out["active_material"] = [str(v) if not isinstance(v, str) else v for v in out["active_material"]]
        out["battery_system"]  = [str(v) for v in out["battery_system"]]

        return out["collector_mass"], out["active_material"], out["capacity"], out["battery_system"]

    def manual_confirm(
        self,
        resource: List[ResourceSlot],
        target_device: DeviceSlot,
        mount_resource: List[ResourceSlot],
        formulations: List[Dict] = None,
        assembly_data: List[Dict] = None,
        csv_export_dir: str = "D:\\2604Agentic_test",
        timeout_seconds: int = 86400,
        assignee_user_ids: list[str] = None,
        A: Dict = None,
        B: Dict = None,
        C: Dict = None,
        **kwargs,
    ) -> dict:
        """
        人工确认节点：
        - 上游接收 bioyond 配方（formulations）+ 扣电组装数据（assembly_data 单数组）
        - 人工在前端按 A/B/C 三种模式之一填写 4 个电池参数（collector_mass / active_material /
          capacity / battery_system(xml工步)），并选择 target_device 与 mount_resource（通道）
        - 后端按优先级 B > C > A 展开为长度 N 的 4 个 list；下游 submit_auto_export_excel 接口不变
        - 内部把 assembly_data 解包为并行数组，把 pole_weight / coin_cell_code 透传给下游
        - 把所有数据整合后写入 {csv_export_dir}/{YYYYMMDD}/battery_test_{YYYYMMDD_HHMMSS}.csv
          额外带上配液的 total_mass_tolerance、Judgment of mixing state，以及扣电的 coaxiality

        Args:
            resource:        扣电组装物料系统（无需选择）—— 由系统自动管理的扣电资源列表
            target_device:   目标新威测试柜设备
            mount_resource:  新威测试通道 —— 选择目标新威测试柜上的测试通道（决定下游 N = len(mount_resource)）
            formulations:    配方信息列表（来自 bioyond mass_ratios）
            assembly_data:   扣电组装数据列表（每颗电池一个 dict）
            csv_export_dir:  整合 CSV 导出根目录
            timeout_seconds: 超时时间（秒），默认 86400；由 node_type=manual_confirm 的外层调度/前端等待机制处理，
                             设备函数体内不做本地计时中断
            assignee_user_ids: 通知人员
            A: 模式A 参数一致（dict, 4 个 default_*）
                {default_collector_mass, default_active_material, default_capacity, default_battery_system}
            B: 模式B 参数不一致 数量少（dict, 4 个 list）
                {collector_mass, active_material, capacity, battery_system}
            C: 模式C 参数不一致 数量多（dict）
                {param_csv_path}
                CSV 表头需含 coin_cell_code(或 电池条码) + 集流体质量(或 collector_mass)
                + 活性物质含量(或 active_material) + 克容量(或 capacity)
                + 电池体系(或 xml工步 / battery_system)；行序不敏感，按 coin_cell_code 对齐
        """
        A = A or {}
        B = B or {}
        C = C or {}

        # 模式 A：标量默认（参数一致，整批共用）
        default_collector_mass  = A.get("default_collector_mass")
        default_active_material = A.get("default_active_material")
        default_capacity        = A.get("default_capacity")
        default_battery_system  = A.get("default_battery_system")

        # 模式 B：逐颗数组（兼容老调用：若 kwargs 里平铺传入则也接受）
        collector_mass  = B.get("collector_mass")  or kwargs.get("collector_mass")  or []
        active_material = B.get("active_material") or kwargs.get("active_material") or []
        capacity        = B.get("capacity")        or kwargs.get("capacity")        or []
        battery_system  = B.get("battery_system")  or kwargs.get("battery_system")  or []

        # 模式 C：CSV 路径
        param_csv_path  = (C.get("param_csv_path") or "").strip()

        resource_dump = ResourceTreeSet.from_plr_resources(resource).dump()
        mount_resource_dump = ResourceTreeSet.from_plr_resources(mount_resource).dump()

        assembly_data = assembly_data or []
        formulations = formulations or []

        Time = [b.get("Time", "") for b in assembly_data]
        open_circuit_voltage = [b.get("open_circuit_voltage", 0.0) for b in assembly_data]
        pole_weight = [b.get("pole_weight", 0.0) for b in assembly_data]
        assembly_time = [b.get("assembly_time", 0) for b in assembly_data]
        assembly_pressure = [b.get("assembly_pressure", 0) for b in assembly_data]
        target_assembly_pressure = [b.get("target_assembly_pressure", "") for b in assembly_data]
        electrolyte_volume = [b.get("electrolyte_volume", 0) for b in assembly_data]
        data_coin_type = [b.get("data_coin_type", 0) for b in assembly_data]
        electrolyte_code = [b.get("electrolyte_code", "") for b in assembly_data]
        coin_cell_code = [b.get("coin_cell_code", "") for b in assembly_data]
        coaxiality = [b.get("coaxiality", "") for b in assembly_data]

        # 按优先级 B > C > A 展开为长度 N 的 4 个 list
        collector_mass, active_material, capacity, battery_system = self._expand_battery_params(
            mount_resource=mount_resource,
            assembly_data=assembly_data,
            collector_mass=collector_mass,
            active_material=active_material,
            capacity=capacity,
            battery_system=battery_system,
            default_collector_mass=default_collector_mass,
            default_active_material=default_active_material,
            default_capacity=default_capacity,
            default_battery_system=default_battery_system,
            param_csv_path=param_csv_path,
        )

        csv_out_path = None
        try:
            csv_out_path = self._export_manual_confirm_csv(
                csv_export_dir=csv_export_dir,
                mount_resource=mount_resource,
                formulations=formulations,
                assembly_rows={
                    "Time": Time,
                    "open_circuit_voltage": open_circuit_voltage,
                    "pole_weight": pole_weight,
                    "assembly_time": assembly_time,
                    "assembly_pressure": assembly_pressure,
                    "target_assembly_pressure": target_assembly_pressure,
                    "electrolyte_volume": electrolyte_volume,
                    "data_coin_type": data_coin_type,
                    "electrolyte_code": electrolyte_code,
                    "coin_cell_code": coin_cell_code,
                    "coaxiality": coaxiality,
                },
                collector_mass=collector_mass,
                active_material=active_material,
                capacity=capacity,
                battery_system=battery_system,
            )
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().warning(f"[manual_confirm] 整合 CSV 导出失败: {e}")
            else:
                print(f"[manual_confirm] 整合 CSV 导出失败: {e}")

        # 推送整合 CSV 到 DataCore 大装置数据接入（尽力而为，失败不影响节点返回）
        if csv_out_path:
            lab_logger = self._ros_node.lab_logger() if self._ros_node else None
            ok = push_csv_by_config(
                csv_out_path,
                self.datacore_config,
                tag="manual_confirm",
                log=lab_logger.info if lab_logger else print,
            )
            if not ok and lab_logger:
                lab_logger.warning(f"[manual_confirm] DataCore 推送未成功: {csv_out_path}")

        return {
            "resource": resource_dump,
            "coin_cell_code": coin_cell_code,
            "electrolyte_code": electrolyte_code,
            "target_device": target_device,
            "mount_resource": mount_resource_dump,
            "collector_mass": collector_mass,
            "active_material": active_material,
            "capacity": capacity,
            "battery_system": battery_system,
            "pole_weight": pole_weight,
        }

    def upload_csv_to_datacore(self, file_path: str = "") -> dict:
        """
        手动把指定 CSV 推送到 DataCore 大装置数据接入

        用于补传历史文件或重传失败的文件。接入凭据取自图 JSON 的 datacore_config，
        无需在此填写。文件筛选由调用方在外部完成，这里一次只认一个完整路径。

        Args:
            file_path: 待上传文件的完整路径（主控电脑上的路径）

        Returns:
            dict: 包含 success / file_path / filename / message
        """
        file_path = (file_path or "").strip()
        if not file_path:
            raise ValueError("[upload_csv_to_datacore] file_path 不能为空")
        if not os.path.isfile(file_path):
            raise ValueError(f"[upload_csv_to_datacore] 文件不存在: {file_path}")

        lab_logger = self._ros_node.lab_logger() if self._ros_node else None
        ok = push_csv_by_config(
            file_path,
            self.datacore_config,
            tag="upload_csv_to_datacore",
            log=lab_logger.info if lab_logger else print,
        )
        if not ok:
            raise ValueError(
                f"[upload_csv_to_datacore] DataCore 推送失败: {file_path}；"
                f"请检查图 JSON 的 datacore_config 与网络连通性"
            )

        filename = os.path.basename(file_path)
        return {
            "success": True,
            "file_path": file_path,
            "filename": filename,
            "message": f"已推送到 DataCore: {filename}",
        }

    def _export_manual_confirm_csv(
        self,
        csv_export_dir: str,
        mount_resource: List[ResourceSlot],
        formulations: List[Dict],
        assembly_rows: Dict[str, List[Any]],
        collector_mass: List[float],
        active_material: List[float],
        capacity: List[float],
        battery_system: List[str],
    ) -> Optional[str]:
        """把 manual_confirm 收集到的全部参数整合写入 CSV。路径：{csv_export_dir}/{YYYYMMDD}/battery_test_{YYYYMMDD_HHMMSS}.csv"""
        n_assembly = len(assembly_rows.get("Time", []))
        n_channel = len(mount_resource) if mount_resource else 0
        n = max(n_assembly, n_channel, len(collector_mass or []), len(active_material or []),
                len(capacity or []), len(battery_system or []))
        if n == 0:
            return None

        date_str = datetime.now().strftime("%Y%m%d")
        datetime_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(csv_export_dir, date_str)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"battery_test_{datetime_str}.csv")

        header = [
            "Time", "open_circuit_voltage", "pole_weight",
            "assembly_time", "assembly_pressure", "target_assembly_pressure", "electrolyte_volume",
            "data_coin_type", "electrolyte_code", "coin_cell_code", "coaxiality",
            "orderName", "prep_bottle_barcode", "vial_bottle_barcodes",
            "target_mass_ratio", "real_mass_ratio", "total_mass_tolerance",
            JUDGMENT_COLUMN,
            "collector_mass", "active_material", "capacity", "battery_system",
            "channel_name",
        ]

        file_exists = os.path.exists(out_path)
        with open(out_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(header)

            def safe_get(lst, i, default=""):
                try:
                    return lst[i] if lst and i < len(lst) else default
                except Exception:
                    return default

            # 按 electrolyte_code 分组匹配配方：相同电解液码的电池共用同一配方；
            # 不同电解液码按「首次出现顺序」依次对应 formulations[0], [1], ...
            # （一瓶电解液 = 一个配方/订单，可灌装多颗扣电，故不能按行号一一对应）
            electrolyte_codes = assembly_rows.get("electrolyte_code", [])
            distinct_codes = []
            for _code in electrolyte_codes:
                _code = str(_code).strip()
                if _code and _code not in distinct_codes:
                    distinct_codes.append(_code)
            code_to_formulation: Dict[str, dict] = {}
            for _idx, _code in enumerate(distinct_codes):
                if _idx < len(formulations) and isinstance(formulations[_idx], dict):
                    code_to_formulation[_code] = formulations[_idx]

            for i in range(n):
                # 优先按电解液码取配方；无可用电解液码时回退到原有的按行号匹配
                code_i = str(safe_get(electrolyte_codes, i, "")).strip()
                if distinct_codes:
                    form = code_to_formulation.get(code_i, {})
                else:
                    form = formulations[i] if formulations and i < len(formulations) else {}
                if not isinstance(form, dict):
                    form = {}
                target_ratio = form.get("target_mass_ratio", {}) if isinstance(form, dict) else {}
                real_ratio = form.get("real_mass_ratio", {}) if isinstance(form, dict) else {}
                total_tol = form.get("total_mass_tolerance", None) if isinstance(form, dict) else None
                judgment = form.get(JUDGMENT_COLUMN, "") if isinstance(form, dict) else ""
                ch_name = self._extract_channel_name(mount_resource[i]) if mount_resource and i < len(mount_resource) else ""

                writer.writerow([
                    safe_get(assembly_rows["Time"], i),
                    safe_get(assembly_rows["open_circuit_voltage"], i, 0.0),
                    safe_get(assembly_rows["pole_weight"], i, 0.0),
                    safe_get(assembly_rows["assembly_time"], i, 0),
                    safe_get(assembly_rows["assembly_pressure"], i, 0),
                    safe_get(assembly_rows["target_assembly_pressure"], i, ""),
                    safe_get(assembly_rows["electrolyte_volume"], i, 0),
                    safe_get(assembly_rows["data_coin_type"], i, 0),
                    safe_get(assembly_rows["electrolyte_code"], i),
                    safe_get(assembly_rows["coin_cell_code"], i),
                    safe_get(assembly_rows.get("coaxiality", []), i),
                    form.get("orderName", "") if isinstance(form, dict) else "",
                    form.get("prep_bottle_barcode", "") if isinstance(form, dict) else "",
                    form.get("vial_bottle_barcodes", "") if isinstance(form, dict) else "",
                    json.dumps(target_ratio, ensure_ascii=False) if target_ratio else "",
                    json.dumps(real_ratio, ensure_ascii=False) if real_ratio else "",
                    "" if total_tol is None else total_tol,
                    "" if judgment is None else judgment,
                    safe_get(collector_mass, i, ""),
                    safe_get(active_material, i, ""),
                    safe_get(capacity, i, ""),
                    safe_get(battery_system, i, ""),
                    (f"'{ch_name}" if ch_name else ""),
                ])
            f.flush()

        if self._ros_node:
            self._ros_node.lab_logger().info(f"[manual_confirm] 整合 CSV 已写入 {out_path}（{n} 行）")
        return out_path
        

    async def battery_transfer_confirm(
        self,
        resource: List[ResourceSlot],
        target_device: DeviceSlot,
        mount_resource: List[ResourceSlot],
        timeout_seconds: int = 86400,
        assignee_user_ids: list[str] = None,
        **kwargs,
    ):
        """
        电池装夹人工确认 + TCP 转运。
        - 该节点通过 yaml 的 node_type: manual_confirm 机制阻塞等待人工确认。
        - 人工在前端确认通道与电池对应关系（装夹就位）后，方法体才会被框架调用。
        - timeout_seconds 由外层调度/前端等待机制处理；该方法体内不做本地计时中断。
        - 方法体执行真正的 TCP 资源转运。

        Args:
            resource:        扣电组装物料系统（无需选择）—— 由系统自动管理的扣电资源列表
            target_device:   目标新威测试柜设备
            mount_resource:  新威测试通道 —— 选择目标新威测试柜上的测试通道
            timeout_seconds: 超时时间（秒），由外层调度/前端等待机制处理
            assignee_user_ids: 通知人员
        """
        future = ROS2DeviceNode.run_async_func(
            self._ros_node.transfer_resource_to_another, True,
            **{
                "plr_resources": resource,
                "target_device_id": target_device,
                "target_resources": mount_resource,
                "sites": [None] * len(mount_resource),
            },
        )
        result = await future
        return result
    # ──────────────────────────────────────────────
    # test() 辅助方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _extract_channel_name(res) -> Optional[str]:
        """从 BatteryTestPosition 或通用 Resource 中提取 Channel_Name (devid-subdevid-chlid)"""
        # 情况1: ResourceSlot 对象 —— 直接读 _unilabos_state
        state = getattr(res, "_unilabos_state", None)
        if isinstance(state, dict):
            ch = state.get("Channel_Name")
            if ch:
                return str(ch)
        # 情况2: serialize_state()
        if hasattr(res, "serialize_state"):
            try:
                ss = res.serialize_state()
                if isinstance(ss, dict):
                    ch = ss.get("Channel_Name")
                    if ch:
                        return str(ch)
            except Exception:
                pass
        # 情况3: 来自 ResourceTreeSet.dump() 的 dict
        if isinstance(res, dict):
            data = res.get("data", {})
            if isinstance(data, dict):
                ch = data.get("Channel_Name")
                if ch:
                    return str(ch)
            ch = res.get("name") or res.get("id")
            if ch and len(str(ch).split("-")) == 3:
                return str(ch)
        # 情况4: name 本身就是 "devid-subdevid-chlid"
        name = getattr(res, "name", "")
        if name and len(name.split("-")) == 3:
            return name
        return None

    @staticmethod
    def _extract_pole_weight(res) -> float:
        """从电池资源 state 中提取极片称重 (mg)"""
        state = getattr(res, "_unilabos_state", None)
        if isinstance(state, dict) and "pole_weight" in state:
            return float(state["pole_weight"])
        if hasattr(res, "serialize_state"):
            try:
                ss = res.serialize_state()
                if isinstance(ss, dict) and "pole_weight" in ss:
                    return float(ss["pole_weight"])
            except Exception:
                pass
        if isinstance(res, dict):
            data = res.get("data", {})
            if isinstance(data, dict) and "pole_weight" in data:
                return float(data["pole_weight"])
        return 0.0

    @staticmethod
    def _parse_active_material(val) -> float:
        """解析活性物质含量，支持 0.97 或 '97%' 两种格式"""
        if isinstance(val, str):
            val = val.strip()
            if val.endswith("%"):
                return float(val[:-1]) / 100.0
            return float(val)
        return float(val)

    # ──────────────────────────────────────────────
    # test 动作：下发测试
    # ──────────────────────────────────────────────

    async def submit_auto_export_excel(
        self,
        mount_resource: List[ResourceSlot],
        collector_mass: List[float],
        active_material: List[float],
        capacity: List[float],
        battery_system: List[str],
        pole_weight: List[float] = None,
        coin_cell_code: List[str] = None,
        electrolyte_code: List[str] = None,
        resource: List[ResourceSlot] = None,
        output_dir: str = "D:\\2604Agentic_test",
    ) -> dict:
        """
        对每颗电池计算测试参数、生成 XML 工步文件并通过 TCP 下发给新威测试仪。

        循环长度由 mount_resource 驱动（真正要下发的通道数量）。

        Args:
            mount_resource:  新威测试通道 —— 目标通道资源列表（含 Channel_Name = devid-subdevid-chlid），循环长度来源
            collector_mass:  各电池集流体质量 (mg)
            active_material: 各电池活性物质比例（0.97 或 "97%"）
            capacity:        各电池克容量 (mAh/g)
            battery_system:  xml 工步标识（如 "811_LI_002"）
            pole_weight:     各电池极片质量 (mg)，来自上游 manual_confirm 的透传；为空时回退到从 resource 状态提取
            coin_cell_code:  各电池条码（来自上游 manual_confirm 从 assembly_data 解包）；作为 Neware 备份文件的 CoinID/barcode
            resource:        扣电组装物料系统（无需选择）—— 由系统自动管理的扣电资源列表；仅在 coin_cell_code 与 pole_weight 均未提供时作为回退
        """
        import importlib
        gen_mod = importlib.import_module(
            "unilabos.devices.neware_battery_test_system.generate_xml_content"
        )
        from .neware_driver import start_test as _start_test

        resource = resource or []
        pole_weight = pole_weight or []
        coin_cell_code = coin_cell_code or []
        electrolyte_code = electrolyte_code or []

        n = len(mount_resource) if mount_resource else 0
        results = []
        submitted = 0

        if n == 0:
            msg = "mount_resource 为空，没有通道可下发"
            if self._ros_node:
                self._ros_node.lab_logger().warning(f"[test] {msg}")
            return {
                "return_info": f"共 0 颗电池，成功下发 0 颗（{msg}）",
                "success": False,
                "submitted_count": 0,
                "total_count": 0,
                "results": [],
            }

        xml_dir = os.path.join(output_dir, "xml_dir")
        os.makedirs(xml_dir, exist_ok=True)
        backup_dir = os.path.join(output_dir, "backup_dir")
        os.makedirs(backup_dir, exist_ok=True)

        for i in range(n):
            try:
                # 1. 解析通道地址
                ch_name = self._extract_channel_name(mount_resource[i])
                if not ch_name:
                    raise ValueError(f"无法从 mount_resource[{i}] 提取 Channel_Name")
                parts = ch_name.split("-")
                if len(parts) != 3:
                    raise ValueError(f"Channel_Name 格式错误，期望 devid-subdevid-chlid，实际: {ch_name}")
                devid, subdevid, chlid = int(parts[0]), int(parts[1]), int(parts[2])

                # 2. 获取电池标识与极片重量（按优先级 coin_cell_code > resource > 兜底）
                res = resource[i] if i < len(resource) else None
                base_coin = (
                    (coin_cell_code[i] if i < len(coin_cell_code) and coin_cell_code[i] else None)
                    or (getattr(res, "name", None) if res is not None else None)
                    or (res.get("name") if isinstance(res, dict) else None)
                    or f"battery_{i}"
                )
                elec_code = electrolyte_code[i] if i < len(electrolyte_code) and electrolyte_code[i] else ""
                coin_id = f"{base_coin}-{elec_code}-{devid}-{subdevid}-{chlid}"
                if pole_weight and i < len(pole_weight):
                    pw = float(pole_weight[i])
                elif res is not None:
                    pw = self._extract_pole_weight(res)
                else:
                    raise ValueError(f"无法获取 pole_weight：pole_weight 列表长度不足 且 resource 为空")

                # 3. 计算活性物质质量与容量
                cm = float(collector_mass[i])
                amv = self._parse_active_material(active_material[i])
                sc = float(capacity[i])
                act_mass = round((pw - cm) * amv, 4)
                if act_mass <= 0:
                    raise ValueError(
                        f"活性物质质量异常: pole_weight={pw}mg, collector_mass={cm}mg, "
                        f"active_material={amv}, act_mass={act_mass}"
                    )
                cap_mAh = round(act_mass * sc / 1000.0, 4)
                if cap_mAh <= 0:
                    raise ValueError(f"容量计算异常: act_mass={act_mass}mg, capacity={sc}mAh/g, cap_mAh={cap_mAh}")

                # 4. 生成 XML 工步文件
                key = self._canon(battery_system[i])
                builder = self._get_xml_builder(gen_mod, key)
                req_args = self._get_builder_required_positional_count(builder)
                xml_content = builder(act_mass, cap_mAh) if req_args >= 2 else builder()
                recipe_path = os.path.join(xml_dir, f"{coin_id}_{devid}_{subdevid}_{chlid}.xml")
                self._save_xml(xml_content, recipe_path)

                # 5. TCP 下发测试
                resp = _start_test(
                    ip=self.ip,
                    port=int(self.port),
                    devid=devid,
                    subdevid=subdevid,
                    chlid=chlid,
                    CoinID=coin_id,
                    recipe_path=recipe_path,
                    backup_dir=backup_dir,
                    filetype=1,
                )
                submitted += 1
                results.append({
                    "index": i,
                    "coin_id": coin_id,
                    "channel": ch_name,
                    "act_mass_mg": act_mass,
                    "cap_mAh": cap_mAh,
                    "success": True,
                    "response": str(resp)[:300],
                })
                if self._ros_node:
                    self._ros_node.lab_logger().info(
                        f"[test] 已下发 {coin_id} → {ch_name}  "
                        f"act_mass={act_mass}mg  cap={cap_mAh}mAh"
                    )

            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().error(f"[test] 电池[{i}] 下发失败: {e}")
                results.append({"index": i, "success": False, "error": str(e)})

        summary = f"共 {n} 颗电池，成功下发 {submitted} 颗"
        return {
            "return_info": summary,
            "success": submitted > 0,
            "submitted_count": submitted,
            "total_count": n,
            "results": results,
        }

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
