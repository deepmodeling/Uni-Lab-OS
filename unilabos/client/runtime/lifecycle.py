"""统一管理地址的生命周期 SDK；查询、构造客户端均不会启动进程。"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from typing import Any, Self

import httpx

from unilabos.client.http import HTTPClient, HTTPClientConfig
from unilabos.protocol.runtime.lifecycle import (
    HealthStatus,
    ReadinessStatus,
    ResetRequest,
    ResetStatus,
    RestartRequest,
    RestartStatus,
    SchedulerDrainStatus,
)
from unilabos.protocol.runtime.workspace_host import DEFAULT_LOG_MAX_BYTES, WorkspaceHostError
from unilabos.utils.address import normalize_api_address

#: V1 ``WorkspaceHostClient.execute`` 的缺省等待预算（秒）。
WORKSPACE_OPERATION_TIMEOUT_S = 630.0


class SchedulerDrainTimeout(TimeoutError):
    """排空超时；保持服务运行和排空闸门，由调用方决定后续恢复。"""

    def __init__(self, status: SchedulerDrainStatus | None) -> None:
        super().__init__("设备作业未在排空预算内收敛；服务保持运行，未自动恢复派发")
        self.status = status


class HTTPLifecycleClient:
    """管理一个 backend 及其主 runtime；复用控制面 token 与地址规范化。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        get_auth_secret: Callable[[], str] | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("HTTP 超时必须为有限正数")
        # 重启/重置在响应丢失后不能盲目重试，调用方应重新查询状态。
        # 管理 API 统一信封；只有 /health、/readiness 两个探针是裸 JSON。
        api_root = normalize_api_address(base_url)
        self._http = HTTPClient(
            HTTPClientConfig(api_root, timeout=timeout, max_retries=0),
            get_auth_secret=get_auth_secret,
        )
        # V1 Workspace Host 路由不带 /api 前缀：同一服务根地址下的 /v1/*
        self._service_root = api_root.removesuffix("/api/v1")
        self._timeout = timeout

    def health(self) -> HealthStatus:
        return HealthStatus.model_validate(self._http.get("/health", envelope=False))

    def readiness(self) -> ReadinessStatus:
        try:
            payload = self._http.get("/readiness", envelope=False)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 503:
                raise
            payload = exc.response.json()
        return ReadinessStatus.model_validate(payload)

    def restart_status(self) -> RestartStatus:
        return RestartStatus.model_validate(self._http.get("/restart"))

    def request_restart(self, request: RestartRequest | None = None) -> RestartStatus:
        body = request or RestartRequest()
        return RestartStatus.model_validate(self._http.post("/restart", json=body.model_dump()))

    def cancel_restart(self) -> RestartStatus:
        return RestartStatus.model_validate(self._http.delete("/restart"))

    def reset_preview(self) -> ResetStatus:
        return ResetStatus.model_validate(self._http.get("/reset"))

    def request_reset(self, request: ResetRequest) -> ResetStatus:
        return ResetStatus.model_validate(self._http.post("/reset", json=request.model_dump()))

    def drain_status(self) -> SchedulerDrainStatus:
        return SchedulerDrainStatus.model_validate(self._http.get("/scheduler/drain"))

    def begin_drain(self) -> SchedulerDrainStatus:
        return SchedulerDrainStatus.model_validate(self._http.post("/scheduler/drain", json={}))

    def resume_from_drain(self) -> SchedulerDrainStatus:
        return SchedulerDrainStatus.model_validate(self._http.post("/scheduler/drain/resume", json={}))

    def drain_and_wait(self, *, timeout: float, poll_interval: float = 0.1) -> SchedulerDrainStatus:
        """开始排空并有界等待；失败不关闭 runtime，也不自动解除任何闸门。"""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("排空预算必须为有限正数")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("轮询间隔必须为有限正数")
        deadline = time.monotonic() + timeout
        status: SchedulerDrainStatus | None = None
        first = True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SchedulerDrainTimeout(status)
            budget = min(self._timeout, remaining)
            try:
                payload = (
                    self._http.post("/scheduler/drain", json={}, timeout=budget)
                    if first else self._http.get("/scheduler/drain", timeout=budget)
                )
            except httpx.TimeoutException as exc:
                if time.monotonic() >= deadline:
                    raise SchedulerDrainTimeout(status) from exc
                raise
            status = SchedulerDrainStatus.model_validate(payload)
            if status.phase == "drained":
                return status
            first = False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SchedulerDrainTimeout(status)
            time.sleep(min(poll_interval, remaining))

    # ── V1 Workspace Host（/v1/*，裸 JSON；失败抛 WorkspaceHostError） ──────────

    def _workspace(self, method: str, path: str, *, json: Any = None, timeout: float | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"envelope": False, "timeout": timeout or self._timeout}
        if json is not None:
            kwargs["json"] = json
        try:
            payload = self._http._request(method, f"{self._service_root}{path}", **kwargs)
        except httpx.HTTPStatusError as exc:
            try:
                failure = exc.response.json().get("error") or {}
            except (ValueError, AttributeError):
                failure = {}
            raise WorkspaceHostError(
                str(failure.get("code") or "host_request_failed"),
                str(failure.get("message") or f"Workspace Host HTTP {exc.response.status_code}"),
                details=failure.get("details"),
            ) from exc
        except httpx.HTTPError as exc:
            raise WorkspaceHostError("host_unreachable", f"Workspace Host 不可达：{self._service_root}") from exc
        if not isinstance(payload, dict):
            raise WorkspaceHostError("host_protocol_invalid", "Workspace Host 响应无效")
        return payload

    def workspace_snapshot(self) -> dict[str, Any]:
        """``GET /v1/snapshot``：调度权威 / Host / 受管设备进程的阶段与 revision。"""
        return self._workspace("GET", "/v1/snapshot")

    def submit_workspace_operation(
        self,
        command: str,
        *,
        parameters: dict[str, Any] | None = None,
        operation_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/operations``：同一 operationId 重放安全（响应丢失后可用原 id 重提）。"""
        payload: dict[str, Any] = {
            "operationId": operation_id or str(uuid.uuid4()),
            "command": command,
            "parameters": parameters or {},
        }
        if expected_revision is not None:
            payload["expectedRevision"] = expected_revision
        return self._workspace("POST", "/v1/operations", json=payload)

    def workspace_operation(self, operation_id: str) -> dict[str, Any]:
        return self._workspace("GET", f"/v1/operations/{operation_id}")

    def wait_workspace_operation(
        self, operation_id: str, *, timeout: float = WORKSPACE_OPERATION_TIMEOUT_S, poll_interval: float = 0.1
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            latest = self.workspace_operation(operation_id)
            if latest.get("phase") in {"succeeded", "failed"}:
                return latest
            time.sleep(poll_interval)
        raise WorkspaceHostError("operation_timeout", f"等待操作完成超时：{operation_id}", details=latest)

    def execute_workspace_operation(
        self,
        command: str,
        *,
        parameters: dict[str, Any] | None = None,
        operation_id: str | None = None,
        expected_revision: int | None = None,
        timeout: float = WORKSPACE_OPERATION_TIMEOUT_S,
    ) -> dict[str, Any]:
        """提交并等待一个工作区操作；失败按操作记录里的 error 抛 ``WorkspaceHostError``（V1 同口径）。"""
        submitted = self.submit_workspace_operation(
            command, parameters=parameters, operation_id=operation_id, expected_revision=expected_revision
        )
        accepted_id = submitted.get("operationId")
        if not isinstance(accepted_id, str) or not accepted_id:
            raise WorkspaceHostError("host_protocol_invalid", "Workspace Host 操作未返回 operationId")
        result = self.wait_workspace_operation(accepted_id, timeout=timeout)
        if result.get("phase") != "failed":
            return result
        failure = result.get("error")
        if isinstance(failure, dict):
            raise WorkspaceHostError(
                str(failure.get("code") or "operation_failed"),
                str(failure.get("message") or "Workspace Host 操作失败"),
                details=failure.get("details"),
            )
        raise WorkspaceHostError("operation_failed", "Workspace Host 操作失败")

    def workspace_logs(self, component: str, *, max_bytes: int = DEFAULT_LOG_MAX_BYTES) -> dict[str, Any]:
        """``GET /v1/logs/{component}?maxBytes=``：组件日志尾部。"""
        return self._workspace("GET", f"/v1/logs/{component}?maxBytes={int(max_bytes)}")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
