"""``telemetry.v1`` 的 Local/HTTP 等价客户端。"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from unilabos.server.models.telemetry import (
    DeviceStateLatestRecord,
    TelemetryEventRecord,
    TelemetrySourceCursorRecord,
)
from unilabos.server.protocol.telemetry import (
    DeviceStateSnapshot,
    TelemetryEventQuery,
    TelemetryEventWrite,
    TelemetryIngestRequest,
    TelemetryIngestResult,
)
from unilabos.server.services.telemetry import TelemetryService


def _resolve_query(
    query: Optional[TelemetryEventQuery], filters: dict[str, object]
) -> TelemetryEventQuery:
    if query is not None and filters:
        raise ValueError("pass a TelemetryEventQuery or keyword filters, not both")
    return query or TelemetryEventQuery.model_validate(filters)


class LocalTelemetryClient:
    """测试和微后端同进程调用使用；写入仍只能经过 ingest 状态机。"""

    def __init__(self, service: TelemetryService):
        self.service = service

    def ingest(self, request: TelemetryIngestRequest) -> TelemetryIngestResult:
        return self.service.ingest(request)

    def ingest_event(
        self,
        event: TelemetryEventWrite,
        *,
        device_state: Optional[DeviceStateSnapshot] = None,
    ) -> TelemetryIngestResult:
        return self.ingest(
            TelemetryIngestRequest(event=event, device_state=device_state)
        )

    def get_event(self, event_uuid: str) -> Optional[TelemetryEventRecord]:
        return self.service.get_event(event_uuid)

    def query_events(
        self, query: Optional[TelemetryEventQuery] = None, **filters: object
    ) -> list[TelemetryEventRecord]:
        return self.service.query_events(_resolve_query(query, filters))

    def get_source_cursor(
        self, endpoint_uuid: str
    ) -> Optional[TelemetrySourceCursorRecord]:
        return self.service.get_source_cursor(endpoint_uuid)

    def get_device_state(
        self, endpoint_uuid: str, device_uuid: str
    ) -> Optional[DeviceStateLatestRecord]:
        return self.service.get_device_state(endpoint_uuid, device_uuid)

    def list_device_states(
        self, endpoint_uuid: Optional[str] = None
    ) -> list[DeviceStateLatestRecord]:
        return self.service.list_device_states(endpoint_uuid)


class TelemetryHTTPError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"telemetry API returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class HTTPTelemetryClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0):
        base = base_url.rstrip("/")
        if base.endswith("/api/v1/telemetry"):
            self.base_url = base
        elif base.endswith("/api/v1"):
            self.base_url = base + "/telemetry"
        else:
            self.base_url = base + "/api/v1/telemetry"
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Any] = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            if hasattr(body, "model_dump"):
                body = body.model_dump(mode="json", exclude_none=False)
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                detail = json.loads(raw).get("detail", raw)
            except ValueError:
                detail = raw
            raise TelemetryHTTPError(exc.code, str(detail)) from exc

    def ingest(self, request: TelemetryIngestRequest) -> TelemetryIngestResult:
        return TelemetryIngestResult.model_validate(
            self._request("POST", "/events", request)
        )

    def ingest_event(
        self,
        event: TelemetryEventWrite,
        *,
        device_state: Optional[DeviceStateSnapshot] = None,
    ) -> TelemetryIngestResult:
        return self.ingest(
            TelemetryIngestRequest(event=event, device_state=device_state)
        )

    def get_event(self, event_uuid: str) -> TelemetryEventRecord:
        return TelemetryEventRecord.model_validate(
            self._request("GET", f"/events/{quote(event_uuid, safe='')}")
        )

    def query_events(
        self, query: Optional[TelemetryEventQuery] = None, **filters: object
    ) -> list[TelemetryEventRecord]:
        resolved = _resolve_query(query, filters)
        params = resolved.model_dump(mode="json", exclude_none=True)
        response = self._request("GET", f"/events?{urlencode(params)}")
        return [TelemetryEventRecord.model_validate(item) for item in response]

    def get_source_cursor(self, endpoint_uuid: str) -> TelemetrySourceCursorRecord:
        endpoint = quote(endpoint_uuid, safe="")
        return TelemetrySourceCursorRecord.model_validate(
            self._request("GET", f"/sources/{endpoint}/cursor")
        )

    def get_device_state(
        self, endpoint_uuid: str, device_uuid: str
    ) -> DeviceStateLatestRecord:
        endpoint = quote(endpoint_uuid, safe="")
        device = quote(device_uuid, safe="")
        return DeviceStateLatestRecord.model_validate(
            self._request("GET", f"/states/{endpoint}/{device}")
        )

    def list_device_states(
        self, endpoint_uuid: Optional[str] = None
    ) -> list[DeviceStateLatestRecord]:
        path = "/states"
        if endpoint_uuid is not None:
            path += "?" + urlencode({"endpoint_uuid": endpoint_uuid})
        response = self._request("GET", path)
        return [DeviceStateLatestRecord.model_validate(item) for item in response]


__all__ = [
    "HTTPTelemetryClient",
    "LocalTelemetryClient",
    "TelemetryHTTPError",
]
