"""设备命令持久化合同；领域包负责请求指纹和物理状态判定。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class CommandState(str, Enum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELED, self.REJECTED}


class CommandRequest(Protocol):
    @property
    def command_id(self) -> str: ...
    @property
    def source(self) -> str: ...
    @property
    def source_boot_id(self) -> str: ...
    @property
    def monotonic_sequence(self) -> int: ...
    def to_dict(self) -> dict[str, Any]: ...
    def fingerprint(self) -> str: ...


@dataclass(frozen=True)
class CommandRecord:
    request_hash: str
    request: Mapping[str, Any]
    state: CommandState
    boot_id: str
    message: str
    output: Mapping[str, Any]

    def public_result(self) -> dict[str, Any]:
        return {
            "command_id": str(self.request["command_id"]), "state": self.state.value,
            "success": self.state is CommandState.SUCCEEDED,
            "message": self.message, "boot_id": self.boot_id,
        }


class CommandConflictError(RuntimeError):
    pass


class CommandSequenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyCommandTables:
    """旧版同结构日志的表名，仅用于一次性原子导入；不执行领域提供的 SQL。"""
    commands: str
    epochs: str


class CommandJournal(Protocol):
    def accept(self, request: CommandRequest, boot_id: str) -> tuple[bool, CommandRecord]: ...
    def get(self, command_id: str) -> CommandRecord | None: ...
    def update(self, command_id: str, state: CommandState, boot_id: str, message: str,
               output: Mapping[str, Any] | None = None) -> CommandRecord: ...
    def has_unknown(self) -> bool: ...
    def unknown_command_ids(self) -> list[str]: ...
    def mark_previous_boot_inflight_unknown(self, current_boot_id: str) -> int: ...
    def reset(self) -> None:
        """仅供明确要求清空状态的仿真冷启动，不允许正常恢复时调用。"""
        ...


def open_command_journal(path: str | Path, *, namespace: str,
                         legacy_tables: LegacyCommandTables | None = None) -> CommandJournal:
    """打开 OS 管理的本地执行日志；保留路径，按稳定设备身份隔离记录。

    旧日志只原子导入一次，并保留旧表供备份核查。旧表已由其他命名空间认领、
    缺表或目标已有数据时拒绝导入，避免把旧命令错误归属或覆盖新事实。
    """
    from unilabos.persistence.command_journal import SqliteCommandJournal

    return SqliteCommandJournal(path, namespace=namespace, legacy_tables=legacy_tables)
