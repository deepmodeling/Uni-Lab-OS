"""
设备工具函数模块

提供处理设备配置的辅助函数
"""

import json
from typing import Dict, Any

# 这里不能循环导入
# 在函数内部导入process_device_actions
# from unilabos.web.utils.action_utils import process_device_actions


def get_registry_info() -> Dict[str, Any]:
    """获取Registry相关路径信息

    Returns:
        包含Registry路径信息的字典
    """
    from unilabos.registry.registry import lab_registry
    from pathlib import Path

    registry_info = {}

    if lab_registry:
        # 获取所有registry路径
        if hasattr(lab_registry, "registry_paths") and lab_registry.registry_paths:
            # 保存所有注册表路径
            registry_info["paths"] = [str(path).replace("\\", "/") for path in lab_registry.registry_paths]

            # 获取设备和资源的相关路径
            for reg_path in lab_registry.registry_paths:
                base_path = Path(reg_path)

                # 检查设备目录
                devices_path = base_path / "devices"
                if devices_path.exists():
                    if "devices_paths" not in registry_info:
                        registry_info["devices_paths"] = []
                    registry_info["devices_paths"].append(str(devices_path).replace("\\", "/"))

                # 检查设备通信目录
                device_comms_path = base_path / "device_comms"
                if device_comms_path.exists():
                    if "device_comms_paths" not in registry_info:
                        registry_info["device_comms_paths"] = []
                    registry_info["device_comms_paths"].append(str(device_comms_path).replace("\\", "/"))

                # 检查资源目录
                resources_path = base_path / "resources"
                if resources_path.exists():
                    if "resources_paths" not in registry_info:
                        registry_info["resources_paths"] = []
                    registry_info["resources_paths"].append(str(resources_path).replace("\\", "/"))

    return registry_info
