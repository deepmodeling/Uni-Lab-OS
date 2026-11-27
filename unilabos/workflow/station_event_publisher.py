"""工站事件发件箱的可恢复异步发布器。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from unilabos.workflow.station_event_outbox import StationEventOutboxStore
from unilabos.workflow.store import StoreConflict

logger = logging.getLogger(__name__)

StationEventSender = Callable[
    [list[dict[str, Any]]],
    Sequence[Mapping[str, Any]],
]


class StationEventPublisher:
    """按严格工站序列向 Backend 投影任务和节点作业事实。"""

    def __init__(
        self,
        outbox: StationEventOutboxStore,
        sender: StationEventSender,
        *,
        batch_size: int = 100,
        poll_interval: float = 1.0,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        """装配可恢复发布器，但不自动启动后台线程。

        参数：``outbox`` 是本地权威；``sender`` 只负责 HTTP 载荷并返回精确 ACK
        列表；其余参数控制批量与退避。返回无。异常：非法容量或时间配置抛
        ``ValueError``，避免启动后形成忙循环。
        """

        if batch_size < 1 or not 0 < poll_interval or not 0 < base_backoff:
            raise ValueError("发件箱批量和轮询时间必须为正数")
        if max_backoff < base_backoff:
            raise ValueError("最大发件箱退避不能小于基础退避")
        self._outbox = outbox
        self._sender = sender
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def flush_once(self) -> int:
        """投递一批并只结算 Backend 精确确认的连续前缀。

        参数无。返回本次 ACK 数。异常：传输异常原样抛出；ACK 不是当前批次的
        连续前缀、身份不匹配或包含重复项时抛 ``StoreConflict``。失败不会删除
        任何本地事件，下一次继续从最小未 ACK 序列重放。
        """

        events = self._outbox.list_pending(limit=self._batch_size)
        if not events:
            self._failures = 0
            return 0
        for event in events:
            self._outbox.mark_delivery_attempt(
                event_uuid=str(event["event_uuid"]),
                station_sequence=int(event["station_sequence"]),
            )
        try:
            acknowledgements = list(self._sender([dict(event) for event in events]))
            normalized = _validate_acknowledgements(events, acknowledgements)
            for event_uuid, station_sequence in normalized:
                self._outbox.acknowledge(
                    event_uuid=event_uuid,
                    station_sequence=station_sequence,
                )
        except BaseException:
            self._failures += 1
            raise
        self._failures = 0
        return len(normalized)

    def start(self) -> None:
        """幂等启动后台发布线程；已运行时不创建第二消费者。"""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="station-event-publisher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """停止发布线程，并保证正常返回后线程已经退出。

        参数：``timeout`` 是可选最大等待秒数；省略时等待当前有限时 HTTP 请求
        完成。返回：无，且正常返回即证明发布线程不再访问发件箱。异常：负超时抛
        ``ValueError``；显式超时到达后线程仍存活则抛 ``TimeoutError``，调用方
        必须保留工作流存储并稍后重试停止。
        """

        if timeout is not None and timeout < 0:
            raise ValueError("发布器停止超时不能为负数")
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise TimeoutError("工站事件发布线程未在停止预算内退出")
        self._thread = None

    def _run(self) -> None:
        """持续发布并在 Backend 断线时指数退避，事件始终保留在本地。"""

        while not self._stop.is_set():
            try:
                published = self.flush_once()
                wait = self._poll_interval if published == 0 else 0.0
            except Exception as error:  # noqa: BLE001 - 后台边界必须持续恢复
                wait = min(
                    self._base_backoff * (2 ** max(self._failures - 1, 0)),
                    self._max_backoff,
                )
                logger.warning(
                    "工站事件投影失败，第 %s 次，%.1f 秒后重试：%s",
                    self._failures,
                    wait,
                    error,
                )
            self._stop.wait(wait)


def _validate_acknowledgements(
    events: Sequence[Mapping[str, Any]],
    acknowledgements: Sequence[Mapping[str, Any]],
) -> list[tuple[str, int]]:
    """验证 Backend ACK 是本批事件的精确连续前缀。"""

    if len(acknowledgements) > len(events):
        raise StoreConflict("Backend ACK 数量超过本次工站事件批量")
    result: list[tuple[str, int]] = []
    for index, acknowledgement in enumerate(acknowledgements):
        if not isinstance(acknowledgement, Mapping):
            raise StoreConflict("Backend ACK 必须是对象")
        event_uuid = str(acknowledgement.get("event_uuid") or "").strip()
        station_sequence = acknowledgement.get("station_sequence")
        expected = events[index]
        if (
            not event_uuid
            or isinstance(station_sequence, bool)
            or not isinstance(station_sequence, int)
            or event_uuid != expected["event_uuid"]
            or station_sequence != expected["station_sequence"]
        ):
            raise StoreConflict("Backend ACK 不是当前工站事件批次的连续前缀")
        result.append((event_uuid, station_sequence))
    return result


__all__ = ["StationEventPublisher", "StationEventSender"]
