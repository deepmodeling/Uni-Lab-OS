"""HTTP 客户端

基于 httpx 的 HTTP 客户端，提供：
- ak/sk 认证（Authorization: Lab <base64(ak:sk)>）与控制面 token（X-UniLab-Token）
- 重试机制（指数退避，仅对 5xx 和网络错误）
- 响应信封解析：返回 ``data``；``code != 0`` 一律抛 :class:`EnvelopeError`（带业务码、
  符号码、details 与真实 HTTP 状态码），不论 HTTP 状态是 200（业务失败）还是 401 / 503
  （中间件级拒绝）
"""

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import httpx

from .utils.envelope import EnvelopeError, is_envelope, unwrap_envelope

#: 信封业务码：依赖暂不可用。带 ``Retry-After`` 时是瞬时状态（Host 子进程启动 / 重启中）。
SERVICE_UNAVAILABLE_CODE = 5001


class _TransientEnvelopeError(Exception):
    """可重试的 5001 信封错误；重试用尽时抛出原 :class:`EnvelopeError`。"""

    def __init__(self, error: EnvelopeError):
        super().__init__(str(error))
        self.error = error


def _looks_like_json(response: httpx.Response) -> bool:
    return "json" in str(response.headers.get("content-type") or "").lower()


@dataclass
class HTTPClientConfig:
    """HTTP 客户端配置"""
    base_url: str
    timeout: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 1.0
    # 微后端全部 JSON API 使用信封；只有健康 / 就绪探针等端点按调用传 envelope=False。
    response_envelope: bool = True


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

    def _request(self, method: str, path: str, *, envelope: Optional[bool] = None, **kwargs) -> Any:
        """发送 HTTP 请求

        Args:
            envelope: 本次响应是否为信封；缺省取 ``config.response_envelope``。
                不包信封的端点（``/health``、``/readiness``）传 ``False``。

        Returns:
            响应数据（已解析信封）

        Raises:
            EnvelopeError: 业务错误（code != 0，任何 HTTP 状态码）
            httpx.HTTPError: 网络错误，或不包信封的 HTTP 错误响应
        """
        use_envelope = self.config.response_envelope if envelope is None else envelope
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
                if response.status_code >= 500 and retries < self.config.max_retries:
                    # 5xx（含维护闸门 503）按退避重试；业务失败（HTTP 200 + code）不重试
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
                try:
                    return self._decode(response, path, use_envelope)
                except EnvelopeError as exc:
                    # 例外：Host 子进程启动 / 重启中，Host 专有路由回 HTTP 200 + 5001 并带 Retry-After，
                    # 与原先的 HTTP 503 一样是瞬时不可用，按同样的退避重试。
                    if (
                        exc.code == SERVICE_UNAVAILABLE_CODE
                        and "retry-after" in response.headers
                        and retries < self.config.max_retries
                    ):
                        raise _TransientEnvelopeError(exc) from exc
                    raise

            except (httpx.HTTPError, EnvelopeError, _TransientEnvelopeError) as e:
                last_error = e.error if isinstance(e, _TransientEnvelopeError) else e

                # 业务错误和 4xx 不重试
                if isinstance(e, EnvelopeError):
                    raise
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                    raise

                retries += 1
                if retries <= self.config.max_retries:
                    sleep_time = self.config.retry_backoff * (2 ** (retries - 1))
                    time.sleep(sleep_time)

        raise last_error

    def _decode(self, response: httpx.Response, path: str, use_envelope: bool) -> Any:
        if use_envelope and _looks_like_json(response):
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if is_envelope(payload):
                return unwrap_envelope(payload, http_status=response.status_code)
        if response.status_code == 404 and not _looks_like_json(response):
            # 路由不存在（纯文本 404）：多半是把微后端 API 指向了
            # 旧云端 Backend，给出可操作的提示而不是堆栈。
            raise EnvelopeError(
                404,
                f"{self.config.base_url} 不提供 {path.split('?')[0]}；"
                "该接口仅微后端 / --role backend 支持，"
                "旧云端 Backend 请使用网页端操作或去掉 --remote",
                http_status=404,
            )
        response.raise_for_status()
        payload = response.json()
        return unwrap_envelope(payload, http_status=response.status_code) if use_envelope else payload

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
