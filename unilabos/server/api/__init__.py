"""微后端四库 HTTP API 的公共安装入口。"""

from fastapi import FastAPI

from unilabos.server.api.history import create_history_router, install_history_api
from unilabos.server.api.materials import create_materials_router, install_materials_api
from unilabos.server.api.runtime import create_runtime_router, install_runtime_api
from unilabos.server.api.telemetry import (
    create_telemetry_router,
    install_telemetry_api,
)
from unilabos.server.composition import ServerServices


def install_server_apis(
    app: FastAPI,
    services: ServerServices,
    *,
    include_materials: bool = True,
) -> None:
    """安装进程持有的数据库 API。

    外部微后端作为物料权威时，本进程仍会打开四库组合供 runtime 等服务使用，
    但不得暴露本地 materials writer，避免出现第二个可写物料中心。
    """

    install_runtime_api(app, services.runtime)
    if include_materials:
        install_materials_api(app, services.materials)
    install_telemetry_api(app, services.telemetry)
    install_history_api(app, services.history)


__all__ = [
    "create_history_router",
    "create_materials_router",
    "create_runtime_router",
    "create_telemetry_router",
    "install_history_api",
    "install_materials_api",
    "install_runtime_api",
    "install_server_apis",
    "install_telemetry_api",
]
