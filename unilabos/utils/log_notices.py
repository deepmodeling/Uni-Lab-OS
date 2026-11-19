"""日志追加后的有界轻通知；不在写日志的线程中执行网络 I/O。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from unilabos.protocol.runtime.logs import RuntimeLogNotice

LOG_NOTICE_EVENT = "runtime.logs.changed"


def merge_notices(left: RuntimeLogNotice, right: RuntimeLogNotice) -> RuntimeLogNotice:
    sources = set(left.source_ids) | set(right.source_ids)
    all_sources = left.all_sources or right.all_sources or len(sources) > 256
    return RuntimeLogNotice(
        source_ids=[] if all_sources else sorted(sources),
        all_sources=all_sources,
        sources_changed=left.sources_changed or right.sources_changed,
    )


class LogNoticeHub:
    """突发日志合并为每 200ms 至多一条通知；无追加时阻塞等待，不轮询文件。"""

    def __init__(self, batch_seconds: float = .2):
        self.batch_seconds = batch_seconds
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending: RuntimeLogNotice | None = None
        self._listeners: set[Callable[[RuntimeLogNotice], None]] = set()
        self._worker: threading.Thread | None = None

    def subscribe(self, listener: Callable[[RuntimeLogNotice], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, daemon=True, name="RuntimeLogNotices")
                self._worker.start()

        def release():
            with self._lock:
                self._listeners.discard(listener)

        return release

    def publish(self, notice: RuntimeLogNotice) -> None:
        with self._lock:
            if self._stop.is_set() or not self._listeners:
                return
            self._pending = merge_notices(self._pending or RuntimeLogNotice(), notice)
        self._wake.set()

    def changed(self, source_id: str = "", *, sources_changed: bool = False, all_sources: bool = False) -> None:
        self.publish(RuntimeLogNotice(source_ids=[source_id] if source_id else [],
                                      sources_changed=sources_changed, all_sources=all_sources))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.wait(self.batch_seconds):
                return
            with self._lock:
                notice, self._pending = self._pending, None
                self._wake.clear()
                listeners = tuple(self._listeners)
            if notice is not None:
                for listener in listeners:
                    try:
                        listener(notice)
                    except Exception:  # noqa: BLE001 - 轻通知允许丢失，重连校准；这里不能递归写日志
                        pass

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._worker is not None:
            self._worker.join(timeout=2)


log_notices = LogNoticeHub()
_local_source: str | None = "host"


def set_local_log_source(source_id: str | None) -> str | None:
    """分离部署的权威只中继 Host 通知，自身日志不能冒充 Host 日志。"""
    global _local_source
    previous, _local_source = _local_source, source_id
    return previous


class LogAppendHandler(logging.Handler):
    """在 FileHandler 写完后标记本进程；通知/读取自身的传输日志不再触发通知。"""

    def emit(self, record: logging.LogRecord) -> None:
        if _local_source is None or record.threadName == "RuntimeLogNotices":
            return
        if record.name.startswith(("uvicorn", "websockets", "httpx", "httpcore", "urllib3", "unilabos.comm")):
            return
        # 拦住控制面读取失败的诊断行，否则「读日志 → 失败日志 → 再读」会形成自激循环。
        if any(marker in record.getMessage() for marker in ("process.log.", "/hostlink/log", "runtime_logs_changed")):
            return
        log_notices.changed(_local_source)
