"""HTTP 客户端

基于 httpx 的 HTTP 客户端，提供：
- ak/sk 认证（Authorization: Lab <base64(ak:sk)>）
- 重试机制（指数退避，仅对 5xx 和网络错误）
- 响应信封解析
"""

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import httpx

from .utils.envelope import EnvelopeError, unwrap_envelope


def _looks_like_json(response: httpx.Response) -> bool:
    return "json" in str(response.headers.get("content-type") or "").lower()


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

    def _get_headers(self, *, has_body: bool) -> Dict[str, str]:
        # 只有带 body 的请求才声明 Content-Type：无 body 的 GET/DELETE 带上它会让
        # 严格的服务端（如 Workflow 路由）去解析空 body 并判为格式错误
        headers: Dict[str, str] = {}
        if has_body:
            headers["Content-Type"] = "application/json"
        if self.get_auth_secret:
            secret = self.get_auth_secret()
            if secret:
                headers["Authorization"] = f"Lab {secret}"
        from unilabos.config.control_token import client_headers

        headers.update(client_headers())
        return headers

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """发送 HTTP 请求

        Returns:
            响应数据（已解析信封）

        Raises:
            EnvelopeError: 业务错误（code != 0）
            httpx.HTTPError: HTTP 错误
        """
        has_body = any(
            kwargs.get(key) is not None for key in ("json", "content", "data", "files")
        )
        headers = self._get_headers(has_body=has_body)
        kwargs.setdefault("headers", {}).update(headers)

        retries = 0
        last_error = None

        while retries <= self.config.max_retries:
            try:
                response = self._client.request(method, path, **kwargs)
                response.raise_for_status()
                return unwrap_envelope(response.json())

            except (httpx.HTTPError, EnvelopeError) as e:
                last_error = e

                # 业务错误和 4xx 不重试
                if isinstance(e, EnvelopeError):
                    raise
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                    if e.response.status_code == 404 and not _looks_like_json(e.response):
                        # 路由不存在（纯文本 404）：多半是把微后端 API 指向了
                        # 旧云端 Backend，给出可操作的提示而不是堆栈。
                        raise EnvelopeError(
                            404,
                            f"{self.config.base_url} 不提供 {path.split('?')[0]}；"
                            "该接口仅微后端 / --role backend 支持，"
                            "旧云端 Backend 请使用网页端操作或去掉 --remote",
                        ) from e
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
