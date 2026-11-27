"""兼容旧导入路径：工站基类已移到 ``unilabos.workstation``。

已有设备包按本模块路径导入，拿到的类对象与新路径完全相同，``issubclass`` 判断不受影响。
新代码请从 ``unilabos.workstation`` 导入。
"""

from unilabos.workstation.base import (
    ProtocolNode,
    ResourceSynchronizer,
    WorkflowInfo,
    WorkflowStatus,
    WorkstationBase,
    WorkStationContainer,
    get_workstation_plate_resource,
)

__all__ = [
    "ProtocolNode",
    "ResourceSynchronizer",
    "WorkStationContainer",
    "WorkflowInfo",
    "WorkflowStatus",
    "WorkstationBase",
    "get_workstation_plate_resource",
]
