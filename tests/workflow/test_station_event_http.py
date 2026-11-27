"""验证工站事件向上游 Backend 的 HTTP 投影合同。"""

from __future__ import annotations

from typing import Any

from unilabos.workflow.station_event_http import BackendStationEventSender


class _Response:
    """提供测试所需的最小成功 HTTP 响应。"""

    def raise_for_status(self) -> None:
        """模拟 HTTP 状态成功；参数和返回值为空。"""

    def json(self) -> dict[str, Any]:
        """返回一个精确确认首个工站事件的业务响应。"""

        return {
            "code": 0,
            "data": {
                "acknowledgements": [{"event_uuid": "event-1", "station_sequence": 1}]
            },
        }


class _Session:
    """记录 Backend 投影请求的测试会话。"""

    def __init__(self) -> None:
        """初始化空请求记录；参数和返回值为空。"""

        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        """记录一次 POST 并返回成功响应。"""

        self.calls.append({"url": url, **kwargs})
        return _Response()


def _event(sequence: int) -> dict[str, Any]:
    """构造包含本地投递字段的完整工站事件测试快照。"""

    return {
        "event_uuid": f"event-{sequence}",
        "station_sequence": sequence,
        "created_at": "2026-08-30T00:00:00Z",
        "update_time": "2026-08-30T00:00:01Z",
        "event_type": "job.completed",
        "aggregate_type": "workflow_node_job",
        "aggregate_uuid": f"job-{sequence}",
        "source_kind": "job_result",
        "source_uuid": f"result-{sequence}",
        "idempotency_key": f"job-{sequence}:completed",
        "payload": {"actual_param": {"temperature": 25}},
        "delivery_attempts": 3,
        "last_attempt_at": "2026-08-30T00:00:01Z",
        "acked_at": None,
    }


def test_sender_posts_station_facts_and_accepts_precise_ack_prefix() -> None:
    """投影只发送业务事实，并使用上游凭据和批次幂等身份。"""

    session = _Session()
    sender = BackendStationEventSender(
        "https://backend.example.test/api/v1",
        "backend-secret",
        "station-a",
        session=session,  # type: ignore[arg-type]
    )

    acknowledgements = sender([_event(1), _event(2)])

    assert acknowledgements == [{"event_uuid": "event-1", "station_sequence": 1}]
    request = session.calls[0]
    assert request["url"] == ("https://backend.example.test/api/v1/edge/station-events")
    assert request["headers"]["Authorization"] == "Bearer backend-secret"
    assert request["headers"]["Idempotency-Key"] == "station-a:1:2"
    assert request["json"]["station_key"] == "station-a"
    sent = request["json"]["events"][0]
    assert sent["payload"] == {"actual_param": {"temperature": 25}}
    assert "delivery_attempts" not in sent
    assert "last_attempt_at" not in sent
    assert "acked_at" not in sent
