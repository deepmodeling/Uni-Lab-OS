"""设备运行时异常：类解析失败、跨设备动作失败、Action 结果失败、动作超时。"""

from typing import Any, Dict, List, Optional


class DeviceClassInvalid(Exception):
    pass


class ActionTimeoutBase(RuntimeError):
    """``@action`` 超时闸门触发时由执行面构造的异常基类。

    子类通过 ``category`` / ``severity`` 与 ``unilabos.utils.exception`` 异常族同形，
    :meth:`to_error_info` 产出的字典与 HostLink ``exception_error_info`` 一致，
    可直接进入错误决策链（``error_policy.options`` 按 ``exception_type`` 匹配）。
    """

    category = "timeout"
    severity = "error"

    def __init__(
        self,
        action_name: str,
        timeout_seconds: float,
        *,
        device_id: str = "",
        elapsed_seconds: Optional[float] = None,
        message: str = "",
        driver_still_running: bool = False,
    ) -> None:
        self.action_name = action_name
        self.device_id = device_id
        self.timeout_seconds = float(timeout_seconds)
        self.elapsed_seconds = elapsed_seconds
        # 同步驱动的线程杀不掉：超时报出时它仍在执行，执行面据此继续持有锁（ADR 0008）
        self.driver_still_running = bool(driver_still_running)
        super().__init__(message or self._default_message())

    def _default_message(self) -> str:
        target = f"{self.device_id}.{self.action_name}" if self.device_id else self.action_name
        return f"动作 {target} 执行超时 (>{self.timeout_seconds:g}s)"

    def to_error_info(self) -> Dict[str, Any]:
        """错误决策报告使用的结构化异常身份。"""

        mro: List[str] = [
            klass.__name__
            for klass in type(self).__mro__
            if klass not in (object, BaseException)
        ]
        info: Dict[str, Any] = {
            "action_name": self.action_name,
            "exception_type": type(self).__name__,
            "exception_mro": mro,
            "error_message": str(self),
            "traceback": "",
            "category": self.category,
            "severity": self.severity,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.elapsed_seconds is not None:
            info["elapsed_seconds"] = float(self.elapsed_seconds)
        if self.driver_still_running:
            info["driver_still_running"] = True
        return info


class TimeoutException(ActionTimeoutBase):
    """``@action(timeout=...)`` 硬超时：执行面已对动作发起协作式取消，attempt 以失败进入决策链。"""

    severity = "error"

    def _default_message(self) -> str:
        return super()._default_message() + "，已请求取消该动作"


class ExecutionTimeoutException(ActionTimeoutBase):
    """``@action(execution_timeout=...)`` 业务软超时：动作仍在执行，等待操作员决定继续等待或终止。"""

    severity = "warning"

    def _default_message(self) -> str:
        return super()._default_message() + "，动作仍在执行中，请选择继续等待或终止"


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
