"""
主机节点工具模块

提供与主机节点相关的工具函数
"""

import time
from typing import Dict, Any

from unilabos.config.config import BasicConfig
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos.app.web.utils.action_utils import get_action_info


def get_host_node_info() -> Dict[str, Any]:
    """
    获取主机节点信息

    尝试获取HostNode实例并提取其设备、主题和动作客户端信息

    Returns:
        Dict: 包含主机节点信息的字典
    """
    host_info = {
        "available": False,
        "devices": {},
        "subscribed_topics": [],
        "action_clients": {},
        "business_actions": {},
    }
    if not BasicConfig.is_host_mode:
        return host_info
    # 尝试获取HostNode实例，设置超时为0秒
    host_node = HostNode.get_instance(0)
    if not host_node:
        return host_info
    host_info["available"] = True
    host_info["devices"] = {
        edge_device_id: {
            "namespace": namespace,
            "is_online": f"{namespace}/{edge_device_id}" in host_node._online_devices,
            "key": f"{namespace}/{edge_device_id}" if namespace.startswith("/") else f"/{namespace}/{edge_device_id}",
            "machine_name": host_node.device_machine_names.get(edge_device_id, "未知"),
        }
        for edge_device_id, namespace in host_node.devices_names.items()
    }
    # 获取已订阅的主题
    host_info["subscribed_topics"] = sorted(list(host_node._subscribed_topics))
    # 获取动作客户端信息
    for action_id, client in host_node._action_clients.items():
        host_info["action_clients"][action_id] = get_action_info(client, full_name=action_id)

    # @action 生成的 UniLabJsonCommand 复用底层 command ROS action，不会出现在
    # _action_clients 中；单独展示业务映射，避免主机页误认为动作未注册。
    for device_id, mappings in host_node._action_value_mappings.items():
        for action_name, mapping in (mappings or {}).items():
            action_type = str(mapping.get("type", ""))
            if not action_type.startswith("UniLabJsonCommand") or action_name == "auto-close":
                continue
            schema = mapping.get("schema") or {}
            key = f"{device_id}/{action_name}"
            host_info["business_actions"][key] = {
                "device_id": device_id,
                "action_name": action_name,
                "action_type": action_type,
                "description": schema.get("description", ""),
            }

    # 获取设备状态
    host_info["device_status"] = host_node.device_status

    # 添加设备状态更新时间戳
    current_time = time.time()
    host_info["device_status_timestamps"] = {}
    for device_id, properties in host_node.device_status_timestamps.items():
        host_info["device_status_timestamps"][device_id] = {}
        for prop_name, timestamp in properties.items():
            if timestamp > 0:  # 只处理有效的时间戳
                host_info["device_status_timestamps"][device_id][prop_name] = {
                    "timestamp": timestamp,
                    "elapsed": round(current_time - timestamp, 2),  # 计算经过的时间（秒）
                }
            else:
                host_info["device_status_timestamps"][device_id][prop_name] = {
                    "timestamp": 0,
                    "elapsed": -1,  # 表示未曾更新过
                }

    return host_info
