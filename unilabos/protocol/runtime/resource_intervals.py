"""资源区间人工释放：版本校验和物理状态核实说明。"""
from typing import Literal
from unilabos.protocol.base import NonEmptyStr
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class ResourceIntervalReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: StrictInt = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("人工释放资源必须填写物理状态核实说明")
        return value.strip()


class WorkflowResourceScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1)
    kind: Literal["root", "with", "action"] = "with"
    resources: list[str] = Field(min_length=1)
    node_uuids: list[str] = Field(min_length=1)
    parent_scope_id: str | None = None

    @field_validator("resources", "node_uuids")
    @classmethod
    def distinct_nonblank(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("资源和节点引用必须非空且不重复")
        return values


class WorkflowResourceScopesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: StrictInt = Field(ge=1)
    scopes: list[WorkflowResourceScope]


class DeviceTenancyTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["task_while_loaded"] = "task_while_loaded"
    material_uuid: NonEmptyStr
    acquire_device_id: NonEmptyStr | None = None
    release_device_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_transition(self) -> "DeviceTenancyTransition":
        if not (self.acquire_device_id or self.release_device_id) or self.acquire_device_id == self.release_device_id:
            raise ValueError("设备托管必须指定不同的装载或卸载设备")
        return self


class DeviceTenancyRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_uuid: NonEmptyStr
    device_id: NonEmptyStr


class ResourceLoanRequest(ResourceIntervalReleaseRequest):
    loan_uuid: str = Field(min_length=1, max_length=200)
    borrower_task_uuid: str = Field(min_length=1, max_length=200)

    @field_validator("loan_uuid", "borrower_task_uuid")
    @classmethod
    def nonblank_identity(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("借用身份不能为空或带有首尾空格")
        return value
