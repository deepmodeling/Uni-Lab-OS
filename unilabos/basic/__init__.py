"""Basic 进程内 backend。

该 backend 直接加载 Python 设备驱动，不提供分布式传输，用于本地开发和与中间件
无关的驱动检查。
"""

from unilabos.basic.runtime import BasicDeviceNode, BasicRuntime

__all__ = ["BasicDeviceNode", "BasicRuntime"]
