"""V1 Workspace Host 协议（``/v1/snapshot``、``/v1/operations``、``/v1/logs/{component}``）。

V1 的 Workspace Host 是独立的工作区监督进程；统一分支里它对应 dev 已有的进程管理：
``unilab`` 调度权威进程（``app/backend_main.py``）就是监督者，直接看护 Host 子进程
（``app/supervisor.py`` 的 ``HostChildSupervisor``），受管 Slave / 设备进程由 Host 的
``services/device_processes.py`` 管理。这里只保留 V1 的线上合同：路径不带 ``/api`` 前缀、
响应是裸 JSON（不包信封）、失败用 HTTP 状态码 + ``{"error": {code, message, details?}}``。

组件名沿用 V1 的 ``backend`` / ``edge`` / ``plc`` / ``renderer``，dev 额外给每个受管设备进程
一个 ``managed:<process_id>`` 组件（与 ``/api/v1/hostlink/log-sources`` 的来源标识一致）。
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "unilab-workspace-host/v1"
#: V1 固定组件；dev 不运行 plc / renderer，快照里恒为 idle（不支持）。
V1_COMPONENT_NAMES = ("backend", "edge", "plc", "renderer")
UNSUPPORTED_COMPONENTS = frozenset({"plc", "renderer"})
MANAGED_COMPONENT_PREFIX = "managed:"
#: 受管进程 id 只允许安全字符：组件名会拼进 Host 日志来源标识，不能夹带路径。
MANAGED_PROCESS_ID = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9_.-]{1,128}$")

#: V1 ``GET /v1/logs/{component}`` 的缺省与上限。
DEFAULT_LOG_MAX_BYTES = 64 * 1024
MAX_LOG_MAX_BYTES = 4 * 1024 * 1024
#: V1 ``POST /v1/operations`` 请求体上限。
MAX_REQUEST_BYTES = 1024 * 1024
OPERATION_ID_MAX_LENGTH = 200

OPERATION_PHASES = ("pending", "running", "succeeded", "failed")
TERMINAL_OPERATION_PHASES = frozenset({"succeeded", "failed"})

#: dev 能执行的命令（V1 同名命令按 dev 进程模型重新解释，dev 独有命令另列）。
SUPPORTED_COMMANDS = (
    "workspace.start",
    "workspace.stop",
    "workspace.restart",
    "backend.start",
    "backend.stop",
    "backend.restart",
    "os.start",
    "os.stop",
    "os.restart",
    "scheduler.drain",
    "scheduler.resume",
    "device-process.start",
    "device-process.stop",
    "device-process.restart",
    "driver-package.install",
    "driver-package.uninstall",
    "driver-package.enable",
    "material.layout.inspect",
    "material.layout.preview",
    "material.layout.apply",
)
#: V1 有、dev 没有对应进程或能力的命令：照 V1 先受理，执行时失败（``command_unsupported``）。
UNSUPPORTED_V1_COMMANDS = {
    "local.reset-state": "dev 的全量重置走 POST /api/v1/reset（预览令牌 + 停机归档），不经工作区操作",
    "plc.start": "dev 不运行 PLC 仿真器",
    "plc.stop": "dev 不运行 PLC 仿真器",
    "plc.restart": "dev 不运行 PLC 仿真器",
    "renderer.attach": "dev 不托管渲染器（前端是独立静态站）",
    "renderer.detach": "dev 不托管渲染器（前端是独立静态站）",
    "renderer.headless.ensure": "dev 不托管渲染器（前端是独立静态站）",
    "renderer.headless.stop": "dev 不托管渲染器（前端是独立静态站）",
    "configuration.update": "dev 的启动参数来自 CLI / 配置文件，运行中不改",
    "authority.switch": "当前 OS 固定使用本机调度权威，不能切换",
    "release.publish": "dev 不向远端 Backend 发布数据",
    "release.inspect": "dev 不检查远端 Backend 发布目标",
    "material.template.validate": "dev 的物料模板由注册表权威在上报时校验",
}


class WorkspaceHostError(RuntimeError):
    """稳定的 Workspace Host 错误：``code`` 是 V1 符号码，``details`` 原样给调用方。"""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


def error_http_status(code: str) -> int:
    """V1 ``_error_status``：不存在 404、冲突 409，其余 400；dev 增加不可用 503。"""

    if code in {"operation_not_found", "host_not_found", "not_found"}:
        return 404
    if code in {"operation_conflict", "revision_conflict", "host_already_running"}:
        return 409
    if code == "unauthorized":
        return 401
    if code == "component_unavailable":
        return 503
    return 400


class WorkspaceOperationRequest(BaseModel):
    """``POST /v1/operations`` 请求体（V1 原样）。"""

    model_config = ConfigDict(extra="ignore")

    operationId: str = Field(
        min_length=1,
        max_length=OPERATION_ID_MAX_LENGTH,
        description="调用方生成的幂等键；同一 operationId 同一请求重放返回原记录，不同请求 409 operation_conflict",
    )
    command: str = Field(min_length=1, description="操作命令，例如 os.restart、material.layout.preview")
    parameters: dict[str, Any] = Field(default_factory=dict, description="命令参数")
    expectedRevision: Optional[int] = Field(
        default=None,
        description="可选：快照 revision 的比较并交换；不一致 409 revision_conflict",
    )


class WorkspaceComponent(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    phase: str
    pid: Optional[int] = None
    address: Optional[str] = None
    generation: Optional[str] = None
    logPath: Optional[str] = None
    diagnostic: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")

    schemaVersion: Literal["unilab-workspace-host/v1"] = SCHEMA_VERSION
    revision: int
    eventCursor: int
    workspacePath: str
    host: dict[str, Any]
    configuration: dict[str, Any]
    components: dict[str, WorkspaceComponent]
    updatedAt: str


class WorkspaceOperation(BaseModel):
    model_config = ConfigDict(extra="allow")

    schemaVersion: Literal["unilab-workspace-host/v1"] = SCHEMA_VERSION
    operationId: str
    command: str
    parameters: dict[str, Any]
    requestHash: str
    phase: Literal["pending", "running", "succeeded", "failed"]
    submittedAt: str
    startedAt: Optional[str] = None
    finishedAt: Optional[str] = None
    result: Any = None
    error: Optional[dict[str, Any]] = None


class WorkspaceLogTail(BaseModel):
    model_config = ConfigDict(extra="allow")

    component: str
    logPath: Optional[str] = None
    content: str


class WorkspaceHostErrorDetail(BaseModel):
    code: str
    message: str = ""
    details: Any = None


class WorkspaceHostErrorBody(BaseModel):
    error: WorkspaceHostErrorDetail


__all__ = [
    "DEFAULT_LOG_MAX_BYTES",
    "MANAGED_COMPONENT_PREFIX",
    "MANAGED_PROCESS_ID",
    "MAX_LOG_MAX_BYTES",
    "MAX_REQUEST_BYTES",
    "OPERATION_ID_MAX_LENGTH",
    "OPERATION_PHASES",
    "SCHEMA_VERSION",
    "SUPPORTED_COMMANDS",
    "TERMINAL_OPERATION_PHASES",
    "UNSUPPORTED_COMPONENTS",
    "UNSUPPORTED_V1_COMMANDS",
    "V1_COMPONENT_NAMES",
    "WorkspaceComponent",
    "WorkspaceHostError",
    "WorkspaceHostErrorBody",
    "WorkspaceHostErrorDetail",
    "WorkspaceLogTail",
    "WorkspaceOperation",
    "WorkspaceOperationRequest",
    "WorkspaceSnapshot",
]
