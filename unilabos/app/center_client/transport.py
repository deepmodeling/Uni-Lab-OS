"""mTLS HTTP 传输。

身份由已登记的客户端证书承载，不再传递第二份机器密钥。
因此这一层不做"重试时换一个身份"之类的事——身份是部署事实，不是请求参数。

重试只针对**可能已经被处理**的情况保持原样重发，绝不改变请求内容：中心侧靠事件身份
与调用身份去重，工站侧改内容只会让去重失效。唯一例外是控制会话过期，那需要重新
握手，由上层处理。
"""

from __future__ import annotations

import json
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

import requests

from unilabos.app.center_client.config import CenterClientConfig

#: 控制会话请求头。中心侧会与证书身份一起校验，两者都必须成立。
SESSION_HEADERS = {
    "process": "X-Station-Process",
    "session": "X-Station-Session",
    "epoch": "X-Session-Epoch",
}


def retry_after_seconds(value: str | None) -> float | None:
    """接受秒数和带时区的 HTTP 日期；非法头不能破坏恢复循环。"""
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(int(value))
    try:
        timestamp = parsedate_to_datetime(value)
        if timestamp.tzinfo is None:
            return None
        return max(0.0, timestamp.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("中心响应包含重复 JSON 字段")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"中心响应包含非法 JSON 数值：{value}")


def _response_object(response: requests.Response, *, allow_null: bool = False) -> dict[str, Any] | None:
    value = json.loads(response.content, object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant)
    if value is None and allow_null:
        return None
    if not isinstance(value, dict):
        raise ValueError("中心响应必须为 JSON 对象")
    return value


