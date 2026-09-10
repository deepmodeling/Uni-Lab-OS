"""Edge Local Inventory API v1 wire and atomic-command mock tests.

All tests use in-memory/temporary SQLite and fake WS/Cloud transports only.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from queue import Queue
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

import unilabos.app.scheduler.inventory.commands as command_module
import unilabos.app.ws_client as ws_module
from unilabos.app.scheduler.inventory.api import create_app
from unilabos.app.scheduler.inventory.commands import execute_command
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.schemas import (
    ContentListResponse,
    InstanceDetailResponse,
    InstanceListResponse,
    InventoryCommandResult,
    InventoryHealthResponse,
    InventoryLotResponse,
    InventoryReservationResponse,
    InventorySnapshotResponse,
    LedgerListResponse,
    LotListResponse,
    OutboxBacklogResponse,
    OutboxListResponse,
    ProcessedCommandListResponse,
    RelationListResponse,
    ReservationListResponse,
    ResourceTemplateResponse,
    SyncCursorListResponse,
    TemplateListResponse,
    WorkflowReservationListResponse,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.ws_client import DeviceActionManager, MessageProcessor
from tests.app.inventory_wire_fixture import (
    FIXTURE_PATH,
    build_inventory_wire_fixture,
)


COMMAND_TYPES = [
    "inventory.template.upsert",
    "inventory.template.delete",
    "inventory.inbound",
    "inventory.reserve",
    "inventory.release",
    "inventory.consume",
    "inventory.quarantine",
    "material.deploy",
    "material.move",
    "material.detach",
    "material.set_parent",
    "material.content.set",
    "material.content.clear",
    "material.consume",
    "material.discard",
    "material.adjust",
]


def test_cross_repository_fixture_matches_openapi_and_runtime():
    fixture_path: Path = FIXTURE_PATH
    expected = json.loads(fixture_path.read_text(encoding="utf-8"))
    actual = build_inventory_wire_fixture()

    assert actual == expected
    assert len(actual["command_variants"]) == 17
    operation_paths = {
        (item["method"], item["path"]) for item in actual["operations"]
    }
    assert ("GET", "/api/v1/inventory/relations") in operation_paths
    assert ("GET", "/api/v1/inventory/contents") in operation_paths
    assert ("GET", "/api/v1/inventory/outbox/events") in operation_paths
    assert ("GET", "/api/v1/inventory/commands/processed") in operation_paths
    assert ("GET", "/api/v1/inventory/sync/cursors") in operation_paths


def test_system_owned_entity_diagnostics_are_read_only_views():
    service = _service()
    client = TestClient(create_app(service))
    response = client.post(
        "/api/v1/inventory/commands",
        json=_wire_command(
            "cmd-diagnostic",
            "inventory.template.upsert",
            {"template_id": "tpl-diagnostic", "name": "Diagnostic"},
        ),
    )
    assert response.status_code == 200
    maximum = service.store.max_outbox_sequence()
    service.store.set_cursor("cloud", maximum, 1_784_840_000_000)

    events = OutboxListResponse.model_validate(
        client.get("/api/v1/inventory/outbox/events").json()
    )
    commands = ProcessedCommandListResponse.model_validate(
        client.get("/api/v1/inventory/commands/processed").json()
    )
    cursors = SyncCursorListResponse.model_validate(
        client.get("/api/v1/inventory/sync/cursors").json()
    )
    assert events.events[-1].causation_id == "cmd-diagnostic"
    assert commands.commands[0].command_id == "cmd-diagnostic"
    assert cursors.cursors[0].acked_sequence == maximum


def _service() -> InventoryService:
    return InventoryService(
        InventoryStore(":memory:"),
        edge_id="edge-mock",
        lab_id="lab-mock",
    )


def _wire_command(
    command_id: str,
    command_type: str,
    payload: Dict[str, Any],
    **extra: Any,
) -> Dict[str, Any]:
    return {
        "command_id": command_id,
        "type": command_type,
        "actor": "operator:mock",
        "warehouse_zone_id": "zone-mock",
        "payload": payload,
        **extra,
    }


def _prepare_command(
    service: InventoryService,
    command_type: str,
) -> Dict[str, Any]:
    if command_type == "inventory.template.upsert":
        return {"template_id": "tpl-command", "name": "Command template"}
    if command_type == "inventory.template.delete":
        service.upsert_template("tpl-delete", name="Delete me")
        return {"template_id": "tpl-delete"}
    if command_type == "inventory.inbound":
        return {
            "template_id": "tpl-inbound",
            "quantity": 5,
            "lot_id": "lot-inbound",
        }
    if command_type == "inventory.reserve":
        service.inbound_lot("tpl-reserve", 10, lot_id="lot-reserve")
        return {
            "workflow_id": "wf-reserve",
            "node_requirements": {
                "node-a": [{"lot_id": "lot-reserve", "quantity": 2}]
            },
        }
    if command_type == "inventory.release":
        service.inbound_lot("tpl-release", 10, lot_id="lot-release")
        service.reserve_workflow(
            "wf-release",
            {
                "node-a": [
                    MaterialRequirement(lot_id="lot-release", quantity=2)
                ]
            },
        )
        return {"workflow_id": "wf-release", "node_id": "node-a"}
    if command_type == "inventory.consume":
        service.inbound_lot("tpl-consume", 10, lot_id="lot-consume")
        service.reserve_workflow(
            "wf-consume",
            {
                "node-a": [
                    MaterialRequirement(lot_id="lot-consume", quantity=2)
                ]
            },
        )
        return {"workflow_id": "wf-consume", "node_id": "node-a"}
    if command_type == "inventory.quarantine":
        service.register_instance(edge_uuid="mi-quarantine")
        service.reserve_workflow(
            "wf-quarantine",
            {
                "node-a": [
                    MaterialRequirement(instance_uuid="mi-quarantine")
                ]
            },
        )
        service.consume_reservation("wf-quarantine", "node-a")
        return {
            "workflow_id": "wf-quarantine",
            "node_id": "node-a",
            "reason": "mock failure",
        }
    if command_type == "material.deploy":
        service.register_instance(edge_uuid="mi-deploy")
        return {"edge_uuid": "mi-deploy", "parent_uuid": "rack-a", "slot_id": "A1"}
    if command_type == "material.move":
        service.register_instance(edge_uuid="mi-move")
        service.deploy_instance("mi-move", parent_uuid="rack-a", slot_id="A1")
        return {"edge_uuid": "mi-move", "parent_uuid": "rack-b", "slot_id": "B1"}
    if command_type == "material.detach":
        service.register_instance(
            edge_uuid="mi-detach",
            parent_uuid="rack-a",
            slot_id="A1",
        )
        return {"edge_uuid": "mi-detach"}
    if command_type == "material.set_parent":
        service.register_instance(edge_uuid="mi-parent")
        service.register_instance(edge_uuid="mi-child")
        return {"edge_uuid": "mi-child", "parent_uuid": "mi-parent"}
    if command_type == "material.content.set":
        service.register_instance(edge_uuid="mi-content-set")
        return {
            "edge_uuid": "mi-content-set",
            "state": {"substance": "water", "volume_ml": 2.5},
        }
    if command_type == "material.content.clear":
        service.register_instance(edge_uuid="mi-content-clear")
        service.update_content("mi-content-clear", {"substance": "water"})
        return {"edge_uuid": "mi-content-clear"}
    if command_type == "material.consume":
        service.register_instance(edge_uuid="mi-terminal-consume")
        service.deploy_instance(
            "mi-terminal-consume",
            parent_uuid="rack-a",
            slot_id="A1",
        )
        return {"edge_uuid": "mi-terminal-consume"}
    if command_type == "material.discard":
        service.register_instance(edge_uuid="mi-discard")
        return {"edge_uuid": "mi-discard", "reason": "broken"}
    if command_type == "material.adjust":
        service.inbound_lot("tpl-adjust", 10, lot_id="lot-adjust")
        return {"lot_id": "lot-adjust", "new_total": 12, "reason": "counted"}
    raise AssertionError(f"unhandled command type {command_type}")


@pytest.mark.parametrize("command_type", COMMAND_TYPES)
def test_every_material_command_succeeds_over_rest(command_type: str):
    service = _service()
    client = TestClient(create_app(service))
    payload = _prepare_command(service, command_type)

    response = client.post(
        "/api/v1/inventory/commands",
        json=_wire_command(f"cmd-{command_type}", command_type, payload),
    )

    assert response.status_code == 200, response.text
    parsed = InventoryCommandResult.model_validate(response.json())
    assert parsed.status.value == "completed"


@pytest.mark.parametrize(
    "body",
    [
        {"type": "inventory.inbound", "payload": {"template_id": "t", "quantity": 1}},
        _wire_command(
            "   ",
            "inventory.inbound",
            {"kind": "lot", "template_id": "t", "quantity": 1},
        ),
        _wire_command("bad-type", "inventory.unknown", {}),
        {
            **_wire_command(
                "bad-outer",
                "inventory.inbound",
                {"template_id": "t", "quantity": 1},
            ),
            "unexpected": True,
        },
        _wire_command(
            "bad-payload-extra",
            "inventory.inbound",
            {"template_id": "t", "quantity": 1, "unexpected": True},
        ),
        _wire_command(
            "bad-quantity",
            "inventory.inbound",
            {"template_id": "t", "quantity": 0},
        ),
        _wire_command(
            "coerced-quantity",
            "inventory.inbound",
            {"template_id": "t", "quantity": "1"},
        ),
        _wire_command(
            "bad-parent",
            "material.move",
            {"edge_uuid": "mi-a", "parent_uuid": "   "},
        ),
        _wire_command(
            "bad-requirements",
            "inventory.reserve",
            {"workflow_id": "wf-a", "node_requirements": {}},
        ),
        _wire_command(
            "bad-version",
            "material.detach",
            {"edge_uuid": "mi-a"},
            expected_version=True,
        ),
        _wire_command(
            "negative-version",
            "material.detach",
            {"edge_uuid": "mi-a"},
            expected_version=-1,
        ),
        _wire_command(
            "fractional-version",
            "material.detach",
            {"edge_uuid": "mi-a"},
            expected_version=1.5,
        ),
        _wire_command(
            "missing-inbound-kind-fields",
            "inventory.inbound",
            {"kind": "instance"},
        ),
        _wire_command(
            "lot-with-instance-field",
            "inventory.inbound",
            {
                "kind": "lot",
                "template_id": "tpl-a",
                "quantity": 1,
                "barcode": "BC-A",
            },
        ),
        _wire_command(
            "instance-with-lot-field",
            "inventory.inbound",
            {
                "kind": "instance",
                "edge_uuid": "mi-a",
                "quantity": 1,
            },
        ),
        _wire_command(
            "deploy-parent-missing",
            "material.deploy",
            {"edge_uuid": "mi-a"},
        ),
        _wire_command(
            "set-parent-null",
            "material.set_parent",
            {"edge_uuid": "mi-a", "parent_uuid": None},
        ),
        {
            **_wire_command(
                "adjust-empty-actor",
                "material.adjust",
                {"lot_id": "lot-a", "new_total": 1, "reason": "counted"},
            ),
            "actor": "   ",
        },
        _wire_command(
            "adjust-empty-reason",
            "material.adjust",
            {"lot_id": "lot-a", "new_total": 1, "reason": "   "},
        ),
        _wire_command(
            "adjust-negative",
            "material.adjust",
            {"lot_id": "lot-a", "new_total": -1, "reason": "counted"},
        ),
        _wire_command(
            "reserve-empty-node-list",
            "inventory.reserve",
            {
                "workflow_id": "wf-a",
                "node_requirements": {"node-a": []},
            },
        ),
        _wire_command(
            "reserve-two-instance-selectors",
            "inventory.reserve",
            {
                "workflow_id": "wf-a",
                "node_requirements": {
                    "node-a": [
                        {"instance_uuid": "mi-a", "barcode": "BC-A"}
                    ]
                },
            },
        ),
        _wire_command(
            "reserve-instance-quantity",
            "inventory.reserve",
            {
                "workflow_id": "wf-a",
                "node_requirements": {
                    "node-a": [{"instance_uuid": "mi-a", "quantity": 1}]
                },
            },
        ),
        _wire_command(
            "reserve-lot-no-quantity",
            "inventory.reserve",
            {
                "workflow_id": "wf-a",
                "node_requirements": {"node-a": [{"lot_id": "lot-a"}]},
            },
        ),
        _wire_command(
            "reserve-attempt-zero",
            "inventory.reserve",
            {
                "workflow_id": "wf-a",
                "attempt": 0,
                "node_requirements": {
                    "node-a": [{"lot_id": "lot-a", "quantity": 1}]
                },
            },
        ),
        _wire_command(
            "content-null-state",
            "material.content.set",
            {"edge_uuid": "mi-a", "state": None},
        ),
    ],
)
def test_rest_rejects_invalid_or_extra_fields_with_422(body: Dict[str, Any]):
    response = TestClient(create_app(_service())).post(
        "/api/v1/inventory/commands",
        json=body,
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("command_id", "payload"),
    [
        (
            "cmd-inbound-lot-explicit",
            {
                "kind": "lot",
                "template_id": "tpl-explicit",
                "quantity": 1,
                "lot_id": "lot-explicit",
            },
        ),
        (
            "cmd-inbound-instance-explicit",
            {
                "kind": "instance",
                "template_id": "tpl-explicit",
                "edge_uuid": "mi-explicit",
            },
        ),
    ],
)
def test_rest_accepts_both_explicit_inbound_discriminants(
    command_id: str,
    payload: Dict[str, Any],
):
    response = TestClient(create_app(_service())).post(
        "/api/v1/inventory/commands",
        json=_wire_command(command_id, "inventory.inbound", payload),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_rest_accepts_set_parent_empty_and_reserve_instance_quantity_zero():
    service = _service()
    service.register_instance(edge_uuid="mi-parent")
    service.register_instance(edge_uuid="mi-child", parent_uuid="mi-parent")
    client = TestClient(create_app(service))

    clear_parent = client.post(
        "/api/v1/inventory/commands",
        json=_wire_command(
            "cmd-clear-parent",
            "material.set_parent",
            {"edge_uuid": "mi-child", "parent_uuid": ""},
            expected_version=1,
        ),
    )
    assert clear_parent.status_code == 200
    assert clear_parent.json()["result"]["parent_uuid"] == ""

    reserve = client.post(
        "/api/v1/inventory/commands",
        json=_wire_command(
            "cmd-reserve-instance",
            "inventory.reserve",
            {
                "workflow_id": "wf-instance",
                "node_requirements": {
                    "node-instance": [
                        {"instance_uuid": "mi-child", "quantity": 0}
                    ]
                },
            },
        ),
    )
    assert reserve.status_code == 200
    assert reserve.json()["status"] == "completed"

    service.inbound_lot("tpl-adjust-zero", 1, lot_id="lot-adjust-zero")
    adjust = client.post(
        "/api/v1/inventory/commands",
        json=_wire_command(
            "cmd-adjust-zero",
            "material.adjust",
            {
                "lot_id": "lot-adjust-zero",
                "new_total": 0,
                "reason": "empty shelf",
            },
            expected_version=1,
        ),
    )
    assert adjust.status_code == 200
    assert adjust.json()["result"]["quantity_total"] == 0


def test_expected_version_conflict_is_http_409():
    service = _service()
    service.register_instance(edge_uuid="mi-version")
    service.deploy_instance("mi-version", parent_uuid="rack-a")
    client = TestClient(create_app(service))

    response = client.post(
        "/api/v1/inventory/commands",
        json=_wire_command(
            "cmd-stale",
            "material.move",
            {"edge_uuid": "mi-version", "parent_uuid": "rack-b"},
            expected_version=1,
        ),
    )

    assert response.status_code == 409
    assert response.json()["status"] == "rejected"
    assert response.json()["error_code"] == "version_conflict"
    assert service.store.get_instance("mi-version")["parent_uuid"] == "rack-a"


def test_rest_command_replay_returns_first_result_once():
    service = _service()
    client = TestClient(create_app(service))
    command = _wire_command(
        "cmd-replay",
        "inventory.inbound",
        {"template_id": "tpl-r", "quantity": 7, "lot_id": "lot-r"},
    )

    first = client.post("/api/v1/inventory/commands", json=command)
    replay = client.post("/api/v1/inventory/commands", json=command)

    assert first.status_code == replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert service.store.get_lot("lot-r")["quantity_total"] == 7
    assert len(
        service.store.query_all(
            "SELECT * FROM inventory_ledger WHERE causation_id = ?",
            ("cmd-replay",),
        )
    ) == 1


def test_concurrent_same_command_executes_business_once():
    service = _service()
    command = _wire_command(
        "cmd-concurrent",
        "inventory.inbound",
        {"template_id": "tpl-c", "quantity": 9, "lot_id": "lot-c"},
    )
    barrier = threading.Barrier(8)
    responses = []
    errors = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            result = execute_command(service, command)
            with lock:
                responses.append(result)
        except BaseException as exc:  # pragma: no cover - assertion aid
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(responses) == 8
    assert sum(not item.get("replayed", False) for item in responses) == 1
    assert service.store.get_lot("lot-c")["quantity_total"] == 9
    assert len(
        service.store.query_all(
            "SELECT * FROM inventory_ledger WHERE causation_id = ?",
            ("cmd-concurrent",),
        )
    ) == 1
    assert len(
        service.store.query_all(
            "SELECT * FROM sync_outbox WHERE causation_id = ?",
            ("cmd-concurrent",),
        )
    ) == 1
    assert len(
        service.store.query_all(
            "SELECT * FROM processed_command WHERE command_id = ?",
            ("cmd-concurrent",),
        )
    ) == 1


def test_unexpected_crash_rolls_back_claim_business_ledger_and_outbox(monkeypatch):
    service = _service()
    command = _wire_command(
        "cmd-crash",
        "inventory.inbound",
        {"template_id": "tpl-crash", "quantity": 3, "lot_id": "lot-crash"},
    )
    original = command_module.COMMAND_HANDLERS["inventory.inbound"]

    def crash_after_business(svc, validated):
        original(svc, validated)
        raise RuntimeError("simulated process failure before result")

    monkeypatch.setitem(
        command_module.COMMAND_HANDLERS,
        "inventory.inbound",
        crash_after_business,
    )
    with pytest.raises(RuntimeError, match="simulated process failure"):
        execute_command(service, command)

    assert service.store.get_lot("lot-crash") is None
    assert service.store.get_processed_command("cmd-crash") is None
    assert service.store.query_all("SELECT * FROM inventory_ledger") == []
    assert service.store.query_all("SELECT * FROM sync_outbox") == []

    monkeypatch.setitem(
        command_module.COMMAND_HANDLERS,
        "inventory.inbound",
        original,
    )
    assert execute_command(service, command)["status"] == "completed"
    assert service.store.get_lot("lot-crash")["quantity_total"] == 3


def test_all_inventory_read_responses_match_declared_models():
    service = _service()
    service.upsert_template("tpl-read", name="Read")
    service.inbound_lot("tpl-read", 10, lot_id="lot-read")
    service.register_instance(
        edge_uuid="mi-read",
        template_id="tpl-read",
        parent_uuid="rack-read",
        slot_id="A1",
    )
    service.update_content("mi-read", {"substance": "water"})
    service.reserve_workflow(
        "wf-read",
        {"node-read": [MaterialRequirement(lot_id="lot-read", quantity=2)]},
    )
    client = TestClient(create_app(service))
    reservation = service.store.get_reservation("wf-read", "node-read", 1)
    assert reservation is not None

    InventoryHealthResponse.model_validate(
        client.get("/api/v1/inventory/health").json()
    )
    TemplateListResponse.model_validate(client.get("/api/v1/inventory/templates").json())
    ResourceTemplateResponse.model_validate(
        client.get("/api/v1/inventory/templates/tpl-read").json()
    )
    LotListResponse.model_validate(client.get("/api/v1/inventory/lots").json())
    InventoryLotResponse.model_validate(
        client.get("/api/v1/inventory/lots/lot-read").json()
    )
    InstanceListResponse.model_validate(client.get("/api/v1/inventory/instances").json())
    InstanceDetailResponse.model_validate(
        client.get("/api/v1/inventory/instances/mi-read").json()
    )
    RelationListResponse.model_validate(client.get("/api/v1/inventory/relations").json())
    ContentListResponse.model_validate(client.get("/api/v1/inventory/contents").json())
    ReservationListResponse.model_validate(
        client.get("/api/v1/inventory/reservations").json()
    )
    InventoryReservationResponse.model_validate(
        client.get(
            f"/api/v1/inventory/reservations/{reservation['reservation_id']}"
        ).json()
    )
    WorkflowReservationListResponse.model_validate(
        client.get(
            "/api/v1/inventory/workflows/wf-read/reservations"
        ).json()
    )
    LedgerListResponse.model_validate(client.get("/api/v1/inventory/ledger").json())
    OutboxBacklogResponse.model_validate(
        client.get("/api/v1/inventory/outbox/backlog").json()
    )
    snapshot = client.get("/api/v1/inventory/snapshot")
    parsed_snapshot = InventorySnapshotResponse.model_validate(snapshot.json())
    assert set(snapshot.json()) == {
        "snapshot_sequence",
        "templates",
        "lots",
        "instances",
        "relations",
        "contents",
        "reservations",
    }
    assert parsed_snapshot.instances[0].parent_uuid == "rack-read"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/inventory/templates/missing",
        "/api/v1/inventory/lots/missing",
        "/api/v1/inventory/instances/missing",
        "/api/v1/inventory/reservations/missing",
    ],
)
def test_inventory_detail_routes_return_documented_404(path: str):
    response = TestClient(create_app(_service())).get(path)
    assert response.status_code == 404
    assert set(response.json()) == {"detail"}


class _NoopThread:
    """Prevent the WS command-result HTTP callback from touching a real cloud."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self) -> None:
        pass


