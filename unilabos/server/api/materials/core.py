"""``materials.v1`` FastAPI 路由；HTTP 仅做协议校验。

响应走统一信封（``api/envelope.py``，资源口径）；物料服务的领域异常直接抛给信封路由，
按 ``envelope.DOMAIN_ERRORS`` 换算业务码：物料 / 库位 / 模板不存在为 6000 / 6006 / 6001，
其余不存在 4001；库位占用 6007、未结算动作占用 6011、其余冲突 4002；校验失败 1000；
设备投影未收敛 5001。
"""

from __future__ import annotations

import json
from typing import Literal, Optional

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from unilabos.server.api.envelope import RESOURCE, envelope_router

from unilabos.server.database.tables.base import NonEmptyStr, ServerObject
from unilabos.server.lifecycle import shutting_down, sleep_unless_shutting_down
from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import (
    InventoryRequirement,
    InventoryStocktake,
    InventoryLotInbound,
    InventoryReservationCreate,
    InventoryReservationTransition,
    InventoryTaskReservationCreate,
    MaterialDataWrite,
    MaterialDelete,
    MaterialDelta,
    MaterialInstantiate,
    MaterialMove,
    MaterialPatch,
    MaterialPosition,
    MaterialSnapshot,
    MaterialTreeCreate,
    MaterialTransfer,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials import MaterialsService


class InventoryPreviewRequest(ServerObject):
    requirements: list[InventoryRequirement]


class LedgerAcknowledge(ServerObject):
    through_sequence: int = Field(ge=0)


class ResourceTreeNotify(ServerObject):
    """前端在权威完成物料变更后，请求 edge hostnode 把变更分发到目标设备。"""

    device_id: NonEmptyStr = Field(description="目标边缘设备 id（可对应 slave edge 上的设备）")
    action: Literal["add", "update", "remove"] = "add"
    resource_uuids: list[str] = Field(min_length=1)


class ResourceTreeNotifyResult(ServerObject):
    notified: Optional[bool] = Field(
        description="True=设备已确认投影；False=通知失败；null=设备未注册被跳过"
    )


class MaterialLinkUpsert(ServerObject):
    """拓扑边 upsert 请求：两端为已落权威的 material（含设备行）。"""

    source_material_uuid: NonEmptyStr
    target_material_uuid: NonEmptyStr
    link_type: str = ""
    source_handle: str = ""
    target_handle: str = ""
    extra: dict = Field(default_factory=dict)


def _payload(mutation: InventoryMutation, model: type):
    try:
        return model.model_validate(mutation.payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _call(function, *args, **kwargs):
    """调用物料服务；领域异常原样上抛，由信封路由按 ``DOMAIN_ERRORS`` 换算业务码。"""

    return function(*args, **kwargs)


def create_materials_router(service: MaterialsService) -> APIRouter:
    router = envelope_router(prefix="/api/v1/materials", tags=["materials-v1"], profile=RESOURCE)
    from .execution import create_material_execution_router
    router.include_router(create_material_execution_router(service))

    @router.put("/templates/{template_uuid}")
    async def put_template(template_uuid: str, mutation: InventoryMutation):
        value = _payload(mutation, ResourceTemplateWrite)
        if value.template_uuid != template_uuid:
            raise HTTPException(status_code=422, detail="template UUID path mismatch")
        return _call(service.put_template, mutation, value)

    @router.post("/templates")
    async def create_template(mutation: InventoryMutation):
        value = _payload(mutation, ResourceTemplateWrite)
        if value.template_uuid is not None:
            raise HTTPException(
                status_code=422,
                detail="POST template lets the materials authority allocate UUID",
            )
        return _call(service.put_template, mutation, value)

    @router.get("/templates")
    async def list_templates(
        name: Optional[str] = Query(
            default=None,
            description="按模板 name 精确筛选：存在性检查 / 按名取 uuid 只回 0 或 1 条。",
        ),
        include_definition: bool = Query(
            default=False,
            description=(
                "默认只返回名称 / uuid / 类型 / 分类 / 位点 / 版本 / definition_hash 等"
                "目录字段（前端选择器、存在性检查、变更判定够用）；true 时附带 registry "
                "全量 definition（全注册表可达十几 MB），只在确实要读 definition 正文时开。"
            ),
        ),
    ):
        # 筛选与目录模式都在 SQL 层完成：前端每隔几秒轮询一次，不能每次都反序列化
        # 并校验十几 MB 的 definition 再丢掉
        return _call(
            service.list_templates, name=name, include_definition=include_definition
        )

    @router.get("/templates/{template_uuid}")
    async def get_template(template_uuid: str):
        return _call(service.get_template, template_uuid)

    @router.delete("/templates/{template_uuid}")
    async def delete_template(template_uuid: str, mutation: InventoryMutation):
        expected = {"template_uuid": template_uuid}
        if mutation.payload and mutation.payload != expected:
            raise HTTPException(status_code=422, detail="template UUID path mismatch")
        mutation = mutation.model_copy(update={"payload": expected})
        return _call(service.delete_template, mutation, template_uuid)

    @router.post("/lots/inbound")
    async def inbound_inventory_lot(mutation: InventoryMutation):
        return _call(
            service.inbound_inventory_lot,
            mutation,
            _payload(mutation, InventoryLotInbound),
        )

    @router.get("/lots")
    async def list_inventory_lots(
        template_uuid: str | None = Query(default=None),
        unit: str | None = Query(default=None),
        include_quarantined: bool = Query(default=False),
    ):
        return _call(
            service.list_inventory_lots,
            template_uuid=template_uuid,
            unit=unit,
            include_quarantined=include_quarantined,
        )

    @router.post("/lots/{lot_uuid}/stocktake")
    def stocktake_inventory_lot(lot_uuid: str, mutation: InventoryMutation):
        value = _payload(mutation, InventoryStocktake)
        if value.lot_uuid != lot_uuid:
            raise HTTPException(status_code=422, detail="盘点批次与路径不一致")
        return _call(service.stocktake_inventory_lot, mutation, value)

    @router.get("/lots/{lot_uuid}")
    async def get_inventory_lot(lot_uuid: str):
        return _call(service.get_inventory_lot, lot_uuid)

    @router.post("/inventory/preview")
    def preview_inventory(body: InventoryPreviewRequest):
        return _call(service.preview_inventory, body.requirements)

    @router.post("/reservations")
    async def reserve_inventory(mutation: InventoryMutation):
        return _call(
            service.reserve_inventory,
            mutation,
            _payload(mutation, InventoryReservationCreate),
        )

    @router.post("/reservations/batch")
    async def reserve_task_inventory(mutation: InventoryMutation):
        return _call(
            service.reserve_task_inventory,
            mutation,
            _payload(mutation, InventoryTaskReservationCreate),
        )

    @router.get("/reservations")
    async def list_inventory_reservations(
        task_uuid: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ):
        return _call(
            service.list_inventory_reservations,
            task_uuid=task_uuid,
            status=status,
        )

    @router.get("/reservations/by-job/{job_uuid}")
    async def get_inventory_reservation_by_job(job_uuid: str):
        return _call(service.get_inventory_reservation_by_job, job_uuid)

    @router.get("/reservations/{reservation_uuid}")
    async def get_inventory_reservation(reservation_uuid: str):
        return _call(service.get_inventory_reservation, reservation_uuid)

    def reservation_transition(
        reservation_uuid: str,
        mutation: InventoryMutation,
        method,
    ):
        value = _payload(mutation, InventoryReservationTransition)
        if value.reservation_uuid != reservation_uuid:
            raise HTTPException(
                status_code=422,
                detail="inventory reservation UUID path mismatch",
            )
        return _call(method, mutation, value)

    @router.post("/reservations/{reservation_uuid}/consume")
    async def consume_inventory_reservation(
        reservation_uuid: str, mutation: InventoryMutation
    ):
        return reservation_transition(
            reservation_uuid,
            mutation,
            service.consume_inventory_reservation,
        )

    @router.post("/reservations/{reservation_uuid}/release")
    async def release_inventory_reservation(
        reservation_uuid: str, mutation: InventoryMutation
    ):
        return reservation_transition(
            reservation_uuid,
            mutation,
            service.release_inventory_reservation,
        )

    @router.post("/reservations/{reservation_uuid}/quarantine")
    async def quarantine_inventory_reservation(
        reservation_uuid: str, mutation: InventoryMutation
    ):
        return reservation_transition(
            reservation_uuid,
            mutation,
            service.quarantine_inventory_reservation,
        )

    @router.post("/trees")
    async def create_tree(mutation: InventoryMutation):
        return _call(
            service.create_tree,
            mutation,
            _payload(mutation, MaterialTreeCreate),
        )

    @router.get("/registry-classes")
    def list_registry_classes():
        """registry 可实例化资源类目录（前端出库选择器的数据源）。

        只列 pylabrobot 类型（可被 /instantiate 实例化）；显示名走 registry
        display_name 约定，缺省回退资源类 id。
        """
        from unilabos.registry.registry import lab_registry
        from unilabos.registry.utils.tools import resolve_registry_display_name

        items = []
        for resource_id, entry in lab_registry.resource_type_registry.items():
            if not isinstance(entry, dict):
                continue
            cls = entry.get("class")
            if not isinstance(cls, dict) or cls.get("type") != "pylabrobot":
                continue
            items.append(
                {
                    "registry_class": resource_id,
                    "display_name": resolve_registry_display_name(
                        entry.get("display_name"), resource_id
                    ),
                }
            )
        return sorted(items, key=lambda item: item["registry_class"])

    @router.post("/instantiate")
    def instantiate_material(mutation: InventoryMutation):
        """物料出库/实例化：按 registry 资源类实例化草稿 → 权威登记（权威发 uuid）。

        微前端出库入口——前端只提供「资源类 + 实例名」，实例化发生在微后端
        （Host 进程内已加载 registry/PLR）。产物以 ResourceSlot 引用 {id, uuid}
        写回动作参数（unilabos_deduct_resource 选择器），再由 apply_deduct_resource
        等动作消费。（def 端点走线程池，registry 实例化为阻塞调用。）
        """
        value = _payload(mutation, MaterialInstantiate)
        from pylabrobot.resources.resource import Resource as ResourcePLR

        from unilabos.resources.graphio import initialize_resource
        from unilabos.resources.adapters.plr_materials import plr_resources_to_create

        draft = initialize_resource(
            {"name": value.name, "class": value.registry_class},
            resource_type=ResourcePLR,
        )
        if not isinstance(draft, ResourcePLR):
            raise HTTPException(
                status_code=422,
                detail=f"registry 资源类不可实例化: {value.registry_class}",
            )
        # 底层落库统一走 create_material_tree；effect_key 保留调用方原值维持幂等。
        # payload 重绑为展开后的树（service 校验 payload 与 typed body 一致）。
        tree_create = plr_resources_to_create([draft])
        if value.barcode:
            # 条码只落在出库产物的根节点（子节点如 tip spot 不带条码）。
            for node in tree_create.nodes:
                if node.parent_client_ref is None:
                    node.identity.barcode = value.barcode
                    break
        create_mutation = mutation.model_copy(
            update={
                "operation": "create_material_tree",
                "payload": tree_create.model_dump(mode="json", exclude_none=False),
            }
        )
        return _call(service.create_tree, create_mutation, tree_create)

    @router.get("/instances")
    async def list_materials(
        roots_only: bool = Query(default=False),
        name: Optional[str] = Query(default=None, description="按名称精确搜索；未命中返回 []"),
    ):
        if name is not None:
            return _call(service.search_materials, name)
        return _call(service.list_materials, roots_only=roots_only)

    @router.get("/instances/by-resource-id/{resource_id:path}")
    async def get_material_by_resource_id(resource_id: str):
        return _call(service.get_material_by_resource_id, resource_id)

    @router.get("/instances/{material_uuid}")
    async def get_material(material_uuid: str):
        return _call(service.get_material, material_uuid)

    @router.get("/instances/{material_uuid}/tree")
    async def get_tree(material_uuid: str):
        return _call(service.get_tree, material_uuid)

    @router.patch("/instances/{material_uuid}")
    async def patch_material(material_uuid: str, mutation: InventoryMutation):
        return _call(
            service.patch_material,
            mutation,
            material_uuid,
            _payload(mutation, MaterialPatch),
        )

    @router.put("/instances/{material_uuid}/position")
    async def put_position(material_uuid: str, mutation: InventoryMutation):
        return _call(
            service.put_position,
            mutation,
            material_uuid,
            _payload(mutation, MaterialPosition),
        )

    @router.put("/instances/{material_uuid}/data")
    async def put_data(material_uuid: str, mutation: InventoryMutation):
        return _call(
            service.put_data,
            mutation,
            material_uuid,
            _payload(mutation, MaterialDataWrite),
        )

    @router.delete("/instances/{material_uuid}")
    async def delete_material(material_uuid: str, mutation: InventoryMutation):
        value = _payload(mutation, MaterialDelete)
        if value.material_uuid != material_uuid:
            raise HTTPException(status_code=422, detail="material UUID path mismatch")
        return _call(service.delete_material, mutation, value)

    @router.post("/move")
    async def move_material(mutation: InventoryMutation):
        return _call(
            service.move_material,
            mutation,
            _payload(mutation, MaterialMove),
        )

    @router.post("/transfer")
    def transfer_material(mutation: InventoryMutation):
        """transfer 提交权威位置后同步等设备完成 unload/load 投影；设备在此期间会回头
        读权威（tree.get），所以必须走线程池（def），不能占住事件循环。"""

        return _call(
            service.transfer_material,
            mutation,
            _payload(mutation, MaterialTransfer),
        )

    @router.get("/links")
    async def list_links(
        material_uuid: str = Query(default=""),
        source_material_uuid: str = Query(default=""),
        target_material_uuid: str = Query(default=""),
        link_type: Optional[str] = Query(default=None),
    ):
        """拓扑边查询：物料/设备节点间的连接关系（node-link 的 link 行）。"""
        return _call(
            service.list_links,
            material_uuid=material_uuid or None,
            source_material_uuid=source_material_uuid or None,
            target_material_uuid=target_material_uuid or None,
            link_type=link_type,
        )

    @router.post("/links")
    async def upsert_link(value: MaterialLinkUpsert):
        """拓扑边 upsert：同两端/handle/类型的边身份稳定，重复提交幂等。"""
        return _call(
            service.upsert_link,
            source_material_uuid=value.source_material_uuid,
            target_material_uuid=value.target_material_uuid,
            link_type=value.link_type,
            source_handle=value.source_handle,
            target_handle=value.target_handle,
            extra=value.extra,
        )

    @router.delete("/links/{link_uuid}")
    async def delete_link(link_uuid: str):
        removed = _call(service.delete_link_record, link_uuid)
        if not removed:
            raise HTTPException(status_code=404, detail=f"link not found: {link_uuid}")
        return {"deleted": True}

    @router.post("/snapshots/compare")
    async def compare_snapshot(snapshot: MaterialSnapshot):
        return _call(service.compare_snapshot, snapshot)

    @router.post("/snapshots/apply")
    async def apply_snapshot(mutation: InventoryMutation):
        return _call(
            service.apply_snapshot,
            mutation,
            _payload(mutation, MaterialSnapshot),
        )

    @router.post("/snapshots/delta")
    async def apply_delta(mutation: InventoryMutation):
        """设备增量上报：只带变了的节点 / 段，权威按段合并（乐观锁在节点内）。"""
        return _call(
            service.apply_delta,
            mutation,
            _payload(mutation, MaterialDelta),
        )

    @router.post("/notify-device", response_model=ResourceTreeNotifyResult)
    def notify_device(value: ResourceTreeNotify):
        """把权威已完成的物料变更分发到目标设备（本进程直调 / 跨机 HostLink）。

        物料创建/变更只发生在微后端；设备侧投影由模块级
        downlink.notify_resource_tree_update 触发（add=拉取实例化+assign，
        remove=卸载移除），不经任何 host 编排类。同步等待设备回执；调用方
        应校验 notified 为 true。（def 端点走线程池，允许阻塞等待。）
        """
        from unilabos.backend.hostlink.downlink import notify_resource_tree_update

        notified = notify_resource_tree_update(
            value.device_id, value.action, list(value.resource_uuids)
        )
        return ResourceTreeNotifyResult(notified=notified)

    @router.get("/changes")
    async def changes(
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return service.changes(after_sequence=after_sequence, limit=limit)

    @router.post("/changes/ack")
    async def acknowledge_changes(value: LedgerAcknowledge):
        return {"acknowledged": service.acknowledge_changes(value.through_sequence)}

    @router.get("/events")
    async def events(
        request: Request,
        last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        """物料变更 SSE 失效通知（与 workflow ``/api/v1/events`` 同范式）。

        以 inventory_ledger 的 sequence 为游标推送 ``materials.changed``；
        浏览器只把事件当失效信号并回 HTTP 重取，payload 不承载业务正文。
        首连（无 Last-Event-ID）静默追平账本尾部，只推送此后的增量；
        重连带 Last-Event-ID 时从该游标续传，补齐离线期间的变更。
        """
        replay = False
        cursor = 0
        if last_event_id is not None and last_event_id.strip():
            try:
                cursor = int(last_event_id.strip())
                if cursor < 0:
                    raise ValueError
                replay = True
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid Last-Event-ID")

        if not replay:
            # 必须在发出响应头/onopen 之前固定基线。否则浏览器已校准 HTTP 快照后，
            # 首连再追平账本会吞掉这期间的创建/移动；用 MAX 查询也避免全量扫描历史。
            cursor = service.latest_ledger_sequence()

        def _format_event(row) -> str:
            data = json.dumps(
                {
                    "sequence": row.sequence,
                    "operation": row.operation,
                    "aggregate_type": row.aggregate_type,
                    "aggregate_uuid": row.aggregate_uuid,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return f"id: {row.sequence}\nevent: materials.changed\ndata: {data}\n\n"

        async def stream():
            nonlocal cursor
            yield "retry: 3000\n: connected\n\n"
            # 停机开始就主动结束：浏览器按 retry 自动重连，uvicorn 不必超时取消
            while not shutting_down() and not await request.is_disconnected():
                rows = service.changes(after_sequence=cursor, limit=500)
                for row in rows:
                    cursor = row.sequence
                    yield _format_event(row)
                if not rows:
                    yield ": keepalive\n\n"
                if not await sleep_unless_shutting_down(1):
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def install_materials_api(app: FastAPI, service: MaterialsService) -> None:
    app.include_router(create_materials_router(service))


__all__ = [
    "LedgerAcknowledge",
    "ResourceTreeNotify",
    "ResourceTreeNotifyResult",
    "create_materials_router",
    "install_materials_api",
]
