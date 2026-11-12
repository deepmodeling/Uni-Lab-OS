"""dev 独有的 ``/api/v1/materials/*`` 路由（V1 合同没有对应能力的部分）。

V1 已覆盖的能力改由 V1 路径提供，dev 旧路径删除（不保留双路径）：

- 全部 ``materials.v1`` 幂等变更（模板、批次入库 / 盘点 / 校正、预留与结算、建树、实例化、
  改标识 / 位置 / 数据、删除、移动、转运、快照应用、增量）→ ``POST /api/v1/inventory/commands``；
- 读：模板 → ``/api/v1/inventory/templates``、批次 → ``/inventory/lots``、预留 → ``/inventory/reservations``、
  物料聚合 → ``/inventory/instances``、拓扑边 → ``/inventory/relations``、账本 → ``/inventory/ledger``；
  V1 Backend DTO 的模板 / 物料见 ``/api/v1/resource-templates``、``/api/v1/materials``。

这里保留：按 Job 读预留、registry 资源类目录、按 resource_id 读物料、物料树快照、
库存预检、拓扑边写入、快照比对、设备通知、账本确认、物料 SSE、物理动作执行（``/executions``）。
响应走统一信封（资源口径），领域异常按 ``envelope.DOMAIN_ERRORS`` 换算业务码。
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
from unilabos.protocol.materials import InventoryRequirement, MaterialSnapshot
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


def _call(function, *args, **kwargs):
    """调用物料服务；领域异常原样上抛，由信封路由按 ``DOMAIN_ERRORS`` 换算业务码。"""

    return function(*args, **kwargs)


def create_materials_router(service: MaterialsService) -> APIRouter:
    router = envelope_router(prefix="/api/v1/materials", tags=["materials-v1"], profile=RESOURCE)
    from .execution import create_material_execution_router
    router.include_router(create_material_execution_router(service))

    @router.post("/inventory/preview")
    def preview_inventory(body: InventoryPreviewRequest):
        return _call(service.preview_inventory, body.requirements)

    @router.get("/reservations/by-job/{job_uuid}")
    async def get_inventory_reservation_by_job(job_uuid: str):
        return _call(service.get_inventory_reservation_by_job, job_uuid)

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

    @router.get("/instances/by-resource-id/{resource_id:path}")
    async def get_material_by_resource_id(resource_id: str):
        return _call(service.get_material_by_resource_id, resource_id)

    @router.get("/instances/{material_uuid}/tree")
    async def get_tree(material_uuid: str):
        return _call(service.get_tree, material_uuid)

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
    """dev 物料路由在前，V1 资源 / 内容物合同在后：V1 的 ``/materials/{material_uuid}``
    不能抢走 dev 的字面量路由（``/materials/registry-classes``、``/materials/events`` …）。"""

    from unilabos.server.api.materials.contents import create_v1_content_router
    from unilabos.server.api.materials.inventory import create_inventory_router
    from unilabos.server.api.materials.resources import create_v1_resource_router

    app.include_router(create_materials_router(service))
    app.include_router(create_inventory_router(service))
    app.include_router(create_v1_resource_router(service))
    app.include_router(create_v1_content_router(service))


__all__ = [
    "LedgerAcknowledge",
    "ResourceTreeNotify",
    "ResourceTreeNotifyResult",
    "create_materials_router",
    "install_materials_api",
]
