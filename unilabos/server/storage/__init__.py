"""微后端旧运行库的路径、authority profile 与迁移兼容组合层。

规范数据库 schema 位于 :mod:`unilabos.server.database`；本包只负责现有
``workflow_history/inventory/device_state`` 存储迁入前的组合兼容。
"""

from unilabos.server.storage.paths import RuntimeStorageConflict, RuntimeStoragePaths
from unilabos.server.storage.profiles import (
    SchedulerAuthorityConflict,
    SchedulerAuthorityProfile,
    select_scheduler_authority_profile,
)
from unilabos.server.storage.table_contracts import MICROBACKEND_DATABASES, table_owner

__all__ = [
    "RuntimeStorageConflict",
    "RuntimeStoragePaths",
    "SchedulerAuthorityConflict",
    "SchedulerAuthorityProfile",
    "MICROBACKEND_DATABASES",
    "select_scheduler_authority_profile",
    "table_owner",
]
