"""当前 Workflow Authority 的 HTTP/WS 客户端。

HTTP 是工作流定义、任务和 Authoring 状态的唯一事实来源。WebSocket
只作为失效通知：收到相关通知后，本客户端始终重新通过 HTTP 拉取任务正文。
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from typing import Any, Optional, Protocol
from uuid import uuid4

from unilabos.client.envelope import EnvelopeError
from unilabos.client.http import HTTPClient, HTTPClientConfig
from unilabos.utils.address import (
    derive_websocket_address,
    normalize_api_address,
)


TERMINAL_WORKFLOW_TASK_STATUSES = frozenset(
    {"succeeded", "failed", "canceled", "timeout"}
)


class WorkflowClientError(RuntimeError):
    """工作流客户端本地校验、通知连接或等待超时错误。"""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class _NoticeSocket(Protocol):
    def recv(self, timeout: Optional[float] = None) -> str | bytes: ...


NoticeConnector = Callable[
    [str, Mapping[str, str], float],
    AbstractContextManager[_NoticeSocket],
]


def normalize_workflow_api_url(base_url: str) -> str:
    """把管理端或 Backend 地址规范化到共享的 ``/api/v1`` 根。"""

    try:
        return normalize_api_address(base_url)
    except ValueError as error:
        raise ValueError("workflow API 地址不能为空") from error


def derive_workflow_websocket_url(
    base_url: str,
    schedule_addr: Optional[str] = None,
) -> str:
    """构造只传递变更通知的 WebSocket 地址。"""

    try:
        return derive_websocket_address(
            base_url,
            websocket_address=schedule_addr,
        )
    except ValueError as error:
        raise ValueError("workflow WebSocket 地址无效") from error


def _default_notice_connector(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> AbstractContextManager[_NoticeSocket]:
    from websockets.sync.client import connect

    return connect(
        url,
        additional_headers=dict(headers),
        open_timeout=min(timeout, 20.0),
        close_timeout=5.0,
    )


class HTTPWorkflowClient:
    """共享 Workflow REST API 的窄客户端。"""

    def __init__(
        self,
        base_url: str,
        *,
        schedule_addr: Optional[str] = None,
        ak: str = "",
        sk: str = "",
        timeout: float = 30.0,
        http_client: Optional[Any] = None,
        notice_connector: Optional[NoticeConnector] = None,
    ) -> None:
        self.base_url = normalize_workflow_api_url(base_url)
        self.notification_url = derive_workflow_websocket_url(
            self.base_url,
            schedule_addr,
        )
        self.timeout = timeout
        self._auth_secret = ""
        if ak and sk:
            token = base64.b64encode(f"{ak}:{sk}".encode("utf-8"))
            self._auth_secret = token.decode("ascii")
        self._http = http_client or HTTPClient(
            HTTPClientConfig(base_url=self.base_url, timeout=timeout),
            get_auth_secret=(lambda: self._auth_secret),
        )
        self._owns_http_client = http_client is None
        self._notice_connector = notice_connector or _default_notice_connector

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> "HTTPWorkflowClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def list_workflows(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        name: str = "",
    ) -> dict[str, Any]:
        return self._http.get(
            "/workflows",
            params={"page": page, "page_size": page_size, "name": name},
        )

    def create_workflow(
        self,
        *,
        name: str,
        tags: list[Any],
        description: Optional[str],
        meta_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._http.post(
            "/workflows",
            json={
                "name": name,
                "tags": tags,
                "description": description,
                "meta_data": meta_data or {},
            },
        )

    def get_workflow(self, workflow_uuid: str) -> dict[str, Any]:
        return self._http.get(f"/workflows/{workflow_uuid}")

    def get_workflow_graph(self, workflow_uuid: str) -> dict[str, Any]:
        return self._http.get(f"/workflows/{workflow_uuid}/graph")

    def save_workflow_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._http.put(
            f"/workflows/{workflow_uuid}/graph",
            json={"revision": revision, "nodes": nodes, "edges": edges},
        )

    def inspect_workflow(self, workflow_uuid: str) -> dict[str, Any]:
        workflow = self.get_workflow(workflow_uuid)
        graph = self.get_workflow_graph(workflow_uuid)
        try:
            authoring: Optional[dict[str, Any]] = self.get_authoring(workflow_uuid)
        except EnvelopeError as error:
            if error.code != 3002:
                raise
            authoring = None
        return {"workflow": workflow, "graph": graph, "authoring": authoring}

    def create_task(
        self,
        workflow_uuid: str,
        *,
        run_mode: str = "normal",
        target_node_uuid: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        operation_id = operation_id or str(uuid4())
        return self._http.post(
            "/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": run_mode,
                "target_node_uuid": target_node_uuid,
                "description": "由 unilab workflow CLI 启动",
                "meta_data": {
                    "source": "unilab-workflow-client",
                    "operation_id": operation_id,
                },
            },
        )

    def get_task(self, task_uuid: str) -> dict[str, Any]:
        return self._http.get(f"/workflow-tasks/{task_uuid}")

    def list_task_jobs(self, task_uuid: str) -> list[dict[str, Any]]:
        return self._http.get(f"/workflow-tasks/{task_uuid}/jobs")

    def inspect_task(self, task_uuid: str) -> dict[str, Any]:
        return {
            "task": self.get_task(task_uuid),
            "jobs": self.list_task_jobs(task_uuid),
        }

    def get_job(self, job_uuid: str) -> dict[str, Any]:
        return self._http.get(f"/workflow-node-jobs/{job_uuid}")

    def get_authoring(self, workflow_uuid: str) -> dict[str, Any]:
        return self._http.get(f"/workflows/{workflow_uuid}/authoring")

    def wait_authoring(
        self,
        workflow_uuid: str,
        *,
        after_revision: int,
        timeout: float = 30.0,
        poll_interval: float = 0.2,
    ) -> dict[str, Any]:
        if after_revision < 0 or timeout < 0 or poll_interval < 0:
            raise WorkflowClientError("invalid_input", "Authoring 等待参数无效")
        deadline = time.monotonic() + timeout
        last: Optional[dict[str, Any]] = None
        while True:
            value = self.get_authoring(workflow_uuid)
            if not isinstance(value, dict):
                raise WorkflowClientError(
                    "protocol_invalid",
                    "Authoring 响应不是对象",
                )
            last = value
            revision = int(value.get("workflow_revision") or 0)
            draft = value.get("draft")
            diagnostics = (
                draft.get("diagnostics") if isinstance(draft, Mapping) else None
            )
            if revision > after_revision or diagnostics:
                return value
            if time.monotonic() >= deadline:
                raise WorkflowClientError(
                    "authoring_timeout",
                    f"等待 Authoring revision 超时：{workflow_uuid}",
                    details=last,
                )
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def watch_task(
        self,
        task_uuid: str,
        *,
        after: int = 0,
        timeout: float = 300.0,
        max_events: int = 500,
    ) -> Iterator[dict[str, Any]]:
        """监听轻通知，并在每次通知后经 HTTP 拉取任务与 Job 正文。"""

        if after < 0 or timeout <= 0 or max_events < 1:
            raise WorkflowClientError("invalid_input", "watch 参数无效")

        cursor = after
        snapshot = self.inspect_task(task_uuid)
        yield {
            "kind": "task_snapshot",
            "cursor": cursor,
            **snapshot,
        }
        if self._is_terminal(snapshot["task"]):
            return

        deadline = time.monotonic() + timeout
        headers = {"Accept": "application/json"}
        if self._auth_secret:
            headers["Authorization"] = f"Lab {self._auth_secret}"

        emitted = 0
        try:
            with self._notice_connector(
                self.notification_url,
                headers,
                timeout,
            ) as websocket:
                # 连接建立后再拉一次，封住“首次 HTTP 快照到 WS 订阅”之间的竞态。
                synchronized = self.inspect_task(task_uuid)
                if synchronized != snapshot:
                    snapshot = synchronized
                    emitted += 1
                    yield {
                        "kind": "task_changed",
                        "cursor": cursor,
                        **snapshot,
                    }
                    if self._is_terminal(snapshot["task"]):
                        return
                while emitted < max_events:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    raw_notice = websocket.recv(timeout=remaining)
                    notice = self._decode_notice(raw_notice)
                    if notice is None or not self._notice_matches_task(
                        notice,
                        task_uuid,
                    ):
                        continue
                    sequence = self._notice_sequence(notice)
                    if sequence is not None:
                        if sequence <= cursor:
                            continue
                        cursor = sequence

                    # WS 内容只触发失效；任务事实固定从 HTTP 重新拉取。
                    snapshot = self.inspect_task(task_uuid)
                    emitted += 1
                    yield {
                        "kind": "task_changed",
                        "cursor": cursor,
                        **snapshot,
                    }
                    if self._is_terminal(snapshot["task"]):
                        return
        except TimeoutError:
            pass
        except WorkflowClientError:
            raise
        except Exception as error:
            raise WorkflowClientError(
                "watch_disconnected",
                f"工作流通知连接中断：{error}",
                details={"task_uuid": task_uuid, "cursor": cursor},
            ) from error

        if emitted >= max_events:
            return
        raise WorkflowClientError(
            "watch_timeout",
            f"等待工作流任务终态超时：{task_uuid}",
            details={"cursor": cursor},
        )

    @staticmethod
    def _is_terminal(task: Mapping[str, Any]) -> bool:
        return str(task.get("status") or "").lower() in TERMINAL_WORKFLOW_TASK_STATUSES

    @staticmethod
    def _decode_notice(raw_notice: str | bytes) -> Optional[dict[str, Any]]:
        try:
            value = json.loads(raw_notice)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _notice_data(notice: Mapping[str, Any]) -> Mapping[str, Any]:
        data = notice.get("data")
        return data if isinstance(data, Mapping) else notice

    @classmethod
    def _notice_matches_task(
        cls,
        notice: Mapping[str, Any],
        task_uuid: str,
    ) -> bool:
        data = cls._notice_data(notice)
        direct_ids = {
            str(data.get(key) or "") for key in ("workflow_task_uuid", "task_uuid")
        }
        if any(direct_ids):
            return task_uuid in direct_ids
        aggregate_type = str(data.get("aggregate_type") or "").lower()
        if aggregate_type in {"workflow_task", "task"}:
            return str(data.get("aggregate_uuid") or "") == task_uuid

        event_type = str(
            data.get("event_type")
            or data.get("change_type")
            or data.get("type")
            or notice.get("action")
            or ""
        ).lower()
        return any(token in event_type for token in ("workflow", "task", "job"))

    @classmethod
    def _notice_sequence(cls, notice: Mapping[str, Any]) -> Optional[int]:
        data = cls._notice_data(notice)
        for key in ("event_sequence", "backend_sequence", "sequence", "id"):
            value = data.get(key)
            if isinstance(value, bool):
                continue
            try:
                sequence = int(value)
            except (TypeError, ValueError):
                continue
            if sequence >= 0:
                return sequence
        return None


__all__ = [
    "HTTPWorkflowClient",
    "TERMINAL_WORKFLOW_TASK_STATUSES",
    "WorkflowClientError",
    "derive_workflow_websocket_url",
    "normalize_workflow_api_url",
]
