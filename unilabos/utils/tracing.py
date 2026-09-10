"""Uni-Lab Edge 的可选 OpenTelemetry 追踪层。

默认不初始化 SDK；只有显式开启 ``OTelConfig.enabled`` 或
``UNILABOS_OTEL_ENABLED=true`` 时才创建 OTLP exporter。模块自身不硬依赖
OpenTelemetry，未安装依赖、配置错误或 SigNoz 不可达时都按 no-op/fail-open
处理，不允许观测链路影响仪器控制。

跨进程/线程边界统一使用 W3C Trace Context（``traceparent`` /
``tracestate``），同时附带只读关联字段 ``trace_id`` / ``span_id`` 供云端表和
日志检索；后两者不用于恢复父上下文。
"""

from __future__ import annotations

import atexit
import copy
import contextlib
import contextvars
import logging
import os
import re
import threading
import traceback
import types
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Mapping, MutableMapping, Optional
from urllib.parse import unquote, urlsplit, urlunsplit


logger = logging.getLogger(__name__)

_ACTIVE_OTEL_LOG_HANDLER: Optional[logging.Handler] = None
_ACTIVE_OTEL_LOG_TARGETS: set[logging.Logger] = set()

INSTRUMENTATION_NAME = "unilabos.edge"
TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"
TRACE_ID = "trace_id"
SPAN_ID = "span_id"

_WORKFLOW_EXECUTION_IDENTITY: contextvars.ContextVar[Dict[str, str]] = (
    contextvars.ContextVar("unilabos_workflow_execution_identity", default={})
)

_SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|password|passwd|secret|token|api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)\b(bearer|lab)\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key|access[_-]?key)"
    r"(\s*[:=]\s*)[^\s,;]+"
)
_HTTP_SECRET_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)"
    r"(\s*:\s*)[^\r\n]+"
)


@contextlib.contextmanager
def attach_workflow_execution_identity(
    node_job_uuid: str,
    task_uuid: str,
) -> Iterator[None]:
    """让驱动调用沿 Python context 继承已认证的工作流执行身份。"""

    token = _WORKFLOW_EXECUTION_IDENTITY.set(
        {
            "node_job_uuid": str(node_job_uuid or ""),
            "task_uuid": str(task_uuid or ""),
        }
    )
    try:
        yield None
    finally:
        _WORKFLOW_EXECUTION_IDENTITY.reset(token)


def capture_workflow_execution_identity() -> Dict[str, str]:
    """读取当前驱动调用所属的工作流节点作业与工作流任务身份。"""

    return dict(_WORKFLOW_EXECUTION_IDENTITY.get())


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        return min(maximum, max(minimum, float(value)))
    except (TypeError, ValueError):
        return default


def _parse_headers(raw: str) -> Dict[str, str]:
    """解析 OTEL_EXPORTER_OTLP_HEADERS；不记录 header 值。"""

    headers: Dict[str, str] = {}
    for item in (raw or "").split(","):
        if not item.strip() or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = unquote(key.strip())
        if key:
            headers[key] = unquote(value.strip())
    return headers


def _parse_resource_attributes(raw: str) -> Dict[str, str]:
    attributes: Dict[str, str] = {}
    for item in (raw or "").split(","):
        if not item.strip() or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = unquote(key.strip())
        if not key or _SENSITIVE_KEY.search(key):
            continue
        attributes[key] = _sanitize_text(unquote(value.strip()), 256)
    return attributes


