"""后端形状工作流节点模板查询的只读 HTTP 适配器。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.presentation_metadata import PRESENTATION_KEYS

_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100
_DEFAULT_VISIBLE_NODE_TYPES = frozenset(
    {
        "ILab",
        "device_action",
        "py_script",
        "tool_call",
        "manual_confirm",
        "condition",
        "repeat_until",
        "material_io",
    }
)


class TemplateSnapshotProvider(Protocol):
    """提供最近一次完整模板投影的窄读取接口。"""

    def snapshot(self) -> AuthoringCatalogSnapshot:
        """返回已提交的不可变设备注册表模板投影。"""


class WorkflowTemplateQueryError(RuntimeError):
    """模板查询的稳定业务错误。"""

    def __init__(self, code: int, message: str) -> None:
        """保存后端（Backend）业务码和可读消息。

        参数说明：``code`` 是响应 envelope（响应外壳）业务码，``message`` 是
        前端可显示的错误说明。
        """

        super().__init__(message)
        self.code = code
        self.message = message


class WorkflowTemplateQueryService:
    """从一个不可变快照完成列表、筛选、游标和详情查询。"""

    def __init__(
        self,
        snapshot_provider: TemplateSnapshotProvider,
    ) -> None:
        """绑定模板快照提供者。

        参数说明：``snapshot_provider`` 通常是设备注册表模板投影；每次请求只取
        一次快照，避免一条响应混合两个发布代际。
        """

        self._snapshot_provider = snapshot_provider

    def list_node_templates(
        self,
        *,
        page: int,
        page_size: int,
        keyword: str,
        resource_template_uuid: str | None,
        action_type: str,
        node_type: str,
    ) -> dict[str, Any]:
        """按后端（Backend）当前查询合同返回节点模板摘要页。

        参数说明：``page`` 和 ``page_size`` 被规范为 Backend 页码合同；其余字段
        分别筛选名称、资源模板 UUID、动作类型和节点类型。返回 has-more 页对象。
        """

        normalized_page = _DEFAULT_PAGE if page < 1 else page
        normalized_page_size = (
            _DEFAULT_PAGE_SIZE
            if page_size < 1
            else min(page_size, _MAX_PAGE_SIZE)
        )
        snapshot = self._snapshot_provider.snapshot()
        # ``ordered_actions`` 使用 Backend 的创建时间降序、UUID 降序规则。
        ordered_actions = sorted(snapshot.actions, key=_action_order, reverse=True)
        normalized_resource_uuid = (
            _validated_uuid(resource_template_uuid, "resource_template_uuid")
            if resource_template_uuid is not None
            else None
        )
        normalized_keyword = keyword.strip().lower()
        normalized_type = action_type.strip()
        normalized_node_type = node_type.strip()
        matches: list[AuthoringCatalogAction] = []
        for action in ordered_actions:
            template = action.template
            if normalized_resource_uuid is not None and str(
                template.get("resource_template_uuid")
            ) != normalized_resource_uuid:
                continue
            if normalized_keyword and normalized_keyword not in str(
                template.get("name", "")
            ).lower() and normalized_keyword not in str(
                template.get("display_name", "")
            ).lower():
                continue
            if normalized_type and template.get("type") != normalized_type:
                continue
            candidate_node_type = str(template.get("node_type") or "")
            if normalized_node_type:
                if candidate_node_type != normalized_node_type:
                    continue
            elif candidate_node_type not in _DEFAULT_VISIBLE_NODE_TYPES:
                continue
            matches.append(action)

        offset = (normalized_page - 1) * normalized_page_size
        page_items = matches[offset : offset + normalized_page_size]
        return {
            "items": [_summary(action) for action in page_items],
            "has_more": len(matches) > offset + normalized_page_size,
            "page": normalized_page,
            "page_size": normalized_page_size,
        }

    def get_node_template(self, template_uuid: str) -> dict[str, Any]:
        """按 UUID 返回节点模板及其全部句柄模板。

        参数说明：``template_uuid`` 是路径身份；返回后端（Backend）详情形状
        ``template + handles``，未知模板使用业务码 3002。
        """

        template_identity = _validated_uuid(template_uuid, "template_uuid")
        snapshot = self._snapshot_provider.snapshot()
        action = next(
            (
                candidate
                for candidate in snapshot.actions
                if str(candidate.template["uuid"]) == template_identity
            ),
            None,
        )
        if action is None:
            raise WorkflowTemplateQueryError(
                3002,
                f"工作流节点模板不存在或已被删除（模板 UUID：{template_identity}）",
            )
        return _omit_none(
            {
                "template": action.detached_template(),
                "handles": action.detached_handles(),
            }
        )


def _validated_uuid(value: str, field: str) -> str:
    """校验并规范化查询 UUID。

    参数说明：``value`` 是调用方身份，``field`` 用于错误消息；返回非空规范 UUID。
    """

    try:
        return validate_uuid(value)
    except (TypeError, ValueError):
        raise WorkflowTemplateQueryError(
            1000,
            f"{field} 必须是有效的 UUID，请检查后重试",
        ) from None


def _action_order(action: AuthoringCatalogAction) -> tuple[str, str]:
    """取得后端（Backend）兼容的模板游标排序键。

    参数说明：``action`` 是快照动作；返回 ``(create_time, uuid)``，旧投影缺失
    创建时间时使用空字符串但仍保持 UUID 稳定排序。
    """

    return str(action.template.get("create_time") or ""), str(
        action.template["uuid"]
    )


def _summary(action: AuthoringCatalogAction) -> dict[str, Any]:
    """把快照动作映射为节点模板列表摘要。

    参数说明：``action`` 是一个聚合后的节点与句柄；返回值仅包含 Backend 列表
    字段，资源模板摘要来自投影时固化的元数据。
    异常：资源模板摘要缺失时抛出 ``WorkflowTemplateQueryError``。
    """

    template = action.template
    meta_data = template.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    resource_template = (
        unilab.get("resource_template") if isinstance(unilab, Mapping) else None
    )
    # 已发布工作流的 ``meta_data.unilab`` 是与前端冻结的封闭来源合同；框架
    # 所有者摘要因此位于相邻 ``meta_data.resource_template``，避免扩张 wire。
    if not isinstance(resource_template, Mapping) and isinstance(meta_data, Mapping):
        resource_template = meta_data.get("resource_template")
    if not isinstance(resource_template, Mapping):
        raise WorkflowTemplateQueryError(
            5004,
            "工作流节点模板缺少关联的资源模板信息，无法展示模板；请检查设备动作目录配置",
        )
    result = {
        "uuid": template["uuid"],
        "name": template["name"],
        "display_name": template["display_name"],
        # 设备包 @action(description=...) 的业务说明；单点动作页按它显示动作用途，
        # 缺省时由 _omit_none 省略，消费方回退到显示名。
        "description": template.get("description"),
        "type": template["type"],
        "node_type": template["node_type"],
        "icon": template.get("icon"),
        "resource_template": {
            "uuid": resource_template.get("uuid"),
            "name": resource_template.get("name"),
            "display_name": resource_template.get("display_name"),
        },
    }
    return _omit_none(result)


def _omit_none(value: Any) -> Any:
    """递归移除 Go ``omitempty`` 对应的空可选字段。

    参数说明：``value`` 是待返回 JSON；字典中的 ``None`` 被移除，数组和标量保持
    结构，返回值与不可变快照不共享可变容器。
    """

    if isinstance(value, Mapping):
        return {
            key: deepcopy(child) if key in PRESENTATION_KEYS else _omit_none(child)
            for key, child in value.items()
            if child is not None
        }
    if isinstance(value, (list, tuple)):
        return [_omit_none(child) for child in value]
    return value


def _success(data: Any) -> JSONResponse:
    """构造后端（Backend）成功响应外壳。

    参数说明：``data`` 是查询结果；返回 HTTP 200 和业务码 0。
    """

    return JSONResponse(status_code=200, content={"code": 0, "data": data})


def _error(error: WorkflowTemplateQueryError) -> JSONResponse:
    """构造后端（Backend）业务错误响应外壳。

    参数说明：``error`` 持有稳定业务码和消息；HTTP 状态保持 200。
    """

    return JSONResponse(
        status_code=200,
        content={"code": error.code, "error": {"msg": error.message}},
    )


def _call(callback: Any, *args: Any, **kwargs: Any) -> JSONResponse:
    """调用查询服务并统一映射业务错误。

    参数说明：``callback`` 是查询方法，其余参数原样转发；返回统一响应外壳。
    """

    try:
        return _success(callback(*args, **kwargs))
    except WorkflowTemplateQueryError as error:
        return _error(error)


def create_workflow_template_router(
    service: WorkflowTemplateQueryService,
) -> APIRouter:
    """创建后端形状工作流节点模板只读路由。

    参数说明：``service`` 绑定单一模板快照来源；返回只含列表与详情的路由。
    """

    router = APIRouter(prefix="/api/v1", tags=["workflow-template"])

    @router.get("/workflow-node-templates")
    def list_node_templates(
        page: int = Query(default=0),
        page_size: int = Query(default=0),
        keyword: str = Query(default=""),
        resource_template_uuid: Annotated[UUID | None, Query()] = None,
        action_type: str = Query(default="", alias="type"),
        node_type: str = Query(default=""),
    ) -> JSONResponse:
        """按 Backend 查询参数返回工作流节点模板摘要页。

        参数说明：``page`` 和 ``page_size`` 控制页码；``keyword``、资源
        模板 UUID、动作类型和节点类型执行服务端筛选。返回统一 JSON 外壳。
        """

        return _call(
            service.list_node_templates,
            page=page,
            page_size=page_size,
            keyword=keyword,
            resource_template_uuid=(
                str(resource_template_uuid) if resource_template_uuid else None
            ),
            action_type=action_type,
            node_type=node_type,
        )

    @router.get("/workflow-node-templates/{template_uuid}")
    def get_node_template(template_uuid: UUID) -> JSONResponse:
        """返回一个节点模板及其句柄详情。"""

        return _call(service.get_node_template, str(template_uuid))

    return router


def install_workflow_template_api(
    app: FastAPI,
    service: WorkflowTemplateQueryService,
) -> None:
    """向现有 FastAPI 应用安装模板查询路由和校验错误映射。

    参数说明：``app`` 是 OS HTTP 应用，``service`` 是只读查询服务。
    """

    @app.exception_handler(RequestValidationError)
    async def template_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        """把模板路由的参数校验错误映射为 Backend 业务错误。

        参数说明：``request`` 用于识别模板路径，``error`` 转交非模板路由的默认
        FastAPI 处理器。
        """

        if request.url.path.startswith("/api/v1/workflow-node-templates"):
            return _error(
                WorkflowTemplateQueryError(
                    1000,
                    "模板查询参数不符合要求，请检查 UUID、页码和每页数量的格式",
                )
            )
        return await request_validation_exception_handler(request, error)

    app.include_router(create_workflow_template_router(service))


def create_workflow_template_app(
    snapshot_provider: TemplateSnapshotProvider,
) -> FastAPI:
    """创建模板查询聚焦测试应用。

    参数说明：``snapshot_provider`` 提供最近已提交快照；返回独立 FastAPI 应用。
    """

    app = FastAPI(title="Uni-Lab Workflow Templates", version="0.1.0")
    install_workflow_template_api(
        app,
        WorkflowTemplateQueryService(snapshot_provider),
    )
    return app


__all__ = [
    "TemplateSnapshotProvider",
    "WorkflowTemplateQueryError",
    "WorkflowTemplateQueryService",
    "create_workflow_template_app",
    "create_workflow_template_router",
    "install_workflow_template_api",
]
