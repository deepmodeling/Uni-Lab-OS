"""Outbox 同步协议测试.

覆盖测试门槛：
- 重复 event 不重复应用（云端 (edge_id,event_id) 去重语义）
- 乱序 aggregate_version 不覆盖新状态
- 业务提交后、网络发送前 crash → 重启回放 outbox
- 离线变更恢复后顺序 ACK（指数退避期间 outbox 保留）
- snapshot 可重建云端 projection，ledger 可对账
"""

import json

import pytest

from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import (
    InvalidCursorAdvance,
    InventoryStore,
)
from unilabos.app.scheduler.inventory.sync import (
    CloudProjectionReference,
    OutboxWorker,
    build_snapshot,
)


def _req(lot="", qty=0.0):
    return MaterialRequirement(lot_id=lot, quantity=qty)


class _CollectingSender:
    """测试 sender：可注入失败次数，成功时全量 ACK."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0
        self.received = []

    def __call__(self, events):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("network down")
        self.received.extend(events)
        return max(e["sequence"] for e in events)


class TestOutboxWorker:
    def test_flush_and_cursor_advance(self):
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=10.0)]})

        sender = _CollectingSender()
        worker = OutboxWorker(svc.store, sender)
        n = worker.flush_all()
        assert n == svc.store.max_outbox_sequence()
        assert worker.backlog() == 0
        # envelope 完整性
        ev = sender.received[0]
        for key in ("event_id", "edge_id", "lab_id", "sequence", "aggregate_type",
                    "aggregate_id", "aggregate_version", "event_type", "occurred_at",
                    "causation_id", "payload"):
            assert key in ev

    def test_send_failure_keeps_outbox(self):
        """网络失败：事件保留，退避计数上升，恢复后继续发."""
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")

        sender = _CollectingSender(fail_times=2)
        worker = OutboxWorker(svc.store, sender)
        with pytest.raises(ConnectionError):
            worker.flush_once()
        with pytest.raises(ConnectionError):
            worker.flush_once()
        assert worker.backlog() > 0  # 未 ACK，事件还在
        worker.flush_all()
        assert worker.backlog() == 0

    def test_crash_before_send_replays_after_restart(self, tmp_path):
        """业务事务已提交、网络发送前 crash：重启后 outbox 完整回放."""
        db = str(tmp_path / "inv.db")
        svc = InventoryService(InventoryStore(db))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=10.0)]})
        expected = svc.store.max_outbox_sequence()
        svc.store.close()  # 模拟 crash：worker 从未跑过

        store2 = InventoryStore(db)
        sender = _CollectingSender()
        worker = OutboxWorker(store2, sender)
        n = worker.flush_all()
        assert n == expected
        assert [e["sequence"] for e in sender.received] == list(range(1, expected + 1))
        store2.close()

    def test_offline_changes_acked_in_order(self, tmp_path):
        """离线累积多批变更，恢复后按 sequence 顺序 ACK，cursor 只前进."""
        db = str(tmp_path / "inv.db")
        svc = InventoryService(InventoryStore(db))
        for i in range(5):
            svc.inbound_lot("tpl-w", 10.0, lot_id=f"lot-{i}")

        sender = _CollectingSender()
        worker = OutboxWorker(svc.store, sender, batch_size=2)  # 强制分批
        worker.flush_all()
        seqs = [e["sequence"] for e in sender.received]
        assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))
        assert svc.store.get_cursor() == len(seqs)
        svc.store.close()

    def test_partial_ack_resends_remainder(self):
        """云端只 ACK 一部分（缺口）：cursor 停在水位，剩余下一批重发."""
        svc = InventoryService(InventoryStore(":memory:"))
        for i in range(4):
            svc.inbound_lot("tpl-w", 10.0, lot_id=f"lot-{i}")

        acks = []

        def partial_sender(events):
            acks.append([e["sequence"] for e in events])
            return events[0]["sequence"]  # 每次只确认第一条

        worker = OutboxWorker(svc.store, partial_sender, batch_size=10)
        worker.flush_once()
        assert svc.store.get_cursor() == 1
        worker.flush_once()
        # 第二批从 seq=2 开始重发（seq=1 不再发）
        assert acks[1][0] == 2

    def test_cloud_event_has_payload_not_payload_json_and_optional_trace(self):
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-wire", 3.0, lot_id="lot-wire")
        traceparent = (
            "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
        )
        with svc.store.transaction() as conn:
            conn.execute(
                "UPDATE sync_outbox SET traceparent = ?, tracestate = ?, "
                "trace_id = ?, span_id = ? WHERE sequence = 1",
                (
                    traceparent,
                    "vendor=value",
                    "0123456789abcdef0123456789abcdef",
                    "0123456789abcdef",
                ),
            )

        sender = _CollectingSender()
        OutboxWorker(svc.store, sender).flush_once()
        event = sender.received[0]
        assert "payload" in event
        assert "payload_json" not in event
        assert event["payload"]["quantity"] == 3.0
        assert event["traceparent"] == traceparent
        assert event["tracestate"] == "vendor=value"

    def test_future_ack_is_rejected_without_losing_events_then_retries(self):
        svc = InventoryService(InventoryStore(":memory:"))
        for i in range(3):
            svc.inbound_lot("tpl-wire", 1.0, lot_id=f"lot-future-{i}")
        maximum = svc.store.max_outbox_sequence()

        def future_sender(events):
            return events[-1]["sequence"] + 1

        worker = OutboxWorker(svc.store, future_sender)
        with pytest.raises(InvalidCursorAdvance, match="exceeds sent"):
            worker.flush_once()
        assert svc.store.get_cursor() == 0
        assert worker.backlog() == maximum

        sender = _CollectingSender()
        worker.sender = sender
        assert worker.flush_all() == maximum
        assert svc.store.get_cursor() == maximum

    def test_regressing_ack_is_rejected_and_future_events_remain(self):
        svc = InventoryService(InventoryStore(":memory:"))
        for i in range(3):
            svc.inbound_lot("tpl-wire", 1.0, lot_id=f"lot-regress-{i}")

        worker = OutboxWorker(
            svc.store,
            lambda events: events[0]["sequence"],
            batch_size=3,
        )
        assert worker.flush_once() == 1
        assert svc.store.get_cursor() == 1

        worker.sender = lambda _events: 0
        with pytest.raises(InvalidCursorAdvance, match="regression"):
            worker.flush_once()
        assert svc.store.get_cursor() == 1
        assert [row["sequence"] for row in svc.store.pending_outbox(1)] == [2, 3]

        sender = _CollectingSender()
        worker.sender = sender
        assert worker.flush_all() == 2
        assert svc.store.get_cursor() == 3


class TestCloudProjectionContract:
    """云端 inbox 参考语义（Go 实现的契约）."""

    def _event(self, seq, agg="lot:lot-1", version=1, event_id=None, payload=None):
        atype, aid = agg.split(":")
        return {
            "event_id": event_id or f"ev-{seq}",
            "edge_id": "edge-t", "lab_id": "lab-t", "sequence": seq,
            "aggregate_type": atype, "aggregate_id": aid,
            "aggregate_version": version, "event_type": "lot.inbound",
            "occurred_at": 1000 + seq, "causation_id": "",
            "payload": payload or {"v": version},
        }

    def test_duplicate_event_not_applied_twice(self):
        proj = CloudProjectionReference()
        e = self._event(1, version=1, payload={"quantity_total": 10})
        proj.ingest([e])
        proj.ingest([e])  # Edge 重发同一 event
        assert proj.acked_sequence == 1
        assert proj.state["lot:lot-1"]["version"] == 1

    def test_out_of_order_version_does_not_overwrite(self):
        proj = CloudProjectionReference()
        newer = self._event(2, version=5, payload={"quantity_total": 50})
        older = self._event(3, version=3, payload={"quantity_total": 30},
                            event_id="ev-old")
        proj.ingest([self._event(1, version=1)])
        proj.ingest([newer])
        proj.ingest([older])  # 乱序旧版本
        assert proj.state["lot:lot-1"]["version"] == 5
        assert proj.state["lot:lot-1"]["payload"]["quantity_total"] == 50

    def test_gap_does_not_advance_ack(self):
        proj = CloudProjectionReference()
        proj.ingest([self._event(1)])
        proj.ingest([self._event(3)])  # seq=2 缺口
        assert proj.acked_sequence == 1  # 等 Edge 重发


class TestSnapshotReconciliation:
    def test_snapshot_rebuilds_projection(self):
        """snapshot 与 事件回放 两条路径重建的 projection 版本一致."""
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=30.0)]})
        svc.consume_reservation("wf1", "n1")
        svc.register_instance(edge_uuid="mi-1", barcode="BC-1")

        # 路径 1：事件流回放
        sender = _CollectingSender()
        OutboxWorker(svc.store, sender).flush_all()
        proj_events = CloudProjectionReference()
        proj_events.ingest(sender.received)

        # 路径 2：snapshot 全量重建
        proj_snap = CloudProjectionReference()
        proj_snap.load_snapshot(build_snapshot(svc.store))

        for key in ("lot:lot-1", "instance:mi-1"):
            assert proj_events.versions[key] == proj_snap.versions[key]

    def test_ledger_reconciles_quantities(self):
        """ledger 可对账：数量事件累计 == 当前库存行."""
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        svc.reserve_workflow("wf1", {"n1": [_req(lot="lot-1", qty=30.0)]})
        svc.consume_reservation("wf1", "n1")
        svc.reserve_workflow("wf2", {"n1": [_req(lot="lot-1", qty=10.0)]})
        svc.release_reservation("wf2", "n1")

        total = 0.0
        for row in svc.store.query_all(
            "SELECT op_type, delta_json FROM inventory_ledger WHERE aggregate_id = 'lot-1'"
        ):
            delta = json.loads(row["delta_json"])
            if row["op_type"] in ("lot.created", "lot.inbound"):
                total += delta["quantity"]
            elif row["op_type"] == "lot.consumed":
                total -= delta["quantity"]
        lot = svc.store.get_lot("lot-1")
        assert total == pytest.approx(lot["quantity_total"])