def _sanitize_text(value: Any, limit: int = 1024) -> str:
    text = str(value or "")
    text = _BEARER_VALUE.sub(r"\1 <redacted>", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", text)
    text = _HTTP_SECRET_HEADER.sub(r"\1\2<redacted>", text)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _safe_attributes(attributes: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """只保留低体积标量；调用方不得传 recipe/payload/动作参数原文。"""

    result: Dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        key = str(key)
        if not key or _SENSITIVE_KEY.search(key) or value is None:
            continue
        if isinstance(value, bool):
            result[key] = value
        elif isinstance(value, int):
            result[key] = value
        elif isinstance(value, float):
            result[key] = value
        elif isinstance(value, str):
            limit = 16384 if key == "exception.stacktrace" else 2048 if key == "exception.message" else 256
            result[key] = _sanitize_text(value, limit)
        elif isinstance(value, (list, tuple)) and len(value) <= 16:
            cleaned = []
            for item in value:
                if isinstance(item, (bool, int, float)):
                    cleaned.append(item)
                elif isinstance(item, str):
                    cleaned.append(_sanitize_text(item, 128))
            if cleaned:
                result[key] = cleaned
    return result


@dataclass(frozen=True)
class TracingSettings:
    enabled: bool = False
    service_name: str = "uni-lab-edge"
    service_namespace: str = "unilab"
    service_version: str = "0.11.3"
    deployment_environment: str = ""
    endpoint: str = ""
    protocol: str = "grpc"
    logs_enabled: bool = True
    logs_endpoint: str = ""
    logs_protocol: str = "grpc"
    insecure: bool = True
    headers: Dict[str, str] = field(default_factory=dict)
    trace_sampler: str = "parentbased_always_on"
    sample_ratio: float = 1.0
    max_queue_size: int = 2048
    max_export_batch_size: int = 512
    schedule_delay_ms: int = 5000
    export_timeout_ms: int = 5000
    shutdown_timeout_ms: int = 5000
    resource_attributes: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_runtime(cls) -> "TracingSettings":
        try:
            from unilabos.config.config import OTelConfig
        except (ImportError, AttributeError):
            OTelConfig = None  # type: ignore[assignment,misc]

        def configured(name: str, default: Any) -> Any:
            return getattr(OTelConfig, name, default) if OTelConfig is not None else default

        enabled = _env_bool(
            "UNILABOS_OTEL_ENABLED", bool(configured("enabled", False))
        )
        endpoint = (
            os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or str(configured("endpoint", ""))
        ).strip()
        logs_endpoint = (
            os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
            or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or str(configured("logs_endpoint", ""))
            or endpoint
        ).strip()
        headers_raw = (
            os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS")
            or os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
            or str(configured("headers", ""))
        )
        attrs_raw = os.environ.get(
            "OTEL_RESOURCE_ATTRIBUTES", str(configured("resource_attributes", ""))
        )
        insecure = _env_bool(
            "OTEL_EXPORTER_OTLP_TRACES_INSECURE",
            _env_bool(
                "OTEL_EXPORTER_OTLP_INSECURE",
                bool(configured("insecure", True)),
            ),
        )
        protocol = (
            os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
            or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
            or str(configured("protocol", "grpc"))
        ).strip().lower()
        logs_protocol = (
            os.environ.get("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL")
            or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
            or str(configured("logs_protocol", "")).strip()
            or protocol
        ).strip().lower()
        return cls(
            enabled=enabled,
            service_name=(
                os.environ.get("OTEL_SERVICE_NAME")
                or str(configured("service_name", "uni-lab-edge"))
            ).strip(),
            service_namespace=(
                os.environ.get("OTEL_SERVICE_NAMESPACE")
                or str(configured("service_namespace", "unilab"))
            ).strip(),
            service_version=(
                os.environ.get("OTEL_SERVICE_VERSION")
                or str(configured("service_version", "0.11.3"))
            ).strip(),
            deployment_environment=(
                os.environ.get("OTEL_DEPLOYMENT_ENVIRONMENT")
                or str(configured("deployment_environment", ""))
            ).strip(),
            endpoint=endpoint,
            protocol=protocol,
            logs_enabled=(
                _env_bool(
                    "UNILABOS_OTEL_LOGS_ENABLED",
                    bool(configured("logs_enabled", enabled)),
                )
                and os.environ.get("OTEL_LOGS_EXPORTER", "otlp").strip().lower()
                != "none"
            ),
            logs_endpoint=logs_endpoint,
            logs_protocol=logs_protocol,
            insecure=insecure,
            headers=_parse_headers(headers_raw),
            trace_sampler=(
                os.environ.get("OTEL_TRACES_SAMPLER")
                or str(configured("trace_sampler", "parentbased_always_on"))
            ).strip().lower(),
            sample_ratio=_safe_float(
                os.environ.get(
                    "OTEL_TRACES_SAMPLER_ARG", configured("sample_ratio", 1.0)
                ),
                1.0,
                0.0,
                1.0,
            ),
            max_queue_size=_safe_int(
                os.environ.get(
                    "OTEL_BSP_MAX_QUEUE_SIZE",
                    configured("max_queue_size", 2048),
                ),
                2048,
            ),
            max_export_batch_size=_safe_int(
                os.environ.get(
                    "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
                    configured("max_export_batch_size", 512),
                ),
                512,
            ),
            schedule_delay_ms=_safe_int(
                os.environ.get(
                    "OTEL_BSP_SCHEDULE_DELAY",
                    configured("schedule_delay_ms", 5000),
                ),
                5000,
            ),
            export_timeout_ms=_safe_int(
                os.environ.get(
                    "OTEL_BSP_EXPORT_TIMEOUT",
                    configured("export_timeout_ms", 5000),
                ),
                5000,
            ),
            shutdown_timeout_ms=_safe_int(
                configured("shutdown_timeout_ms", 5000), 5000
            ),
            resource_attributes=_parse_resource_attributes(attrs_raw),
        )


class _NullSpan:
    def add_event(self, _name: str, _attributes: Optional[Mapping[str, Any]] = None) -> None:
        return

    def end(self) -> None:
        return


_NULL_SPAN = _NullSpan()


class _OtelExporterNoiseFilter(logging.Filter):
    """阻止 exporter 自身错误重新进入 OTLP 日志管线形成递归。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(("opentelemetry", "grpc"))


class _SanitizingOtelLogHandler(logging.Handler):
    """仅向 OTLP 副本写入脱敏后的日志，不修改其他本地 handler 的记录。"""

    def __init__(self, target: logging.Handler):
        super().__init__(logging.NOTSET)
        self.target = target
        self.addFilter(_OtelExporterNoiseFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sanitized = copy.copy(record)
            sanitized.msg = _sanitize_text(record.getMessage(), 4096)
            sanitized.args = ()
            if record.exc_info:
                sanitized.exc_text = _sanitize_text(
                    "".join(traceback.format_exception(*record.exc_info)), 8192
                )
                sanitized.exc_info = None
            if record.stack_info:
                sanitized.stack_info = _sanitize_text(record.stack_info, 8192)
            standard_fields = logging.makeLogRecord({}).__dict__
            for key, value in tuple(sanitized.__dict__.items()):
                if key in standard_fields:
                    continue
                if _SENSITIVE_KEY.search(key):
                    sanitized.__dict__[key] = "<redacted>"
                elif isinstance(value, str):
                    sanitized.__dict__[key] = _sanitize_text(value, 1024)
                elif value is not None and not isinstance(value, (bool, int, float)):
                    sanitized.__dict__[key] = _sanitize_text(value, 1024)
            self.target.handle(sanitized)
        except Exception:
            # 日志导出必须 fail-open，不能让遥测异常回流到业务 logger 调用。
            return


def _attach_otel_log_handler(
    target_logger: logging.Logger,
    handler: logging.Handler,
) -> logging.Handler:
    """把 OTLP handler 挂到目标 logger，并过滤 exporter/gRPC 内部日志。"""

    sanitized_handler = _SanitizingOtelLogHandler(handler)
    target_logger.addHandler(sanitized_handler)
    return sanitized_handler


def attach_active_otel_log_handler(target_logger: logging.Logger) -> None:
    """让运行期创建的非传播 logger 复用当前 OTLP 日志出口。"""

    handler = _ACTIVE_OTEL_LOG_HANDLER
    if handler is None or handler in target_logger.handlers:
        return
    target_logger.addHandler(handler)
    _ACTIVE_OTEL_LOG_TARGETS.add(target_logger)


def _activate_otel_log_handler(
    handler: logging.Handler,
    root_logger: logging.Logger,
) -> None:
    global _ACTIVE_OTEL_LOG_HANDLER

    setattr(handler, "_unilabos_otel_handler", True)
    _ACTIVE_OTEL_LOG_HANDLER = handler
    _ACTIVE_OTEL_LOG_TARGETS.add(root_logger)
    comm_logger = logging.getLogger("unilabos.comm")
    if not comm_logger.propagate:
        attach_active_otel_log_handler(comm_logger)


def _deactivate_otel_log_handler(handler: logging.Handler) -> None:
    global _ACTIVE_OTEL_LOG_HANDLER

    if _ACTIVE_OTEL_LOG_HANDLER is not handler:
        return
    for target_logger in tuple(_ACTIVE_OTEL_LOG_TARGETS):
        if handler in target_logger.handlers:
            target_logger.removeHandler(handler)
    _ACTIVE_OTEL_LOG_TARGETS.clear()
    _ACTIVE_OTEL_LOG_HANDLER = None


def _otlp_http_signal_endpoint(endpoint: str, signal: str) -> str:
    """把 OTLP/HTTP collector 根地址规范为具体 signal endpoint。"""

    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if not path.endswith(f"/v1/{signal}"):
        path = f"{path}/v1/{signal}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class _OpenTelemetryBackend:
    """延迟导入 OTel SDK，避免默认路径产生硬依赖。"""

    def __init__(self, settings: TracingSettings):
        from opentelemetry import context, propagate, trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import (
            ALWAYS_OFF,
            ALWAYS_ON,
            ParentBased,
            TraceIdRatioBased,
        )
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        self.context_api = context
        self.propagate_api = propagate
        self.trace_api = trace
        self.settings = settings

        if settings.protocol == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            span_exporter = OTLPSpanExporter(
                endpoint=_otlp_http_signal_endpoint(settings.endpoint, "traces"),
                headers=settings.headers,
                timeout=settings.export_timeout_ms / 1000.0,
            )
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            span_exporter = OTLPSpanExporter(
                endpoint=settings.endpoint,
                insecure=settings.insecure,
                headers=settings.headers,
                timeout=settings.export_timeout_ms / 1000.0,
            )
        attributes: Dict[str, Any] = dict(settings.resource_attributes)
        attributes.update({
            "service.name": settings.service_name,
            "service.namespace": settings.service_namespace,
            "service.version": settings.service_version,
        })
        if settings.deployment_environment:
            attributes["deployment.environment.name"] = settings.deployment_environment
            attributes["deployment.environment"] = settings.deployment_environment
        resource = Resource.create(attributes)
        ratio_sampler = TraceIdRatioBased(settings.sample_ratio)
        samplers = {
            "always_on": ALWAYS_ON,
            "always_off": ALWAYS_OFF,
            "traceidratio": ratio_sampler,
            "parentbased_always_on": ParentBased(ALWAYS_ON),
            "parentbased_always_off": ParentBased(ALWAYS_OFF),
            "parentbased_traceidratio": ParentBased(ratio_sampler),
        }
        sampler = samplers.get(settings.trace_sampler)
        if sampler is None:
            logger.warning(
                "[Tracing] unsupported OTEL_TRACES_SAMPLER=%s; "
                "using parentbased_always_on",
                _sanitize_text(settings.trace_sampler, 64),
            )
            sampler = ParentBased(ALWAYS_ON)
        provider = TracerProvider(
            resource=resource,
            sampler=sampler,
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                span_exporter,
                max_queue_size=settings.max_queue_size,
                schedule_delay_millis=settings.schedule_delay_ms,
                max_export_batch_size=min(
                    settings.max_export_batch_size, settings.max_queue_size
                ),
                export_timeout_millis=settings.export_timeout_ms,
            )
        )
        self.provider = provider
        self.logger_provider: Any = None
        self.log_handler: Optional[logging.Handler] = None
        try:
            if settings.logs_enabled and settings.logs_endpoint:
                if settings.logs_protocol == "http/protobuf":
                    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                        OTLPLogExporter,
                    )

                    log_exporter = OTLPLogExporter(
                        endpoint=_otlp_http_signal_endpoint(
                            settings.logs_endpoint, "logs"
                        ),
                        headers=settings.headers,
                        timeout=settings.export_timeout_ms / 1000.0,
                    )
                else:
                    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
                        OTLPLogExporter,
                    )

                    log_exporter = OTLPLogExporter(
                        endpoint=settings.logs_endpoint,
                        insecure=settings.insecure,
                        headers=settings.headers,
                        timeout=settings.export_timeout_ms / 1000.0,
                    )
                logger_provider = LoggerProvider(resource=resource)
                logger_provider.add_log_record_processor(
                    BatchLogRecordProcessor(
                        log_exporter,
                        max_queue_size=settings.max_queue_size,
                        schedule_delay_millis=settings.schedule_delay_ms,
                        max_export_batch_size=min(
                            settings.max_export_batch_size,
                            settings.max_queue_size,
                        ),
                        export_timeout_millis=settings.export_timeout_ms,
                    )
                )
                self.logger_provider = logger_provider
                self.log_handler = _attach_otel_log_handler(
                    logging.getLogger(),
                    LoggingHandler(
                        level=logging.NOTSET,
                        logger_provider=logger_provider,
                    ),
                )
                _activate_otel_log_handler(
                    self.log_handler,
                    logging.getLogger(),
                )
        except Exception:
            if self.log_handler is not None:
                _deactivate_otel_log_handler(self.log_handler)
            if self.logger_provider is not None:
                self.logger_provider.shutdown()
            provider.shutdown()
            raise
        trace.set_tracer_provider(provider)
        # 云端统一只持久化/传播 W3C traceparent + tracestate。不要透传 baggage，
        # 防止上游把高基数业务参数或敏感数据带入 Edge 消息与 outbox。
        propagate.set_global_textmap(TraceContextTextMapPropagator())
        self.tracer = provider.get_tracer(
            INSTRUMENTATION_NAME, settings.service_version
        )

    def start_span(
        self,
        name: str,
        *,
        parent_context: Any,
        kind: str,
        attributes: Mapping[str, Any],
    ) -> tuple[Any, Any]:
        kinds = self.trace_api.SpanKind
        span_kind = {
            "client": kinds.CLIENT,
            "consumer": kinds.CONSUMER,
            "internal": kinds.INTERNAL,
            "producer": kinds.PRODUCER,
            "server": kinds.SERVER,
        }.get(kind, kinds.INTERNAL)
        span = self.tracer.start_span(
            name,
            context=parent_context,
            kind=span_kind,
            attributes=dict(attributes),
        )
        return span, self.trace_api.set_span_in_context(span, parent_context)

    def current_context(self) -> Any:
        return self.context_api.get_current()

    def attach(self, context_value: Any) -> Any:
        return self.context_api.attach(context_value)

    def detach(self, token: Any) -> None:
        self.context_api.detach(token)

    def extract(self, carrier: Mapping[str, str]) -> Any:
        return self.propagate_api.extract(carrier=dict(carrier))

    def inject(
        self, carrier: MutableMapping[str, str], context_value: Any = None
    ) -> None:
        self.propagate_api.inject(carrier=carrier, context=context_value)

    def current_span(self, context_value: Any = None) -> Any:
        return self.trace_api.get_current_span(context_value)

    def trace_ids(self, context_value: Any = None) -> tuple[str, str]:
        span = self.current_span(context_value)
        span_context = span.get_span_context()
        if not span_context.is_valid:
            return "", ""
        return (
            format(span_context.trace_id, "032x"),
            format(span_context.span_id, "016x"),
        )

    def record_exception(self, span: Any, exc: BaseException) -> None:
        from opentelemetry.trace import Status, StatusCode

        message = _sanitize_text(exc, 2048)
        stack = _sanitize_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            16384,
        )
        span.add_event(
            "exception",
            {
                "exception.type": type(exc).__name__,
                "exception.message": message,
                "exception.stacktrace": stack,
                "exception.escaped": False,
            },
        )
        span.set_status(Status(StatusCode.ERROR, message))

    def set_error(self, span: Any, description: str) -> None:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(
            Status(StatusCode.ERROR, _sanitize_text(description, 2048))
        )

    def shutdown(self) -> None:
        if self.log_handler is not None:
            _deactivate_otel_log_handler(self.log_handler)
            self.log_handler = None
        try:
            if self.logger_provider is not None:
                self.logger_provider.shutdown()
        finally:
            self.provider.shutdown()


_backend: Any = None
_backend_lock = threading.Lock()
_shutdown_registered = False
_shutdown_started = False
_initialization_attempted = False
_settings: Optional[TracingSettings] = None


def initialize_tracing(
    settings: Optional[TracingSettings] = None,
) -> bool:
    """按显式配置初始化 OTLP；任何失败都降级为 no-op。"""

    global _backend, _settings, _shutdown_registered, _initialization_attempted
    with _backend_lock:
        if _backend is not None:
            return True
        if _initialization_attempted:
            return False
        _initialization_attempted = True
        resolved = settings or TracingSettings.from_runtime()
        _settings = resolved
        if not resolved.enabled:
            return False
        if not resolved.endpoint:
            logger.warning(
                "[Tracing] 已启用但未配置 OTLP endpoint，保持 no-op"
            )
            return False
        try:
            _backend = _OpenTelemetryBackend(resolved)
        except Exception as exc:  # noqa: BLE001 - 观测初始化必须 fail-open
            logger.warning(
                "[Tracing] OpenTelemetry 初始化失败，保持 no-op: %s",
                _sanitize_text(exc, 512),
            )
            _backend = None
            return False
        if not _shutdown_registered:
            atexit.register(shutdown_tracing)
            _shutdown_registered = True
        logger.info(
            "[Tracing] OTLP tracing enabled service.name=%s",
            resolved.service_name,
        )
        return True


def shutdown_tracing(timeout_ms: Optional[int] = None) -> bool:
    """有界 flush/shutdown；超时或 exporter 故障都不阻断进程退出。"""

    global _backend, _shutdown_started
    with _backend_lock:
        if _backend is None or _shutdown_started:
            return True
        backend = _backend
        _backend = None
        _shutdown_started = True
        timeout = (
            timeout_ms
            if timeout_ms is not None
            else (_settings.shutdown_timeout_ms if _settings else 5000)
        )

    done = threading.Event()

    def _shutdown() -> None:
        try:
            backend.shutdown()
        except Exception as exc:  # noqa: BLE001 - exporter 关停必须 fail-open
            logger.warning(
                "[Tracing] OpenTelemetry shutdown failed: %s",
                _sanitize_text(exc, 512),
            )
        finally:
            done.set()

    thread = threading.Thread(
        target=_shutdown, name="otel-shutdown", daemon=True
    )
    thread.start()
    finished = done.wait(max(0, timeout) / 1000.0)
    if not finished:
        logger.warning("[Tracing] OpenTelemetry shutdown timed out")
    return finished


def capture_context() -> Any:
    return _backend.current_context() if _backend is not None else None


@contextlib.contextmanager
def use_context(context_value: Any) -> Iterator[None]:
    if _backend is None or context_value is None:
        yield
        return
    token = _backend.attach(context_value)
    try:
        yield
    finally:
        _backend.detach(token)


def await_with_context(context_value: Any, awaitable: Any) -> Any:
    """逐次恢复异步上下文，兼容会跨 Context 推进协程的 rclpy Task。"""

    python_context = contextvars.copy_context()

    @types.coroutine
    def _runner():
        iterator = awaitable.__await__()
        value = None
        pending_error: Optional[BaseException] = None
        while True:
            try:
                def _advance():
                    nonlocal pending_error
                    with use_context(context_value):
                        if pending_error is None:
                            return iterator.send(value)
                        error_value = pending_error
                        pending_error = None
                        return iterator.throw(
                            type(error_value),
                            error_value,
                            error_value.__traceback__,
                        )

                yielded = python_context.run(_advance)
            except StopIteration as stopped:
                return stopped.value
            try:
                value = yield yielded
            except BaseException as exc:  # 由外层 Task 注入取消或等待异常
                pending_error = exc
                value = None

    return _runner()


def extract_trace_context(carrier: Optional[Mapping[str, Any]]) -> Any:
    if _backend is None or not carrier:
        return None
    source: Dict[str, str] = {}
    nested = carrier.get("trace_context")
    if isinstance(nested, Mapping):
        for key in (TRACEPARENT, TRACESTATE):
            if nested.get(key):
                source[key] = str(nested[key])
    for key in (TRACEPARENT, TRACESTATE):
        if carrier.get(key):
            source[key] = str(carrier[key])
    if not source:
        return None
    try:
        return _backend.extract(source)
    except Exception:  # noqa: BLE001 - 非法远端上下文按新根处理
        return None


def inject_trace_context(
    carrier: MutableMapping[str, Any], context_value: Any = None
) -> MutableMapping[str, Any]:
    if _backend is None:
        return carrier
    text_carrier: Dict[str, str] = {}
    try:
        _backend.inject(text_carrier, context_value)
        for key in (TRACEPARENT, TRACESTATE):
            if text_carrier.get(key):
                carrier[key] = text_carrier[key]
        trace_id, span_id = _backend.trace_ids(context_value)
        if trace_id:
            carrier[TRACE_ID] = trace_id
        if span_id:
            carrier[SPAN_ID] = span_id
    except Exception:  # noqa: BLE001 - 传播失败不得影响业务消息
        pass
    return carrier


def current_trace_ids(context_value: Any = None) -> tuple[str, str]:
    if _backend is None:
        return "", ""
    try:
        return _backend.trace_ids(context_value)
    except Exception:  # noqa: BLE001
        return "", ""


def current_span() -> Any:
    if _backend is None:
        return _NULL_SPAN
    try:
        return _backend.current_span()
    except Exception:  # noqa: BLE001
        return _NULL_SPAN


def add_event(
    name: str,
    attributes: Optional[Mapping[str, Any]] = None,
    *,
    span: Any = None,
) -> None:
    target = span or current_span()
    try:
        target.add_event(name, _safe_attributes(attributes))
    except Exception:  # noqa: BLE001 - 埋点永不影响业务
        pass


def record_exception(exc: BaseException, *, span: Any = None) -> None:
    if _backend is None:
        return
    try:
        _backend.record_exception(span or current_span(), exc)
    except Exception:  # noqa: BLE001
        pass


def set_error(description: str, *, span: Any = None) -> None:
    if _backend is None:
        return
    try:
        _backend.set_error(span or current_span(), description)
    except Exception:  # noqa: BLE001
        pass


class SpanScope:
    def __init__(
        self,
        name: str,
        *,
        attributes: Optional[Mapping[str, Any]] = None,
        kind: str = "internal",
        parent_context: Any = None,
    ):
        self.name = name
        self.attributes = _safe_attributes(attributes)
        self.kind = kind
        self.parent_context = parent_context
        self.span: Any = _NULL_SPAN
        self.context: Any = None
        self._token: Any = None

    def __enter__(self) -> Any:
        if _backend is None:
            return self.span
        try:
            self.span, self.context = _backend.start_span(
                self.name,
                parent_context=self.parent_context,
                kind=self.kind,
                attributes=self.attributes,
            )
            self._token = _backend.attach(self.context)
        except Exception:  # noqa: BLE001 - 埋点永不影响业务
            self.span = _NULL_SPAN
            self.context = None
        return self.span

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc is not None:
            record_exception(exc, span=self.span)
        if _backend is not None and self._token is not None:
            try:
                _backend.detach(self._token)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.span.end()
        except Exception:  # noqa: BLE001
            pass
        return False


def span(
    name: str,
    *,
    attributes: Optional[Mapping[str, Any]] = None,
    kind: str = "internal",
    parent_context: Any = None,
) -> SpanScope:
    return SpanScope(
        name,
        attributes=attributes,
        kind=kind,
        parent_context=parent_context,
    )


class DetachedSpan:
    """可跨调度轮次保存的长生命周期 span（workflow/job）。"""

    def __init__(
        self,
        name: str,
        attributes: Mapping[str, Any],
        parent_context: Any,
        kind: str = "internal",
    ):
        self.span: Any = _NULL_SPAN
        self.context: Any = None
        self._ended = False
        if _backend is not None:
            try:
                self.span, self.context = _backend.start_span(
                    name,
                    parent_context=parent_context,
                    kind=kind,
                    attributes=_safe_attributes(attributes),
                )
            except Exception:  # noqa: BLE001
                self.span = _NULL_SPAN

    @contextlib.contextmanager
    def activate(self) -> Iterator[Any]:
        with use_context(self.context):
            yield self.span

    def event(
        self, name: str, attributes: Optional[Mapping[str, Any]] = None
    ) -> None:
        add_event(name, attributes, span=self.span)

    def fail(self, exc: BaseException) -> None:
        record_exception(exc, span=self.span)

    def error(self, description: str) -> None:
        set_error(description, span=self.span)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            self.span.end()
        except Exception:  # noqa: BLE001
            pass


def start_detached_span(
    name: str,
    *,
    attributes: Optional[Mapping[str, Any]] = None,
    parent_context: Any = None,
    kind: str = "internal",
) -> DetachedSpan:
    return DetachedSpan(
        name,
        _safe_attributes(attributes),
        parent_context if parent_context is not None else capture_context(),
        kind,
    )


def run_with_context(
    context_value: Any, function: Any, *args: Any, **kwargs: Any
) -> Any:
    with use_context(context_value):
        return function(*args, **kwargs)


def submit_with_context(executor: Any, function: Any, *args: Any, **kwargs: Any):
    context_value = capture_context()
    python_context = contextvars.copy_context()
    return executor.submit(
        python_context.run,
        run_with_context,
        context_value,
        function,
        *args,
        **kwargs,
    )


def wrap_with_current_context(function: Any):
    context_value = capture_context()
    python_context = contextvars.copy_context()

    def _wrapped(*args: Any, **kwargs: Any):
        return python_context.copy().run(
            run_with_context,
            context_value,
            function,
            *args,
            **kwargs,
        )

    return _wrapped


def install_http_tracing(app: Any) -> None:
    """给 FastAPI/Starlette app 安装低基数 HTTP server span。"""

    if getattr(app.state, "_unilabos_http_tracing_installed", False):
        return
    app.state._unilabos_http_tracing_installed = True

    @app.middleware("http")
    async def _trace_http(request: Any, call_next: Any):
        initialize_tracing()
        if request.url.path in {"/health", "/health/live", "/health/ready"}:
            return await call_next(request)
        parent = extract_trace_context(dict(request.headers))
        route = request.scope.get("route")
        route_path = getattr(route, "path", "") or ""
        attributes = {
            "http.request.method": request.method,
            "http.route": route_path,
            "server.address": request.url.hostname or "",
        }
        with span(
            f"HTTP {request.method}",
            attributes=attributes,
            kind="server",
            parent_context=parent,
        ) as http_span:
            response = await call_next(request)
            try:
                http_span.set_attribute(
                    "http.response.status_code", response.status_code
                )
                route_after = request.scope.get("route")
                route_after_path = getattr(route_after, "path", "") or ""
                if route_after_path:
                    if hasattr(http_span, "update_name"):
                        http_span.update_name(
                            f"{request.method} {route_after_path}"
                        )
                    http_span.set_attribute("http.route", route_after_path)
                if response.status_code >= 500:
                    set_error(
                        f"HTTP {response.status_code}", span=http_span
                    )
            except Exception:  # noqa: BLE001
                pass
            trace_id, span_id = current_trace_ids()
            if trace_id:
                response.headers.setdefault("trace_id", trace_id)
            if span_id:
                response.headers.setdefault("span_id", span_id)
            return response


def _set_backend_for_test(backend: Any) -> None:
    """仅供离线单测注入 recording backend。"""

    global _backend, _shutdown_started, _initialization_attempted
    with _backend_lock:
        _backend = backend
        _shutdown_started = False
        _initialization_attempted = True


def _reset_for_test() -> None:
    global _backend, _settings, _shutdown_started, _initialization_attempted
    with _backend_lock:
        _backend = None
        _settings = None
        _shutdown_started = False
        _initialization_attempted = False


__all__ = [
    "DetachedSpan",
    "SPAN_ID",
    "TRACEPARENT",
    "TRACESTATE",
    "TRACE_ID",
    "TracingSettings",
    "add_event",
    "capture_context",
    "await_with_context",
    "current_span",
    "current_trace_ids",
    "extract_trace_context",
    "initialize_tracing",
    "inject_trace_context",
    "install_http_tracing",
    "record_exception",
    "run_with_context",
    "set_error",
    "shutdown_tracing",
    "span",
    "start_detached_span",
    "submit_with_context",
    "use_context",
    "wrap_with_current_context",
]
