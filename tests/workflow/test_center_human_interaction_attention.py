"""原生人工交互参与 Center 当前待办快照，并在状态变化后及时通知。"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.workflow.test_center_attention_snapshot import JOB_UUID, TASK_UUID, _bind_center_task, _seed
from unilabos.app.center_client.client import CenterClientError
from unilabos.app.center_client.websocket import validate_attention_snapshot
from unilabos.workflow.human_commands import PREFIX, HumanInteractionStore
from unilabos.workflow.store import WorkflowStore

CASES = [
    ("form", {"prompt": "判定", "fields": [{"name": "qualified", "label": "合格", "kind": "boolean"}]}, {"answers": {"qualified": False}}),
    ("choose", {"prompt": "继续吗？", "options": ["yes", "no"], "labels": ["继续", "停止"]}, {"choice": "yes"}),
    ("text", {"prompt": "填写备注", "default": ""}, {"text": "已核对样品"}),
    ("numbers", {"prompt": "输入偏移", "labels": ["X", "Y"], "defaults": [0.0, 0.0]}, {"values": [1.25, -2.5]}),
    (
        "sketch",
        {"prompt": "标注区域", "image": "image.png"},
        {"x": [0, 1, 1], "y": [0, 0, 1], "corner_x": [], "corner_y": []},
    ),
    (
        "review",
        {
            "prompt": "复核",
            "options": ["accept", "reject"],
            "labels": ["接收", "拒绝"],
            "forms": ["text", "none"],
            "image": "image.png",
            "text_default": "",
            "number_labels": [],
            "number_defaults": [],
        },
        {"choice": "accept", "text": "已核对", "values": [], "x": [], "y": [], "corner_x": [], "corner_y": []},
    ),
]


def seed_human(store, kind, arguments, *, center_created=True):
    _seed(store, center_created=center_created)
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_manual_confirmation SET status='approved'")
        conn.execute("UPDATE workflow_node_job SET executor_kind='workflow_command', status='waiting'")
    interactions = HumanInteractionStore(store)
    args = {"image": "", "timeout_seconds": 60, **arguments}
    event = {"job_id": JOB_UUID, "command_name": PREFIX + kind, "arguments": args}
    return interactions, event, interactions.evaluate(event, TASK_UUID)


@pytest.mark.parametrize("kind,arguments,result", CASES)
def test_pending_native_human_interactions_are_counted_unchanged_and_reply_removes_them(
    tmp_path, kind, arguments, result
):
    store = WorkflowStore(tmp_path / "human-attention.db")
    try:
        interactions, event, request = seed_human(store, kind, arguments)
        snapshot = store.center_attention_snapshot()
        assert snapshot["manual_confirmation_count"] == 1
        assert snapshot["items"]["manual_confirmations"] == []
        assert snapshot["items"]["human_interactions"] == [request]
        assert validate_attention_snapshot(snapshot) == snapshot
        interactions.reply(request["uuid"], result)
        snapshot = store.center_attention_snapshot()
        assert snapshot["manual_confirmation_count"] == 0
        assert not snapshot["items"].get("human_interactions")
    finally:
        store.close()


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled", "canceling", "timeout", "execution_unknown"])
def test_terminal_or_canceling_task_does_not_leave_a_human_attention(tmp_path, status):
    store = WorkflowStore(tmp_path / "closed.db")
    try:
        seed_human(store, *CASES[0][:2])
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_task SET status=? WHERE uuid=?", (status, TASK_UUID))
        assert store.center_attention_snapshot()["manual_confirmation_count"] == 0
        assert not store.center_attention_snapshot()["items"].get("human_interactions")
    finally:
        store.close()


def test_timed_out_human_interaction_does_not_remain_in_snapshot(tmp_path):
    store = WorkflowStore(tmp_path / "timeout.db")
    try:
        interactions, event, request = seed_human(store, *CASES[0][:2])
        expired = interactions.evaluate(
            event, TASK_UUID, now=datetime.fromisoformat(request["deadline_at"]) + timedelta(seconds=1)
        )
        assert expired["status"] == "timed_out"
        assert store.center_attention_snapshot()["manual_confirmation_count"] == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.update(manual_confirmation_count=0),
        lambda s: s["items"]["human_interactions"][0].update(kind="unknown"),
        lambda s: s["items"]["human_interactions"][0].update(status="answered"),
        lambda s: s["items"]["human_interactions"][0].update(arguments=[]),
    ],
)
def test_human_snapshot_validation_rejects_mismatched_count_or_invalid_native_shape(tmp_path, change):
    store = WorkflowStore(tmp_path / "invalid.db")
    try:
        _, _, request = seed_human(store, *CASES[0][:2])
        payload = {
            "manual_confirmation_count": 1,
            "exception_count": 0,
            "items": {"manual_confirmations": [], "exceptions": [], "human_interactions": [request]},
        }
        change(payload)
        with pytest.raises(CenterClientError):
            validate_attention_snapshot(payload)
    finally:
        store.close()


@pytest.mark.parametrize("notifier_raises", [False, True])
def test_runtime_creation_reply_and_cancellation_notify_committed_human_snapshot(tmp_path, notifier_raises):
    from uuid import uuid4

    from tests.workflow.test_authoring_engine import WORKFLOW_UUID, _compile
    from tests.workflow.test_human_commands import SOURCE
    from tests.workflow.test_inventory_native_command import engine
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder
    from unilabos.workflow.service import WorkflowService
    from unilabos.workflow.task_input import prepare_task_input
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    compiled = _compile(engine(), SOURCE)
    assert compiled.valid, compiled.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(compiled.graph, run_mode="normal", target_node_uuid=None)
    db = tmp_path / "notifications.db"
    store = WorkflowStore(db)
    observer = WorkflowStore(db)
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID, name="Human notification", tags=[], description=None, meta_data={}
    )
    identity = str(uuid4())
    store.create_task_with_jobs(
        workflow_uuid=WORKFLOW_UUID,
        task_uuid=identity,
        run_mode="normal",
        target_node_uuid=None,
        description=None,
        meta_data={},
        plan_builder=lambda _: prepare_task_input(graph=compiled.graph, execution_plan=plan, jobs=jobs, raw_input={}),
    )
    _bind_center_task(store, identity)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    observed = []

    def notify():
        observed.append(observer.center_attention_snapshot())
        if notifier_raises:
            raise RuntimeError("Notification transport failure")

    service.set_attention_notifier(notify)
    try:
        bridge.submit(store.get_task(identity))
        assert observed, "Opening a human interaction must notify Center"
        first = bridge.human_interactions.list(identity)[0]
        assert any(first["uuid"] in [i["uuid"] for i in s["items"].get("human_interactions", [])] for s in observed)
        observed.clear()
        reply = service.reply_human_interaction(first["uuid"], {"choice": "yes"})
        assert reply["status"] == "answered"
        assert observed, "Committed reply must notify Center even if the task continues"
        assert any(first["uuid"] not in [i["uuid"] for i in s["items"].get("human_interactions", [])] for s in observed)
        bridge.cancel(identity, command_uuid=str(uuid4()))
        assert observer.center_attention_snapshot()["manual_confirmation_count"] == 0
        assert observed[-1]["manual_confirmation_count"] == 0
    finally:
        bridge.close()
        observer.close()
        store.close()


@pytest.mark.parametrize("recovery", [False, "before_deadline", "past_deadline"])
def test_paused_human_interaction_expires_and_notifies_without_dispatch(tmp_path, recovery):
    from uuid import uuid4

    from tests.workflow.test_authoring_engine import WORKFLOW_UUID, _compile
    from tests.workflow.test_human_commands import SOURCE
    from tests.workflow.test_inventory_native_command import engine
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder
    from unilabos.workflow.task_input import prepare_task_input
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    class DeferredTimer:
        def __init__(self, delay, callback):
            self.callback = callback

        def start(self):
            pass

        def cancel(self):
            pass

    compiled = _compile(engine(), SOURCE)
    assert compiled.valid, compiled.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(compiled.graph, run_mode="normal", target_node_uuid=None)
    store = WorkflowStore(tmp_path / "paused.db")
    store.create_workflow(workflow_uuid=WORKFLOW_UUID, name="Paused human", tags=[], description=None, meta_data={})
    task_uuid = str(uuid4())
    store.create_task_with_jobs(
        workflow_uuid=WORKFLOW_UUID,
        task_uuid=task_uuid,
        run_mode="normal",
        target_node_uuid=None,
        description=None,
        meta_data={},
        plan_builder=lambda _: prepare_task_input(graph=compiled.graph, execution_plan=plan, jobs=jobs, raw_input={}),
    )
    _bind_center_task(store, task_uuid)
    now = [datetime.now(timezone.utc)]
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler, clock=lambda: now[0], timer_factory=DeferredTimer)
    snapshots = []
    bridge.set_attention_notifier(lambda: snapshots.append(store.center_attention_snapshot()))
    try:
        bridge.submit(store.get_task(task_uuid))
        request = bridge.human_interactions.list(task_uuid)[0]
        timer = bridge._human_timers[request["job_uuid"]]
        bridge.pause(task_uuid)
        snapshots.clear()
        if recovery:
            bridge.close()
            if recovery == "past_deadline":
                now[0] = datetime.fromisoformat(request["deadline_at"]) + timedelta(seconds=1)
            scheduler = EdgeScheduler(dispatcher=dispatcher)
            bridge = TaskSchedulerBridge(store, scheduler=scheduler, clock=lambda: now[0], timer_factory=DeferredTimer)
            bridge.set_attention_notifier(lambda: snapshots.append(store.center_attention_snapshot()))
            bridge.recover_active_tasks(execution_process_restarted=False)
            if recovery == "before_deadline":
                timer = bridge._human_timers[request["job_uuid"]]
        now[0] = datetime.fromisoformat(request["deadline_at"]) + timedelta(seconds=1)
        if recovery != "past_deadline":
            timer.callback()
        scheduler._wake_reconcile()
        assert snapshots and snapshots[-1]["manual_confirmation_count"] == 0
        assert bridge.human_interactions.list(task_uuid)[0]["status"] == "timed_out"
        assert dispatcher.dispatched == []
        with pytest.raises(RuntimeError):
            bridge.reply_human_interaction(request["uuid"], {"choice": "yes"})
    finally:
        bridge.close()
        store.close()


def test_expiry_never_turns_a_canceled_request_into_timeout(tmp_path):
    store = WorkflowStore(tmp_path / "cancel-before-expire.db")
    try:
        interactions, _, request = seed_human(store, *CASES[0][:2])
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_task SET status='canceled' WHERE uuid=?", (TASK_UUID,))
        interactions.expire(request["uuid"], now=datetime.fromisoformat(request["deadline_at"]) + timedelta(seconds=1))
        with store.read() as conn:
            assert (
                conn.execute(
                    "SELECT status FROM workflow_human_interaction WHERE uuid=?", (request["uuid"],)
                ).fetchone()[0]
                == "canceled"
            )
    finally:
        store.close()


def test_local_native_human_interaction_stays_on_station_with_center_connected(tmp_path):
    from unilabos.workflow.human_commands import pending_human_interactions
    store = WorkflowStore(tmp_path / "local-human.db")
    try:
        interactions, _event, request = seed_human(store, *CASES[0][:2], center_created=False)
        from tests.app.center_client.test_center_client import INSTANCE, STATION
        from unilabos.persistence.center_schema import ensure_center_schema
        with store.transaction() as conn:
            ensure_center_schema(conn, station_uuid=STATION, instance_uuid=INSTANCE)
            assert pending_human_interactions(conn) == [request]
        snapshot = store.center_attention_snapshot()
        assert snapshot["manual_confirmation_count"] == 0
        assert not snapshot["items"].get("human_interactions")
        assert interactions.list(TASK_UUID)[0]["status"] == "pending"
    finally:
        store.close()
