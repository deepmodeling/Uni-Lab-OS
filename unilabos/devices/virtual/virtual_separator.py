"""虚拟分液器：按「搅拌 → 静置分层」循环萃取 / 洗涤（``separate``），以及分液协议用的底阀放液
（``valve_open``，命令如 ``"time > 60"`` 放 60 秒、``"delta > 0.05"`` 放到电导突跃）。"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Union

from unilabos.backend.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import VesselRef, VirtualDevice, nested_settings
from unilabos.registry.decorators import (
    DataSource,
    InputHandle,
    OutputHandle,
    Side,
    action,
    device,
    topic_config,
)

_PHASES = ("top", "bottom")
_PURPOSES = ("wash", "extract", "separate")
#: 「电导突跃」类放液命令的模拟时长（s）
DELTA_DRAIN_SECONDS = 30.0
_TIME_COMMAND = re.compile(r"^\s*time\s*>\s*([0-9]*\.?[0-9]+)\s*$", re.IGNORECASE)
_DELTA_COMMAND = re.compile(r"^\s*delta\s*>\s*([0-9]*\.?[0-9]+)\s*$", re.IGNORECASE)


@device(
    id="virtual_separator",
    display_name="虚拟分离器",
    category=["virtual_device"],
    description="搅拌、静置分层、底阀放液的虚拟分液器，供分液协议与示例图使用",
    icon="Separator.webp",
    supported_backends=["hostlink", "ros2"],
    handles=[
        InputHandle(
            key="separatorin",
            data_type="fluid",
            label="separator_in",
            side=Side.NORTH,
            data_key="from_vessel",
            data_source=DataSource.HANDLE,
            description="需要分离的混合液体输入口",
        ),
        OutputHandle(
            key="bottomphaseout",
            data_type="fluid",
            label="bottom_phase_out",
            side=Side.SOUTH,
            data_key="bottom_outlet",
            data_source=DataSource.EXECUTOR,
            description="下相（重相）液体输出口",
        ),
        OutputHandle(
            key="topphaseout",
            data_type="fluid",
            label="top_phase_out",
            side=Side.NORTH,
            data_key="top_outlet",
            data_source=DataSource.EXECUTOR,
            description="上相（轻相）液体输出口",
        ),
        InputHandle(
            key="bind",
            data_type="mechanical",
            label="bind",
            side=Side.WEST,
            data_key="mechanical_port",
            data_source=DataSource.HANDLE,
            description="用于连接搅拌器等机械设备的接口",
        ),
    ],
)
class VirtualSeparator(VirtualDevice):
    """容积 volume（mL）的分液器。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        volume: float = 250.0,
        has_phases: bool = True,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟分液器。

        Args:
            device_id[设备ID]: 设备实例 ID。
            volume[容积]: 分液器容积（mL）。
            has_phases[可分层]: 液体是否能分出两相。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用全局 virtual_time_scale。
        """
        settings = nested_settings(kwargs, volume=volume, has_phases=has_phases, port=port, time_scale=time_scale)
        super().__init__(device_id or kwargs.pop("id", None), "virtual_separator", settings["time_scale"])
        self._volume = float(settings["volume"])
        self._has_phases = bool(settings["has_phases"])
        self.port = str(settings["port"])
        self._separator_state = "Ready"
        self._phase_separation = False
        self._stir_speed = 0.0
        self._settling_time = 0.0
        self._valve_state = "Closed"

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Separating / Draining / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def separator_state(self) -> str:
        """Ready / Stirring / Settling / Draining / Completed / Stopped / Error。"""
        return self._separator_state

    @property
    @topic_config(period=1.0)
    def phase_separation(self) -> bool:
        """当前是否已分层。"""
        return self._phase_separation

    @property
    @topic_config(period=1.0)
    def stir_speed(self) -> float:
        """当前搅拌速度（RPM）。"""
        return self._stir_speed

    @property
    @topic_config(period=1.0)
    def settling_time(self) -> float:
        """最近一次的静置时间（s）。"""
        return self._settling_time

    @property
    @topic_config(period=1.0)
    def valve_state(self) -> str:
        """底阀：Open / Closed。"""
        return self._valve_state

    @property
    @topic_config(period=1.0)
    def progress(self) -> float:
        """当前动作进度（%）。"""
        return self._progress

    @property
    @topic_config(period=1.0)
    def message(self) -> str:
        """最近一次操作的说明。"""
        return self._message

    @property
    @topic_config(period=5.0)
    def volume(self) -> float:
        """分液器容积（mL）。"""
        return self._volume

    @property
    @topic_config(period=5.0)
    def has_phases(self) -> bool:
        """液体是否能分出两相。"""
        return self._has_phases

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="关阀、停止搅拌、回到就绪")
    async def initialize(self) -> bool:
        """初始化：关阀、回到就绪。"""
        self._reset("Ready")
        return True

    @action(auto_prefix=True, display_name="清理", description="停止正在进行的分液")
    async def cleanup(self) -> bool:
        """清理：请求停止当前分液。"""
        self._request_stop()
        self._reset("Ready")
        return True

    # ============ 协议使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:Separate",
        display_name="萃取 / 洗涤分液",
        description="按 repeats 次「搅拌 stir_time → 静置 settling_time」分液，产物相留在 product_phase",
        feedback_interval=0.5,
    )
    async def separate(
        self,
        purpose: str,
        product_phase: str,
        from_vessel: VesselRef,
        separation_vessel: VesselRef,
        to_vessel: VesselRef,
        waste_phase_to_vessel: VesselRef = "",
        solvent: str = "",
        solvent_volume: Union[float, str] = 50.0,
        through: str = "",
        repeats: int = 1,
        stir_time: float = 30.0,
        stir_speed: float = 300.0,
        settling_time: float = 300.0,
        vessel: VesselRef = "",
        volume: str = "",
        product_vessel: VesselRef = "",
        waste_vessel: VesselRef = "",
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """萃取 / 洗涤分液。

        Args:
            purpose[目的]: wash（洗涤）、extract（萃取）或 separate。
            product_phase[产物相]: top（上相）或 bottom（下相）。
            from_vessel[来源容器]: 混合液来源。
            separation_vessel[分液容器]: 分液所用容器。
            to_vessel[产物容器]: 产物相去向。
            waste_phase_to_vessel[废相容器]: 废相去向，可不填。
            solvent[溶剂]: 加入的萃取 / 洗涤溶剂，仅记录。
            solvent_volume[溶剂体积]: 每次加入的溶剂体积（mL 或带单位的描述），仅记录。
            through[经过]: 经过的过滤介质，仅记录。
            repeats[次数]: 搅拌 - 静置循环次数。
            stir_time[搅拌时间]: 每次搅拌时间（s）。
            stir_speed[搅拌速度]: 搅拌速度（RPM）。
            settling_time[静置时间]: 每次静置分层时间（s）。
            vessel[容器]: 协议里的容器字段，仅记录。
            volume[体积]: 协议里的体积描述，仅记录。
            product_vessel[产物容器（协议）]: 协议里的产物容器字段，仅记录。
            waste_vessel[废液容器（协议）]: 协议里的废液容器字段，仅记录。
        """
        if product_phase not in _PHASES:
            self._reject(f"产物相 {product_phase!r} 无效，必须是 top 或 bottom")
        if purpose not in _PURPOSES:
            self._reject(f"分离目的 {purpose!r} 无效，必须是 {' / '.join(_PURPOSES)}")
        cycles = int(repeats)
        stir_seconds, settle_seconds = float(stir_time), float(settling_time)
        if cycles < 1 or stir_seconds < 0 or settle_seconds < 0:
            self._reject(f"repeats 至少为 1、时间不能为负：repeats={cycles}, stir_time={stir_seconds}, settling_time={settle_seconds}")
        source, product = self._vessel_label(from_vessel), self._vessel_label(to_vessel)
        cycle_seconds = stir_seconds + settle_seconds
        total = cycle_seconds * cycles
        with self._operation("Separating", f"{purpose} {source or '?'} → {product or '?'}（{cycles} 次）", action_context):
            self._phase_separation = False
            try:
                for index in range(cycles):
                    start = 100.0 * index / cycles
                    stir_end = start + (100.0 * stir_seconds / total if total > 0 else 100.0 / cycles)
                    self._separator_state, self._stir_speed = "Stirring", float(stir_speed)
                    await self._simulate(
                        stir_seconds,
                        action_context,
                        progress_span=(start, stir_end),
                        remaining_after=total - index * cycle_seconds - stir_seconds,
                    )
                    self._separator_state, self._stir_speed = "Settling", 0.0
                    await self._simulate(
                        settle_seconds,
                        action_context,
                        progress_span=(stir_end, 100.0 * (index + 1) / cycles),
                        remaining_after=total - (index + 1) * cycle_seconds,
                    )
                    self._phase_separation = self._has_phases
            except BaseException:
                self._separator_state = "Stopped"
                raise
            finally:
                self._stir_speed = 0.0
            self._settling_time = settle_seconds
            self._separator_state = "Completed"
        self._message = f"分液完成：{purpose}，{cycles} 次，产物相 {product_phase} → {product or '?'}"
        return {
            "success": True,
            "purpose": purpose,
            "product_phase": product_phase,
            "repeats": cycles,
            "from_vessel": source,
            "to_vessel": product,
            "phase_separation": self._phase_separation,
        }

    @action(
        action_type="unilabos_msgs.action:SendCmd",
        display_name="底阀放液",
        description='打开底阀放液直到条件满足后关阀：「time > 60」放 60 秒，「delta > 0.05」放到电导突跃',
        feedback_interval=0.5,
    )
    async def valve_open(self, command: str, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """打开底阀放液，条件满足后关阀。

        Args:
            command[关阀条件]: 「time > 秒数」或「delta > 电导变化阈值」。
        """
        text = str(command or "").strip()
        timed, delta = _TIME_COMMAND.match(text), _DELTA_COMMAND.match(text)
        if timed is None and delta is None:
            self._reject(f"无法识别的放液条件：{command!r}（支持 time > 秒数、delta > 阈值）")
        seconds = float(timed.group(1)) if timed is not None else DELTA_DRAIN_SECONDS
        with self._operation("Draining", f"底阀放液直到 {text}", action_context):
            self._separator_state, self._valve_state = "Draining", "Open"
            try:
                await self._simulate(seconds, action_context, feedback={"command": text})
            finally:
                self._valve_state = "Closed"
            self._separator_state = "Ready"
        self._message = f"底阀放液完成（{text}）"
        return {"success": True, "command": text, "drain_time": seconds}

    # ============ 内部 ============

    def _reset(self, state: str) -> None:
        with self._lock:
            self._status = "Idle"
            self._separator_state = state
            self._phase_separation = False
            self._stir_speed = 0.0
            self._valve_state = "Closed"
            self._remaining_time = 0.0

    def __repr__(self) -> str:
        return f"VirtualSeparator({self.device_id}: {self._separator_state})"
