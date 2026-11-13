"""history.v1 的 Local/HTTP 等价客户端。"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from unilabos.client.utils.envelope import APIHTTPError, EnvelopeError, request_json

from unilabos.config.control_token import client_headers

from unilabos.server.database.tables.history import HistoryEventRecord
from unilabos.protocol.history import (
    HistoryEventAppend,
    HistoryEventQuery,
    ManualResultReplacement,
    PayloadObjectRead,
    PayloadWrite,
)
from unilabos.server.services.history import HistoryService


def _payload_read(value: Any) -> PayloadObjectRead:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    return PayloadObjectRead.model_validate(value)


class LocalHistoryClient:
    """测试和同进程微后端使用；方法与 HTTP client 对齐。"""

    def __init__(self, service: HistoryService):
        self.service = service

    def store_payload(self, value: PayloadWrite) -> PayloadObjectRead:
        return _payload_read(self.service.store_payload(value))

    def get_payload(self, payload_uuid: str) -> PayloadObjectRead:
        return _payload_read(self.service.get_payload(payload_uuid))

    def append_event(self, value: HistoryEventAppend) -> HistoryEventRecord:
        return self.service.append_event(value)

    def get_event(self, event_uuid: str) -> HistoryEventRecord:
        return self.service.get_event(event_uuid)

    def query_events(
        self, query: Optional[HistoryEventQuery] = None
    ) -> list[HistoryEventRecord]:
        return self.service.query_events(query)

    def append_replacement(self, value: ManualResultReplacement) -> HistoryEventRecord:
        return self.service.append_replacement(value)

    def replacement_chain(self, event_uuid: str) -> list[HistoryEventRecord]:
        return self.service.replacement_chain(event_uuid)


class HistoryHTTPError(APIHTTPError):
    api_name = "history"


class HTTPHistoryClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0):
        base = base_url.rstrip("/")
        # dev 事件 / 载荷存储在 /api/v1/edge/history；/api/v1/history 归 V1 运行历史
        if base.endswith("/api/v1/edge/history"):
            self.base_url = base
        elif base.endswith("/api/v1"):
            self.base_url = base + "/edge/history"
        else:
            self.base_url = base + "/api/v1/edge/history"
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Any] = None) -> Any:
        data = None
        headers = {"Accept": "application/json", **client_headers()}
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
            return request_json(request, timeout=self.timeout, opener=urlopen)
        except EnvelopeError as exc:
            raise HistoryHTTPError.from_envelope_error(exc) from exc

    def store_payload(self, value: PayloadWrite) -> PayloadObjectRead:
        return _payload_read(self._request("POST", "/payloads", value))

    def get_payload(self, payload_uuid: str) -> PayloadObjectRead:
        return _payload_read(self._request("GET", f"/payloads/{payload_uuid}"))

    def append_event(self, value: HistoryEventAppend) -> HistoryEventRecord:
        return HistoryEventRecord.model_validate(
            self._request("POST", "/events", value)
        )

    def get_event(self, event_uuid: str) -> HistoryEventRecord:
        return HistoryEventRecord.model_validate(
            self._request("GET", f"/events/{event_uuid}")
        )

    def query_events(
        self, query: Optional[HistoryEventQuery] = None
    ) -> list[HistoryEventRecord]:
        value = query or HistoryEventQuery()
        params: list[tuple[str, Any]] = [
            ("after_sequence", value.after_sequence),
            ("limit", value.limit),
        ]
        params.extend(("event_types", item) for item in value.event_types)
        for name in (
            "job_uuid",
            "endpoint_uuid",
            "device_uuid",
            "event_key",
            "occurred_from_ms",
            "occurred_through_ms",
        ):
            field_value = getattr(value, name)
            if field_value is not None:
                params.append((name, field_value))
        response = self._request("GET", f"/events?{urlencode(params)}")
        return [HistoryEventRecord.model_validate(item) for item in response]

    def append_replacement(self, value: ManualResultReplacement) -> HistoryEventRecord:
        return HistoryEventRecord.model_validate(
            self._request(
                "POST",
                f"/events/{value.supersedes_event_uuid}/replacement",
                value,
            )
        )

    def replacement_chain(self, event_uuid: str) -> list[HistoryEventRecord]:
        response = self._request("GET", f"/events/{event_uuid}/replacement-chain")
        return [HistoryEventRecord.model_validate(item) for item in response]


__all__ = [
    "HTTPHistoryClient",
    "HistoryHTTPError",
    "LocalHistoryClient",
]
