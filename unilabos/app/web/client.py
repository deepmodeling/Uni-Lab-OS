"""旧 Backend HTTP 兼容层使用的通用传输基类。

物料通信统一由 :mod:`unilabos.server.clients.materials` 提供；这里不再暴露
任何物料 CRUD，避免形成第二套协议和绕过 MaterialsService 权威。
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

import requests

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.utils.log import info
from unilabos.utils.tracing import inject_trace_context, span


class TracedSession(requests.Session):
    """为 Edge 主动 HTTP 请求统一创建 Client Span 并注入 W3C 上下文。"""

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        parsed = urlsplit(str(url))
        with span(
            "edge.http.backend.request",
            kind="client",
            attributes={
                "http.request.method": str(method).upper(),
                "server.address": parsed.hostname or "",
                "url.scheme": parsed.scheme,
                "url.path": parsed.path,
            },
        ) as request_span:
            inject_trace_context(headers)
            kwargs["headers"] = headers
            response = super().request(method, url, **kwargs)
            try:
                request_span.set_attribute(
                    "http.response.status_code", response.status_code
                )
            except Exception:  # noqa: BLE001 - tracing must remain fail-open
                pass
            return response


class HTTPClient:
    """为受 ``--legacy`` 控制的旧 Backend API 提供连接和认证。"""

    def __init__(
        self,
        remote_addr: Optional[str] = None,
        auth: Optional[str] = None,
        **_: Any,
    ) -> None:
        self.remote_addr = remote_addr or HTTPConfig.remote_addr
        if auth is not None:
            self.auth = auth
        else:
            self.auth = BasicConfig.auth_secret()
            info(f"正在使用ak sk作为授权信息：[{self.auth}]")
        self._session = TracedSession()
        self._session.headers.update({"Authorization": f"Lab {self.auth}"})
        info(f"HTTPClient 初始化完成: remote_addr={self.remote_addr}")


def __getattr__(name: str) -> Any:
    """将旧 ``http_client`` 名称映射到受 ``--legacy`` 控制的兼容层。"""

    if name == "http_client":
        from unilabos.legacy_support.http import get_legacy_http_client

        return get_legacy_http_client()
    raise AttributeError(name)


__all__ = ["HTTPClient", "TracedSession"]
