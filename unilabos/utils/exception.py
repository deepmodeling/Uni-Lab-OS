
class DeviceClassInvalid(Exception):
    pass


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
    ):
        self.device_id = device_id
        self.action_name = action_name
        self.remote_error = remote_error or ""
        self.rejected = rejected
        self.return_value = return_value
        detail = " (目标拒绝了请求)" if rejected else ""
        suffix = f": {self.remote_error}" if self.remote_error else ""
        super().__init__(f"调用设备动作 [{device_id}.{action_name}] 失败{detail}{suffix}")


class ActionResultError(RuntimeError):
    """设备未抛异常、但通过原生 Action 结果明确报告失败。"""
