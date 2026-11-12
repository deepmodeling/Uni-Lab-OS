"""驱动包随包设备图 API（``/api/v1/driver-packages/{name}/graphs``）。

前端「驱动包」页在安装完成后用它列出包自带的设备图（示例包的 demo 图），并一键
以受管设备进程启动；启动后的进程在 ``/api/v1/device-processes`` 里继续管理。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from unilabos.server.api.envelope import envelope_router
from unilabos.server.services.device_processes import (
    DeviceProcessError,
    get_device_process_service,
)
from unilabos.server.services.driver_package_graphs import (
    bundled_graph_payload,
    launch_bundled_graph,
    list_bundled_graphs,
)
from unilabos.server.services.driver_packages import (
    DriverPackageError,
    get_driver_package_service,
)


def create_driver_package_graphs_router() -> APIRouter:
    router = envelope_router(prefix="/api/v1/driver-packages", tags=["driver-packages"])

    def _working_dir():
        return get_driver_package_service().working_dir

    def _call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DriverPackageError as exc:
            message = str(exc)
            raise HTTPException(
                status_code=404 if "not found" in message else 422, detail=message
            ) from exc
        except DeviceProcessError as exc:
            message = str(exc)
            status = 404 if "not found" in message else 409 if "已在运行" in message else 422
            raise HTTPException(status_code=status, detail=message) from exc

    @router.get("/{name}/graphs")
    def graphs(name: str) -> List[Dict[str, Any]]:
        """包自带的设备图（data-files ``share/<包>/graph`` 或源码目录 ``graph/``）。"""
        return _call(list_bundled_graphs, _working_dir(), name)

    @router.get("/{name}/graphs/{graph_name}")
    def graph(name: str, graph_name: str) -> Dict[str, Any]:
        return _call(bundled_graph_payload, _working_dir(), name, graph_name)

    @router.post("/{name}/graphs/{graph_name}/launch")
    def launch(name: str, graph_name: str) -> Dict[str, Any]:
        """以受管设备进程启动随包图：同名进程已存在则更新规格后重启。"""
        return _call(
            launch_bundled_graph,
            _working_dir(),
            name,
            graph_name,
            processes=get_device_process_service(),
        )

    return router


__all__ = ["create_driver_package_graphs_router"]
