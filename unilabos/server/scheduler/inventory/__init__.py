"""Edge 本地仓储/物料库（唯一事实源）.

分层：
- domain    —— 状态机、不变量、领域错误（零外部依赖）
- schemas   —— REST/Cloud wire 的 Pydantic v2 模型（严格边界）
- store     —— SQLite WAL 持久化 + 事务 API
- service   —— 业务写操作（业务行 + ledger + outbox 同事务提交）
- sync      —— outbox worker（批量上报云端、ACK cursor、snapshot）
- commands  —— 云端 command-to-edge 幂等执行入口
- api       —— 本地 FastAPI 路由（薄层）
"""

from unilabos.server.scheduler.inventory.domain import (
    CommandRejected,
    DuplicateBarcode,
    InstanceState,
    InsufficientStock,
    InvariantViolation,
    InventoryError,
    LotState,
    MaterialRequirement,
    NotFound,
    ReservationState,
    VersionConflict,
)
from unilabos.server.scheduler.inventory.store import InventoryStore
from unilabos.server.scheduler.inventory.service import InventoryService
from unilabos.server.scheduler.inventory.schemas import (
    CloudInventoryCommandResultRequest,
    CloudInventoryEventBatch,
    CloudInventorySnapshotRequest,
    CloudResponse,
    CloudSyncAck,
    InventoryCommand,
    InventoryCommandResult,
    InventoryEvent,
)
from unilabos.server.scheduler.inventory.sync import OutboxWorker, build_snapshot
from unilabos.server.scheduler.inventory.commands import execute_command

__all__ = [
    "CloudInventoryCommandResultRequest",
    "CloudInventoryEventBatch",
    "CloudInventorySnapshotRequest",
    "CloudResponse",
    "CloudSyncAck",
    "CommandRejected",
    "DuplicateBarcode",
    "InstanceState",
    "InsufficientStock",
    "InvariantViolation",
    "InventoryError",
    "InventoryCommand",
    "InventoryCommandResult",
    "InventoryEvent",
    "InventoryService",
    "InventoryStore",
    "LotState",
    "MaterialRequirement",
    "NotFound",
    "OutboxWorker",
    "ReservationState",
    "VersionConflict",
    "build_snapshot",
    "execute_command",
]
