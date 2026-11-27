"""中心取消复用现存任务和本地控制命令；丢确认、重启与迟到终态不造新执行。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.center_client.test_atomic_workflow_events import _events, _start, _submit, _workflow
from tests.app.center_client.test_runtime import Center, runtime
from tests.app.center_client.test_websocket import Connection, hello, setup_receiver
from unilabos.app.center_client.client import SessionFencedError
from unilabos.app.center_client.digest import digest
from unilabos.app.center_client.runtime import submission
from unilabos.app.center_client.websocket import envelope
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


def cancellation(server, package, **changes):
    command = {
        "protocol_version": "1.0", "type": "task.cancel", "command_uuid": str(uuid4()),
        "command_sequence": len(server.commands) + 1, "station_uuid": package.raw["station_uuid"],
        "payload": {"station_invocation_uuid": package.station_invocation_uuid,
                    "global_task_uuid": package.global_task_uuid, "reason": "用户取消"},
    }
    command["payload"].update(changes)
    notification = {"command": command, "command_digest": digest(command),
                    "disposition": "active", "disposition_revision": 0}
    server.commands.append(notification)
    return notification


def install_cancel(monkeypatch, scheduler, store):
    calls = []

    def cancel(task_uuid, *, command_uuid):
        calls.append(command_uuid)
        return TaskRuntimeProjection(store).project_cancel_requested(
            task_uuid, command_uuid=command_uuid, ack_deadline_at="2099-01-01T00:00:00Z",
            complete_deadline_at="2099-01-01T00:01:00Z",
        )

    monkeypatch.setattr(scheduler, "cancel", cancel, raising=False)
    return calls


@pytest.mark.parametrize("in_flight", [False, True])
def test_http_cancel_controls_existing_task_and_reports_only_real_terminal(tmp_path, monkeypatch, in_flight):
    with _workflow(tmp_path, schedule=True) as (store, service, _, package, scheduler):
        task = _submit(service, package)
        calls = install_cancel(monkeypatch, scheduler, store)
        if in_flight:
            projection, job = _start(store, task)
        server = Center()
        notice = cancellation(server, package)
        rt = runtime(tmp_path, server, service)
        try:
            rt.poll_once()
            assert len(calls) == 1
            assert store.get_task(task["uuid"])["status"] == ("canceling" if in_flight else "canceled")
            assert len(_events(store)) == (1 if in_flight else 2)
            assert not rt.store.commands() and rt.store.cursor() == 1
            if in_flight:
                projection.project_job_finished(job_uuid=job, scheduler_state="canceled", return_info={})
                rt.poll_once()
                assert _events(store)[-1]["payload"]["status"] == "canceled"
            assert server.reports[-1]["payload"]["status"] == "canceled"
            rt.store.receive(deepcopy(notice))
            rt.poll_once()
            assert len(calls) == 1
            assert len(scheduler.submissions) == 1
        finally:
            rt.close()


def test_cancel_crash_before_local_action_resumes_same_pending_command(tmp_path, monkeypatch):
    with _workflow(tmp_path, schedule=True) as (store, service, _, package, scheduler):
        task = _submit(service, package)
        calls = install_cancel(monkeypatch, scheduler, store)
        actual_cancel = scheduler.cancel

        def crash(*args, **kwargs):
            raise RuntimeError("模拟控制命令落盘后、调度器受理前退出")

        monkeypatch.setattr(scheduler, "cancel", crash)
        server = Center()
        cancellation(server, package)
        first = runtime(tmp_path, server, service)
        try:
            with pytest.raises(RuntimeError):
                first.poll_once()
            assert len(first.store.commands()) == 1
            assert store.get_task(task["uuid"])["status"] != "canceled"
        finally:
            first.close()
        monkeypatch.setattr(scheduler, "cancel", actual_cancel)
        second = runtime(tmp_path, server, service)
        try:
            second.poll_once()
            assert len(calls) == 1 and not second.store.commands()
            assert store.get_task(task["uuid"])["status"] == "canceled"
            assert len(scheduler.submissions) == 1
            assert store.db.execute("SELECT COUNT(*) FROM workflow_task_command").fetchone()[0] == 1
        finally:
            second.close()


def test_cancel_after_local_commit_before_inbox_finish_replays_without_second_action(tmp_path, monkeypatch):
    with _workflow(tmp_path, schedule=True) as (store, service, _, package, scheduler):
        _submit(service, package)
        calls = install_cancel(monkeypatch, scheduler, store)
        server = Center()
        cancellation(server, package)
        rt = runtime(tmp_path, server, service)
        original = rt.store.finish_command
        monkeypatch.setattr(rt.store, "finish_command", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模拟落盘后退出")))
        try:
            with pytest.raises(RuntimeError):
                rt.poll_once()
            assert len(calls) == 1
            monkeypatch.setattr(rt.store, "finish_command", original)
            rt.poll_once()
            assert len(calls) == 1 and not rt.store.commands()
            assert len(_events(store)) == 2
        finally:
            rt.close()


def test_cancel_before_task_creation_records_terminal_and_blocks_late_submission(tmp_path, monkeypatch):
    """已领取但未建 Task 时直接取消收尾，重投和迟到通知不能恢复执行。"""
    with _workflow(tmp_path, schedule=True) as (store, service, _, package, scheduler):
        calls = install_cancel(monkeypatch, scheduler, store)
        server = Center()
        notice = cancellation(server, package)
        rt = runtime(tmp_path, server, service)
        try:
            rt.poll_once()
            assert not rt.store.commands() and not calls and not scheduler.submissions
            assert store.count_rows("workflow_task") == 0
            assert store.count_rows("workflow_node_job") == 0
            events = _events(store)
            assert len(events) == 1
            event = events[0]
            assert event["event_type"] == "admission_failed" and event["local_task_uuid"] is None
            assert event["payload"]["output"]["error_info"][0]["code"] == "canceled_before_creation"
            command = store.db.execute(
                "SELECT acknowledged,handled,error FROM center_command_inbox WHERE command_uuid=?",
                (notice["command"]["command_uuid"],),
            ).fetchone()
            assert tuple(command) == (1, 1, "canceled_before_creation")

            rt.store.receive(deepcopy(notice))
            server.package = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
            server.add()
            rt.poll_once()
            rt.poll_once()
            assert not rt.store.commands() and not calls and not scheduler.submissions
            assert store.count_rows("workflow_task") == 0
            assert store.count_rows("workflow_node_job") == 0
            assert _events(store) == events
            assert len(server.reports) == 1 and server.reports[0]["event_uuid"] == event["event_uuid"]
            assert server.claims == 0
        finally:
            rt.close()


def test_claim_recovery_with_known_cancel_never_submits_to_scheduler(tmp_path, monkeypatch):
    with _workflow(tmp_path, schedule=True) as (store, service, _, package, scheduler):
        # 原 claim 已确认，但在创建 Task 前退出；恢复页同时包含可领取及取消通知。
        server = Center()
        server.package = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
        server.add()
        cancellation(server, package)
        calls = install_cancel(monkeypatch, scheduler, store)
        rt = runtime(tmp_path, server, service)
        try:
            rt.poll_once()
            task = service.find_station_workflow_submission(**submission(package))
            assert task["status"] == "canceled" and not scheduler.submissions
            assert len(calls) == 1 and len(_events(store)) == 2
            assert _events(store)[-1]["payload"]["status"] == "canceled"
            assert not rt.store.commands() and server.claims == 1
            rt.poll_once()
            assert len(calls) == 1 and not scheduler.submissions
        finally:
            rt.close()


def test_scheduler_reentry_respects_durable_cancel_before_any_dispatch(tmp_path):
    from tests.workflow.test_f05_task_scheduler_bridge import _bridge
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.service import EdgeScheduler

    with _workflow(tmp_path, schedule=True) as (store, service, durable, package, scheduler):
        durable.receive(cancellation(Center(), package))
        task = _submit(service, package)
        dispatcher = RecordingDispatcher()
        bridge = _bridge(store, EdgeScheduler(dispatcher=dispatcher))
        try:
            assert not scheduler.submissions
            assert bridge.submit(task)["task"]["status"] == "pending"
            assert bridge.cancel(task["uuid"])["task"]["status"] == "canceled"
            assert dispatcher.dispatched == []
            assert len(_events(store)) == 2
        finally:
            bridge.close()


@pytest.mark.parametrize("authority", ["current", "fenced", "stale"])
def test_acknowledged_cancel_during_capacity_pause_requires_current_session(tmp_path, monkeypatch, authority):
    with _workflow(tmp_path, schedule=True) as (store, service, durable, package, scheduler):
        task = _submit(service, package)
        calls = install_cancel(monkeypatch, scheduler, store)
        server = Center()
        notice = cancellation(server, package)
        durable.receive(notice)
        durable.acknowledge_command(notice["command"]["command_uuid"])
        rt = runtime(tmp_path, server, service)
        try:
            session = rt.client.open_session(durable.event_epoch_uuid)
            if authority == "fenced":
                assert rt.client.fence_session(session)
            elif authority == "stale":
                server.reject_control = True
            monkeypatch.setattr(rt, "capacity_status", lambda: {"new_claim_paused": True})
            if authority == "current":
                rt.poll_once()
                assert len(calls) == 1 and store.get_task(task["uuid"])["status"] == "canceled"
            else:
                with pytest.raises(SessionFencedError):
                    rt.poll_once()
                assert not calls and store.get_task(task["uuid"])["status"] == "pending"
                assert len(rt.store.commands()) == 1
        finally:
            rt.close()


@pytest.mark.parametrize("change", [{"global_task_uuid": str(uuid4())}, {"reason": " "}, {"reason": "长" * 334}, {"extra": 1}])
def test_invalid_cancel_does_not_control_a_task(tmp_path, monkeypatch, change):
    with _workflow(tmp_path, schedule=True) as (store, service, _, package, scheduler):
        task = _submit(service, package)
        calls = install_cancel(monkeypatch, scheduler, store)
        server = Center()
        cancellation(server, package, **change)
        rt = runtime(tmp_path, server, service)
        try:
            rt.poll_once()
            assert not calls and store.get_task(task["uuid"])["status"] != "canceled"
        finally:
            rt.close()


def test_rejected_local_cancel_keeps_notification_for_review(tmp_path):
    with _workflow(tmp_path) as (store, service, _, package, _):
        task = _submit(service, package)
        server = Center()
        cancellation(server, package)
        rt = runtime(tmp_path, server, service)
        try:
            rt.poll_once()
            assert rt.store.cursor() == 1 and len(rt.store.commands()) == 1
            assert store.get_task(task["uuid"])["status"] != "canceled"
        finally:
            rt.close()


def test_upgrade_reopens_only_legacy_unsupported_cancel(tmp_path, monkeypatch):
    with _workflow(tmp_path, schedule=True) as (store, service, durable, package, scheduler):
        task = _submit(service, package)
        calls = install_cancel(monkeypatch, scheduler, store)
        server = Center()
        notice = cancellation(server, package)
        durable.receive(notice)
        durable.acknowledge_command(notice["command"]["command_uuid"])
        durable.finish_command(notice["command"]["command_uuid"], error="当前运行时只支持 task.available；需要人工处理")
        assert not durable.commands()
        rt = runtime(tmp_path, server, service)
        try:
            assert len(rt.store.commands()) == 1 and rt.store.cursor() == 1
            rt.poll_once()
            assert len(calls) == 1 and store.get_task(task["uuid"])["status"] == "canceled"
        finally:
            rt.close()


def test_wss_cancel_is_durable_before_ack_loss(tmp_path):
    from unilabos.app.center_client.client import TaskPackage

    server, client, store, receiver = setup_receiver(tmp_path)
    notice = cancellation(server, TaskPackage.from_response(server.package))

    def lost_ack(body):
        if body["type"] == "command.ack":
            assert store.commands(unacknowledged=True) == [notice]
            raise ConnectionError("收件已提交后丢失确认")

    try:
        with pytest.raises(ConnectionError):
            asyncio.run(receiver._exchange(Connection([hello(client.session), envelope(client.session, "task.cancel", notice)], lost_ack), client.session))
        assert store.commands() == [notice]
    finally:
        store.close()


def test_cancel_does_not_override_an_already_failed_task(tmp_path, monkeypatch):
    with _workflow(tmp_path, schedule=True) as (store, service, _, package, scheduler):
        task = _submit(service, package)
        projection, job = _start(store, task)
        projection.project_job_finished(job_uuid=job, scheduler_state="failed", return_info={})
        calls = install_cancel(monkeypatch, scheduler, store)
        server = Center()
        cancellation(server, package)
        rt = runtime(tmp_path, server, service)
        try:
            rt.poll_once()
            assert not calls and not rt.store.commands()
            assert store.get_task(task["uuid"])["status"] == "failed"
            assert server.reports[-1]["payload"]["status"] == "failed"
        finally:
            rt.close()
