"""V1 ``/api/v1/device-state*``：设备属性当前值与变化轨迹（Host 数据面，经 edge 代理转发）。

数据仍在 dev 的 ``telemetry.db``：执行面 ``TelemetryDeviceStateProjection``（``execution_backend.device_state``）
已提供 V1 设备状态存储的 ``latest_all / latest_for / history / history_all / set / stats`` 形状，
这里只是 V1 路径与 DTO 的薄适配；dev 的 ``/api/v1/telemetry/*`` 仍是完整的遥测查询面。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from unilabos.server.api.envelope import envelope_router


class DeviceStateReportIn(BaseModel):
    """V1 上报入口：ROS / HostLink 之外的设备（或调试）直接写标量属性。"""

    device_id: str = Field(min_length=1)
    properties: Dict[str, Any]


def create_device_state_router(get_execution_backend: Callable[[], Any]) -> APIRouter:
    router = envelope_router(tags=["device-state"])

    def store() -> Any:
        backend = get_execution_backend()
        projection = getattr(backend, "device_state", None) if backend is not None else None
        if projection is None:
            raise HTTPException(status_code=503, detail="设备状态存储未启用（当前进程没有执行面）")
        return projection

    @router.get("/device-state")
    def device_state_all() -> Dict[str, Any]:
        """全量设备属性当前值 + 存储统计。"""

        projection = store()
        return {"devices": projection.latest_all(), "stats": projection.stats()}

    @router.get("/device-state/history")
    def device_state_history_all(since_ms: int = 0, limit: int = 500) -> Dict[str, Any]:
        """跨设备 / 属性的最近变化点（新 → 旧）。"""

        return {"entries": store().history_all(since_ms, limit)}

    @router.post("/device-state/report")
    def device_state_report(body: DeviceStateReportIn) -> Dict[str, Any]:
        """写入标量属性；只在值变化时记一条历史。"""

        projection = store()
        changed: Dict[str, bool] = {}
        for name, value in body.properties.items():
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise HTTPException(status_code=422, detail=f"属性 {name} 的值必须是标量")
            changed[name] = bool(projection.set(body.device_id, name, value))
        return {"device_id": body.device_id, "changed": changed}

    @router.get("/device-state/{device_id:path}/history")
    def device_state_history(device_id: str, property: str, since_ms: int = 0, limit: int = 200) -> Dict[str, Any]:
        """单属性变化轨迹（新 → 旧）。"""

        return {
            "device_id": device_id,
            "property": property,
            "entries": store().history(device_id, property, since_ms, limit),
        }

    @router.get("/device-state/{device_id:path}")
    def device_state_one(device_id: str) -> Dict[str, Any]:
        properties = store().latest_for(device_id)
        if not properties:
            raise HTTPException(status_code=404, detail=f"设备 {device_id} 没有状态记录")
        return {"device_id": device_id, "properties": properties}

    return router


__all__ = ["DeviceStateReportIn", "create_device_state_router"]
