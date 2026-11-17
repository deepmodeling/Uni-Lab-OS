"""虚拟加热 / 冷却器：定时控温（``heat_chill``）或持续控温（``heat_chill_start`` / ``heat_chill_stop``）。

定时控温先按 ``ramp_rate`` 线性升 / 降温，再保持到总时长；持续控温直接到达目标温度。
"""

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

#: 室温（°C）：冷却 / 加热的分界与停止后的回落温度
AMBIENT_TEMPERATURE = 25.0


@device(
    id="virtual_heatchill",
    display_name="虚拟加热冷却器",
    category=["virtual_device"],
    description="定时 / 持续控温的虚拟加热冷却器（可带搅拌），供控温、溶解、干燥等协议与示例图使用",
    icon="Heater.webp",
    supported_backends=["hostlink", "ros2"],
    handles=[
        OutputHandle(
            key="heatchill",
            data_type="mechanical",
            label="heatchill",
            side=Side.NORTH,
            data_key="vessel",
            data_source=DataSource.HANDLE,
            description="加热/冷却器的物理连接口",
        ),
    ],
)
class VirtualHeatChill(VirtualDevice):
    """温度范围 min_temp ~ max_temp（°C），可带搅拌（≤ max_stir_speed）。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        max_temp: float = 200.0,
        min_temp: float = -80.0,
        max_stir_speed: float = 1000.0,
        ramp_rate: float = 1.0,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟加热冷却器。

        Args:
            device_id[设备ID]: 设备实例 ID。
            max_temp[最高温度]: 最高温度（°C）。
            min_temp[最低温度]: 最低温度（°C）。
            max_stir_speed[最高搅拌速度]: 最高搅拌速度（RPM）。
            ramp_rate[升降温速率]: 定时控温时的升 / 降温速率（°C/s）。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用全局 virtual_time_scale。
        """
        settings = nested_settings(
            kwargs,
            max_temp=max_temp,
            min_temp=min_temp,
            max_stir_speed=max_stir_speed,
            ramp_rate=ramp_rate,
            port=port,
            time_scale=time_scale,
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_heatchill", settings["time_scale"])
        self._max_temp = float(settings["max_temp"])
        self._min_temp = float(settings["min_temp"])
        if self._min_temp > self._max_temp:
            raise ValueError(f"min_temp {self._min_temp} 不能大于 max_temp {self._max_temp}")
        self._max_stir_speed = float(settings["max_stir_speed"])
        self.ramp_rate = float(settings["ramp_rate"])
        if self.ramp_rate <= 0:
            raise ValueError(f"ramp_rate 必须大于 0，收到 {self.ramp_rate}")
        self.port = str(settings["port"])
        self._operation_mode = "Idle"
        self._current_temp = AMBIENT_TEMPERATURE
        self._target_temp = AMBIENT_TEMPERATURE
        self._stir_speed = 0.0
        self._continuous = False

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Heating / Cooling / Maintaining / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def operation_mode(self) -> str:
        """Idle / Heating / Cooling / Maintaining / Completed / Stopped / Error。"""
        return self._operation_mode

    @property
    @topic_config(period=1.0)
    def current_temp(self) -> float:
        """当前温度（°C）。"""
        return round(self._current_temp, 3)

    @property
    @topic_config(period=1.0)
    def target_temp(self) -> float:
        """目标温度（°C）。"""
        return round(self._target_temp, 3)

    @property
    @topic_config(period=1.0)
    def is_stirring(self) -> bool:
        """是否在搅拌。"""
        return self._stir_speed > 0

    @property
    @topic_config(period=1.0)
    def stir_speed(self) -> float:
        """当前搅拌速度（RPM）。"""
        return self._stir_speed

    @property
    @topic_config(period=1.0)
    def remaining_time(self) -> float:
        """剩余模拟时间（s）；持续控温时为 -1。"""
        return -1.0 if self._continuous else self._remaining_time

    @property
    @topic_config(period=1.0)
    def progress(self) -> float:
        """当前动作进度（%）。"""
        return self._progress

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

    @property
    @topic_config(period=5.0)
    def max_stir_speed(self) -> float:
        """最高搅拌速度（RPM）。"""
        return self._max_stir_speed

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="停止控温并回到室温")
    async def initialize(self) -> bool:
        """初始化：回到室温待机。"""
        self._stop("Idle")
        return True

    @action(auto_prefix=True, display_name="清理", description="停止正在进行的控温并离线")
    async def cleanup(self) -> bool:
        """清理：请求停止当前控温。"""
        self._request_stop()
        self._stop("Offline")
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:HeatChill",
        display_name="定时控温",
        description="升 / 降温到目标温度并保持，总时长 time（可带单位），可同时搅拌",
        feedback_interval=0.5,
    )
    async def heat_chill(
        self,
        temp: float,
        time: Union[float, str],
        stir: bool = False,
        stir_speed: float = 0.0,
        purpose: str = "",
        vessel: VesselRef = "",
        pressure: str = "",
        reflux_solvent: str = "",
        temp_spec: str = "",
        time_spec: str = "",
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """定时控温。

        Args:
            temp[目标温度]: 目标温度（°C）。
            time[时长]: 总时长，数字按秒，也可写 "30 min"。
            stir[搅拌]: 是否同时搅拌。
            stir_speed[搅拌速度]: 搅拌速度（RPM），stir 为真时生效。
            purpose[目的]: 操作目的，仅记录。
            vessel[容器]: 被控温的容器，可不填。
            pressure[压力]: 协议给的压力描述，仅记录。
            reflux_solvent[回流溶剂]: 回流溶剂，仅记录。
            temp_spec[温度规格]: 协议里的温度规格，仅记录。
            time_spec[时间规格]: 协议里的时间规格，仅记录。
        """
        target = self._checked_temp(temp)
        duration = parse_duration(time)
        if duration <= 0:
            self._reject(f"控温时长必须大于 0，收到 {time!r}")
        speed = float(stir_speed) if stir else 0.0
        if speed < 0 or speed > self._max_stir_speed:
            self._reject(f"搅拌速度 {speed:g} RPM 超出范围 0 ~ {self._max_stir_speed:g} RPM")
        label = self._vessel_label(vessel)
        mode = self._mode_for(target)
        start = self._current_temp
        ramp_seconds = min(duration, abs(target - start) / self.ramp_rate)
        ramp_share = 100.0 * ramp_seconds / duration

        def ramp(fraction: float) -> None:
            self._current_temp = start + (target - start) * fraction

        self._continuous = False
        with self._operation(mode, f"{label or '容器'} 控温到 {target:g}°C，{duration:g}s", action_context):
            self._operation_mode = mode
            self._target_temp = target
            self._stir_speed = speed
            completed = False
            try:
                await self._simulate(
                    ramp_seconds,
                    action_context,
                    progress_span=(0.0, ramp_share),
                    remaining_after=duration - ramp_seconds,
                    feedback={"target_temp": target},
                    on_step=ramp,
                )
                self._current_temp = target
                await self._simulate(
                    duration - ramp_seconds,
                    action_context,
                    progress_span=(ramp_share, 100.0),
                    feedback={"target_temp": target},
                )
                completed = True
            finally:
                self._stir_speed = 0.0
                self._operation_mode = "Completed" if completed else "Stopped"
        self._message = f"控温完成：{target:g}°C × {duration:g}s" + (f"，搅拌 {speed:g} RPM" if speed else "")
        return {
            "success": True,
            "vessel": label,
            "temp": target,
            "time": duration,
            "stir_speed": speed,
            "current_temp": round(self._current_temp, 3),
        }

    @action(
        action_type="unilabos_msgs.action:HeatChillStart",
        display_name="开始控温",
        description="持续控温到目标温度，直到 heat_chill_stop",
    )
    async def heat_chill_start(self, temp: float, purpose: str = "", vessel: VesselRef = "") -> Dict[str, Any]:
        """开始持续控温。

        Args:
            temp[目标温度]: 目标温度（°C）。
            purpose[目的]: 操作目的，仅记录。
            vessel[容器]: 被控温的容器，可不填。
        """
        target = self._checked_temp(temp)
        label = self._vessel_label(vessel)
        mode = self._mode_for(target)
        with self._lock:
            self._status = "Idle"
            self._operation_mode = mode
            self._continuous = True
            self._target_temp = target
            self._current_temp = target
            self._remaining_time = 0.0
            self._message = f"持续控温 {label or '容器'} {target:g}°C" + (f"（{purpose}）" if purpose else "")
        return {"success": True, "vessel": label, "temp": target}

    @action(
        action_type="unilabos_msgs.action:HeatChillStop",
        display_name="停止控温",
        description="停止持续控温，回到室温",
    )
    async def heat_chill_stop(self, vessel: VesselRef = "", purpose: str = "") -> Dict[str, Any]:
        """停止控温。

        Args:
            vessel[容器]: 被控温的容器，可不填。
            purpose[目的]: 操作目的，仅记录。
        """
        label = self._vessel_label(vessel)
        previous = self._target_temp
        self._stop("Stopped")
        self._message = f"已停止控温 {label or ''}（之前目标 {previous:g}°C）".strip()
        return {"success": True, "vessel": label, "previous_target_temp": previous}

    # ============ 内部 ============

    def _checked_temp(self, temp: Any) -> float:
        target = float(temp)
        if not self._min_temp <= target <= self._max_temp:
            self._reject(f"温度 {target:g}°C 超出范围 {self._min_temp:g} ~ {self._max_temp:g}°C")
        return target

    @staticmethod
    def _mode_for(target: float) -> str:
        if target > AMBIENT_TEMPERATURE:
            return "Heating"
        if target < AMBIENT_TEMPERATURE:
            return "Cooling"
        return "Maintaining"

    def _stop(self, mode: str) -> None:
        with self._lock:
            self._status = "Idle"
            self._operation_mode = mode
            self._continuous = False
            self._target_temp = AMBIENT_TEMPERATURE
            self._current_temp = AMBIENT_TEMPERATURE
            self._stir_speed = 0.0
            self._remaining_time = 0.0

    def __repr__(self) -> str:
        return f"VirtualHeatChill({self.device_id}: {self._operation_mode}, {self._current_temp:.1f}°C)"
