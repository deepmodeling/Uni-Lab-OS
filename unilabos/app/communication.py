"""应用启动层的 Backend 会话入口。

连接实现及生命周期归属于 :mod:`unilabos.server.backend`；这里仅保留稳定的
应用层导入路径，避免 CLI、HostNode 和运行时工具各自创建连接。
"""

from unilabos.server.backend.session import (
    APP_BRIDGES,
    COMMUNICATION_PROTOCOL,
    BackendSessionFactory,
    BaseCommunicationClient,
    CommunicationClientFactory,
    get_backend_client,
    get_communication_client,
)

__all__ = [
    "APP_BRIDGES",
    "COMMUNICATION_PROTOCOL",
    "BackendSessionFactory",
    "BaseCommunicationClient",
    "CommunicationClientFactory",
    "get_backend_client",
    "get_communication_client",
]
