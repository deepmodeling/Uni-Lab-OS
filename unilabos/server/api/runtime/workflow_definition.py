"""V1 工作流作者路由：增量节点 / 连线编辑、实验操作类别、节点模板查询。

与 ``workflow.py`` 共用同一个 Workflow Authority 与工作流错误口径（404 → 3002、409 → 3003），
请求 / 响应 DTO 以 V1.0 为准（``unilabos/app/workflow_api.py``、``operation_category_api.py``、
``workflow_template_api.py``）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unilabos.protocol.runtime.workflow import normalize_json_object
from unilabos.server.services.runtime.workflow.service import WorkflowService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _BackendModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WorkflowNodeCreateRequest(_StrictModel):
    """向现有工作流增加一个节点（V1）；模板节点的类型 / 动作 / 默认参数由模板派生。"""

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

    @field_validator("pose", "manual_confirmation", "execution_policy", "meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class WorkflowNodePatchRequest(_StrictModel):
    """V1 允许局部更新的节点字段（只应用显式提交的字段）。"""

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
    node_uuids: List[str] = Field(default_factory=list)
    edge_uuids: List[str] = Field(default_factory=list)


class WorkflowDuplicateRequest(_BackendModel):
    name: Optional[str] = None


class DeviceActionRunCreateRequest(_StrictModel):
    """V1 设备单动作运行：设备物料 UUID + 动作模板 UUID。"""

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
        return normalize_json_object(value)


class JobAddRequest(_BackendModel):
    """V1 旧版 ``POST /job/add``（仍写在 V1 http_api.md 中）：按设备 ID + 动作名下发单动作。"""

    device_id: str = ""
    action: str = ""
    action_type: str = ""
    sample_material: Dict[str, Any] = Field(default_factory=dict)
    action_args: Dict[str, Any] = Field(default_factory=dict)
    task_id: str = ""
    job_id: str = ""
    node_id: str = ""
    notebook_id: str = ""
    server_info: Dict[str, Any] = Field(default_factory=dict)
    data: Dict[str, Any] = Field(default_factory=dict)


#: dev 节点作业状态 → V1 旧版 JobData.status（0 UNKNOWN 1 ACCEPTED 2 EXECUTING 3 CANCELING 4 SUCCEEDED 5 CANCELED 6 ABORTED）
_LEGACY_JOB_STATUS = {
    "pending": 1, "queued": 1, "dispatching": 1, "dispatched": 1, "accepted": 1,
    "running": 2, "execution_unknown": 2, "intervention_required": 2,
    "cancel_requested": 3, "canceling": 3,
    "succeeded": 4, "skipped": 4,
    "canceled": 5,
    "failed": 6, "timeout": 6, "aborted": 6,
}


class OperationCategoryCreateRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=64)
    sort_order: int = Field(default=100, ge=0, le=10_000, strict=True)


class OperationCategoryUpdateRequest(_StrictModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    sort_order: Optional[int] = Field(default=None, ge=0, le=10_000, strict=True)


def add_definition_routes(
    router: APIRouter,
    service: WorkflowService,
    *,
    success: Callable[..., Any],
    with_workflow_status: Callable[[Any], Any],
) -> None:
    """把 V1 增量编辑、类别与节点模板路由挂到工作流路由器上（同一信封路由类）。"""

    # ── 节点 / 连线 ───────────────────────────────────────────────

    @router.post("/workflows/{workflow_uuid}/nodes")
    def create_workflow_node(workflow_uuid: str, body: WorkflowNodeCreateRequest) -> Any:
        """增加节点，由完整图校验器原子推进修订；返回整图（HTTP 201）。"""

        return success(
            with_workflow_status(service.create_workflow_node(workflow_uuid, payload=body.model_dump())),
            status=201,
        )

    @router.get("/workflows/{workflow_uuid}/nodes")
    def list_workflow_nodes(
        workflow_uuid: str,
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        workflow_node_template_uuid: Optional[str] = Query(default=None),
        material_uuid: Optional[str] = Query(default=None),
    ) -> Any:
        return success(
            service.list_workflow_nodes(
                workflow_uuid,
                page=page,
                page_size=page_size,
                workflow_node_template_uuid=workflow_node_template_uuid,
                material_uuid=material_uuid,
            )
        )

    @router.post("/workflows/{workflow_uuid}/edges")
    def create_workflow_edge(workflow_uuid: str, body: WorkflowEdgeCreateRequest) -> Any:
        """增加连线（复用完整图引用与环路校验）；返回整图（HTTP 201）。"""

        return success(
            with_workflow_status(service.create_workflow_edge(workflow_uuid, payload=body.model_dump())),
            status=201,
        )

    @router.post("/workflows/{workflow_uuid}/batch-delete")
    def batch_delete_workflow_graph(workflow_uuid: str, body: WorkflowBatchDeleteRequest) -> Any:
        return success(
            with_workflow_status(
                service.batch_delete_workflow_graph(
                    workflow_uuid, node_uuids=body.node_uuids, edge_uuids=body.edge_uuids,
                )
            )
        )

    @router.post("/workflows/{workflow_uuid}/duplicate")
    def duplicate_workflow(workflow_uuid: str, body: WorkflowDuplicateRequest) -> Any:
        return success(with_workflow_status(service.duplicate_workflow(workflow_uuid, name=body.name)), status=201)

    @router.get("/workflow-nodes/{node_uuid}")
    def get_workflow_node(node_uuid: str) -> Any:
        return success(service.get_workflow_node(node_uuid))

    @router.patch("/workflow-nodes/{node_uuid}")
    def patch_workflow_node(node_uuid: str, body: WorkflowNodePatchRequest) -> Any:
        return success(service.patch_workflow_node(node_uuid, patch=body.model_dump(exclude_unset=True)))

    @router.delete("/workflow-nodes/{node_uuid}")
    def delete_workflow_node(node_uuid: str) -> Any:
        service.delete_workflow_node(node_uuid)
        return success()

    @router.post("/workflow-nodes/{node_uuid}/duplicate")
    def duplicate_workflow_node(node_uuid: str, body: WorkflowDuplicateRequest) -> Any:
        return success(service.duplicate_workflow_node(node_uuid, name=body.name), status=201)

    @router.delete("/workflow-edges/{edge_uuid}")
    def delete_workflow_edge(edge_uuid: str) -> Any:
        service.delete_workflow_edge(edge_uuid)
        return success()

    # ── 设备单动作 ──────────────────────────────────────────────

    @router.post("/device-action-runs")
    def create_device_action_run(body: DeviceActionRunCreateRequest) -> Any:
        """V1：创建或幂等复用一次设备单动作运行；首次创建 HTTP 201，复用 200。"""

        result = service.create_device_action_run(**body.model_dump())
        return success(result, status=201 if result["created"] else 200)

    @router.post("/job/add")
    def legacy_job_add(body: JobAddRequest) -> Any:
        """V1 旧版单动作下发：返回 ``{jobId, status}``（1 = 已受理）。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowError

        action_name = str(body.data.get("action") or body.action) if body.data else body.action
        action_args = (body.data.get("action_kwargs") or body.data.get("action_args")) if body.data else body.action_args
        if action_args is None:
            action_args = body.action_args or {}
        elif isinstance(action_args, dict) and "command" in action_args and len(action_args) == 1:
            action_args = action_args["command"]
        if not body.device_id:
            raise WorkflowError("invalid_input", detail="device_id is required")
        if not action_name:
            raise WorkflowError("invalid_input", detail="action is required")
        task = service.create_ad_hoc_device_action_task(
            device_id=body.device_id,
            action_name=action_name,
            action_type=body.action_type,
            param=action_args if isinstance(action_args, dict) else {"command": action_args},
            idempotency_key=body.job_id or None,
            description=None,
            meta_data={"legacy_job_add": True, **({"task_id": body.task_id} if body.task_id else {})},
        )
        jobs = service.list_workflow_node_jobs(task["uuid"])
        return success({"jobId": jobs[0]["uuid"] if jobs else task["uuid"], "status": 1, "result": {}})

    @router.get("/job/{job_id}/status")
    def legacy_job_status(job_id: str) -> Any:
        """V1 旧版作业状态：未知作业返回 status 0（与 V1 一致，不报错）。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowError

        try:
            job = service.get_workflow_node_job(job_id)
        except WorkflowError as exc:
            if exc.code not in {"not_found", "invalid_input"}:
                raise
            return success({"jobId": job_id, "status": 0, "result": {}})
        status = _LEGACY_JOB_STATUS.get(str(job.get("status") or ""), 0)
        result = job.get("return_info") if status >= 4 else {}
        return success({"jobId": job["uuid"], "status": status, "result": result if isinstance(result, dict) else {}})

    # ── 实验操作类别 ──────────────────────────────────────────────

    @router.get("/experiment-operation-categories")
    def list_experiment_operation_categories() -> Any:
        return success({"items": service.list_operation_categories()})

    @router.post("/experiment-operation-categories")
    def create_experiment_operation_category(body: OperationCategoryCreateRequest) -> Any:
        return success(service.create_operation_category(**body.model_dump()), status=201)

    @router.get("/experiment-operation-categories/{category_uuid}")
    def get_experiment_operation_category(category_uuid: str) -> Any:
        return success(service.get_operation_category(category_uuid))

    @router.put("/experiment-operation-categories/{category_uuid}")
    def update_experiment_operation_category(category_uuid: str, body: OperationCategoryUpdateRequest) -> Any:
        return success(service.update_operation_category(category_uuid, **body.model_dump(exclude_unset=True)))

    @router.delete("/experiment-operation-categories/{category_uuid}")
    def delete_experiment_operation_category(category_uuid: str) -> Any:
        service.delete_operation_category(category_uuid)
        return success()

    # ── 节点模板（设备注册表投影 + 已发布工作流模板） ─────────────────────

    @router.get("/workflow-node-templates")
    def list_node_templates(
        page: int = Query(default=0),
        page_size: int = Query(default=0),
        keyword: str = Query(default=""),
        resource_template_uuid: Optional[UUID] = Query(default=None),
        action_type: str = Query(default="", alias="type"),
        node_type: str = Query(default=""),
    ) -> Any:
        return success(
            service.list_node_templates(
                page=page,
                page_size=page_size,
                keyword=keyword,
                resource_template_uuid=str(resource_template_uuid) if resource_template_uuid else None,
                action_type=action_type,
                node_type=node_type,
            )
        )

    @router.get("/workflow-node-templates/{template_uuid}")
    def get_node_template(template_uuid: UUID) -> Any:
        return success(service.get_node_template(str(template_uuid)))


__all__ = ["add_definition_routes"]
