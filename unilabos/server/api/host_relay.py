"""Host 进程的设备下行中继：让不带设备的调度权威把物料投影送到设备。

调度权威（materials 权威）在完成 transfer / 前端物料变更后，要把 unload / load 或
资源树变更投影到设备台面；设备（本进程或经 HostLink 的 Slave）只有 Host 进程能碰到。
权威通过 ``downlink.configure_remote_device_relay`` 把这两类下行改成对本路由的 HTTP
调用。控制面内部接口，不进 OpenAPI，浏览器不该调用。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from unilabos.server.api.envelope import envelope_router
from unilabos.protocol.materials import MaterialDeviceSync
from unilabos.server.api.materials.core import ResourceTreeNotify, ResourceTreeNotifyResult


def create_host_relay_router() -> APIRouter:
    router = envelope_router(prefix="/api/v1/hostlink", tags=["hostlink"], include_in_schema=False)

    @router.post("/material-sync")
    def material_sync(command: MaterialDeviceSync) -> Dict[str, Any]:
        """微后端 transfer 的设备侧投影（unload / load）：本进程直调，跨机经 HostLink。"""

        from unilabos.backend.hostlink.downlink import material_sync_to_device

        try:
            result = material_sync_to_device(
                command.device_id, command.model_dump(mode="json", exclude_none=False)
            )
        except Exception as exc:  # noqa: BLE001 - 设备侧失败原样告知权威
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not isinstance(result, dict) or not result.get("success"):
            raise HTTPException(status_code=409, detail=f"设备 {command.device_id!r} material_sync 失败：{result}")
        return result

    @router.post("/notify-device", response_model=ResourceTreeNotifyResult)
    def notify_device(value: ResourceTreeNotify) -> ResourceTreeNotifyResult:
        """权威已完成的物料变更分发到目标设备（add / update / remove）。"""

        from unilabos.backend.hostlink.downlink import notify_resource_tree_update

        notified = notify_resource_tree_update(
            value.device_id, value.action, list(value.resource_uuids)
        )
        return ResourceTreeNotifyResult(notified=notified)

    return router


__all__ = ["create_host_relay_router"]
