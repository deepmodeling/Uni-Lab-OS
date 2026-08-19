"""history.v1 HTTP 与 Local client 契约测试。"""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.api.history import install_history_api
from unilabos.server.clients.history import (
    HTTPHistoryClient,
    HistoryHTTPError,
    LocalHistoryClient,
)
from unilabos.server.protocol.history import (
    ExternalPayloadWrite,
    HistoryEventAppend,
    HistoryEventQuery,
    InlinePayloadWrite,
    ManualResultReplacement,
)
from unilabos.server.services.history import HistoryService


class _URLResponse:
    def __init__(self, content: bytes):
        self._content = content

    def read(self) -> bytes:
        return self._content

    def __enter__(self) -> "_URLResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _bind_http_client(monkeypatch, app: FastAPI) -> TestClient:
    test_client = TestClient(app)

    def urlopen(request, *, timeout):
        del timeout
        url = urlsplit(request.full_url)
        body = json.loads(request.data) if request.data is not None else None
        response = test_client.request(
            request.get_method(),
            url.path + (f"?{url.query}" if url.query else ""),
            json=body,
        )
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url,
                response.status_code,
                response.reason_phrase,
                response.headers,
                io.BytesIO(response.content),
            )
        return _URLResponse(response.content)

    monkeypatch.setattr("unilabos.server.clients.history.urlopen", urlopen)
    return test_client


def test_http_client_exposes_payload_event_query_and_replacement(
    tmp_path, monkeypatch
) -> None:
    service = HistoryService(tmp_path / "history.db")
    app = FastAPI()
    install_history_api(app, service)
    transport = _bind_http_client(monkeypatch, app)
    client = HTTPHistoryClient("http://testserver")
    try:
        with transport:
            payload = client.store_payload(
                InlinePayloadWrite(
                    payload_uuid="binary-payload",
                    media_type="application/octet-stream",
                    inline_payload=b"\x00\xffhistory",
                    created_at_ms=1,
                )
            )
            assert payload.inline_payload == b"\x00\xffhistory"
            assert client.get_payload(payload.payload_uuid) == payload

            original = client.append_event(
                HistoryEventAppend(
                    event_uuid="result-1",
                    event_type="job_result",
                    job_uuid="job-1",
                    endpoint_uuid="endpoint-1",
                    action_name="transfer",
                    event_key="result",
                    state_version=1,
                    payload_uuid=payload.payload_uuid,
                    occurred_at_ms=1,
                    recorded_at_ms=1,
                )
            )
            assert client.get_event(original.event_uuid) == original
            assert client.query_events(
                HistoryEventQuery(job_uuid="job-1", event_types=["job_result"])
            ) == [original]

            replacement = client.append_replacement(
                ManualResultReplacement(
                    supersedes_event_uuid=original.event_uuid,
                    event_uuid="result-2",
                    actor_uuid="operator-1",
                    summary={"result": "manually corrected"},
                    occurred_at_ms=2,
                    recorded_at_ms=2,
                )
            )
            assert replacement.supersedes_event_uuid == original.event_uuid
            assert [
                event.event_uuid
                for event in client.replacement_chain(original.event_uuid)
            ] == ["result-1", "result-2"]

            with pytest.raises(HistoryHTTPError) as exc_info:
                client.get_event("missing")
            assert exc_info.value.status_code == 404
    finally:
        service.close()


def test_http_router_maps_conflict_and_path_mismatch(tmp_path) -> None:
    service = HistoryService(tmp_path / "history.db")
    app = FastAPI()
    install_history_api(app, service)
    try:
        with TestClient(app) as client:
            original = client.post(
                "/api/v1/history/events",
                json=HistoryEventAppend(
                    event_uuid="result-1",
                    event_type="job_result",
                    job_uuid="job-1",
                    state_version=1,
                    occurred_at_ms=1,
                    recorded_at_ms=1,
                ).model_dump(mode="json"),
            )
            assert original.status_code == 200, original.text

            mismatch = client.post(
                "/api/v1/history/events/result-1/replacement",
                json=ManualResultReplacement(
                    supersedes_event_uuid="other-result",
                    actor_uuid="operator-1",
                ).model_dump(mode="json"),
            )
            assert mismatch.status_code == 422

            body = ManualResultReplacement(
                supersedes_event_uuid="result-1",
                event_uuid="result-2",
                actor_uuid="operator-1",
                occurred_at_ms=2,
                recorded_at_ms=2,
            ).model_dump(mode="json")
            assert (
                client.post(
                    "/api/v1/history/events/result-1/replacement", json=body
                ).status_code
                == 200
            )
            conflict = client.post(
                "/api/v1/history/events/result-1/replacement",
                json={**body, "event_uuid": "fork"},
            )
            assert conflict.status_code == 409
    finally:
        service.close()


def test_local_client_exposes_external_payload_and_append_query(tmp_path) -> None:
    service = HistoryService(tmp_path / "history.db")
    client = LocalHistoryClient(service)
    try:
        payload = client.store_payload(
            ExternalPayloadWrite(
                payload_uuid="external-payload",
                media_type="application/octet-stream",
                byte_length=1_000_000,
                sha256="a" * 64,
                external_uri="s3://bucket/result.bin",
                created_at_ms=1,
            )
        )
        event = client.append_event(
            HistoryEventAppend(
                event_uuid="log-1",
                event_type="job_log",
                job_uuid="job-1",
                payload_uuid=payload.payload_uuid,
                occurred_at_ms=1,
                recorded_at_ms=1,
            )
        )

        assert client.get_payload(payload.payload_uuid).external_uri == (
            "s3://bucket/result.bin"
        )
        assert client.get_event(event.event_uuid) == event
        assert client.query_events(HistoryEventQuery(job_uuid="job-1")) == [event]
    finally:
        service.close()


def test_history_router_has_no_destructive_crud_methods(tmp_path) -> None:
    service = HistoryService(tmp_path / "history.db")
    app = FastAPI()
    install_history_api(app, service)
    try:
        methods = {
            method for path in app.openapi()["paths"].values() for method in path
        }
        assert methods == {"get", "post"}
    finally:
        service.close()
