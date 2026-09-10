"""实时监控总线（四通道）与监控 API 测试。

- MonitorBus：emit / subscribe / backlog 回放 / 通道过滤 / 慢消费者不阻塞
- EdgeScheduler 事件：submit → scheduler+action+device；finish → action+device+终态
- InventoryService 事件：事务提交后才发 material 事件，回滚不发
- device_status()：busy 来自 inflight、idle 来自时间线痕迹
- GET /api/v1/monitor/snapshot：面板初始填充结构
"""

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.monitor import MonitorBus
from unilabos.app.scheduler.service import EdgeScheduler


def _spec(workflow_id="wf-m", device="dev1"):
    return WorkflowSpec(
        workflow_id=workflow_id,
        nodes=[
            WorkflowNode(id="A", device_id=device, action_name="run", action_type="goal"),
            WorkflowNode(id="B", device_id=device, action_name="run", action_type="goal"),
        ],
        edges=[WorkflowEdge(uuid="e", source_node_id="A", target_node_id="B")],
    )


class TestMonitorBus:
    def test_emit_and_subscribe(self):
        bus = MonitorBus()
        sub_id, q, replay = bus.subscribe()
        assert replay == []
        bus.emit("action", "job_dispatched", {"job_id": "j1"})
        event = q.get_nowait()
        assert event["channel"] == "action"
        assert event["type"] == "job_dispatched"
        assert event["data"]["job_id"] == "j1"
        assert event["seq"] == 1
        bus.unsubscribe(sub_id)
        bus.emit("action", "x", {})
        assert bus.subscriber_count == 0

    def test_backlog_replay_with_channel_filter(self):
        bus = MonitorBus()
        bus.emit("material", "lot.inbound", {"lot_id": "l1"})
        bus.emit("device", "device_busy", {"device_id": "d1"})
        bus.emit("material", "lot.adjusted", {"lot_id": "l1"})
        _, q, replay = bus.subscribe(channels={"material"}, backlog=10)
        assert [e["type"] for e in replay] == ["lot.inbound", "lot.adjusted"]
        # 订阅后按通道过滤推送
        bus.emit("device", "device_idle", {})
        bus.emit("material", "lot.consumed", {})
        assert q.get_nowait()["type"] == "lot.consumed"

    def test_slow_subscriber_drops_but_never_blocks(self):
        bus = MonitorBus(subscriber_buffer=2)
        _, q, _ = bus.subscribe()
        for i in range(5):
            bus.emit("action", "e", {"i": i})
        assert q.qsize() == 2  # 超出缓冲丢弃
        # history 仍完整
        assert len(bus.recent("action", limit=10)) == 5

    def test_recent_per_channel(self):
        bus = MonitorBus()
        bus.emit("scheduler", "reschedule", {})
        bus.emit("action", "job_dispatched", {})
        assert [e["channel"] for e in bus.recent("scheduler")] == ["scheduler"]


class TestSchedulerEmissions:
    def _make(self):
        bus = MonitorBus()
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher(), monitor=bus)
        return scheduler, bus

    def test_submit_emits_scheduler_action_device(self):
        scheduler, bus = self._make()
        result = scheduler.submit_workflow(_spec())
        types = [(e["channel"], e["type"]) for e in bus.recent("scheduler", 10)]
        assert ("scheduler", "workflow_submitted") in types
        assert ("scheduler", "reschedule") in types
        dispatched = [e for e in bus.recent("action", 10) if e["type"] == "job_dispatched"]
        assert len(dispatched) == 1
        assert dispatched[0]["data"]["job_id"] == result["dispatched"][0]["job_id"]
        assert dispatched[0]["data"]["estimated_s"] > 0
        busy = bus.recent("device", 10)
        assert busy[-1]["type"] == "device_busy"
        assert busy[-1]["data"]["device_id"] == "dev1"

    def test_finish_emits_action_device_and_terminal_state(self):
        scheduler, bus = self._make()
        r = scheduler.submit_workflow(_spec("wf-fin"))
        scheduler.on_job_finished(r["dispatched"][0]["job_id"], True)
        # B 下发后完成 → 工作流终态
        second = [e for e in bus.recent("action", 20) if e["type"] == "job_dispatched"][-1]
        scheduler.on_job_finished(second["data"]["job_id"], True)

        finished = [e for e in bus.recent("action", 20) if e["type"] == "job_finished"]
        assert len(finished) == 2
        assert finished[0]["data"]["state"] == "success"
        assert finished[0]["data"]["actual_s"] >= 0
        idle = [e for e in bus.recent("device", 20) if e["type"] == "device_idle"]
        assert len(idle) == 2
        states = [e for e in bus.recent("scheduler", 20) if e["type"] == "workflow_state"]
        assert states[-1]["data"] == {"workflow_id": "wf-fin", "state": "success"}

    def test_failure_and_cancel_states(self):
        scheduler, bus = self._make()
        r = scheduler.submit_workflow(_spec("wf-f"))
        scheduler.on_job_finished(r["dispatched"][0]["job_id"], False)
        finished = [e for e in bus.recent("action", 20) if e["type"] == "job_finished"]
        assert finished[-1]["data"]["state"] == "failed"
        states = [e for e in bus.recent("scheduler", 20) if e["type"] == "workflow_state"]
        assert states[-1]["data"]["state"] == "failed"

        scheduler.submit_workflow(_spec("wf-c"))
        scheduler.cancel_workflow("wf-c")
        finished = [e for e in bus.recent("action", 20) if e["type"] == "job_finished"]
        assert finished[-1]["data"]["state"] == "canceled"

    def test_monitor_disabled_is_noop(self):
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())  # monitor=None
        r = scheduler.submit_workflow(_spec("wf-none"))
        scheduler.on_job_finished(r["dispatched"][0]["job_id"], True)  # 不应抛错


