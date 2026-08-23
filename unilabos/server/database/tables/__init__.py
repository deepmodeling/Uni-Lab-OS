"""四个独立 SQLite 数据库的 SQLModel 表映射与数据库规格。"""

from unilabos.server.database.tables.base import SchemaMigrationRecord
from unilabos.server.database.tables.history import (
    HISTORY_DATABASE,
    HISTORY_TABLE_MODELS,
    INLINE_PAYLOAD_LIMIT_BYTES,
)
from unilabos.server.database.tables.materials import (
    MATERIALS_DATABASE,
    MATERIALS_TABLE_MODELS,
)
from unilabos.server.database.tables.runtime import (
    RUNTIME_DATABASE,
    RUNTIME_TABLE_MODELS,
)
from unilabos.server.database.tables.telemetry import (
    TELEMETRY_DATABASE,
    TELEMETRY_TABLE_MODELS,
)


DATABASE_TABLE_MODELS = {
    "runtime": (SchemaMigrationRecord, *RUNTIME_TABLE_MODELS),
    "materials": (SchemaMigrationRecord, *MATERIALS_TABLE_MODELS),
    "telemetry": (SchemaMigrationRecord, *TELEMETRY_TABLE_MODELS),
    "history": (SchemaMigrationRecord, *HISTORY_TABLE_MODELS),
}

__all__ = [
    "DATABASE_TABLE_MODELS",
    "HISTORY_DATABASE",
    "INLINE_PAYLOAD_LIMIT_BYTES",
    "MATERIALS_DATABASE",
    "RUNTIME_DATABASE",
    "TELEMETRY_DATABASE",
]
