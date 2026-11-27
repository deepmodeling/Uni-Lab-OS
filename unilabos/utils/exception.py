
class DeviceClassInvalid(Exception):
    pass


class TimeoutException(RuntimeError):
    """由动作运行时生成的统一超时异常。

    ``kind`` 只用于审计和前端展示；两类超时都进入同一套人工决策流程。
    当前运行时不会强杀已经下发给设备的同步调用，因而超时表示该次动作的
    编排结果已超出等待窗口，而不是对硬件状态作出额外断言。
    """

    category = "timeout"
    severity = "error"

    def __init__(self, message: str, *, kind: str = "hard", seconds: float | None = None):
        self.kind = kind
        self.seconds = seconds
        super().__init__(message)


class DeviceActionError(RuntimeError):
    """跨设备调用动作失败时抛出。

    把远端设备执行动作时产生的错误（被拒绝 / 执行失败 / 超时 / 结果无法解析）
    转换成本地异常，在调用方的执行流程中 raise 出来。

    Attributes:
        device_id: 远端设备 ID。
        action_name: 远端动作 / 函数名。
        remote_error: 远端返回的原始错误信息（通常是远端 traceback 字符串）。
        rejected: 目标是否被远端拒绝。
        return_value: 失败时远端附带的返回值（如有）。
    """

    def __init__(
        self,
        device_id: str,
        action_name: str,
        remote_error: str = "",
        *,
        rejected: bool = False,
        return_value=None,
        code: str = "failed",
        outcome_unknown: bool = False,
    ):
        self.device_id = device_id
        self.action_name = action_name
        self.remote_error = remote_error or ""
        self.rejected = rejected
        self.return_value = return_value
        self.code = "rejected" if rejected else code
        self.outcome_unknown = outcome_unknown
        detail = " (目标拒绝了请求)" if rejected else ""
        suffix = f": {self.remote_error}" if self.remote_error else ""
        super().__init__(f"调用设备动作 [{device_id}.{action_name}] 失败{detail}{suffix}")
