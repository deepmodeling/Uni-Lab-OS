"""Workflow Authority 存储层的冲突与缺失错误。"""

from __future__ import annotations


class StoreNotFound(LookupError):
    pass


class StoreConflict(RuntimeError):
    pass


class StoreRevisionConflict(StoreConflict):
    pass


class StoreAuthoringConflict(StoreConflict):
    """Apply 事务提交前发生了 Authoring 前置条件冲突。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


__all__ = [
    "StoreAuthoringConflict",
    "StoreConflict",
    "StoreNotFound",
    "StoreRevisionConflict",
]
