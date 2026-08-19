"""UniLabOS 微后端的领域记录与独立 SQLite schema。"""

from unilabos.server.database import (
    DATABASE_SPECS,
    DatabaseLayoutConflict,
    DatabaseIdentityConflict,
    HISTORY_DATABASE,
    MATERIALS_DATABASE,
    RUNTIME_DATABASE,
    ServerDatabasePaths,
    SchemaDriftError,
    TELEMETRY_DATABASE,
    DatabaseSpec,
    initialize_database,
    validate_distinct_database_paths,
)

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
    "initialize_database",
    "validate_distinct_database_paths",
]
