"""Backend / runtime 生命周期管理协议，复用统一的 /api/v1 入口。

Host 是一个 backend 对应的主 runtime；slave 属于该 runtime 的设备执行面。
这些 DTO 不负责进程发现、启动、文件路径或设备命令投递。
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Self

from pydantic import BaseModel, Field, model_validator


class HealthStatus(BaseModel):
    """``GET /api/v1/health``（V1 字段）：``scheduler`` 为 V1 的 ``ready | disabled``。

    dev 原来的本机 / 远端调度归属改名为 ``scheduler_mode``，执行面状态 ``execution`` 保留。
    """

    status: Literal["ok"]
    scheduler: Literal["ready", "disabled"]
    scheduler_mode: Literal["local", "remote"]
    execution: Literal["ready", "restarting", "disabled"]
    #: dev 扩展，只在非空时出现：本进程设备 runtime 跳过的设备（设备图里有、当前 backend 跑不了）
    #: （``device_id`` / ``registry_name`` / ``backend`` / ``reason`` / ``supported_backends``）；
    #: 两进程拓扑下 Host 子进程跳过的设备见 ``/api/v1/hostlink/peers``
    unavailable_devices: list[dict[str, Any]] = Field(default_factory=list)


class RuntimeCapabilities(BaseModel):
    """V1 Runtime 能力投影（``/readiness.capabilities``）；本地默认全开放。"""

    can_edit_definitions: bool = True
    can_view_drafts: bool = True
    can_run_unpublished: bool = True
    can_debug: bool = True
    can_execute_device_actions: bool = True


class WorkflowProgress(BaseModel):
    loaded: int = Field(ge=0)
    total: int = Field(ge=0)


class ReadinessError(BaseModel):
    code: str
    message: str


class ReadinessStatus(BaseModel):
    """``GET /api/v1/readiness``（V1 字段 + dev 探针摘要）：就绪 HTTP 200，否则 503。

    ``status``：``ready`` 可派发；``starting`` 执行面启动 / 重启中或派发暂停；``failed`` 启动失败。
    ``scheduler`` / ``execution`` / ``reasons`` 是 dev 的探针摘要（V1 没有，作为扩展字段保留）。
    """

    status: Literal["ready", "starting", "failed"]
    capabilities: RuntimeCapabilities = Field(default_factory=RuntimeCapabilities)
    phase: str
    workflowRuntime: str
    workflowProgress: Optional[WorkflowProgress] = None
    observability: Optional[dict] = None
    error: Optional[ReadinessError] = None
    scheduler: Literal["local", "remote"]
    execution: Literal["ready", "starting", "restarting", "disabled"]
    reasons: list[str]


class RestartRequest(BaseModel):
    """auto 在调度权威上解析为 edge；process 需要外部父进程监督。"""

    mode: Literal["quiescent", "immediate"] = "quiescent"
    scope: Literal["auto", "edge", "process"] = "auto"


class RestartStatus(BaseModel):
    pending: bool
    mode: Literal["quiescent", "immediate"]
    scope: Literal["auto", "edge", "process"]
    effective_scope: Literal["edge", "process"]
    requested_at: float | None
    restarting: bool
    safe_restart: bool
    active_jobs: list[str]
    dispatch_paused: bool


class ResetRequest(BaseModel):
    confirmation_token: str = Field(min_length=1, max_length=128)
    confirmation: str = Field(min_length=1, max_length=64)


class ResetStatus(BaseModel):
    supported: bool
    pending: bool
    confirmation_token: str
    backup_path: str
    detail: str


class SchedulerDrainStatus(BaseModel):
    """保留 V1 排空合同的三个必需字段，补充各暂停来源的可观测状态。

    drained 表示已阻止新派发且设备在途作业已收敛，不代表工作流全部完成。
    requested 只描述显式 drain；恢复 drain 后仍可能被 restart/reset 暂停。
    """

    phase: Literal["running", "draining", "drained"]
    active_device_job_count: int = Field(ge=0, strict=True)
    active_device_job_ids: list[str]
    requested: bool
    dispatch_paused: bool
    pause_reasons: list[str]

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        jobs = self.active_device_job_ids
        if self.active_device_job_count != len(jobs) or len(set(jobs)) != len(jobs):
            raise ValueError("排空作业数量必须与去重后的 ID 列表一致")
        expected = "running" if not self.dispatch_paused else ("draining" if jobs else "drained")
        if self.phase != expected:
            raise ValueError("排空阶段必须与派发闸门及在途作业一致")
        if self.dispatch_paused != bool(self.pause_reasons) or self.requested != ("drain" in self.pause_reasons):
            raise ValueError("排空请求必须与暂停来源一致")
        return self
