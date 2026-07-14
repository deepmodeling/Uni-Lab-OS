"""设备资源池管理.

对 models/resources.py 中 DevicePool 的高层封装, 提供调度器所需的分配/释放接口.
"""

from __future__ import annotations

from scheduler.models.resources import DeviceInstance, DevicePool


def build_device_pool(machines: list[dict]) -> DevicePool:
    """从 API 请求的 machines 列表构建 DevicePool.

    Args:
        machines: [{"type": "A", "count": 3}, ...]
    """
    pool = DevicePool()
    for m in machines:
        pool.add_device_type(m["type"], m["count"])
    return pool
