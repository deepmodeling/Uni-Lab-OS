"""Thin FastAPI adapter for the Backend-shaped local Workflow authority."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unilabos.protocol.utils.json_codec import decode_json_bytes, encode_json
from unilabos.protocol.runtime.workflow import (
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    normalize_json_array,
    normalize_json_object,
)
from unilabos.server.services.runtime.workflow.service import WorkflowError, WorkflowService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _BackendModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


HashToken = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_SIGNED_DECIMAL = re.compile(r"[+-]?[0-9]+\Z")
_INT64_MAX = (1 << 63) - 1
_GO_WHITE_SPACE = (
    "\t\n\v\f\r "
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


class _BackendJSONRoute(APIRoute):
    """Preload JSON with the frozen Backend depth and error-envelope rules."""

    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def backend_json_route_handler(request: Request) -> Response:
            content_type = request.headers.get("content-type", "")
            mime = content_type.split(";", 1)[0].strip().lower()
            if mime == "application/json" or mime.endswith("+json"):
                body = await request.body()
                # 无 body 的 GET / DELETE 也常带 JSON Content-Type（httpx / axios 默认头），
                # 与 FastAPI 自身一致：空 body 不解码，是否缺参交给路由参数校验判定。
                if body:
                    try:
                        request._json = decode_json_bytes(body)
                    except (
                        OverflowError,
                        UnicodeError,
                        ValueError,
                    ):
                        return _error(WorkflowError("invalid_input"))
            return await route_handler(request)

        return backend_json_route_handler


def _parse_non_negative_int64_decimal(value: str) -> int:
    """Match Go strconv.ParseInt(value, 10, 64) for an SSE cursor."""

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


class WorkflowCreateRequest(_BackendModel):
    name: str
    tags: List[Any] = Field(default_factory=list)
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    #: 可选的稳定 uuid：Host 子进程上报 ``@workflow`` 默认子工作流时用声明式 uuid
    #: 幂等 upsert（同 uuid 已存在返回冲突，调用方转 PUT）。浏览器创建时不传。
    workflow_uuid: Optional[str] = None

    @field_validator("tags", mode="before")
    @classmethod
    def _json_array(cls, value: Any) -> List[Any]:
        return normalize_json_array(value)

    @field_validator("meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class WorkflowUpdateRequest(WorkflowCreateRequest):
    pass


class WorkflowFromTemplateRequest(_BackendModel):
    """把注册表里的工作流模板（设备包 ``@workflow``）按角色绑定实例化成可运行的工作流。

    ``bindings`` 是 ``{角色 id: device_id}``：设备角色缺省即其设备 id，类角色在物料
    权威里恰有一个该类设备时自动填充，否则必须显式给出。同一模板 + 同一组绑定
    反复调用幂等覆盖同一个工作流（脚本 / e2e 的"运行模板"入口）。
    """

    template_uuid: str
    bindings: Dict[str, str] = Field(default_factory=dict)
    name: Optional[str] = None
    site_binding_mode: Literal["resolve", "preserve"] = Field(
        default="resolve", description="resolve：程序化导入按目标物料解析 Site 标签；preserve：保留浏览器草稿，等待用户确认。"
    )

    @field_validator("bindings", mode="before")
    @classmethod
    def _string_map(cls, value: Any) -> Dict[str, str]:
        mapping = normalize_json_object(value)
        return {str(key): str(item) for key, item in mapping.items()}


class GraphWriteRequest(_BackendModel):
    revision: int = Field(ge=1, le=_INT64_MAX, strict=True)
    nodes: List[WorkflowNodeWrite] = Field(default_factory=list)
    edges: List[WorkflowEdgeWrite] = Field(default_factory=list)
    site_binding_mode: Literal["resolve", "preserve"] = Field(
        default="resolve", description="SiteSlot 导入策略；浏览器保存草稿必须传 preserve，确认后参数中存放 site_uuid。"
    )

    @field_validator("nodes", "edges", mode="before")
    @classmethod
    def _json_array(cls, value: Any) -> List[Any]:
        return [] if value is None else value


class WorkflowTaskCreateRequest(_BackendModel):
    """整图运行与单点设备动作共用的提交体。

    execution_kind=workflow（默认）：workflow_uuid 必填，走整图编排。
    execution_kind=ad_hoc_device_action：device_id + action_name + param 必填，
    生成单 job 任务（微前端设备页/画布单点动作）。

    idempotency_key 两种任务都可选：同键同语义的重复提交返回既有任务，同键不同语义按冲突拒绝。
    """

    execution_kind: str = "workflow"
    workflow_uuid: str = ""
    run_mode: str = "normal"
    target_node_uuid: Optional[str] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    device_id: str = ""
    action_name: str = ""
    action_type: str = ""
    param: Dict[str, Any] = Field(default_factory=dict)
    execution_policy: Dict[str, Any] = Field(default_factory=dict)
    execution_timeout_seconds: int = 0
    idempotency_key: Optional[str] = None

    @field_validator("meta_data", "param", "execution_policy", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class WorkflowTaskCommandRequest(_StrictModel):
    """step 放行一个动作；resume 切回自动；pause 转单步并停止派发新动作（已下发的照常跑完）；
    cancel 取消任务：不再派发，正在执行的动作收到取消请求，设备真实返回后任务落 canceled
    （期间任务为 canceling，设备锁保持）。

    版本与幂等键防止跨页面重复放行。
    """

    type: Literal["step", "resume", "pause", "cancel"]
    expected_revision: int = Field(ge=0, le=_INT64_MAX, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200)


class ManualConfirmationDecisionRequest(_BackendModel):
    """人工确认决策；``confirmed_by`` 缺省时仅 unrestricted 单可用默认操作员。"""

    action: str
    confirmed_by: Optional[str] = None
    comment: Optional[str] = None
    decision_idempotency_key: Optional[str] = None


class DraftWriteRequest(_StrictModel):
    python_source: str
    expected_draft_hash: Optional[HashToken]
    expected_workflow_revision: int = Field(
        ge=1,
        le=_INT64_MAX,
        strict=True,
    )


class ApplyRequest(_StrictModel):
    expected_draft_hash: HashToken
    expected_workflow_revision: int = Field(
        ge=1,
        le=_INT64_MAX,
        strict=True,
    )
    expected_candidate_hash: HashToken


class _BackendJSONResponse(JSONResponse):
    """Render deeply nested Backend JSON without process-global recursion state."""

    def render(self, content: Any) -> bytes:
        return encode_json(content)


def _public_data(data: Any) -> Any:
    """Remove internal workflow fields from public API responses."""

    if isinstance(data, list):
        return [_public_data(value) for value in data]
    if not isinstance(data, dict):
        return data
    result = {key: _public_data(value) for key, value in data.items()}
    if "workflow_snapshot" in result and "workflow_uuid" in result:
        result.pop("input", None)
        result.pop("output", None)
    if "workflow_uuid" in result and "pose" in result and "param" in result:
        result.pop("status", None)
    return result


def _success(data: Any = None, *, status: int = 200) -> _BackendJSONResponse:
    content: Dict[str, Any] = {"code": 0}
    if data is not None:
        content["data"] = _public_data(data)
    return _BackendJSONResponse(status_code=status, content=content)


def _error(error: WorkflowError) -> _BackendJSONResponse:
    conflict_codes = {
        "conflict",
        "draft_hash_conflict",
        "workflow_revision_conflict",
        "candidate_hash_conflict",
        "template_catalog_conflict",
        "candidate_not_ready",
        "draft_invalid",
        "candidate_invalid",
        "manual_confirmation_not_assignee",
        "manual_confirmation_decided",
        "manual_confirmation_key_used",
    }
    if error.code in {"invalid_input", "template_binding_invalid", "site_binding_invalid"}:
        business_code = 1000
    elif error.code in {"not_found", "workflow_not_found", "workflow_template_not_found"}:
        business_code = 3002
    elif error.code in conflict_codes:
        business_code = 3003
    elif error.code in {"template_catalog_unavailable", "workflow_template_unavailable"}:
        business_code = 5001
    else:
        business_code = 1
    return _BackendJSONResponse(
        status_code=200,
        content={
            "code": business_code,
            "error": {"msg": error.message},
        },
    )


def format_sse_event(event: Dict[str, Any]) -> str:
    payload = json.dumps(
        event["data"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event['id']}\nevent: {event['event']}\ndata: {payload}\n\n"


def create_workflow_router(service: WorkflowService) -> APIRouter:
    """Build the public Workflow router around one injected authority."""

    router = APIRouter(
        prefix="/api/v1",
        tags=["workflow"],
        route_class=_BackendJSONRoute,
    )

    def bind_sites(nodes: List[Any], *, mapped_paths: Optional[Dict[str, List[str]]] = None) -> List[Any]:
        from unilabos.server.backend.composition import get_materials_service
        from unilabos.server.composition import get_server_services
        from unilabos.server.api.edge_proxy import edge_http, edge_proxy_enabled
        from unilabos.server.services.runtime.registry import get_registry_service
        from unilabos.server.services.runtime.workflow.site_bindings import (
            SiteBindingError, resolve_workflow_sites,
        )

        registry = get_registry_service()
        materials = get_materials_service()
        try:
            if edge_proxy_enabled():
                # 分进程时 endpoint 数据面在 Host 的库；和浏览器读取同一个入口，
                # 不读取调度权威里为空（或过期）的同名表。
                response = edge_http("GET", "/api/v1/runtime/endpoints?state=online&limit=1000", timeout=5.0)
                if response is None or response.status_code != 200:
                    raise SiteBindingError("Host 动作能力暂不可用，请稍后重试，或用 site_binding_mode=preserve 保存草稿")
                try:
                    endpoints = json.loads(response.body_bytes())
                except (ValueError, UnicodeError) as exc:
                    raise SiteBindingError("Host 动作能力响应无效，不能校验 Site 绑定") from exc
                if not isinstance(endpoints, list):
                    raise SiteBindingError("Host 动作能力响应无效，不能校验 Site 绑定")
            else:
                services = get_server_services()
                endpoints = services.runtime.list_endpoint_snapshots(state="online", limit=1000) if services is not None else []
            return resolve_workflow_sites(
                nodes, registry=registry,
                materials=materials.list_materials() if materials is not None else [],
                mapped_paths=mapped_paths,
                endpoints=endpoints,
            )
        except SiteBindingError as exc:
            raise WorkflowError("site_binding_invalid", detail=str(exc)) from exc

    @router.post("/workflows")
    def create_workflow(body: WorkflowCreateRequest) -> JSONResponse:
        return _success(
            service.create_workflow(**body.model_dump()),
            status=201,
        )

    @router.post("/workflows/from-template")
    def create_workflow_from_template(body: WorkflowFromTemplateRequest) -> JSONResponse:
        """注册表工作流模板 → 可运行工作流（角色绑定 + 类单实例自动解析，幂等 upsert）。"""

        from unilabos.registry.workflows import (
            DeviceCatalog,
            WorkflowTemplateBindingError,
            materialize_workflow_template,
            upsert_workflow,
        )
        from unilabos.server.backend.composition import get_materials_service
        from unilabos.server.services.runtime.registry import get_registry_service

        registry = get_registry_service()
        if registry is None:
            raise WorkflowError("workflow_template_unavailable")
        template = registry.get_workflow_template(body.template_uuid)
        if template is None:
            raise WorkflowError("workflow_template_not_found")
        catalog = DeviceCatalog.from_materials_service(get_materials_service())
        try:
            payload = materialize_workflow_template(
                template, catalog, body.bindings, name=body.name or ""
            )
        except WorkflowTemplateBindingError as exc:
            raise WorkflowError("template_binding_invalid", detail=str(exc)) from exc
        except ValueError as exc:
            raise WorkflowError("invalid_input") from exc
        if body.site_binding_mode == "resolve":
            # 先完成全部绑定再 upsert，失败时不留下半个工作流或覆盖原有参数。
            payload["nodes"] = bind_sites(payload["nodes"])
        workflow = upsert_workflow(service, payload)
        return _success(
            {
                "workflow": workflow,
                "template_uuid": str(template["uuid"]),
                "bindings": payload["bindings"],
            },
            status=201,
        )

    @router.get("/workflows")
    def list_workflows(
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        name: str = Query(default=""),
    ) -> JSONResponse:
        return _success(
            service.list_workflows(page=page, page_size=page_size, name=name)
        )

    @router.get("/workflows/{workflow_uuid}")
    def get_workflow(workflow_uuid: str) -> JSONResponse:
        return _success(service.get_workflow(workflow_uuid))

    @router.put("/workflows/{workflow_uuid}")
    def update_workflow(
        workflow_uuid: str,
        body: WorkflowUpdateRequest,
    ) -> JSONResponse:
        return _success(
            service.update_workflow(
                workflow_uuid, **body.model_dump(exclude={"workflow_uuid"})
            )
        )

    @router.delete("/workflows/{workflow_uuid}")
    def delete_workflow(workflow_uuid: str) -> JSONResponse:
        service.delete_workflow(workflow_uuid)
        return _success()

    @router.get("/workflows/{workflow_uuid}/graph")
    def get_graph(workflow_uuid: str) -> JSONResponse:
        return _success(service.get_graph(workflow_uuid))

    @router.put("/workflows/{workflow_uuid}/graph")
    def save_graph(
        workflow_uuid: str,
        body: GraphWriteRequest,
    ) -> JSONResponse:
        nodes: List[Any] = body.nodes
        if body.site_binding_mode == "resolve":
            mapped_paths: Dict[str, List[str]] = {}
            if body.edges:
                graph = service.get_graph(workflow_uuid)
                handles = {str(item["uuid"]): item for item in graph.get("handle_templates", [])}
                for edge in body.edges:
                    handle = handles.get(edge.target_handle_uuid, {})
                    path = str(handle.get("data_key") or handle.get("handle_key") or "").split("@@@")[-1]
                    if path and path != "ready":
                        mapped_paths.setdefault(edge.target_node_uuid, []).append(path)
            nodes = bind_sites(body.nodes, mapped_paths=mapped_paths)
        return _success(
            service.save_graph(
                workflow_uuid,
                revision=body.revision,
                nodes=nodes,
                edges=body.edges,
            )
        )

    @router.post("/workflow-tasks")
    def create_workflow_task(
        body: WorkflowTaskCreateRequest,
    ) -> JSONResponse:
        if body.execution_kind == "ad_hoc_device_action":
            return _success(
                service.create_ad_hoc_device_action_task(
                    device_id=body.device_id,
                    action_name=body.action_name,
                    action_type=body.action_type,
                    param=body.param,
                    execution_policy=body.execution_policy,
                    execution_timeout_seconds=body.execution_timeout_seconds,
                    idempotency_key=body.idempotency_key,
                    description=body.description,
                    meta_data=body.meta_data,
                ),
                status=201,
            )
        if body.execution_kind != "workflow":
            raise WorkflowError("invalid_input")
        return _success(
            service.create_workflow_task(
                workflow_uuid=body.workflow_uuid,
                run_mode=body.run_mode,
                target_node_uuid=body.target_node_uuid,
                input_value={},
                description=body.description,
                meta_data=body.meta_data,
                idempotency_key=body.idempotency_key,
            ),
            status=201,
        )

    @router.get("/workflow-tasks")
    def list_workflow_tasks(
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        workflow_uuid: Optional[str] = Query(default=None),
        status: str = Query(default=""),
        cleanup_status: str = Query(default=""),
    ) -> JSONResponse:
        return _success(
            service.list_workflow_tasks(
                page=page,
                page_size=page_size,
                workflow_uuid=workflow_uuid,
                status=status,
                cleanup_status=cleanup_status,
            )
        )

    @router.get("/workflow-tasks/{task_uuid}")
    def get_workflow_task(task_uuid: str) -> JSONResponse:
        return _success(service.get_workflow_task(task_uuid))

    @router.post("/workflow-tasks/{task_uuid}/commands")
    def command_workflow_task(task_uuid: str, body: WorkflowTaskCommandRequest) -> JSONResponse:
        return _success(service.command_workflow_task(
            task_uuid, command_type=body.type, expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        ))

    @router.get("/workflow-tasks/{task_uuid}/node-runs")
    def list_workflow_node_runs(task_uuid: str) -> JSONResponse:
        """节点运行视图：每节点一条，status/return_info 为当前 attempt，attempts 为历史。"""

        return _success(service.list_workflow_node_runs(task_uuid))

    @router.get("/workflow-node-runs/{run_uuid}")
    def get_workflow_node_run(run_uuid: str) -> JSONResponse:
        return _success(service.get_workflow_node_run(run_uuid))

    @router.get("/workflow-tasks/{task_uuid}/jobs")
    def list_workflow_node_jobs(task_uuid: str) -> JSONResponse:
        """attempt（物理执行）平铺视图；job uuid 与执行器/错误决策的 job_id 一致。"""

        return _success(service.list_workflow_node_jobs(task_uuid))

    @router.get("/workflow-node-jobs/{job_uuid}")
    def get_workflow_node_job(job_uuid: str) -> JSONResponse:
        return _success(service.get_workflow_node_job(job_uuid))

    @router.get("/workflow-tasks/{task_uuid}/manual-confirmations")
    def list_task_manual_confirmations(
        task_uuid: str,
        limit: Optional[int] = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        return _success(
            service.list_task_manual_confirmations(
                task_uuid, limit=limit, offset=offset
            )
        )

    @router.get("/workflow-manual-confirmations/{confirmation_uuid}")
    def get_manual_confirmation(confirmation_uuid: str) -> JSONResponse:
        return _success(service.get_workflow_manual_confirmation(confirmation_uuid))

    @router.post("/workflow-manual-confirmations/{confirmation_uuid}/decision")
    def decide_manual_confirmation(
        confirmation_uuid: str,
        body: ManualConfirmationDecisionRequest,
    ) -> JSONResponse:
        """原子记录人工确认决策；调度器消费后才推进对应 workflow job。"""

        return _success(
            service.decide_workflow_manual_confirmation(
                confirmation_uuid,
                action=body.action,
                confirmed_by=body.confirmed_by,
                comment=body.comment,
                decision_idempotency_key=body.decision_idempotency_key,
            )
        )

    @router.post(
        "/workflow-tasks/{task_uuid}/manual-confirmations/{confirmation_uuid}/decision"
    )
    def decide_task_manual_confirmation(
        task_uuid: str,
        confirmation_uuid: str,
        body: ManualConfirmationDecisionRequest,
    ) -> JSONResponse:
        """task 维度兼容入口：先校验确认单确实属于该 task。"""

        confirmation = service.get_workflow_manual_confirmation(confirmation_uuid)
        if confirmation["workflow_task_uuid"] != service.get_workflow_task(task_uuid)["uuid"]:
            raise WorkflowError("not_found")
        return _success(
            service.decide_workflow_manual_confirmation(
                confirmation_uuid,
                action=body.action,
                confirmed_by=body.confirmed_by,
                comment=body.comment,
                decision_idempotency_key=body.decision_idempotency_key,
            )
        )

    @router.get("/workflow-tasks/{task_uuid}/interventions")
    def list_task_interventions(
        task_uuid: str,
        limit: Optional[int] = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        return _success(
            service.list_task_interventions(task_uuid, limit=limit, offset=offset)
        )

    @router.get("/workflow-node-jobs/{job_uuid}/results")
    def list_node_job_results(
        job_uuid: str,
        limit: Optional[int] = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        return _success(
            service.list_node_job_results(job_uuid, limit=limit, offset=offset)
        )

    @router.get("/workflow-node-jobs/{job_uuid}/feedback-history")
    def list_node_job_feedback_history(
        job_uuid: str,
        limit: Optional[int] = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        return _success(
            service.list_node_job_feedback_history(
                job_uuid, limit=limit, offset=offset
            )
        )

    @router.get("/workflows/{workflow_uuid}/authoring")
    def get_authoring(workflow_uuid: str) -> JSONResponse:
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
        return _success(
            service.apply_authoring(
                workflow_uuid,
                expected_draft_hash=body.expected_draft_hash,
                expected_workflow_revision=body.expected_workflow_revision,
                expected_candidate_hash=body.expected_candidate_hash,
            )
        )

    from unilabos.server.api.runtime.events import create_runtime_events_router

    router.include_router(create_runtime_events_router(service))

    return router


def install_workflow_api(app: FastAPI, service: WorkflowService) -> None:
    """Install error mapping and routes into an OS FastAPI application."""

    @app.exception_handler(WorkflowError)
    async def workflow_error_handler(
        _request: Request,
        error: WorkflowError,
    ) -> JSONResponse:
        return _error(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        workflow_prefixes = (
            "/api/v1/workflows",
            "/api/v1/workflow-tasks",
            "/api/v1/workflow-node-jobs",
            "/api/v1/workflow-manual-confirmations",
            "/api/v1/events",
        )
        if any(
            request.url.path == prefix or request.url.path.startswith(f"{prefix}/")
            for prefix in workflow_prefixes
        ):
            # 只回字段路径与校验信息，不把完整输入（可能含驱动凭据）或堆栈回显。
            detail = "; ".join(
                f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                for item in error.errors()[:5]
            )
            return _error(WorkflowError("invalid_input", detail=detail))
        return await request_validation_exception_handler(request, error)

    # 同进程后装配 Workflow Authority 时替换无工作流读取器的公共通知路由。
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "name", "") != "runtime_events"]
    app.include_router(create_workflow_router(service))


def create_workflow_app(service: WorkflowService) -> FastAPI:
    """Create a focused application used by composition and contract tests."""

    app = FastAPI(title="Uni-Lab Workflow", version="0.1.0")
    install_workflow_api(app, service)
    return app


__all__ = [
    "create_workflow_app",
    "create_workflow_router",
    "format_sse_event",
    "install_workflow_api",
]
