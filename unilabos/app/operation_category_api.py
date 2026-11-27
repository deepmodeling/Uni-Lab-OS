"""实验操作类别的薄 HTTP 适配器。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict, Field

from unilabos.app.runtime_capabilities import get_runtime_capabilities
from unilabos.app.workflow_api import (
    BackendJSONResponse,
    BackendJSONRoute,
    workflow_success_response,
)
from unilabos.app.workflow_openapi import (
    BackendEmptySuccessResponse,
    BackendErrorResponse,
    OperationCategoryListSuccessResponse,
    OperationCategorySuccessResponse,
)
from unilabos.workflow.service import WorkflowError, WorkflowService


class _StrictModel(BaseModel):
    """拒绝未知字段的类别写入 DTO 基类。"""

    model_config = ConfigDict(extra="forbid")


class OperationCategoryCreateRequest(_StrictModel):
    """新增实验操作类别的公共 DTO。"""

    name: str = Field(
        min_length=1,
        max_length=64,
        description="类别显示名称",
        examples=["数据处理"],
    )
    sort_order: int = Field(
        default=100,
        ge=0,
        le=10_000,
        strict=True,
        description="类别排序值；数值越小越靠前",
        examples=[40],
    )


class OperationCategoryUpdateRequest(_StrictModel):
    """修改实验操作类别名称或顺序的公共 DTO。"""

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="新的类别显示名称；不修改时省略",
        examples=["数据处理"],
    )
    sort_order: int | None = Field(
        default=None,
        ge=0,
        le=10_000,
        strict=True,
        description="新的类别排序值；数值越小越靠前",
        examples=[40],
    )


CategoryUUIDPath = Annotated[
    str,
    Path(
        description="实验操作类别的稳定 UUID",
        examples=["1ade6f36-40a9-58fe-a6c8-c7418e651a49"],
    ),
]


def create_operation_category_router(service: WorkflowService) -> APIRouter:
    """创建实验操作类别 CRUD 路由。

    参数：``service`` 是同一工作流定义权威，负责类别引用完整性。返回：只承载
    类别接口的路由。异常：服务错误交给上层已安装的统一工作流错误处理器。
    """

    router = APIRouter(
        prefix="/api/v1/experiment-operation-categories",
        tags=["experiment-operation-category"],
        route_class=BackendJSONRoute,
    )

    @router.get(
        "",
        summary="查询实验操作类别",
        response_model=OperationCategoryListSuccessResponse | BackendErrorResponse,
    )
    def list_experiment_operation_categories() -> BackendJSONResponse:
        """返回领域包当前可用的实验操作类别。

        参数：无。返回：按展示顺序排列的类别集合。异常：领域包配置损坏时交给
        公共工作流错误适配器，避免前端使用半份分类数据。查看草稿能力关闭时不展示
        实验操作，因此返回空列表；能力允许时读取本地分类目录。
        """

        if not get_runtime_capabilities().can_view_drafts:
            return workflow_success_response({"items": []})
        return workflow_success_response({"items": service.list_operation_categories()})

    @router.post(
        "",
        summary="新增实验操作类别",
        status_code=201,
        response_model=OperationCategorySuccessResponse,
        responses={
            200: {
                "model": BackendErrorResponse,
                "description": "类别名称冲突或领域包不可写",
            }
        },
    )
    def create_experiment_operation_category(
        body: OperationCategoryCreateRequest,
    ) -> BackendJSONResponse:
        """在唯一领域包中新增实验操作类别。

        参数：``body`` 含显示名与顺序。返回：新类别，HTTP 201。异常：名称冲突
        或领域包不可写时由服务层映射为统一业务响应。
        """

        return workflow_success_response(
            service.create_operation_category(**body.model_dump()),
            status=201,
        )

    @router.get(
        "/{category_uuid}",
        summary="查询实验操作类别详情",
        response_model=OperationCategorySuccessResponse | BackendErrorResponse,
    )
    def get_experiment_operation_category(
        category_uuid: CategoryUUIDPath,
    ) -> BackendJSONResponse:
        """按稳定 UUID 返回一个实验操作类别。

        参数：``category_uuid`` 是路径身份。返回：类别读模型。异常：查看草稿能力
        关闭或分类不存在时按统一 not_found 错误返回。状态不变量：不会暴露未授权的
        实验操作分类。
        """

        if not get_runtime_capabilities().can_view_drafts:
            raise WorkflowError("not_found")
        return workflow_success_response(service.get_operation_category(category_uuid))

    @router.put(
        "/{category_uuid}",
        summary="更新实验操作类别",
        response_model=OperationCategorySuccessResponse | BackendErrorResponse,
    )
    def update_experiment_operation_category(
        category_uuid: CategoryUUIDPath,
        body: OperationCategoryUpdateRequest,
    ) -> BackendJSONResponse:
        """修改实验操作类别名称或展示顺序。

        参数：路径 UUID 定位类别，``body`` 至少应提供一个修改字段。返回：修改后
        类别。异常：空修改、名称冲突或 CAS 冲突由服务层处理。
        """

        return workflow_success_response(
            service.update_operation_category(
                category_uuid,
                **body.model_dump(exclude_unset=True),
            )
        )

    @router.delete(
        "/{category_uuid}",
        summary="删除实验操作类别",
        response_model=BackendEmptySuccessResponse | BackendErrorResponse,
    )
    def delete_experiment_operation_category(
        category_uuid: CategoryUUIDPath,
    ) -> BackendJSONResponse:
        """删除未被任何实验操作引用的类别。

        参数：``category_uuid`` 是路径身份。返回：统一空成功响应。异常：仍被引用
        时返回冲突，防止现有实验操作静默失去分类。
        """

        service.delete_operation_category(category_uuid)
        return workflow_success_response()

    return router


__all__ = ["create_operation_category_router"]
