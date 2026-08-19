import time
from typing import Any, Dict, Optional, Tuple

from unilabos.utils.log import logger
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


def register_devices_and_resources(
    lab_registry: Any,
    gather_only: bool = False,
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """收集注册表；legacy 模式可继续上传到原后端。"""

    devices, resources = collect_devices_and_resources(lab_registry)
    if gather_only:
        return devices, resources

    from unilabos.legacy_support.http import get_legacy_http_client

    http_client = get_legacy_http_client()
    for tag, values in (
        ("device_registry", devices),
        ("resource_registry", resources),
    ):
        if not values:
            continue
        started = time.time()
        try:
            response = http_client.resource_registry(
                {"resources": list(values.values())},
                tag=tag,
            )
            elapsed = time.time() - started
            if response.status_code in {200, 201}:
                logger.info(
                    "[UniLab Register] %s 上传完成：%s 个，%.3fs",
                    tag,
                    len(values),
                    elapsed,
                )
            else:
                logger.error(
                    "[UniLab Register] %s 上传失败：%s %s",
                    tag,
                    response.status_code,
                    response.text,
                )
        except Exception as exc:
            logger.error("[UniLab Register] %s 上传异常：%s", tag, exc)
    return None
