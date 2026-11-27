"""OTel 关闭时本地 trace_id 的公共行为测试。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from unilabos.app.edge_control.client import _stored_event_envelope
from unilabos.app.edge_control.store import EdgeControlStore
from unilabos.utils import tracing


@pytest.fixture(autouse=True)
def reset_tracing_context() -> Iterator[None]:
    """隔离每个用例的追踪后端和本地 trace_id，避免测试相互污染。"""

    tracing._reset_for_test()
    yield
    tracing._reset_for_test()


def test_trace_id_is_generated_and_reused_when_otel_is_disabled() -> None:
    """关闭 OTel 时，嵌套操作、线程任务和消息载体复用同一个 trace_id。"""

    with tracing.span("local.root"):
        trace_id, span_id = tracing.current_trace_ids()
        carrier: dict[str, Any] = {}
        tracing.inject_trace_context(carrier)

        with tracing.span(
            "local.child",
            parent_context=tracing.extract_trace_context(carrier),
        ):
            nested_trace_id, nested_span_id = tracing.current_trace_ids()

        with ThreadPoolExecutor(max_workers=1) as executor:
            thread_trace_id, thread_span_id = tracing.submit_with_context(
                executor, tracing.current_trace_ids
            ).result()

    assert len(trace_id) == 32
    assert int(trace_id, 16) != 0
    assert span_id == ""
    assert carrier["trace_id"] == trace_id
    assert "traceparent" not in carrier
    assert (nested_trace_id, nested_span_id) == (trace_id, "")
    assert (thread_trace_id, thread_span_id) == (trace_id, "")
    assert tracing.current_trace_ids() == ("", "")


def test_trace_id_can_be_provided_by_an_independent_async_caller() -> None:
    """独立异步调用携带合法 trace_id 时，OS 使用调用方提供的编号。"""

    provided_trace_id = "0123456789abcdef0123456789abcdef"
    parent_context = tracing.extract_trace_context(
        {"trace_id": provided_trace_id}
    )

    with tracing.span("independent.async", parent_context=parent_context):
        assert tracing.current_trace_ids() == (provided_trace_id, "")


def test_invalid_trace_id_is_not_accepted() -> None:
    """非法或全零 trace_id 不会污染本地上下文，操作会生成新编号。"""

    parent_context = tracing.extract_trace_context(
        {"trace_id": "not-a-trace-id"}
    )
    with tracing.span("invalid.parent", parent_context=parent_context):
        trace_id, span_id = tracing.current_trace_ids()

    assert len(trace_id) == 32
    assert trace_id != "not-a-trace-id"
    assert span_id == ""


def test_log_formatter_prints_local_trace_id() -> None:
    """关闭 OTel 时，日志格式化结果包含当前操作的 trace_id。"""

    from unilabos.utils.log import ColoredFormatter

    record = logging.LogRecord(
        "unilabos.test",
        logging.INFO,
        __file__,
        1,
        "workflow started",
        (),
        None,
    )
    with tracing.span("local.log"):
        trace_id, _ = tracing.current_trace_ids()
        formatted = ColoredFormatter(use_colors=False).format(record)

    assert f"[trace_id={trace_id} span_id=]" in formatted


def test_edge_store_preserves_trace_id_for_replayed_events_and_jobs(
    tmp_path,
) -> None:
    """OTel 关闭时，Edge 重启和结果重放仍保留同一 trace_id。"""

    path = tmp_path / "edge-control.db"
    trace_id = "0123456789abcdef0123456789abcdef"
    job_uuid = str(uuid.uuid4())
    store = EdgeControlStore(str(path))
    event_uuid = store.enqueue_event(
        "job.started",
        {"job_uuid": job_uuid},
        {"trace_id": trace_id},
    )
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "claim_uuid": str(uuid.uuid4()),
            "attempt": 1,
            "fences": [],
            "job_access_token": "token",
        },
        str(uuid.uuid4()),
        {"trace_id": trace_id},
    )
    assert store.save_pending_outcome(job_uuid, "succeeded", {}, [])
    outcome_event_uuid = store.complete_pending_outcome(
        job_uuid,
        {"job_uuid": job_uuid},
    )
    store.close()

    reopened = EdgeControlStore(str(path))
    event = reopened.event_for_ack(event_uuid)
    outcome_event = reopened.event_for_ack(outcome_event_uuid)
    job = reopened.get_job(job_uuid)
    assert event is not None
    assert outcome_event is not None
    assert job is not None
    assert event.trace_id == trace_id
    assert outcome_event.trace_id == trace_id
    assert job.trace_id == trace_id
    assert _stored_event_envelope(event)["trace_id"] == trace_id
    reopened.close()
