"""工作流与实验操作 HTTP 接口的 OpenAPI 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _PublicResponseModel(BaseModel):
    """只声明稳定公开字段，并允许响应以后增加向后兼容字段。"""

    model_config = ConfigDict(extra="allow")


class BackendErrorDetail(_PublicResponseModel):
    """Backend 统一业务错误中的可读详情。"""

    msg: str = Field(description="错误原因", examples=["工作流不存在"])
    code: str | None = Field(
        default=None,
        description="仅部分冲突返回的细分错误标识",
        examples=["workflow_identity_mismatch"],
    )


class BackendErrorResponse(_PublicResponseModel):
    """HTTP 200 中承载的 Backend 统一业务错误包络。"""

    code: int = Field(description="非 0 表示业务处理失败", examples=[3002])
    error: BackendErrorDetail = Field(description="业务错误详情")


class BackendEmptySuccessResponse(_PublicResponseModel):
    """删除等无返回数据操作的成功包络。"""

    code: Literal[0] = Field(description="0 表示业务处理成功", examples=[0])


class WorkflowReadModel(_PublicResponseModel):
    """普通工作流与实验操作共用的公开读模型。"""

    uuid: str = Field(
        description="工作流稳定 UUID",
        examples=["53a80bc4-a648-4d4b-afcc-47b940f93769"],
    )
    name: str = Field(description="工作流显示名称", examples=["样品前处理"])
    tags: list[Any] = Field(description="检索和兼容分类标签", examples=[["chemistry"]])
    description: str | None = Field(
        default=None,
        description="工作流用途说明；未填写时可能不返回该字段",
        examples=["完成称量、溶解和转运"],
    )
    meta_data: dict[str, Any] = Field(description="业务扩展元数据")
    workflow_type: Literal["normal", "experiment_operation"] = Field(
        description="normal 为普通工作流；experiment_operation 为实验操作"
    )
    operation_category_uuid: str | None = Field(
        description="实验操作类别 UUID；普通工作流或未分类实验操作为 null",
        examples=["1ade6f36-40a9-58fe-a6c8-c7418e651a49"],
    )
    status: Literal["source", "published"] = Field(
        description=(
            "source 表示当前修订尚未发布；published 表示当前修订已经发布。"
            "只有已发布的实验操作可以被其他工作流引用"
        )
    )
    published_revision: int | None = Field(
        default=None,
        description=(
            "中心目录当前可选用的已发布版本对应的工作流修订号；从未发布或已下架为 null。"
            "可能小于 revision，此时中心使用的是这一旧版本"
        ),
        examples=[1],
    )
    has_unpublished_changes: bool = Field(
        default=False,
        description="已有可供中心选用的发布版本，但当前修订尚未发布",
    )
    revision: int = Field(description="当前工作流修订号", examples=[1])
    create_time: datetime = Field(description="创建时间（UTC）")
    update_time: datetime = Field(description="最后更新时间（UTC）")


class WorkflowSuccessResponse(_PublicResponseModel):
    """单个工作流或实验操作的成功响应。"""

    code: Literal[0] = Field(description="0 表示业务处理成功", examples=[0])
    data: WorkflowReadModel = Field(description="工作流或实验操作详情")


class WorkflowListData(_PublicResponseModel):
    """工作流与实验操作的分页列表数据。"""

    items: list[WorkflowReadModel] = Field(description="当前页结果")
    has_more: bool = Field(description="是否还有下一页")
    page: int = Field(description="实际页码", examples=[1])
    page_size: int = Field(description="实际每页数量，服务端最多返回 100 条", examples=[20])


class WorkflowListSuccessResponse(_PublicResponseModel):
    """工作流与实验操作分页查询成功响应。"""

    code: Literal[0] = Field(description="0 表示业务处理成功", examples=[0])
    data: WorkflowListData = Field(description="分页列表")


class WorkflowPublishSuccessResponse(_PublicResponseModel):
    """发布工作流成功响应。

    发布接口的历史返回还包含供旧客户端使用的内部投影字段，因此这里只把
    ``data`` 作为可扩展对象公开；前端判断当前修订是否已发布，应读取工作流
    列表/详情中的 ``status=published``，不依赖发布记录或版本字段。
    """

    code: Literal[0] = Field(description="0 表示业务处理成功", examples=[0])
    data: dict[str, Any] = Field(
        description=(
            "发布后的工作流结果；发布完成后对应工作流状态为 published。"
            "已发布的实验操作还可以被其他工作流引用"
        )
    )


class OperationCategoryReadModel(_PublicResponseModel):
    """实验操作类别公开读模型。"""

    uuid: str = Field(
        description="类别稳定 UUID",
        examples=["1ade6f36-40a9-58fe-a6c8-c7418e651a49"],
    )
    name: str = Field(description="类别显示名称", examples=["设备操作"])
    sort_order: int = Field(description="类别排序值，数值越小越靠前", examples=[10])


class OperationCategorySuccessResponse(_PublicResponseModel):
    """单个实验操作类别的成功响应。"""

    code: Literal[0] = Field(description="0 表示业务处理成功", examples=[0])
    data: OperationCategoryReadModel = Field(description="实验操作类别详情")


class OperationCategoryListData(_PublicResponseModel):
    """实验操作类别列表数据。"""

    items: list[OperationCategoryReadModel] = Field(description="按 sort_order 排列的类别")


class OperationCategoryListSuccessResponse(_PublicResponseModel):
    """实验操作类别列表查询成功响应。"""

    code: Literal[0] = Field(description="0 表示业务处理成功", examples=[0])
    data: OperationCategoryListData = Field(description="实验操作类别列表")





class WorkflowPreflightCheck(_PublicResponseModel):
    """候选或整批运行检查项。"""

    type: str = Field(description="检查类别")
    status: str = Field(description="检查结果：通过、阻断、延后检查或需要人工确认")
    code: str = Field(description="检查结果标识")
    message: str = Field(description="面向操作人员的检查说明")
    blocking: bool = Field(description="是否阻止本次候选运行")
    details: dict[str, Any] = Field(description="检查详情；库存不足时含 shortages，说明可用数量、需求数量及单位")
    node_uuid: str | None = Field(default=None, description="所属工站内部节点编号；整批库存检查不带此字段")
    node_name: str | None = Field(default=None, description="所属节点名称")


class WorkflowRunPreflightReport(_PublicResponseModel):
    """单次候选执行报告；上游输入未就绪时做基础检查并保留非阻断延后项。"""

    workflow_uuid: str = Field(description="工站工作流编号")
    workflow_revision: int = Field(description="实际检查的工作流修订号")
    run_mode: str = Field(description="运行模式")
    target_node_uuid: str | None = Field(default=None, description="单节点运行的目标节点")
    status: Literal["runnable_now", "temporarily_unavailable", "invalid"] = Field(description="当前预检无真实阻断、存在真实阻断或定义无效；单纯延后检查不阻断")
    can_run: bool = Field(description="此项当前能检查的条件是否没有真实阻断；input_ready=false 本身不阻断，延后项仍保留，整批结论须读取外层 can_run")
    checked_at: datetime = Field(description="检查时间")
    summary: dict[str, int] = Field(description="执行节点数及各类检查数量")
    checks: list[WorkflowPreflightCheck] = Field(description="该候选各项条件的检查结果")


class WorkflowBatchRunPreflightData(_PublicResponseModel):
    """整批候选和共享库存合计检查结果。"""

    status: Literal["runnable_now", "temporarily_unavailable", "invalid"] = Field(description="整批预检无真实阻断、存在真实阻断或有定义无效；可包含非阻断的延后项")
    can_run: bool = Field(description="当前能检查的候选和已知库存是否没有真实阻断；未知下游本身不阻断，不表示所有未来条件已验证或资源已预留")
    checked_at: datetime = Field(description="整批检查完成时间")
    summary: dict[str, int] = Field(description="候选数量、执行节点总数和各类检查总数")
    items: list[WorkflowRunPreflightReport] = Field(description="与请求 items 位置一一对应的候选报告，同一工作流可以重复出现")
    checks: list[WorkflowPreflightCheck] = Field(description="整批共享库存检查，需求量按库存实例合计")


class WorkflowBatchRunPreflightSuccessResponse(_PublicResponseModel):
    """批量预检成功响应；业务受阻通过 data.can_run=false 表达。"""

    code: Literal[0] = Field(description="0 表示检查请求处理成功")
    data: WorkflowBatchRunPreflightData = Field(description="批量运行预检报告，不创建任务和资源占用")


__all__ = [
    "BackendEmptySuccessResponse",
    "BackendErrorResponse",
    "OperationCategoryListSuccessResponse",
    "OperationCategorySuccessResponse",
    "WorkflowListSuccessResponse",
    "WorkflowPublishSuccessResponse",
    "WorkflowSuccessResponse",
    "WorkflowBatchRunPreflightSuccessResponse",
]
