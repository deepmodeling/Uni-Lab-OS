"""领域状态的本地持久化接口；状态含义、恢复与业务判定由领域包负责。"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ContextManager, Protocol


class LegacyState(Protocol):
    """只在首次原子迁移内读取旧记录，不向领域包开放 SQL 或连接。"""

    @property
    def tables(self) -> frozenset[str]: ...
    def rows(self, table: str, columns: tuple[str, ...]) -> list[dict[str, Any]]: ...


class LocalState(Protocol):
    """按稳定命名空间保存 JSON 记录。每次调用独立提交，transaction 可组合原子操作。

    文件丢失、被替换或命名空间不符时拒绝继续，不自动创建空库恢复运行。
    items 返回快照；写入要求完整 JSON 值，不允许 NaN、无穷值或非字符串键。
    此接口不授予动作执行或库存结算资格。
    """

    def transaction(self) -> ContextManager["LocalState"]: ...
    def get(self, collection: str, key: str, default: Any = None) -> Any: ...
    def items(self, collection: str) -> Mapping[str, Any]: ...
    def put(self, collection: str, key: str, value: Any) -> None: ...
    def delete(self, collection: str, key: str) -> None: ...
    def append(self, collection: str, value: Any) -> int: ...
    def close(self) -> None: ...


def open_local_state(
    path: str | Path, *, namespace: str, create: bool = True, readonly: bool = False,
    initialize: Callable[[LocalState, LegacyState], None] | None = None,
) -> LocalState:
    """打开 OS 管理的本地状态。初始化和旧数据迁移整体提交，成功后只运行一次。

    initialize 解释旧记录并写入新记录；旧表保留为核查快照，不能用于降级运行。
    readonly 仅查询已初始化文件；不会触发迁移或创建文件。
    """
    from unilabos.persistence.local_state import SqliteLocalState

    return SqliteLocalState(path, namespace=namespace, create=create, readonly=readonly,
                            initialize=initialize)
