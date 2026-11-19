"""微后端四个物理 SQLite 文件的独立 schema。"""

from unilabos.server.database.layout import (
    DatabaseLayoutConflict,
    ServerDatabasePaths,
    validate_distinct_database_paths,
)
from unilabos.server.database.root_lock import (
    DatabaseRootLock,
    DatabaseRootLockError,
    DatabaseRootLocked,
    claim_database_root,
    release_database_root,
)
from unilabos.server.database.schema import (
    DatabaseIdentityConflict,
    DatabaseMigrationFailed,
    DatabaseMigrationRequired,
    DatabaseSpec,
    Migration,
    TableSpec,
    initialize_database,
    sql_migration,
)
from unilabos.server.database.tables import (
    DATABASE_TABLE_MODELS,
    HISTORY_DATABASE,
    MATERIALS_DATABASE,
    RUNTIME_DATABASE,
    TELEMETRY_DATABASE,
)


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
    "DATABASE_TABLE_MODELS",
    "DatabaseLayoutConflict",
    "DatabaseIdentityConflict",
    "DatabaseRootLock",
    "DatabaseRootLockError",
    "DatabaseRootLocked",
    "DatabaseMigrationFailed",
    "DatabaseMigrationRequired",
    "DatabaseSpec",
    "HISTORY_DATABASE",
    "MATERIALS_DATABASE",
    "RUNTIME_DATABASE",
    "ServerDatabasePaths",
    "TELEMETRY_DATABASE",
    "Migration",
    "TableSpec",
    "claim_database_root",
    "initialize_database",
    "release_database_root",
    "sql_migration",
    "validate_distinct_database_paths",
]
