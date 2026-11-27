"""Authority-aware Workflow Domain client used by CLI and Agent adapters.

The client discovers the active Domain Authority from the per-workspace
Workspace Host.  It never owns a process and never reads a runtime database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from unilabos.workspace_host.client import ensure_workspace_host
from unilabos.workspace_host.model import WorkspaceHostError

from .envelope import EnvelopeError, unwrap_envelope

DOMAIN_CLIENT_SCHEMA = "unilab-domain-client/v1"
TERMINAL_TASK_STATUSES = frozenset(
    {"succeeded", "success", "failed", "canceled", "timeout"}
)


class DomainClientError(RuntimeError):
    """Stable public failure returned by the Domain client SDK."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: object = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class DomainSource:
    """Immutable identity of one selected local or Backend Authority."""

    authority: str
    endpoint: str
    workspace_path: str
    host_revision: int
    generation: str | None
    edge_generation: str | None

    @property
    def api_base_url(self) -> str:
        return _api_base_url(self.endpoint)

    @property
    def source_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.authority}\0{self.api_base_url}".encode("utf-8")
        ).hexdigest()
        return f"{self.authority}:sha256:{digest}"

    def as_dict(self) -> dict[str, object]:
        return {
            "sourceId": self.source_id,
            "authority": self.authority,
            "apiBaseUrl": self.api_base_url,
            "workspacePath": self.workspace_path,
            "hostRevision": self.host_revision,
            "generation": self.generation,
            "edgeGeneration": self.edge_generation,
        }

    @classmethod
    def discover(cls, workspace: str | Path) -> "DomainSource":
        """Resolve the active Authority through the Workspace Host snapshot."""

        try:
            snapshot = ensure_workspace_host(workspace).snapshot()
        except WorkspaceHostError as error:
            raise DomainClientError(
                error.code,
                error.message,
                details=error.details,
            ) from error
        configuration = snapshot.get("configuration")
        components = snapshot.get("components")
        if not isinstance(configuration, Mapping) or not isinstance(
            components, Mapping
        ):
            raise DomainClientError(
                "host_state_invalid", "Workspace Host 缺少 Authority 状态"
            )
        authority = str(configuration.get("domainMode") or "local")
        backend = components.get("backend")
        edge = components.get("edge")
        backend = backend if isinstance(backend, Mapping) else {}
        edge = edge if isinstance(edge, Mapping) else {}
        if authority == "local":
            endpoint = _optional_text(backend.get("address"))
            if backend.get("phase") != "ready" or endpoint is None:
                raise DomainClientError(
                    "local_backend_not_ready",
                    "Local Domain Backend 尚未就绪；请先运行 unilab workspace start --component backend",
                    details={"component": dict(backend)},
                )
            generation = _optional_text(backend.get("generation"))
        elif authority == "backend":
            endpoint = _optional_text(configuration.get("backendUrl"))
            if endpoint is None:
                raise DomainClientError(
                    "backend_url_missing", "Backend Authority 缺少 backendUrl"
                )
            # Backend 的服务 generation 不由本地 Host 伪造；当前设备连接 generation
            # 仍随结果返回，供 Agent 判断运行事实是否跨越 Edge 重启。
            generation = None
        else:
            raise DomainClientError(
                "authority_invalid", f"未知 Domain Authority：{authority}"
            )
        return cls(
            authority=authority,
            endpoint=endpoint,
            workspace_path=str(snapshot.get("workspacePath") or Path(workspace).resolve()),
            host_revision=int(snapshot.get("revision") or 0),
            generation=generation,
            edge_generation=_optional_text(edge.get("generation")),
        )


