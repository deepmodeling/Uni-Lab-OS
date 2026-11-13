"""受管设备进程 API（``/api/v1/device-processes``）。

前端「驱动包与设备进程」页用：把驱动包里的设备类配成一个本机 Slave 子进程，
启动 / 停止 / 重启 / 看日志；崩溃由 Host 按策略看护重启。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from unilabos.server.api.envelope import envelope_router
from unilabos.server.services.device_processes import (
    DeviceProcessError,
    DeviceProcessBusyError,
    build_device_node,
    get_device_process_service,
)


class DeviceNodeInput(BaseModel):
    """简化的设备节点：id + 注册表类 + 初始化配置；服务补齐 uuid / pose 等字段。"""

    id: str = Field(min_length=1)
    device_class: str = Field(min_length=1, alias="class")
    name: str = ""
    config: Dict[str, Any] = Field(default_factory=dict)
    pose: Optional[Dict[str, Any]] = None

    model_config = {"populate_by_name": True}


class DeviceProcessWrite(BaseModel):
    name: str = Field(min_length=1)
    devices: List[DeviceNodeInput] = Field(default_factory=list)
    # 直接给完整 node-link 节点时优先（编辑已有进程保留 uuid）
    graph_nodes: Optional[List[Dict[str, Any]]] = None
    devices_dirs: List[str] = Field(default_factory=list)
    package_names: List[str] = Field(default_factory=list)
    external_only: bool = False
    auto_start: bool = True
    restart_policy: str = "on-failure"
    max_restarts: int = 5
    extra_args: List[str] = Field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        payload = self.model_dump(exclude={"devices", "graph_nodes"})
        if self.graph_nodes is not None:
            payload["graph_nodes"] = self.graph_nodes
        elif self.devices:
            payload["graph_nodes"] = [
                build_device_node(item.id, item.device_class, name=item.name, config=item.config, pose=item.pose)
                for item in self.devices
            ]
        return payload


def create_device_processes_router() -> APIRouter:
    router = envelope_router(prefix="/api/v1/device-processes", tags=["device-processes"])

    def _call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DeviceProcessError as exc:
            message = str(exc)
            status = 404 if "not found" in message else 409 if isinstance(exc, DeviceProcessBusyError) or "已在运行" in message else 422
            raise HTTPException(status_code=status, detail=message) from exc

    @router.get("")
    def list_processes() -> Dict[str, Any]:
        service = get_device_process_service()
        return {"hostlink": service.host_link_target(), "processes": service.list()}

    @router.get("/device-classes")
    def device_classes() -> List[Dict[str, Any]]:
        """可配置的设备类：本进程注册表已加载的 + 驱动包台账扫描到的。"""
        from unilabos.server.services.driver_packages import get_driver_package_service

        classes: Dict[str, Dict[str, Any]] = {}
        try:
            from unilabos.registry.registry import lab_registry

            for device_id, entry in lab_registry.device_type_registry.items():
                if device_id == "host_node":
                    continue
                display = ""
                if isinstance(entry, dict):
                    display = str(entry.get("display_name") or entry.get("name") or "")
                classes[device_id] = {"id": device_id, "source": "registry", "display_name": display, "package": None}
        except Exception:  # noqa: BLE001
            pass
        for package in get_driver_package_service().inventory()["packages"]:
            for device_id in package["device_ids"]:
                classes.setdefault(
                    device_id,
                    {"id": device_id, "source": "package", "display_name": "", "package": package["name"]},
                )
                if classes[device_id]["package"] is None:
                    classes[device_id]["package"] = package["name"]
        return sorted(classes.values(), key=lambda item: item["id"])

    @router.post("", status_code=201)
    def create(body: DeviceProcessWrite) -> Dict[str, Any]:
        return _call(get_device_process_service().create, body.to_payload())

    @router.get("/{process_id}")
    def get(process_id: str) -> Dict[str, Any]:
        return _call(get_device_process_service().get, process_id)

    @router.put("/{process_id}")
    def update(process_id: str, body: DeviceProcessWrite) -> Dict[str, Any]:
        return _call(get_device_process_service().update, process_id, body.to_payload())

    @router.delete("/{process_id}", status_code=204, response_model=None)
    def delete(process_id: str) -> None:
        _call(get_device_process_service().delete, process_id)

    @router.post("/{process_id}/start")
    def start(process_id: str) -> Dict[str, Any]:
        return _call(get_device_process_service().start, process_id)

    @router.post("/{process_id}/stop")
    def stop(process_id: str) -> Dict[str, Any]:
        return _call(get_device_process_service().stop, process_id)

    @router.post("/{process_id}/restart")
    def restart(process_id: str) -> Dict[str, Any]:
        return _call(get_device_process_service().restart, process_id)

    return router


__all__ = ["create_device_processes_router"]
