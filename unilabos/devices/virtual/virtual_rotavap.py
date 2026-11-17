"""虚拟旋转蒸发仪：按温度、真空度、转速蒸发溶剂。

水系溶剂自动提高温度（≥ 80°C）与压力（≥ 0.2 bar），乙醇 / 甲醇 / 丙酮等易挥发溶剂自动降低
（≤ 50°C、≤ 0.05 bar）。容器在本设备可见时，蒸发结束从容器里去掉该溶剂（未给溶剂时去掉全部液体，
只留固体），写回 PLR 体积跟踪器。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from unilabos.backend.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import (
    UL_PER_ML,
    VesselRef,
    VirtualDevice,
    nested_settings,
    parse_duration,
)
from unilabos.registry.decorators import (
    DataSource,
    InputHandle,
    OutputHandle,
    Side,
    action,
    device,
    topic_config,
)

MIN_TEMP = 10.0
MIN_ROTATION_SPEED = 10.0
MIN_PRESSURE, MAX_PRESSURE = 0.01, 1.0
AMBIENT_TEMPERATURE = 25.0
ATMOSPHERE_BAR = 1.0
_AQUEOUS = ("water", "aqueous")
_VOLATILE = ("ethanol", "methanol", "acetone")


@device(
    id="virtual_rotavap",
    display_name="虚拟旋转蒸发仪",
    category=["virtual_device"],
    description="按温度、真空度与转速蒸发溶剂的虚拟旋转蒸发仪，供蒸发协议与示例图使用",
    icon="Rotaryevaporator.webp",
    supported_backends=["hostlink", "ros2"],
    handles=[
        InputHandle(
            key="samplein",
            data_type="fluid",
            label="sample_in",
            side=Side.NORTH,
            data_key="vessel_in",
            data_source=DataSource.HANDLE,
            description="样品连接口",
        ),
        OutputHandle(
            key="productout",
            data_type="fluid",
            label="product_out",
            side=Side.SOUTH,
            data_key="product_out",
            data_source=DataSource.HANDLE,
            description="浓缩产物出口",
        ),
        OutputHandle(
            key="solventout",
            data_type="fluid",
            label="solvent_out",
            side=Side.EAST,
            data_key="solvent_out",
            data_source=DataSource.HANDLE,
            description="冷凝溶剂出口",
        ),
    ],
)
class VirtualRotavap(VirtualDevice):
    """温度 10 ~ max_temp（°C），转速 10 ~ max_rotation_speed（RPM），真空 0.01 ~ 1 bar。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        max_temp: float = 180.0,
        max_rotation_speed: float = 280.0,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟旋转蒸发仪。

        Args:
            device_id[设备ID]: 设备实例 ID。
            max_temp[最高温度]: 水浴最高温度（°C）。
            max_rotation_speed[最高转速]: 最高旋转速度（RPM）。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用全局 virtual_time_scale。
        """
        settings = nested_settings(
            kwargs, max_temp=max_temp, max_rotation_speed=max_rotation_speed, port=port, time_scale=time_scale
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_rotavap", settings["time_scale"])
        self._max_temp = float(settings["max_temp"])
        self._max_rotation_speed = float(settings["max_rotation_speed"])
        self.port = str(settings["port"])
        self._rotavap_state = "Ready"
        self._current_temp = AMBIENT_TEMPERATURE
        self._rotation_speed = 0.0
        self._vacuum_pressure = ATMOSPHERE_BAR
        self._evaporated_volume = 0.0

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Evaporating / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def rotavap_state(self) -> str:
        """Ready / Evaporating / Completed / Stopped / Error。"""
        return self._rotavap_state

    @property
    @topic_config(period=1.0)
    def current_temp(self) -> float:
        """水浴温度（°C）。"""
        return self._current_temp

    @property
    @topic_config(period=1.0)
    def rotation_speed(self) -> float:
        """旋转速度（RPM）。"""
        return self._rotation_speed

    @property
    @topic_config(period=1.0)
    def vacuum_pressure(self) -> float:
        """真空度（bar）。"""
        return self._vacuum_pressure

    @property
    @topic_config(period=1.0)
    def evaporated_volume(self) -> float:
        """已蒸发体积（mL）。"""
        return round(self._evaporated_volume, 6)

    @property
    @topic_config(period=1.0)
    def progress(self) -> float:
        """当前蒸发进度（%）。"""
        return self._progress

    @property
    @topic_config(period=1.0)
    def remaining_time(self) -> float:
        """剩余模拟时间（s）。"""
        return self._remaining_time

    @property
    @topic_config(period=1.0)
    def message(self) -> str:
        """最近一次操作的说明。"""
        return self._message

    @property
    @topic_config(period=5.0)
    def max_temp(self) -> float:
        """水浴最高温度（°C）。"""
        return self._max_temp

    @property
    @topic_config(period=5.0)
    def max_rotation_speed(self) -> float:
        """最高旋转速度（RPM）。"""
        return self._max_rotation_speed

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="停转、泄压、回到室温")
    async def initialize(self) -> bool:
        """初始化：停转、泄压。"""
        self._halt("Ready")
        return True

    @action(auto_prefix=True, display_name="清理", description="停止正在进行的蒸发并离线")
    async def cleanup(self) -> bool:
        """清理：请求停止当前蒸发。"""
        self._request_stop()
        self._halt("Offline")
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:Evaporate",
        display_name="旋转蒸发",
        description="按温度、真空度与转速蒸发 time 秒；按溶剂自动调整温度与压力",
        feedback_interval=0.5,
    )
    async def evaporate(
        self,
        vessel: VesselRef,
        pressure: float = 0.1,
        temp: float = 60.0,
        time: Union[float, str] = 180.0,
        stir_speed: float = 100.0,
        solvent: str = "",
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """旋转蒸发。

        Args:
            vessel[容器]: 待蒸发的容器。
            pressure[真空度]: 真空度（bar），0.01 ~ 1。
            temp[温度]: 水浴温度（°C）。
            time[时长]: 蒸发时长，数字按秒，也可写 "30 min"。
            stir_speed[转速]: 旋转速度（RPM）。
            solvent[溶剂]: 要蒸掉的溶剂名；不填时蒸干全部液体。
        """
        temperature, vacuum = float(temp), float(pressure)
        solvent_name = str(solvent or "").strip()
        lowered = solvent_name.lower()
        if any(item in lowered for item in _AQUEOUS):
            temperature, vacuum = max(temperature, 80.0), max(vacuum, 0.2)
        elif any(item in lowered for item in _VOLATILE):
            temperature, vacuum = min(temperature, 50.0), min(vacuum, 0.05)
        if not MIN_TEMP <= temperature <= self._max_temp:
            self._reject(f"温度 {temperature:g}°C 超出范围 {MIN_TEMP:g} ~ {self._max_temp:g}°C")
        speed = float(stir_speed)
        if not MIN_ROTATION_SPEED <= speed <= self._max_rotation_speed:
            self._reject(f"旋转速度 {speed:g} RPM 超出范围 {MIN_ROTATION_SPEED:g} ~ {self._max_rotation_speed:g} RPM")
        if not MIN_PRESSURE <= vacuum <= MAX_PRESSURE:
            self._reject(f"真空度 {vacuum:g} bar 超出范围 {MIN_PRESSURE:g} ~ {MAX_PRESSURE:g} bar")
        duration = parse_duration(time)
        if duration <= 0:
            self._reject(f"蒸发时长必须大于 0，收到 {time!r}")
        label = self._vessel_label(vessel)
        rate = 0.6 if any(item in lowered for item in _AQUEOUS) else 1.0 if any(item in lowered for item in _VOLATILE) else 0.8

        def step(fraction: float) -> None:
            self._evaporated_volume = 100.0 * rate * fraction

        with self._operation("Evaporating", f"蒸发 {label or '容器'}：{temperature:g}°C，{vacuum:g} bar，{speed:g} RPM", action_context):
            self._rotavap_state = "Evaporating"
            self._current_temp, self._vacuum_pressure, self._rotation_speed = temperature, vacuum, speed
            self._evaporated_volume = 0.0
            completed = False
            try:
                await self._simulate(duration, action_context, on_step=step, feedback={"current_temp": temperature})
                completed = True
            finally:
                self._rotation_speed = 0.0
                self._vacuum_pressure = ATMOSPHERE_BAR
                self._current_temp = AMBIENT_TEMPERATURE
                self._rotavap_state = "Completed" if completed else "Stopped"
            names: List[str] = [solvent_name] if solvent_name else []
            removed = self._drop_from_vessel(vessel, names)
            if removed > 0:
                self._evaporated_volume = removed / UL_PER_ML
        self._message = f"蒸发完成：{label or '容器'}，蒸出 {self._evaporated_volume:.3f} mL" + (
            f" {solvent_name}" if solvent_name else ""
        )
        return {
            "success": True,
            "vessel": label,
            "temp": temperature,
            "pressure": vacuum,
            "stir_speed": speed,
            "time": duration,
            "evaporated_volume": round(self._evaporated_volume, 6),
        }

    # ============ 内部 ============

    def _halt(self, state: str) -> None:
        with self._lock:
            self._status = "Idle"
            self._rotavap_state = state
            self._current_temp = AMBIENT_TEMPERATURE
            self._rotation_speed = 0.0
            self._vacuum_pressure = ATMOSPHERE_BAR
            self._remaining_time = 0.0

    def __repr__(self) -> str:
        return f"VirtualRotavap({self.device_id}: {self._rotavap_state})"