class DomainBackendClient:
    """Deep client module for the shared ``/api/v1`` Workflow contract."""

    def __init__(
        self,
        source: DomainSource,
        *,
        ak: str = "",
        sk: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.source = source
        headers = {"Accept": "application/json"}
        if ak and sk:
            secret = base64.b64encode(f"{ak}:{sk}".encode("utf-8")).decode(
                "ascii"
            )
            headers["Authorization"] = f"Lab {secret}"
        self._client = httpx.Client(
            base_url=source.api_base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def discover(
        cls,
        workspace: str | Path,
        *,
        ak: str = "",
        sk: str = "",
        timeout: float = 30.0,
    ) -> "DomainBackendClient":
        return cls(
            DomainSource.discover(workspace),
            ak=ak,
            sk=sk,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DomainBackendClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def list_workflows(
        self, *, page: int = 1, page_size: int = 100, name: str = ""
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "workflows",
            params={"page": page, "page_size": page_size, "keyword": name},
        )

    def inspect_workflow(self, workflow_uuid: str) -> dict[str, Any]:
        definition = self._request("GET", f"workflows/{workflow_uuid}")
        graph = self._request("GET", f"workflows/{workflow_uuid}/graph")
        authoring: object = None
        if self.source.authority == "local":
            authoring = self._request("GET", f"workflows/{workflow_uuid}/authoring")
        return self.result(
            {"workflow": definition, "graph": graph, "authoring": authoring},
            revision=_workflow_revision(graph) or _workflow_revision(definition),
        )

    def create_task(
        self,
        workflow_uuid: str,
        *,
        run_mode: str = "normal",
        target_node_uuid: str | None = None,
        input_value: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        operation_id = operation_id or str(uuid.uuid4())
        task = self._request(
            "POST",
            "workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": run_mode,
                "target_node_uuid": target_node_uuid,
                "input": dict(input_value or {}),
                "description": "由 unilab workflow CLI 启动",
                "meta_data": {
                    "source": "unilab-domain-client",
                    "operation_id": operation_id,
                },
            },
        )
        return self.result(
            task,
            operation_id=operation_id,
            task_uuid=_required_identity(task, "uuid"),
            revision=_task_revision(task),
        )

    def create_debug_task(
        self,
        workflow_uuid: str,
        *,
        start_node_uuid: str,
        breakpoint_node_uuids: Sequence[str] = (),
        input_value: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        operation_id = operation_id or str(uuid.uuid4())
        task = self._request(
            "POST",
            "debug/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "start_node_uuids": [start_node_uuid],
                "breakpoint_node_uuids": list(breakpoint_node_uuids),
                "input": dict(input_value or {}),
                "description": "由 unilab workflow CLI 启动调试",
                "meta_data": {
                    "source": "unilab-domain-client",
                    "operation_id": operation_id,
                },
            },
        )
        return self.result(
            task,
            operation_id=operation_id,
            task_uuid=_required_identity(task, "uuid"),
            revision=_task_revision(task),
        )

    def inspect_task(
        self,
        task_uuid: str,
        *,
        include_events: bool = True,
        event_limit: int = 100,
    ) -> dict[str, Any]:
        task = self._request("GET", f"workflow-tasks/{task_uuid}")
        jobs = self._request("GET", f"workflow-tasks/{task_uuid}/jobs")
        events: object = None
        if include_events:
            try:
                events = self.task_events(task_uuid, limit=event_limit)
            except DomainClientError as error:
                if error.code != "endpoint_not_supported":
                    raise
        return self.result(
            {"task": task, "jobs": jobs, "events": events},
            task_uuid=task_uuid,
            revision=_task_revision(task),
        )

    def inspect_job(self, job_uuid: str) -> dict[str, Any]:
        job = self._request("GET", f"workflow-node-jobs/{job_uuid}")
        return self.result(job, task_uuid=_optional_text(job.get("workflow_task_uuid")))

    def task_events(
        self,
        task_uuid: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            return self._request(
                "GET",
                f"workflow-tasks/{task_uuid}/events",
                params={"after_sequence": after, "limit": limit},
            )
        except DomainClientError as error:
            if error.code == "http_not_found":
                raise DomainClientError(
                    "endpoint_not_supported",
                    "当前 Authority 尚未提供任务运行事件查询",
                ) from error
            raise

    def command_task(
        self,
        task_uuid: str,
        command_type: str,
        *,
        target_node_uuid: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        normalized = "resume" if command_type == "continue" else command_type
        key = idempotency_key or f"cli-{normalized}-{uuid.uuid4()}"
        command = self._request(
            "POST",
            f"workflow-tasks/{task_uuid}/commands",
            json={
                "type": normalized,
                "target_node_uuid": target_node_uuid,
                "idempotency_key": key,
                "description": "由 unilab workflow CLI 提交",
                "meta_data": {"source": "unilab-domain-client"},
            },
        )
        return self.result(
            command,
            operation_id=_optional_text(command.get("uuid")) or key,
            task_uuid=task_uuid,
            revision=_task_revision(command),
        )

    def command_debug_task(
        self,
        task_uuid: str,
        command_type: str,
        *,
        hold_uuid: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        normalized = "continue" if command_type in {"continue", "resume"} else command_type
        key = idempotency_key or f"cli-debug-{normalized}-{uuid.uuid4()}"
        command = self._request(
            "POST",
            f"debug/workflow-tasks/{task_uuid}/commands",
            json={
                "type": normalized,
                "scope": {"type": "hold", "hold_uuid": hold_uuid},
                "idempotency_key": key,
            },
        )
        return self.result(
            command,
            operation_id=_optional_text(command.get("uuid")) or key,
            task_uuid=task_uuid,
        )

    def get_debug_task(self, task_uuid: str) -> dict[str, Any]:
        debug = self._request("GET", f"debug/workflow-tasks/{task_uuid}")
        task = debug.get("task") if isinstance(debug, Mapping) else None
        return self.result(
            debug,
            task_uuid=task_uuid,
            revision=_task_revision(task if isinstance(task, Mapping) else {}),
        )

    def wait_authoring(
        self,
        workflow_uuid: str,
        *,
        after_revision: int,
        timeout: float = 30.0,
        poll_interval: float = 0.2,
    ) -> dict[str, Any]:
        if self.source.authority != "local":
            raise DomainClientError(
                "authoring_not_active",
                "Backend Authority 下工作区代码不驱动画布；请在 Local Authority 等待 Authoring",
            )
        deadline = time.monotonic() + timeout
        last: Mapping[str, Any] | None = None
        while time.monotonic() <= deadline:
            value = self._request("GET", f"workflows/{workflow_uuid}/authoring")
            if not isinstance(value, Mapping):
                raise DomainClientError("protocol_invalid", "Authoring 响应不是对象")
            last = value
            revision = int(value.get("workflow_revision") or 0)
            draft = value.get("draft")
            diagnostics = (
                draft.get("diagnostics")
                if isinstance(draft, Mapping)
                else None
            )
            if revision > after_revision or diagnostics:
                return self.result(
                    dict(value),
                    revision=revision,
                    extra={"frontendRefresh": "authoring-sse"},
                )
            time.sleep(poll_interval)
        raise DomainClientError(
            "authoring_timeout",
            f"等待 Authoring revision 超时：{workflow_uuid}",
            details=last,
        )

    def watch_task(
        self,
        task_uuid: str,
        *,
        after: int = 0,
        limit: int = 100,
        timeout: float = 300.0,
        poll_interval: float = 0.25,
        max_events: int = 500,
    ) -> Iterator[dict[str, Any]]:
        """Yield a cursor-safe bounded task timeline and stop at terminal state."""

        if after < 0 or limit < 1 or limit > 500 or max_events < 1:
            raise DomainClientError("invalid_input", "watch 游标或上限无效")
        deadline = time.monotonic() + timeout
        cursor = after
        emitted = 0
        event_endpoint_supported = True
        while time.monotonic() <= deadline and emitted < max_events:
            page: Mapping[str, Any] | None = None
            if event_endpoint_supported:
                try:
                    candidate = self.task_events(
                        task_uuid,
                        after=cursor,
                        limit=min(limit, max_events - emitted),
                    )
                    page = candidate if isinstance(candidate, Mapping) else None
                except DomainClientError as error:
                    if error.code != "endpoint_not_supported":
                        raise
                    event_endpoint_supported = False
                    yield from self._watch_task_from_frontend_events(
                        task_uuid,
                        after=cursor,
                        deadline=deadline,
                        max_events=max_events - emitted,
                    )
                    return
            if page is not None:
                items = page.get("items")
                items = items if isinstance(items, list) else []
                for event in items:
                    if not isinstance(event, Mapping):
                        continue
                    sequence = int(event.get("sequence") or 0)
                    if sequence <= cursor:
                        continue
                    cursor = sequence
                    emitted += 1
                    yield self.result(
                        dict(event),
                        task_uuid=task_uuid,
                        cursor=cursor,
                    )
                next_cursor = page.get("next_cursor")
                if isinstance(next_cursor, int) and next_cursor > cursor:
                    cursor = next_cursor
                if bool(page.get("has_more")):
                    continue
            task = self._request("GET", f"workflow-tasks/{task_uuid}")
            status = str(task.get("status") or "") if isinstance(task, Mapping) else ""
            if status in TERMINAL_TASK_STATUSES:
                yield self.result(
                    {"kind": "task_terminal", "task": task},
                    task_uuid=task_uuid,
                    revision=_task_revision(task),
                    cursor=cursor,
                )
                return
            time.sleep(poll_interval)
        if emitted >= max_events:
            return
        raise DomainClientError(
            "watch_timeout",
            f"等待工作流任务终态超时：{task_uuid}",
            details={"cursor": cursor},
        )

    def _watch_task_from_frontend_events(
        self,
        task_uuid: str,
        *,
        after: int,
        deadline: float,
        max_events: int,
    ) -> Iterator[dict[str, Any]]:
        """Hydrate Backend Task facts from durable global invalidations."""

        task = self._request("GET", f"workflow-tasks/{task_uuid}")
        if str(task.get("status") or "") in TERMINAL_TASK_STATUSES:
            yield self.result(
                {"kind": "task_terminal", "task": task},
                task_uuid=task_uuid,
                revision=_task_revision(task),
                cursor=after,
            )
            return
        emitted = 0
        cursor = after
        for event in self._frontend_events(
            after=after,
            deadline=deadline,
            max_events=max(max_events * 20, max_events),
        ):
            cursor = int(event["id"])
            payload = event.get("data")
            related = event.get("event") == "stream.reset" or (
                isinstance(payload, Mapping)
                and str(payload.get("workflow_task_uuid") or payload.get("task_uuid") or "")
                == task_uuid
            )
            if not related:
                continue
            task = self._request("GET", f"workflow-tasks/{task_uuid}")
            jobs = self._request("GET", f"workflow-tasks/{task_uuid}/jobs")
            emitted += 1
            yield self.result(
                {
                    "kind": "task_changed",
                    "event": event,
                    "task": task,
                    "jobs": jobs,
                },
                task_uuid=task_uuid,
                revision=_task_revision(task),
                cursor=cursor,
            )
            if str(task.get("status") or "") in TERMINAL_TASK_STATUSES:
                return
            if emitted >= max_events:
                return
        raise DomainClientError(
            "watch_timeout",
            f"等待工作流任务终态超时：{task_uuid}",
            details={"cursor": cursor},
        )

    def _frontend_events(
        self,
        *,
        after: int,
        deadline: float,
        max_events: int,
    ) -> Iterator[dict[str, Any]]:
        """Read SSE with an exclusive cursor and suppress replay duplicates."""

        cursor = after
        seen = 0
        while time.monotonic() <= deadline and seen < max_events:
            try:
                with self._client.stream(
                    "GET",
                    "events",
                    headers={"Last-Event-ID": str(cursor)},
                ) as response:
                    response.raise_for_status()
                    event_id: int | None = None
                    event_type = "message"
                    data_lines: list[str] = []
                    for line in response.iter_lines():
                        if time.monotonic() > deadline:
                            return
                        if line == "":
                            if event_id is not None and (
                                event_type == "stream.reset" or event_id > cursor
                            ):
                                # 水位帧没有 data；只更新续传位置，不产生业务通知。
                                if not data_lines:
                                    cursor = event_id
                                    event_id = None
                                    event_type = "message"
                                    continue
                                try:
                                    payload = json.loads("\n".join(data_lines) or "null")
                                except json.JSONDecodeError as error:
                                    raise DomainClientError(
                                        "protocol_invalid", "SSE data 不是有效 JSON"
                                    ) from error
                                cursor = event_id
                                seen += 1
                                yield {
                                    "id": event_id,
                                    "event": event_type,
                                    "data": payload,
                                }
                                if seen >= max_events:
                                    return
                            event_id = None
                            event_type = "message"
                            data_lines = []
                            continue
                        if line.startswith(":") or line.startswith("retry:"):
                            continue
                        field, separator, value = line.partition(":")
                        if not separator:
                            continue
                        value = value[1:] if value.startswith(" ") else value
                        if field == "id":
                            try:
                                event_id = int(value)
                            except ValueError as error:
                                raise DomainClientError(
                                    "protocol_invalid", "SSE id 不是非负整数"
                                ) from error
                            if event_id < 0:
                                raise DomainClientError(
                                    "protocol_invalid", "SSE id 不是非负整数"
                                )
                        elif field == "event":
                            event_type = value
                        elif field == "data":
                            data_lines.append(value)
            except (httpx.HTTPError, httpx.StreamError):
                if time.monotonic() > deadline:
                    return
                time.sleep(0.1)
                continue

    def result(
        self,
        data: object,
        *,
        operation_id: str | None = None,
        task_uuid: str | None = None,
        revision: int | None = None,
        cursor: int | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schemaVersion": DOMAIN_CLIENT_SCHEMA,
            "sourceIdentity": self.source.as_dict(),
            "generation": self.source.generation,
            "edgeGeneration": self.source.edge_generation,
            "data": data,
        }
        if operation_id is not None:
            result["operationId"] = operation_id
        if task_uuid is not None:
            result["taskUuid"] = task_uuid
        if revision is not None:
            result["revision"] = revision
        if cursor is not None:
            result["cursor"] = cursor
        if extra:
            result.update(extra)
        return result

    def _request(self, method: str, path: str, **kwargs: object) -> Any:
        try:
            response = self._client.request(method, path.lstrip("/"), **kwargs)
        except httpx.HTTPError as error:
            raise DomainClientError(
                "domain_unreachable",
                f"Domain Authority 不可达：{self.source.api_base_url}",
            ) from error
        if response.status_code == 404:
            raise DomainClientError(
                "http_not_found", f"Domain API 不存在：{method} {path}"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise DomainClientError(
                "http_error",
                f"Domain API HTTP {response.status_code}",
                details=response.text[:4096],
            ) from error
        try:
            return unwrap_envelope(response.json())
        except EnvelopeError as error:
            detail = error.error
            message = (
                str(detail.get("msg") or detail)
                if isinstance(detail, Mapping)
                else str(detail)
            )
            raise DomainClientError(
                f"domain_{error.code}",
                message or "Domain API 拒绝请求",
                details=detail,
            ) from error
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise DomainClientError(
                "protocol_invalid", "Domain API 响应不是有效 Backend envelope"
            ) from error


def _api_base_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DomainClientError("endpoint_invalid", "Domain endpoint 必须是 HTTP(S) URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/v1"):
        path = f"{path}/api/v1" if path else "/api/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _required_identity(value: object, field: str) -> str:
    if isinstance(value, Mapping):
        identity = _optional_text(value.get(field))
        if identity is not None:
            return identity
    raise DomainClientError("protocol_invalid", f"Domain 响应缺少 {field}")


def _workflow_revision(value: object) -> int | None:
    if not isinstance(value, Mapping):
        return None
    workflow = value.get("workflow")
    if isinstance(workflow, Mapping):
        return _integer_or_none(workflow.get("revision"))
    return _integer_or_none(value.get("revision"))


def _task_revision(value: object) -> int | None:
    if not isinstance(value, Mapping):
        return None
    revision = _integer_or_none(value.get("revision"))
    if revision is not None:
        return revision
    snapshot = value.get("workflow_snapshot")
    return _workflow_revision(snapshot)


def _integer_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = [
    "DOMAIN_CLIENT_SCHEMA",
    "TERMINAL_TASK_STATUSES",
    "DomainBackendClient",
    "DomainClientError",
    "DomainSource",
]
