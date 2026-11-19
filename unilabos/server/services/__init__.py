"""微后端领域服务（目录严格按四库划分）。

- ``services/runtime/``：runtime.db（RuntimeService / workflow 子包 / RegistryService）
- ``services/materials/``：materials.db（MaterialsService / GraphService / 快照对比）
- ``services/telemetry.py``：telemetry.db
- ``services/history.py``：history.db

顶层名字按需加载：四库的 SQLModel 表一起构建要近 1s，而 Slave / CLI 只是为了
``services.driver_packages`` 这类轻量子模块就会触发本包 ``__init__``。
``from unilabos.server.services import MaterialsService`` 写法不变。
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY_EXPORTS = {
    "HistoryService": ".history",
    "MaterialConflictError": ".materials",
    "MaterialNoChangeError": ".materials",
    "MaterialNotFoundError": ".materials",
    "MaterialValidationError": ".materials",
    "MaterialsService": ".materials",
    "MaterialsServiceError": ".materials",
    "RejectedMutationError": ".materials",
    "compare_material_snapshot": ".materials",
    "snapshot_state_hash": ".materials",
    "RuntimeService": ".runtime",
    "TelemetryService": ".telemetry",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name, __name__), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    "MaterialConflictError",
    "MaterialNoChangeError",
    "MaterialNotFoundError",
    "MaterialValidationError",
    "MaterialsService",
    "MaterialsServiceError",
    "HistoryService",
    "RejectedMutationError",
    "RuntimeService",
    "TelemetryService",
    "compare_material_snapshot",
    "snapshot_state_hash",
]
