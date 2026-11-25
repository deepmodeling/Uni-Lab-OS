"""设备运行时异常：类解析失败、跨设备动作失败、Action 结果失败、动作超时。"""

from typing import Any, Dict, List, Optional


class DeviceClassInvalid(Exception):
    pass


class DeviceBackendUnsupported(DeviceClassInvalid):
    """设备类声明的 ``supported_backends`` 不含当前 backend（如只能跑在 ROS 2 上的 MoveIt 设备）。

    与注册表缺失、模板名非法等配置错误不同：设备本身没问题，只是不能在这个 backend 上运行。
    HostLink 启动时据此跳过该设备（其余设备照常启动）并上报不可用原因。
    """

    def __init__(
        self,
        device_id: str,
        *,
        backend_name: str,
        supported_backends: tuple[str, ...] | list[str],
        registry_name: str = "",
    ) -> None:
        self.device_id = str(device_id)
        self.backend_name = str(backend_name)
        self.supported_backends = [str(item) for item in supported_backends]
        self.registry_name = str(registry_name or "")
        super().__init__(
            f"Device [{self.device_id}] does not support backend {self.backend_name!r}; "
            f"supported: {', '.join(self.supported_backends)}"
        )


class DeviceMaintenanceError(DeviceClassInvalid):
    """这台设备起不来、进入维护模式：设备不启动（子设备随之跳过），Host 照常运行、其余设备照常启动。

    原因经 :meth:`unavailable_record` 登记为不可用设备，HostLink 与 ROS 2 同一份记录，经能力快照到达
    ``GET /api/v1/devices``，并出现在 ``/api/v1/health`` 里。注册表缺失、模板名非法等图配置错误不属于
    这一类，仍是 :class:`DeviceClassInvalid`。
    """

    device_id: str = ""

    def unavailable_record(self) -> Dict[str, Any]:
        return {"maintenance": True, "reason": str(self)}


class DeviceStartupError(DeviceMaintenanceError):
    """设备装配、构造或初始化失败：权威里的设备状态装配不起来（如 Site 与注册表对不上）、驱动导入失败、
    构造器或 ``post_init`` / ``setup`` / ``initialize`` 抛错。``stage`` 是 ``definition`` / ``construct`` /
    ``initialize`` 之一。
    """

    STAGES = {"definition": "装配", "construct": "构造", "initialize": "初始化"}

    def __init__(self, *, stage: str, error: BaseException, device_id: str = "") -> None:
        if stage not in self.STAGES:
            raise ValueError(f"未知的设备启动阶段: {stage!r}")
        self.stage = stage
        self.error_type = type(error).__name__
        self.detail = str(error)
        self.device_id = str(device_id or "")
        super().__init__(self.detail)
        # 原始异常（带 traceback）留给日志
        self.__cause__ = error

    def __str__(self) -> str:
        device = f"设备 {self.device_id} " if self.device_id else "设备"
        return f"{device}{self.STAGES[self.stage]}失败，设备进入维护模式：{self.error_type}: {self.detail}"

    def unavailable_record(self) -> Dict[str, Any]:
        return {**super().unavailable_record(), "stage": self.stage, "error_type": self.error_type}


class DeviceMaterialError(DeviceMaintenanceError):
    """设备持有的物料建不成 PLR 实例（缺 / 写错 ``config.type``、类不存在、反序列化失败）。

    设备进入维护模式：这台设备不启动，Host 照常运行、其余设备照常启动；原因（设备、物料 uuid、
    ``config.type``）登记为不可用设备，经能力快照到达 ``GET /api/v1/devices``。构造器不知道设备 id，
    由启动设备的一方补上 ``device_id``。
    """

    def __init__(self, *, material_uuid: Any, material_type: Any, reason: str, device_id: str = "") -> None:
        self.material_uuid = str(material_uuid or "")
        self.material_type = material_type
        self.reason = str(reason)
        self.device_id = str(device_id or "")
        super().__init__(self.reason)

    def __str__(self) -> str:
        device = f"设备 {self.device_id} " if self.device_id else "设备"
        return (
            f"{device}持有的物料 {self.material_uuid}（config.type={self.material_type!r}）"
            f"建不成 PLR 实例，设备进入维护模式：{self.reason}"
        )

    def unavailable_record(self) -> Dict[str, Any]:
        """不可用设备记录里的维护模式字段（与 backend 不支持等原因同一份记录）。"""

        return {
            **super().unavailable_record(),
            "material_uuid": self.material_uuid,
            "material_type": (
                self.material_type
                if self.material_type is None or isinstance(self.material_type, str)
                else repr(self.material_type)
            ),
        }


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
