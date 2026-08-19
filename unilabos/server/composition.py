"""微后端四库服务的进程级组合根。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from unilabos.server.database import ServerDatabasePaths
from unilabos.server.services.history import HistoryService
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.runtime import RuntimeService
from unilabos.server.services.telemetry import TelemetryService


@dataclass
class ServerServices:
    """四个独立 writer；跨库只通过服务层和规范 UUID 协作。"""

    paths: ServerDatabasePaths
    runtime: RuntimeService
    materials: MaterialsService
    telemetry: TelemetryService
    history: HistoryService

    @classmethod
    def open(cls, paths: ServerDatabasePaths) -> "ServerServices":
        if not isinstance(paths, ServerDatabasePaths):
            raise TypeError("paths must be ServerDatabasePaths")

        opened: list[object] = []
        try:
            runtime = RuntimeService(paths.runtime_db)
            opened.append(runtime)
            materials = MaterialsService(paths.materials_db)
            opened.append(materials)
            telemetry = TelemetryService(paths.telemetry_db)
            opened.append(telemetry)
            history = HistoryService(paths.history_db)
            opened.append(history)
        except BaseException:
            for service in reversed(opened):
                service.close()  # type: ignore[attr-defined]
            raise
        return cls(
            paths=paths,
            runtime=runtime,
            materials=materials,
            telemetry=telemetry,
            history=history,
        )

    def close(self) -> None:
        """按与打开相反的顺序释放四个 SQLite connection。"""

        for service in (
            self.history,
            self.telemetry,
            self.materials,
            self.runtime,
        ):
            service.close()


_lock = threading.RLock()
_services: Optional[ServerServices] = None


def configure_server_services(paths: ServerDatabasePaths) -> ServerServices:
    """装配一次微后端服务；同一进程不允许运行时切换数据库。"""

    global _services
    with _lock:
        if _services is None:
            _services = ServerServices.open(paths)
        elif _services.paths != paths:
            raise RuntimeError(
                "microbackend services are already bound to another database layout"
            )
        return _services


def get_server_services() -> Optional[ServerServices]:
    with _lock:
        return _services


def shutdown_server_services() -> None:
    global _services
    with _lock:
        services = _services
        _services = None
    if services is not None:
        services.close()


__all__ = [
    "ServerServices",
    "configure_server_services",
    "get_server_services",
    "shutdown_server_services",
]
