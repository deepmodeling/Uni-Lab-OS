"""原生错误决定的不可变尝试处置；不承载设备参数或重试引擎。"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal, TypedDict, cast

RecoveryDispositionName = Literal["retry_execution", "acknowledge_without_execution"]


class RecoveryDisposition(TypedDict):
    """绑定已验证决定与来源尝试的控制事实。"""

    disposition: RecoveryDispositionName
    decision_id: str
    decision_revision: int
    source_job_uuid: str


DISPOSITIONS = frozenset({"retry_execution", "acknowledge_without_execution"})
_current: ContextVar[RecoveryDispositionName | None] = ContextVar("action_recovery_disposition", default=None)


def normalize_recovery_disposition(value: Any) -> RecoveryDisposition | None:
    """校验只由已认证决定创建的尝试元数据。"""
    if value is None:
        return None
    fields = {"disposition", "decision_id", "decision_revision", "source_job_uuid"}
    if not isinstance(value, Mapping) or set(value) != fields or not isinstance(value["disposition"], str) or value["disposition"] not in DISPOSITIONS:
        raise ValueError("动作恢复处置无效")
    from uuid import UUID
    for key in ("decision_id", "source_job_uuid"):
        if not isinstance(value[key], str) or str(UUID(value[key])) != value[key]:
            raise ValueError("动作恢复身份无效")
    revision = value["decision_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("动作恢复决定修订无效")
    return cast(RecoveryDisposition, dict(value))


def capture_action_recovery_disposition() -> RecoveryDispositionName | None:
    """读取当前可信调用的恢复处置，普通调用返回 None。"""
    return _current.get()


@contextmanager
def attach_action_recovery_disposition(value: Any) -> Iterator[None]:
    """在现有执行身份边界绑定处置并始终恢复调用上下文。"""
    normalized = normalize_recovery_disposition(value)
    token = _current.set(normalized["disposition"] if normalized else None)
    try:
        yield
    finally:
        _current.reset(token)
