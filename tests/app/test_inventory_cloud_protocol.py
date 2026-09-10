"""Typed Edge ↔ Cloud inventory HTTP boundary tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from unilabos.app.scheduler.integration import (
    CloudBusinessError,
    make_http_snapshot_sender,
    make_http_sync_sender,
    report_http_inventory_command_result,
)
from unilabos.app.scheduler.inventory.schemas import CloudInventoryEventBatch


class _Response:
    def __init__(self, body: object):
        self.body = body
        self.raise_called = False

    def raise_for_status(self) -> None:
        self.raise_called = True

    def json(self) -> object:
        return self.body


class _Session:
    def __init__(self, *bodies: object):
        self.responses = [_Response(body) for body in bodies]
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses[len(self.calls) - 1]


def _install_http(monkeypatch: pytest.MonkeyPatch, *bodies: object) -> _Session:
    from unilabos.app.web import client as client_module

    session = _Session(*bodies)
    monkeypatch.setattr(client_module.http_client, "remote_addr", "https://cloud.test/api/v1")
    monkeypatch.setattr(client_module.http_client, "_session", session)
    return session


def _event(edge_id: str = "edge-1") -> dict[str, Any]:
    return {
        "event_id": "evt-1",
        "edge_id": edge_id,
        "lab_id": "lab-edge",
        "sequence": 1,
        "aggregate_type": "lot",
        "aggregate_id": "lot-1",
        "aggregate_version": 1,
        "event_type": "lot.inbound",
        "occurred_at": 1_784_840_000_000,
        "causation_id": "cmd-1",
        "payload": {"lot_id": "lot-1", "version": 1},
    }


def _snapshot() -> dict[str, Any]:
    return {
        "snapshot_sequence": 7,
        "templates": [],
        "lots": [],
        "instances": [],
        "relations": [],
        "contents": [],
        "reservations": [],
    }


def test_http_sync_sender_posts_typed_batch_and_unwraps_ack(monkeypatch):
    session = _install_http(
        monkeypatch,
        {"code": 0, "data": {"acked_sequence": 1}},
    )

    assert make_http_sync_sender()([_event()]) == 1
    call = session.calls[0]
    assert call["url"].endswith("/api/v1/edge/sync/events")
    assert call["json"]["edge_id"] == "edge-1"
    assert call["json"]["events"][0]["payload"] == {
        "lot_id": "lot-1",
        "version": 1,
    }
    assert session.responses[0].raise_called


def test_http_200_cloud_business_error_does_not_advance_ack(monkeypatch):
    _install_http(
        monkeypatch,
        {"code": 4090004, "error": {"msg": "rejected", "info": ["stale"]}},
    )

    with pytest.raises(CloudBusinessError, match="rejected") as caught:
        make_http_sync_sender()([_event()])
    assert caught.value.code == 4090004
    assert caught.value.info == ["stale"]


def test_snapshot_and_command_result_use_distinct_cloud_envelopes(monkeypatch):
    session = _install_http(monkeypatch, {"code": 0}, {"code": 0})

    make_http_snapshot_sender("edge-1")(_snapshot())
    report_http_inventory_command_result(
        {
            "command_id": "cmd-1",
            "status": "completed",
            "result": {"lot_id": "lot-1"},
            "error_code": "local-only",
            "replayed": True,
        }
    )

    snapshot_call, result_call = session.calls
    assert snapshot_call["url"].endswith("/api/v1/edge/sync/snapshot")
    assert snapshot_call["json"] == {
        "edge_id": "edge-1",
        "snapshot_sequence": 7,
        "aggregates": {
            "templates": [],
            "lots": [],
            "instances": [],
            "relations": [],
            "contents": [],
            "reservations": [],
        },
    }
    assert result_call["url"].endswith(
        "/api/v1/edge/inventory/command_result"
    )
    assert result_call["json"] == {
        "command_id": "cmd-1",
        "status": "completed",
        "result": {"lot_id": "lot-1"},
    }


def test_event_batch_rejects_cross_edge_identity():
    with pytest.raises(ValidationError, match="differs from batch"):
        CloudInventoryEventBatch.model_validate(
            {"edge_id": "edge-2", "events": [_event("edge-1")]}
        )
