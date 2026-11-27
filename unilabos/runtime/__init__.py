"""领域包公共运行合同；导入时不加载 ROS、库存数据库或设备。"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Protocol

from unilabos.runtime.commands import CommandJournal, LegacyCommandTables


def current_execution_identity() -> Mapping[str, str]:
    """读取 OS 当前执行上下文的只读快照；非工作流调用返回空映射。

    身份由 OS 调度入口建立，不能从动作业务参数补造。命令幂等键的派生
    仍由领域协议负责，已有 node_job_uuid/task_uuid 字段保持不变。
    """
    from unilabos.utils.tracing import capture_workflow_execution_identity

    identity = capture_workflow_execution_identity()
    return MappingProxyType(identity if identity.get("node_job_uuid") else {})


class DeviceInvocationError(RuntimeError):
    """依赖调用失败。超时只结束等待，不能据此认定物理操作已停止。"""

    def __init__(self, device_id: str, operation: str, code: str, detail: str = "",
                 *, outcome_unknown: bool = False) -> None:
        self.device_id = device_id
        self.operation = operation
        self.code = code
        self.outcome_unknown = outcome_unknown
        super().__init__(f"依赖设备调用失败 [{device_id}.{operation}] {code}: {detail}")


class DeviceRuntimeContext(Protocol):
    """OS 通过驱动的 bind_runtime(context) 注入；驱动不持有 HostNode。"""

    @property
    def device_id(self) -> str: ...

    @property
    def device_uuid(self) -> str: ...

    def open_command_journal(
        self, name: str, path: str, *, legacy_tables: LegacyCommandTables | None = None,
    ) -> CommandJournal:
        """按稳定设备 UUID 隔离命令日志；路径来自部署配置。"""
        ...

    def invoke(
        self, device_id: str, operation: str, arguments: Mapping[str, Any] | None = None,
        *, timeout: float | None = 300.0, server_wait_timeout: float = 10.0,
    ) -> Any:
        """调用依赖设备，返回 Python 结果或抛出 DeviceInvocationError。

        timeout 是含服务等待的总等待预算，None 表示不限时；不自动重试。
        初始化期仅允许已初始化的本地依赖，缺失时立即返回 not_ready。
        销毁后返回 closed。业务参数不承担认证身份传输。
        """
        ...
