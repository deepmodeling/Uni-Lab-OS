import json
import time
from typing import Optional, Tuple, Dict, Any

from unilabos.utils.log import logger
from unilabos.utils.type_check import TypeEncoder


def register_devices_and_resources(lab_registry, gather_only=False) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    注册设备和资源到服务器（仅支持HTTP）
    """

    # 注册资源信息 - 使用HTTP方式
    from unilabos.app.web.client import http_client

    logger.info("[UniLab Register] 开始注册设备和资源...")

    # 注册设备信息
    devices_to_register = {}
    for device_info in lab_registry.obtain_registry_device_info():
        devices_to_register[device_info["id"]] = json.loads(
            json.dumps(device_info, ensure_ascii=False, cls=TypeEncoder)
        )
        logger.debug(f"[UniLab Register] 收集设备: {device_info['id']}")

    resources_to_register = {}
    for resource_info in lab_registry.obtain_registry_resource_info():
        resources_to_register[resource_info["id"]] = resource_info
        logger.debug(f"[UniLab Register] 收集资源: {resource_info['id']}")

    if gather_only:
        return devices_to_register, resources_to_register
    # 注册设备
    if devices_to_register:
        try:
            start_time = time.time()
            response = http_client.resource_registry({"resources": list(devices_to_register.values())})
            cost_time = time.time() - start_time
            if response.status_code in [200, 201]:
                logger.info(f"[UniLab Register] 成功注册 {len(devices_to_register)} 个设备 {cost_time}s")
            else:
                logger.error(f"[UniLab Register] 设备注册失败: {response.status_code}, {response.text} {cost_time}s")
        except Exception as e:
            logger.error(f"[UniLab Register] 设备注册异常: {e}")

    # 注册资源
    if resources_to_register:
        try:
            start_time = time.time()
            response = http_client.resource_registry({"resources": list(resources_to_register.values())})
            cost_time = time.time() - start_time
            if response.status_code in [200, 201]:
                logger.info(f"[UniLab Register] 成功注册 {len(resources_to_register)} 个资源 {cost_time}s")
            else:
                logger.error(f"[UniLab Register] 资源注册失败: {response.status_code}, {response.text} {cost_time}s")
        except Exception as e:
            logger.error(f"[UniLab Register] 资源注册异常: {e}")

    logger.info("[UniLab Register] 设备和资源注册完成.")
