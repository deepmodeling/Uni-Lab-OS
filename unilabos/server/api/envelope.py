"""微后端 HTTP API 的统一响应信封与业务码（V1.0 合同）。

所有 ``/api/v1`` JSON 路由都用同一个形状：

- 成功：HTTP 200（创建类可 201 / 202），``{"code": 0, "data": <payload>}``；无数据的命令省略 ``data``。
- 失败：HTTP 200，``{"code": <业务码>, "error": {"msg": "<中文说明>", "code"?: "<符号码>", "details"?: {...}}}``。

机制是 :class:`EnvelopeRoute`（``APIRoute`` 子类），路由器用 :func:`envelope_router` 创建即可：

- 路由函数直接返回 DTO / dict / list / ``None``，由路由类包成 ``{"code": 0, "data": ...}``，
  OpenAPI 的响应模型同步改为 ``Envelope[原模型]``；
- 路由函数抛出 :class:`ApiError`、``HTTPException``、请求校验失败或已登记的领域异常时，
  统一转成失败信封（HTTP 状态码 → 业务码的换算按路由器的 :class:`ErrorProfile`）；
- SSE / 文件下载等非 JSON 响应、生成器流式端点和 :func:`raw_response` 标记的端点原样返回。

业务码表以 V1 为准（``unilabos/app/scheduler/inventory/backend_contract.py`` 与
``workflow_api._business_code``），dev 原来的 ``unauthorized = 4001`` 改为 ``1002``，以免与 V1
的 4001（资源不存在）冲突。dev 领域异常到业务码的映射集中在本模块 :data:`DOMAIN_ERRORS`，
路由里不写业务码数字。约定见 ``docs/developer_guide/api_v1_migration.md``。
"""

from __future__ import annotations

import functools
import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Generic, Iterable, Mapping, Optional, TypeVar

from fastapi import APIRouter
from fastapi.datastructures import Default, DefaultPlaceholder
from fastapi.dependencies.utils import get_typed_return_annotation
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from unilabos.protocol.utils.json_codec import encode_json
from unilabos.utils.log import error as log_error

# ── 业务码表（V1 为准） ─────────────────────────────────────────────

OK = 0
INTERNAL_ERROR = 1
INVALID_PARAMETER = 1000
OPERATION_NOT_ALLOWED = 1001
UNAUTHORIZED = 1002
DATABASE_CONFLICT = 2005
WORKFLOW_NOT_FOUND = 3002
STATE_CONFLICT = 3003
REAGENT_INFO_IN_USE = 4000
RESOURCE_NOT_FOUND = 4001
RESOURCE_DATA_CONFLICT = 4002
RESOURCE_TEMPLATE_NOT_FOUND = 5000
SERVICE_UNAVAILABLE = 5001
TEMPLATE_DEFINITION_INVALID = 5003
TEMPLATE_DATA_CONFLICT = 5004
MATERIAL_NOT_FOUND = 6000
MATERIAL_TEMPLATE_NOT_FOUND = 6001
MATERIAL_TEMPLATE_IMMUTABLE = 6002
MATERIAL_PARENT_NOT_FOUND = 6003
MATERIAL_PARENT_CYCLE = 6004
MATERIAL_SITE_NOT_FOUND = 6006
MATERIAL_SITE_OCCUPIED = 6007
MATERIAL_SITE_TEMPLATE_NOT_ALLOWED = 6008
MATERIAL_SITE_CYCLE = 6009
MATERIAL_IDENTITY_CONFLICT = 6010
MATERIAL_ACTIVE_CLAIM_CONFLICT = 6011

