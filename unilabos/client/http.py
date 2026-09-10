"""HTTP 客户端

基于 httpx 的 HTTP 客户端，提供：
- ak/sk 认证（Authorization: Lab <base64(ak:sk)>）
- 重试机制（指数退避，仅对 5xx 和网络错误）
- 响应信封解析
"""

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import httpx

from .envelope import EnvelopeError, unwrap_envelope
from unilabos.utils.tracing import inject_trace_context, span


@dataclass
class HTTPClientConfig:
    """HTTP 客户端配置"""
    base_url: str
    timeout: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 1.0


class HTTPClient:
    """HTTP 客户端

    使用示例：
        config = HTTPClientConfig(base_url="https://leap-lab.bohrium.com/api/v1")
        client = HTTPClient(config, get_auth_secret=lambda: "base64_ak_sk")
        data = client.get("/labs")
        data = client.post("/labs", json={"name": "Lab1"})
    """

    def __init__(
        self,
        config: HTTPClientConfig,
        get_auth_secret: Optional[Callable[[], str]] = None,
    ):
        """初始化 HTTP 客户端

        Args:
            config: 客户端配置
            get_auth_secret: 获取 base64(ak:sk) 的回调函数
        """
        self.config = config
        self.get_auth_secret = get_auth_secret
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout,
        )

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.get_auth_secret:
            secret = self.get_auth_secret()
            if secret:
                headers["Authorization"] = f"Lab {secret}"
        return headers

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """发送 HTTP 请求

        Returns:
            响应数据（已解析信封）

        Raises:
            EnvelopeError: 业务错误（code != 0）
            httpx.HTTPError: HTTP 错误
        """
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._get_headers())
        target = urlsplit(str(self._client.base_url.join(path)))

        with span(
            "edge.http.backend.request",
            kind="client",
            attributes={
                "http.request.method": method.upper(),
                "server.address": target.hostname or "",
                "url.scheme": target.scheme,
                "url.path": target.path,
            },
        ) as request_span:
            inject_trace_context(headers)
            kwargs["headers"] = headers
            retries = 0
            last_error = None

            while retries <= self.config.max_retries:
                try:
                    response = self._client.request(method, path, **kwargs)
                    try:
                        request_span.set_attribute(
                            "http.response.status_code", response.status_code
                        )
                    except Exception:  # noqa: BLE001 - tracing stays fail-open
                        pass
                    response.raise_for_status()
                    return unwrap_envelope(response.json())

                except (httpx.HTTPError, EnvelopeError) as e:
                    last_error = e

                    # 业务错误和 4xx 不重试
                    if isinstance(e, EnvelopeError):
                        raise
                    if (
                        isinstance(e, httpx.HTTPStatusError)
                        and e.response.status_code < 500
                    ):
                        raise

                    retries += 1
                    if retries <= self.config.max_retries:
                        sleep_time = self.config.retry_backoff * (2 ** (retries - 1))
                        time.sleep(sleep_time)

            raise last_error

    def get(self, path: str, **kwargs) -> Any:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> Any:
        return self._request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self._request("DELETE", path, **kwargs)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
