"""Workbench 使用的工作区只读创作合同。"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from unilabos.package_manager.workspace_runtime.authoring_mounts import (
    WorkspacePackageMountProjection,
)


def create_workspace_authoring_router(
    projection: WorkspacePackageMountProjection,
) -> APIRouter:
    """创建固定在一个 OS 启动代的 Workspace 创作路由。

    参数：``projection`` 是包目录编译阶段产生的不可变挂载投影。返回：只读
    FastAPI 路由。异常：投影类型错误时抛出 ``TypeError``。
    """

    if not isinstance(projection, WorkspacePackageMountProjection):
        raise TypeError("Workspace 创作路由需要软件包挂载投影")
    router = APIRouter(prefix="/api/v1/workspace", tags=["workspace-authoring"])

    @router.get("/package-mounts")
    def get_package_mounts() -> dict[str, object]:
        """返回当前完整候选代的精确软件包源码挂载。"""

        return {"code": 0, "data": projection.to_dict()}

    return router


def install_workspace_authoring_api(
    app: FastAPI,
    projection: WorkspacePackageMountProjection,
) -> None:
    """把 Workspace 创作合同装配到产品 FastAPI 应用。"""

    app.include_router(create_workspace_authoring_router(projection))


__all__ = ["create_workspace_authoring_router", "install_workspace_authoring_api"]
