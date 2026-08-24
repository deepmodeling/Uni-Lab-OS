from typing import Any, Dict, Tuple

from unilabos.utils.tools import normalize_json as _normalize_device


def collect_devices_and_resources(
    lab_registry: Any,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """纯读取 Registry，供正式后端与微后端共享同一份模板输入。"""

    devices = {
        item["id"]: _normalize_device(item)
        for item in lab_registry.obtain_registry_device_info()
    }
    resources = {
        item["id"]: item for item in lab_registry.obtain_registry_resource_info()
    }
    return devices, resources

__all__ = ["collect_devices_and_resources"]
