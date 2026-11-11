"""驱动包管理 API（``/api/v1/driver-packages``）。

给前端「驱动包」页用：看台账、装 / 卸 / 启停、跟踪后台操作。
``changes`` 返回源码变更和具体 Host/slave 重启范围，本路由不自动重启。
Host 重挂载走 ``POST /api/v1/restart``，单个 slave 走设备进程重启接口。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from unilabos.protocol.runtime.driver_packages import (
    DriverPackageEnableRequest,
    DriverPackageInstallRequest,
)

from unilabos.server.services.driver_packages import (
    DriverPackageBusyError,
    DriverPackageError,
    get_driver_package_service,
)


def create_driver_packages_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/driver-packages", tags=["driver-packages"])

    def _call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DriverPackageError as exc:
            message = str(exc)
            status = 409 if isinstance(exc, DriverPackageBusyError) else (404 if "not found" in message else 422)
            raise HTTPException(status_code=status, detail=message) from exc

    @router.get("")
    def inventory() -> Dict[str, Any]:
        return get_driver_package_service().inventory()

    @router.get("/catalog")
    def catalog() -> Dict[str, Any]:
        """官方索引（HTTPConfig.driver_package_index_url）+ 本地 driver_package_catalog.json 合并后的可安装目录。"""
        return get_driver_package_service().catalog()

    @router.get("/changes")
    def changes() -> Dict[str, Any]:
        """变更文件、需重启的 Host/slave 及连带影响；查询不会执行或确认重启。"""
        return get_driver_package_service().source_changes.status()

    @router.post("/install", status_code=202)
    def install(body: DriverPackageInstallRequest) -> Dict[str, Any]:
        return _call(get_driver_package_service().start_install, body.spec, body.enable, body.upgrade, body.name)

    @router.get("/operations")
    def operations() -> List[Dict[str, Any]]:
        return get_driver_package_service().operations()

    @router.get("/operations/{operation_id}")
    def operation(operation_id: str) -> Dict[str, Any]:
        return _call(get_driver_package_service().operation, operation_id)

    @router.put("/{name}/enabled")
    def set_enabled(name: str, body: DriverPackageEnableRequest) -> Dict[str, Any]:
        return _call(get_driver_package_service().set_enabled, name, body.enabled)

    @router.delete("/{name}", status_code=202)
    def uninstall(name: str) -> Dict[str, Any]:
        return _call(get_driver_package_service().start_uninstall, name)

    return router


__all__ = ["create_driver_packages_router"]
