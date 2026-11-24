"""前端失效通知写入器：物料账本与日志的通知写进 ``frontend_event``，与工作流事件共用一个序列。

``/api/v1/events`` 因此每一帧（水位、工作流、物料、日志、重置）都带同一个序列的整数 id：断线续传
（``Last-Event-ID``）、超前 / 过期游标的 ``stream.reset`` 与保留清理对它们一视同仁。写入器只在持有
Workflow Authority 的进程里运行（默认拓扑的权威进程、``--role backend``；单进程模式下就是这个进程），
由 ``server/execution_composition.py`` 随本机调度权威启停。

- **物料**：订阅本进程物料权威的共享账本轮询（``server/services/materials/ledger_watch.py``，约每秒读一次），
  账本每前进一批就并进待写槽，写一行 ``material.changed``，``data`` 为
  ``{"sequence": <账本末尾>, "material_uuids": [...]}``。带 ``material_uuids``：恰好是这段账本里变了的物料
  （``[]`` 表示只动了批次 / 预留，没有物料要重读）；不带：列不准（模板变化、库被重置，或超过
  :data:`MATERIAL_UUIDS_LIMIT` 个），客户端整体重读。物料权威不在本进程时不写物料通知。
- **日志**：订阅进程内日志通知中心（``utils/log_notices.py``）。Host 子进程（以及 ``--address`` 接入的 Host）
  的通知经 runtime.v1 ``runtime_logs_changed`` 到达权威，由 ``EdgeControlService`` 发布到同一个通知中心；
  Host 上下线时权威自己也发一条。合并后写一行 ``runtime.logs.changed``，``data`` 为 ``RuntimeLogNotice``。
- **合并与限速**：两类各自每 :data:`NOTICE_INTERVAL_S` 秒至多写一行。空闲后的第一条立即写；间隔内到达的
  全部并进下一行（物料取并集，任一批列不准就整体重读；日志合并来源）。一阵日志刷屏因此每秒最多一行。
- **唤醒**：写完一行后唤醒本进程的 SSE 连接立即读（:data:`appended`），不等它们下一轮轮询。
- **续写**：启动时从最新一条 ``material.changed`` 记下的账本序号之后续读，停机前没来得及写的那段账本也会补写；
  账本比记下的还短（库被重置）写一行整体重读。保留窗口里没有物料通知时从当前账本末尾开始。
- 写失败只记日志（写入器线程的日志不触发日志通知），待写内容保留，约 1 秒后重试。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from unilabos.protocol.runtime.logs import RuntimeLogNotice
from unilabos.server.services.materials.ledger_watch import LedgerBatch, LedgerWatch, ledger_watch
from unilabos.utils.log_notices import LOG_NOTICE_EVENT, NOTICE_WRITER_THREAD, log_notices, merge_notices

logger = logging.getLogger(__name__)

#: 物料账本前进的失效通知（V1 名）
MATERIAL_CHANGED_EVENT = "material.changed"
#: 物料 / 日志通知各自的最小写入间隔（秒）
NOTICE_INTERVAL_S = 1.0
#: ``material_uuids`` 最多列这么多个；再多就省略这个键，客户端整体重读比逐个重读更省
MATERIAL_UUIDS_LIMIT = 500
_RETRY_S = 1.0


def merge_material_batches(current: Optional[LedgerBatch], batch: LedgerBatch) -> LedgerBatch:
    """把多批合成一批：区间取并、物料取并；任一批列不准或重置就整体重读。``current`` 在前。"""

    if current is None:
        return batch
    changed = (
        None
        if current.reset or batch.reset or current.changed is None or batch.changed is None
        else sorted(set(current.changed) | set(batch.changed))
    )
    return LedgerBatch(after=current.after, upto=batch.upto, changed=changed, reset=current.reset or batch.reset)


def material_notice_data(batch: LedgerBatch) -> Dict[str, Any]:
    """一行 ``material.changed`` 的 ``data``；列不准、重置或列不下时省略 ``material_uuids``。"""

    data: Dict[str, Any] = {"sequence": batch.upto}
    if not batch.reset and batch.changed is not None and len(batch.changed) <= MATERIAL_UUIDS_LIMIT:
        data["material_uuids"] = list(batch.changed)
    return data


class _Appended:
    """写入器写完一行后通知本进程的 SSE 连接；回调在写入器线程上调用，必须很快返回。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: set[Callable[[], None]] = set()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def release() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return release

    def notify(self) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001 - 唤醒只是提前读，丢了下一轮轮询照样读到
                pass


#: 本进程写入器追加通知后的唤醒信号（``/api/v1/events`` 的连接订阅它）
appended = _Appended()


