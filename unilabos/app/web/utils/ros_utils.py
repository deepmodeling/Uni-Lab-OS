"""
ROS 工具函数模块

提供处理 ROS 节点信息的辅助函数
"""

import traceback
from typing import Dict, Any

# 存储 ROS 节点信息的全局变量
ros_node_info = {"online_devices": {}, "device_topics": {}, "device_actions": {}}


def get_ros_node_info() -> Dict[str, Any]:
    """获取 ROS 节点信息，包括设备节点、发布的状态和动作

    Returns:
        包含 ROS 节点信息的字典
    """
    global ros_node_info
    # 触发更新以获取最新信息
    update_ros_node_info()
    return ros_node_info


def update_ros_node_info() -> Dict[str, Any]:
    """更新 ROS 节点信息，使用全局设备注册表

    Returns:
        更新后的 ROS 节点信息字典
    """
    global ros_node_info
    result = {"registered_devices": {}, "device_topics": {}, "device_actions": {}}

    from unilabos.config.config import BasicConfig

    if BasicConfig.backend != "ros2":
        ros_node_info = result
        return result

    try:
        from unilabos.app.web.utils.action_utils import get_action_info
        from unilabos.ros.nodes.base_device_node import registered_devices
        from unilabos.ros.nodes.presets.host_node import HostNode

        # 尝试获取主机节点实例
        host_node = HostNode.get_instance(0)
        device_machine_names = {}
        if host_node:
            device_machine_names = host_node.device_machine_names

        for device_id, device_info in registered_devices.items():
            # 设备基本信息
            result["registered_devices"][device_id] = {
                "node_name": device_info["node_name"],
                "namespace": device_info["namespace"],
                "uuid": device_info["uuid"],
                "machine_name": device_machine_names.get(device_id, "本地"),
            }

            # 设备话题（状态）信息
            result["device_topics"][device_id] = {
                k: {
                    "type_name": v.msg_type.__module__ + "." + v.msg_type.__name__,
                    "timer_period": v.timer_period,
                    "topic_path": device_info["base_node_instance"].namespace + "/" + v.name,
                }
                for k, v in device_info["status_publishers"].items()
            }

            # 设备动作信息
            result["device_actions"][device_id] = {k: get_action_info(v, k) for k, v in device_info["actions"].items()}
        # 更新全局变量
        ros_node_info = result
    except Exception as e:
        print(f"更新ROS节点信息出错: {e}")
        traceback.print_exc()

    return result