class TestDeviceStatus:
    def test_busy_then_idle(self):
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
        r = scheduler.submit_workflow(_spec("wf-d"))
        devices = scheduler.device_status()
        assert len(devices) == 1
        d = devices[0]
        assert d["device_id"] == "dev1"
        assert d["status"] == "busy"
        assert d["action_name"] == "run"
        assert d["elapsed_s"] >= 0

        scheduler.on_job_finished(r["dispatched"][0]["job_id"], True)
        # B 又被下发 → 仍 busy；完成后 idle 且带最近动作痕迹
        second = scheduler.snapshot()["inflight_jobs"]
        scheduler.on_job_finished(next(iter(second)), True)
        d = scheduler.device_status()[0]
        assert d["status"] == "idle"
        assert d["last_action"] == "run"
        assert d["last_state"] == "success"


class TestInventoryEmissions:
    def _svc(self):
        pytest.importorskip("sqlite3")
        from unilabos.app.scheduler.inventory.service import InventoryService
        from unilabos.app.scheduler.inventory.store import InventoryStore

        bus = MonitorBus()
        svc = InventoryService(
            InventoryStore(":memory:"), edge_id="edge-t", lab_id="lab-t", monitor=bus
        )
        return svc, bus

    def test_committed_tx_emits_material_events(self):
        svc, bus = self._svc()
        svc.inbound_lot("tpl-water", 100.0, unit="mL", lot_id="lot-1")
        events = bus.recent("material", 10)
        assert [e["type"] for e in events] == ["lot.created"]
        assert events[0]["data"]["aggregate_id"] == "lot-1"
        assert events[0]["data"]["payload"]["quantity_total"] == 100.0

        svc.adjust_lot("lot-1", 90.0, reason="盘亏", actor="tester")
        assert bus.recent("material", 10)[-1]["type"] == "lot.adjusted"

    def test_rolled_back_tx_emits_nothing(self):
        svc, bus = self._svc()
        from unilabos.app.scheduler.inventory.domain import InvariantViolation

        with pytest.raises(InvariantViolation):
            svc.inbound_lot("tpl-water", -5.0)
        assert bus.recent("material", 10) == []

    def test_monitorless_service_still_works(self):
        from unilabos.app.scheduler.inventory.service import InventoryService
        from unilabos.app.scheduler.inventory.store import InventoryStore

        svc = InventoryService(InventoryStore(":memory:"))
        lot = svc.inbound_lot("tpl-x", 1.0)
        assert lot["quantity_total"] == 1.0


class TestMonitorApi:
    def test_snapshot_endpoint(self):
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        from fastapi.testclient import TestClient

        from unilabos.app.scheduler.api import create_app

        client = TestClient(create_app())
        body = {
            "workflow_id": "wf-snap",
            "nodes": [{"id": "A", "device_id": "d1", "action_name": "run", "action_type": "goal"}],
        }
        assert client.post("/api/v1/workflows", json=body).status_code == 200

        r = client.get("/api/v1/monitor/snapshot")
        assert r.status_code == 200
        snap = r.json()
        assert snap["scheduler"]["inflight"] == 1
        assert snap["scheduler"]["workflow_states"].get("running") == 1
        assert any(d["status"] == "busy" for d in snap["devices"])
        assert set(snap["recent"]) == {"material", "device", "action", "scheduler"}
        # create_app 默认接全局 monitor_bus → scheduler 通道应有事件
        types = [e["type"] for e in snap["recent"]["scheduler"]]
        assert "workflow_submitted" in types
