"""串行排程策略。只根据任务身份、提交时间和在途事实选择队首。"""

from __future__ import annotations

from collections.abc import Mapping, Set


class SerialWorkflowQueue:
    """一次只推进一个任务。传入的在途身份可以包括等待结算的终态任务。"""

    def __init__(self) -> None:
        self._current: str | None = None

    def select(self, active: Mapping[str, float], inflight: Set[str]) -> str | None:
        if len(inflight) > 1:
            # 兼容从允许多任务并行的模式恢复，等待已在途的作业收敛。
            return None
        if inflight:
            self._current = next(iter(inflight))
        elif self._current not in active:
            self._current = min(active, key=lambda item: (active[item], item), default=None)
        return self._current

    def advance(
        self, active: Mapping[str, float], blocker: str | None, visited: set[str]
    ) -> bool:
        if self._current is not None:
            visited.add(self._current)
            # 纯本地控制节点结束后无需等待设备回调。
            if self._current not in active:
                return True
        if blocker is None or blocker in visited or blocker not in active:
            return False
        # 卸料等待时推进相关用料任务，资源检查和派发仍由执行内核完成。
        self._current = blocker
        return True
