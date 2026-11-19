"""锁授予策略的接口与 Python 参考实现。

宿主（:class:`SchedulerResourceManager`）只把决策所需的事实交给策略：按到达顺序的等待请求、已被持有的
键、被 pending handoff 预留的键、哪些请求是 handoff 目标（键在本轮编号为 ``0..n`` 的整数）；策略返回
本轮授予的请求下标（按授予顺序）。宿主用 :func:`validate_grants` 复核，不合格整轮退回 :func:`fifo_grants`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable, Protocol, Sequence


@dataclass(frozen=True)
class WaitingRequest:
    keys: tuple[int, ...]
    handoff_target: bool = False


class GrantPolicy(Protocol):
    name: str

    def decide(
        self,
        waiting: Sequence[WaitingRequest],
        held: Iterable[int],
        reserved: Iterable[int],
    ) -> list[int]: ...


def fifo_grants(waiting: Sequence[WaitingRequest], held: Iterable[int], reserved: Iterable[int]) -> list[int]:
    """按键严格 FIFO（native/grant-policy 与之逐条等价）：不相交的集合并行授予，被挡住的请求阻塞后来者。"""

    taken = set(held) | set(reserved)
    blocked: set[int] = set()
    grants: list[int] = []
    for index, request in enumerate(waiting):
        keys = set(request.keys)
        if request.handoff_target or keys & taken or keys & blocked:
            blocked |= keys
            continue
        taken |= keys
        grants.append(index)
    return grants


class PythonFifoPolicy:
    name = "python-fifo"

    def decide(self, waiting, held, reserved) -> list[int]:
        return fifo_grants(waiting, held, reserved)


class KeyIndex:
    """把锁键映射为本轮决策用的连续整数编号。"""

    def __init__(self) -> None:
        self._ids: dict[Hashable, int] = {}

    def __call__(self, key: Hashable) -> int:
        return self._ids.setdefault(key, len(self._ids))


def validate_grants(
    grants: Sequence[int],
    waiting: Sequence[WaitingRequest],
    held: Iterable[int],
    reserved: Iterable[int],
) -> bool:
    """下标有效且不重复、不授予 handoff 目标、与已持有 / 预留 / 彼此不相交。"""

    taken = set(held) | set(reserved)
    seen: set[int] = set()
    for index in grants:
        if not isinstance(index, int) or index in seen or not 0 <= index < len(waiting):
            return False
        seen.add(index)
        keys = set(waiting[index].keys)
        if waiting[index].handoff_target or keys & taken:
            return False
        taken |= keys
    return True
