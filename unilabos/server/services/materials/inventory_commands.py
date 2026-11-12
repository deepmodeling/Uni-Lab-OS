"""V1 ``POST /api/v1/inventory/commands``：库存写入的统一命令入口。

两类命令共用这一个入口：

- **dev ``materials.v1`` 变更**（``protocol_version == "materials.v1"``）：原 dev 按操作分路由的
  幂等变更（建树、改数据、移动、转运、快照、模板、批次、预留 …）。按 ``command_uuid`` +
  ``effect_key`` 幂等，结果是 :class:`MutationResult`。``patch_material`` / ``put_position`` /
  ``put_data`` 需要的物料身份放在命令顶层 ``target_uuid``（原路径参数）。
- **V1 类型化命令**（``type`` + ``command_id``）：``reagent.dispense``、``inventory.*``、
  ``material.*`` 以及本分支新增的 ``inventory.lot.correct``、``content.*``、``source.*``。
  按 ``command_id`` 幂等，首次结果（完成或拒绝）存在 ``inventory_processed_command``。

响应保持 V1 的裸结果 ``{command_id, status, result | error, error_code, replayed}``（V1 控制台按
``status`` 判断，不经信封）；拒绝时另带 ``code``（V1 业务码）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional
from uuid import NAMESPACE_URL, uuid5

from unilabos.protocol.base import canonical_hash
from unilabos.protocol.materials import (
    InventoryLotCorrection,
    InventoryLotInbound,
    InventoryMutation,
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationTransition,
    InventoryStocktake,
    InventoryTaskReservationCreate,
    MaterialDataWrite,
    MaterialDelete,
    MaterialDelta,
    MaterialInstantiate,
    MaterialMove,
    MaterialPatch,
    MaterialPosition,
    MaterialSnapshot,
    MaterialTransfer,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.api.v1_fields import iso_to_ms
from unilabos.server.services.materials.chemicals import ChemicalError
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.core import (
    MaterialConflictError,
    MaterialNotFoundError,
    MaterialsService,
    MaterialsServiceError,
    MaterialValidationError,
)
from unilabos.server.services.materials.resource_contract import new_mutation

_JOB_NAMESPACE = uuid5(NAMESPACE_URL, "unilabos://v1-inventory-reservations")
CodeOf = Callable[[BaseException], int]


class UnsupportedCommand(MaterialValidationError):
    code = "unsupported_command"


def instantiate_tree(value: MaterialInstantiate) -> MaterialTreeCreate:
    """registry 资源类 → 物料创建树（Host 进程内已加载 registry / PLR）。"""

    from pylabrobot.resources.resource import Resource as ResourcePLR

    from unilabos.resources.adapters.plr_materials import plr_resources_to_create
    from unilabos.resources.graphio import initialize_resource

    draft = initialize_resource({"name": value.name, "class": value.registry_class}, resource_type=ResourcePLR)
    if not isinstance(draft, ResourcePLR):
        raise MaterialValidationError(f"registry 资源类不可实例化: {value.registry_class}")
    tree = plr_resources_to_create([draft])
    if value.barcode:
        # 条码只落在出库产物的根节点（子节点如 tip spot 不带条码）
        for node in tree.nodes:
            if node.parent_client_ref is None:
                node.identity.barcode = value.barcode
                break
    return tree


class InventoryCommands:
    """统一库存命令入口的执行器；返回值即 HTTP 响应体（V1 裸结果）。"""

    def __init__(self, materials: MaterialsService, *, contents: Optional[ContentContract] = None) -> None:
        self.materials = materials
        self.contents = contents or ContentContract(materials, compound_source=None)

    # ── dev materials.v1 变更 ────────────────────────────────────

    def _dev_handlers(self) -> dict[str, tuple[type, Callable[..., Any], bool]]:
        service = self.materials
        return {
            # operation: (载荷模型, 执行函数, 是否需要 target_uuid)
            "put_template": (ResourceTemplateWrite, service.put_template, False),
            "create_template": (ResourceTemplateWrite, service.put_template, False),
            "sync_template": (ResourceTemplateWrite, service.put_template, False),
            "inbound_inventory_lot": (InventoryLotInbound, service.inbound_inventory_lot, False),
            "stocktake_inventory_lot": (InventoryStocktake, service.stocktake_inventory_lot, False),
            "correct_inventory_lot": (InventoryLotCorrection, service.correct_inventory_lot, False),
            "reserve_inventory": (InventoryReservationCreate, service.reserve_inventory, False),
            "reserve_task_inventory": (InventoryTaskReservationCreate, service.reserve_task_inventory, False),
            "consume_inventory_reservation": (InventoryReservationTransition, service.consume_inventory_reservation, False),
            "release_inventory_reservation": (InventoryReservationTransition, service.release_inventory_reservation, False),
            "quarantine_inventory_reservation": (InventoryReservationTransition, service.quarantine_inventory_reservation, False),
            "create_material": (MaterialTreeCreate, service.create_tree, False),
            "create_material_tree": (MaterialTreeCreate, service.create_tree, False),
            "patch_material": (MaterialPatch, service.patch_material, True),
            "update_material": (MaterialPatch, service.patch_material, True),
            "put_position": (MaterialPosition, service.put_position, True),
            "update_position": (MaterialPosition, service.put_position, True),
            "put_data": (MaterialDataWrite, service.put_data, True),
            "update_data": (MaterialDataWrite, service.put_data, True),
            "update_substances": (MaterialDataWrite, service.put_data, True),
            "delete_material": (MaterialDelete, service.delete_material, False),
            "move_material": (MaterialMove, service.move_material, False),
            "transfer_material": (MaterialTransfer, service.transfer_material, False),
            "apply_material_snapshot": (MaterialSnapshot, service.apply_snapshot, False),
            "apply_material_delta": (MaterialDelta, service.apply_delta, False),
        }

    def execute_mutation(self, body: Mapping[str, Any]) -> Any:
        """执行一条 dev 变更；异常原样上抛（由调用方按业务码组装拒绝结果）。"""

        values = dict(body)
        target_uuid = values.pop("target_uuid", None)
        try:
            mutation = InventoryMutation.model_validate(values)
        except ValueError as exc:
            raise MaterialValidationError(f"invalid materials.v1 mutation: {exc}") from None
        operation = mutation.operation
        if operation == "delete_template":
            template_uuid = str(mutation.payload.get("template_uuid") or "")
            if not template_uuid:
                raise MaterialValidationError("delete_template payload requires template_uuid")
            return self.materials.delete_template(mutation, template_uuid)
        if operation == "instantiate_material":
            try:
                value = MaterialInstantiate.model_validate(mutation.payload)
            except ValueError as exc:
                raise MaterialValidationError(str(exc)) from None
            tree = instantiate_tree(value)
            # 实例化展开为建树：effect_key 保持调用方原值维持幂等，载荷重绑为展开后的树
            create = mutation.model_copy(update={
                "operation": "create_material_tree",
                "payload": tree.model_dump(mode="json", exclude_none=False),
            })
            return self.materials.create_tree(create, tree)
        handler = self._dev_handlers().get(operation)
        if handler is None:
            raise UnsupportedCommand(f"unsupported materials.v1 operation: {operation}")
        model, function, needs_target = handler
        try:
            value = model.model_validate(mutation.payload)
        except ValueError as exc:
            raise MaterialValidationError(str(exc)) from None
        if needs_target:
            if not target_uuid:
                raise MaterialValidationError(f"{operation} requires target_uuid")
            return function(mutation, str(target_uuid), value)
        return function(mutation, value)

    def dev_command(self, body: Mapping[str, Any], code_of: CodeOf) -> dict[str, Any]:
        command_id = str(body.get("command_uuid") or "")
        try:
            result = self.execute_mutation(body)
        except (MaterialsServiceError, ChemicalError) as exc:
            return self._rejected(command_id, exc, code_of)
        data = result.model_dump(mode="json", exclude_none=False)
        return {"command_id": command_id, "status": "completed", "result": data, "replayed": bool(data.get("replayed"))}

    # ── V1 类型化命令 ────────────────────────────────────────────

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        code = str(getattr(exc, "code", "") or "inventory_error")
        qualifier = getattr(exc, "subject", None) or getattr(exc, "conflict", None)
        return f"{code}:{qualifier}" if qualifier else code

    def _rejected(self, command_id: str, exc: BaseException, code_of: CodeOf) -> dict[str, Any]:
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": getattr(exc, "message", None) or str(exc) or "command rejected",
            "error_code": self._error_code(exc),
            "code": code_of(exc),
        }

    def _replay(self, row: Any) -> dict[str, Any]:
        if row["status"] == "completed":
            return {"command_id": row["command_id"], "status": "completed",
                    "result": json.loads(row["result_json"] or "{}"), "replayed": True}
        return {"command_id": row["command_id"], "status": "rejected", "error": row["error_message"] or "command rejected",
                "error_code": row["error_code"] or "inventory_error", "code": row["business_code"], "replayed": True}

    def typed_command(self, body: Mapping[str, Any], code_of: CodeOf) -> dict[str, Any]:
        command_id = str(body.get("command_id") or "").strip()
        command_type = str(body.get("type") or "").strip()
        if not command_id or not command_type:
            raise MaterialValidationError("command_id and type are required")
        actor = str(body.get("actor") or "")
        request_hash = canonical_hash({key: value for key, value in body.items() if key != "actor"})
        connection = self.materials.connection
        handler = self._typed_handlers().get(command_type)
        try:
            with self.materials.write():
                existing = connection.execute(
                    "SELECT * FROM inventory_processed_command WHERE command_id=?", (command_id,)
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise MaterialConflictError("command_id was already used with another request", conflict="state")
                    return self._replay(existing)
                if handler is None:
                    raise UnsupportedCommand(f"unsupported inventory command type: {command_type}")
                result = handler(dict(body.get("payload") or {}), command_id=command_id, actor=actor,
                                 expected_version=body.get("expected_version"))
                connection.execute(
                    "INSERT INTO inventory_processed_command(command_id, command_type, request_hash, status, result_json, "
                    "actor, processed_at_ms) VALUES (?,?,?,'completed',?,?,?)",
                    (command_id, command_type, request_hash, json.dumps(result, ensure_ascii=False), actor,
                     self.materials._now_ms()),
                )
        except (MaterialsServiceError, ChemicalError) as exc:
            rejected = self._rejected(command_id, exc, code_of)
            with self.materials.write():
                connection.execute(
                    "INSERT OR IGNORE INTO inventory_processed_command(command_id, command_type, request_hash, status, "
                    "result_json, error_code, error_message, business_code, actor, processed_at_ms) "
                    "VALUES (?,?,?,'rejected','{}',?,?,?,?,?)",
                    (command_id, command_type, request_hash, rejected["error_code"], rejected["error"], rejected["code"],
                     actor, self.materials._now_ms()),
                )
            return rejected
        return {"command_id": command_id, "status": "completed", "result": result, "replayed": False}

    def _typed_handlers(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {
            "reagent.dispense": self._reagent_dispense,
            "inventory.template.upsert": self._template_upsert,
            "inventory.template.delete": self._template_delete,
            "inventory.inbound": self._inbound,
            "inventory.reserve": self._reserve,
            "inventory.release": lambda payload, **kw: self._transition(payload, "release", **kw),
            "inventory.consume": lambda payload, **kw: self._transition(payload, "consume", **kw),
            "inventory.quarantine": lambda payload, **kw: self._transition(payload, "quarantine", **kw),
            "inventory.reconcile_count": self._reconcile_count,
            "inventory.lot.correct": self._lot_correct,
            "material.move": self._material_move,
            "material.detach": self._material_detach,
            "material.set_parent": self._material_set_parent,
            "material.discard": self._material_discard,
            "content.reserve": lambda payload, **kw: self.contents.reserve_content(payload),
            "content.release": lambda payload, **kw: self.contents.finish_content_reservation(
                str(payload.get("reservation_uuid") or ""), "released"),
            "content.consume": lambda payload, **kw: self.contents.finish_content_reservation(
                str(payload.get("reservation_uuid") or ""), "consumed", consumed_quantity=payload.get("quantity")),
            "source.bind": lambda payload, **kw: self.contents.bind_material_source(payload),
            "source.release": lambda payload, **kw: {"released": self.contents.release_material_sources(
                str(payload.get("task_uuid") or payload.get("workflow_id") or ""), node_uuid=payload.get("node_uuid"))},
        }

    def _mutation(self, operation: str, actor: str) -> InventoryMutation:
        return new_mutation(operation, actor_type="human", actor_uuid=actor or "inventory.command")

    def _reagent_dispense(self, payload: Mapping[str, Any], *, command_id: str, actor: str, **_: Any) -> dict[str, Any]:
        return self.contents.dispense_reagent(payload, command_id=command_id, actor=actor)

    def _template(self, identity: str) -> Any:
        try:
            return self.materials.get_template(identity)
        except MaterialNotFoundError:
            template = self.materials.get_template_by_name(identity)
            if template is None:
                raise MaterialNotFoundError(f"template not found: {identity}", subject="template") from None
            return self.materials.get_template(template.template_uuid)

    def _template_upsert(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        identity = str(payload.get("template_id") or "").strip()
        if not identity:
            raise MaterialValidationError("template_id is required")
        try:
            current = self._template(identity)
        except MaterialNotFoundError:
            current = None
        spec = dict(payload.get("spec") or {})
        write = ResourceTemplateWrite(
            template_uuid=current.template_uuid if current else None,
            name=current.name if current else identity,
            display_name=str(payload.get("name") or (current.display_name if current else identity)),
            resource_type=current.resource_type if current else str(payload.get("category") or "resource"),
            class_name=current.class_name if current else None,
            category=[str(payload["category"])] if payload.get("category") else (current.category if current else []),
            available_sites=current.available_sites if current else [],
            handles=current.handles if current else [],
            definition={**(current.definition if current else {}), **spec},
        )
        result = self.materials.put_template(self._mutation("put_template", actor), write)
        return result.data.model_dump(mode="json")

    def _template_delete(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        template = self._template(str(payload.get("template_id") or ""))
        self.materials.delete_template(
            new_mutation("delete_template", actor_uuid=actor or None, payload={"template_uuid": template.template_uuid}),
            template.template_uuid,
        )
        return {"template_id": template.template_uuid, "deleted": True}

    def _inbound(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        if str(payload.get("kind") or "lot") != "lot":
            raise UnsupportedCommand("instance inbound is created through POST /api/v1/materials")
        template = self._template(str(payload.get("template_id") or ""))
        try:
            expiry = iso_to_ms(payload.get("expiry")) if payload.get("expiry") else None
        except ValueError:
            raise MaterialValidationError("expiry must be ISO 8601") from None
        value = InventoryLotInbound(
            lot_uuid=payload.get("lot_id") or None, template_uuid=template.template_uuid,
            batch_no=str(payload.get("batch_no") or ""), unit=str(payload.get("unit") or "count"),
            quantity=float(payload.get("quantity") or 0), expiry_at_ms=expiry,
            location_site_uuid=payload.get("location_site_uuid") or None,
            warehouse_zone_id=str(payload.get("warehouse_zone_id") or ""),
        )
        lot = self.materials.inbound_inventory_lot(self._mutation("inbound_inventory_lot", actor), value).data
        return {**lot.model_dump(mode="json"), "lot_id": lot.lot_uuid, "template_id": lot.template_uuid}

    def _reserve(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if not workflow_id:
            raise MaterialValidationError("workflow_id is required")
        attempt = int(payload.get("attempt") or 1)
        reservations = []
        for node_id, requirements in dict(payload.get("node_requirements") or {}).items():
            items = []
            for index, raw in enumerate(requirements or []):
                item = dict(raw)
                if item.get("instance_uuid") or item.get("barcode"):
                    material_uuid = item.get("instance_uuid")
                    if not material_uuid:
                        row = self.materials.connection.execute(
                            "SELECT material_uuid FROM material WHERE barcode=? AND deleted_at_ms IS NULL", (item["barcode"],)
                        ).fetchone()
                        if row is None:
                            raise MaterialNotFoundError(f"material not found: barcode {item['barcode']}", subject="material")
                        material_uuid = row[0]
                    items.append(InventoryRequirement(key=f"{node_id}:{index}", kind="material", material_uuid=material_uuid))
                else:
                    template_uuid = self._template(item["template_id"]).template_uuid if item.get("template_id") else None
                    items.append(InventoryRequirement(
                        key=f"{node_id}:{index}", kind="lot", template_uuid=template_uuid,
                        lot_uuid=item.get("lot_id") or None, quantity=float(item.get("quantity") or 0) or None,
                        unit=item.get("unit") or None,
                    ))
            reservations.append(InventoryReservationCreate(
                task_uuid=workflow_id, node_uuid=str(node_id),
                job_uuid=str(uuid5(_JOB_NAMESPACE, f"{workflow_id}:{node_id}:{attempt}")),
                scheduler_revision=attempt, requirements=items,
            ))
        if not reservations:
            raise MaterialValidationError("node_requirements is required")
        result = self.materials.reserve_task_inventory(
            self._mutation("reserve_task_inventory", actor),
            InventoryTaskReservationCreate(task_uuid=workflow_id, scheduler_revision=attempt, reservations=reservations),
        )
        return {"workflow_id": workflow_id, "attempt": attempt, **result.data.model_dump(mode="json")}

    def _transition(self, payload: Mapping[str, Any], action: str, *, actor: str, **_: Any) -> dict[str, Any]:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if not workflow_id:
            raise MaterialValidationError("workflow_id is required")
        node_id = str(payload.get("node_id") or "")
        active = [
            item for item in self.materials.list_inventory_reservations(task_uuid=workflow_id, status="active")
            if not node_id or item.node_uuid == node_id
        ]
        if not active and action != "release":
            raise MaterialNotFoundError(f"no active reservation for workflow {workflow_id}", subject="reservation")
        method = {"release": self.materials.release_inventory_reservation,
                  "consume": self.materials.consume_inventory_reservation,
                  "quarantine": self.materials.quarantine_inventory_reservation}[action]
        done = []
        for item in active:
            value = InventoryReservationTransition(reservation_uuid=item.reservation_uuid, reason=str(payload.get("reason") or ""))
            done.append(method(self._mutation(f"{action}_inventory_reservation", actor), value).data.model_dump(mode="json"))
        return {"workflow_id": workflow_id, "reservations": done}

    def _reconcile_count(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None, **_: Any) -> dict[str, Any]:
        lot = self.materials.get_inventory_lot(str(payload.get("lot_id") or ""))
        value = InventoryStocktake(
            lot_uuid=lot.lot_uuid, expected_version=int(payload.get("expected_version") or expected_version or lot.version),
            measured_count=int(payload.get("measured_count", payload.get("count", 0))),
            reason=str(payload.get("reason") or "reconcile_count"),
        )
        return self.materials.stocktake_inventory_lot(self._mutation("stocktake_inventory_lot", actor), value).data.model_dump(mode="json")

    def _lot_correct(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None, **_: Any) -> dict[str, Any]:
        values = dict(payload)
        values.setdefault("lot_uuid", values.pop("lot_id", None))
        if values.get("expected_version") is None and expected_version is not None:
            values["expected_version"] = expected_version
        try:
            value = InventoryLotCorrection.model_validate(values)
        except ValueError as exc:
            raise MaterialValidationError(str(exc)) from None
        return self.materials.correct_inventory_lot(self._mutation("correct_inventory_lot", actor), value).data.model_dump(mode="json")

    def _move(self, move: MaterialMove, actor: str) -> dict[str, Any]:
        return self.materials.move_material(self._mutation("move_material", actor), move).data.model_dump(mode="json")

    def _material_move(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        material_uuid = str(payload.get("edge_uuid") or payload.get("material_uuid") or "")
        site_uuid = payload.get("site_uuid") or None
        if not site_uuid and payload.get("parent_uuid") and payload.get("slot_id"):
            site = self.materials.connection.execute(
                "SELECT site_uuid FROM site WHERE owner_material_uuid=? AND (label=? OR CAST(site_index AS TEXT)=?) "
                "AND deleted_at_ms IS NULL", (payload["parent_uuid"], payload["slot_id"], str(payload["slot_id"])),
            ).fetchone()
            if site is None:
                raise MaterialNotFoundError(f"site not found: {payload['parent_uuid']}/{payload['slot_id']}", subject="site")
            site_uuid = site[0]
        return self._move(MaterialMove(material_uuid=material_uuid, destination_site_uuid=site_uuid,
                                       parent_material_uuid=None if site_uuid else payload.get("parent_uuid") or None), actor)

    def _material_detach(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        return self._move(MaterialMove(material_uuid=str(payload.get("edge_uuid") or payload.get("material_uuid") or "")), actor)

    def _material_set_parent(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        return self._material_move(payload, actor=actor)

    def _material_discard(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        value = MaterialDelete(material_uuid=str(payload.get("edge_uuid") or payload.get("material_uuid") or ""),
                               recursive=bool(payload.get("recursive", True)))
        return self.materials.delete_material(
            new_mutation("delete_material", actor_uuid=actor or None, payload=value.model_dump(mode="json")), value
        ).data.model_dump(mode="json")

    # ── 入口 ──────────────────────────────────────────────────

    def execute(self, body: Mapping[str, Any], code_of: CodeOf) -> dict[str, Any]:
        if body.get("protocol_version") == "materials.v1" or "command_uuid" in body:
            return self.dev_command(body, code_of)
        return self.typed_command(body, code_of)

    def processed(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.materials.connection.execute(
            "SELECT command_id, command_type, status, error_code, business_code, actor, processed_at_ms "
            "FROM inventory_processed_command ORDER BY processed_at_ms DESC, command_id LIMIT ?", (limit,),
        ).fetchall()
        commands = [{**{key: row[key] for key in row.keys()}, "type": row["command_type"], "family": "v1"} for row in rows]
        effects = self.materials.connection.execute(
            "SELECT command_uuid, effect_key, operation, status, error_code, completed_at_ms, updated_at_ms "
            "FROM inventory_command_effect ORDER BY updated_at_ms DESC, command_uuid LIMIT ?", (limit,),
        ).fetchall()
        commands += [{
            "command_id": row["command_uuid"], "effect_key": row["effect_key"], "type": row["operation"],
            "status": {"applied": "completed"}.get(row["status"], row["status"]), "error_code": row["error_code"],
            "processed_at_ms": row["completed_at_ms"] or row["updated_at_ms"], "family": "materials.v1",
        } for row in effects]
        commands.sort(key=lambda item: item["processed_at_ms"] or 0, reverse=True)
        return commands[:limit]


__all__ = ["InventoryCommands", "UnsupportedCommand", "instantiate_tree"]
