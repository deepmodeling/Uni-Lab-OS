from typing import Any, Dict, Optional, Tuple

from unilabos.utils.log import logger
from unilabos.utils.tools import normalize_json as _normalize_device


def collect_devices_and_resources(
    lab_registry: Any,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """从同一个 Registry 快照收集设备模板和器材模板。"""

    logger.info("[UniLab Register] 开始收集设备和资源模板...")

    devices_to_register = {}
    for device_info in lab_registry.obtain_registry_device_info():
        devices_to_register[device_info["id"]] = _normalize_device(device_info)
        logger.trace(f"[UniLab Register] 收集设备: {device_info['id']}")

    resources_to_register = {}
    for resource_info in lab_registry.obtain_registry_resource_info():
        resources_to_register[resource_info["id"]] = resource_info
        logger.trace(f"[UniLab Register] 收集资源: {resource_info['id']}")

    return devices_to_register, resources_to_register


def register_devices_and_resources(
    lab_registry: Any, gather_only: bool = False
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """兼容旧收集调用；常驻 Edge 不再拥有模板写入口。"""

    templates = collect_devices_and_resources(lab_registry)
    if gather_only:
        return templates
    raise RuntimeError(
        "startup registry upload has been removed; run the independent template-sync job"
    )