#: 业务码 → 默认中文说明（异常没有给出具体文案时使用，也是文档与契约测试的码表来源）。
CODE_MESSAGES: Dict[int, str] = {
    OK: "成功",
    INTERNAL_ERROR: "服务内部错误",
    INVALID_PARAMETER: "提交内容格式不正确",
    OPERATION_NOT_ALLOWED: "当前模式不允许该操作",
    UNAUTHORIZED: "写操作需要有效的控制面 token",
    DATABASE_CONFLICT: "数据库冲突",
    WORKFLOW_NOT_FOUND: "工作流或任务不存在",
    STATE_CONFLICT: "状态冲突，请刷新后重试",
    REAGENT_INFO_IN_USE: "试剂信息仍被引用",
    RESOURCE_NOT_FOUND: "资源不存在",
    RESOURCE_DATA_CONFLICT: "资源数据冲突",
    RESOURCE_TEMPLATE_NOT_FOUND: "资源模板不存在",
    SERVICE_UNAVAILABLE: "依赖暂不可用或当前部署不支持",
    TEMPLATE_DEFINITION_INVALID: "模板定义不合法",
    TEMPLATE_DATA_CONFLICT: "模板数据冲突",
    MATERIAL_NOT_FOUND: "物料不存在",
    MATERIAL_TEMPLATE_NOT_FOUND: "物料模板不存在",
    MATERIAL_TEMPLATE_IMMUTABLE: "物料模板不可修改",
    MATERIAL_PARENT_NOT_FOUND: "父级物料不存在",
    MATERIAL_PARENT_CYCLE: "物料父子关系成环",
    MATERIAL_SITE_NOT_FOUND: "库位不存在",
    MATERIAL_SITE_OCCUPIED: "库位已被占用",
    MATERIAL_SITE_TEMPLATE_NOT_ALLOWED: "库位不允许该模板",
    MATERIAL_SITE_CYCLE: "库位关系成环",
    MATERIAL_IDENTITY_CONFLICT: "物料身份冲突",
    MATERIAL_ACTIVE_CLAIM_CONFLICT: "物料仍被未结算的动作占用",
}

#: 旧的类别名 → 业务码（graphs / chemicals / workflow 早期按类别取码，保留给外部调用方）。
BUSINESS_CODES: Dict[str, int] = {
    "ok": OK,
    "internal": INTERNAL_ERROR,
    "invalid": INVALID_PARAMETER,
    "unauthorized": UNAUTHORIZED,
    "not_found": WORKFLOW_NOT_FOUND,
    "conflict": STATE_CONFLICT,
    "unavailable": SERVICE_UNAVAILABLE,
}


def business_code(category: str) -> int:
    return BUSINESS_CODES.get(category, INTERNAL_ERROR)


# ── 失败信封 ─────────────────────────────────────────────────────


