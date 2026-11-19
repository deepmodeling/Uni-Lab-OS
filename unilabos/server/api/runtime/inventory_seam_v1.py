"""V1 作业与库存接缝路由：消费事实、实际用量核对、结果未知处置。

- ``GET /workflow-tasks/{task_uuid}/inventory-consumptions``、``GET /workflow-node-jobs/{job_uuid}/inventory-consumptions``、
  ``GET /reagents/{reagent_uuid}/inventory-consumptions``：materials.db 的消费事实（读模型见
  ``services/materials/consumption.py``），由这里补齐 runtime 一侧的节点 / 工作流名称与人工核对记录。未装配
  物料权威时为空数组（V1 同口径）；任务 / 作业不存在 3002，非法身份 1000；试剂删除后仍可读。
- ``POST /workflow-node-jobs/{job_uuid}/reconcile-quantity``：人工核对任何作业的实际用量；返回作业（V1）并附
  ``inventory_consumptions``。规则见 ``backend/scheduler/manual_settlement.py``。
- ``POST /workflow-node-jobs/{job_uuid}/resolve-uncertain``：结果未知作业的人工处置（``resolution=canceled``）；
  ``{job, resolution_command_uuid, pending_edge_confirmation, created}``，执行面尚未确认时 HTTP 202，否则 200。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Literal, Mapping, Optional

from fastapi import APIRouter, Request
from pydantic import Field, field_validator, model_validator

from unilabos.protocol.runtime.workflow import validate_uuid
from unilabos.server.api.envelope import SERVICE_UNAVAILABLE, WORKFLOW, ApiError, envelope_router
from unilabos.server.api.runtime.workflow import _StrictModel, _success


class QuantityConsumptionCorrection(_StrictModel):
    """操作员核对后的一项实际用量；``lot`` 是 dev 扩展（批次库存，身份为批次 uuid）。"""

    inventory_type: Literal["reagent", "current_substance", "lot"]
    inventory_uuid: str = Field(min_length=1)
    actual_quantity: float = Field(ge=0, allow_inf_nan=False)
    quantity_unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def _identity(self) -> "QuantityConsumptionCorrection":
        if self.inventory_type != "lot":
            self.inventory_uuid = validate_uuid(self.inventory_uuid)
        return self


class QuantityReconciliationRequest(_StrictModel):
    """V1：更正请求不授权重新派发设备，只结算已有结果。"""

    consumptions: List[QuantityConsumptionCorrection] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1)


class UncertainJobResolutionRequest(_StrictModel):
    resolution: str
    reason: str
    device_command_id: Optional[str] = None

    @field_validator("resolution", "reason")
    @classmethod
    def _text(cls, value: str) -> str:
        return value.strip()


def _default_materials() -> Any:
    from unilabos.server.backend.composition import get_materials_service

    return get_materials_service()


def _default_desk() -> Any:
    from unilabos.server.services.runtime.error_decisions import default_desk

    return default_desk()


def _names(task: Mapping[str, Any]) -> tuple[str, Dict[str, Mapping[str, Any]]]:
    snapshot = task.get("workflow_snapshot") or {}
    workflow = snapshot.get("workflow") or {}
    nodes = {str(node.get("uuid")): node for node in snapshot.get("nodes") or [] if isinstance(node, Mapping)}
    return str(workflow.get("name") or ""), nodes


def annotate_consumptions(records: List[Dict[str, Any]], jobs: Mapping[str, Mapping[str, Any]],
                          tasks: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """补齐节点 / 工作流名称（V1 消费快照字段）与该作业的人工核对记录。"""

    for record in records:
        job = jobs.get(str(record.get("workflow_node_job_uuid") or "")) or {}
        task = tasks.get(str(record.get("workflow_task_uuid") or "")) or {}
        workflow_name, nodes = _names(task)
        node_uuid = str(job.get("workflow_node_uuid") or record.get("workflow_node_uuid") or "")
        record["workflow_node_uuid"] = node_uuid or None
        record["workflow_name"] = workflow_name
        record["node_name"] = str((nodes.get(node_uuid) or {}).get("name") or "")
        reconciliation = (job.get("control_data") or {}).get("quantity_reconciliation") or {}
        corrected = {(item.get("inventory_type"), item.get("inventory_uuid"))
                     for item in reconciliation.get("consumptions") or []}
        if (record.get("inventory_type"), record.get("inventory_uuid")) in corrected:
            record["meta_data"] = {**record.get("meta_data", {}), "reconciliation": {
                "request": {"consumptions": reconciliation.get("consumptions"), "reason": reconciliation.get("reason")},
                "reason": reconciliation.get("reason"),
                "at": reconciliation.get("reconciled_at"),
                "operator_id": reconciliation.get("operator_id"),
            }}
    return records


def create_inventory_seam_router(
    service: Any,
    *,
    get_materials: Optional[Callable[[], Any]] = None,
    get_desk: Optional[Callable[[], Any]] = None,
) -> APIRouter:
    router = envelope_router(profile=WORKFLOW, prefix="/api/v1", tags=["workflow"])
    materials_getter = get_materials or _default_materials
    desk_getter = get_desk or _default_desk

    def consumptions_of(materials: Any) -> Any:
        from unilabos.server.services.materials.consumption import InventoryConsumptions

        return InventoryConsumptions(materials)

    def job_records(task: Mapping[str, Any], jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        materials = materials_getter()
        if materials is None:
            return []
        records = consumptions_of(materials).for_jobs([job["uuid"] for job in jobs])
        return annotate_consumptions(records, {job["uuid"]: job for job in jobs}, {task["uuid"]: task})

    @router.get("/workflow-tasks/{task_uuid}/inventory-consumptions", summary="任务的数量型库存消费事实")
    def list_task_inventory_consumptions(task_uuid: str) -> List[Dict[str, Any]]:
        task = service.get_workflow_task(task_uuid)
        return job_records(task, service.list_jobs(task["uuid"]))

    @router.get("/workflow-node-jobs/{job_uuid}/inventory-consumptions", summary="作业的数量型库存消费事实")
    def list_job_inventory_consumptions(job_uuid: str) -> List[Dict[str, Any]]:
        job = service.get_workflow_node_job(job_uuid)
        return job_records(service.get_workflow_task(job["workflow_task_uuid"]), [job])

    @router.get("/reagents/{reagent_uuid}/inventory-consumptions", summary="试剂的工作流消费谱系")
    def list_reagent_inventory_consumptions(reagent_uuid: str) -> List[Dict[str, Any]]:
        from unilabos.server.services.runtime.workflow.service import WorkflowError

        try:
            reagent_uuid = validate_uuid(reagent_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        materials = materials_getter()
        if materials is None:
            return []
        records = consumptions_of(materials).list_substance(reagent_uuid)
        jobs: Dict[str, Dict[str, Any]] = {}
        tasks: Dict[str, Dict[str, Any]] = {}
        for record in records:
            job_uuid, task_uuid = record["workflow_node_job_uuid"], record["workflow_task_uuid"]
            for cache, identity, reader in ((jobs, job_uuid, service.get_workflow_node_job),
                                            (tasks, task_uuid, service.get_workflow_task)):
                if identity and identity not in cache:
                    try:
                        cache[identity] = reader(identity)
                    except WorkflowError:  # 任务已删除或不在本权威：谱系仍保留，名称留空
                        cache[identity] = {}
        return annotate_consumptions(records, jobs, tasks)

    @router.post("/workflow-node-jobs/{job_uuid}/reconcile-quantity", summary="核对作业实际用量")
    def reconcile_job_quantity(job_uuid: str, body: QuantityReconciliationRequest, request: Request) -> Any:
        from unilabos.server.api.runtime.tasks_v1 import operator_identity
        from unilabos.server.backend.scheduler.manual_settlement import QuantityReconciliation

        materials = materials_getter()
        if materials is None:
            raise ApiError(SERVICE_UNAVAILABLE, "本进程没有物料权威，不能核对实际用量")
        job = QuantityReconciliation(service, materials).reconcile(
            job_uuid, [item.model_dump() for item in body.consumptions], body.reason,
            operator_id=operator_identity(request),
        )
        task = service.get_workflow_task(job["workflow_task_uuid"])
        return _success({**job, "inventory_consumptions": job_records(task, [job])})

    @router.post("/workflow-node-jobs/{job_uuid}/resolve-uncertain", summary="结果未知作业的人工处置")
    def resolve_uncertain_job(job_uuid: str, body: UncertainJobResolutionRequest, request: Request) -> Any:
        from unilabos.server.api.runtime.tasks_v1 import operator_identity
        from unilabos.server.backend.scheduler.manual_settlement import UncertainResolution
        from unilabos.server.services.runtime.error_decisions import ErrorDecisionNotPending
        from unilabos.server.services.runtime.workflow.errors import StoreConflict

        try:
            result = UncertainResolution(service, desk_getter()).resolve(
                job_uuid, resolution=body.resolution, reason=body.reason,
                device_command_id=body.device_command_id, operator_id=operator_identity(request),
            )
        except ErrorDecisionNotPending as exc:
            raise StoreConflict(f"作业的待裁决已不在等待：{exc}") from exc
        return _success(result, status=202 if result["pending_edge_confirmation"] else 200)

    return router


__all__ = [
    "QuantityConsumptionCorrection",
    "QuantityReconciliationRequest",
    "UncertainJobResolutionRequest",
    "annotate_consumptions",
    "create_inventory_seam_router",
]
