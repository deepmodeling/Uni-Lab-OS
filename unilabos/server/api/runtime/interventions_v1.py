"""V1 工作流干预、结构化人工交互与按作业的人工确认路由。"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictFloat

from unilabos.server.api.envelope import WORKFLOW, envelope_router, ok_response
from unilabos.server.api.runtime.tasks_v1 import operator_identity

_INT64_MAX = (1 << 63) - 1


class HumanInteractionReply(BaseModel):
    """V1 回复形状：choice / text / values / x / y / corner_x / corner_y，按请求类型取其一组。"""

    model_config = ConfigDict(extra="forbid")
    choice: Optional[str] = None
    text: Optional[str] = None
    values: Optional[list[StrictFloat]] = None
    x: Optional[list[StrictFloat]] = None
    y: Optional[list[StrictFloat]] = None
    corner_x: Optional[list[StrictFloat]] = None
    corner_y: Optional[list[StrictFloat]] = None


class WorkflowInterventionDecisionRequest(BaseModel):
    """V1：按修订选择一个设备已经提供的干预方案。"""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, le=_INT64_MAX, strict=True)
    option_id: str
    # 需要服务端补记的兜底动作结果（operator_intervention）；普通 retry / skip / abort 不传
    result: Any = None


class ManualConfirmationDecisionRequest(BaseModel):
    """V1 ``{action: approve|reject}``；dev 的确认人、备注与决策幂等键作为可选扩展保留。"""

    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject"]
    confirmed_by: Optional[str] = None
    comment: Optional[str] = None
    decision_idempotency_key: Optional[str] = None


def _workflow_errors(callable_, *args, **kwargs):
    """干预 / 人工交互存储异常 → 工作流错误口径（not_found 3002、conflict 3003、invalid 1000）。"""

    from unilabos.server.services.runtime.workflow.errors import StoreConflict, StoreNotFound
    from unilabos.server.services.runtime.workflow.service import WorkflowConflict, WorkflowError

    try:
        return callable_(*args, **kwargs)
    except StoreNotFound:
        raise WorkflowError("not_found") from None
    except StoreConflict as exc:
        raise WorkflowConflict("conflict", detail=str(exc)) from None
    except ValueError as exc:
        raise WorkflowError("invalid_input", detail=str(exc)) from None


def create_interventions_router(service: Any) -> APIRouter:
    from unilabos.protocol.runtime.workflow import validate_uuid
    from unilabos.server.services.runtime.workflow.human_interactions import HumanInteractions
    from unilabos.server.services.runtime.workflow.interventions import WorkflowInterventions
    from unilabos.server.services.runtime.workflow.service import WorkflowError

    router = envelope_router(profile=WORKFLOW, prefix="/api/v1", tags=["workflow"])
    humans = HumanInteractions(service)

    def deliver(decision_id: str, payload: Dict[str, Any]) -> bool:
        deliverer = getattr(service, "_intervention_delivery", None)
        if deliverer is None:
            from unilabos.server.services.runtime.error_decisions import default_desk

            deliverer = default_desk().deliver
        return bool(deliverer(decision_id, payload))

    interventions = WorkflowInterventions(service, deliver)

    def identity(value: str) -> str:
        try:
            return validate_uuid(value)
        except ValueError:
            raise WorkflowError("invalid_input") from None

    @router.get("/workflow-interventions", summary="工作流干预列表")
    def list_workflow_interventions(status: str = Query(default="open"), limit: int = Query(default=100)):
        """按状态（open / selected / superseded）查询等待处理或已经处理的工作流干预。"""

        return _workflow_errors(interventions.list, status=status, limit=limit)

    @router.get("/workflow-interventions/{intervention_uuid}", summary="工作流干预详情")
    def get_workflow_intervention(intervention_uuid: str):
        return _workflow_errors(interventions.get, identity(intervention_uuid))

    @router.post("/workflow-interventions/{intervention_uuid}/decisions", summary="选择干预方案")
    def select_workflow_intervention(
        intervention_uuid: str,
        body: WorkflowInterventionDecisionRequest,
        request: Request,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> JSONResponse:
        """首次选择返回 201，同键重放返回 200；投递未被执行面接受时 3003（可用同一键重试）。"""

        result = _workflow_errors(
            interventions.select, identity(intervention_uuid), revision=body.revision, option_id=body.option_id,
            idempotency_key=idempotency_key, result=body.result, operator_id=operator_identity(request),
        )
        return ok_response(result, status_code=201 if result["created"] else 200)

    @router.get("/workflow-tasks/{task_uuid}/human-interactions", summary="任务的人工交互")
    def list_human_interactions(task_uuid: str):
        task = service.get_workflow_task(task_uuid)
        return humans.list(task["uuid"])

    @router.post("/workflow-human-interactions/{interaction_uuid}/reply", summary="回复人工交互")
    def reply_human_interaction(interaction_uuid: str, body: HumanInteractionReply):
        reply = _workflow_errors(humans.reply, identity(interaction_uuid), body.model_dump(exclude_none=True))
        controller = getattr(service, "_task_controller", None)
        if controller is not None:
            try:
                controller(reply["task_uuid"])
            except Exception:  # noqa: BLE001 - 回复已落库，调度器恢复后重新读取
                pass
        return reply

    @router.post("/workflow-node-jobs/{job_uuid}/manual-confirmation", summary="按作业提交人工确认")
    def decide_manual_confirmation(job_uuid: str, body: ManualConfirmationDecisionRequest):
        job = service.get_workflow_node_job(job_uuid)
        confirmation = service.get_manual_confirmation_for_job(job["uuid"])
        if confirmation is None:
            raise WorkflowError("not_found", detail="该作业没有人工确认单")
        return service.decide_workflow_manual_confirmation(
            confirmation["uuid"], action=body.action, confirmed_by=body.confirmed_by, comment=body.comment,
            decision_idempotency_key=body.decision_idempotency_key,
        )

    return router


__all__ = ["create_interventions_router"]
