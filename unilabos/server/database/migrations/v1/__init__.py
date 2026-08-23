"""四个微后端数据库的 v1 migration snapshots。"""

from unilabos.server.database.migrations.v1.history import HISTORY_DATABASE
from unilabos.server.database.migrations.v1.materials import MATERIALS_DATABASE
from unilabos.server.database.migrations.v1.runtime import RUNTIME_DATABASE
from unilabos.server.database.migrations.v1.telemetry import TELEMETRY_DATABASE

__all__ = [
    "HISTORY_DATABASE",
    "MATERIALS_DATABASE",
    "RUNTIME_DATABASE",
    "TELEMETRY_DATABASE",
]
