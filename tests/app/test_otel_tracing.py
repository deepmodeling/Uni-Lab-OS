"""OpenTelemetry 追踪离线契约测试（不启动 exporter、不访问网络）。"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Dict, Mapping

import pytest

from unilabos.app.scheduler.dispatch import (
    DispatchPayload,
    RecordingDispatcher,
)
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.sync import OutboxWorker
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.utils import tracing
from unilabos.workflow.material_transfer_settlement import MaterialTransferSettlement


class _RecordingSpan:
    def __init__(
        self,
        backend: "_RecordingBackend",
        name: str,
        parent: Any,
        kind: str,
        attributes: Mapping[str, Any],
        *,
        trace_id: int | None = None,
        span_id: int | None = None,
        record: bool = True,
    ):
        self.backend = backend
        self.name = name
        self.parent_span_id = parent.span_id if parent is not None else 0
        self.trace_id = trace_id or (parent.trace_id if parent is not None else next(backend.ids))
        self.span_id = span_id or next(backend.ids)
        self.kind = kind
        self.attributes = dict(attributes)
        self.events: list[tuple[str, Dict[str, Any]]] = []
        self.status = ""
        self.ended = False
        if record:
            backend.spans.append(self)

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        self.events.append((name, dict(attributes or {})))

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: Any) -> None:
        self.status = str(status)

    def get_span_context(self) -> Any:
        return SimpleNamespace(
            is_valid=True,
            trace_id=self.trace_id,
            span_id=self.span_id,
        )

    def end(self) -> None:
        self.ended = True


class _RecordingBackend:
    def __init__(self):
        self.ids = itertools.count(1)
        self.spans: list[_RecordingSpan] = []
        self._current: contextvars.ContextVar[Any] = contextvars.ContextVar(
            "recording_trace_context", default=None
        )

    @staticmethod
    def _span(context_value: Any) -> Any:
        return getattr(context_value, "span", None)

    def start_span(
        self,
        name: str,
        *,
        parent_context: Any,
        kind: str,
        attributes: Mapping[str, Any],
    ) -> tuple[_RecordingSpan, Any]:
        parent = self._span(parent_context or self.current_context())
        started = _RecordingSpan(self, name, parent, kind, attributes)
        return started, SimpleNamespace(span=started)

    def current_context(self) -> Any:
        return self._current.get()

    def attach(self, context_value: Any) -> Any:
        return self._current.set(context_value)

    def detach(self, token: Any) -> None:
        self._current.reset(token)

    def inject(self, carrier: Dict[str, str], context_value: Any = None) -> None:
        current = self._span(context_value or self.current_context())
        if current is not None:
            carrier["traceparent"] = (
                f"00-{current.trace_id:032x}-{current.span_id:016x}-01"
            )

    def extract(self, carrier: Mapping[str, str]) -> Any:
        parts = str(carrier.get("traceparent") or "").split("-")
        if len(parts) != 4:
            return None
        remote = _RecordingSpan(
            self,
            "remote",
            None,
            "internal",
            {},
            trace_id=int(parts[1], 16),
            span_id=int(parts[2], 16),
            record=False,
        )
        return SimpleNamespace(span=remote)

    def current_span(self, context_value: Any = None) -> Any:
        return self._span(context_value or self.current_context())

    def trace_ids(self, context_value: Any = None) -> tuple[str, str]:
        current = self.current_span(context_value)
        if current is None:
            return "", ""
        return f"{current.trace_id:032x}", f"{current.span_id:016x}"

    def record_exception(self, target: _RecordingSpan, exc: BaseException) -> None:
        target.add_event(
            "exception",
            {
                "exception.type": type(exc).__name__,
                "exception.message": str(exc),
            },
        )
        target.status = "error"

    def set_error(self, target: _RecordingSpan, description: str) -> None:
        target.status = f"error:{description}"

    def shutdown(self) -> None:
        return


@pytest.fixture()
def recorder():
    backend = _RecordingBackend()
    tracing._set_backend_for_test(backend)
    try:
        yield backend
    finally:
        tracing._reset_for_test()


def _span_by_name(recorder: _RecordingBackend, name: str) -> list[_RecordingSpan]:
    return [item for item in recorder.spans if item.name == name]


def test_initialization_failure_is_fail_open(monkeypatch):
    """OTel 初始化失败时业务继续运行，并在本地上下文中保留 trace_id。"""

    tracing._reset_for_test()

    def fail_backend(_settings):
        raise RuntimeError("collector setup failed")

    monkeypatch.setattr(tracing, "_OpenTelemetryBackend", fail_backend)
    settings = tracing.TracingSettings(
        enabled=True,
        endpoint="http://127.0.0.1:4317",
    )
    try:
        assert tracing.initialize_tracing(settings) is False
        with tracing.span("still.noop"):
            trace_id, span_id = tracing.current_trace_ids()
            assert len(trace_id) == 32
            assert int(trace_id, 16) != 0
            assert span_id == ""
    finally:
        tracing._reset_for_test()


def test_enabled_otel_rate_limits_exporter_errors_but_keeps_business_errors(
    monkeypatch,
):
    """Collector 缺失时 exporter 同类错误限频，业务错误仍写入本地日志。"""

    tracing._reset_for_test()
    monkeypatch.setattr(
        tracing,
        "_OpenTelemetryBackend",
        lambda _settings: _RecordingBackend(),
    )
    emitted: list[logging.LogRecord] = []

    class RecordingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(record)

    handler = RecordingHandler()
    exporter_loggers = [
        logging.getLogger(name)
        for name in (
            "opentelemetry.exporter.otlp.proto.grpc.exporter",
            "opentelemetry.exporter.otlp.proto.http.trace_exporter",
            "opentelemetry.exporter.otlp.proto.http._log_exporter",
            "opentelemetry.sdk._shared_internal",
            "opentelemetry.sdk.trace.export",
            "opentelemetry.sdk._logs._internal.export",
        )
    ]
    business_logger = logging.getLogger("unilabos.business")
    for exporter_logger in exporter_loggers:
        exporter_logger.addHandler(handler)
    business_logger.addHandler(handler)
    try:
        assert tracing.initialize_tracing(
            tracing.TracingSettings(
                enabled=True,
                endpoint="http://missing-signoz:4317",
            )
        ) is True

        for exporter_logger in exporter_loggers:
            for _ in range(3):
                exporter_logger.error("Failed to export telemetry to missing-signoz")
        business_logger.error("device operation failed")

        assert [record.getMessage() for record in emitted] == [
            *(["Failed to export telemetry to missing-signoz"] * 6),
            "device operation failed",
        ]
    finally:
        for exporter_logger in exporter_loggers:
            exporter_logger.removeHandler(handler)
        business_logger.removeHandler(handler)
        tracing._reset_for_test()


def test_runtime_settings_follow_cloud_otel_environment(monkeypatch):
    monkeypatch.setenv("UNILABOS_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "uni-lab-edge-test")
    monkeypatch.setenv("OTEL_SERVICE_VERSION", "v1.2.3")
    monkeypatch.setenv("OTEL_DEPLOYMENT_ENVIRONMENT", "test")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://logs-collector:4317"
    )
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "64")

    settings = tracing.TracingSettings.from_runtime()

    assert settings.enabled is True
    assert settings.service_name == "uni-lab-edge-test"
    assert settings.service_version == "v1.2.3"
    assert settings.deployment_environment == "test"
    assert settings.endpoint == "http://collector:4317"
    assert settings.protocol == "http/protobuf"
    assert settings.logs_enabled is True
    assert settings.logs_endpoint == "http://logs-collector:4317"
    assert settings.trace_sampler == "parentbased_traceidratio"
    assert settings.sample_ratio == 0.25
    assert settings.max_queue_size == 64


def test_otlp_http_signal_endpoint_appends_signal_without_losing_prefix():
    assert tracing._otlp_http_signal_endpoint(
        "http://collector:4318/otel/", "traces"
    ) == "http://collector:4318/otel/v1/traces"


def test_log_protocol_reuses_trace_protocol_when_not_overridden(monkeypatch):
    from unilabos.config.config import OTelConfig

    monkeypatch.setattr(OTelConfig, "protocol", "http/protobuf")
    monkeypatch.setattr(OTelConfig, "logs_protocol", "")

    settings = tracing.TracingSettings.from_runtime()

    assert settings.protocol == "http/protobuf"
    assert settings.logs_protocol == "http/protobuf"


def test_otel_log_handler_captures_application_logs_without_exporter_recursion():
    root_logger = logging.Logger("unilabos-test-root")
    root_logger.setLevel(logging.INFO)
    emitted: list[logging.LogRecord] = []

    class RecordingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(record)

    handler = tracing._attach_otel_log_handler(
        root_logger,
        RecordingHandler(),
    )

    root_logger.info(
        "workflow started token=secret-value Authorization: Basic abc123",
        extra={"auth_token": "raw-secret"},
    )
    root_logger.handle(logging.LogRecord(
        "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
        logging.ERROR,
        __file__,
        1,
        "collector unavailable",
        (),
        None,
    ))

    assert handler in root_logger.handlers
    assert [record.getMessage() for record in emitted] == [
        "workflow started token=<redacted> Authorization: <redacted>"
    ]
    assert emitted[0].auth_token == "<redacted>"


def test_otel_log_handler_failure_never_escapes_to_business_logger():
    root_logger = logging.Logger("unilabos-fail-open-root")

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("collector unavailable")

    tracing._attach_otel_log_handler(root_logger, FailingHandler())

    root_logger.warning("business operation continues")


def test_active_otel_handler_can_attach_to_non_propagating_comm_logger():
    root_logger = logging.Logger("unilabos-active-root")
    comm_logger = logging.Logger("unilabos.comm")
    comm_logger.setLevel(logging.INFO)
    comm_logger.propagate = False
    emitted: list[logging.LogRecord] = []

    class RecordingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(record)

    handler = tracing._attach_otel_log_handler(root_logger, RecordingHandler())
    try:
        tracing._activate_otel_log_handler(handler, root_logger)
        tracing.attach_active_otel_log_handler(comm_logger)
        comm_logger.info("websocket connected")
    finally:
        tracing._deactivate_otel_log_handler(handler)

    assert [record.getMessage() for record in emitted] == ["websocket connected"]


def test_edge_cors_allows_w3c_trace_context_headers():
    from fastapi.middleware.cors import CORSMiddleware

    from unilabos.app.scheduler.api import create_app

    app = create_app()
    cors = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    allowed = {str(value).lower() for value in cors.kwargs["allow_headers"]}
    exposed = {str(value).lower() for value in cors.kwargs["expose_headers"]}

    assert {"trace_id", "traceparent", "tracestate", "idempotency-key"} <= allowed
    assert {"trace_id", "span_id"} <= exposed


def test_main_web_cors_allows_idempotency_key():
    """跨域提交工作流干预决策时，浏览器预检必须放行幂等键。"""
    from fastapi.middleware.cors import CORSMiddleware

    from unilabos.app.web import server

    cors = next(
        middleware
        for middleware in server.app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    allowed = {str(value).lower() for value in cors.kwargs["allow_headers"]}

    assert "idempotency-key" in allowed


def test_context_propagates_across_carrier_and_thread(recorder):
    def make_child():
        with tracing.span("thread.child") as child_span:
            return child_span

    with tracing.span("request.root", kind="server") as root:
        carrier: Dict[str, Any] = {}
        tracing.inject_trace_context(carrier)
        with ThreadPoolExecutor(max_workers=1) as executor:
            tracing.submit_with_context(executor, make_child).result()

    remote = tracing.extract_trace_context(carrier)
    with tracing.span("remote.server", kind="server", parent_context=remote):
        pass

    thread_child = _span_by_name(recorder, "thread.child")[0]
    remote_server = _span_by_name(recorder, "remote.server")[0]
    assert thread_child.trace_id == root.trace_id
    assert thread_child.parent_span_id == root.span_id
    assert remote_server.trace_id == root.trace_id
    assert remote_server.parent_span_id == root.span_id


def test_scheduler_submission_exposes_a_durable_trace_context_and_reuses_it(
    recorder,
):
    """Task 首次提交和恢复运行必须保留同一 Trace ID。"""

    first_scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    with tracing.span("workflow.create.request") as request_span:
        submitted = first_scheduler.submit_workflow(
            WorkflowSpec(workflow_id="workflow-traced", task_id="task-traced", nodes=[])
        )

    carrier = submitted["trace_context"]
    assert carrier["trace_id"] == f"{request_span.trace_id:032x}"
    assert carrier["traceparent"].startswith(
        f"00-{request_span.trace_id:032x}-"
    )

    recovered_scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    recovered = recovered_scheduler.restore_workflow(
        WorkflowSpec(
            workflow_id="workflow-recovered",
            task_id="task-recovered",
            nodes=[],
            trace_context=carrier,
        ),
        {},
    )

    workflow_spans = _span_by_name(recorder, "workflow.task.run")
    assert len(workflow_spans) == 2
    assert workflow_spans[1].trace_id == workflow_spans[0].trace_id
    assert workflow_spans[1].parent_span_id == workflow_spans[0].span_id
    assert recovered["trace_context"]["trace_id"] == carrier["trace_id"]


def test_signoz_ui_configuration_accepts_only_a_valid_http_base(monkeypatch):
    monkeypatch.setenv("UNILABOS_SIGNOZ_UI_URL", "http://127.0.0.1:30081/signoz/")

    assert tracing.trace_ui_base_url() == "http://127.0.0.1:30081/signoz"
    assert tracing.trace_ui_base_url("javascript:alert(1)") == ""


def test_readiness_exposes_signoz_as_separate_ui_runtime_configuration(monkeypatch):
    from unilabos.app.web.server import api_readiness

    monkeypatch.setenv("UNILABOS_SIGNOZ_UI_URL", "http://127.0.0.1:30081/")

    payload = json.loads(api_readiness().body)

    assert payload["observability"] == {
        "traceUiUrl": "http://127.0.0.1:30081"
    }


def test_workflow_execution_identity_reaches_driver_thread_and_is_restored():
    job_uuid = "6199359e-c8e4-4a86-b709-1c50fc192ff7"
    task_uuid = "89326717-9448-47ce-825a-e679d6556c27"

    assert tracing.capture_workflow_execution_identity() == {}
    with tracing.attach_workflow_execution_identity(job_uuid, task_uuid):
        with ThreadPoolExecutor(max_workers=1) as executor:
            captured = tracing.submit_with_context(
                executor,
                tracing.capture_workflow_execution_identity,
            ).result()

        assert captured == {
            "node_job_uuid": job_uuid,
            "task_uuid": task_uuid,
        }

    assert tracing.capture_workflow_execution_identity() == {}


def test_await_with_context_restores_workflow_identity_for_each_coroutine_step():
    job_uuid = "6199359e-c8e4-4a86-b709-1c50fc192ff7"
    task_uuid = "89326717-9448-47ce-825a-e679d6556c27"

    async def capture_after_yield():
        await asyncio.sleep(0)
        return tracing.capture_workflow_execution_identity()

    async def exercise():
        with tracing.attach_workflow_execution_identity(job_uuid, task_uuid):
            contextual_awaitable = tracing.await_with_context(
                None,
                capture_after_yield(),
            )
        assert tracing.capture_workflow_execution_identity() == {}
        return await contextual_awaitable

    assert asyncio.run(exercise()) == {
        "node_job_uuid": job_uuid,
        "task_uuid": task_uuid,
    }


def test_edge_http_data_plane_injects_client_span_context(recorder):
    from unilabos.app.edge_control.http import EdgeDataPlane
    from unilabos.app.edge_control.store import StoredJob

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"code": 0, "data": {}}

    class Session:
        def __init__(self):
            self.headers: Dict[str, str] = {}
            self.calls: list[Dict[str, Any]] = []

        def request(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            return Response()

    job = StoredJob(
        job_uuid="11111111-1111-1111-1111-111111111111",
        task_uuid="22222222-2222-2222-2222-222222222222",
        node_uuid="33333333-3333-3333-3333-333333333333",
        command_uuid="44444444-4444-4444-4444-444444444444",
        job_access_token="short-token",
        status="received",
        feedback_sequence=0,
    )
    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = Session()
    plane._session = session

    with tracing.span("edge.job.dispatch") as dispatch_span:
        plane.fetch_job(job)

    request_span = _span_by_name(recorder, "edge.http.job.fetch")[0]
    traceparent = session.calls[0]["headers"]["traceparent"]
    parts = traceparent.split("-")
    assert request_span.trace_id == dispatch_span.trace_id
    assert request_span.parent_span_id == dispatch_span.span_id
    assert int(parts[1], 16) == request_span.trace_id
    assert int(parts[2], 16) == request_span.span_id


def test_legacy_backend_session_injects_client_span_context(
    recorder, monkeypatch
):
    import requests

    from unilabos.app.web.client import TracedSession

    calls: list[Dict[str, Any]] = []

    class Response:
        status_code = 200

    def request(_session, method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return Response()

    monkeypatch.setattr(requests.Session, "request", request)
    session = TracedSession()

    with tracing.span("edge.startup") as startup_span:
        session.get("https://backend.example/api/v1/edge/material/download")

    request_span = _span_by_name(recorder, "edge.http.backend.request")[0]
    traceparent = calls[0]["headers"]["traceparent"].split("-")
    assert request_span.kind == "client"
    assert request_span.trace_id == startup_span.trace_id
    assert request_span.parent_span_id == startup_span.span_id
    assert int(traceparent[1], 16) == request_span.trace_id
    assert int(traceparent[2], 16) == request_span.span_id
    assert request_span.attributes["http.response.status_code"] == 200


def test_general_backend_httpx_client_injects_client_span_context(recorder):
    from unilabos.client.http import HTTPClient, HTTPClientConfig

    calls: list[Dict[str, Any]] = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return

        @staticmethod
        def json() -> Dict[str, Any]:
            return {"code": 0, "data": {"ok": True}}

    client = HTTPClient(
        HTTPClientConfig(base_url="https://backend.example/api/v1")
    )

    def request(method, path, **kwargs):
        calls.append({"method": method, "path": path, **kwargs})
        return Response()

    client._client.request = request  # type: ignore[method-assign]
    try:
        with tracing.span("edge.operation") as operation_span:
            assert client.get("/devices") == {"ok": True}
    finally:
        client.close()

    request_span = _span_by_name(recorder, "edge.http.backend.request")[0]
    traceparent = calls[0]["headers"]["traceparent"].split("-")
    assert request_span.trace_id == operation_span.trace_id
    assert request_span.parent_span_id == operation_span.span_id
    assert int(traceparent[1], 16) == request_span.trace_id
    assert int(traceparent[2], 16) == request_span.span_id


def test_edge_websocket_event_injects_send_span_context(recorder, tmp_path):
    from unilabos.app.edge_control.client import (
        EdgeControlClient,
        EdgeControlSettings,
    )
    from unilabos.app.edge_control.store import EdgeControlStore

    class WebSocket:
        def __init__(self, client):
            self.client = client
            self.messages = []

        async def send(self, encoded):
            self.messages.append(json.loads(encoded))
            self.client._stopping.set()

    path = tmp_path / "edge-trace.db"
    settings = EdgeControlSettings(
        scheduler_address="http://scheduler:8081",
        backend_address="http://backend:8080",
        api_key="edge-secret",
        edge_key="edge-test",
        capability_revision="test-v1",
        instance_uuid="",
        state_db=str(path),
        reconnect_interval=0.01,
        request_timeout=1,
        event_retry_interval=0.01,
    )
    store = EdgeControlStore(str(path))
    client = EdgeControlClient(
        settings,
        store=store,
        data_plane=SimpleNamespace(),
        host_node_provider=lambda: None,
    )
    with tracing.span("edge.command.receive") as receive_span:
        client._enqueue_event("command.ack", {"command_uuid": "command-t"})

    websocket = WebSocket(client)
    asyncio.run(client._event_sender(websocket))

    enqueue_span = _span_by_name(recorder, "edge.control.event.enqueue")[0]
    send_span = _span_by_name(recorder, "edge.control.event.send")[0]
    traceparent = websocket.messages[0]["traceparent"].split("-")
    assert enqueue_span.parent_span_id == receive_span.span_id
    assert send_span.parent_span_id == enqueue_span.span_id
    assert int(traceparent[1], 16) == receive_span.trace_id
    assert int(traceparent[2], 16) == send_span.span_id
    store.close()


def test_local_authority_command_replay_keeps_scheduler_and_runtime_in_one_trace(
    recorder,
    tmp_path,
):
    """Scheduler 落盘命令在 Authority 重启后仍把 W3C carrier 送到 Runtime。"""

    from unilabos.app.edge_control.client import (
        EdgeControlClient,
        EdgeControlSettings,
    )
    from unilabos.app.edge_control.local_authority import LocalEdgeAuthorityStore
    from unilabos.app.edge_control.store import EdgeControlStore

    authority_path = tmp_path / "local-authority-trace.db"
    authority = LocalEdgeAuthorityStore(authority_path)
    payload = DispatchPayload(
        job_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        workflow_id=str(uuid.uuid4()),
        node_id=str(uuid.uuid4()),
        device_id="robot-trace",
        action="transfer",
        action_type="normal",
        action_args={"source": "A", "target": "B"},
        attempt=1,
        command_uuid=str(uuid.uuid4()),
        claim_uuid=str(uuid.uuid4()),
        fences=[{"lock_key": "/devices/robot-trace", "fencing_token": 1}],
    )
    with tracing.span("workflow.job.dispatch") as dispatch_span:
        authority.dispatch(payload)
    authority.close()

    restarted_authority = LocalEdgeAuthorityStore(authority_path)
    command = restarted_authority.pending_commands()[0]
    restarted_authority.close()
    assert command["traceparent"].startswith(
        f"00-{dispatch_span.trace_id:032x}-"
    )

    runtime_path = tmp_path / "edge-runtime-trace.db"
    runtime_store = EdgeControlStore(str(runtime_path))
    client = EdgeControlClient(
        EdgeControlSettings(
            scheduler_address="http://scheduler:8081",
            backend_address="http://backend:8080",
            api_key="edge-secret",
            edge_key="edge-test",
            capability_revision="test-v1",
            instance_uuid="",
            state_db=str(runtime_path),
            reconnect_interval=0.01,
            request_timeout=1,
            event_retry_interval=0.01,
        ),
        store=runtime_store,
        data_plane=SimpleNamespace(),
        host_node_provider=lambda: None,
    )
    accepted_carriers: list[dict[str, str]] = []

    async def accept_job_start(
        _command_uuid: str,
        _payload: dict[str, Any],
        carrier: dict[str, str],
    ) -> None:
        accepted_carriers.append(dict(carrier))

    client._accept_job_start = accept_job_start  # type: ignore[method-assign]
    try:
        asyncio.run(client._handle_envelope(command))
    finally:
        runtime_store.close()

    receive_span = _span_by_name(recorder, "edge.command.receive")[0]
    assert receive_span.trace_id == dispatch_span.trace_id
    assert accepted_carriers == [
        {
            "traceparent": command["traceparent"],
            "tracestate": command.get("tracestate", ""),
        }
    ]


def test_errors_and_sensitive_attributes_are_sanitized(recorder):
    with pytest.raises(RuntimeError):
        with tracing.span(
            "failing.operation",
            attributes={
                "authorization_token": "do-not-export",
                "error.message": "Bearer abc.def",
            },
        ):
            raise RuntimeError("boom")

    failed = _span_by_name(recorder, "failing.operation")[0]
    assert "authorization_token" not in failed.attributes
    assert failed.attributes["error.message"] == "Bearer <redacted>"
    assert failed.status == "error"
    assert failed.events[0][0] == "exception"


def test_scheduler_keeps_workflow_action_parentage_and_error_status(recorder):
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = WorkflowSpec(
        workflow_id="wf-trace",
        task_id="task-trace",
        nodes=[
            WorkflowNode(
                id="node-a",
                device_id="device-a",
                action_name="run",
                action_type="goal",
                param={},
            )
        ],
        edges=[],
    )

    with tracing.span("http.server") as request_span:
        submitted = scheduler.submit_workflow(spec)
    scheduler.on_job_finished(
        submitted["dispatched"][0]["job_id"],
        success=False,
        ret_value=None,
    )

    workflow = _span_by_name(recorder, "workflow.task.run")[0]
    action = _span_by_name(recorder, "action.run")[0]
    dispatch = _span_by_name(recorder, "workflow.job.dispatch")[0]
    assert workflow.trace_id == request_span.trace_id
    assert workflow.parent_span_id == request_span.span_id
    assert workflow.attributes["workflow.uuid"] == "wf-trace"
    assert workflow.attributes["workflow.task.uuid"] == "task-trace"
    assert action.parent_span_id == workflow.span_id
    assert dispatch.parent_span_id == action.span_id
    assert action.status.startswith("error:")
    assert workflow.status.startswith("error:")
    assert workflow.ended and action.ended


def test_material_transitions_persist_context_and_outbox_continues_trace(recorder):
    store = InventoryStore(":memory:")
    service = InventoryService(store, edge_id="edge-t", lab_id="lab-t")
    requirement = MaterialRequirement(lot_id="lot-t", quantity=4.0)

    with tracing.span("workflow.task.run") as workflow:
        service.inbound_lot("tpl-t", 10.0, lot_id="lot-t")
        service.reserve_workflow("wf-t", {"node-t": [requirement]})
        service.consume_reservation("wf-t", "node-t")

    ledger = store.query_all("SELECT trace_id, span_id FROM inventory_ledger")
    outbox = store.query_all(
        "SELECT traceparent, trace_id, span_id FROM sync_outbox ORDER BY sequence"
    )
    assert ledger and all(row["trace_id"] and row["span_id"] for row in ledger)
    assert outbox and all(row["traceparent"] for row in outbox)
    assert store.get_lot("lot-t")["quantity_total"] == 6.0

    received: list[Dict[str, Any]] = []

    def sender(events):
        received.extend(events)
        return max(event["sequence"] for event in events)

    OutboxWorker(store, sender).flush_all()
    publish_spans = _span_by_name(recorder, "inventory.outbox.publish")
    assert received and all(event.get("traceparent") for event in received)
    assert publish_spans
    assert all(item.trace_id == workflow.trace_id for item in publish_spans)


def test_action_retry_and_skip_emit_decision_events(recorder):
    from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode

    class DecisionNode:
        device_id = "device-t"

        def __init__(self, action: str):
            self.action = action

        async def _request_action_error_decision(self, *_args, **_kwargs):
            return {"action": self.action}

    async def successful_retry():
        return {"ok": True}

    policy = {
        "max_retries": 2,
        "decision_timeout_seconds": 1,
        "default_on_decision_timeout": "abort",
        "options": {
            "ValueError": [{"action": "retry"}, {"action": "skip"}],
        },
    }
    with tracing.span("action.execute") as action_span:
        retried = asyncio.run(
            BaseROS2DeviceNode._resolve_action_exception(
                DecisionNode("retry"),
                ValueError("transient"),
                successful_retry,
                "run",
                {"job_id": "job-t", "task_id": "task-t"},
                policy,
            )
        )
        skipped = asyncio.run(
            BaseROS2DeviceNode._resolve_action_exception(
                DecisionNode("skip"),
                ValueError("bad sample"),
                successful_retry,
                "run",
                {"job_id": "job-s", "task_id": "task-s"},
                policy,
            )
        )

    event_names = [name for name, _attributes in action_span.events]
    assert retried.value == {"ok": True}
    assert skipped.suc_type == "user_bypass_error"
    assert "action.retry" in event_names
    assert "action.retry.succeeded" in event_names
    assert "action.skipped" in event_names


def test_ros_async_driver_preserves_submit_context_and_runs_once(
    recorder, monkeypatch
):
    import unilabos.ros.nodes.base_device_node as base_device_node

    class ClearedContextExecutor:
        def create_task(self, coroutine):
            async def run_without_inherited_context():
                token = recorder._current.set(None)
                try:
                    return await coroutine
                finally:
                    recorder._current.reset(token)

            return asyncio.create_task(run_without_inherited_context())

    monkeypatch.setattr(
        base_device_node.rclpy,
        "get_global_executor",
        lambda: ClearedContextExecutor(),
    )
    calls = 0
    callback_results = []

    async def operation():
        nonlocal calls
        calls += 1
        with tracing.span("driver.async"):
            return "done"

    async def scenario():
        with tracing.span("action.execute") as action_span:
            future = base_device_node.ROS2DeviceNode.run_async_func(
                operation,
                inner_trace_callback=callback_results.append,
            )
            assert await future == "done"
        return action_span

    action_span = asyncio.run(scenario())
    driver_span = _span_by_name(recorder, "driver.async")[0]
    assert calls == 1
    assert callback_results == ["done"]
    assert driver_span.trace_id == action_span.trace_id
    assert driver_span.parent_span_id == action_span.span_id


def test_material_transfer_settlement_has_child_trace_interface(recorder) -> None:
    """证明 Scheduler 的 Claim/Fence 库存提交具有可独立检索的子 Span。"""

    class Inventory:
        def settle_material_transfer(self, command):
            return {"edge_uuid": command.material_uuid}

    job_uuid = "40000000-0000-4000-8000-000000000001"
    task_uuid = "41000000-0000-4000-8000-000000000001"
    material_uuid = "50000000-0000-4000-8000-000000000001"
    node_uuid = "30000000-0000-4000-8000-000000000001"
    with tracing.span("workflow.job.result") as parent:
        result = MaterialTransferSettlement(Inventory()).settle_success(
            job={
                "uuid": job_uuid,
                "workflow_task_uuid": task_uuid,
                "workflow_node_uuid": node_uuid,
                "executor_kind": "material_transfer",
                "attempt": 1,
                "dispatch_effect_uuid": "effect-001",
                "dispatch_parameter_hash": "sha256:parameters",
                "expected_change_set": {
                    "kind": "material_transfer",
                    "material_uuid": material_uuid,
                    "source_site_uuid": "source-site",
                    "target_site_uuid": "target-site",
                },
                "param": {
                    "resource": {"uuid": material_uuid},
                    "target": {"uuid": "60000000-0000-4000-8000-000000000001"},
                    "site": "S0721",
                },
            },
            execution_plan={
                "nodes": [
                    {
                        "uuid": node_uuid,
                        "action_resource_contract": {
                            "version": 1,
                            "transfer": {
                                "material_param": "resource",
                                "source_owner_param": "",
                                "source_site_uuid_param": "",
                                "source_site_name_param": "",
                                "target_owner_param": "target",
                                "target_site_uuid_param": "",
                                "target_site_name_param": "site",
                                "gripper_site_role": "robot.gripper",
                            },
                        },
                    }
                ]
            },
            execution_claim={
                "claim_uuid": "80000000-0000-4000-8000-000000000001",
                "attempt": 1,
                "fences": [
                    {
                        "lock_key": f"material/{material_uuid}/exclusive",
                        "fencing_token": 3,
                    }
                ],
            },
        )

    settlement_span = _span_by_name(recorder, "inventory.material_transfer.settle")[0]
    assert result == {"edge_uuid": material_uuid}
    assert settlement_span.trace_id == parent.trace_id
    assert settlement_span.parent_span_id == parent.span_id
    assert settlement_span.attributes == {
        "workflow.job.uuid": job_uuid,
        "workflow.task.uuid": task_uuid,
        "inventory.claim.uuid": "80000000-0000-4000-8000-000000000001",
        "material.uuid": material_uuid,
        "inventory.target.owner.uuid": "60000000-0000-4000-8000-000000000001",
        "inventory.target.site.uuid": "target-site",
        "inventory.target.site.name": "S0721",
        "workflow.job.attempt": 1,
    }
    assert settlement_span.ended is True
