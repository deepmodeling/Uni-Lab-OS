"""runtime.db 域的 HTTP API（运行控制 + runtime.v1 控制面通道 + 诊断 + workflow + registry 路由）。"""

from unilabos.server.api.runtime.control import install_edge_control_api
from unilabos.server.api.runtime.data import (
    create_runtime_router,
    install_runtime_api,
)
from unilabos.server.api.runtime.diagnostics import (
    create_backend_app,
    create_backend_router,
)
from unilabos.server.api.runtime.registry import install_registry_api
from unilabos.server.api.runtime.workflow import (
    create_workflow_app,
    create_workflow_router,
    format_sse_event,
    install_workflow_api,
)

__all__ = [
    "create_backend_app",
    "create_backend_router",
    "create_runtime_router",
    "create_workflow_app",
    "create_workflow_router",
    "format_sse_event",
    "install_edge_control_api",
    "install_registry_api",
    "install_runtime_api",
    "install_workflow_api",
]
