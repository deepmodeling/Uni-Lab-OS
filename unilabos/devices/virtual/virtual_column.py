"""虚拟柱层析装置：样品从源容器上柱、洗脱，收集到目标容器。

分离时长按柱长与最大流速估算（至少 20 秒模拟时间）；源 / 目标容器有体积跟踪器时，
把源容器里的液体整体转到目标容器（PLR 体积跟踪器）。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from unilabos.client.host.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import UL_PER_ML, VirtualDevice, nested_settings
from unilabos.registry.decorators import (
    DataSource,
    InputHandle,
    OutputHandle,
    Side,
    action,
    device,
    topic_config,
)
from unilabos.registry.placeholder_type import ResourceSlot

#: 最短分离时间（模拟秒）
MIN_SEPARATION_SECONDS = 20.0
_PHASES = ((25.0, "样品上柱"), (50.0, "开始洗脱"), (75.0, "组分分离"), (100.0, "收集产物"))


@device(
    id="virtual_column",
    display_name="虚拟柱层析装置",
    category=["virtual_device"],
    description="模拟上柱、洗脱、收集的虚拟柱层析装置，供柱层析协议与示例图使用",
    supported_backends=["hostlink", "ros2"],
    handles=[
        InputHandle(
            key="columnin",
            data_type="transport",
            label="columnin",
            side=Side.WEST,
            data_key="from_vessel",
            data_source=DataSource.HANDLE,
            description="样品输入口",
        ),
        OutputHandle(
            key="columnout",
            data_type="transport",
            label="columnout",
            side=Side.EAST,
            data_key="to_vessel",
            data_source=DataSource.HANDLE,
            description="产物输出口",
        ),
    ],
)
class VirtualColumn(VirtualDevice):
    """柱长 column_length（cm）、柱径 column_diameter（cm）、最大流速 max_flow_rate（mL/s）。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        max_flow_rate: float = 10.0,
        column_length: float = 25.0,
        column_diameter: float = 2.0,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟柱层析装置。

        Args:
            device_id[设备ID]: 设备实例 ID。
            max_flow_rate[最大流速]: 最大流速（mL/s）。
            column_length[柱长]: 柱长（cm）。
            column_diameter[柱径]: 柱内径（cm）。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用设备类的默认值（0.001）。
        """
        settings = nested_settings(
            kwargs,
            max_flow_rate=max_flow_rate,
            column_length=column_length,
            column_diameter=column_diameter,
            port=port,
            time_scale=time_scale,
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_column", settings["time_scale"])
        self._max_flow_rate = float(settings["max_flow_rate"])
        self._column_length = float(settings["column_length"])
        self._column_diameter = float(settings["column_diameter"])
        if min(self._max_flow_rate, self._column_length, self._column_diameter) <= 0:
            raise ValueError("max_flow_rate、column_length、column_diameter 都必须大于 0")
        self.port = str(settings["port"])
        self._column_state = "Ready"
        self._current_phase = "待机"
        self._current_flow_rate = 0.0
        self._processed_volume = 0.0
        self._final_volume = 0.0

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Running / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def column_state(self) -> str:
        """Ready / Separating / Completed / Stopped / Error。"""
        return self._column_state

    @property
    @topic_config(period=1.0)
    def current_phase(self) -> str:
        """当前阶段：样品上柱 / 开始洗脱 / 组分分离 / 收集产物。"""
        return self._current_phase

    @property
    @topic_config(period=1.0)
    def current_status(self) -> str:
        """最近一次操作的说明。"""
        return self._message

    @property
    @topic_config(period=1.0)
    def current_flow_rate(self) -> float:
        """当前流速（mL/s）。"""
        return self._current_flow_rate

    @property
    @topic_config(period=1.0)
    def processed_volume(self) -> float:
        """已处理体积（mL）。"""
        return round(self._processed_volume, 6)

    @property
    @topic_config(period=1.0)
    def final_volume(self) -> float:
        """上一次分离收集的体积（mL）。"""
        return round(self._final_volume, 6)

    @property
    @topic_config(period=1.0)
    def progress(self) -> float:
        """当前分离进度（%）。"""
        return self._progress

    @property
    @topic_config(period=5.0)
    def max_flow_rate(self) -> float:
        """最大流速（mL/s）。"""
        return self._max_flow_rate

    @property
    @topic_config(period=5.0)
    def column_length(self) -> float:
        """柱长（cm）。"""
        return self._column_length

    @property
    @topic_config(period=5.0)
    def column_diameter(self) -> float:
        """柱内径（cm）。"""
        return self._column_diameter

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="回到就绪")
    async def initialize(self) -> bool:
        """初始化：回到就绪。"""
        self._reset("Ready")
        return True

    @action(auto_prefix=True, display_name="清理", description="停止正在进行的分离并离线")
    async def cleanup(self) -> bool:
        """清理：请求停止当前分离。"""
        self._request_stop()
        self._reset("Offline")
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:RunColumn",
        display_name="柱层析",
        description="样品从源容器上柱、洗脱并收集到目标容器",
        feedback_interval=0.5,
    )
    async def run_column(
        self,
        from_vessel: ResourceSlot,
        to_vessel: ResourceSlot,
        column: str,
        rf: str = "",
        pct1: str = "",
        pct2: str = "",
        solvent1: str = "",
        solvent2: str = "",
        ratio: str = "",
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """运行柱层析。

        Args:
            from_vessel[样品容器]: 样品所在的源容器。
            to_vessel[收集容器]: 收集产物的容器。
            column[色谱柱]: 色谱柱名称或图节点 id。
            rf[Rf 值]: 目标组分的 Rf 值，仅记录。
            pct1[溶剂1比例]: 溶剂 1 百分比，仅记录。
            pct2[溶剂2比例]: 溶剂 2 百分比，仅记录。
            solvent1[溶剂1]: 洗脱溶剂 1，仅记录。
            solvent2[溶剂2]: 洗脱溶剂 2，仅记录。
            ratio[溶剂比]: 溶剂比例，仅记录。
        """
        source, target = self._vessel_label(from_vessel), self._vessel_label(to_vessel)
        duration = max(self._column_length * 2.0 / self._max_flow_rate, MIN_SEPARATION_SECONDS)
        bed_volume = math.pi * (self._column_diameter / 2.0) ** 2 * self._column_length

        def step(fraction: float) -> None:
            percent = fraction * 100.0
            self._current_phase = next(name for limit, name in _PHASES if percent <= limit + 1e-9)
            self._processed_volume = bed_volume * fraction

        with self._operation("Running", f"柱层析 {source or '?'} → {target or '?'}（{column}）", action_context):
            self._column_state = "Separating"
            self._current_flow_rate = self._max_flow_rate
            self._processed_volume = 0.0
            completed = False
            try:
                await self._simulate(duration, action_context, on_step=step, feedback={"column": str(column)})
                completed = True
            finally:
                self._current_flow_rate = 0.0
                self._column_state = "Completed" if completed else "Stopped"
            taken = self._transfer_liquid(from_vessel, to_vessel, float("inf"))
            self._final_volume = sum(item[1] for item in taken) / UL_PER_ML if taken else bed_volume
            self._processed_volume = self._final_volume
        self._current_phase = "完成"
        self._message = f"柱层析完成：{source or '?'} → {target or '?'}，收集 {self._final_volume:.3f} mL"
        return {
            "success": True,
            "from_vessel": source,
            "to_vessel": target,
            "column": str(column),
            "collected_volume": round(self._final_volume, 6),
            "solvents": {"solvent1": solvent1, "solvent2": solvent2, "ratio": ratio, "rf": rf},
        }

    # ============ 内部 ============

    def _reset(self, state: str) -> None:
        with self._lock:
            self._status = "Idle"
            self._column_state = state
            self._current_phase = "待机"
            self._current_flow_rate = 0.0
            self._processed_volume = 0.0
            self._remaining_time = 0.0

    def __repr__(self) -> str:
        return f"VirtualColumn({self.device_id}: {self._column_state}, {self._current_phase})"
