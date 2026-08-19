"""微后端协议客户端。"""

from unilabos.server.clients.history import (
    HTTPHistoryClient,
    HistoryHTTPError,
    LocalHistoryClient,
)
from unilabos.server.clients.materials import (
    HTTPMaterialsClient,
    HostLinkMaterialsClient,
    LocalMaterialsClient,
    MaterialsHTTPError,
    bind_payload,
)
from unilabos.server.clients.runtime import (
    HTTPRuntimeClient,
    LocalRuntimeClient,
    RuntimeHTTPError,
)
from unilabos.server.clients.telemetry import (
    HTTPTelemetryClient,
    LocalTelemetryClient,
    TelemetryHTTPError,
)

__all__ = [
    "HTTPHistoryClient",
    "HTTPMaterialsClient",
    "HostLinkMaterialsClient",
    "HTTPRuntimeClient",
    "HTTPTelemetryClient",
    "HistoryHTTPError",
    "LocalHistoryClient",
    "LocalMaterialsClient",
    "LocalRuntimeClient",
    "LocalTelemetryClient",
    "MaterialsHTTPError",
    "RuntimeHTTPError",
    "TelemetryHTTPError",
    "bind_payload",
]
