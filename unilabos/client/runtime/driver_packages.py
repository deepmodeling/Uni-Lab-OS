"""通过统一管理地址操作 Host 的驱动包；客户端不安装包、不启动进程。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any, Self
from urllib.parse import quote

import httpx

from unilabos.client.http import HTTPClient, HTTPClientConfig
from unilabos.protocol.runtime.driver_packages import (
    DriverPackageEnableRequest,
    DriverPackageInstallRequest,
)
from unilabos.utils.address import normalize_api_address


class DriverPackageWaitTimeout(TimeoutError):
    """等待超时不取消 Host 中已接受的操作；可用 operation_id 继续查询。"""

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(f"等待驱动包操作超时，请继续查询 operation {operation_id}")


class HTTPDriverPackageClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        get_auth_secret: Callable[[], str] | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("HTTP 超时必须为有限正数")
        # 安装/卸载没有请求幂等键，响应丢失后必须查询，不能自动重发。
        self._http = HTTPClient(
            HTTPClientConfig(
                normalize_api_address(base_url), timeout=timeout,
                max_retries=0, response_envelope=False,
            ),
            get_auth_secret=get_auth_secret,
        )
        self._timeout = timeout

    def inventory(self) -> dict[str, Any]:
        return self._http.get("/driver-packages")

    def catalog(self) -> dict[str, Any]:
        return self._http.get("/driver-packages/catalog")

    def changes(self) -> dict[str, Any]:
        return self._http.get("/driver-packages/changes")

    def install(self, request: DriverPackageInstallRequest) -> dict[str, Any]:
        return self._validate_operation(self._http.post("/driver-packages/install", json=request.model_dump()))

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        request = DriverPackageEnableRequest(enabled=enabled)
        return self._http.put(
            f"/driver-packages/{quote(name, safe='')}/enabled", json=request.model_dump(),
        )

    def uninstall(self, name: str) -> dict[str, Any]:
        return self._validate_operation(self._http.delete(f"/driver-packages/{quote(name, safe='')}"))

    def operations(self) -> list[dict[str, Any]]:
        return self._http.get("/driver-packages/operations")

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self._validate_operation(
            self._http.get(f"/driver-packages/operations/{quote(operation_id, safe='')}"), operation_id,
        )

    @staticmethod
    def _validate_operation(payload: Any, operation_id: str | None = None) -> dict[str, Any]:
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("operation_id"), str)
            or not payload["operation_id"]
            or (operation_id is not None and payload["operation_id"] != operation_id)
            or not isinstance(payload.get("status"), str)
            or payload.get("status") not in {"running", "succeeded", "failed"}
        ):
            raise ValueError(f"驱动包操作 {operation_id or ''} 的响应身份或状态无效，不能判定为完成")
        return payload

    def wait_operation(
        self, operation_id: str, *, timeout: float, poll_interval: float = 0.2,
    ) -> dict[str, Any]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("等待预算必须为有限正数")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("轮询间隔必须为有限正数")
        deadline = time.monotonic() + timeout
        path = f"/driver-packages/operations/{quote(operation_id, safe='')}"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DriverPackageWaitTimeout(operation_id)
            try:
                operation = self._validate_operation(
                    self._http.get(path, timeout=min(self._timeout, remaining)), operation_id,
                )
            except httpx.TimeoutException as exc:
                if time.monotonic() >= deadline:
                    raise DriverPackageWaitTimeout(operation_id) from exc
                raise
            if operation["status"] != "running":
                return operation
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