class CenterTransportError(RuntimeError):
    """传输失败。``retryable`` 表示"重发同一请求可能成功"。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds

    def __repr__(self) -> str:
        return (
            f"CenterTransportError(status={self.status_code}, code={self.code!r}, "
            f"retryable={self.retryable})"
        )


class CenterRequestHeldError(CenterTransportError):
    """永久请求错误已保留，只有显式核实后才允许原请求再次发网。"""

    def __init__(self, request_digest: str, status_code: int) -> None:
        super().__init__("原请求暂停重传，须先修正中心限制或兼容问题并显式核实",
                         status_code=status_code, code="REQUEST_REQUIRES_REVIEW")
        self.request_digest = request_digest


class RequestHoldStore(Protocol):
    def request_hold(self, request_digest: str) -> dict[str, Any] | None: ...

    def hold_request(self, request_digest: str, method: str, path: str,
                     event_uuid: str | None, status_code: int, error_code: str | None) -> None: ...


class CenterTransport:
    """对中心机器接口的薄封装。线程安全：会话可被多个执行线程共享。"""

    def __init__(self, config: CenterClientConfig) -> None:
        self._config = config
        self._local = threading.local()
        self._retry_lock = threading.Lock()
        self._retry_until = 0.0
        self._retry_error: CenterTransportError | None = None
        self._request_holds: RequestHoldStore | None = None

    def reload_credentials(self, config: CenterClientConfig) -> None:
        """新请求换用新证书；各线程在下一次请求关闭自己的旧连接池。"""
        self._config = config

    def install_request_holds(self, store: RequestHoldStore) -> None:
        """运行时启动前装配；跨重启复用同一工作流数据库的暂停记录。"""
        self._request_holds = store

    def _check_retry_after(self) -> None:
        with self._retry_lock:
            remaining = self._retry_until - time.monotonic()
            error = self._retry_error
            if remaining > 0 and error is not None:
                # 交接等独立补报可能捕获错误继续处理；传输层也须守住中心的期限。
                raise CenterTransportError(
                    "中心要求退避，持久请求等待原期限后重放",
                    status_code=error.status_code, code=error.code, retryable=True,
                    retry_after_seconds=remaining,
                )
            self._retry_error = None

    def _session(self) -> requests.Session:
        # 每线程一个 Session：`requests.Session` 不是线程安全的。
        session = getattr(self._local, "session", None)
        if session is None or getattr(self._local, "config", None) is not self._config:
            if session is not None:
                session.close()
            session = requests.Session()
            session.verify = str(self._config.server_ca) if self._config.server_ca else True
            session.cert = (
                str(self._config.client_cert),
                str(self._config.client_key),
            )
            self._local.session = session
            self._local.config = self._config
        return session

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        session_headers: Mapping[str, str] | None = None,
        expected_status: int | None = None,
        allow_null_response: bool = False,
    ) -> dict[str, Any] | None:
        from unilabos.utils.tracing import inject_trace_context, span

        # 观测字段只进入 HTTP 头，不改变持久事件、业务摘要或重放身份。
        with span("center.request", kind="client", attributes={
            "http.request.method": method,
            "url.path": path,
            "server.address": urlsplit(self._config.endpoint).hostname or "center",
        }):
            headers = dict(session_headers or {})
            inject_trace_context(headers)
            # 中心当前锁定的 Rust OTel 0.31 拒绝 W3C v00 的随机标志位。
            # 只在这个兼容边界去掉随机位，保留 trace/span 身份和采样决定。
            parent = headers.get("traceparent", "").split("-")
            if len(parent) == 4 and parent[0] == "00" and parent[3] in {"02", "03"}:
                parent[3] = "01" if parent[3] == "03" else "00"
                headers["traceparent"] = "-".join(parent)
            return self._request(method, path, json_body=json_body, session_headers=headers,
                                 expected_status=expected_status, allow_null_response=allow_null_response)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        session_headers: Mapping[str, str] | None = None,
        expected_status: int | None = None,
        allow_null_response: bool = False,
    ) -> dict[str, Any] | None:
        self._check_retry_after()
        request_digest = None
        if self._request_holds is not None:
            from unilabos.app.center_client.digest import digest

            request_digest = digest({"method": method, "path": path, "body": json_body})
            held = self._request_holds.request_hold(request_digest)
            if held is not None:
                raise CenterRequestHeldError(request_digest, held["status_code"])
        url = f"{self._config.endpoint.rstrip('/')}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if session_headers:
            headers.update({key: value for key, value in session_headers.items() if key.lower() != "authorization"})
        try:
            response = self._session().request(
                method,
                url,
                json=None if json_body is None else dict(json_body),
                headers=headers,
                timeout=self._config.timeout_seconds,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError as error:
            # 证书被拒不是可重试的：重发同一张证书只会再被拒一次。
            raise CenterTransportError(
                f"TLS 握手失败，证书可能未被中心接受：{error}", retryable=False
            ) from error
        except requests.exceptions.RequestException as error:
            # 网络层失败：重发同一请求是安全的，中心侧靠身份去重。
            raise CenterTransportError(f"请求中心失败：{error}", retryable=True) from error

        if 300 <= response.status_code < 400:
            raise CenterTransportError("中心机器接口不允许重定向", status_code=response.status_code)
        if response.status_code >= 400:
            error = self._error_from(response)
            if error.status_code in {400, 413, 415, 422, 431} and self._request_holds is not None:
                assert request_digest is not None
                event_uuid = json_body.get("event_uuid") if json_body is not None else None
                self._request_holds.hold_request(request_digest, method, path, event_uuid, response.status_code, error.code)
                raise CenterRequestHeldError(request_digest, response.status_code) from error
            if error.status_code in {429, 503} and error.retry_after_seconds is not None:
                with self._retry_lock:
                    deadline = time.monotonic() + error.retry_after_seconds
                    if deadline > self._retry_until:
                        self._retry_until, self._retry_error = deadline, error
            raise error
        if expected_status is not None and response.status_code != expected_status:
            raise CenterTransportError(
                f"中心确认状态码应为 {expected_status}，实际为 {response.status_code}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return _response_object(response, allow_null=allow_null_response)
        except ValueError as error:
            raise CenterTransportError(
                "中心返回了非 JSON 响应", status_code=response.status_code, retryable=False
            ) from error

    def _error_from(self, response: requests.Response) -> CenterTransportError:
        code: str | None = None
        message = f"中心返回 {response.status_code}"
        try:
            body = _response_object(response)
        except ValueError:
            body = None
        if isinstance(body, dict):
            code = body.get("code")
            message = str(body.get("message") or message)
        # 5xx 与 429 可能是瞬时的；4xx 是请求本身的问题，重发不会变好。
        retryable = response.status_code >= 500 or response.status_code == 429
        return CenterTransportError(
            message, status_code=response.status_code, code=code, retryable=retryable,
            retry_after_seconds=(retry_after_seconds(response.headers.get("Retry-After"))
                                 if response.status_code in {429, 503} else None),
        )
