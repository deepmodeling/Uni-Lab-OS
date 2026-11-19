"""Backend HTTP 数据面客户端。

WebSocket 只通知对象变化；调度命令及其他数据域的权威正文通过这里拉取。
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import requests

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.protocol.runtime.control import BackendCommandDocument
from unilabos.utils.tracing import inject_trace_context


class BackendHTTPError(RuntimeError):
    """Backend HTTP 数据面返回了无效或失败响应。"""


class BackendHTTPClient:
    """Backend WS 通知对应的通用 HTTP 数据面客户端。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = str(base_url or HTTPConfig.remote_addr).rstrip("/")
        if not self.base_url:
            raise ValueError("backend HTTP address is required")
        self.timeout = timeout
        self.session = session or requests.Session()
        if "Authorization" not in self.session.headers:
            self.session.headers.update(
                {"Authorization": f"Lab {BasicConfig.auth_secret()}"}
            )

    def fetch_command(self, command_uuid: str) -> BackendCommandDocument:
        """仅由 UUID 派生固定路径，禁止 WS 下发任意拉取 URL。"""

        headers: dict[str, Any] = {}
        inject_trace_context(headers)
        response = self.session.get(
            f"{self.base_url}/edge/commands/{quote(command_uuid, safe='')}",
            headers=headers,
            timeout=self.timeout,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise BackendHTTPError(
                f"command {command_uuid!r} returned non-JSON HTTP "
                f"{response.status_code}"
            ) from exc
        if not 200 <= response.status_code < 300:
            raise BackendHTTPError(
                f"command {command_uuid!r} returned HTTP {response.status_code}: "
                f"{body}"
            )
        if not isinstance(body, dict):
            raise BackendHTTPError("backend command response must be an object")
        if "code" in body and int(body.get("code") or 0) != 0:
            raise BackendHTTPError(
                f"backend command business error {body.get('code')}: "
                f"{body.get('error') or body.get('message')}"
            )
        data = body.get("data", body)
        try:
            return BackendCommandDocument.model_validate(data)
        except Exception as exc:
            raise BackendHTTPError(
                f"command {command_uuid!r} has an invalid document"
            ) from exc


__all__ = ["BackendHTTPClient", "BackendHTTPError"]
