"""Generate the cross-repository Inventory Local API v1 contract fixture.

The fixture is derived from FastAPI OpenAPI plus real TestClient responses.  It
is canonical in Uni-Lab-OS and mirrored byte-for-byte into unilab-edge-ui,
where Vitest consumes it.  Generation is fully offline and only uses in-memory
SQLite databases.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.api import create_app
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.schemas import (
    SyncOutboxRowResponse,
    parse_inventory_command,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.sync import _row_to_envelope

FIXED_NOW_SECONDS = 1_784_840_000.0
FIXTURE_NAME = "inventory-local-api-v1.json"
FIXTURE_PATH = Path(__file__).with_name("fixtures") / FIXTURE_NAME

TRACE_ID = "0123456789abcdef0123456789abcdef"
SPAN_ID = "0123456789abcdef"
TRACEPARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"


COMMAND_VARIANTS: list[Dict[str, Any]] = [
    {
        "action_id": "template.upsert",
        "request": {
            "command_id": "cmd-template-upsert",
            "type": "inventory.template.upsert",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {
                "template_id": "tpl-1",
                "name": "Water",
                "spec": {"storage_class": "ambient"},
            },
        },
    },
    {
        "action_id": "template.delete",
        "request": {
            "command_id": "cmd-template-delete",
            "type": "inventory.template.delete",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {"template_id": "tpl-1"},
        },
    },
    {
        "action_id": "lot.inbound",
        "request": {
            "command_id": "cmd-lot-inbound",
            "type": "inventory.inbound",
            "actor": "operator:contract",
            "warehouse_zone_id": "zone-a",
            "payload": {
                "kind": "lot",
                "template_id": "tpl-1",
                "quantity": 100,
                "unit": "mL",
                "lot_id": "lot-1",
            },
        },
    },
    {
        "action_id": "lot.adjust",
        "request": {
            "command_id": "cmd-lot-adjust",
            "type": "material.adjust",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {
                "lot_id": "lot-1",
                "new_total": 95,
                "reason": "cycle count",
            },
        },
    },
    {
        "action_id": "instance.inbound",
        "request": {
            "command_id": "cmd-instance-inbound",
            "type": "inventory.inbound",
            "actor": "operator:contract",
            "payload": {
                "kind": "instance",
                "template_id": "tpl-1",
                "edge_uuid": "mi-1",
                "barcode": "BC-1",
            },
        },
    },
    {
        "action_id": "instance.deploy",
        "request": {
            "command_id": "cmd-instance-deploy",
            "type": "material.deploy",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {
                "edge_uuid": "mi-1",
                "parent_uuid": "deck-1",
                "slot_id": "A1",
            },
        },
    },
    {
        "action_id": "instance.move",
        "request": {
            "command_id": "cmd-instance-move",
            "type": "material.move",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {
                "edge_uuid": "mi-1",
                "parent_uuid": "deck-2",
                "slot_id": "B2",
            },
        },
    },
    {
        "action_id": "instance.set_parent",
        "request": {
            "command_id": "cmd-instance-parent",
            "type": "material.set_parent",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {"edge_uuid": "mi-1", "parent_uuid": ""},
        },
    },
    {
        "action_id": "instance.detach",
        "request": {
            "command_id": "cmd-instance-detach",
            "type": "material.detach",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {"edge_uuid": "mi-1"},
        },
    },
    {
        "action_id": "instance.consume",
        "request": {
            "command_id": "cmd-instance-consume",
            "type": "material.consume",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {"edge_uuid": "mi-1"},
        },
    },
    {
        "action_id": "instance.discard",
        "request": {
            "command_id": "cmd-instance-discard",
            "type": "material.discard",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {"edge_uuid": "mi-1", "reason": "damaged"},
        },
    },
    {
        "action_id": "content.set",
        "request": {
            "command_id": "cmd-content-set",
            "type": "material.content.set",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {
                "edge_uuid": "mi-1",
                "state": {"substance": "water", "volume_ml": 5},
            },
        },
    },
    {
        "action_id": "content.clear",
        "request": {
            "command_id": "cmd-content-clear",
            "type": "material.content.clear",
            "actor": "operator:contract",
            "expected_version": 3,
            "payload": {"edge_uuid": "mi-1"},
        },
    },
    {
        "action_id": "reservation.reserve",
        "request": {
            "command_id": "cmd-reservation-reserve",
            "type": "inventory.reserve",
            "actor": "operator:contract",
            "payload": {
                "workflow_id": "wf-1",
                "node_requirements": {
                    "node-1": [{"lot_id": "lot-1", "quantity": 10}]
                },
            },
        },
    },
    {
        "action_id": "reservation.release",
        "request": {
            "command_id": "cmd-reservation-release",
            "type": "inventory.release",
            "actor": "operator:contract",
            "payload": {
                "workflow_id": "wf-1",
                "node_id": "node-1",
                "reason": "cancelled",
            },
        },
    },
    {
        "action_id": "reservation.consume",
        "request": {
            "command_id": "cmd-reservation-consume",
            "type": "inventory.consume",
            "actor": "operator:contract",
            "payload": {
                "workflow_id": "wf-1",
                "node_id": "node-1",
                "parent_uuid": "deck-1",
                "slot_id": "A1",
            },
        },
    },
    {
        "action_id": "reservation.quarantine",
        "request": {
            "command_id": "cmd-reservation-quarantine",
            "type": "inventory.quarantine",
            "actor": "operator:contract",
            "payload": {
                "workflow_id": "wf-1",
                "node_id": "node-1",
                "reason": "failed run",
            },
        },
    },
]


def _service() -> InventoryService:
    return InventoryService(
        InventoryStore(":memory:"),
        edge_id="edge-contract",
        lab_id="lab-contract",
        time_fn=lambda: FIXED_NOW_SECONDS,
    )


def _response(response) -> Dict[str, Any]:  # type: ignore[no-untyped-def]
    return {"status": response.status_code, "body": response.json()}


def _schema_name(schema: Optional[Dict[str, Any]]) -> Optional[str]:
    if not schema:
        return None
    reference = schema.get("$ref")
    if reference:
        return str(reference).rsplit("/", 1)[-1]
    if schema.get("discriminator", {}).get("propertyName") == "type":
        return "InventoryCommand"
    title = schema.get("title")
    return str(title) if title else None


def _operation_matrix(client: TestClient) -> list[Dict[str, Any]]:
    specification = client.app.openapi()
    operations: list[Dict[str, Any]] = []
    for path, path_item in sorted(specification["paths"].items()):
        if not path.startswith("/api/v1/inventory"):
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if operation is None:
                continue
            request_schema = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            responses = {
                str(status): _schema_name(
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                for status, response in operation.get("responses", {}).items()
            }
            operations.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "request_model": _schema_name(request_schema),
                    "responses": responses,
                }
            )
    return operations


def _validation_error_projection(response) -> Dict[str, Any]:  # type: ignore[no-untyped-def]
    body = response.json()
    return {
        "status": response.status_code,
        "body": {
            "detail": [
                {
                    key: issue[key]
                    for key in ("type", "loc", "msg")
                    if key in issue
                }
                for issue in body.get("detail", [])
            ]
        },
    }


def _replace_strings(value: Any, replacements: Dict[str, str]) -> Any:
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
        return value
    if isinstance(value, list):
        return [_replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_strings(item, replacements)
            for key, item in value.items()
        }
    return value


def _seed_read_responses() -> tuple[
    Dict[str, Dict[str, Any]],
    Dict[str, Any],
]:
    service = _service()
    client = TestClient(create_app(service))

    successful_request = {
        "command_id": "cmd-success",
        "type": "inventory.template.upsert",
        "actor": "operator:contract",
        "expected_version": 0,
        "payload": {
            "template_id": "tpl-contract",
            "name": "Water",
            "category": "reagent",
            "spec": {"storage_class": "ambient"},
        },
    }
    successful = _response(
        client.post("/api/v1/inventory/commands", json=successful_request)
    )
    service.inbound_lot(
        "tpl-contract",
        20,
        unit="mL",
        lot_id="lot-contract",
        warehouse_zone_id="zone-cold",
        actor="operator:contract",
        causation_id="seed-lot",
    )
    service.register_instance(
        template_id="tpl-contract",
        lot_id="lot-contract",
        barcode="BC-CONTRACT",
        edge_uuid="mi-contract",
        parent_uuid="rack-contract",
        slot_id="A1",
        actor="operator:contract",
        causation_id="seed-instance",
    )
    service.update_content(
        "mi-contract",
        {"substance": "water", "volume_ml": 5},
        actor="operator:contract",
        causation_id="seed-content",
    )
    service.reserve_workflow(
        "wf-contract",
        {
            "node-contract": [
                MaterialRequirement(lot_id="lot-contract", quantity=3)
            ]
        },
        actor="operator:contract",
        causation_id="seed-reservation",
    )

    reservation = service.store.get_reservation(
        "wf-contract", "node-contract", 1
    )
    assert reservation is not None
    reservation_id = str(reservation["reservation_id"])

    with service.store.transaction() as connection:
        connection.execute(
            "UPDATE inventory_ledger SET trace_id = ?, span_id = ? "
            "WHERE causation_id = 'cmd-success'",
            (TRACE_ID, SPAN_ID),
        )
        connection.execute(
            "UPDATE sync_outbox SET event_id = 'event-contract', "
            "traceparent = ?, tracestate = 'vendor=contract', "
            "trace_id = ?, span_id = ? WHERE causation_id = 'cmd-success'",
            (TRACEPARENT, TRACE_ID, SPAN_ID),
        )
        connection.execute(
            "UPDATE processed_command SET processed_at = ?",
            (int(FIXED_NOW_SECONDS * 1000),),
        )
        for row in connection.execute(
            "SELECT sequence, causation_id FROM sync_outbox ORDER BY sequence"
        ).fetchall():
            if row["causation_id"] == "cmd-success":
                continue
            connection.execute(
                "UPDATE sync_outbox SET event_id = ? WHERE sequence = ?",
                (f"event-contract-{row['sequence']}", row["sequence"]),
            )

    ledger_response = _response(client.get("/api/v1/inventory/ledger"))
    responses = {
        "relations": _response(client.get("/api/v1/inventory/relations")),
        "contents": _response(client.get("/api/v1/inventory/contents")),
        "snapshot": _response(client.get("/api/v1/inventory/snapshot")),
        "outbox_backlog": _response(
            client.get("/api/v1/inventory/outbox/backlog")
        ),
        "outbox_events": _response(
            client.get("/api/v1/inventory/outbox/events")
        ),
        "processed_commands": _response(
            client.get("/api/v1/inventory/commands/processed")
        ),
        "sync_cursors": _response(
            client.get("/api/v1/inventory/sync/cursors")
        ),
    }

    outbox_row = service.store.query_one(
        "SELECT * FROM sync_outbox WHERE causation_id = 'cmd-success'"
    )
    assert outbox_row is not None
    row_wire = SyncOutboxRowResponse.model_validate(outbox_row).model_dump(
        mode="json"
    )
    event_wire = _row_to_envelope(outbox_row).model_dump(
        mode="json", exclude_none=True
    )
    trace = {
        "ledger_entry": next(
            entry
            for entry in ledger_response["body"]["entries"]
            if entry["causation_id"] == "cmd-success"
        ),
        "outbox_row": row_wire,
        "cloud_event": event_wire,
    }
    replacements = {reservation_id: "rsv-contract"}
    return (
        _replace_strings(responses, replacements),
        {
            "request": successful_request,
            "response": successful,
            "trace": _replace_strings(trace, replacements),
        },
    )


def _command_outcomes() -> Dict[str, Any]:
    conflict_service = _service()
    conflict_service.register_instance(edge_uuid="mi-version")
    conflict_service.deploy_instance("mi-version", parent_uuid="rack-a")
    conflict_client = TestClient(create_app(conflict_service))
    conflict_request = {
        "command_id": "cmd-version-conflict",
        "type": "material.move",
        "actor": "operator:contract",
        "expected_version": 1,
        "payload": {
            "edge_uuid": "mi-version",
            "parent_uuid": "rack-b",
        },
    }
    conflict_response = _response(
        conflict_client.post(
            "/api/v1/inventory/commands", json=conflict_request
        )
    )

    validation_client = TestClient(create_app(_service()))
    validation_request = {
        "command_id": "cmd-validation",
        "type": "inventory.inbound",
        "actor": "operator:contract",
        "payload": {
            "kind": "lot",
            "template_id": "tpl-invalid",
            "quantity": 0,
        },
    }
    validation_response = _validation_error_projection(
        validation_client.post(
            "/api/v1/inventory/commands", json=validation_request
        )
    )

    replay_client = TestClient(create_app(_service()))
    replay_request = {
        "command_id": "cmd-replay",
        "type": "inventory.inbound",
        "actor": "operator:contract",
        "warehouse_zone_id": "zone-contract",
        "payload": {
            "kind": "lot",
            "template_id": "tpl-replay",
            "quantity": 7,
            "lot_id": "lot-replay",
        },
    }
    first = _response(
        replay_client.post("/api/v1/inventory/commands", json=replay_request)
    )
    replay = _response(
        replay_client.post("/api/v1/inventory/commands", json=replay_request)
    )
    return {
        "version_conflict": {
            "request": conflict_request,
            "response": conflict_response,
        },
        "validation_error": {
            "request": validation_request,
            "response": validation_response,
        },
        "replay": {
            "request": replay_request,
            "first_response": first,
            "replay_response": replay,
        },
    }


def build_inventory_wire_fixture() -> Dict[str, Any]:
    for variant in COMMAND_VARIANTS:
        parse_inventory_command(variant["request"])

    matrix_client = TestClient(create_app(_service()))
    responses, successful = _seed_read_responses()
    return {
        "fixture_version": 1,
        "scope": "Edge Local Inventory REST; Cloud event is backend-only trace evidence",
        "operations": _operation_matrix(matrix_client),
        "command_variants": COMMAND_VARIANTS,
        "successful_command": {
            "request": successful["request"],
            "response": successful["response"],
        },
        **_command_outcomes(),
        "responses": responses,
        "ledger_outbox_trace": successful["trace"],
    }


def fixture_text() -> str:
    return json.dumps(
        build_inventory_wire_fixture(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _check_paths(text: str, paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"fixture missing: {path}")
        if path.read_text(encoding="utf-8") != text:
            raise SystemExit(f"fixture is stale: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="append",
        default=[],
        type=Path,
        help="write the generated fixture to this path (repeatable)",
    )
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        type=Path,
        help="fail unless this path is byte-identical (repeatable)",
    )
    args = parser.parse_args()
    text = fixture_text()
    for path in args.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _check_paths(text, args.check)
    if not args.write and not args.check:
        print(text, end="")


if __name__ == "__main__":
    main()
