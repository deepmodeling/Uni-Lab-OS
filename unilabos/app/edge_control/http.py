"""生产 Edge 协议的 HTTP 事实数据面。"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

import requests

from unilabos.app.edge_control.store import StoredJob
from unilabos.utils.tracing import inject_trace_context, span


class EdgeProtocolHTTPError(RuntimeError):
    """后端拒绝 Edge 数据面请求。"""


class EdgeDataPlane:
    def __init__(
        self,
        backend_address: str,
        scheduler_address: str,
        api_key: str,
        timeout: float = 10.0,
    ) -> None:
        self.backend_api = _api_base(backend_address)
        self.scheduler_api = _api_base(scheduler_address)
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {api_key}"})
        self._lock = threading.Lock()

    def register_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"{self.scheduler_api}/edge/sessions",
            span_name="edge.http.session.register",
            http_route="/api/v1/edge/sessions",
            json=payload,
        )

    def fetch_job(self, job: StoredJob) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"{self.backend_api}/edge/jobs/{job.job_uuid}",
            span_name="edge.http.job.fetch",
            http_route="/api/v1/edge/jobs/:job_uuid",
            params={"task_uuid": job.task_uuid, "node_uuid": job.node_uuid},
            headers=_job_headers(job),
        )

    def commit_feedback(
        self,
        job: StoredJob,
        sequence: int,
        feedback_type: str,
        feedback: Dict[str, Any],
        observed_at: str,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"{self.backend_api}/edge/jobs/{job.job_uuid}/feedback",
            span_name="edge.http.job.feedback.commit",
            http_route="/api/v1/edge/jobs/:job_uuid/feedback",
            headers=_job_headers(job),
            json={
                "sequence": sequence,
                "feedback_type": feedback_type,
                "data": feedback,
                "observed_at": observed_at,
                "idempotency_key": f"{job.job_uuid}:feedback:{sequence}",
            },
        )

    def commit_outcome(
        self,
        job: StoredJob,
        outcome: str,
        return_info: Dict[str, Any],
        error_info: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        headers = _job_headers(job)
        headers["Idempotency-Key"] = f"{job.job_uuid}:outcome:v1"
        return self._request(
            "PUT",
            f"{self.backend_api}/edge/jobs/{job.job_uuid}/outcome",
            span_name="edge.http.job.outcome.commit",
            http_route="/api/v1/edge/jobs/:job_uuid/outcome",
            headers=headers,
            json={
                "task_uuid": job.task_uuid,
                "node_uuid": job.node_uuid,
                "outcome": outcome,
                "return_info": return_info,
                "error_info": error_info,
            },
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        span_name: str,
        http_route: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        kwargs.setdefault("timeout", self.timeout)
        headers = dict(kwargs.pop("headers", {}) or {})
        with span(
            span_name,
            kind="client",
            attributes={
                "http.request.method": method,
                "http.route": http_route,
            },
        ):
            carrier: Dict[str, Any] = {}
            inject_trace_context(carrier)
            for key in ("traceparent", "tracestate"):
                if carrier.get(key):
                    headers[key] = str(carrier[key])
            if headers:
                kwargs["headers"] = headers
            with self._lock:
                response = self._session.request(method, url, **kwargs)
            try:
                payload = response.json()
            except ValueError as exc:
                raise EdgeProtocolHTTPError(
                    f"{method} {url} returned non-JSON HTTP {response.status_code}"
                ) from exc
            if response.status_code < 200 or response.status_code >= 300:
                raise EdgeProtocolHTTPError(
                    f"{method} {url} returned HTTP {response.status_code}: {payload}"
                )
            if not isinstance(payload, dict):
                raise EdgeProtocolHTTPError(
                    f"{method} {url} returned a non-object payload"
                )
            if "code" in payload and int(payload.get("code") or 0) != 0:
                raise EdgeProtocolHTTPError(
                    f"{method} {url} returned business error {payload.get('code')}: "
                    f"{payload.get('error')}"
                )
            result = payload.get("data", payload)
            if not isinstance(result, dict):
                raise EdgeProtocolHTTPError(f"{method} {url} returned invalid data")
            return result


def _job_headers(job: StoredJob) -> Dict[str, str]:
    return {
        "X-Command-UUID": job.command_uuid,
        "X-Job-Token": job.job_access_token,
    }


def _api_base(address: str) -> str:
    base = str(address or "").strip().rstrip("/")
    if not base:
        raise ValueError("Edge protocol address is required")
    for suffix in ("/api/v1/edge/ws", "/api/v1/ws/schedule"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"


def websocket_url(scheduler_address: str) -> str:
    api = _api_base(scheduler_address)
    if api.startswith("https://"):
        return "wss://" + api[len("https://") :] + "/edge/ws"
    if api.startswith("http://"):
        return "ws://" + api[len("http://") :] + "/edge/ws"
    if api.startswith("wss://"):
        return api + "/edge/ws"
    if api.startswith("ws://"):
        return api + "/edge/ws"
    raise ValueError("scheduler address must use http(s) or ws(s)")
