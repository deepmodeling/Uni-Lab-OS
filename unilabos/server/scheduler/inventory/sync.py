"""Outbox 同步：批量上报云端、ACK cursor、指数退避、snapshot.

协议（Edge → 云）：
- 批量 POST /api/v1/edge/sync/events，body = {"edge_id", "events": [envelope...]}
- 云端按 (edge_id, event_id) 去重、按 aggregate_version 防乱序覆盖
- 响应 {"acked_sequence": N}（连续 ACK 水位）；Edge 只推进 cursor，不删除 outbox 行
- 发送失败指数退避；未 ACK 的 outbox 永久保留在 SQLite（crash 后可回放）
- 初次接入/缺口用 POST /api/v1/edge/sync/snapshot，Local snapshot 先显式转换为
  {"edge_id", "snapshot_sequence", "aggregates": {...}}

sender 以 callable 注入（返回 acked_sequence），领域层不直接依赖 HTTP。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, List, Mapping, Optional

from unilabos.server.scheduler.inventory.schemas import (
    InventoryEvent,
    InventorySnapshotResponse,
    JsonObject,
)
from unilabos.server.scheduler.inventory.store import (
    InvalidCursorAdvance,
    InventoryStore,
)
from unilabos.utils.tracing import (
    add_event,
    extract_trace_context,
    inject_trace_context,
    span,
)

logger = logging.getLogger(__name__)

#: sender 签名：输入事件 envelope 列表，返回云端确认的连续 acked_sequence；失败抛异常
SyncSender = Callable[[List[JsonObject]], int]


def _row_to_envelope(row: Mapping[str, object]) -> InventoryEvent:
    envelope: JsonObject = {
        "event_id": row["event_id"],
        "edge_id": row["edge_id"],
        "lab_id": row["lab_id"],
        "sequence": row["sequence"],
        "aggregate_type": row["aggregate_type"],
        "aggregate_id": row["aggregate_id"],
        "aggregate_version": row["aggregate_version"],
        "event_type": row["event_type"],
        "occurred_at": row["occurred_at"],
        "causation_id": row["causation_id"],
        "payload": json.loads(str(row["payload_json"])),
    }
    for key in ("traceparent", "tracestate", "trace_id", "span_id"):
        if row.get(key):
            envelope[key] = row[key]
    return InventoryEvent.model_validate(envelope)


class OutboxWorker:
    """后台批量推送 sync_outbox；cursor 只前进，crash 后从 cursor 回放."""

    def __init__(
        self,
        store: InventoryStore,
        sender: SyncSender,
        batch_size: int = 100,
        poll_interval: float = 1.0,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
        cursor_name: str = "cloud",
    ):
        self.store = store
        self.sender = sender
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.cursor_name = cursor_name
        self._failures = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 同步一批（可单独调用，测试友好） -----------------------------------

    def flush_once(self) -> int:
        """推送一批未 ACK 事件，返回本次被 ACK 的事件数；失败抛异常并累积退避."""
        acked_from = self.store.get_cursor(self.cursor_name)
        rows = self.store.pending_outbox(acked_from, self.batch_size)
        if not rows:
            return 0
        sequences = [int(row["sequence"]) for row in rows]
        expected = list(range(acked_from + 1, acked_from + 1 + len(rows)))
        if sequences != expected:
            self._failures += 1
            raise InvalidCursorAdvance(
                f"outbox batch is not contiguous after {acked_from}: {sequences}"
            )
        envelopes = [
            _row_to_envelope(row).model_dump(mode="json", exclude_none=True)
            for row in rows
        ]
        first_parent = extract_trace_context(envelopes[0])
        # 每条领域事件都从其入库时保存的 W3C 上下文继续，并把新的 producer
        # 上下文写回 envelope；云端可直接把 ingest span 接成其子 span。
        for envelope in envelopes:
            parent = extract_trace_context(envelope)
            with span(
                "inventory.outbox.publish",
                kind="producer",
                parent_context=parent,
                attributes={
                    "inventory.event.id": envelope["event_id"],
                    "inventory.event.type": envelope["event_type"],
                    "inventory.aggregate.type": envelope["aggregate_type"],
                    "inventory.aggregate.id": envelope["aggregate_id"],
                    "inventory.outbox.sequence": envelope["sequence"],
                },
            ):
                inject_trace_context(envelope)
        with span(
            "inventory.outbox.flush",
            kind="producer",
            parent_context=first_parent,
            attributes={
                "inventory.outbox.batch_size": len(envelopes),
                "inventory.outbox.sequence.first": rows[0]["sequence"],
                "inventory.outbox.sequence.last": rows[-1]["sequence"],
            },
        ) as flush_span:
            try:
                # Injection mutates the wire dict; validate again so fake and
                # production senders observe the exact same Cloud DTO.
                envelopes = [
                    InventoryEvent.model_validate(envelope).model_dump(
                        mode="json", exclude_none=True
                    )
                    for envelope in envelopes
                ]
                acked_sequence = int(self.sender(envelopes))
                self.store.advance_cursor(
                    self.cursor_name,
                    expected_current=acked_from,
                    acked_sequence=acked_sequence,
                    sent_through=sequences[-1],
                    now_ms=int(time.time() * 1000),
                )
            except Exception:
                self._failures += 1
                raise
            else:
                add_event(
                    "inventory.outbox.ack",
                    {"inventory.outbox.acked_sequence": acked_sequence},
                    span=flush_span,
                )
        self._failures = 0
        return sum(1 for r in rows if r["sequence"] <= acked_sequence)

    def flush_all(self, max_batches: int = 1000) -> int:
        """连续推送直到 outbox 清空（供恢复/测试用）."""
        total = 0
        for _ in range(max_batches):
            n = self.flush_once()
            if n == 0:
                break
            total += n
        return total

    def backlog(self) -> int:
        return self.store.max_outbox_sequence() - self.store.get_cursor(self.cursor_name)

    # -- 后台线程 -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="inventory-outbox", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sent = self.flush_once()
                wait = self.poll_interval if sent == 0 else 0.0
            except Exception as exc:  # 网络/云端错误：指数退避，事件保留
                wait = min(self.base_backoff * (2 ** (self._failures - 1)), self.max_backoff)
                logger.warning("outbox flush failed (attempt %s, retry in %.1fs): %s",
                               self._failures, wait, exc)
            self._stop.wait(wait)


# ---------------------------------------------------------------------------
# Snapshot（初次接入 / 缺口重建）
# ---------------------------------------------------------------------------


def build_snapshot(store: InventoryStore) -> JsonObject:
    """导出 Edge 全量状态：云端可据此重建 projection，与 ledger 对账."""
    snapshot = InventorySnapshotResponse.model_validate(
        {
            "snapshot_sequence": store.max_outbox_sequence(),
            **store.snapshot_rows(),
        }
    )
    return snapshot.model_dump(mode="json")


# ---------------------------------------------------------------------------
# 云端投影参考实现（契约测试用；Go 侧 inbox/projection 需实现同等语义）
# ---------------------------------------------------------------------------


class CloudProjectionReference:
    """云端 sync inbox 的参考语义：(edge_id,event_id) 去重 + aggregate_version 防乱序.

    Go 实现必须满足：
    1. 重复 event_id 幂等（返回 ACK 但不重复应用）
    2. aggregate_version <= 已应用版本的事件直接跳过（乱序不覆盖新状态）
    3. ACK 水位 = 连续收到的最大 sequence
    """

    def __init__(self) -> None:
        self.seen: set = set()                      # (edge_id, event_id)
        self.versions: Dict[str, int] = {}          # aggregate_key -> version
        self.state: Dict[str, JsonObject] = {}  # aggregate_key -> 最新 payload
        self.acked_sequence = 0

    def ingest(self, events: List[JsonObject]) -> int:
        validated = [
            InventoryEvent.model_validate(event).model_dump(
                mode="json", exclude_none=True
            )
            for event in events
        ]
        for ev in sorted(validated, key=lambda e: e["sequence"]):
            dedupe_key = (ev["edge_id"], ev["event_id"])
            if dedupe_key not in self.seen:
                self.seen.add(dedupe_key)
                agg_key = f"{ev['aggregate_type']}:{ev['aggregate_id']}"
                if ev["aggregate_version"] > self.versions.get(agg_key, 0):
                    self.versions[agg_key] = ev["aggregate_version"]
                    self.state[agg_key] = {
                        "event_type": ev["event_type"],
                        "payload": ev["payload"],
                        "version": ev["aggregate_version"],
                    }
            if ev["sequence"] == self.acked_sequence + 1:
                self.acked_sequence = ev["sequence"]
            elif ev["sequence"] > self.acked_sequence + 1:
                # 缺口：不推进水位，等 Edge 重发
                pass
        return self.acked_sequence

    def load_snapshot(self, snapshot: JsonObject) -> None:
        """从 snapshot 重建投影（初次接入/缺口恢复）."""
        snapshot = InventorySnapshotResponse.model_validate(snapshot).model_dump(
            mode="json"
        )
        self.state.clear()
        self.versions.clear()
        for lot in snapshot.get("lots", []):
            key = f"lot:{lot['lot_id']}"
            self.versions[key] = lot["version"]
            self.state[key] = {"event_type": "snapshot", "payload": dict(lot),
                               "version": lot["version"]}
        for inst in snapshot.get("instances", []):
            key = f"instance:{inst['edge_uuid']}"
            self.versions[key] = inst["version"]
            self.state[key] = {"event_type": "snapshot", "payload": dict(inst),
                               "version": inst["version"]}
        self.acked_sequence = snapshot.get("snapshot_sequence", 0)