class EnvelopeResponse(JSONResponse):
    """已经是信封形状的 JSON 响应；路由类见到它不再二次包装。

    用有界非递归编码器渲染，深层嵌套的工作流图不会触发解释器递归上限；遇到编码器
    不认识的类型（枚举、日期等）退回 ``jsonable_encoder``。
    """

    def render(self, content: Any) -> bytes:
        try:
            return encode_json(content)
        except (TypeError, ValueError):
            return json.dumps(
                jsonable_encoder(content), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")


def ok_body(data: Any = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"code": OK}
    if data is not None:
        body["data"] = data
    return body


def error_body(
    code: int,
    message: str = "",
    *,
    symbol: Optional[str] = None,
    details: Any = None,
) -> Dict[str, Any]:
    error: Dict[str, Any] = {"msg": str(message or CODE_MESSAGES.get(code, CODE_MESSAGES[INTERNAL_ERROR]))}
    if symbol:
        error["code"] = symbol
    if details:
        error["details"] = details
    return {"code": int(code), "error": error}


def ok_response(data: Any = None, *, status_code: int = 200) -> EnvelopeResponse:
    """成功信封；``data`` 为 ``None`` 时不输出 ``data`` 字段（V1 无数据命令的形状）。"""

    return EnvelopeResponse(status_code=status_code, content=ok_body(data))


def error_response(
    code: int,
    message: str = "",
    *,
    symbol: Optional[str] = None,
    details: Any = None,
    http_status: int = 200,
    headers: Optional[Mapping[str, str]] = None,
) -> EnvelopeResponse:
    """失败信封：业务失败固定 HTTP 200；只有控制面 token（401）等中间件级拒绝带别的状态码。"""

    return EnvelopeResponse(
        status_code=http_status,
        content=error_body(code, message, symbol=symbol, details=details),
        headers=dict(headers) if headers else None,
    )


class ApiError(Exception):
    """携带业务码的接口错误；路由抛出后由 :class:`EnvelopeRoute` 转成失败信封。

    ``symbol`` 对应 ``error.code``（只在 V1 同路由也输出符号码时使用，如
    ``workflow_identity_mismatch``）；``details`` 对应 ``error.details``。
    """

    def __init__(
        self,
        code: int,
        message: str = "",
        *,
        symbol: Optional[str] = None,
        details: Any = None,
        http_status: int = 200,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.code = int(code)
        self.message = str(message or CODE_MESSAGES.get(self.code, CODE_MESSAGES[INTERNAL_ERROR]))
        self.symbol = symbol
        self.details = details
        self.http_status = http_status
        self.headers = dict(headers) if headers else None
        super().__init__(self.message)

    def to_body(self) -> Dict[str, Any]:
        return error_body(self.code, self.message, symbol=self.symbol, details=self.details)

    def to_response(self) -> EnvelopeResponse:
        return error_response(
            self.code,
            self.message,
            symbol=self.symbol,
            details=self.details,
            http_status=self.http_status,
            headers=self.headers,
        )


# ── HTTP 状态码 → 业务码 ─────────────────────────────────────────


@dataclass(frozen=True)
class ErrorProfile:
    """路由器的错误口径：同一个 HTTP 404 / 409 在不同领域对应不同业务码。"""

    name: str
    not_found: int
    conflict: int


#: 工作流 / 任务 / 节点作业：404 → 3002，409 → 3003。
WORKFLOW = ErrorProfile("workflow", WORKFLOW_NOT_FOUND, STATE_CONFLICT)
#: 物料 / 化学品 / 设备图等资源：404 → 4001，409 → 4002。
RESOURCE = ErrorProfile("resource", RESOURCE_NOT_FOUND, RESOURCE_DATA_CONFLICT)
#: 其余领域（runtime / telemetry / history / 驱动包 / 诊断 …）：404 → 4001，409 → 3003。
GENERIC = ErrorProfile("generic", RESOURCE_NOT_FOUND, STATE_CONFLICT)


def code_for_status(status: int, profile: ErrorProfile = GENERIC) -> int:
    """``HTTPException`` 的状态码换算业务码；表外状态一律 1（未归类）。"""

    if status in (400, 422):
        return INVALID_PARAMETER
    if status == 401:
        return UNAUTHORIZED
    if status == 403:
        return OPERATION_NOT_ALLOWED
    if status == 404:
        return profile.not_found
    if status == 409:
        return profile.conflict
    if status == 503:
        return SERVICE_UNAVAILABLE
    return INTERNAL_ERROR


def validation_detail(error: RequestValidationError) -> str:
    # 只回字段路径与校验信息，不把完整输入（可能含驱动凭据）或堆栈回显。
    return "; ".join(
        f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors()[:5]
    )


def _detail_message(detail: Any) -> tuple[str, Any]:
    if isinstance(detail, str):
        return detail, None
    if isinstance(detail, Mapping):
        message = detail.get("msg") or detail.get("message") or detail.get("detail") or ""
        return str(message), dict(detail)
    return "", jsonable_encoder(detail)


# ── dev 领域异常 → 业务码（集中登记，按类的完整路径沿 MRO 匹配，不导入服务模块） ──

# V1 ``workflow_api._business_code`` 的分类，加上 dev 独有的错误码。
_WORKFLOW_INVALID = frozenset({
    "invalid_input",
    "template_binding_invalid",
    "site_binding_invalid",
    "invalid_composite_child_type",
    "invalid_control_region",
    "material_shortage_unhandled",
    "graph_not_executable",
    "invalid_material_source",
    "material_flow_fan_out",
    "material_template_mismatch",
})
_WORKFLOW_NOT_FOUND = frozenset({"not_found", "workflow_not_found", "workflow_template_not_found"})
_WORKFLOW_CONFLICT = frozenset({
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
    "invalid_composite_child_status",
    "debug_task_conflict",
    "preflight_failed",
    "manual_confirmation_not_assignee",
    "manual_confirmation_decided",
    "manual_confirmation_key_used",
})
_WORKFLOW_NOT_ALLOWED = frozenset({
    "read_only_mode",
    "debug_not_allowed",
    "device_action_not_allowed",
    "local_task_authority_forbidden",
})
_WORKFLOW_UNAVAILABLE = frozenset({"template_catalog_unavailable", "workflow_template_unavailable"})
#: V1 同路由也输出 ``error.code`` 的符号码（前端据此区分身份拒绝与普通 3003 CAS 冲突）。
_WORKFLOW_SYMBOLS = frozenset({
    "workflow_identity_mismatch",
    "debug_not_allowed",
    "debug_task_conflict",
    "device_action_not_allowed",
    "preflight_failed",
})


def workflow_business_code(error_code: str) -> int:
    """工作流领域错误分类 → 业务码（V1 ``_business_code`` 同口径；未知分类为 1）。"""

    if error_code in _WORKFLOW_INVALID:
        return INVALID_PARAMETER
    if error_code in _WORKFLOW_NOT_FOUND:
        return WORKFLOW_NOT_FOUND
    if error_code in _WORKFLOW_CONFLICT:
        return STATE_CONFLICT
    if error_code in _WORKFLOW_NOT_ALLOWED:
        return OPERATION_NOT_ALLOWED
    if error_code in _WORKFLOW_UNAVAILABLE:
        return SERVICE_UNAVAILABLE
    return INTERNAL_ERROR


def _workflow_error(exc: Any) -> ApiError:
    code = str(getattr(exc, "code", "") or "")
    return ApiError(
        workflow_business_code(code),
        getattr(exc, "message", "") or str(exc),
        symbol=code if code in _WORKFLOW_SYMBOLS else None,
        details=getattr(exc, "details", None),
    )


def _coded(table: Mapping[str, int], default: int = INTERNAL_ERROR) -> Callable[[Any], ApiError]:
    """``exc.code``（符号分类）查表的领域异常，如 GraphError / ChemicalError。"""

    def mapper(exc: Any) -> ApiError:
        return ApiError(
            table.get(str(getattr(exc, "code", "")), default),
            getattr(exc, "message", "") or str(exc),
        )

    return mapper


def _fixed(code: int) -> Callable[[Any], ApiError]:
    def mapper(exc: Any) -> ApiError:
        return ApiError(code, str(exc))

    return mapper


#: 物料「不存在」按消息前缀判断对象（服务层统一写成 ``<对象> not found: <uuid>``）；
#: 异常带 ``subject`` 属性时以它为准，物料域迁移时可逐处补上。
_MATERIAL_NOT_FOUND_SUBJECTS = (
    ("material", MATERIAL_NOT_FOUND),
    ("site", MATERIAL_SITE_NOT_FOUND),
    ("template", MATERIAL_TEMPLATE_NOT_FOUND),
    ("parent", MATERIAL_PARENT_NOT_FOUND),
)
#: 已知含义的物料冲突文案片段 → V1 6000 段业务码；其余冲突为 4002。
_MATERIAL_CONFLICT_MARKERS = (
    ("occupied", MATERIAL_SITE_OCCUPIED),
    ("已被占用", MATERIAL_SITE_OCCUPIED),
    ("未结算", MATERIAL_ACTIVE_CLAIM_CONFLICT),
    ("material uuid already exists", MATERIAL_IDENTITY_CONFLICT),
)


def _material_not_found(exc: Any) -> ApiError:
    message = str(exc)
    subject = str(getattr(exc, "subject", "") or "").lower()
    lowered = message.lower()
    for name, code in _MATERIAL_NOT_FOUND_SUBJECTS:
        if subject == name or (not subject and lowered.startswith(name)):
            return ApiError(code, message)
    return ApiError(RESOURCE_NOT_FOUND, message)


def _material_conflict(exc: Any) -> ApiError:
    message = str(exc)
    explicit = getattr(exc, "business_code", None)
    if isinstance(explicit, int):
        return ApiError(explicit, message)
    lowered = message.lower()
    for marker, code in _MATERIAL_CONFLICT_MARKERS:
        if marker in lowered:
            return ApiError(code, message)
    return ApiError(RESOURCE_DATA_CONFLICT, message)


def _message_not_found(fallback: int) -> Callable[[Any], ApiError]:
    """驱动包 / 受管进程：服务层只有一个异常类，「not found」靠文案区分。"""

    def mapper(exc: Any) -> ApiError:
        message = str(exc)
        return ApiError(RESOURCE_NOT_FOUND if "not found" in message else fallback, message)

    return mapper


_SERVICES = "unilabos.server.services"
_BACKEND = "unilabos.server.backend"

#: 异常类完整路径 → 映射函数。沿异常 MRO 取第一个命中的条目，子类继承父类口径。
DOMAIN_ERRORS: Dict[str, Callable[[Any], ApiError]] = {
    # 工作流
    f"{_SERVICES}.runtime.workflow.service.WorkflowError": _workflow_error,
    f"{_SERVICES}.runtime.workflow.errors.StoreNotFound": _fixed(WORKFLOW_NOT_FOUND),
    f"{_SERVICES}.runtime.workflow.errors.StoreConflict": _fixed(STATE_CONFLICT),
    # materials.db：物料 / 化学品 / 设备图
    f"{_SERVICES}.materials.core.MaterialNotFoundError": _material_not_found,
    f"{_SERVICES}.materials.core.MaterialConflictError": _material_conflict,
    f"{_SERVICES}.materials.core.MaterialNoChangeError": _fixed(RESOURCE_DATA_CONFLICT),
    f"{_SERVICES}.materials.core.InsufficientInventoryError": _fixed(RESOURCE_DATA_CONFLICT),
    f"{_SERVICES}.materials.core.RejectedMutationError": _fixed(RESOURCE_DATA_CONFLICT),
    f"{_SERVICES}.materials.core.MaterialValidationError": _fixed(INVALID_PARAMETER),
    f"{_SERVICES}.materials.core.MaterialTransferSyncError": _fixed(SERVICE_UNAVAILABLE),
    f"{_SERVICES}.materials.core.MaterialsServiceError": _fixed(INTERNAL_ERROR),
    f"{_SERVICES}.materials.chemicals.ChemicalError": _coded({
        "invalid_input": INVALID_PARAMETER,
        "not_found": RESOURCE_NOT_FOUND,
        "conflict": RESOURCE_DATA_CONFLICT,
        "in_use": REAGENT_INFO_IN_USE,
    }),
    f"{_SERVICES}.materials.graph.GraphError": _coded({
        "not_found": RESOURCE_NOT_FOUND,
        "invalid_input": INVALID_PARAMETER,
        "invalid_payload": INVALID_PARAMETER,
        "identity_conflict": RESOURCE_DATA_CONFLICT,
        "unsupported": SERVICE_UNAVAILABLE,
    }),
    # runtime.db / telemetry.db / history.db
    f"{_SERVICES}.runtime.data.RuntimeNotFoundError": _fixed(RESOURCE_NOT_FOUND),
    f"{_SERVICES}.runtime.data.RuntimeConflictError": _fixed(STATE_CONFLICT),
    f"{_SERVICES}.runtime.data.RuntimeValidationError": _fixed(INVALID_PARAMETER),
    f"{_SERVICES}.runtime.lab.LabLayoutConflict": _fixed(STATE_CONFLICT),
    f"{_SERVICES}.runtime.logs.LogSourceNotFound": _fixed(RESOURCE_NOT_FOUND),
    f"{_SERVICES}.runtime.devices.DeviceNotFound": _fixed(RESOURCE_NOT_FOUND),
    f"{_SERVICES}.runtime.logs.LogSourceUnavailable": _fixed(SERVICE_UNAVAILABLE),
    f"{_SERVICES}.telemetry.TelemetryConflictError": _fixed(STATE_CONFLICT),
    f"{_SERVICES}.telemetry.TelemetryValidationError": _fixed(INVALID_PARAMETER),
    f"{_SERVICES}.history.HistoryNotFoundError": _fixed(RESOURCE_NOT_FOUND),
    f"{_SERVICES}.history.HistoryConflictError": _fixed(STATE_CONFLICT),
    f"{_SERVICES}.history.HistoryValidationError": _fixed(INVALID_PARAMETER),
    # 动作异常决策（/error-decisions）与工站异常处置（/stations/*）
    f"{_SERVICES}.runtime.error_decisions.ErrorDecisionConflict": _fixed(STATE_CONFLICT),
    f"{_SERVICES}.runtime.error_decisions.ErrorDecisionUnavailable": _fixed(SERVICE_UNAVAILABLE),
    f"{_SERVICES}.runtime.station_handling.StationNotFound": _fixed(RESOURCE_NOT_FOUND),
    f"{_SERVICES}.runtime.station_handling.StationConflict": _fixed(STATE_CONFLICT),
    f"{_SERVICES}.runtime.station_handling.StationInvalid": _fixed(INVALID_PARAMETER),
    f"{_SERVICES}.runtime.station_handling.StationUnavailable": _fixed(SERVICE_UNAVAILABLE),
    # Host 驱动包 / 受管设备进程
    f"{_SERVICES}.driver_packages.DriverPackageBusyError": _fixed(STATE_CONFLICT),
    f"{_SERVICES}.driver_packages.DriverPackageError": _message_not_found(INVALID_PARAMETER),
    f"{_SERVICES}.device_processes.DeviceProcessBusyError": _fixed(STATE_CONFLICT),
    f"{_SERVICES}.device_processes.DeviceProcessError": _message_not_found(INVALID_PARAMETER),
    # 调度权威生命周期
    f"{_BACKEND}.lifecycle.SchedulerUnavailable": _fixed(SERVICE_UNAVAILABLE),
    f"{_BACKEND}.reset.ResetConflict": _fixed(STATE_CONFLICT),
}


def register_domain_error(exc_type: type[BaseException], mapper: Callable[[Any], ApiError]) -> None:
    """登记一个领域异常的业务码映射（领域迁移新增异常时用；优先写进 :data:`DOMAIN_ERRORS`）。"""

    DOMAIN_ERRORS[f"{exc_type.__module__}.{exc_type.__qualname__}"] = mapper


def to_api_error(exc: BaseException, profile: ErrorProfile = GENERIC) -> Optional[ApiError]:
    """把路由里抛出的异常换算成 :class:`ApiError`；不认识的异常返回 ``None``（继续上抛）。"""

    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, RequestValidationError):
        return ApiError(INVALID_PARAMETER, f"提交内容格式不正确：{validation_detail(exc)}")
    if isinstance(exc, StarletteHTTPException):
        message, details = _detail_message(exc.detail)
        code = code_for_status(exc.status_code, profile)
        return ApiError(
            code,
            message,
            details=details,
            http_status=401 if code == UNAUTHORIZED else 200,
            headers=getattr(exc, "headers", None),
        )
    for cls in type(exc).__mro__:
        mapper = DOMAIN_ERRORS.get(f"{cls.__module__}.{cls.__qualname__}")
        if mapper is not None:
            return mapper(exc)
    return None


# ── 信封路由 ─────────────────────────────────────────────────────

DataT = TypeVar("DataT")


class Envelope(BaseModel, Generic[DataT]):
    """成功信封的 OpenAPI 形状；失败时同一路由返回 ``{"code", "error": {"msg"}}``。"""

    code: int = OK
    data: DataT


_ENVELOPED = "__unilab_envelope__"
_RAW = "raw"


def raw_response(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    """标记端点不走信封：健康 / 就绪探针、文件下载、Host↔权威内部通道等。

    被标记的端点保持 FastAPI 默认行为（返回值、HTTPException、422 都不改写）。
    """

    setattr(endpoint, _ENVELOPED, _RAW)
    return endpoint


def is_raw_endpoint(endpoint: Callable[..., Any]) -> bool:
    return getattr(endpoint, _ENVELOPED, None) == _RAW


def _is_json_response(response: Response) -> bool:
    media_type = str(response.media_type or response.headers.get("content-type", "")).lower()
    return isinstance(response, JSONResponse) or "json" in media_type


def _rewrap_response(response: Response, profile: ErrorProfile) -> Response:
    """路由直接返回的 Response：信封响应原样；JSON 包成信封；空 204 换成 ``{"code": 0}``；其余原样。"""

    if isinstance(response, EnvelopeResponse):
        return response
    if response.status_code == 204 and not getattr(response, "body", b""):
        return ok_response()
    if not _is_json_response(response):
        return response
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    if response.status_code < 400:
        body = bytes(response.body or b"null")
        return Response(
            content=b'{"code":0,"data":' + body + b"}",
            status_code=response.status_code,
            headers=headers,
            media_type="application/json",
            background=response.background,
        )
    try:
        payload = json.loads(bytes(response.body or b"null"))
    except ValueError:
        payload = None
    message, details = _detail_message(
        payload.get("detail", payload) if isinstance(payload, Mapping) else payload
    )
    code = code_for_status(response.status_code, profile)
    return error_response(
        code,
        message,
        details=details,
        http_status=401 if code == UNAUTHORIZED else 200,
        headers=headers,
    )


def _wrap_result(result: Any, profile: ErrorProfile) -> Any:
    if isinstance(result, Response):
        return _rewrap_response(result, profile)
    if result is None:
        return ok_response()
    return {"code": OK, "data": result}


def _wrap_endpoint(endpoint: Callable[..., Any], profile: ErrorProfile) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(endpoint):

        @functools.wraps(endpoint)
        async def async_enveloped(*args: Any, **kwargs: Any) -> Any:
            return _wrap_result(await endpoint(*args, **kwargs), profile)

        wrapper: Callable[..., Any] = async_enveloped
    else:

        @functools.wraps(endpoint)
        def sync_enveloped(*args: Any, **kwargs: Any) -> Any:
            return _wrap_result(endpoint(*args, **kwargs), profile)

        wrapper = sync_enveloped
    setattr(wrapper, _ENVELOPED, True)
    return wrapper


def _wants_raw(endpoint: Callable[..., Any], response_class: Any) -> bool:
    if is_raw_endpoint(endpoint):
        return True
    # 生成器端点由 FastAPI 按 JSON Lines / SSE 流式输出，不是单个 JSON 文档。
    if inspect.isgeneratorfunction(endpoint) or inspect.isasyncgenfunction(endpoint):
        return True
    # 显式声明了非 JSON 响应类（纯文本、HTML、文件）的端点；声明为基类 Response
    # 只是「无响应体」的意思，仍按信封处理。
    if not isinstance(response_class, DefaultPlaceholder) and response_class is not Response:
        return not issubclass(response_class, JSONResponse)
    return False


class EnvelopeRoute(APIRoute):
    """``/api/v1`` JSON 路由的统一信封：成功包 ``data``，失败换算业务码。

    子类只改 :attr:`error_profile`；用 :func:`envelope_router` 创建路由器即可，
    不需要直接引用本类。
    """

    error_profile: ErrorProfile = GENERIC

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        response_class = kwargs.get("response_class", Default(JSONResponse))
        if getattr(endpoint, _ENVELOPED, None) is True or _wants_raw(endpoint, response_class):
            # 已包装（同一端点被再次登记）或不走信封：保持调用方给的参数
            super().__init__(path, endpoint, **kwargs)
            return
        response_model = kwargs.get("response_model", Default(None))
        if isinstance(response_model, DefaultPlaceholder):
            annotation = get_typed_return_annotation(endpoint)
            response_model = (
                None
                if annotation is None or (inspect.isclass(annotation) and issubclass(annotation, Response))
                else annotation
            )
        kwargs["response_model"] = Envelope[response_model] if response_model is not None else None
        kwargs["response_class"] = Default(JSONResponse)
        if kwargs.get("status_code") == 204:
            # 信封总有响应体：「无内容」命令改为 HTTP 200 + {"code": 0}
            kwargs["status_code"] = None
        super().__init__(path, _wrap_endpoint(endpoint, self.error_profile), **kwargs)

    def get_route_handler(self) -> Callable[[Request], Any]:
        handler = super().get_route_handler()
        if is_raw_endpoint(self.endpoint) or getattr(self.endpoint, _ENVELOPED, None) is not True:
            return handler
        profile = self.error_profile

        async def envelope_route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except Exception as exc:  # noqa: BLE001 - 只换算认识的异常，其余原样上抛
                api_error = to_api_error(exc, profile)
                if api_error is None:
                    raise
                if api_error.code == INTERNAL_ERROR and not isinstance(exc, (ApiError, StarletteHTTPException)):
                    log_error(f"[Envelope] {request.method} {request.url.path} 未归类的领域错误: {exc!r}")
                return api_error.to_response()

        return envelope_route_handler


_ROUTE_CLASSES: Dict[str, type[EnvelopeRoute]] = {GENERIC.name: EnvelopeRoute}


def envelope_route_class(profile: ErrorProfile = GENERIC) -> type[EnvelopeRoute]:
    """按错误口径取（并缓存）信封路由类；需要再叠加路由行为时继承它。"""

    route_class = _ROUTE_CLASSES.get(profile.name)
    if route_class is None:
        route_class = type(
            f"{profile.name.title()}EnvelopeRoute", (EnvelopeRoute,), {"error_profile": profile}
        )
        _ROUTE_CLASSES[profile.name] = route_class
    return route_class


def envelope_router(*, profile: ErrorProfile = GENERIC, **kwargs: Any) -> APIRouter:
    """创建走统一信封的 ``APIRouter``（参数同 ``APIRouter``）。"""

    kwargs.setdefault("route_class", envelope_route_class(profile))
    return APIRouter(**kwargs)


def iter_api_routes(routes: Iterable[Any]) -> Iterable[tuple[str, Any]]:
    """展开（含 include_router 进来的）路由，产出 ``(完整路径, 原始路由对象)``。

    给契约测试与诊断用：检查每个 ``/api/v1`` JSON 路由都走信封。
    """

    from fastapi.routing import iter_route_contexts

    for context in iter_route_contexts(list(routes)):
        yield str(context.path or ""), context.original_route


__all__ = [
    "ApiError",
    "BUSINESS_CODES",
    "CODE_MESSAGES",
    "DOMAIN_ERRORS",
    "Envelope",
    "EnvelopeResponse",
    "EnvelopeRoute",
    "ErrorProfile",
    "GENERIC",
    "RESOURCE",
    "WORKFLOW",
    "business_code",
    "code_for_status",
    "envelope_route_class",
    "envelope_router",
    "error_body",
    "error_response",
    "is_raw_endpoint",
    "iter_api_routes",
    "ok_body",
    "ok_response",
    "raw_response",
    "register_domain_error",
    "to_api_error",
    "validation_detail",
    "workflow_business_code",
    # 业务码常量
    "OK",
    "INTERNAL_ERROR",
    "INVALID_PARAMETER",
    "OPERATION_NOT_ALLOWED",
    "UNAUTHORIZED",
    "DATABASE_CONFLICT",
    "WORKFLOW_NOT_FOUND",
    "STATE_CONFLICT",
    "REAGENT_INFO_IN_USE",
    "RESOURCE_NOT_FOUND",
    "RESOURCE_DATA_CONFLICT",
    "RESOURCE_TEMPLATE_NOT_FOUND",
    "SERVICE_UNAVAILABLE",
    "TEMPLATE_DEFINITION_INVALID",
    "TEMPLATE_DATA_CONFLICT",
    "MATERIAL_NOT_FOUND",
    "MATERIAL_TEMPLATE_NOT_FOUND",
    "MATERIAL_TEMPLATE_IMMUTABLE",
    "MATERIAL_PARENT_NOT_FOUND",
    "MATERIAL_PARENT_CYCLE",
    "MATERIAL_SITE_NOT_FOUND",
    "MATERIAL_SITE_OCCUPIED",
    "MATERIAL_SITE_TEMPLATE_NOT_ALLOWED",
    "MATERIAL_SITE_CYCLE",
    "MATERIAL_IDENTITY_CONFLICT",
    "MATERIAL_ACTIVE_CLAIM_CONFLICT",
]
