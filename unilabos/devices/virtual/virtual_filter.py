"""虚拟过滤器：把容器里的液体过滤到滤液容器，可加热、搅拌。

过滤时长按体积估算（5 mL/s，搅拌 ×0.8、50°C 以上 ×0.7，至少 10 秒模拟时间）；容器在本设备
可见时把滤过的液体从源容器转到滤液容器（PLR 体积跟踪器）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from unilabos.backend.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import (
    UL_PER_ML,
    VesselRef,
    VirtualDevice,
    nested_settings,
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

#: 过滤器能工作的最低温度（°C）；0 表示「未指定」按室温处理
MIN_TEMP = 4.0
AMBIENT_TEMPERATURE = 25.0
#: 未给体积时按此体积估算时长（mL）
DEFAULT_FILTER_VOLUME = 50.0
FILTER_RATE = 5.0
MIN_FILTER_SECONDS = 10.0


@device(
    id="virtual_filter",
    display_name="虚拟过滤器",
    category=["virtual_device"],
    description="可加热、搅拌的虚拟过滤器，供过滤、洗涤固体等协议与示例图使用",
    supported_backends=["hostlink", "ros2"],
    handles=[
        InputHandle(
            key="filterin",
            data_type="transport",
            label="filter_in",
            side=Side.NORTH,
            data_key="vessel_in",
            data_source=DataSource.HANDLE,
            description="需要过滤的样品容器",
        ),
        OutputHandle(
            key="filtrateout",
            data_type="transport",
            label="filtrate_out",
            side=Side.SOUTH,
            data_key="filtrate_out",
            data_source=DataSource.HANDLE,
            description="滤液出口",
        ),
        OutputHandle(
            key="retentateout",
            data_type="transport",
            label="retentate_out",
            side=Side.EAST,
            data_key="retentate_out",
            data_source=DataSource.HANDLE,
            description="滤渣/固体出口",
        ),
    ],
)
class VirtualFilter(VirtualDevice):
    """温度 4 ~ max_temp（°C），搅拌 ≤ max_stir_speed（RPM），单次 ≤ max_volume（mL）。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        max_temp: float = 100.0,
        max_stir_speed: float = 1000.0,
        max_volume: float = 500.0,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟过滤器。

        Args:
            device_id[设备ID]: 设备实例 ID。
            max_temp[最高温度]: 最高温度（°C）。
            max_stir_speed[最高搅拌速度]: 最高搅拌速度（RPM）。
            max_volume[最大体积]: 单次最大过滤体积（mL）。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用设备类的默认值（0.001）。
        """
        settings = nested_settings(
            kwargs,
            max_temp=max_temp,
            max_stir_speed=max_stir_speed,
            max_volume=max_volume,
            port=port,
            time_scale=time_scale,
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_filter", settings["time_scale"])
        self._max_temp = float(settings["max_temp"])
        self._max_stir_speed = float(settings["max_stir_speed"])
        self._max_volume = float(settings["max_volume"])
        self.port = str(settings["port"])
        self._current_temp = AMBIENT_TEMPERATURE
        self._filtered_volume = 0.0

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Running / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def current_status(self) -> str:
        """最近一次操作的说明。"""
        return self._message

    @property
    @topic_config(period=1.0)
    def message(self) -> str:
        """最近一次操作的说明。"""
        return self._message

    @property
    @topic_config(period=1.0)
    def progress(self) -> float:
        """当前过滤进度（%）。"""
        return self._progress

    @property
    @topic_config(period=1.0)
    def current_temp(self) -> float:
        """当前温度（°C）。"""
        return self._current_temp

    @property
    @topic_config(period=1.0)
    def filtered_volume(self) -> float:
        """已过滤体积（mL）。"""
        return round(self._filtered_volume, 6)

    @property
    @topic_config(period=5.0)
    def max_temp(self) -> float:
        """最高温度（°C）。"""
        return self._max_temp

    @property
    @topic_config(period=5.0)
    def max_stir_speed(self) -> float:
        """最高搅拌速度（RPM）。"""
        return self._max_stir_speed

    @property
    @topic_config(period=5.0)
    def max_volume(self) -> float:
        """单次最大过滤体积（mL）。"""
        return self._max_volume

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="回到室温待机")
    async def initialize(self) -> bool:
        """初始化：回到室温待机。"""
        self._reset()
        return True

    @action(auto_prefix=True, display_name="清理", description="停止正在进行的过滤")
    async def cleanup(self) -> bool:
        """清理：请求停止当前过滤。"""
        self._request_stop()
        self._reset()
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:Filter",
        display_name="过滤",
        description="把容器里的液体过滤到滤液容器（volume 为 0 时过滤全部），可加热、搅拌",
        feedback_interval=0.5,
    )
    async def filter(
        self,
        vessel: VesselRef,
        filtrate_vessel: VesselRef = "",
        stir: bool = False,
        stir_speed: float = 300.0,
        temp: float = 25.0,
        continue_heatchill: bool = False,
        volume: float = 0.0,
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """过滤。

        Args:
            vessel[待过滤容器]: 待过滤的容器（协议编译器传过滤器本身时只模拟过程）。
            filtrate_vessel[滤液容器]: 接收滤液的容器，可不填。
            stir[搅拌]: 过滤时是否搅拌。
            stir_speed[搅拌速度]: 搅拌速度（RPM），stir 为真时校验。
            temp[温度]: 过滤温度（°C）；0 表示室温，低于 4°C 按 4°C。
            continue_heatchill[保持控温]: 过滤后是否保持温度。
            volume[体积]: 过滤体积（mL），0 表示全部。
        """
        temperature = AMBIENT_TEMPERATURE if float(temp) == 0.0 else max(float(temp), MIN_TEMP)
        if temperature > self._max_temp:
            self._reject(f"温度 {temperature:g}°C 超出范围 {MIN_TEMP:g} ~ {self._max_temp:g}°C")
        if stir and not 0 <= float(stir_speed) <= self._max_stir_speed:
            self._reject(f"搅拌速度 {float(stir_speed):g} RPM 超出范围 0 ~ {self._max_stir_speed:g} RPM")
        requested = float(volume)
        if requested < 0 or requested > self._max_volume:
            self._reject(f"过滤体积 {requested:g} mL 超出范围 0 ~ {self._max_volume:g} mL")
        source, filtrate = self._vessel_label(vessel), self._vessel_label(filtrate_vessel)
        estimate = requested if requested > 0 else DEFAULT_FILTER_VOLUME
        duration = estimate / FILTER_RATE * (0.8 if stir else 1.0) * (0.7 if temperature > 50.0 else 1.0)
        duration = max(duration, MIN_FILTER_SECONDS)

        def step(fraction: float) -> None:
            self._filtered_volume = estimate * fraction

        with self._operation("Running", f"过滤 {source or '?'} → {filtrate or '滤液'}，{temperature:g}°C", action_context):
            self._current_temp = temperature
            self._filtered_volume = 0.0
            await self._simulate(duration, action_context, on_step=step, feedback={"current_temp": temperature})
            taken = self._transfer_liquid(
                vessel, filtrate_vessel, requested * UL_PER_ML if requested > 0 else float("inf")
            )
            if taken:
                self._filtered_volume = sum(item[1] for item in taken) / UL_PER_ML
            if not continue_heatchill:
                self._current_temp = AMBIENT_TEMPERATURE
        self._message = f"过滤完成：{source or '?'} → {filtrate or '滤液'}，{self._filtered_volume:.3f} mL"
        return {
            "success": True,
            "vessel": source,
            "filtrate_vessel": filtrate,
            "filtered_volume": round(self._filtered_volume, 6),
            "temp": temperature,
        }

    # ============ 内部 ============

    def _reset(self) -> None:
        with self._lock:
            self._status = "Idle"
            self._current_temp = AMBIENT_TEMPERATURE
            self._filtered_volume = 0.0
            self._remaining_time = 0.0

    def __repr__(self) -> str:
        return f"VirtualFilter({self.device_id}: {self._status}, {self._filtered_volume:.1f} mL)"
