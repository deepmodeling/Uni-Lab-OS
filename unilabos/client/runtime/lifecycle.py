"""统一管理地址的生命周期 SDK；查询、构造客户端均不会启动进程。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Self

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
from unilabos.utils.address import normalize_api_address


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
        self._http = HTTPClient(
            HTTPClientConfig(normalize_api_address(base_url), timeout=timeout, max_retries=0),
            get_auth_secret=get_auth_secret,
        )
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

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