def _drain(queue: Queue) -> list[Dict[str, Any]]:
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


def test_cloud_ws_uses_same_schema_and_result_contract(monkeypatch):
    monkeypatch.setattr(ws_module.threading, "Thread", _NoopThread)
    queue = Queue(maxsize=20)
    processor = MessageProcessor(
        "ws://mock",
        queue,
        DeviceActionManager(),
    )
    processor.inventory_service = _service()
    valid = _wire_command(
        "cmd-ws-valid",
        "inventory.inbound",
        {"template_id": "tpl-ws", "quantity": 4, "lot_id": "lot-ws"},
    )
    invalid = {
        **_wire_command(
            "cmd-ws-invalid",
            "inventory.inbound",
            {"template_id": "tpl-ws", "quantity": 99, "lot_id": "lot-invalid"},
        ),
        "unknown": True,
    }

    asyncio.run(processor._handle_inventory_command(valid))
    asyncio.run(processor._handle_inventory_command(invalid))

    results = [
        message["data"]
        for message in _drain(queue)
        if message.get("action") == "inventory_command_result"
    ]
    assert [item["status"] for item in results] == ["completed", "rejected"]
    assert results[1]["error_code"] == "validation_error"
    assert isinstance(results[0]["timestamp"], int)
    assert results[0]["timestamp"] > 1_000_000_000_000
    assert processor.inventory_service.store.get_lot("lot-ws") is not None
    assert processor.inventory_service.store.get_lot("lot-invalid") is None

    rest = TestClient(create_app(processor.inventory_service)).post(
        "/api/v1/inventory/commands",
        json=invalid,
    )
    assert rest.status_code == 422
