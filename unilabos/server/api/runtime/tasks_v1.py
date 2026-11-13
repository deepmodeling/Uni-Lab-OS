"""V1 任务观测与删除路由：展示投影、删除资格、单步候选、执行锁、运行日志、等待图、过程反馈。"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from unilabos.server.api.envelope import WORKFLOW, envelope_router
from unilabos.server.services.runtime.workflow.task_lifecycle import TaskObservability

_INT64_MAX = (1 << 63) - 1
_DECIMAL = re.compile(r"[+-]?[0-9]+\Z")


class WorkflowTaskJobParamUpdateRequest(BaseModel):
    """V1：暂停任务中尚未派发作业的参数补丁（顶层合并）。"""

    model_config = ConfigDict(extra="forbid")
    param: Dict[str, Any]


class WorkflowTaskExecutionLockReleaseRequest(BaseModel):
    """V1：执行锁人工释放；必须携带物理安全确认和 Claim / Fence 快照。"""

    model_config = ConfigDict(extra="forbid")
    expected_claim_uuid: str
    expected_fencing_token: StrictInt = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)
    physical_settlement_confirmed: StrictBool


def _cursor(value: str, *, default: int, maximum: int, positive: bool = False) -> int:
    """V1 的 Go 风格十进制游标：空串取默认值；非法、越界按参数错误。"""

    from unilabos.server.services.runtime.workflow.service import WorkflowError

    text = value.strip()
    if not text:
        return default
    if _DECIMAL.fullmatch(text) is None:
        raise WorkflowError("invalid_input")
    number = int(text, 10)
    if number < (1 if positive else 0) or number > maximum:
        raise WorkflowError("invalid_input")
    return number


def operator_identity(request: Request) -> str:
    """操作员身份：请求上下文给出时用它，本地无多用户身份时记服务端身份（V1 同口径）。"""

    return str(getattr(request.state, "operator_id", None) or request.headers.get("X-UniLab-Operator") or
               "local-workspace")


def create_task_observability_router(service: Any) -> APIRouter:
    router = envelope_router(profile=WORKFLOW, prefix="/api/v1", tags=["workflow"])
    observability = TaskObservability(service)

    @router.get("/workflow-task-presentations", summary="任务矩阵紧凑投影")
    def list_workflow_task_presentations(
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        workflow_uuid: Optional[str] = Query(default=None),
        execution_kind: str = Query(default=""),
        status: str = Query(default=""),
        cleanup_status: str = Query(default=""),
        view: str = Query(default=""),
        terminal_limit: int = Query(default=20),
    ) -> Dict[str, Any]:
        """每个任务批量嵌入紧凑 Job 状态；``view=matrix`` 返回全部活动 / 需关注任务与最多
        ``terminal_limit`` 个近期终态任务。"""

        return observability.presentations(
            page=page, page_size=page_size, workflow_uuid=workflow_uuid, execution_kind=execution_kind,
            status=status, cleanup_status=cleanup_status, view=view, terminal_limit=terminal_limit,
        )

    @router.get("/workflow-tasks/{task_uuid}/delete-eligibility", summary="查询工作流任务删除资格")
    def get_workflow_task_delete_eligibility(task_uuid: str) -> Dict[str, Any]:
        return observability.delete_eligibility(task_uuid)

    @router.delete("/workflow-tasks/{task_uuid}", summary="删除工作流任务")
    def delete_workflow_task(task_uuid: str) -> Dict[str, Any]:
        """软删除已终态且执行资源全部释放的任务；重复请求返回 ``already_deleted``。"""

        return observability.delete(task_uuid)

    @router.patch("/workflow-tasks/{task_uuid}/jobs/{job_uuid}/param", summary="修改暂停任务的未下发参数")
    def patch_workflow_node_job_param(task_uuid: str, job_uuid: str,
                                      body: WorkflowTaskJobParamUpdateRequest) -> Dict[str, Any]:
        from unilabos.server.services.runtime.workflow.task_lifecycle import patch_pending_job_param

        return patch_pending_job_param(service, task_uuid, job_uuid, body.param)

    @router.get("/workflow-tasks/{task_uuid}/step-state", summary="单步候选")
    def get_workflow_task_step_state(task_uuid: str) -> Dict[str, Any]:
        return observability.step_state(task_uuid)

    @router.get("/workflow-tasks/{task_uuid}/execution-locks", summary="任务活动执行锁")
    def list_workflow_task_execution_locks(task_uuid: str) -> Dict[str, Any]:
        return observability.execution_locks(task_uuid)

    @router.post("/workflow-tasks/{task_uuid}/execution-locks/{lease_uuid}/force-release",
                 summary="人工释放执行锁")
    def force_release_workflow_task_execution_lock(
        task_uuid: str, lease_uuid: str, body: WorkflowTaskExecutionLockReleaseRequest, request: Request,
    ) -> Dict[str, Any]:
        return observability.force_release(
            task_uuid, lease_uuid,
            expected_claim_uuid=body.expected_claim_uuid,
            expected_fencing_token=body.expected_fencing_token,
            reason=body.reason,
            physical_settlement_confirmed=body.physical_settlement_confirmed,
            operator_id=operator_identity(request),
        )

    @router.get("/workflow-tasks/{task_uuid}/events", summary="任务运行日志")
    def list_workflow_task_runtime_events(
        task_uuid: str,
        after_sequence: str = Query(default=""),
        limit: str = Query(default=""),
    ) -> Dict[str, Any]:
        return observability.task_events(
            task_uuid,
            after_sequence=_cursor(after_sequence, default=0, maximum=_INT64_MAX),
            limit=_cursor(limit, default=100, maximum=500, positive=True),
        )

    @router.get("/workflow-runtime/wait-graph", summary="整站作业等待图")
    def get_execution_wait_graph() -> Dict[str, Any]:
        return observability.wait_graph()

    @router.get("/workflow-tasks/{task_uuid}/material-events", summary="任务造成的物料变化")
    def list_task_material_events(task_uuid: str) -> list:
        """物料权威台账中由本任务作业产生的变化（搬运、消耗、标为已使用…），标注节点与步骤路径。"""

        from unilabos.server.backend.composition import get_materials_service
        from unilabos.server.services.runtime.workflow.task_lifecycle import task_material_events

        return task_material_events(service, task_uuid, get_materials_service())

    @router.get("/workflow-node-jobs/{job_uuid}/feedback", summary="作业过程反馈")
    def list_workflow_node_job_feedback(
        job_uuid: str,
        page: int = Query(default=1),
        page_size: int = Query(default=20),
    ) -> Dict[str, Any]:
        return observability.feedback(job_uuid, page=page, page_size=page_size)

    return router


__all__ = ["create_task_observability_router", "operator_identity"]
