"""runtime.v1 控制面服务端（--role backend 进程）与 Edge 客户端契约的往返验证。"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from unilabos.protocol.base import canonical_hash
from unilabos.protocol.runtime.control import (
    BackendCommandDocument,
    BackendCommandNotice,
    BackendSessionNotice,
    CancelJobContent,
    ExecuteJobContent,
)
from unilabos.server.backend.edge_control import (
    EDGE_EVENT_MAX_ATTEMPTS,
    EdgeControlService,
    get_edge_control_service,
    set_edge_control_service,
)
from unilabos.server.backend.scheduler.payloads import build_job_start_payload
from unilabos.server.api.runtime.control import _pump_incoming


class _FakePayloadClient:
    def __init__(self, payloads: dict[str, Any] | None = None) -> None:
        self.payloads = payloads or {}
        self.requested: list[str] = []

    def fetch_json(self, payload_uuid: str) -> Any:
        self.requested.append(payload_uuid)
        return self.payloads.get(payload_uuid)


def _service(payloads: dict[str, Any] | None = None) -> EdgeControlService:
    return EdgeControlService(payload_client=_FakePayloadClient(payloads))


def _dispatch_payload(job_id: str = "job-1", task_id: str = "task-1"):
    return build_job_start_payload(
        job_id=job_id,
        task_id=task_id,
        workflow_id="workflow-1",
        node_id="node-1",
        device_id="device-1",
        action_name="pick",
        action_type="UniAction",
        action_args={"volume": 5},
        materials_need_lock=["plate"],
        scheduler_revision=3,
    )


def _edge_change(
    sequence: int,
    *,
    event_type: str = "execution.succeeded",
    job_uuid: str = "job-1",
    detail_payload_uuid: str | None = "payload-1",
) -> dict[str, Any]:
    return {
        "session_uuid": "session-x",
        "event_uuid": f"event-{sequence}",
        "event_sequence": sequence,
        "event_type": event_type,
        "aggregate_type": "execution_job",
        "aggregate_uuid": job_uuid,
        "aggregate_version": sequence,
        "job_uuid": job_uuid,
        "detail_payload_uuid": detail_payload_uuid,
    }


def test_dispatch_issues_notice_and_document_that_pass_edge_validation() -> None:
    service = _service()
    epoch, session_message = service.attach_connection()

    session = BackendSessionNotice.model_validate(session_message["data"])
    assert session.session_uuid == service.session_uuid
    assert session.connection_epoch == epoch

    service.dispatch(_dispatch_payload())

    message = service.outgoing.get_nowait()
    assert message["action"] == "backend_change"
    notice = BackendCommandNotice.model_validate(message["data"])
    assert notice.command_type == "execute_job"
    assert notice.session_uuid == service.session_uuid
    assert notice.backend_sequence == 1

    # Edge 侧 handle_backend_notice 的三重校验：身份、通知哈希、命令哈希
    document = BackendCommandDocument.model_validate(
        service.get_command_document(notice.command_uuid)
    )
    assert document.command.command_uuid == notice.command_uuid
    assert canonical_hash(document.payload) == notice.content_sha256
    assert canonical_hash(document.payload) == document.command.payload_sha256

    content = ExecuteJobContent.model_validate(document.payload)
    assert content.job_uuid == "job-1"
    assert content.device_uuid == "device-1"
    assert content.action_args == {"volume": 5}
    assert content.materials_need_lock == ["plate"]
    assert content.scheduler_revision == 3
    assert service.active_job_ids() == ["job-1"]


def test_dispatch_preserves_attempt_group_and_retry_link() -> None:
    service = _service()
    service.attach_connection()
    service.dispatch(
        build_job_start_payload(
            job_id="job-2",
            task_id="task-1",
            workflow_id="workflow-1",
            node_id="node-1",
            node_run_uuid="run-1",
            attempt_no=2,
            retry_of_job_uuid="job-1",
            device_id="device-1",
            action_name="pick",
            action_type="UniAction",
            action_args={"volume": 5},
        )
    )
    notice = BackendCommandNotice.model_validate(service.outgoing.get_nowait()["data"])
    document = BackendCommandDocument.model_validate(
        service.get_command_document(notice.command_uuid)
    )
    content = ExecuteJobContent.model_validate(document.payload)
    assert content.attempt_group_uuid == "run-1"
    assert content.attempt_no == 2
    assert content.retry_of_job_uuid == "job-1"
    assert content.attempt_trigger == "initial"


def test_dispatch_carries_loop_iteration_attempts_without_a_retry_link() -> None:
    """循环体第 N 轮：attempt_no > 1、无重试链，靠 attempt_trigger=loop_iteration 通过 runtime.v1 校验；
    retry_count 由调度器给出（不能再从 attempt_no 推）。"""

    service = _service()
    service.attach_connection()
    service.dispatch(
        build_job_start_payload(
            job_id="job-3",
            task_id="task-1",
            workflow_id="workflow-1",
            node_id="node-1",
            node_run_uuid="run-1",
            attempt_no=3,
            attempt_trigger="loop_iteration",
            retry_count=0,
            device_id="device-1",
            action_name="pick",
            action_type="UniAction",
            action_args={"volume": 5},
        )
    )
    notice = BackendCommandNotice.model_validate(service.outgoing.get_nowait()["data"])
    document = BackendCommandDocument.model_validate(
        service.get_command_document(notice.command_uuid)
    )
    content = ExecuteJobContent.model_validate(document.payload)
    assert content.attempt_no == 3
    assert content.retry_of_job_uuid is None
    assert content.attempt_trigger == "loop_iteration"
    assert content.retry_count == 0


def test_terminal_event_invokes_listener_with_local_semantics() -> None:
    detail = {
        "status": "success",
        "feedback_data": {},
        "return_info": {
            "error": "",
            "suc": True,
            "return_value": {"mass": 1.5},
            "suc_type": "normal",
        },
    }
    service = _service({"payload-1": detail})
    service.attach_connection()
    service.dispatch(_dispatch_payload())
    finished: list[tuple] = []
    # 与 JobExecutionBackend._notify_finished 同签名：第五个参数是 return_info
    service.add_job_finished_listener(
        lambda job_id, success, ret, suc_type, return_info: finished.append(
            (job_id, success, ret, suc_type, return_info)
        )
    )

    reply = service.handle_message("edge_change", _edge_change(1))

    assert reply is not None and reply["action"] == "edge_change_ack"
    assert reply["data"]["through_sequence"] == 1
    assert finished == [("job-1", True, {"mass": 1.5}, "normal", detail["return_info"])]
    assert service.active_job_ids() == []
    # 终态后命令存储被清理
    notice = service.outgoing.get_nowait()
    assert service.get_command_document(notice["data"]["command_uuid"]) is None


def test_error_pending_event_becomes_decision_and_release_command() -> None:
    """Edge 打开终态闸门 → 权威登记决策并通知调度器；决策后签发 release_failed，
    confirmed_scheduler_revision 满足闸门要求；operator_intervention 带结果走 replace_result。"""

    from unilabos.protocol.runtime.control import ErrorDecisionContent

    report = {
        "decision_id": "decision-1",
        "job_id": "job-1",
        "device_id": "device-1",
        "action_name": "pick",
        "options": [{"action": "abort"}, {"action": "retry"}, {"action": "operator_intervention"}],
        "retry_count": 0,
        "max_retries": 3,
    }
    service = _service({"snapshot-1": {"report": report, "item": {}, "return_info": {}}})
    service.attach_connection()
    service.dispatch(_dispatch_payload())
    service.outgoing.get_nowait()
    required: list[dict[str, Any]] = []
    service.result_bridges.append(
        type("Bridge", (), {"publish_job_error_decision_required": lambda self, r: required.append(r)})()
    )

    reply = service.handle_message(
        "edge_change",
        _edge_change(1, event_type="execution.error_pending", detail_payload_uuid="snapshot-1"),
    )
    assert reply["action"] == "edge_change_ack"
    assert [item["decision_id"] for item in service.list_error_decisions()] == ["decision-1"]
    assert required and required[0]["decision_id"] == "decision-1"
    # job 仍在途：重启协调器据此等待
    assert service.active_job_ids() == ["job-1"]

    assert service.resolve_error_decision("missing", {"action": "abort"}) is False
    assert service.resolve_error_decision("decision-1", {"action": "not-an-option"}) is False

    assert service.resolve_error_decision("decision-1", {"action": "retry", "reason": "瞬时故障"}) is True
    notice = BackendCommandNotice.model_validate(service.outgoing.get_nowait()["data"])
    assert notice.command_type == "release_failed"
    document = BackendCommandDocument.model_validate(service.get_command_document(notice.command_uuid))
    content = ErrorDecisionContent.model_validate(document.payload)
    assert content.decision_uuid == "decision-1"
    assert content.selected_action == "retry"
    # dispatch 时 scheduler_revision=3，Edge 闸门要求 >= 4
    assert content.confirmed_scheduler_revision == 4
    assert service.list_error_decisions() == []

    # 人工替换结果 → replace_result 且带 result
    service.handle_message(
        "edge_change",
        _edge_change(2, event_type="execution.error_pending", detail_payload_uuid="snapshot-1"),
    )
    assert service.resolve_error_decision(
        "decision-1", {"action": "operator_intervention", "result": {"ok": True}}
    ) is True
    notice = BackendCommandNotice.model_validate(service.outgoing.get_nowait()["data"])
    assert notice.command_type == "replace_result"
    content = ErrorDecisionContent.model_validate(
        BackendCommandDocument.model_validate(service.get_command_document(notice.command_uuid)).payload
    )
    assert content.result == {"ok": True}


def test_retry_decision_respects_attempt_limit() -> None:
    report = {
        "decision_id": "decision-2",
        "job_id": "job-1",
        "device_id": "device-1",
        "options": [{"action": "retry"}, {"action": "abort"}],
        "retry_count": 3,
        "max_retries": 3,
    }
    service = _service({"snapshot-2": {"report": report}})
    service.attach_connection()
    service.dispatch(_dispatch_payload())
    service.handle_message(
        "edge_change",
        _edge_change(1, event_type="execution.error_pending", detail_payload_uuid="snapshot-2"),
    )
    assert service.resolve_error_decision("decision-2", {"action": "retry"}) is False
    assert service.resolve_error_decision("decision-2", {"action": "abort"}) is True


def test_backend_http_roundtrip_over_control_plane() -> None:
    """权威把一条 HTTP 请求下发到 WS 队列；Edge 用 HTTP 送回结果后等待者拿到响应。"""

    from unilabos.protocol.runtime.control import BackendHttpRequest, EdgeHttpResponse

    service = _service()
    service.attach_connection()

    def _edge_side() -> None:
        # 模拟 Edge：从下行队列取到请求，执行后回送
        message = service.outgoing.get(timeout=2)
        assert message["action"] == "backend_http"
        request = BackendHttpRequest.model_validate(message["data"])
        assert request.method == "POST" and request.path == "/api/v1/restart"
        assert json.loads(request_body := __import__("base64").b64decode(request.body_base64)) == {"mode": "immediate"}
        service.complete_http_response(
            EdgeHttpResponse(
                request_uuid=request.request_uuid,
                status_code=200,
                headers={"content-type": "application/json"},
                body_base64=__import__("base64").b64encode(b'{"ok": true}').decode(),
            )
        )

    worker = threading.Thread(target=_edge_side, daemon=True)
    worker.start()
    response = service.http_request(
        "POST", "/api/v1/restart", headers={"content-type": "application/json"},
        body=b'{"mode": "immediate"}', timeout=5,
    )
    worker.join(timeout=5)
    assert response is not None and response.status_code == 200
    assert json.loads(response.body_bytes()) == {"ok": True}
    # 迟到 / 未知的响应被忽略
    assert service.complete_http_response(
        EdgeHttpResponse(request_uuid="unknown", status_code=200)
    ) is False


def test_backend_http_fails_fast_when_edge_offline() -> None:
    service = _service()
    assert service.http_request("GET", "/api/v1/health", timeout=1) is None

    epoch, _ = service.attach_connection()
    results: list = []
    worker = threading.Thread(
        target=lambda: results.append(service.http_request("GET", "/api/v1/health", timeout=10)),
        daemon=True,
    )
    worker.start()
    service.outgoing.get(timeout=2)
    # 连接断开：等待中的请求立刻返回 None，而不是等到超时
    service.detach_connection(epoch)
    worker.join(timeout=3)
    assert results == [None]


def test_payload_client_reads_history_payload_via_backend_http() -> None:
    from unilabos.protocol.runtime.control import BackendHttpRequest, EdgeHttpResponse

    service = EdgeControlService()
    service.attach_connection()
    document = {"inline_payload": __import__("base64").b64encode(b'{"status": "success"}').decode(), "encoding": "utf-8"}

    def _edge_side() -> None:
        message = service.outgoing.get(timeout=2)
        request = BackendHttpRequest.model_validate(message["data"])
        assert request.path == "/api/v1/history/payloads/payload-9"
        service.complete_http_response(
            EdgeHttpResponse(
                request_uuid=request.request_uuid,
                status_code=200,
                body_base64=__import__("base64").b64encode(json.dumps(document).encode()).decode(),
            )
        )

    threading.Thread(target=_edge_side, daemon=True).start()
    assert service._payloads.fetch_json("payload-9") == {"status": "success"}


def test_duplicate_edge_events_ack_but_apply_once() -> None:
    service = _service({"payload-1": {"return_info": {"suc": False}}})
    service.attach_connection()
    finished: list[str] = []
    service.add_job_finished_listener(
        lambda job_id, *_args: finished.append(job_id)
    )

    first = service.handle_message(
        "edge_change", _edge_change(1, event_type="execution.failed")
    )
    second = service.handle_message(
        "edge_change", _edge_change(1, event_type="execution.failed")
    )

    assert first is not None and second is not None
    assert second["data"]["through_sequence"] == 1
    assert finished == ["job-1"]


def _finished_recorder(service: EdgeControlService) -> list[tuple[str, bool, Any]]:
    finished: list[tuple[str, bool, Any]] = []
    service.add_job_finished_listener(
        lambda job_id, success, ret_value, *_rest: finished.append((job_id, success, ret_value))
    )
    return finished


def test_event_whose_payload_is_unavailable_is_not_acked_until_redelivered() -> None:
    """详情 payload 暂时取不到：不确认、不落地，Host 重发时再处理（以前会带空结果直接确认）。"""

    service = _service()
    service.attach_connection()
    finished = _finished_recorder(service)

    assert service.handle_message("edge_change", _edge_change(1)) is None
    assert finished == []

    service._payloads.payloads["payload-1"] = {"return_info": {"suc": True, "return_value": {"volume": 5}}}
    ack = service.handle_message("edge_change", _edge_change(1))
    assert ack is not None and ack["data"]["through_sequence"] == 1
    assert finished == [("job-1", True, {"volume": 5})]


def test_events_after_a_failed_one_wait_because_acks_are_cumulative() -> None:
    service = _service({"payload-2": {"return_info": {"return_value": 2}}})
    service.attach_connection()
    finished = _finished_recorder(service)

    assert service.handle_message("edge_change", _edge_change(1)) is None
    # #2 能处理，但确认到 #2 会连带确认还没落地的 #1：先不处理也不确认
    later = _edge_change(2, job_uuid="job-2", detail_payload_uuid="payload-2")
    assert service.handle_message("edge_change", later) is None
    assert finished == []

    service._payloads.payloads["payload-1"] = {"return_info": {"return_value": 1}}
    assert service.handle_message("edge_change", _edge_change(1))["data"]["through_sequence"] == 1
    assert service.handle_message("edge_change", later)["data"]["through_sequence"] == 2
    assert finished == [("job-1", True, 1), ("job-2", True, 2)]
    # 重复投递只重新确认
    assert service.handle_message("edge_change", later)["data"]["through_sequence"] == 2
    assert len(finished) == 2


def test_unexpected_apply_error_is_retried_then_dead_lettered() -> None:
    service = _service({"payload-2": {"return_info": {}}})
    service.attach_connection()
    finished = _finished_recorder(service)
    calls: list[int] = []
    original = service._apply_edge_event

    def broken(notice, *, final_attempt: bool = False) -> None:
        if notice.event_sequence == 1:
            calls.append(notice.event_sequence)
            raise ValueError("坏事件")
        original(notice, final_attempt=final_attempt)

    service._apply_edge_event = broken  # type: ignore[method-assign]
    for _ in range(EDGE_EVENT_MAX_ATTEMPTS - 1):
        assert service.handle_message("edge_change", _edge_change(1, job_uuid="job-bad")) is None
    ack = service.handle_message("edge_change", _edge_change(1, job_uuid="job-bad"))
    assert ack is not None and ack["data"]["through_sequence"] == 1
    assert len(calls) == EDGE_EVENT_MAX_ATTEMPTS
    letters = service.edge_dead_letters()
    assert [(item["event_sequence"], item["job_uuid"], item["attempts"]) for item in letters] == [
        (1, "job-bad", EDGE_EVENT_MAX_ATTEMPTS)
    ]
    assert "坏事件" in letters[0]["error"]

    # 死信不再挡住后面的事件
    ack = service.handle_message("edge_change", _edge_change(2, job_uuid="job-2", detail_payload_uuid="payload-2"))
    assert ack["data"]["through_sequence"] == 2
    assert finished == [("job-2", True, None)]


def test_payload_that_never_arrives_settles_degraded_on_the_last_attempt() -> None:
    """拉了 N 次仍取不到：按降级口径落地（空 return_info），不让 job 永远停在运行中。"""

    service = _service()
    service.attach_connection()
    finished = _finished_recorder(service)
    for _ in range(EDGE_EVENT_MAX_ATTEMPTS - 1):
        assert service.handle_message("edge_change", _edge_change(1, event_type="execution.failed")) is None
    ack = service.handle_message("edge_change", _edge_change(1, event_type="execution.failed"))
    assert ack["data"]["through_sequence"] == 1
    assert finished == [("job-1", False, None)]
    assert service.edge_dead_letters() == []
    assert service._payloads.requested == ["payload-1"] * EDGE_EVENT_MAX_ATTEMPTS


def test_error_decision_waits_for_its_snapshot() -> None:
    service = _service()
    service.attach_connection()
    service.dispatch(_dispatch_payload(job_id="job-1"))
    event = _edge_change(1, event_type="execution.error_pending", detail_payload_uuid="decision-1")

    assert service.handle_message("edge_change", event) is None
    assert service.list_error_decisions() == []

    service._payloads.payloads["decision-1"] = {
        "report": {"decision_id": "d-1", "job_uuid": "job-1", "options": ["retry", "fail"]}
    }
    assert service.handle_message("edge_change", event)["data"]["through_sequence"] == 1
    assert [item.get("decision_id") for item in service.list_error_decisions()] == ["d-1"]


def test_cancel_task_issues_cancel_commands_for_inflight_jobs() -> None:
    service = _service()
    service.attach_connection()
    service.dispatch(_dispatch_payload(job_id="job-1", task_id="task-9"))
    service.dispatch(_dispatch_payload(job_id="job-2", task_id="task-other"))
    service.outgoing.get_nowait()
    service.outgoing.get_nowait()

    service.cancel_task("task-9")

    message = service.outgoing.get_nowait()
    notice = BackendCommandNotice.model_validate(message["data"])
    assert notice.command_type == "cancel_job"
    document = BackendCommandDocument.model_validate(
        service.get_command_document(notice.command_uuid)
    )
    assert document.command.job_uuid == "job-1"
    CancelJobContent.model_validate(document.payload)
    with pytest.raises(Exception):
        service.outgoing.get_nowait()


def test_reconnect_replays_only_unfetched_commands() -> None:
    service = _service()
    # 未连接时 dispatch：通知被丢弃，命令仍权威保存
    service.dispatch(_dispatch_payload(job_id="job-1"))
    service.dispatch(_dispatch_payload(job_id="job-2", task_id="task-2"))
    assert service.outgoing.qsize() == 0

    epoch1, _ = service.attach_connection()
    replayed = [service.outgoing.get_nowait() for _ in range(2)]
    sequences = [item["data"]["backend_sequence"] for item in replayed]
    assert sequences == [1, 2]
    # 每条补发通知携带当前连接代际
    assert all(
        item["data"]["connection_epoch"] == epoch1 for item in replayed
    )

    # Edge 拉取第一条后再次断连重连：只补发未拉取的第二条
    service.get_command_document(replayed[0]["data"]["command_uuid"])
    service.detach_connection(epoch1)
    _epoch2, _ = service.attach_connection()
    resent = service.outgoing.get_nowait()
    assert resent["data"]["backend_sequence"] == 2
    assert service.outgoing.qsize() == 0


def test_stale_epoch_detach_does_not_break_new_connection() -> None:
    service = _service()
    old_epoch, _ = service.attach_connection()
    new_epoch, _ = service.attach_connection()

    service.detach_connection(old_epoch)
    assert service.connected

    service.detach_connection(new_epoch)
    assert not service.connected


def test_restart_coordinator_uses_edge_scope_and_inflight_view() -> None:
    from unilabos.server.backend.restart import RestartCoordinator

    service = _service()
    set_edge_control_service(service)
    try:
        coordinator = RestartCoordinator(lambda: None, lambda: None)
        assert get_edge_control_service() is service
        assert coordinator._resolve_scope() == "edge"

        service.attach_connection()
        service.dispatch(_dispatch_payload())
        assert coordinator.active_job_ids() == ["job-1"]

        with pytest.raises(ValueError):
            coordinator.request(scope="devices")
    finally:
        set_edge_control_service(None)


def test_ping_is_answered_with_pong() -> None:
    service = _service()
    reply = service.handle_message(
        "ping", {"ping_id": "p1", "client_timestamp": 1.0}
    )
    assert reply["action"] == "pong"
    # 回显 Edge 字段并补 server_timestamp，供 host test_latency 计算时钟偏差；
    # pong 按 PongNotice 严格重建，只有契约里的三个字段。
    assert set(reply["data"]) == {"ping_id", "client_timestamp", "server_timestamp"}
    assert reply["data"]["ping_id"] == "p1"
    assert reply["data"]["client_timestamp"] == 1.0
    assert isinstance(reply["data"]["server_timestamp"], float)


def test_malformed_heartbeat_is_rejected_before_business_handlers() -> None:
    from pydantic import ValidationError

    service = _service()
    with pytest.raises(ValidationError):
        service.handle_message("ping", {"client_timestamp": 1.0})
    # 心跳契约不接受业务正文或旧协议别名混入。
    with pytest.raises(ValidationError):
        service.handle_message(
            "ping", {"ping_id": "p1", "client_timestamp": 1.0, "action_args": {}}
        )
    with pytest.raises(ValidationError):
        service.handle_message("pong", {"ping_id": "p1", "client_timestamp": 1.0})
    assert service.handle_message(
        "pong", {"ping_id": "p1", "client_timestamp": 1.0, "server_timestamp": 2.0}
    ) is None


def test_scheduler_stamps_server_info_only_for_test_latency() -> None:
    plain = _dispatch_payload()
    assert "server_info" not in plain
    latency = build_job_start_payload(
        job_id="job-2",
        task_id="task-1",
        workflow_id="workflow-1",
        node_id="node-2",
        device_id="host_node",
        action_name="test_latency",
        action_type="UniLabJsonCommand",
        action_args={},
    )
    # host_node 用它计算“服务端下发 → 客户端开始”的任务延迟。
    assert isinstance(latency["server_info"]["send_timestamp"], float)
    assert latency["server_info"]["send_timestamp"] > 0


def test_server_receive_loop_answers_ping_while_business_handler_is_blocked() -> None:
    """慢 edge_change 处理不能占住 WS receive loop，导致后续 ping 超时。"""

    async def scenario() -> None:
        business_started = threading.Event()
        business_release = threading.Event()
        log_received = asyncio.Event()

        class _Service:
            connection_epoch = "epoch-1"

            def handle_message(self, action: str, data: dict[str, Any]):
                if action == "runtime_logs_changed":
                    log_received.set()
                    return None
                if action == "edge_change":
                    business_started.set()
                    business_release.wait(timeout=2)
                    return None
                if action == "ping":
                    return {
                        "action": "pong",
                        "data": {
                            "ping_id": data["ping_id"],
                            "client_timestamp": data["client_timestamp"],
                            "server_timestamp": 2.0,
                        },
                    }
                return None

        class _Socket:
            def __init__(self) -> None:
                self.incoming: asyncio.Queue[str] = asyncio.Queue()
                self.sent: list[dict[str, Any]] = []
                self.sent_event = asyncio.Event()

            async def receive_text(self) -> str:
                return await self.incoming.get()

            async def send_text(self, value: str) -> None:
                self.sent.append(json.loads(value))
                self.sent_event.set()

        service = _Service()
        socket = _Socket()
        pump = asyncio.create_task(_pump_incoming(socket, service, "epoch-1"))
        try:
            await socket.incoming.put(
                json.dumps({"action": "edge_change", "data": {"event_sequence": 1}})
            )
            assert await asyncio.to_thread(business_started.wait, 1)
            await socket.incoming.put(json.dumps({"action": "runtime_logs_changed", "data": {"source_ids": ["host"]}}))
            await asyncio.wait_for(log_received.wait(), timeout=.5)
            await socket.incoming.put(
                json.dumps(
                    {
                        "action": "ping",
                        "data": {"ping_id": "p1", "client_timestamp": 1.0},
                    }
                )
            )
            await asyncio.wait_for(socket.sent_event.wait(), timeout=0.5)
            assert socket.sent == [
                {
                    "action": "pong",
                    "data": {
                        "ping_id": "p1",
                        "client_timestamp": 1.0,
                        "server_timestamp": 2.0,
                    },
                }
            ]
            assert not business_release.is_set()
        finally:
            business_release.set()
            pump.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pump

    asyncio.run(scenario())


def _wait_for(predicate, timeout: float = 2.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _notice_for(service: EdgeControlService) -> BackendCommandNotice:
    return BackendCommandNotice.model_validate(service.outgoing.get_nowait()["data"])


def test_host_process_restart_reports_started_jobs_as_unknown() -> None:
    from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN

    service = _service()
    calls: list[tuple] = []
    service.add_job_finished_listener(lambda *args: calls.append(args))
    service.attach_connection(edge_process="host-process-1")
    service.dispatch(_dispatch_payload("job-started", "task-1"))
    service.dispatch(_dispatch_payload("job-unfetched", "task-2"))
    started_notice = _notice_for(service)
    service.get_command_document(started_notice.command_uuid)  # 旧 Host 拉取了第一个 job 的命令
    unfetched_notice = _notice_for(service)

    service.attach_connection(edge_process="host-process-2")

    assert _wait_for(lambda: calls)
    ((job_uuid, success, ret_value, suc_type, info),) = calls
    assert (job_uuid, success, ret_value, suc_type) == ("job-started", False, None, SUCCESS_TYPE_EXECUTION_UNKNOWN)
    assert "Host" in info["uncertainty_reason"]
    # 从未被拉取的命令没开始执行：仍在途，并补发给新 Host
    assert service.active_job_ids() == ["job-unfetched"]
    resent = _notice_for(service)
    assert resent.command_uuid == unfetched_notice.command_uuid
    assert service.get_command_document(started_notice.command_uuid) is None


def test_reconnect_of_the_same_host_process_keeps_jobs_in_flight() -> None:
    service = _service()
    calls: list[tuple] = []
    service.add_job_finished_listener(lambda *args: calls.append(args))
    service.attach_connection(edge_process="host-process-1")
    service.dispatch(_dispatch_payload())
    service.get_command_document(_notice_for(service).command_uuid)

    service.attach_connection(edge_process="host-process-1")  # 网络断线重连
    service.attach_connection()  # 没带身份的连接（旧客户端）也不触发

    assert not _wait_for(lambda: calls, timeout=0.2)
    assert service.active_job_ids() == ["job-1"]


def test_unknown_start_event_reaches_scheduler_as_unknown_not_failure():
    from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
    detail = {"status": "failed", "return_info": {"uncertainty_reason": "开始不确定"}}
    service = _service({"payload-1": detail})
    service.attach_connection()
    service.dispatch(_dispatch_payload())
    seen = []
    service.add_job_finished_listener(lambda *args: seen.append(args))
    assert service.handle_message("edge_change", _edge_change(1, event_type="execution.execution_unknown"))
    assert seen[0][3] == SUCCESS_TYPE_EXECUTION_UNKNOWN
    assert seen[0][4]["uncertainty_reason"] == "开始不确定"
