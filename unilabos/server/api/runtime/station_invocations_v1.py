"""V1 工站调用入口 ``POST /api/v1/station/workflow-invocations``（上游 Backend → 本工站）。

鉴权按 V1：``Authorization: Bearer <station_api_key>``（密钥见 ``unilabos/config/station_key.py``），缺失或
错误为 HTTP 401 + 信封 1002；控制面 token 不能代替工站密钥。控制面中间件在请求进入路由前已按同一规则放行
或拒绝，这里在依赖里再校验一次，使不经中间件装配的应用（测试、嵌入式）同样拒绝无密钥调用；依赖先于请求体
校验执行，所以无密钥的非法请求也先得到 401。业务语义见 ``services/runtime/workflow/station_invocations.py``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import Field, field_validator, model_validator

from unilabos.config.station_key import station_key_accepted
from unilabos.protocol.runtime.workflow import normalize_json_object, validate_uuid
from unilabos.server.api.envelope import UNAUTHORIZED, WORKFLOW, ApiError, envelope_router
from unilabos.server.api.runtime.workflow import HashToken, WorkflowInventoryBindingRequest, _StrictModel, _success


class StationWorkflowInvocationRequest(_StrictModel):
    """V1：冻结发布修订的规范调用，或按工作流名称的兼容调用（两套身份不能混用）。"""

    task_uuid: Optional[str] = None
    workflow_id: Optional[str] = None
    revision_fingerprint: Optional[HashToken] = None
    normalized_input: Optional[Dict[str, Any]] = None
    deadline: Optional[str] = None
    backend_task_uuid: Optional[str] = None
    invocation_key: str
    workflow_name: Optional[str] = None
    input: Optional[Dict[str, Any]] = None
    priority: float = Field(default=1.0, allow_inf_nan=False)
    inventory_bindings: List[WorkflowInventoryBindingRequest] = Field(default_factory=list)
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_uuid", "backend_task_uuid", "workflow_id")
    @classmethod
    def _uuid(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_uuid(value)

    @field_validator("invocation_key", "workflow_name")
    @classmethod
    def _text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 256:
            raise ValueError("value must be 1..256 characters")
        return normalized

    @field_validator("input", "normalized_input", mode="before")
    @classmethod
    def _optional_object(cls, value: Any) -> Optional[Dict[str, Any]]:
        return None if value is None else normalize_json_object(value)

    @field_validator("meta_data", mode="before")
    @classmethod
    def _object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)

    @model_validator(mode="after")
    def _shape(self) -> "StationWorkflowInvocationRequest":
        canonical = any(value is not None for value in (
            self.task_uuid, self.workflow_id, self.revision_fingerprint, self.normalized_input, self.deadline,
        ))
        if canonical:
            if (self.task_uuid is None or self.workflow_id is None or self.revision_fingerprint is None
                    or self.normalized_input is None or self.backend_task_uuid is not None
                    or self.workflow_name is not None or self.input is not None):
                raise ValueError("规范工站调用身份、修订或输入不完整")
            return self
        if self.backend_task_uuid is None or self.workflow_name is None or self.input is None:
            raise ValueError("兼容工站调用身份、名称或输入不完整")
        return self


def require_station_key(request: Request) -> None:
    """V1：工站密钥未配置或不匹配一律 HTTP 401（信封 1002）。"""

    if not station_key_accepted(request.headers):
        raise ApiError(UNAUTHORIZED, "工站调用凭据无效", http_status=401,
                       headers={"WWW-Authenticate": 'Bearer realm="unilab-station"'})


def create_station_invocation_router(service: Any) -> APIRouter:
    router = envelope_router(profile=WORKFLOW, prefix="/api/v1", tags=["station"])

    @router.post("/station/workflow-invocations", summary="上游 Backend 提交工站工作流调用",
                 dependencies=[Depends(require_station_key)])
    def submit_station_workflow(body: StationWorkflowInvocationRequest) -> Any:
        """首次与幂等重放都返回同一任务（HTTP 201）；同调用键不同请求为 3003。"""

        from unilabos.server.services.runtime.workflow.station_invocations import (
            submit_station_workflow as submit,
        )

        task = submit(
            service,
            backend_task_uuid=body.task_uuid or body.backend_task_uuid or "",
            invocation_key=body.invocation_key,
            workflow_name=body.workflow_name,
            workflow_id=body.workflow_id,
            revision_fingerprint=body.revision_fingerprint,
            input_value=body.normalized_input if body.normalized_input is not None else (body.input or {}),
            priority=body.priority,
            deadline=body.deadline,
            inventory_bindings=[binding.model_dump() for binding in body.inventory_bindings],
            description=body.description,
            meta_data=body.meta_data,
        )
        return _success(task, status=201)

    return router


__all__ = ["StationWorkflowInvocationRequest", "create_station_invocation_router", "require_station_key"]
