"""正式 Backend 控制面的生产 Edge 运行 adapter。"""

from __future__ import annotations

from unilabos.app.communication import BaseCommunicationClient
from unilabos.app.control_plane import (
    ControlPlaneRuntimeContext,
    ControlPlaneRuntimeHandle,
)


def create_edge_control_client() -> BaseCommunicationClient:
    """创建未被遗留客户端缓存污染的生产协议客户端。"""

    from unilabos.app.communication import (
        CommunicationClientFactory,
        get_communication_client,
    )

    CommunicationClientFactory.reset_client()
    return get_communication_client("edge_control")


def start_backend_control_runtime(
    context: ControlPlaneRuntimeContext,
) -> ControlPlaneRuntimeHandle:
    """启动生产协议；不导入本地 Scheduler，也不创建它的三类数据库。"""

    del context
    client = create_edge_control_client()
    client.start()
    return ControlPlaneRuntimeHandle(
        bridges=(client,),
        communication_clients=(client,),
        shutdown_services=lambda: None,
    )


__all__ = ["create_edge_control_client", "start_backend_control_runtime"]
