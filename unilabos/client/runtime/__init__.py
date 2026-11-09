"""runtime.db 域的出站客户端（运行控制 + workflow）。

``data`` 子模块（Local/HTTP runtime client）会拖进整套 SQLModel 表与协议模型（约 1s），
而 CLI 入口只为了 ``HTTPWorkflowClient`` 就会 import 本包：这些名字按需加载，
``from unilabos.client.runtime import HTTPRuntimeClient`` 写法不变。
"""

from __future__ import annotations

import importlib
from typing import Any

from unilabos.client.runtime.workflow import (
    HTTPWorkflowClient,
    TERMINAL_WORKFLOW_TASK_STATUSES,
    WorkflowClientError,
    derive_workflow_websocket_url,
    normalize_workflow_api_url,
)

_LAZY_DATA_EXPORTS = frozenset(
    {"HTTPRuntimeClient", "LocalRuntimeClient", "RuntimeHTTPError"}
)
_LAZY_LIFECYCLE_EXPORTS = frozenset({"HTTPLifecycleClient", "SchedulerDrainTimeout"})


def __getattr__(name: str) -> Any:
    if name in _LAZY_LIFECYCLE_EXPORTS:
        return getattr(importlib.import_module(f"{__name__}.lifecycle"), name)
    if name in _LAZY_DATA_EXPORTS:
        return getattr(importlib.import_module(f"{__name__}.data"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_DATA_EXPORTS | _LAZY_LIFECYCLE_EXPORTS)


__all__ = [
    "HTTPLifecycleClient",
    "SchedulerDrainTimeout",
    "HTTPRuntimeClient",
    "HTTPWorkflowClient",
    "LocalRuntimeClient",
    "RuntimeHTTPError",
    "TERMINAL_WORKFLOW_TASK_STATUSES",
    "WorkflowClientError",
    "derive_workflow_websocket_url",
    "normalize_workflow_api_url",
]
