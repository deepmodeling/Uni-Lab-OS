"""历史终态通过可信中心包补发应用证明，不伪造收货或清除墓碑保护。"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.test_handoff_attention_tombstones import assert_dispatch_blocked, canceled_snapshot
from tests.app.test_station_handoff import package, setup, uid  # noqa: F401
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.handoff import HandoffBridge
from unilabos.app.center_client.inventory_api import create_router
from unilabos.app.scheduler.inventory.handoff_attention import attention_handoffs
from unilabos.persistence.recovery_mode import enter
from unilabos.workflow.store import StoreConflict


@pytest.fixture
def legacy(setup, monkeypatch, request):
    s = setup
    authority = s["auth"]["target"]
    durable = DurableCenterStore(s["path"] / "refresh-center.db", station_uuid=s["ids"]["target"],
                                 instance_uuid=s["ids"]["target_instance"])
    with durable._db.transaction():
        durable._db.execute("UPDATE center_runtime_state SET event_epoch_uuid=?", (s["ids"]["target_epoch"],))
    durable.event_epoch_uuid = s["ids"]["target_epoch"]
    reads, reports = [], []

    class Center:
        def handoff_package(self, identity, revision, epoch):
            assert identity == s["ids"]["handoff"]
            assert revision is None
            assert epoch == s["ids"]["target_epoch"]
            reads.append(identity)
            return package(final)

        def report_handoff(self, event):
            reports.append(copy.deepcopy(event))
            return {"handoff": final, "ack": {
                **{key: event[key] for key in ("event_uuid", "event_epoch_uuid", "event_sequence", "event_digest")},
                "application": "applied", "last_contiguous_event_sequence": event["event_sequence"],
            }}

    bridge = HandoffBridge(Center(), durable, authority)
    try:
        # 模拟旧版本已经处理并确认取消，却尚不产生 snapshot_applied 的完整历史链路。
        with monkeypatch.context() as old_version:
            old_version.setattr(authority, "_record_snapshot_application", lambda *args, **kwargs: None)
            final = canceled_snapshot(s, reconciled=getattr(request, "param", False))
            bridge.pump()
        assert authority.pending_intents() == []
        assert len(attention_handoffs(authority)) == 1
        runtime = SimpleNamespace(store=durable, handoffs=bridge)
        app = FastAPI()
        app.include_router(create_router(lambda: runtime))
        yield SimpleNamespace(s=s, authority=authority, durable=durable, bridge=bridge,
                              client=TestClient(app), reads=reads, reports=reports,
                              path=f"/api/v1/center-handoffs/{s['ids']['handoff']}/refresh", final=final)
    finally:
        durable.close()


def inventory_state(s: dict) -> dict:
    store = s["stores"]["target"]
    return {table: store.query_all(f"SELECT * FROM {table} ORDER BY 1") for table in (
        "material", "site", "station_ingress_reservation", "station_handoff_item", "station_handoff_resource",
    )}


@pytest.mark.parametrize("legacy", [False, True], indirect=True, ids=["old-cancel", "old-reconciliation"])
def test_refresh_replays_trusted_snapshot_and_pumps_exact_missing_proof_idempotently(legacy, monkeypatch):
    case = legacy
    before = inventory_state(case.s)
    old_proofs = case.s["stores"]["target"].query_all("SELECT * FROM station_handoff_outbox ORDER BY event_uuid")
    report_count = len(case.reports)
    case.bridge.pump()
    assert len(case.reports) == report_count
    assert case.reads == []  # 已确认的历史命令不会被普通 pump 自动重取。

    def no_receiving(*args, **kwargs):
        pytest.fail("刷新快照不得触发收货")

    monkeypatch.setattr(case.authority, "receive", no_receiving)
    first = case.client.post(case.path)
    assert first.status_code == 200, first.text
    assert case.client.post(case.path, json={}).json() == first.json()
    assert len(case.reads) == 2
    events = case.authority.pending_intents()
    assert len(events) == 1
    assert events[0]["payload"]["kind"] == "snapshot_applied"
    assert events[0]["payload"]["applied"] is True
    assert events[0]["payload"]["package_digest"] == package(case.final)["payload_digest"]
    assert len(attention_handoffs(case.authority)) == 1  # 等待真实中心 ACK，不能提前消除异常。
    assert inventory_state(case.s) == before
    case.bridge.pump()
    assert case.authority.pending_intents() == []
    assert len(case.reports) == report_count + 1
    assert case.reports[-1]["event_uuid"] == events[0]["event_uuid"]
    assert attention_handoffs(case.authority) == []
    assert inventory_state(case.s) == before
    current = case.s["stores"]["target"].query_all("SELECT * FROM station_handoff_outbox ORDER BY event_uuid")
    assert all(proof in current for proof in old_proofs)
    assert len(current) == len(old_proofs) + 1
    item = before["station_handoff_item"][0]
    assert_dispatch_blocked(case.s, "target", item["carrier_material_uuid"])


@pytest.mark.parametrize("guard", ["evidence-only", "store-conflict"])
def test_refresh_obeys_execution_recovery_guard_without_reading_or_mutating(legacy, monkeypatch, guard):
    case = legacy
    before = inventory_state(case.s)
    if guard == "evidence-only":
        with case.durable._db.transaction():
            enter(case.durable._db, "历史恢复责任未核实")
    else:
        def held():
            raise StoreConflict("当前执行责任待核实")
        monkeypatch.setattr(case.durable, "require_execution", held)
    response = case.client.post(case.path)
    assert response.status_code == 409
    assert case.reads == []
    assert case.authority.pending_intents() == []
    assert inventory_state(case.s) == before
    assert len(attention_handoffs(case.authority)) == 1


def test_refresh_rejects_user_snapshot_and_unknown_local_handoff(legacy):
    case = legacy
    assert case.client.post(case.path, json=package(case.final)).status_code == 422
    assert case.client.post(f"/api/v1/center-handoffs/{uid()}/refresh").status_code == 409
    assert case.reads == []
    assert case.authority.pending_intents() == []


def test_refresh_rejects_invalid_center_package_without_inventing_success(legacy, monkeypatch):
    case = legacy
    before = inventory_state(case.s)
    invalid = package(case.final)
    invalid["payload"]["revision"] += 1  # 摘要不再匹配，不能产生本站成功应用证明。
    monkeypatch.setattr(case.bridge.client, "handoff_package", lambda *args: invalid)
    response = case.client.post(case.path)
    assert response.status_code == 409
    assert case.authority.pending_intents() == []
    assert inventory_state(case.s) == before
    assert len(attention_handoffs(case.authority)) == 1
