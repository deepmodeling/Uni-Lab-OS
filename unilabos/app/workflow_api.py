"""Thin FastAPI adapter for the Backend-shaped local Workflow authority."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from unilabos.app.runtime_capabilities import (
    get_runtime_capabilities,
    is_workflow_visible,
    visible_workflow_filter,
)
from unilabos.app.workflow_openapi import (
    BackendEmptySuccessResponse,
    BackendErrorResponse,
    WorkflowBatchRunPreflightSuccessResponse,
    WorkflowListSuccessResponse,
    WorkflowPublishSuccessResponse,
    WorkflowSuccessResponse,
)
from unilabos.app.workflow_template_api import (
    TemplateSnapshotProvider,
    WorkflowTemplateQueryService,
    create_workflow_template_router,
)
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.models import (
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    WorkflowTaskPriority,
    normalize_json_array,
    normalize_json_object,
    validate_uuid,
)
from unilabos.workflow.service import WorkflowError, WorkflowService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _BackendModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


HashToken = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
WorkflowUUIDPath = Annotated[
    str,
    Path(
        description="工作流或实验操作的稳定 UUID",
        examples=["53a80bc4-a648-4d4b-afcc-47b940f93769"],
    ),
]
_SIGNED_DECIMAL = re.compile(r"[+-]?[0-9]+\Z")
_INT64_MAX = (1 << 63) - 1
_WORKFLOW_BODY_LIMIT = 8 * 1024 * 1024
_WORKFLOW_JSON_INTEGER_DIGITS = 4096
_GO_WHITE_SPACE = (
    "\t\n\v\f\r "
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)

_DEFINITION_WRITE_PREFIXES = (
    "/api/v1/workflows",
    "/api/v1/local/workflows",
    "/api/v1/workflow-nodes",
    "/api/v1/workflow-edges",
    "/api/v1/experiment-operation-categories",
)


def _path_is_or_below(path: str, prefix: str) -> bool:
    """判断 HTTP 路径是否为指定接口或其子路径。

    参数：``path`` 是当前请求路径；``prefix`` 是工作流 API 的稳定路径前缀。
    返回：路径完全匹配前缀或位于其下一级时为 ``True``。异常：无；函数不做
    URL 解码，也不改变请求。状态不变量：只按完整路径段匹配，避免把相似名称
    的其他接口误判为工作流定义接口。
    """

    return path == prefix or path.startswith(f"{prefix}/")


def _is_definition_write_request(path: str, method: str) -> bool:
    """识别需要受 Runtime 定义编辑能力保护的工作流写请求。

    参数：``path`` 与 ``method`` 来自一次公开 HTTP 请求。返回：请求会改变工作流、
    实验操作、图、节点、连线、类别或发布事实时为 ``True``；任务创建、任务控制、
    运行预检和其他运行时处置返回 ``False``。异常：无。状态不变量：定义编辑能力
    关闭时只拒绝定义写入，不阻断查看和工作流任务执行。
    """

    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if not any(_path_is_or_below(path, prefix) for prefix in _DEFINITION_WRITE_PREFIXES):
        return False
    # 运行预检使用 POST 传入候选参数，但只读计算，不改变工作流定义。
    if path.endswith("/run-preflight") or path == "/api/v1/workflows/material-source-bindings":
        return False
    return True


def _import_request_error(
    path: str,
    validation_error: RequestValidationError | None = None,
    request_error: Exception | None = None,
) -> WorkflowError:
    """为两条工作流导入接口生成请求层错误说明。

    参数：``path`` 是当前请求路径；``validation_error`` 是可选的 FastAPI 请求
    校验详情；``request_error`` 是读取请求体或解码 JSON 时的受控异常。返回：
    保留业务码 ``1000`` 的工作流错误，并尽量指出具体字段、类型或请求体问题；
    其他路径使用公共参数错误说明。异常：无；该函数不读取请求体，也不改变
    HTTP 状态码。
    """

    context = None
    if path == "/api/v1/workflows/import":
        context = "JSON 工作流导入失败："
        default_message = (
            "请求体必须是合法 JSON 对象，且 nodes、edges、name 等字段类型必须符合"
            "接口要求"
        )
    elif path == "/api/v1/local/workflows/import-python":
        context = "Python 工作流导入失败："
        default_message = (
            "请同时提供 UTF-8 编码的 .py 文件和 X-Workflow-Filename 文件名请求头"
        )
    else:
        return WorkflowError("invalid_input")

    if request_error is not None:
        raw_message = str(request_error)
        if "超过公共预算" in raw_message:
            detail = "请求体大小超过 8 MiB 限制，请缩小工作流文件或 JSON 后重新上传"
        elif "Content-Length" in raw_message:
            detail = "请求头 Content-Length 无效，请重新发送完整的工作流请求"
        elif path == "/api/v1/workflows/import":
            detail = "请求体不是合法 JSON，请检查引号、括号、逗号和字段类型"
        else:
            detail = default_message
        return WorkflowError("invalid_input", message=f"{context}{detail}")
    if validation_error is None:
        return WorkflowError("invalid_input", message=f"{context}{default_message}")
    issues = _describe_import_validation_errors(validation_error)
    detail = "；".join(issues) if issues else default_message
    return WorkflowError("invalid_input", message=f"{context}{detail}")


def _describe_import_validation_errors(
    error: RequestValidationError,
) -> list[str]:
    """把 FastAPI 的导入请求校验项转换成用户可直接修改的提示。

    参数：``error`` 是请求模型校验异常。返回：最多四条不含原始输入值的中文
    字段说明；不向响应泄露源码、文件内容或内部堆栈。异常：单条校验项格式
    异常时跳过该项，由调用方回退到接口级说明。
    """

    descriptions: list[str] = []
    for item in error.errors()[:4]:
        if not isinstance(item, dict):
            continue
        location = item.get("loc")
        if not isinstance(location, (tuple, list)):
            location = ()
        parts = [str(part) for part in location if part not in {"body", "header"}]
        field = ".".join(parts) if parts else "请求体"
        if location and location[0] == "header":
            field = (
                "请求头 X-Workflow-Filename"
                if field.lower() == "x-workflow-filename"
                else f"请求头 {field}"
            )
        elif field != "请求体":
            field = f"字段 {field}"
        error_type = str(item.get("type") or "")
        if error_type == "missing" or error_type.endswith("_missing"):
            reason = "不能为空，必须提供"
        elif field == "请求体" and "model" in error_type:
            reason = "必须是 JSON 对象"
        elif "list" in error_type:
            reason = "必须是数组"
        elif "dict" in error_type or "mapping" in error_type:
            reason = "必须是 JSON 对象"
        elif "string" in error_type:
            reason = "必须是字符串"
        elif "bytes" in error_type:
            reason = "必须是文件内容"
        elif "bool" in error_type:
            reason = "必须是布尔值（true 或 false）"
        elif "int" in error_type or "float" in error_type:
            reason = "必须是数字"
        elif "literal" in error_type:
            reason = "只能填写接口允许的固定值"
        elif "extra" in error_type:
            reason = "不是接口支持的字段"
        else:
            reason = "格式不正确"
        separator = "" if field == "请求体" else " "
        descriptions.append(f"{field}{separator}{reason}")
    return descriptions


def _with_import_context(error: WorkflowError, *, context: str) -> WorkflowError:
    """为导入服务错误补充 JSON 或 Python 来源，同时避免重复前缀。

    参数：``error`` 是服务层返回的稳定业务错误；``context`` 是以冒号结尾的
    ``JSON 工作流导入失败：`` 或 ``Python 工作流导入失败：``。返回：保留原业务
    错误码、只在消息尚未带来源时补充上下文的错误；HTTP 状态与响应结构不变。
    异常：无。
    """

    if error.message.startswith(context):
        return error
    return WorkflowError(error.code, message=f"{context}{error.message}")


async def _read_limited_body(request: Request) -> bytes:
    """增量读取工作流（Workflow）请求体并在首次超限时停止。

    参数说明：`request` 是当前 ASGI 请求。函数先校验声明长度，再逐块读取，
    最多保留 8 MiB；返回缓存后的原始字节，超限或非法长度抛出 `ValueError`。
    """

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length, 10)
        except ValueError:
            raise ValueError("Content-Length 无效") from None
        if declared_length < 0 or declared_length > _WORKFLOW_BODY_LIMIT:
            raise ValueError("工作流请求体超过公共预算")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _WORKFLOW_BODY_LIMIT:
            raise ValueError("工作流请求体超过公共预算")
        body.extend(chunk)
    payload = bytes(body)
    request._body = payload
    return payload


class _BackendJSONRoute(APIRoute):
    """限制有请求体的路由，并按后端（Backend）规则预载 JSON。"""

    def get_route_handler(self):
        """构造只对有请求体路由执行预算与 JSON 解码的处理器。"""

        route_handler = super().get_route_handler()
        expects_body = self.body_field is not None

        async def backend_json_route_handler(request: Request) -> Response:
            """在业务处理前执行模式门禁和请求体预算校验。

            参数：``request`` 是单次 HTTP 请求。返回：路由处理结果或统一业务
            错误响应。异常：请求体超限、JSON 非法或定义编辑能力关闭时写入定义，
            返回稳定错误，不进入具体业务处理器。状态不变量：写权限只控制定义
            写入，查看和运行权限由各自能力字段决定。
            """

            if not get_runtime_capabilities().can_edit_definitions and _is_definition_write_request(
                request.url.path,
                request.method,
            ):
                return _error(WorkflowError("read_only_mode"))

            if expects_body:
                content_type = request.headers.get("content-type", "")
                mime = content_type.split(";", 1)[0].strip().lower()
                try:
                    body = await _read_limited_body(request)
                    if mime == "application/json" or mime.endswith("+json"):
                        request._json = decode_json_bytes(
                            body,
                            max_integer_digits=_WORKFLOW_JSON_INTEGER_DIGITS,
                        )
                except (
                    OverflowError,
                    UnicodeError,
                    ValueError,
                ) as error:
                    return _error(
                        _import_request_error(
                            request.url.path,
                            request_error=error,
                        )
                    )
            return await route_handler(request)

        return backend_json_route_handler


def _parse_non_negative_int64_decimal(value: str) -> int:
    """按后端（Backend）规则解析 SSE 游标。

    参数：``value`` 是已按 Go 空白规则裁剪的十进制文本。返回：非负 int64。
    异常：格式、负值或上溢时抛 ``ValueError``；不接受小数或指数形式。
    """

    if _SIGNED_DECIMAL.fullmatch(value) is None:
        raise ValueError
    negative = value.startswith("-")
    digits = value[1:] if value[:1] in {"+", "-"} else value
    significant = digits.lstrip("0") or "0"
    if negative and significant != "0":
        raise ValueError
    maximum = str(_INT64_MAX)
    if len(significant) > len(maximum) or (
        len(significant) == len(maximum) and significant > maximum
    ):
        raise ValueError
    return int(significant, 10)


def _parse_positive_decimal(value: str, *, maximum: int) -> int:
    """解析严格正十进制页长并限制公开上界。"""

    if _SIGNED_DECIMAL.fullmatch(value) is None or value.startswith("-"):
        raise ValueError
    digits = value[1:] if value.startswith("+") else value
    significant = digits.lstrip("0") or "0"
    if significant == "0":
        raise ValueError
    maximum_text = str(maximum)
    if len(significant) > len(maximum_text) or (
        len(significant) == len(maximum_text) and significant > maximum_text
    ):
        raise ValueError
    return int(significant, 10)


class WorkflowWriteRequest(_BackendModel):
    """创建与更新工作流共用的公开字段。"""

    name: str = Field(
        description="工作流或实验操作的显示名称",
        examples=["样品前处理"],
    )
    tags: List[Any] = Field(
        default_factory=list,
        description="检索和兼容分类标签；未提供时为空数组",
        examples=[["chemistry"]],
    )
    description: Optional[str] = Field(
        default=None,
        description="用途说明；可不填写",
        examples=["完成称量、溶解和转运"],
    )
    meta_data: Dict[str, Any] = Field(
        default_factory=dict,
        description="业务扩展元数据；未提供时为空对象",
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _json_array(cls, value: Any) -> List[Any]:
        return normalize_json_array(value)

    @field_validator("meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class WorkflowCreateRequest(WorkflowWriteRequest):
    """创建工作流；旧调用省略类型时默认创建普通工作流。"""

    workflow_type: Literal["normal", "experiment_operation"] = Field(
        default="normal",
        description="normal 创建普通工作流；experiment_operation 创建实验操作",
    )
    operation_category_uuid: Optional[str] = Field(
        default=None,
        description=(
            "实验操作所属类别 UUID；普通工作流不能填写，实验操作可不分类"
        ),
        examples=["1ade6f36-40a9-58fe-a6c8-c7418e651a49"],
    )


class WorkflowUpdateRequest(WorkflowWriteRequest):
    """更新工作流；类型可省略或重复当前值，但不能在两类之间转换。"""

    workflow_type: Optional[Literal["normal", "experiment_operation"]] = Field(
        default=None,
        description="可省略或重复当前类型；不允许在普通工作流和实验操作间转换",
    )
    operation_category_uuid: Optional[str] = Field(
        default=None,
        description="实验操作类别 UUID；显式传 null 表示清空类别",
        examples=["1ade6f36-40a9-58fe-a6c8-c7418e651a49"],
    )


class GraphWriteRequest(_BackendModel):
    revision: int = Field(ge=1, le=_INT64_MAX, strict=True)
    nodes: List[WorkflowNodeWrite] = Field(default_factory=list)
    edges: List[WorkflowEdgeWrite] = Field(default_factory=list)

    @field_validator("nodes", "edges", mode="before")
    @classmethod
    def _json_array(cls, value: Any) -> List[Any]:
        return [] if value is None else value


class WorkflowNodeCreateRequest(_StrictModel):
    """向现有工作流增加一个节点的公共 DTO。"""

    workflow_node_template_uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    material_uuid: Optional[str] = None
    name: str = ""
    type: str = ""
    pose: Dict[str, Any] = Field(default_factory=dict)
    param: Optional[Dict[str, Any]] = None
    manual_confirmation: Dict[str, Any] = Field(default_factory=dict)
    execution_policy: Dict[str, Any] = Field(default_factory=dict)
    disabled: bool = False
    minimized: bool = False
    script: Optional[str] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "pose",
        "manual_confirmation",
        "execution_policy",
        "meta_data",
        mode="before",
    )
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class WorkflowNodePatchRequest(_StrictModel):
    """Backend 允许局部更新的节点字段。"""

    parent_uuid: Optional[str] = None
    material_uuid: Optional[str] = None
    name: Optional[str] = None
    pose: Optional[Dict[str, Any]] = None
    param: Optional[Dict[str, Any]] = None
    manual_confirmation: Optional[Dict[str, Any]] = None
    execution_policy: Optional[Dict[str, Any]] = None
    disabled: Optional[bool] = None
    minimized: Optional[bool] = None
    script: Optional[str] = None
    description: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None


class WorkflowEdgeCreateRequest(_BackendModel):
    """向完整图增加一条依赖或数据连线的公共 DTO。"""

    source_node_uuid: str
    target_node_uuid: str
    source_handle_uuid: str
    target_handle_uuid: str
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class WorkflowBatchDeleteRequest(_BackendModel):
    """原子删除一组节点和连线的公共 DTO。"""

    node_uuids: List[str] = Field(default_factory=list)
    edge_uuids: List[str] = Field(default_factory=list)


class WorkflowDuplicateRequest(_BackendModel):
    """工作流或节点复制时可选的新名称。"""

    name: Optional[str] = None


class LegacyWorkflowImportRequest(_BackendModel):
    """兼容直接对象和 ``data`` 包装的旧版工作流导入载荷。"""

    workflow_name: str = ""
    name: str = ""
    tags: List[Any] = Field(default_factory=list)
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    workflow_type: Literal["normal", "experiment_operation"] = "normal"
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)
    inventory_requirements: List[Dict[str, Any]] = Field(default_factory=list)
    data: Optional[Dict[str, Any]] = None


class PublishWorkflowContractRequest(_StrictModel):
    """把指定修订的工作流切换为已发布状态的命令。"""

    revision: int = Field(
        ge=1,
        le=_INT64_MAX,
        strict=True,
        description="要发布的当前工作流修订号；必须与服务端当前修订一致",
        examples=[1],
    )


class InsertCompositeWorkflowRequest(_StrictModel):
    """在父图中插入一个已发布工作流调用。"""

    revision: int = Field(ge=1, le=_INT64_MAX, strict=True)
    contract_uuid: str
    invocation_uuid: Optional[str] = None
    device_bindings: Dict[str, str] = Field(default_factory=dict)
    pose: Dict[str, Any] = Field(default_factory=dict)
    param: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("device_bindings", "pose", "param", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        """规范化组合调用的对象字段并拒绝数组或标量。"""

        return normalize_json_object(value)


class WorkflowInventoryBindingRequest(_StrictModel):
    """把一个逻辑库存需求绑定到本次任务实际使用的库存实例。"""

    requirement_key: str
    inventory_type: Literal["reagent", "current_substance"]
    inventory_uuid: str
    reserved_quantity: float = Field(gt=0, allow_inf_nan=False)
    quantity_unit: str

    @field_validator("inventory_uuid")
    @classmethod
    def _inventory_uuid(cls, value: str) -> str:
        """规范库存实例 UUID；nil 或非法值由 Pydantic 映射为请求错误。"""

        return validate_uuid(value)

    @field_validator("requirement_key", "quantity_unit")
    @classmethod
    def _required_text(cls, value: str) -> str:
        """去除两端空白并拒绝空逻辑键或单位。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class WorkflowTaskCreateRequest(_BackendModel):
    workflow_uuid: str
    run_mode: str = "normal"
    target_node_uuid: Optional[str] = None
    priority: WorkflowTaskPriority = Field(
        default=WorkflowTaskPriority.NORMAL,
        description="任务优先级：normal 普通任务，high 高优先级任务",
        examples=["normal"],
    )
    input: Dict[str, Any] = Field(default_factory=dict)
    inventory_bindings: List[WorkflowInventoryBindingRequest] = Field(
        default_factory=list
    )
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("input", "meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        """规范化任务输入和公开元数据对象。

        参数：``value`` 是 Pydantic 解码前的 JSON 值。返回：独立 JSON 对象，
        显式 ``null`` 按后端（Backend）零值对象处理。异常：非对象或非法 JSON
        值由 ``normalize_json_object`` 抛出并映射为请求错误。
        """

        return normalize_json_object(value)


class WorkflowRunPreflightRequest(_BackendModel):
    """以候选任务入口载荷执行只读运行预检。"""

    run_mode: str = "normal"
    target_node_uuid: Optional[str] = None
    input: Dict[str, Any] = Field(default_factory=dict)
    inventory_bindings: List[WorkflowInventoryBindingRequest] = Field(
        default_factory=list
    )

    @field_validator("input", mode="before")
    @classmethod
    def _input_object(cls, value: Any) -> Dict[str, Any]:
        """规范候选入口参数；非 JSON 对象由 Pydantic 映射为 422。"""

        return normalize_json_object(value)


class WorkflowBatchRunPreflightItem(WorkflowRunPreflightRequest):
    """一次候选执行；同一工作流可由不同输入重复出现。"""

    model_config = ConfigDict(extra="forbid")
    input: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="候选入口参数；沿用单份接口，将 null 按空对象处理",
    )
    workflow_uuid: str = Field(description="要检查的工站工作流编号")
    revision_fingerprint: Optional[HashToken] = Field(
        default=None,
        description="指定已发布合同的冻结图指纹；不传沿用当前定义，指定后不回退最新定义。",
    )
    input_ready: bool = Field(default=True, strict=True, description="输入是否完整；false 时做基础检查，输入和库存延后，等待上游本身不阻断预检")

    @model_validator(mode="after")
    def _pending_input_has_no_candidates(self) -> "WorkflowBatchRunPreflightItem":
        """未就绪时不接受推测输入或绑定，避免它们被误当成实际候选。"""

        if not self.input_ready and (self.input or self.inventory_bindings):
            raise ValueError("输入未就绪时不能提供候选参数或库存绑定")
        return self

    @field_validator("workflow_uuid")
    @classmethod
    def _workflow_uuid(cls, value: str) -> str:
        """校验每项工作流身份，不把请求位置当作运行任务身份。"""

        return validate_uuid(value)


class WorkflowBatchRunPreflightRequest(_StrictModel):
    """前端组装同一工站候选入口，不接收中心实验身份或下游占位输入。"""

    items: List[WorkflowBatchRunPreflightItem] = Field(
        min_length=1, max_length=100,
        description="1 至 100 次候选执行，按请求顺序返回报告，重复工作流独立计量",
    )


class WorkflowMaterialSourceRequest(_StrictModel):
    """中心只读固定发布合同及实际入口，不接收调用方提供的库存身份。"""
    workflow_uuid: UUID
    revision_fingerprint: HashToken
    input: Dict[str, Any] = Field(default_factory=dict)


class StationWorkflowInvocationRequest(_StrictModel):
    """Backend 提交冻结发布修订或兼容旧名称调用的工站 DTO。"""

    task_uuid: Optional[str] = None
    workflow_id: Optional[str] = None
    revision_fingerprint: Optional[HashToken] = None
    normalized_input: Optional[Dict[str, Any]] = None
    deadline: Optional[str] = None
    backend_task_uuid: Optional[str] = None
    invocation_key: str
    workflow_name: Optional[str] = None
    input: Optional[Dict[str, Any]] = None
    priority: WorkflowTaskPriority | FiniteFloat = Field(
        default=WorkflowTaskPriority.NORMAL,
        description="任务优先级默认 normal；支持 normal/high，显式有限数值仅用于既有调用方兼容",
        examples=["normal"],
    )
    inventory_bindings: List[WorkflowInventoryBindingRequest] = Field(
        default_factory=list
    )
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_uuid", "backend_task_uuid")
    @classmethod
    def _backend_task_identity(cls, value: Optional[str]) -> Optional[str]:
        """规范 Backend Task UUID 并拒绝 nil 或非法身份。"""

        return None if value is None else validate_uuid(value)

    @field_validator("workflow_id")
    @classmethod
    def _workflow_identity(cls, value: Optional[str]) -> Optional[str]:
        """规范发布工作流 UUID；旧名称调用没有该字段。"""

        return None if value is None else validate_uuid(value)

    @field_validator("invocation_key", "workflow_name")
    @classmethod
    def _required_text(cls, value: Optional[str]) -> Optional[str]:
        """规范调用键和工作流名称，并限制持久索引文本长度。"""

        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 256:
            raise ValueError("value must be 1..256 characters")
        return normalized

    @field_validator("input", "normalized_input", mode="before")
    @classmethod
    def _optional_json_object(cls, value: Any) -> Optional[Dict[str, Any]]:
        """规范化两种协议形状中的可选入口参数对象。"""

        if value is None:
            return None
        return normalize_json_object(value)

    @field_validator("meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        """规范化审计元数据对象。"""

        return normalize_json_object(value)

    @model_validator(mode="after")
    def _invocation_shape(self) -> "StationWorkflowInvocationRequest":
        """要求完整采用规范发布调用或旧名称调用，禁止两套身份混用。"""

        canonical = any(
            value is not None
            for value in (
                self.task_uuid,
                self.workflow_id,
                self.revision_fingerprint,
                self.normalized_input,
                self.deadline,
            )
        )
        if canonical:
            if (
                self.task_uuid is None
                or self.workflow_id is None
                or self.revision_fingerprint is None
                or self.normalized_input is None
                or self.backend_task_uuid is not None
                or self.workflow_name is not None
                or self.input is not None
            ):
                raise ValueError("规范工站调用身份、修订或输入不完整")
            return self
        if (
            self.backend_task_uuid is None
            or self.workflow_name is None
            or self.input is None
        ):
            raise ValueError("兼容工站调用身份、名称或输入不完整")
        return self


class DebugWorkflowTaskCreateRequest(_BackendModel):
    workflow_uuid: str
    start_node_uuids: List[str]
    breakpoint_node_uuids: List[str] = Field(default_factory=list)
    priority: WorkflowTaskPriority = Field(
        default=WorkflowTaskPriority.NORMAL,
        description="任务优先级：normal 普通任务，high 高优先级任务",
        examples=["normal"],
    )
    input: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("start_node_uuids", "breakpoint_node_uuids", mode="before")
    @classmethod
    def _json_array(cls, value: Any) -> List[Any]:
        return normalize_json_array(value)

    @field_validator("input", "meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class DebugCommandScope(_StrictModel):
    type: str
    hold_uuid: str


class DebugWorkflowTaskCommandRequest(_StrictModel):
    type: str
    scope: DebugCommandScope
    idempotency_key: str


class WorkflowTaskCommandRequest(_StrictModel):
    type: str
    target_node_uuid: Optional[str] = None
    idempotency_key: str
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class WorkflowTaskJobParamUpdateRequest(_StrictModel):
    """修改暂停任务中尚未派发 Job 的参数补丁。"""

    param: Dict[str, Any]

    @field_validator("param", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class DeviceActionRunCreateRequest(_StrictModel):
    """Backend 规范的设备单动作运行（DeviceActionRun）创建 DTO。"""

    material_uuid: str
    workflow_node_template_uuid: str
    param: Optional[Dict[str, Any]] = None
    execution_policy: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("execution_policy", "meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        """规范化设备动作请求中的可选 JSON 对象。"""

        return normalize_json_object(value)


class ManualConfirmationDecisionRequest(_StrictModel):
    """人工确认批准或拒绝的公共 DTO。"""

    action: Literal["approve", "reject"]


class HumanInteractionReply(_StrictModel):
    answers: dict[str, StrictStr | StrictBool | StrictInt | StrictFloat] | None = None
    choice: str | None = None
    text: str | None = None
    values: list[StrictFloat] | None = None
    x: list[StrictFloat] | None = None
    y: list[StrictFloat] | None = None
    corner_x: list[StrictFloat] | None = None
    corner_y: list[StrictFloat] | None = None


class WorkflowTaskExecutionLockReleaseRequest(_StrictModel):
    """工作流任务执行锁人工释放请求；必须携带物理安全确认和并发快照。"""

    expected_claim_uuid: str
    expected_fencing_token: StrictInt = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)
    physical_settlement_confirmed: StrictBool


class WorkflowInterventionDecisionRequest(_StrictModel):
    """按修订选择一个设备已经提供的干预方案。"""

    revision: int = Field(ge=1, le=_INT64_MAX, strict=True)
    option_id: str
    # Local 模式兼容需要服务端执行的兜底动作结果；普通 retry/skip/abort 不传。
    result: Any = None


class UncertainJobResolutionRequest(_StrictModel):
    """物理结果不确定的运行中或失败作业安全处置请求。"""

    resolution: str
    reason: str
    device_command_id: Optional[str] = None


class QuantityConsumptionCorrection(_StrictModel):
    """操作员核对后的单个库存实例实际用量。"""

    inventory_type: Literal["reagent", "current_substance"]
    inventory_uuid: UUID
    actual_quantity: float = Field(ge=0, allow_inf_nan=False)
    quantity_unit: str = Field(min_length=1)


class QuantityReconciliationRequest(_StrictModel):
    """更正请求不授权重新派发设备，只允许结算已有成功结果。"""

    consumptions: List[QuantityConsumptionCorrection] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1)


class FailedMaterialTransferSettlementRequest(_StrictModel):
    """失败转运在设备停止后提交实际物料位置的结算请求。"""

    actual_change_set: Dict[str, Any]
    reason: str


class DraftWriteRequest(_StrictModel):
    python_source: str
    expected_draft_hash: Optional[HashToken]
    expected_workflow_revision: int = Field(
        ge=1,
        le=_INT64_MAX,
        strict=True,
    )


class ApplyRequest(_StrictModel):
    """只携带服务端签发候选哈希（Candidate Hash）的应用命令。"""

    candidate_hash: HashToken


class _BackendJSONResponse(JSONResponse):
    """Render deeply nested Backend JSON without process-global recursion state."""

    def render(self, content: Any) -> bytes:
        return encode_json(content)


def _public_data(data: Any) -> Any:
    """递归移除后端（Backend）迁移已删除的公共字段。

    参数：``data`` 是服务层投影或嵌套集合。返回：不共享容器的公共投影；任务
    输入保留，尚未进入当前迁移合同的输出隐藏。异常：无。
    """

    if isinstance(data, list):
        return [_public_data(value) for value in data]
    if not isinstance(data, dict):
        return data
    result = {key: _public_data(value) for key, value in data.items()}
    if "workflow_uuid" in result and "pose" in result and "param" in result:
        result.pop("status", None)
    return result


def _success(data: Any = None, *, status: int = 200) -> _BackendJSONResponse:
    content: Dict[str, Any] = {"code": 0}
    if data is not None:
        content["data"] = _public_data(data)
    return _BackendJSONResponse(status_code=status, content=content)


_CONFLICT_ERROR_CODES = frozenset(
    {
        "conflict",
        "draft_hash_conflict",
        "workflow_revision_conflict",
        "candidate_hash_conflict",
        "template_catalog_conflict",
        "candidate_not_ready",
        "draft_invalid",
        "candidate_invalid",
        "workflow_identity_mismatch",
        "source_function_conflict",
        "invalid_composite_child_type",
        "invalid_composite_child_status",
        "debug_task_conflict",
        "preflight_failed",
    }
)


def _business_code(error_code: str) -> int:
    """把领域错误分类映射为稳定的 Backend 数值业务码。

    参数：``error_code`` 是工作流服务的内部错误分类。返回：响应体 ``code``；
    HTTP 状态码仍由具体路由自行决定，调用方不能用该数值替代 HTTP 状态。
    异常：无；未知分类使用通用内部错误码 ``1``，避免把内部名称直接暴露给前端。
    """

    if error_code in {
        "invalid_input",
        "invalid_composite_child_type",
        "invalid_control_region",
        "material_shortage_unhandled",
        "graph_not_executable",
        "invalid_material_source",
        "material_flow_fan_out",
        "material_template_mismatch",
    }:
        return 1000
    if error_code in {"not_found", "workflow_not_found"}:
        return 3002
    if error_code in _CONFLICT_ERROR_CODES:
        return 3003
    if error_code in {
        "read_only_mode",
        "debug_not_allowed",
        "device_action_not_allowed",
    }:
        return 1001
    if error_code == "template_catalog_unavailable":
        return 5001
    return 1


def _error(error: WorkflowError) -> _BackendJSONResponse:
    business_code = _business_code(error.code)
    error_content = {"msg": error.message}
    if error.code in {
        "workflow_identity_mismatch",
        "debug_not_allowed",
        "debug_task_conflict",
        "device_action_not_allowed",
        "preflight_failed",
    }:
        # Backend 包络保持 HTTP 200；该窄符号码让前端区分身份拒绝与
        # 需要重读远端版本的普通 3003 CAS 冲突。
        error_content["code"] = error.code
    if error.details:
        error_content["details"] = error.details
    return _BackendJSONResponse(
        status_code=200,
        content={
            "code": business_code,
            "error": error_content,
        },
    )


def format_sse_event(event: Dict[str, Any]) -> str:
    """把一个持久失效通知编码为服务器发送事件（SSE）帧。

    参数：``event`` 含全局序号、事件类型和小型身份载荷。返回：UTF-8 文本帧；
    客户端必须再用 REST 复原（Rehydrate）权威事实。异常：记录缺字段或载荷不能
    JSON 编码时传播，不从内存历史补值。
    """

    payload = json.dumps(
        event["data"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event['id']}\nevent: {event['event']}\ndata: {payload}\n\n"


def create_workflow_router(
    service: WorkflowService,
    *, wait_until_ready: Callable[[], Awaitable[bool]] | None = None,
    inventory_store_provider: Callable[[], Any | None] | None = None,
) -> APIRouter:
    """围绕唯一工作流权威创建 Backend-shaped HTTP Router。

    参数：``service`` 是注入的工作流应用服务；延迟服务须传入后台就绪等待
    ``wait_until_ready``，False 表示启动失败或禁用。返回同时承载工作流、任务、
    作业、设备单动作运行（DeviceActionRun）及创作接口的 FastAPI Router。
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        from unilabos.workflow.event_retention import maintain_frontend_events

        async with maintain_frontend_events(service, wait_until_ready=wait_until_ready):
            yield

    router = APIRouter(
        prefix="/api/v1",
        tags=["workflow"],
        route_class=_BackendJSONRoute,
        lifespan=lifespan,
    )
    from unilabos.app.error_handling_api import create_error_handling_router

    router.include_router(
        create_error_handling_router(
            service_provider=lambda: getattr(service, "error_handling", None),
        )
    )
    from unilabos.app.station_events import configured_inventory_store, create_station_events_router

    router.include_router(create_station_events_router(
        service, inventory_store_provider or configured_inventory_store,
    ))

    def _with_workflow_status(payload: Any) -> Any:
        """定义图只返回当前层级及子调用引用，并补充当前源码/已发布状态。

        参数：``payload`` 是服务层返回的工作流或完整图投影。返回：输入不是
        完整图时原样返回；包含 ``workflow.uuid`` 的图则复制顶层对象，并在其中
        增加派生的 ``workflow.status`` 与顶层实验操作类别，并移除内部元数据中
        的类别副本。异常：状态读取错误原样传播；不修改服务层图快照，避免派生
        字段进入 AST、发布合同或任务快照。
        """

        if not isinstance(payload, dict):
            return payload
        workflow = payload.get("workflow")
        if not isinstance(workflow, dict) or not isinstance(workflow.get("uuid"), str):
            return payload
        public_workflow = service.get_workflow(workflow["uuid"])
        projected = dict(payload)
        projected["workflow"] = {
            **workflow,
            "meta_data": public_workflow["meta_data"],
            "operation_category_uuid": public_workflow["operation_category_uuid"],
            "status": public_workflow["status"],
        }
        from unilabos.workflow.definition_hierarchy import definition_graph

        # 定义读写只呈现当前层，子操作详情由它自己的 Workflow UUID 读取。
        # 该投影不进入发布合同、预检、任务快照或调度图。
        return definition_graph(projected)

    def _visible_workflow(workflow_uuid: str) -> dict[str, Any]:
        """读取当前 Runtime 查看能力允许公开的工作流，否则按不存在处理。

        参数：``workflow_uuid`` 是工作流或实验操作的稳定身份。返回：带派生状态
        的公开工作流读模型。异常：工作流不存在、身份非法或查看草稿能力关闭时
        抛出 ``WorkflowError('not_found')``；这样不会通过详情接口泄露未授权定义
        或实验操作。
        """

        workflow = service.get_workflow(workflow_uuid)
        if not is_workflow_visible(workflow):
            raise WorkflowError("not_found")
        return workflow

    def _execution_workflow(workflow_uuid: str) -> dict[str, Any]:
        """校验工作流可见且满足当前 Runtime 的运行权限。"""

        workflow = _visible_workflow(workflow_uuid)
        if (
            not get_runtime_capabilities().can_run_unpublished
            and workflow.get("status") != "published"
        ):
            raise WorkflowError("not_found")
        return workflow

    def _require_debug_capability() -> None:
        """拒绝当前 Runtime 禁止的单步和调试写操作。"""

        if not get_runtime_capabilities().can_debug:
            raise WorkflowError("debug_not_allowed")

    def _ensure_station_workflow_visible(
        workflow_uuid: str | None,
        workflow_name: str | None,
    ) -> None:
        """在工站按 UUID 或名称提交任务前执行查看与运行能力校验。

        参数：``workflow_uuid`` 和 ``workflow_name`` 来自工站调用请求，二者至少
        由服务层要求其一。返回：工作流在当前 Runtime 能力下可见且可执行时无返回。
        异常：不允许运行未发布定义时，没有可见的已发布普通工作流会抛 ``not_found``，
        不让隐藏的实验操作或源码定义绕过列表接口直接创建任务。状态不变量：本校验只读，不会
        修改工作流或任务事实。
        """

        if workflow_uuid:
            _execution_workflow(workflow_uuid)
            return
        if not workflow_name:
            return
        result = service.list_workflows(
            page=1,
            page_size=100,
            name=workflow_name,
        )
        if not any(
            item.get("name") == workflow_name
            and (
                get_runtime_capabilities().can_run_unpublished
                or item.get("status") == "published"
            )
            for item in result["items"]
        ):
            raise WorkflowError("not_found")

    def _empty_workflow_page(page: int, page_size: int) -> dict[str, Any]:
        """构造查看草稿能力关闭时隐藏筛选条件对应的空工作流页。

        参数：``page`` 和 ``page_size`` 是调用方的分页请求。返回：与工作流列表
        相同形状的空页；页码至少为 1，页长沿用服务层的 20/100 约束。异常：无。
        状态不变量：隐藏的实验操作、源码状态和类别筛选不会触发一次无关的定义
        查询，也不会把其他普通工作流混入结果。
        """

        normalized_page = max(page, 1)
        normalized_page_size = 20 if page_size < 1 else min(page_size, 100)
        return {
            "items": [],
            "has_more": False,
            "page": normalized_page,
            "page_size": normalized_page_size,
        }

    @router.post(
        "/workflows",
        summary="创建工作流或实验操作",
        status_code=201,
        response_model=WorkflowSuccessResponse,
        responses={
            200: {
                "model": BackendErrorResponse,
                "description": "字段组合、类型或类别校验未通过",
            }
        },
    )
    def create_workflow(body: WorkflowCreateRequest) -> JSONResponse:
        """创建普通工作流或带可选类别的实验操作。

        参数：``body`` 是保持旧默认值的完整根字段。返回：新工作流，HTTP 201。
        异常：类型、类别或字段组合非法时由服务层映射为统一业务响应。
        """

        payload = body.model_dump()
        if "operation_category_uuid" not in body.model_fields_set:
            payload.pop("operation_category_uuid", None)
        return _success(service.create_workflow(**payload), status=201)

    @router.post("/workflows/import")
    def import_legacy_workflow(
        body: LegacyWorkflowImportRequest,
    ) -> JSONResponse:
        """校验并在一个事务中导入旧版 JSON 工作流图。

        参数：``body`` 是旧版工作流根对象，支持直接传字段或使用 ``data`` 包装。
        返回：转换为领域包 Python 定义后的完整工作流图，HTTP 201。异常：节点、
        连线、模板引用或工作流类型不合法时返回带具体字段位置的业务错误。
        """

        try:
            imported = service.import_legacy_workflow(payload=body.model_dump())
        except WorkflowError as error:
            raise _with_import_context(
                error,
                context="JSON 工作流导入失败：",
            ) from None
        return _success(_with_workflow_status(imported), status=201)

    @router.post("/local/workflows/import-python", status_code=201)
    def import_python_workflow(
        python_file: Annotated[bytes, Body(media_type="text/x-python")],
        file_name: Annotated[str, Header(alias="X-Workflow-Filename")],
    ) -> JSONResponse:
        """静态校验并原子导入一个 Local 模式 Python 工作流文件。

        参数：请求体是原始 ``.py`` 文件字节，``X-Workflow-Filename`` 是不含路径
        的文件名。返回：新建工作流完整图的统一响应。异常：非 UTF-8、非法文件
        名、AST/模板/图校验失败或身份冲突由公共错误适配器处理。
        """

        try:
            python_source = python_file.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkflowError(
                "invalid_input",
                message=(
                    "Python 工作流导入失败：上传文件不是有效的 UTF-8 文本，"
                    "请将 .py 文件保存为 UTF-8 编码后重新上传"
                ),
            ) from None
        try:
            imported = service.import_python_workflow(
                file_name=file_name,
                python_source=python_source,
            )
        except WorkflowError as error:
            raise _with_import_context(
                error,
                context="Python 工作流导入失败：",
            ) from None
        return _success(_with_workflow_status(imported), status=201)

    @router.get(
        "/workflows",
        summary="查询工作流和实验操作",
        response_model=WorkflowListSuccessResponse | BackendErrorResponse,
    )
    def list_workflows(
        page: int = Query(default=1, description="页码；小于 1 时按第 1 页处理"),
        page_size: int = Query(
            default=20,
            description="每页数量；小于 1 时按 20，超过 100 时按 100 处理",
        ),
        keyword: str = Query(
            default="",
            description="兼容旧前端的名称模糊搜索；name 未传时生效",
            examples=["前处理"],
        ),
        name: Optional[str] = Query(
            default=None,
            description="按名称模糊搜索；同时传 keyword 时以本字段为准",
            examples=["前处理"],
        ),
        workflow_type: Optional[Literal["normal", "experiment_operation"]] = Query(
            default=None,
            description="按类型筛选；不传时同时返回普通工作流和实验操作",
        ),
        status: Optional[Literal["source", "published"]] = Query(
            default=None,
            description=(
                "按当前源码/已发布状态筛选；published 表示当前版本已经发布；"
                "只有已发布的实验操作可以被其他工作流引用；不传时返回全部状态"
            ),
        ),
        operation_category_uuid: Optional[str] = Query(
            default=None,
            description="按实验操作类别 UUID 筛选",
            examples=["1ade6f36-40a9-58fe-a6c8-c7418e651a49"],
        ),
    ) -> JSONResponse:
        """分页读取工作流，可按类型及当前发布状态组合筛选。

        参数：页码、页长和旧 ``keyword`` 保持兼容；新 ``name`` 是同义名称
        筛选且优先于 ``keyword``；类型、状态和实验操作类别筛选均可省略。查看草稿
        能力允许时返回全部类型和状态；关闭时固定只返回已发布普通工作流，且对实验
        操作、源码状态或类别筛选返回空页。返回：统一 Backend 响应中的工作流
        列表与 ``has_more``。异常：非法枚举由请求校验拒绝，服务错误交给公共
        适配器。
        """

        list_options: Dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "name": keyword if name is None else name,
        }
        # 省略新增筛选时保持旧调用形状，避免只实现既有列表合同的适配器被迫
        # 同步升级；真正使用筛选时才把对应条件交给工作流服务。
        if workflow_type is not None:
            list_options["workflow_type"] = workflow_type
        if status is not None:
            list_options["status"] = status
        if operation_category_uuid is not None:
            list_options["operation_category_uuid"] = operation_category_uuid
        effective_type, effective_status, effective_category, always_empty = (
            visible_workflow_filter(
                workflow_type,
                status,
                operation_category_uuid,
            )
        )
        if always_empty:
            return _success(_empty_workflow_page(page, page_size))
        if effective_type is not None:
            list_options["workflow_type"] = effective_type
        if effective_status is not None:
            list_options["status"] = effective_status
        if effective_category is not None:
            list_options["operation_category_uuid"] = effective_category
        result = service.list_workflows(**list_options)
        return _success(
            {
                "items": result["items"],
                "has_more": result["page"] * result["page_size"] < result["total"],
                "page": result["page"],
                "page_size": result["page_size"],
            }
        )

    @router.get(
        "/published-workflow-contracts",
        include_in_schema=False,
    )
    def list_published_workflow_contracts(
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        keyword: str = Query(default=""),
    ) -> JSONResponse:
        """保留旧客户端查询路径，但不把发布合同作为公开业务模型。

        参数：``page``、``page_size`` 和 ``keyword`` 沿用旧客户端调用形状。返回：
        旧版发布合同投影。异常：服务层错误按既有 Backend 包络返回。新前端不得
        使用此兼容路径，应通过工作流列表的 ``workflow_type=experiment_operation``
        与 ``status=published`` 查询当前可复用实验操作；该路径不进入 Swagger，
        避免把历史合同误解为实验操作版本记录。
        """

        if not get_runtime_capabilities().can_view_drafts:
            return _success(_empty_workflow_page(page, page_size))
        return _success(
            service.list_published_workflow_contracts(
                page=page,
                page_size=page_size,
                keyword=keyword,
            )
        )

    @router.get(
        "/workflows/{workflow_uuid}/referenced-by",
        summary="查询引用当前实验操作的工作流",
        response_model=WorkflowListSuccessResponse | BackendErrorResponse,
    )
    def list_referencing_workflows(
        workflow_uuid: WorkflowUUIDPath,
        page: int = Query(default=1, description="页码；小于 1 时按第 1 页处理"),
        page_size: int = Query(
            default=20,
            description="每页数量；小于 1 时按 20，超过 100 时按 100 处理",
        ),
    ) -> JSONResponse:
        """返回当前引用指定实验操作或工作流的父工作流。

        参数：``workflow_uuid`` 是被引用定义的稳定身份；页码与页长沿用工作流
        列表规则。返回：父工作流公开摘要和 ``has_more``。异常：目标身份非法或
        不存在时由公共错误适配器处理；没有引用时返回空数组。
        """

        _visible_workflow(workflow_uuid)
        if not get_runtime_capabilities().can_view_drafts:
            result = service.list_referencing_workflows(
                workflow_uuid,
                page=1,
                page_size=100,
            )
            visible_items = [
                item for item in result["items"] if is_workflow_visible(item)
            ]
            normalized_page = max(page, 1)
            normalized_page_size = min(max(page_size, 1), 100)
            offset = (normalized_page - 1) * normalized_page_size
            result = {
                "items": visible_items[offset : offset + normalized_page_size],
                "total": len(visible_items),
                "page": normalized_page,
                "page_size": normalized_page_size,
            }
        else:
            result = service.list_referencing_workflows(
                workflow_uuid,
                page=page,
                page_size=page_size,
            )
        return _success(
            {
                "items": result["items"],
                "has_more": result["page"] * result["page_size"] < result["total"],
                "page": result["page"],
                "page_size": result["page_size"],
            }
        )

    @router.get(
        "/workflows/{workflow_uuid}",
        summary="查询工作流或实验操作详情",
        response_model=WorkflowSuccessResponse | BackendErrorResponse,
    )
    def get_workflow(workflow_uuid: WorkflowUUIDPath) -> JSONResponse:
        """返回当前 Runtime 查看能力允许展示的工作流详情。

        参数：``workflow_uuid`` 是工作流或实验操作 UUID。返回：公开工作流读模型。
        异常：查看草稿能力关闭时访问未发布或实验操作按不存在处理。状态不变量：
        返回结果一定通过当前 Runtime 的可见性校验。
        """

        return _success(_visible_workflow(workflow_uuid))

    @router.put(
        "/workflows/{workflow_uuid}",
        summary="更新工作流或实验操作",
        response_model=WorkflowSuccessResponse | BackendErrorResponse,
    )
    def update_workflow(
        workflow_uuid: WorkflowUUIDPath,
        body: WorkflowUpdateRequest,
    ) -> JSONResponse:
        """更新工作流根字段，并区分类别省略与显式清空。

        参数：路径 UUID 定位工作流，``body`` 是完整旧字段与可选新增字段。返回：
        更新后工作流。异常：修订、源码或类别冲突由服务层统一处理。
        """

        payload = body.model_dump()
        if "operation_category_uuid" not in body.model_fields_set:
            payload.pop("operation_category_uuid", None)
        return _success(service.update_workflow(workflow_uuid, **payload))

    @router.delete(
        "/workflows/{workflow_uuid}",
        summary="删除工作流或实验操作",
        response_model=BackendEmptySuccessResponse | BackendErrorResponse,
    )
    def delete_workflow(workflow_uuid: WorkflowUUIDPath) -> JSONResponse:
        service.delete_workflow(workflow_uuid)
        return _success()

    @router.get("/workflows/{workflow_uuid}/graph")
    def get_graph(workflow_uuid: str) -> JSONResponse:
        """返回当前模式允许展示的工作流图。

        参数：``workflow_uuid`` 是工作流 UUID。返回：工作流图及派生状态。异常：
        工作流不存在或当前查看能力不可见时按统一工作流错误返回。状态不变量：先
        校验工作流可见性，再读取图内容。
        """

        _visible_workflow(workflow_uuid)
        return _success(_with_workflow_status(service.get_graph(workflow_uuid)))

    @router.put("/workflows/{workflow_uuid}/graph")
    def save_graph(
        workflow_uuid: str,
        body: GraphWriteRequest,
    ) -> JSONResponse:
        return _success(
            _with_workflow_status(
                service.save_graph(
                    workflow_uuid,
                    revision=body.revision,
                    nodes=body.nodes,
                    edges=body.edges,
                )
            )
        )

    @router.post("/workflows/{workflow_uuid}/nodes")
    def create_workflow_node(
        workflow_uuid: str,
        body: WorkflowNodeCreateRequest,
    ) -> JSONResponse:
        """增加节点，并由完整图校验器原子推进修订。"""

        return _success(
            _with_workflow_status(
                service.create_workflow_node(
                    workflow_uuid,
                    payload=body.model_dump(),
                )
            ),
            status=201,
        )

    @router.get("/workflows/{workflow_uuid}/nodes")
    def list_workflow_nodes(
        workflow_uuid: str,
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        workflow_node_template_uuid: Optional[str] = Query(default=None),
        material_uuid: Optional[str] = Query(default=None),
    ) -> JSONResponse:
        """分页查询指定工作流的节点。"""

        _visible_workflow(workflow_uuid)
        return _success(
            service.list_workflow_nodes(
                workflow_uuid,
                page=page,
                page_size=page_size,
                workflow_node_template_uuid=workflow_node_template_uuid,
                material_uuid=material_uuid,
            )
        )

    @router.post("/workflows/{workflow_uuid}/edges")
    def create_workflow_edge(
        workflow_uuid: str,
        body: WorkflowEdgeCreateRequest,
    ) -> JSONResponse:
        """增加连线，并复用完整图引用与环路校验。"""

        return _success(
            _with_workflow_status(
                service.create_workflow_edge(
                    workflow_uuid,
                    payload=body.model_dump(),
                )
            ),
            status=201,
        )

    @router.post("/workflows/{workflow_uuid}/batch-delete")
    def batch_delete_workflow_graph(
        workflow_uuid: str,
        body: WorkflowBatchDeleteRequest,
    ) -> JSONResponse:
        """一次删除多个节点与连线，失败时整图不变。"""

        return _success(
            _with_workflow_status(
                service.batch_delete_workflow_graph(
                    workflow_uuid,
                    node_uuids=body.node_uuids,
                    edge_uuids=body.edge_uuids,
                )
            )
        )

    @router.post("/workflows/{workflow_uuid}/duplicate")
    def duplicate_workflow(
        workflow_uuid: str,
        body: WorkflowDuplicateRequest,
    ) -> JSONResponse:
        """在单个 SQLite 事务中复制工作流和完整图。"""

        return _success(
            _with_workflow_status(
                service.duplicate_workflow(workflow_uuid, name=body.name)
            ),
            status=201,
        )

    @router.post(
        "/workflows/{workflow_uuid}/publications",
        summary="发布工作流",
        status_code=201,
        response_model=WorkflowPublishSuccessResponse,
        responses={
            200: {
                "model": BackendErrorResponse,
                "description": "工作流修订、图或发布条件校验未通过",
            }
        },
    )
    def publish_workflow_contract(
        workflow_uuid: WorkflowUUIDPath,
        body: PublishWorkflowContractRequest,
    ) -> JSONResponse:
        """把当前工作流修订切换为已发布状态。

        参数：``workflow_uuid`` 是待发布工作流的稳定 UUID，``body.revision`` 是
        调用方确认的当前修订。返回：发布结果；发布成功后，工作流列表和详情会
        返回 ``status=published``。其中，已发布的普通工作流可在具备运行权限时运行；
        已发布的实验操作还可被其他工作流引用。异常：服务层错误按既有 Backend
        包络返回。该接口不是“新增发布记录”操作，返回中的历史扩展字段仅为旧客户端
        兼容保留，前端不应据此实现版本管理。
        """

        return _success(
            service.publish_workflow_contract(
                workflow_uuid,
                revision=body.revision,
            ),
            status=201,
        )

    @router.post("/workflows/{workflow_uuid}/composite-invocations")
    def insert_composite_workflow(
        workflow_uuid: str,
        body: InsertCompositeWorkflowRequest,
    ) -> JSONResponse:
        """原子插入并确定性展开一个不可变组合工作流调用。"""

        return _success(
            _with_workflow_status(
                service.insert_composite_workflow(
                    workflow_uuid,
                    revision=body.revision,
                    contract_uuid=body.contract_uuid,
                    invocation_uuid=body.invocation_uuid,
                    device_bindings=body.device_bindings,
                    pose=body.pose,
                    param=body.param,
                )
            )
        )

    @router.post(
        "/workflows/run-preflight",
        summary="批量检查工站工作流运行条件",
        description="按顺序检查候选入口并合计同一库存需求，不创建任务、不预留资源。带 revision_fingerprint 的中心冻结候选还检查已声明的物料输出：直接返回共享来源时 invalid_material_output_custody 阻断，已知载体占用冲突时 material_output_unavailable 阻断；未知产出或未来独占标为 deferred。input_ready=false 仍保留静态已知阻断。新增检查位于 items[].checks，无指纹的原生预检保持原行为。单项通过不代表整批通过，动态条件在执行时复核。",
        response_model=WorkflowBatchRunPreflightSuccessResponse | BackendErrorResponse,
    )
    def post_workflows_run_preflight(body: WorkflowBatchRunPreflightRequest) -> JSONResponse:
        """前端提供完整候选列表，工站只读检查整批共享库存。"""

        for item in body.items:
            _execution_workflow(item.workflow_uuid)
            if item.run_mode == "step":
                _require_debug_capability()
        response = _success(service.get_workflows_run_preflight([
            item.model_dump() for item in body.items
        ]))
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/workflows/material-source-bindings")
    def material_source_bindings(body: WorkflowMaterialSourceRequest) -> JSONResponse:
        _execution_workflow(str(body.workflow_uuid))
        response = _success(service.material_source_bindings(workflow_uuid=str(body.workflow_uuid),
            revision_fingerprint=body.revision_fingerprint, input_value=body.input))
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/workflows/{workflow_uuid}/run-preflight")
    def get_workflow_run_preflight(
        workflow_uuid: str,
        run_mode: str = Query(default="normal"),
        target_node_uuid: Optional[str] = Query(default=None),
    ) -> JSONResponse:
        """返回不创建任务、不占用资源的候选运行检查报告。"""

        _execution_workflow(workflow_uuid)
        if run_mode == "step":
            _require_debug_capability()
        response = _success(
            service.get_workflow_run_preflight(
                workflow_uuid,
                run_mode=run_mode,
                target_node_uuid=target_node_uuid,
            )
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/workflows/{workflow_uuid}/run-preflight")
    def post_workflow_run_preflight(
        workflow_uuid: str,
        body: WorkflowRunPreflightRequest,
    ) -> JSONResponse:
        """按候选入口参数与共享数量绑定执行零写入预检。"""

        _execution_workflow(workflow_uuid)
        if body.run_mode == "step":
            _require_debug_capability()
        response = _success(
            service.get_workflow_run_preflight(
                workflow_uuid,
                run_mode=body.run_mode,
                target_node_uuid=body.target_node_uuid,
                input_value=body.input,
                inventory_bindings=[
                    binding.model_dump() for binding in body.inventory_bindings
                ],
                evaluate_inventory=True,
            )
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/workflow-nodes/{node_uuid}")
    def get_workflow_node(node_uuid: str) -> JSONResponse:
        """返回当前模式允许展示的节点。

        参数：``node_uuid`` 是节点全局 UUID。返回：节点读模型。异常：节点不存在
        或所属工作流在当前查看能力下不可见时按统一工作流错误返回。状态不变量：节点
        只能在所属工作流通过可见性校验后返回。
        """

        _visible_workflow(service.get_workflow_node_owner(node_uuid))
        return _success(service.get_workflow_node(node_uuid))

    @router.patch("/workflow-nodes/{node_uuid}")
    def patch_workflow_node(
        node_uuid: str,
        body: WorkflowNodePatchRequest,
    ) -> JSONResponse:
        return _success(
            service.patch_workflow_node(
                node_uuid,
                patch=body.model_dump(exclude_unset=True),
            )
        )

    @router.delete("/workflow-nodes/{node_uuid}")
    def delete_workflow_node(node_uuid: str) -> JSONResponse:
        service.delete_workflow_node(node_uuid)
        return _success()

    @router.post("/workflow-nodes/{node_uuid}/duplicate")
    def duplicate_workflow_node(
        node_uuid: str,
        body: WorkflowDuplicateRequest,
    ) -> JSONResponse:
        return _success(
            service.duplicate_workflow_node(node_uuid, name=body.name),
            status=201,
        )

    @router.delete("/workflow-edges/{edge_uuid}")
    def delete_workflow_edge(edge_uuid: str) -> JSONResponse:
        service.delete_workflow_edge(edge_uuid)
        return _success()

    @router.post("/workflow-tasks")
    def create_workflow_task(
        body: WorkflowTaskCreateRequest,
    ) -> JSONResponse:
        """通过公共接口创建一次工作流任务（WorkflowTask）。

        参数：``body`` 携带工作流身份、运行模式、任务优先级和任务输入。返回：
        HTTP 201 的标准任务投影，包含已规范化输入与冻结执行计划（ExecutionPlan）。
        异常：
        当前运行能力下工作流不可执行时按 ``not_found`` 拒绝；其他服务层稳定错误由
        应用异常处理器转换为后端业务响应。
        """

        _execution_workflow(body.workflow_uuid)
        if body.run_mode == "step":
            _require_debug_capability()
        return _success(
            service.create_workflow_task(
                workflow_uuid=body.workflow_uuid,
                run_mode=body.run_mode,
                target_node_uuid=body.target_node_uuid,
                priority=body.priority.value,
                input_value=body.input,
                description=body.description,
                meta_data=body.meta_data,
                inventory_bindings=[
                    binding.model_dump() for binding in body.inventory_bindings
                ],
            ),
            status=201,
        )

    @router.post("/station/workflow-invocations")
    def submit_station_workflow(
        body: StationWorkflowInvocationRequest,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """接收 Backend 工站调用，禁止提交 DAG 中间节点参数。

        参数：``body`` 只含工作流名称、入口参数和调用关联字段；
        ``authorization`` 必须匹配本工站协议密钥。返回首次或幂等重放得到的同一
        WorkflowTask。异常：鉴权失败返回 401；业务错误由统一 Workflow 处理器映射。
        """

        from unilabos.config.config import EdgeControlConfig

        station_api_key = str(EdgeControlConfig.api_key or "").strip()
        expected = f"Bearer {station_api_key}"
        if not station_api_key or not hmac.compare_digest(
            str(authorization or ""),
            expected,
        ):
            raise HTTPException(status_code=401, detail="工站调用凭据无效")
        _ensure_station_workflow_visible(body.workflow_id, body.workflow_name)
        return _success(
            service.submit_station_workflow(
                backend_task_uuid=body.task_uuid or body.backend_task_uuid or "",
                invocation_key=body.invocation_key,
                workflow_name=body.workflow_name,
                workflow_id=body.workflow_id,
                revision_fingerprint=body.revision_fingerprint,
                input_value=body.normalized_input or body.input or {},
                priority=body.priority,
                deadline=body.deadline,
                inventory_bindings=[
                    binding.model_dump() for binding in body.inventory_bindings
                ],
                description=body.description,
                meta_data=body.meta_data,
            ),
            status=201,
        )

    @router.get("/workflow-tasks/{task_uuid}/inventory-consumptions")
    def list_task_inventory_consumptions(task_uuid: str) -> JSONResponse:
        """读取任务级数量型库存消费事实。"""

        return _success(service.list_task_inventory_consumptions(task_uuid))

    @router.get("/workflow-tasks/{task_uuid}/material-events")
    def list_task_material_events(task_uuid: str) -> JSONResponse:
        """读取任务造成的物料变化历史（搬运、消耗、标为已使用）及对应步骤；只读。"""

        return _success(service.list_task_material_events(task_uuid))

    @router.get("/workflow-node-jobs/{job_uuid}/inventory-consumptions")
    def list_job_inventory_consumptions(job_uuid: str) -> JSONResponse:
        """读取作业级数量型库存消费事实。"""

        return _success(service.list_job_inventory_consumptions(job_uuid))

    @router.get("/reagents/{reagent_uuid}/inventory-consumptions")
    def list_reagent_inventory_consumptions(reagent_uuid: str) -> JSONResponse:
        """读取试剂库存实例的工作流消费谱系。"""

        return _success(service.list_reagent_inventory_consumptions(reagent_uuid))

    @router.post("/debug/workflow-tasks")
    def create_debug_workflow_task(
        body: Any = Body(default=None),
    ) -> JSONResponse:
        """旧 Debug Task 已由标准 WorkflowTask Step 控制取代。"""

        return JSONResponse(
            status_code=410,
            content={
                "code": 4100,
                "error": {
                    "code": "debug_api_retired",
                    "msg": "请使用 /api/v1/workflow-tasks 的 step 模式",
                },
            },
        )

    @router.get("/debug/workflow-tasks/{task_uuid}")
    def get_debug_workflow_task(task_uuid: str) -> JSONResponse:
        return JSONResponse(
            status_code=410,
            content={
                "code": 4100,
                "error": {
                    "code": "debug_api_retired",
                    "msg": "请使用标准 WorkflowTask 详情接口",
                },
            },
        )

    @router.post("/debug/workflow-tasks/{task_uuid}/commands")
    def command_debug_workflow_task(
        task_uuid: str,
        body: Any = Body(default=None),
    ) -> JSONResponse:
        return JSONResponse(
            status_code=410,
            content={
                "code": 4100,
                "error": {
                    "code": "debug_api_retired",
                    "msg": "请使用标准 WorkflowTask commands 接口",
                },
            },
        )

    @router.post("/device-action-runs")
    def create_device_action_run(
        body: DeviceActionRunCreateRequest,
    ) -> JSONResponse:
        """创建或幂等复用一次设备单动作运行（DeviceActionRun）。

        参数：``body`` 完全采用 Backend DTO。返回标准工作流任务（WorkflowTask）
        与唯一工作流节点作业（WorkflowNodeJob）；首次创建为 HTTP 201，复用为 200。
        """

        result = service.create_device_action_run(**body.model_dump())
        return _success(result, status=201 if result["created"] else 200)

    @router.get("/workflow-tasks")
    def list_workflow_tasks(
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        workflow_uuid: Optional[str] = Query(default=None),
        execution_kind: str = Query(default=""),
        status: str = Query(default=""),
        cleanup_status: str = Query(default=""),
        projection: Literal["full", "summary"] = Query(
            default="full", description="summary 仅返回表格状态与聚合进度，不返回任务冻结图。",
        ),
    ) -> JSONResponse:
        """按 Backend 筛选合同分页返回工作流任务（WorkflowTask）。

        参数包括分页、可选工作流 UUID、执行来源、业务状态和清理状态；返回标准
        分页 envelope，其中直接设备动作可用 ``ad_hoc_device_action`` 单独查询。
        """

        return _success(
            service.list_workflow_tasks(
                page=page,
                page_size=page_size,
                workflow_uuid=workflow_uuid,
                execution_kind=execution_kind,
                status=status,
                cleanup_status=cleanup_status,
                projection=projection,
            )
        )

    @router.get("/workflow-tasks/count")
    def count_workflow_tasks(
        workflow_uuid: Optional[str] = Query(default=None),
        execution_kind: str = Query(default=""),
        status: str = Query(default=""),
        cleanup_status: str = Query(default=""),
    ) -> JSONResponse:
        """只读取任务计数，避免为统计解码、传输一个完整任务。"""
        return _success(service.count_workflow_tasks(
            workflow_uuid=workflow_uuid, execution_kind=execution_kind,
            status=status, cleanup_status=cleanup_status,
        ))

    @router.get("/workflow-task-presentations")
    def list_workflow_task_presentations(
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        workflow_uuid: Optional[str] = Query(default=None),
        execution_kind: str = Query(default=""),
        status: str = Query(default=""),
        cleanup_status: str = Query(default=""),
        view: str = Query(default=""),
        terminal_limit: int = Query(default=20),
    ) -> JSONResponse:
        """返回 Edge 控制台任务矩阵所需的紧凑只读投影。

        这是明确的 Edge-only 展示接口，不改变共享 ``/workflow-tasks`` 合同；
        每个 Task 已批量嵌入紧凑 Job 状态，调用方不应再逐任务查询 Job。
        ``view=matrix`` 一次返回全部活动/需关注 Task 与最多 ``terminal_limit``
        个近期终态 Task，消除按状态拆分的轮询请求风暴。
        """

        return _success(
            service.list_workflow_task_presentations(
                page=page,
                page_size=page_size,
                workflow_uuid=workflow_uuid,
                execution_kind=execution_kind,
                status=status,
                cleanup_status=cleanup_status,
                view=view,
                terminal_limit=terminal_limit,
            )
        )

    @router.get("/workflow-tasks/{task_uuid}")
    def get_workflow_task(
        task_uuid: str,
        projection: Literal["full", "state"] = Query(
            default="full", description="state 只返回动态字段和 definition_version，按版本复用完整定义。",
        ),
    ) -> JSONResponse:
        return _success(service.get_workflow_task(task_uuid, projection=projection))

    @router.get(
        "/workflow-tasks/{task_uuid}/delete-eligibility",
        summary="查询工作流任务删除资格",
        description="按需核验单个任务是否可以软删除，并返回阻塞原因；任务列表接口只返回摘要，不承载资格字段。",
    )
    def get_workflow_task_delete_eligibility(task_uuid: str) -> JSONResponse:
        """返回单个任务的软删除资格与阻塞原因。

        控制台在打开删除确认框时调用；真正的删除仍由 DELETE 接口在同一事务内
        重新核验，本接口只用于交互提示，不是安全门禁。
        """

        return _success(service.get_workflow_task_delete_eligibility(task_uuid))

    @router.delete(
        "/workflow-tasks/{task_uuid}",
        summary="删除工作流任务",
        description="仅允许删除已进入终态且所有执行资源均已释放的 Edge 任务；删除采用软删除并保留运行历史。",
    )
    def delete_workflow_task(task_uuid: str) -> JSONResponse:
        """软删除已终态且全部执行资源已释放的 Edge 任务。

        删除资格由服务端在同一事务中重新核验；重复请求保持幂等，返回
        ``already_deleted``，不会删除工作流定义、Job、运行事件或库存历史。
        """

        return _success(service.delete_workflow_task(task_uuid))

    @router.get("/workflow-tasks/{task_uuid}/step-state")
    def get_workflow_task_step_state(task_uuid: str) -> JSONResponse:
        """返回 Task 详情页使用的权威 Step 候选。"""

        _require_debug_capability()
        return _success(service.get_workflow_task_step_state(task_uuid))

    @router.post("/workflow-tasks/{task_uuid}/commands")
    def command_workflow_task(
        task_uuid: str,
        body: WorkflowTaskCommandRequest,
    ) -> JSONResponse:
        """幂等提交一次工作流任务控制命令。"""

        if body.type in {"step", "switch_to_step"}:
            _require_debug_capability()
        return _success(
            service.command_workflow_task(
                task_uuid,
                command_type=body.type,
                target_node_uuid=body.target_node_uuid,
                idempotency_key=body.idempotency_key,
                description=body.description,
                meta_data=body.meta_data,
            ),
            status=201,
        )

    @router.get("/workflow-tasks/{task_uuid}/jobs")
    def list_workflow_node_jobs(task_uuid: str) -> JSONResponse:
        return _success(service.list_workflow_node_jobs(task_uuid))

    @router.patch(
        "/workflow-tasks/{task_uuid}/jobs/{job_uuid}/param",
        summary="修改暂停任务的未下发参数",
    )
    def patch_workflow_node_job_param(
        task_uuid: str,
        job_uuid: str,
        body: WorkflowTaskJobParamUpdateRequest,
    ) -> JSONResponse:
        """修改已暂停任务中尚未派发 Job 的参数。

        参数：``body.param`` 是递归参数补丁；仅允许当前任务的 pending Job。返回：
        更新后的 Task/Job 投影。异常：任务未暂停、作业已离开 pending 或身份不一致
        时返回冲突错误；不会修改工作流拓扑或已下发参数。
        """

        return _success(
            service.update_workflow_task_job_param(
                task_uuid,
                job_uuid,
                param=body.param,
            )
        )

    @router.get("/workflow-tasks/{task_uuid}/execution-locks")
    def list_workflow_task_execution_locks(task_uuid: str) -> JSONResponse:
        """返回任务当前活动执行锁及每个租约的人工释放资格。"""

        return _success(service.list_workflow_task_execution_locks(task_uuid))

    @router.post(
        "/workflow-tasks/{task_uuid}/execution-locks/{lease_uuid}/force-release"
    )
    def force_release_workflow_task_execution_lock(
        task_uuid: str,
        lease_uuid: str,
        body: WorkflowTaskExecutionLockReleaseRequest,
    ) -> JSONResponse:
        """在安全确认和 CAS 校验通过后释放目标作业的全部执行锁。"""

        return _success(
            service.force_release_workflow_task_execution_lock(
                task_uuid,
                lease_uuid,
                expected_claim_uuid=body.expected_claim_uuid,
                expected_fencing_token=body.expected_fencing_token,
                reason=body.reason,
                physical_settlement_confirmed=body.physical_settlement_confirmed,
            )
        )

    @router.get("/workflow-tasks/{task_uuid}/events")
    def list_workflow_task_runtime_events(
        task_uuid: str,
        after_sequence: str = Query(default=""),
        limit: str = Query(default=""),
    ) -> JSONResponse:
        """分页返回持久任务运行日志，包括动作下发与明确执行结果。"""

        try:
            after_text = after_sequence.strip(_GO_WHITE_SPACE)
            limit_text = limit.strip(_GO_WHITE_SPACE)
            parsed_after = (
                _parse_non_negative_int64_decimal(after_text) if after_text else 0
            )
            parsed_limit = (
                _parse_positive_decimal(limit_text, maximum=500) if limit_text else 100
            )
        except ValueError:
            raise WorkflowError("invalid_input") from None
        return _success(
            service.list_workflow_task_runtime_events(
                task_uuid,
                after_sequence=parsed_after,
                limit=parsed_limit,
            )
        )

    @router.get("/workflow-node-jobs/{job_uuid}")
    def get_workflow_node_job(job_uuid: str) -> JSONResponse:
        return _success(service.get_workflow_node_job(job_uuid))

    @router.get("/workflow-runtime/wait-graph")
    def get_execution_wait_graph() -> JSONResponse:
        """返回整站作业等待图与运行时循环诊断。"""

        return _success(service.get_execution_wait_graph())

    @router.get("/workflow-node-jobs/{job_uuid}/feedback")
    def list_workflow_node_job_feedback(
        job_uuid: str,
        page: int = Query(default=1),
        page_size: int = Query(default=20),
    ) -> JSONResponse:
        """返回作业已经提交的有序过程反馈。"""

        return _success(
            service.list_workflow_node_job_feedback(
                job_uuid,
                page=page,
                page_size=page_size,
            )
        )

    @router.post("/workflow-node-jobs/{job_uuid}/resolve-uncertain")
    def resolve_uncertain_workflow_node_job(
        job_uuid: str,
        body: UncertainJobResolutionRequest,
    ) -> JSONResponse:
        result = service.resolve_uncertain_job(
            job_uuid,
            resolution=body.resolution,
            reason=body.reason,
            device_command_id=body.device_command_id,
        )
        return _success(
            result,
            status=202 if result["pending_edge_confirmation"] else 200,
        )

    @router.post("/workflow-node-jobs/{job_uuid}/reconcile-quantity")
    def reconcile_job_quantity(
        job_uuid: str,
        body: QuantityReconciliationRequest,
    ) -> JSONResponse:
        """核对实际用量，完成待结算库存并重放已提交的设备结果。"""

        return _success(service.reconcile_job_quantity(
            job_uuid,
            consumptions=[item.model_dump(mode="json") for item in body.consumptions],
            reason=body.reason,
        ))

    @router.post("/workflow-node-jobs/{job_uuid}/settle-material-transfer")
    def settle_failed_material_transfer(
        job_uuid: str,
        body: FailedMaterialTransferSettlementRequest,
    ) -> JSONResponse:
        """提交失败转运的实际物料位置并完成物理结算。"""

        return _success(
            service.settle_failed_material_transfer(
                job_uuid,
                actual_change_set=body.actual_change_set,
                reason=body.reason,
            )
        )

    @router.post("/workflow-node-jobs/{job_uuid}/manual-confirmation")
    def decide_manual_confirmation(
        job_uuid: str,
        body: ManualConfirmationDecisionRequest,
    ) -> JSONResponse:
        try:
            return _success(
                service.decide_manual_confirmation(
                    job_uuid,
                    action=body.action,
                )
            )
        except WorkflowError as error:
            status = (
                400
                if error.code == "invalid_input"
                else (404 if error.code == "not_found" else 409)
            )
            return _BackendJSONResponse(
                status_code=status,
                content={
                    "code": _business_code(error.code),
                    "error": {"msg": error.message},
                },
            )

    @router.get("/workflow-tasks/{task_uuid}/human-interactions")
    def list_human_interactions(task_uuid: str) -> JSONResponse:
        return _success(service.list_human_interactions(task_uuid))

    @router.post("/workflow-human-interactions/{interaction_uuid}/reply")
    def reply_human_interaction(interaction_uuid: str, body: HumanInteractionReply) -> JSONResponse:
        try:
            return _success(service.reply_human_interaction(interaction_uuid, body.model_dump(exclude_none=True)))
        except WorkflowError as error:
            return _BackendJSONResponse(status_code=error.status, content={"code": _business_code(error.code), "error": {"msg": error.message}})

    @router.get("/workflow-interventions")
    def list_workflow_interventions(
        status: str = Query(default="open"),
        limit: int = Query(default=100),
    ) -> JSONResponse:
        """按状态查询等待处理或已经处理的工作流干预。"""

        return _success(service.list_workflow_interventions(status=status, limit=limit))

    @router.get("/workflow-interventions/{intervention_uuid}")
    def get_workflow_intervention(intervention_uuid: str) -> JSONResponse:
        return _success(service.get_workflow_intervention(intervention_uuid))

    @router.post("/workflow-interventions/{intervention_uuid}/decisions")
    def select_workflow_intervention(
        intervention_uuid: str,
        body: WorkflowInterventionDecisionRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> JSONResponse:
        result = service.select_workflow_intervention(
            intervention_uuid,
            revision=body.revision,
            option_id=body.option_id,
            idempotency_key=idempotency_key,
            result=body.result,
        )
        return _success(result, status=201 if result["created"] else 200)

    @router.get("/workflows/{workflow_uuid}/authoring")
    def get_authoring(workflow_uuid: str) -> JSONResponse:
        _visible_workflow(workflow_uuid)
        return _success(service.get_authoring(workflow_uuid))

    @router.put("/workflows/{workflow_uuid}/authoring/draft")
    def save_draft(
        workflow_uuid: str,
        body: DraftWriteRequest,
    ) -> JSONResponse:
        return _success(
            service.save_draft(
                workflow_uuid,
                python_source=body.python_source,
                expected_draft_hash=body.expected_draft_hash,
                expected_workflow_revision=body.expected_workflow_revision,
            )
        )

    @router.post("/workflows/{workflow_uuid}/authoring/apply")
    def apply_authoring(
        workflow_uuid: str,
        body: ApplyRequest,
    ) -> JSONResponse:
        """应用服务端持久候选并返回后端形状响应。

        参数：``workflow_uuid`` 是工作流（Workflow）身份；``body`` 只允许包含
        候选哈希（Candidate Hash）。返回：统一后端响应外层。异常：请求字段或
        领域前置条件错误由公共异常处理器转换成稳定业务错误。
        """

        return _success(
            service.apply_authoring(
                workflow_uuid,
                candidate_hash=body.candidate_hash,
            )
        )

    @router.get("/events")
    async def events(
        request: Request,
        last_event_id: Optional[str] = Header(
            default=None,
            alias="Last-Event-ID",
            description="不填时只接收新事件；填 0 回放保留的历史，其他编号从其之后继续发送。",
        ),
    ) -> Response:
        """从持久全局游标建立只作失效通知的 SSE 流。

        参数：``request`` 提供断开状态，``last_event_id`` 是规范请求头；为避免
        框架合并重复头，原始 ASGI 头仍由适配器唯一解析。返回：非法游标的稳定
        错误或事件流。缺省从最新水位订阅；显式零回放保留历史。游标过期或超前
        时发送 stream.reset，客户端须重读 REST。异常：持久读取/编码错误终止
        当前流；不从 MonitorBus 环形缓冲恢复，也不从 SSE 重建业务状态。
        """

        try:
            raw_cursor = next(
                (
                    value
                    for name, value in request.scope["headers"]
                    if name.lower() == b"last-event-id"
                ),
                None,
            )
            cursor_text = (
                raw_cursor.decode("utf-8")
                if raw_cursor is not None
                else (last_event_id or "")
            ).strip(_GO_WHITE_SPACE)
            cursor = (
                _parse_non_negative_int64_decimal(cursor_text)
                if cursor_text else None
            )
        except (UnicodeError, ValueError):
            cursor = -1
        if cursor == -1:
            return _error(WorkflowError("invalid_input"))
        fresh_connection = cursor is None
        if fresh_connection:
            # 必须在发送响应头之前固定水位，使客户端 open 后的 REST 补读覆盖
            # 首次快照与订阅之间的变化。显式游标零仍表示主动回放。
            cursor = service.latest_event_sequence()

        async def stream():
            """持续读取持久事件页并发送保活帧。

            参数：无，闭包持有请求与当前游标。返回：异步 SSE 文本迭代器。异常：
            存储或编码失败时终止连接，让客户端携带最后已收序号重连。
            """

            nonlocal cursor
            yield "retry: 3000\n: connected\n\n"
            if fresh_connection:
                # 只设置浏览器续传水位，不触发业务事件；空闲连接也能正确重连。
                yield f"id: {cursor}\n\n"
            replay_retained = bool(cursor_text) and cursor == 0
            while not await request.is_disconnected():
                page = service.read_event_stream(
                    after_sequence=cursor,
                    limit=100,
                    replay_retained=replay_retained,
                )
                replay_retained = False
                if page["reset_reason"]:
                    cursor = page["next_sequence"]
                    yield format_sse_event({
                        "id": cursor, "event": "stream.reset",
                        "data": {"reason": page["reset_reason"]},
                    })
                    await asyncio.sleep(0)
                    continue
                events_page = page["items"]
                for event in events_page:
                    cursor = event["id"]
                    yield format_sse_event(event)
                if not events_page:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0 if page["has_more"] else 1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def install_workflow_api(
    app: FastAPI,
    service: WorkflowService,
    *,
    template_snapshot_provider: Optional[TemplateSnapshotProvider] = None,
    authoring_transform: Any | None = None,
    wait_until_ready: Callable[[], Awaitable[bool]] | None = None,
    inventory_store_provider: Callable[[], Any | None] | None = None,
) -> None:
    """向 OS FastAPI 应用安装工作流及可选可信创作转换接口。

    参数说明：``app`` 是共享 HTTP 应用，``service`` 是工作流权威；本地调度模式
    传入 ``template_snapshot_provider`` 后，模板查询与 F02 编译器共享同一投影；
    ``authoring_transform`` 是同一目录代际的可信创作转换（Trusted Authoring
    Transform），缺失时不发布三条纯转换路由。延迟服务通过 ``wait_until_ready``
    通知后台任务何时可用，不阻塞路由挂载或 HTTP 启动。返回：无。
    """

    @app.exception_handler(WorkflowError)
    async def workflow_error_handler(
        _request: Request,
        error: WorkflowError,
    ) -> JSONResponse:
        """把工作流领域错误映射成统一业务 envelope。

        参数：``_request`` 是当前 HTTP 请求但不参与裁决；``error`` 携带稳定错误
        分类。返回与 Backend 一致的 HTTP 200 业务错误响应。
        """

        return _error(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        """把工作流相关 DTO 校验错误映射为 Backend 业务码 1000。

        参数：``request`` 用于识别合同路由；``error`` 是 FastAPI 校验详情。返回
        工作流合同的统一错误 envelope，其他路由继续使用框架默认响应。
        """

        workflow_prefixes = (
            "/api/v1/experiment-operation-categories",
            "/api/v1/local/workflows",
            "/api/v1/workflows",
            "/api/v1/workflow-tasks",
            "/api/v1/workflow-node-jobs",
            "/api/v1/workflow-node-templates",
            "/api/v1/device-action-runs",
            "/api/v1/events",
            "/api/v1/authoring",
        )
        if request.url.path.endswith("/manual-confirmation"):
            error = WorkflowError("invalid_input")
            return _BackendJSONResponse(
                status_code=400,
                content={
                    "code": _business_code(error.code),
                    "error": {"msg": error.message},
                },
            )
        if request.url.path in {
            "/api/v1/workflows/import",
            "/api/v1/local/workflows/import-python",
        }:
            return _error(_import_request_error(request.url.path, error))
        if any(
            request.url.path == prefix or request.url.path.startswith(f"{prefix}/")
            for prefix in workflow_prefixes
        ):
            return _error(WorkflowError("invalid_input"))
        return await request_validation_exception_handler(request, error)

    app.include_router(create_workflow_router(
        service, wait_until_ready=wait_until_ready,
        inventory_store_provider=inventory_store_provider,
    ))
    # 类别 CRUD 是独立深模块；延迟导入避免其复用公共响应适配器时形成模块环。
    from unilabos.app.operation_category_api import (
        create_operation_category_router,
    )

    app.include_router(create_operation_category_router(service))
    if template_snapshot_provider is not None:
        app.include_router(
            create_workflow_template_router(
                WorkflowTemplateQueryService(template_snapshot_provider)
            )
        )
    if authoring_transform is not None:
        from unilabos.app.workflow_authoring_transform import (
            create_authoring_transform_router,
        )

        app.include_router(create_authoring_transform_router(authoring_transform))


def create_workflow_app(
    service: WorkflowService,
    *,
    template_snapshot_provider: Optional[TemplateSnapshotProvider] = None,
    authoring_transform: Any | None = None,
    inventory_store_provider: Callable[[], Any | None] | None = None,
) -> FastAPI:
    """创建工作流合同测试应用。

    参数说明：``service`` 是唯一工作流权威；可选模板快照提供者用于本地完整应用
    合同测试；``authoring_transform`` 显式安装纯转换接缝。返回已安装统一错误映射
    的 FastAPI 应用。
    """

    app = FastAPI(title="Uni-Lab Workflow", version="0.1.0")
    install_workflow_api(
        app,
        service,
        template_snapshot_provider=template_snapshot_provider,
        authoring_transform=authoring_transform,
        inventory_store_provider=inventory_store_provider,
    )
    return app


# 以下别名是可信创作转换（Trusted Authoring Transform）适配器复用的公共 HTTP
# 接缝；保留旧私有名称，避免扩大现有工作流路由的机械修改范围。
BackendJSONRoute = _BackendJSONRoute
BackendJSONResponse = _BackendJSONResponse
workflow_success_response = _success
workflow_error_response = _error


__all__ = [
    "BackendJSONResponse",
    "BackendJSONRoute",
    "create_workflow_app",
    "create_workflow_router",
    "format_sse_event",
    "install_workflow_api",
    "workflow_error_response",
    "workflow_success_response",
]
