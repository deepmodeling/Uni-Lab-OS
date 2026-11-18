"""虚拟离心机：按转速、温度离心指定时长。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

from unilabos.backend.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import (
    VesselRef,
    VirtualDevice,
    nested_settings,
    parse_duration,
)
from unilabos.registry.decorators import DataSource, InputHandle, Side, action, device, topic_config

#: 离心机能稳定运行的最低转速（RPM）
MIN_SPEED = 100.0
AMBIENT_TEMPERATURE = 25.0


@device(
    id="virtual_centrifuge",
    display_name="虚拟离心机",
    category=["virtual_device"],
    description="按转速与温度离心的虚拟离心机，供离心协议与示例图使用",
    supported_backends=["hostlink", "ros2"],
    handles=[
        InputHandle(
            key="centrifuge",
            data_type="transport",
            label="centrifuge",
            side=Side.NORTH,
            data_key="vessel",
            data_source=DataSource.HANDLE,
            description="需要离心的样品容器",
        ),
    ],
)
class VirtualCentrifuge(VirtualDevice):
    """转速 100 ~ max_speed（RPM），温度 min_temp ~ max_temp（°C）。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        max_speed: float = 15000.0,
        max_temp: float = 40.0,
        min_temp: float = 4.0,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟离心机。

        Args:
            device_id[设备ID]: 设备实例 ID。
            max_speed[最高转速]: 最高转速（RPM）。
            max_temp[最高温度]: 最高温度（°C）。
            min_temp[最低温度]: 最低温度（°C）。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用设备类的默认值（0.001）。
        """
        settings = nested_settings(
            kwargs, max_speed=max_speed, max_temp=max_temp, min_temp=min_temp, port=port, time_scale=time_scale
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_centrifuge", settings["time_scale"])
        self._max_speed = float(settings["max_speed"])
        self._max_temp = float(settings["max_temp"])
        self._min_temp = float(settings["min_temp"])
        self.port = str(settings["port"])
        self._centrifuge_state = "Ready"
        self._current_speed = 0.0
        self._target_speed = 0.0
        self._current_temp = AMBIENT_TEMPERATURE
        self._target_temp = AMBIENT_TEMPERATURE

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Running / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def centrifuge_state(self) -> str:
        """Ready / Running / Completed / Stopped / Error。"""
        return self._centrifuge_state

    @property
    @topic_config(period=1.0)
    def current_speed(self) -> float:
        """当前转速（RPM）。"""
        return self._current_speed

    @property
    @topic_config(period=1.0)
    def target_speed(self) -> float:
        """目标转速（RPM）。"""
        return self._target_speed

    @property
    @topic_config(period=1.0)
    def current_temp(self) -> float:
        """当前温度（°C）。"""
        return self._current_temp

    @property
    @topic_config(period=1.0)
    def target_temp(self) -> float:
        """目标温度（°C）。"""
        return self._target_temp

    @property
    @topic_config(period=1.0)
    def time_remaining(self) -> float:
        """剩余模拟时间（s）。"""
        return self._remaining_time

    @property
    @topic_config(period=1.0)
    def progress(self) -> float:
        """当前离心进度（%）。"""
        return self._progress

    @property
    @topic_config(period=1.0)
    def message(self) -> str:
        """最近一次操作的说明。"""
        return self._message

    @property
    @topic_config(period=5.0)
    def max_speed(self) -> float:
        """最高转速（RPM）。"""
        return self._max_speed

    @property
    @topic_config(period=5.0)
    def max_temp(self) -> float:
        """最高温度（°C）。"""
        return self._max_temp

    @property
    @topic_config(period=5.0)
    def min_temp(self) -> float:
        """最低温度（°C）。"""
        return self._min_temp

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="停转并回到就绪")
    async def initialize(self) -> bool:
        """初始化：停转、回到就绪。"""
        self._halt("Ready")
        return True

    @action(auto_prefix=True, display_name="清理", description="停止正在进行的离心并离线")
    async def cleanup(self) -> bool:
        """清理：请求停止当前离心。"""
        self._request_stop()
        self._halt("Offline")
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:Centrifuge",
        display_name="离心",
        description="按转速与温度离心 time 秒",
        feedback_interval=0.5,
    )
    async def centrifuge(
        self,
        vessel: VesselRef,
        speed: float,
        time: Union[float, str],
        temp: float = 25.0,
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """离心。

        Args:
            vessel[容器]: 被离心的容器。
            speed[转速]: 离心转速（RPM），100 ~ 最高转速。
            time[时长]: 离心时长，数字按秒，也可写 "5 min"。
            temp[温度]: 离心温度（°C）。
        """
        rpm = float(speed)
        if not MIN_SPEED <= rpm <= self._max_speed:
            self._reject(f"离心速度 {rpm:g} RPM 超出范围 {MIN_SPEED:g} ~ {self._max_speed:g} RPM")
        temperature = float(temp)
        if not self._min_temp <= temperature <= self._max_temp:
            self._reject(f"温度 {temperature:g}°C 超出范围 {self._min_temp:g} ~ {self._max_temp:g}°C")
        duration = parse_duration(time)
        if duration <= 0:
            self._reject(f"离心时长必须大于 0，收到 {time!r}")
        label = self._vessel_label(vessel)
        with self._operation("Running", f"离心 {label or '容器'} {rpm:g} RPM × {duration:g}s，{temperature:g}°C", action_context):
            self._centrifuge_state = "Running"
            self._target_speed = rpm
            self._current_speed = rpm
            self._target_temp = temperature
            self._current_temp = temperature
            completed = False
            try:
                await self._simulate(duration, action_context, feedback={"current_speed": rpm, "current_temp": temperature})
                completed = True
            finally:
                self._current_speed = 0.0
                self._target_speed = 0.0
                self._centrifuge_state = "Completed" if completed else "Stopped"
        self._message = f"离心完成：{label or '容器'} {rpm:g} RPM × {duration:g}s"
        return {"success": True, "vessel": label, "speed": rpm, "time": duration, "temp": temperature}

    # ============ 内部 ============

    def _halt(self, state: str) -> None:
        with self._lock:
            self._status = "Idle"
            self._centrifuge_state = state
            self._current_speed = 0.0
            self._target_speed = 0.0
            self._current_temp = AMBIENT_TEMPERATURE
            self._target_temp = AMBIENT_TEMPERATURE
            self._remaining_time = 0.0

    def __repr__(self) -> str:
        return f"VirtualCentrifuge({self.device_id}: {self._centrifuge_state}, {self._current_speed:g} RPM)"
