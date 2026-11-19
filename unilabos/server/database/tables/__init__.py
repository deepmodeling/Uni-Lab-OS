"""四个独立 SQLite 数据库的 SQLModel 表映射与数据库规格。

目录严格按四库划分，导入只看表（按库名找表，不感知库内文件拆分）：

- ``tables/runtime/``：runtime.db（运行控制 + workflow + registry，子包聚合）
- ``tables/materials.py``：materials.db（物料/位点/拓扑边/图快照）
- ``tables/telemetry.py``：telemetry.db
- ``tables/history.py``：history.db
"""

from unilabos.server.database.tables.base import SchemaIdentityRecord
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
    "runtime": (SchemaIdentityRecord, *RUNTIME_TABLE_MODELS),
    "materials": (SchemaIdentityRecord, *MATERIALS_TABLE_MODELS),
    "telemetry": (SchemaIdentityRecord, *TELEMETRY_TABLE_MODELS),
    "history": (SchemaIdentityRecord, *HISTORY_TABLE_MODELS),
}

__all__ = [
    "DATABASE_TABLE_MODELS",
    "HISTORY_DATABASE",
    "INLINE_PAYLOAD_LIMIT_BYTES",
    "MATERIALS_DATABASE",
    "RUNTIME_DATABASE",
    "TELEMETRY_DATABASE",
]
