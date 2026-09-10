"""只读持久全局事件游标（Cursor），供 REST/SSE 适配器共享。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

MAX_EVENT_SEQUENCE = (1 << 63) - 1
MAX_EVENT_PAGE_SIZE = 1000


class EventProjectionError(RuntimeError):
    """持久事件存储违反严格递增、小型失效通知投影合同时抛出。"""


class DurableEventStore(Protocol):
    """持久事件读取器所需的最小存储端口。"""

    def list_events(
        self,
        *,
        after_sequence: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """读取严格晚于游标的事件。

        参数：``after_sequence`` 是排他序号，``limit`` 是物理读取上限。返回：
        按序号递增的持久事件。异常：存储读取失败时传播底层异常。
        """

        ...


class DurableEventReader:
    """隐藏 int64 游标、分页探测和持久投影完整性检查。"""

    def __init__(self, store: DurableEventStore) -> None:
        """绑定唯一持久事件存储。

        参数：``store`` 实现只读排他游标查询。返回：无。异常：无；端口不完整
        会在首次读取时显式失败。
        """

        self._store = store

    def read(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """读取一个不可回退的持久全局事件页。

        参数：``after_sequence`` 是非负 int64 排他游标，``limit`` 是 1～1000 的
        公开页长。返回：事件、原游标、下一游标和 ``has_more``；空页保持原游标。
        异常：非法游标或页长时抛 ``ValueError``；存储返回非对象、越界、重复、
        倒序或大载荷形态时抛 ``EventProjectionError``。存储异常原样传播。
        本方法只读，不把 SSE 当成历史或状态权威。
        """

        self._validate_cursor(after_sequence)
        self._validate_limit(limit)
        projected = self._store.list_events(
            after_sequence=after_sequence,
            limit=limit + 1,
        )
        if not isinstance(projected, Sequence) or isinstance(
            projected, (str, bytes, bytearray)
        ):
            raise EventProjectionError("持久事件投影必须是序列")
        normalized: list[dict[str, Any]] = []
        previous = after_sequence
        for raw_event in projected:
            if not isinstance(raw_event, Mapping):
                raise EventProjectionError("持久事件必须是对象")
            event = dict(raw_event)
            sequence = event.get("id")
            try:
                self._validate_cursor(sequence)
            except ValueError as error:
                raise EventProjectionError("持久事件序号必须是非负 int64") from error
            if sequence <= previous:
                raise EventProjectionError("持久事件序号必须严格递增")
            previous = sequence
            if not isinstance(event.get("event"), str) or not event["event"]:
                raise EventProjectionError("持久事件类型必须是非空文本")
            if not isinstance(event.get("data"), Mapping):
                raise EventProjectionError("持久事件失效载荷必须是对象")
            event["data"] = dict(event["data"])
            normalized.append(event)
        has_more = len(normalized) > limit
        items = normalized[:limit]
        return {
            "items": items,
            "after_sequence": after_sequence,
            "next_sequence": items[-1]["id"] if items else after_sequence,
            "has_more": has_more,
        }

    @staticmethod
    def _validate_cursor(value: Any) -> None:
        """校验事件序号为非负 int64。

        参数：``value`` 是待校验值。返回：无。异常：布尔值、非整数、负数或超过
        int64 上界时抛 ``ValueError``。
        """

        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_EVENT_SEQUENCE
        ):
            raise ValueError("事件游标必须是非负 int64")

    @staticmethod
    def _validate_limit(value: Any) -> None:
        """校验公开事件页长。

        参数：``value`` 是待校验值。返回：无。异常：布尔值、非整数或超出
        1～1000 时抛 ``ValueError``。
        """

        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_EVENT_PAGE_SIZE
        ):
            raise ValueError("事件页长必须位于 1 到 1000")


__all__ = [
    "MAX_EVENT_PAGE_SIZE",
    "MAX_EVENT_SEQUENCE",
    "DurableEventReader",
    "DurableEventStore",
    "EventProjectionError",
]
