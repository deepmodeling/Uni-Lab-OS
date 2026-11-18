"""虚拟搅拌器：定时搅拌 + 沉降（``stir``），或持续搅拌（``start_stir`` / ``stop_stir``）。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

from unilabos.backend.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import (
    VesselRef,
    VirtualDevice,
    nested_settings,
    parse_duration,
)
from unilabos.registry.decorators import DataSource, OutputHandle, Side, action, device, topic_config


@device(
    id="virtual_stirrer",
    display_name="虚拟搅拌器",
    category=["virtual_device"],
    description="定时搅拌 / 持续搅拌的虚拟搅拌器，供搅拌、加料、溶解等协议与示例图使用",
    icon="Stirrer.webp",
    supported_backends=["hostlink", "ros2"],
    handles=[
        OutputHandle(
            key="stirrer",
            data_type="mechanical",
            label="stirrer",
            side=Side.NORTH,
            data_key="vessel",
            data_source=DataSource.HANDLE,
            description="搅拌器的机械连接口",
        ),
    ],
)
class VirtualStirrer(VirtualDevice):
    """转速范围 min_speed ~ max_speed（RPM）。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        max_speed: float = 1500.0,
        min_speed: float = 50.0,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟搅拌器。

        Args:
            device_id[设备ID]: 设备实例 ID。
            max_speed[最高转速]: 最高转速（RPM）。
            min_speed[最低转速]: 最低转速（RPM）。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用设备类的默认值（0.001）。
        """
        settings = nested_settings(
            kwargs, max_speed=max_speed, min_speed=min_speed, port=port, time_scale=time_scale
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_stirrer", settings["time_scale"])
        self._max_speed = float(settings["max_speed"])
        self._min_speed = float(settings["min_speed"])
        if self._min_speed > self._max_speed:
            raise ValueError(f"min_speed {self._min_speed} 不能大于 max_speed {self._max_speed}")
        self.port = str(settings["port"])
        self._operation_mode = "Idle"
        self._current_vessel = ""
        self._current_speed = 0.0

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Stirring / Settling / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def operation_mode(self) -> str:
        """Idle / Stirring / Settling / Completed / Stopped / Error。"""
        return self._operation_mode

    @property
    @topic_config(period=1.0)
    def current_vessel(self) -> str:
        """正在搅拌的容器（显示名）。"""
        return self._current_vessel

    @property
    @topic_config(period=1.0)
    def current_speed(self) -> float:
        """当前转速（RPM）。"""
        return self._current_speed

    @property
    @topic_config(period=1.0)
    def is_stirring(self) -> bool:
        """是否正在搅拌。"""
        return self._current_speed > 0

    @property
    @topic_config(period=1.0)
    def remaining_time(self) -> float:
        """剩余模拟时间（s）；持续搅拌时为 -1。"""
        if self._operation_mode == "Stirring" and self._status == "Idle" and self._current_speed > 0:
            return -1.0
        return self._remaining_time

    @property
    @topic_config(period=5.0)
    def max_speed(self) -> float:
        """最高转速（RPM）。"""
        return self._max_speed

    @property
    @topic_config(period=5.0)
    def min_speed(self) -> float:
        """最低转速（RPM）。"""
        return self._min_speed

    @property
    @topic_config(period=5.0)
    def device_info(self) -> Dict[str, Any]:
        """状态快照。"""
        return {
            "device_id": self.device_id,
            "status": self._status,
            "operation_mode": self._operation_mode,
            "current_vessel": self._current_vessel,
            "current_speed": self._current_speed,
            "is_stirring": self.is_stirring,
            "remaining_time": self.remaining_time,
            "max_speed": self._max_speed,
            "min_speed": self._min_speed,
            "message": self._message,
        }

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="停止搅拌并回到待机")
    async def initialize(self) -> bool:
        """初始化：停止搅拌。"""
        self._set_idle("Idle")
        return True

    @action(auto_prefix=True, display_name="清理", description="停止正在进行的搅拌并离线")
    async def cleanup(self) -> bool:
        """清理：请求停止当前搅拌。"""
        self._request_stop()
        self._set_idle("Offline")
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:Stir",
        display_name="定时搅拌",
        description="按转速搅拌 stir_time 秒，再停止搅拌沉降 settling_time 秒",
        feedback_interval=0.5,
    )
    async def stir(
        self,
        stir_time: float,
        stir_speed: float,
        settling_time: Union[float, str],
        vessel: VesselRef = "",
        time: str = "",
        time_spec: str = "",
        event: str = "",
        purpose: str = "",
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """定时搅拌后沉降。

        Args:
            stir_time[搅拌时间]: 搅拌时长（s）；为 0 时取 time。
            stir_speed[转速]: 搅拌转速（RPM）。
            settling_time[沉降时间]: 停止搅拌后的沉降时长，数字按秒，也可写 "5 min"。
            vessel[容器]: 被搅拌的容器，可不填。
            time[时间描述]: 带单位的时长（如 "5 min"），stir_time 为 0 时使用。
            time_spec[时间规格]: 协议里的时间规格，仅记录。
            event[事件]: 协议事件，仅记录。
            purpose[目的]: 操作目的，仅记录。
        """
        stir_seconds = float(stir_time) or parse_duration(time)
        settle_seconds = parse_duration(settling_time)
        if stir_seconds < 0 or settle_seconds < 0:
            self._reject(f"搅拌 / 沉降时间不能为负：stir_time={stir_seconds}, settling_time={settle_seconds}")
        speed = self._checked_speed(stir_speed)
        label = self._vessel_label(vessel)
        total = stir_seconds + settle_seconds
        stir_share = 100.0 * stir_seconds / total if total > 0 else 100.0
        with self._operation("Stirring", f"搅拌 {label or '容器'} {speed:g} RPM × {stir_seconds:g}s", action_context):
            self._current_vessel = label
            self._operation_mode = "Stirring"
            self._current_speed = speed
            completed = False
            try:
                await self._simulate(
                    stir_seconds,
                    action_context,
                    progress_span=(0.0, stir_share),
                    remaining_after=settle_seconds,
                    feedback={"stir_speed": speed},
                )
                self._current_speed = 0.0
                if settle_seconds > 0:
                    self._status = "Settling"
                    self._operation_mode = "Settling"
                    await self._simulate(settle_seconds, action_context, progress_span=(stir_share, 100.0))
                completed = True
            finally:
                self._current_speed = 0.0
                self._operation_mode = "Completed" if completed else "Stopped"
        self._message = f"搅拌完成：{speed:g} RPM × {stir_seconds:g}s，沉降 {settle_seconds:g}s"
        return {
            "success": True,
            "vessel": label,
            "stir_speed": speed,
            "stir_time": stir_seconds,
            "settling_time": settle_seconds,
        }

    @action(
        action_type="unilabos_msgs.action:StartStir",
        display_name="开始搅拌",
        description="按转速持续搅拌，直到 stop_stir",
    )
    async def start_stir(self, vessel: VesselRef, stir_speed: float, purpose: str = "") -> Dict[str, Any]:
        """开始持续搅拌。

        Args:
            vessel[容器]: 被搅拌的容器。
            stir_speed[转速]: 搅拌转速（RPM）。
            purpose[目的]: 操作目的，仅记录。
        """
        speed = self._checked_speed(stir_speed)
        label = self._vessel_label(vessel)
        with self._lock:
            self._status = "Idle"
            self._operation_mode = "Stirring"
            self._current_vessel = label
            self._current_speed = speed
            self._remaining_time = 0.0
            self._message = f"持续搅拌 {label or '容器'} {speed:g} RPM" + (f"（{purpose}）" if purpose else "")
        return {"success": True, "vessel": label, "stir_speed": speed}

    @action(
        action_type="unilabos_msgs.action:StopStir",
        display_name="停止搅拌",
        description="停止持续搅拌",
    )
    async def stop_stir(self, vessel: VesselRef, purpose: str = "") -> Dict[str, Any]:
        """停止搅拌。

        Args:
            vessel[容器]: 被搅拌的容器。
            purpose[目的]: 操作目的，仅记录。
        """
        label = self._vessel_label(vessel) or self._current_vessel
        previous = self._current_speed
        self._set_idle("Stopped")
        self._message = f"已停止搅拌 {label or '容器'}（之前 {previous:g} RPM）"
        return {"success": True, "vessel": label, "previous_speed": previous}

    # ============ 内部 ============

    def _checked_speed(self, stir_speed: Any) -> float:
        speed = float(stir_speed)
        if not self._min_speed <= speed <= self._max_speed:
            self._reject(f"搅拌速度 {speed:g} RPM 超出范围 {self._min_speed:g} ~ {self._max_speed:g} RPM")
        return speed

    def _set_idle(self, mode: str) -> None:
        with self._lock:
            self._status = "Idle"
            self._operation_mode = mode
            self._current_vessel = ""
            self._current_speed = 0.0
            self._remaining_time = 0.0

    def __repr__(self) -> str:
        return f"VirtualStirrer({self.device_id}: {self._operation_mode}, {self._current_speed:g} RPM)"
