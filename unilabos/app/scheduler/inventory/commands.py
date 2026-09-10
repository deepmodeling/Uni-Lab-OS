"""REST/Cloud command-to-edge strict parsing and atomic execution.

统一 envelope：command_id / expected_version / warehouse_zone_id / type / actor / payload。
- 同一 SQLite 事务内完成 claim + 业务变更/ledger/outbox + completed result
- expected_version 过期直接 rejected（禁止 Last-Write-Wins）
- 返回 {"command_id", "status": accepted|rejected|completed, "result"|"error"}
  P0 全部同步执行：成功即 completed，领域错误即 rejected。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Callable, Dict

from pydantic import ValidationError

from unilabos.app.scheduler.inventory.domain import (
    InventoryError,
    MaterialRequirement,
)
from unilabos.app.scheduler.inventory.schemas import (
    JSON_OBJECT_ADAPTER,
    InventoryCommandBase,
    JsonObject,
    parse_inventory_command,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.utils.tracing import add_event, set_error, span

CommandHandler = Callable[[InventoryService, JsonObject], JsonObject]


def _serializable(value: Any) -> JsonObject:
    if isinstance(value, dict):
        candidate = value
    else:
        candidate = {"value": value}
    return JSON_OBJECT_ADAPTER.validate_python(candidate)


def _handle_template_upsert(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.upsert_template(
        template_id=p["template_id"],
        name=p.get("name", ""),
        category=p.get("category", ""),
        spec=p.get("spec"),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_template_delete(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.delete_template(
        template_id=p["template_id"],
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_inbound(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    if p.get("kind") == "instance":
        return svc.register_instance(
            template_id=p.get("template_id", ""),
            lot_id=p.get("lot_id", ""),
            barcode=p.get("barcode", ""),
            edge_uuid=p.get("edge_uuid", ""),
            legacy_cloud_id=p.get("legacy_cloud_id", "") or p.get("cloud_uuid", ""),
            parent_uuid=p.get("parent_uuid", ""),
            slot_id=p.get("slot_id", ""),
            actor=cmd.get("actor", ""),
            causation_id=cmd["command_id"],
        )
    return svc.inbound_lot(
        template_id=p.get("template_id", ""),
        quantity=float(p.get("quantity") or 0),
        unit=p.get("unit", ""),
        batch_no=p.get("batch_no", ""),
        expiry=p.get("expiry", ""),
        lot_id=p.get("lot_id", ""),
        warehouse_zone_id=cmd.get("warehouse_zone_id", ""),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
    )


def _handle_reserve(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    node_requirements = {
        node_id: [MaterialRequirement.from_dict(r) for r in reqs]
        for node_id, reqs in (p.get("node_requirements") or {}).items()
    }
    return svc.reserve_workflow(
        workflow_id=p["workflow_id"],
        node_requirements=node_requirements,
        attempt=int(p.get("attempt") or 1),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
    )


def _handle_release(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    if p.get("node_id"):
        return svc.release_reservation(
            workflow_id=p["workflow_id"],
            node_id=p["node_id"],
            attempt=int(p.get("attempt") or 1),
            reason=p.get("reason", "cloud_release"),
            actor=cmd.get("actor", ""),
            causation_id=cmd["command_id"],
        )
    return svc.release_workflow(
        workflow_id=p["workflow_id"],
        reason=p.get("reason", "cloud_release"),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
    )


def _handle_consume_reservation(
    svc: InventoryService, cmd: JsonObject
) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.consume_reservation(
        workflow_id=p["workflow_id"],
        node_id=p["node_id"],
        attempt=int(p.get("attempt") or 1),
        parent_uuid=p.get("parent_uuid", ""),
        slot_id=p.get("slot_id", ""),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
    )


def _handle_quarantine_reservation(
    svc: InventoryService, cmd: JsonObject
) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.quarantine_reservation(
        workflow_id=p["workflow_id"],
        node_id=p["node_id"],
        attempt=int(p.get("attempt") or 1),
        reason=p.get("reason", "command_quarantine"),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
    )


def _handle_deploy(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.deploy_instance(
        edge_uuid=p["edge_uuid"],
        parent_uuid=p.get("parent_uuid", ""),
        slot_id=p.get("slot_id", ""),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_move(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.move_instance(
        edge_uuid=p["edge_uuid"],
        parent_uuid=p["parent_uuid"],
        slot_id=p.get("slot_id", ""),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_detach(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.detach_instance(
        edge_uuid=p["edge_uuid"],
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_set_parent(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    """设父物料（parent_material_uuid ≡ 树父）；slot_id 为可选具名位（PLR site 名）。"""
    p = cmd.get("payload") or {}
    return svc.set_instance_parent(
        edge_uuid=p["edge_uuid"],
        parent_uuid=p.get("parent_uuid", ""),
        slot_id=p.get("slot_id"),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_content_set(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.update_content(
        instance_uuid=p["edge_uuid"],
        state=p.get("state") or {},
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_content_clear(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.clear_content(
        instance_uuid=p["edge_uuid"],
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_output_register(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.register_material_output(
        template_id=p["template_id"],
        content=p.get("content") or {},
        edge_uuid=p.get("edge_uuid") or "",
        barcode=p.get("barcode") or "",
        parent_uuid=p.get("parent_uuid") or "",
        slot_id=p.get("slot_id") or "",
        lot_id=p.get("lot_id") or "",
        task_id=p.get("task_id") or "",
        run_id=p.get("run_id") or "",
        action_id=p.get("action_id") or "",
        output_name=p.get("output_name") or "",
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
    )


def _handle_content_consume(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.consume_material_content(
        instance_uuid=p["edge_uuid"],
        quantity=float(p["quantity"]),
        quantity_key=p.get("quantity_key") or "quantity",
        unit=p.get("unit") or "",
        auto_terminal=bool(p.get("auto_terminal", True)),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
    )


def _handle_consume(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.consume_instance(
        edge_uuid=p["edge_uuid"],
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_discard(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.discard_instance(
        edge_uuid=p["edge_uuid"],
        reason=p.get("reason", ""),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


def _handle_adjust(svc: InventoryService, cmd: JsonObject) -> JsonObject:
    p = cmd.get("payload") or {}
    return svc.adjust_lot(
        lot_id=p["lot_id"],
        new_total=float(p["new_total"]),
        reason=p.get("reason", ""),
        actor=cmd.get("actor", ""),
        causation_id=cmd["command_id"],
        expected_version=cmd.get("expected_version"),
    )


COMMAND_HANDLERS: Dict[str, CommandHandler] = {
    "inventory.template.upsert": _handle_template_upsert,
    "inventory.template.delete": _handle_template_delete,
    "inventory.inbound": _handle_inbound,
    "inventory.reserve": _handle_reserve,
    "inventory.release": _handle_release,
    "inventory.consume": _handle_consume_reservation,
    "inventory.quarantine": _handle_quarantine_reservation,
    "material.deploy": _handle_deploy,
    "material.move": _handle_move,
    "material.detach": _handle_detach,
    "material.set_parent": _handle_set_parent,
    "material.content.set": _handle_content_set,
    "material.content.clear": _handle_content_clear,
    "material.output.register": _handle_output_register,
    "material.content.consume": _handle_content_consume,
    "material.consume": _handle_consume,
    "material.discard": _handle_discard,
    "material.adjust": _handle_adjust,
}


def _command_metadata(command: object) -> tuple[str, str, object]:
    if isinstance(command, InventoryCommandBase):
        return command.command_id, command.type, command.expected_version
    if isinstance(command, Mapping):
        return (
            str(command.get("command_id") or ""),
            str(command.get("type") or ""),
            command.get("expected_version"),
        )
    return "", "", None


def _replay_response(command_id: str, processed: Mapping[str, Any]) -> JsonObject:
    stored = JSON_OBJECT_ADAPTER.validate_python(
        json.loads(str(processed["result_json"]))
    )
    status = str(processed["status"])
    if status == "rejected":
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": str(stored.get("error") or "command rejected"),
            "error_code": str(stored.get("error_code") or "inventory_error"),
            "replayed": True,
        }
    if status == "completed":
        return {
            "command_id": command_id,
            "status": "completed",
            "result": stored,
            "replayed": True,
        }
    # ``accepted`` is only a transaction-local claim and must never survive a
    # clean commit.  Fail closed rather than risking a second execution.
    raise RuntimeError(f"incomplete persisted command claim: {command_id}")


def _execute_validated(
    service: InventoryService,
    parsed: InventoryCommandBase,
) -> JsonObject:
    """Claim, execute and persist one command in a single SQLite transaction."""

    command = parsed.model_dump(mode="python", exclude_none=True)
    command_id = parsed.command_id
    handler = COMMAND_HANDLERS[parsed.type]
    now_ms = int(time.time() * 1000)

    with service.command_transaction() as conn:
        processed = conn.execute(
            "SELECT * FROM processed_command WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if processed is not None:
            return _replay_response(command_id, dict(processed))

        # The accepted claim is intentionally invisible outside this write
        # transaction. A process crash rolls it back together with all domain
        # rows, ledger and outbox events.
        conn.execute(
            "INSERT INTO processed_command(command_id, result_json, status, processed_at) "
            "VALUES (?, '{}', 'accepted', ?)",
            (command_id, now_ms),
        )
        try:
            with service.command_attempt(conn):
                result = _serializable(handler(service, command))
        except InventoryError as exc:
            error = str(exc)
            error_code = getattr(exc, "code", "inventory_error")
            stored: JsonObject = {"error": error, "error_code": error_code}
            conn.execute(
                "UPDATE processed_command SET result_json = ?, status = 'rejected', "
                "processed_at = ? WHERE command_id = ?",
                (json.dumps(stored, ensure_ascii=False), now_ms, command_id),
            )
            return {
                "command_id": command_id,
                "status": "rejected",
                "error": error,
                "error_code": error_code,
            }
        except (KeyError, TypeError, ValueError) as exc:
            error = f"bad payload: {exc}"
            stored = {"error": error, "error_code": "bad_payload"}
            conn.execute(
                "UPDATE processed_command SET result_json = ?, status = 'rejected', "
                "processed_at = ? WHERE command_id = ?",
                (json.dumps(stored, ensure_ascii=False), now_ms, command_id),
            )
            return {
                "command_id": command_id,
                "status": "rejected",
                "error": error,
                "error_code": "bad_payload",
            }

        conn.execute(
            "UPDATE processed_command SET result_json = ?, status = 'completed', "
            "processed_at = ? WHERE command_id = ?",
            (json.dumps(result, ensure_ascii=False), now_ms, command_id),
        )
        return {"command_id": command_id, "status": "completed", "result": result}


def _execute_command(service: InventoryService, command: object) -> JsonObject:
    """Parse REST/WS input once, then execute atomically."""

    try:
        parsed = parse_inventory_command(command)
    except ValidationError as exc:
        command_id, _, _ = _command_metadata(command)
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": str(exc),
            "error_code": "validation_error",
        }
    return _execute_validated(service, parsed)


def execute_command(service: InventoryService, command: object) -> JsonObject:
    """带连续追踪的 REST/Cloud WS 共享命令入口；不记录 payload/actor 原文."""

    command_id, command_type, expected_version = _command_metadata(command)
    attributes = {
        "inventory.command.id": command_id,
        "inventory.command.type": command_type,
        "inventory.expected_version": expected_version,
        "edge.uuid": service.edge_id,
        "lab.id": service.lab_id,
    }
    with span(
        "inventory.command",
        attributes=attributes,
        kind="consumer",
    ) as command_span:
        response = _execute_command(service, command)
        status = str(response.get("status") or "")
        add_event(
            "inventory.command.result",
            {
                "inventory.command.status": status,
                "inventory.command.replayed": bool(response.get("replayed")),
                "error.type": response.get("error_code", ""),
            },
            span=command_span,
        )
        if status == "rejected":
            set_error(str(response.get("error") or "command rejected"), span=command_span)
        return response
