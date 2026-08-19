"""微后端四个物理 SQLite 文件的独立 schema。"""

from unilabos.server.database.history import HISTORY_DATABASE
from unilabos.server.database.layout import (
    DatabaseLayoutConflict,
    ServerDatabasePaths,
    validate_distinct_database_paths,
)
from unilabos.server.database.materials import MATERIALS_DATABASE
from unilabos.server.database.runtime import RUNTIME_DATABASE
from unilabos.server.database.schema import (
    DatabaseIdentityConflict,
    DatabaseSpec,
    SchemaDriftError,
    TableSpec,
    initialize_database,
)
from unilabos.server.database.telemetry import TELEMETRY_DATABASE


DATABASE_SPECS = {
    spec.key: spec
    for spec in (
        RUNTIME_DATABASE,
        MATERIALS_DATABASE,
        TELEMETRY_DATABASE,
        HISTORY_DATABASE,
    )
}


__all__ = [
    "DATABASE_SPECS",
    "DatabaseLayoutConflict",
    "DatabaseIdentityConflict",
    "DatabaseSpec",
    "HISTORY_DATABASE",
    "MATERIALS_DATABASE",
    "RUNTIME_DATABASE",
    "ServerDatabasePaths",
    "SchemaDriftError",
    "TELEMETRY_DATABASE",
    "TableSpec",
    "initialize_database",
    "validate_distinct_database_paths",
]