class FrontendNoticeWriter:
    """把物料与日志通知合并、限速后写进 ``frontend_event``（见模块说明）。"""

    def __init__(
        self,
        store: Any,
        *,
        materials: Any = None,
        hub: Any = log_notices,
        interval: float = NOTICE_INTERVAL_S,
    ) -> None:
        self._store = store
        # 物料权威服务（取它的共享账本轮询）或直接给一个 LedgerWatch；None 表示物料权威不在本进程
        self._materials = materials
        self._hub = hub
        self._interval = interval
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._material: Optional[LedgerBatch] = None
        self._log: Optional[RuntimeLogNotice] = None
        self._next_material = 0.0
        self._next_log = 0.0
        self._failures = 0
        self._releases: list[Callable[[], None]] = []
        self._thread: Optional[threading.Thread] = None

    # ── 生命周期 ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=NOTICE_WRITER_THREAD, daemon=True)
        self._thread.start()
        self._releases.append(self._hub.subscribe(self._receive_log))
        if self._materials is not None:
            watch = self._materials if isinstance(self._materials, LedgerWatch) else ledger_watch(self._materials)
            self._releases.append(watch.subscribe(self._material_cursor(), self._receive_material))

    def stop(self) -> None:
        """停止订阅与写入线程；没写出去的物料区间下次启动按记下的账本序号补写。"""

        for release in self._releases:
            release()
        self._releases.clear()
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _material_cursor(self) -> Optional[int]:
        """最新一条物料通知记下的账本序号；保留窗口里没有时返回 None（从当前账本末尾开始）。"""

        try:
            data = self._store.latest_event_data(MATERIAL_CHANGED_EVENT)
        except Exception as exc:  # noqa: BLE001 - 读不到就从当前账本末尾开始，不挡权威启动
            logger.warning("读取上次的物料通知失败，从当前账本末尾开始：%s", exc)
            return None
        sequence = data.get("sequence") if isinstance(data, dict) else None
        if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0:
            return sequence
        return None

    # ── 收通知（在别的线程上调用，只并进待写槽） ───────────────────────

    def _receive_material(self, batch: LedgerBatch) -> None:
        with self._lock:
            self._material = merge_material_batches(self._material, batch)
        self._wake.set()

    def _receive_log(self, notice: RuntimeLogNotice) -> None:
        with self._lock:
            self._log = notice if self._log is None else merge_notices(self._log, notice)
        self._wake.set()

    # ── 写入线程 ─────────────────────────────────────────────────────

    def _append(self, event: str, data: Dict[str, Any]) -> bool:
        try:
            self._store.append_frontend_event(event, data)
        except Exception as exc:  # noqa: BLE001 - 待写内容保留，稍后重试
            self._failures += 1
            if self._failures == 1 or self._failures % 60 == 0:
                logger.warning("写前端通知 %s 失败（第 %s 次），稍后重试：%s", event, self._failures, exc)
            return False
        self._failures = 0
        return True

    def _run(self) -> None:
        timeout: Optional[float] = None
        while True:
            self._wake.wait(timeout)
            self._wake.clear()
            if self._stop.is_set():
                return
            now = time.monotonic()
            with self._lock:
                material = self._material if now >= self._next_material else None
                if material is not None:
                    self._material = None
                log = self._log if now >= self._next_log else None
                if log is not None:
                    self._log = None
            wrote = False
            if material is not None:
                if self._append(MATERIAL_CHANGED_EVENT, material_notice_data(material)):
                    self._next_material = now + self._interval
                    wrote = True
                else:
                    with self._lock:
                        self._material = merge_material_batches(material, self._material) if self._material else material
                    self._next_material = now + _RETRY_S
            if log is not None:
                if self._append(LOG_NOTICE_EVENT, log.model_dump(mode="json")):
                    self._next_log = now + self._interval
                    wrote = True
                else:
                    with self._lock:
                        self._log = log if self._log is None else merge_notices(log, self._log)
                    self._next_log = now + _RETRY_S
            if wrote:
                appended.notify()
            with self._lock:
                waits = [
                    deadline - now
                    for pending, deadline in ((self._material, self._next_material), (self._log, self._next_log))
                    if pending is not None
                ]
            timeout = max(0.0, min(waits)) if waits else None


__all__ = [
    "FrontendNoticeWriter",
    "MATERIAL_CHANGED_EVENT",
    "MATERIAL_UUIDS_LIMIT",
    "NOTICE_INTERVAL_S",
    "appended",
    "material_notice_data",
    "merge_material_batches",
]
