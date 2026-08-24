"""微后端四个数据库的 SQL Repository。"""

from unilabos.server.database.repositories.history import HistoryRepository
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.database.repositories.runtime import RuntimeRepository
from unilabos.server.database.repositories.telemetry import TelemetryRepository

__all__ = [
    "HistoryRepository",
    "MaterialsRepository",
    "RuntimeRepository",
    "TelemetryRepository",
]
