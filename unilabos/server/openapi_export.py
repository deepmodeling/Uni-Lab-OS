"""离线导出微后端 OpenAPI 文档（契约全集）。

    python -m unilabos.server.openapi_export [--output openapi.json] [--indent 2]

运行中的进程只挂当前角色需要的路由（``GET /api/openapi.json`` 因而是「本进程视角」）；
这里在临时目录里装配 **Host 与 Backend 两个角色的全部路由**，不需要设备、不需要起 uvicorn，
输出的是契约全集，并给每个 operation 打上 ``x-openlab-role``（``host`` / ``backend`` / ``any``），
供 OpenLab 的 ``@openlab/protocol`` 生成类型并与 ``catalog.ts`` 对账。

角色映射是显式表：新增路由如果属于某一角色专属，必须在这里登记，否则按 ``any`` 导出，
前端契约测试会因角色不一致而失败——这正是想要的提醒。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

from fastapi import FastAPI

import unilabos

API_PREFIX = "/api/v1"
#: V1 Workspace Host 路由按 V1 原样不带 ``/api`` 前缀（见 ``server/api/workspace_host.py``）。
WORKSPACE_HOST_PREFIX = "/v1"

# 路径前缀 → 角色。只在带设备执行面的 Host 进程挂载的路由标 host；
# 只在 --role backend 调度权威进程挂载的标 backend；其余两种角色都挂，标 any。
# /registry 与 workflow 一样跟随调度权威归属（默认 Host 与 --role backend 都挂），标 any。
HOST_ONLY_PREFIXES: Tuple[str, ...] = (
    f"{API_PREFIX}/status-incidents",
    f"{API_PREFIX}/error-decisions",
    f"{API_PREFIX}/driver-packages",
    f"{API_PREFIX}/device-processes",
    f"{API_PREFIX}/materials/notify-device",
    f"{API_PREFIX}/hostlink/log-sources",
    f"{API_PREFIX}/hostlink/logs",
)
BACKEND_ONLY_PREFIXES: Tuple[str, ...] = ()


def role_for_path(path: str) -> str:
    if any(path == prefix or path.startswith(prefix + "/") for prefix in BACKEND_ONLY_PREFIXES):
        return "backend"
    if any(path == prefix or path.startswith(prefix + "/") for prefix in HOST_ONLY_PREFIXES):
        return "host"
    return "any"


def build_contract_app(workdir: Path) -> Tuple[FastAPI, Any]:
    """在 ``workdir`` 下用临时四库装配挂满全部路由的 FastAPI 应用。

    返回 ``(app, services)``；调用方负责 ``services.close()``。
    """

    from unilabos.server.api import install_server_apis
    from unilabos.server.api.device_processes import create_device_processes_router
    from unilabos.server.api.driver_package_graphs import create_driver_package_graphs_router
    from unilabos.server.api.driver_packages import create_driver_packages_router
    from unilabos.server.api.runtime.diagnostics import create_backend_router
    from unilabos.server.api.runtime.logs import create_runtime_logs_router
    from unilabos.server.api.runtime.registry import install_registry_api
    from unilabos.server.api.runtime.workflow import install_workflow_api
    from unilabos.server.api.workspace_host import install_workspace_host_api
    from unilabos.server.composition import ServerServices
    from unilabos.server.database import ServerDatabasePaths
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    app = FastAPI(
        title="Uni-Lab-OS Microbackend API",
        description=(
            "Uni-Lab-OS 微后端 HTTP 契约全集（Host 与 --role backend 两种角色的路由并集）。"
            "每个 operation 的 x-openlab-role 标注它在哪种进程角色下可用。"
        ),
        version=unilabos.__version__,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    services = ServerServices.open(ServerDatabasePaths.resolve(workdir))
    install_server_apis(app, services)
    workflow = WorkflowService(":memory:")
    install_workflow_api(app, workflow)
    install_registry_api(app)
    # V1 Workspace Host（/v1/*，裸 JSON）：与 Workflow Authority 同归属
    install_workspace_host_api(app, workflow)
    # 诊断路由（health / hostlink / scheduler / restart / 人工决策）：handler 里才会取真实对象
    app.include_router(create_backend_router(lambda: None, lambda: None))
    app.include_router(create_driver_packages_router())
    app.include_router(create_driver_package_graphs_router())
    app.include_router(create_device_processes_router())
    app.include_router(create_runtime_logs_router())
    return app, services


def annotate(document: Dict[str, Any]) -> Dict[str, Any]:
    """给每个 operation 加 ``x-openlab-role``，并在 info 里记录协议元信息。"""

    for path, item in document.get("paths", {}).items():
        role = role_for_path(path)
        for method, operation in item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
                operation["x-openlab-role"] = role
    info = document.setdefault("info", {})
    info["x-openlab-protocol"] = {
        "api_version": "v1",
        "api_prefix": API_PREFIX,
        "unilabos_version": unilabos.__version__,
        "roles": ["host", "backend", "any"],
        "sse": [f"{API_PREFIX}/events", f"{API_PREFIX}/materials/events"],
        # 不带 /api 前缀、裸 JSON 的 V1 Workspace Host 路由
        "workspace_host_prefix": WORKSPACE_HOST_PREFIX,
    }
    return document


def export_openapi() -> Dict[str, Any]:
    """装配 → 生成 → 标注；顺序稳定（paths 按字典序），便于提交后比 diff。"""

    with tempfile.TemporaryDirectory(prefix="unilabos-openapi-") as tmp:
        app, services = build_contract_app(Path(tmp))
        try:
            document = app.openapi()
        finally:
            services.close()
    document = json.loads(json.dumps(document))  # 脱离 FastAPI 缓存的引用
    document["paths"] = dict(sorted(document["paths"].items()))
    schemas = document.get("components", {}).get("schemas")
    if isinstance(schemas, dict):
        document["components"]["schemas"] = dict(sorted(schemas.items()))
    return annotate(document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 Uni-Lab-OS 微后端 OpenAPI 契约全集")
    parser.add_argument("--output", "-o", default="-", help="输出文件；缺省写到 stdout")
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args(argv)

    document = export_openapi()
    text = json.dumps(document, ensure_ascii=False, indent=args.indent, sort_keys=False) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        operations = sum(
            1 for item in document["paths"].values() for method in item if method in {"get", "post", "put", "patch", "delete"}
        )
        print(f"OpenAPI {document['info']['version']}: {len(document['paths'])} paths, {operations} operations -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
