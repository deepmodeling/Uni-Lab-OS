"""微后端数据库 HTTP API 的公共安装入口（目录严格按四库划分）。

- ``api/runtime/``：runtime.db（运行控制 / runtime.v1 控制面通道 / 诊断 / workflow / registry 路由）
- ``api/materials/``：materials.db（物料 / 拓扑图路由）
- ``api/telemetry.py`` / ``api/history.py``：单文件
- ``api/debug.py``：四库只读浏览（开发调试面，Swagger 即页面）
- ``app.py``：进程装配，非库域
"""

from fastapi import FastAPI

from unilabos.server.api.debug import create_debug_router, install_debug_api
from unilabos.server.api.history import create_history_router, install_history_api
from unilabos.server.api.materials import (
    create_graph_router,
    create_materials_router,
    install_chemical_api,
    install_graph_api,
    install_materials_api,
)
from unilabos.server.api.runtime import (
    create_lab_router,
    create_runtime_router,
    install_lab_api,
    install_runtime_api,
)
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
    include_host_data: bool = True,
) -> None:
    """安装进程持有的数据库 API。

    外部微后端作为物料权威时，本进程仍会打开多库组合供 runtime 等服务使用，
    但不得暴露本地 materials writer，避免出现第二个可写物料中心。

    ``include_host_data=False``：调度权威进程带 Host 子进程时，runtime / telemetry /
    history 这些 Host 数据面路由由 ``edge_proxy`` 转发到子进程，本进程不再用自己的库
    回答，避免浏览器看到两套数据；graphs（Graph Authority）与 lab（布局）留在权威。
    """

    if include_host_data:
        install_runtime_api(app, services.runtime)
    # V1 实验室布局：档案 / 分区 / 摆放在 materials.db，dev 网格文档（runtime.db）并入档案的 grid
    install_lab_api(app, services.materials, services.lab)
    if include_materials:
        install_materials_api(app, services.materials)
        install_chemical_api(app, services.chemicals)
    if include_host_data:
        install_telemetry_api(app, services.telemetry)
        install_history_api(app, services.history)
    install_graph_api(app, services.graph)
    install_debug_api(app, services.paths)


__all__ = [
    "create_debug_router",
    "create_graph_router",
    "create_history_router",
    "create_lab_router",
    "create_materials_router",
    "create_runtime_router",
    "create_telemetry_router",
    "install_debug_api",
    "install_graph_api",
    "install_history_api",
    "install_lab_api",
    "install_materials_api",
    "install_runtime_api",
    "install_server_apis",
    "install_telemetry_api",
]
