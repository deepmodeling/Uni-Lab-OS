"""虚拟注射泵（泵阀一体）。

协议编译器把 ``virtual_transfer_pump`` 当作带分配阀的注射泵：先 ``set_valve_position``
切到连线端口，再 ``set_position`` 把柱塞移到绝对体积（吸液 / 排液）。柱塞移动的模拟时长
= 移动体积 ÷ 速度，按时间倍率缩放后真实等待。``transfer`` 在两个容器之间按注射器容量分次
转移，容器在本设备可见时同步改写两边的内容物（PLR 体积跟踪器）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

from unilabos.backend.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import (
    UL_PER_ML,
    VesselRef,
    VirtualDevice,
    nested_settings,
)
from unilabos.registry.decorators import DataSource, OutputHandle, Side, action, device, topic_config

#: 体积比较的容差（mL）
_EPSILON = 1e-6
_MIN_VELOCITY = 0.1
_MAX_VELOCITY = 50.0


@device(
    id="virtual_transfer_pump",
    display_name="虚拟传输泵",
    category=["virtual_device"],
    description="注射器式虚拟转移泵（泵阀一体），供泵送类协议与示例图使用",
    icon="Pump.webp",
    supported_backends=["hostlink", "ros2"],
    handles=[
        OutputHandle(
            key="transferpump",
            data_type="fluid",
            label="transferpump",
            side=Side.SOUTH,
            data_key="fluid_port",
            data_source=DataSource.HANDLE,
            description="注射器式转移泵的连接口",
        ),
    ],
)
class VirtualTransferPump(VirtualDevice):
    """柱塞位置即注射器内的液体体积（mL）。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        max_volume: float = 25.0,
        transfer_rate: float = 0.0,
        max_velocity: Optional[float] = None,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟注射泵。

        Args:
            device_id[设备ID]: 设备实例 ID。
            max_volume[注射器容量]: 注射器最大体积（mL）。
            transfer_rate[转移速率]: 默认柱塞速度（mL/s）；不填时用 max_velocity。
            max_velocity[最大速度]: 柱塞最大速度（mL/s），默认 5。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用设备类的默认值（0.001）。
        """
        settings = nested_settings(
            kwargs,
            max_volume=max_volume,
            transfer_rate=transfer_rate,
            max_velocity=max_velocity,
            port=port,
            time_scale=time_scale,
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_transfer_pump", settings["time_scale"])
        self.max_volume = float(settings["max_volume"])
        if self.max_volume <= 0:
            raise ValueError(f"max_volume 必须大于 0，收到 {self.max_volume}")
        self.port = str(settings["port"])
        self._transfer_rate = float(settings["transfer_rate"] or 0.0)
        default_velocity = settings["max_velocity"]
        if default_velocity is None:
            default_velocity = self._transfer_rate if self._transfer_rate > 0 else 5.0
        self._max_velocity = self._clamp_velocity(float(default_velocity))
        self._position = 0.0
        # 泵头集成的分配阀当前通道（直连容器时由协议编译器按图里连线的端口名切换）
        self._valve_position = ""

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Aspirating / Dispensing / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def position(self) -> float:
        """柱塞位置（mL）。"""
        return round(self._position, 6)

    @property
    @topic_config(period=1.0)
    def current_volume(self) -> float:
        """注射器内的液体体积（mL），与柱塞位置一致。"""
        return round(self._position, 6)

    @property
    @topic_config(period=1.0)
    def remaining_capacity(self) -> float:
        """注射器剩余容量（mL）。"""
        return round(self.max_volume - self._position, 6)

    @property
    @topic_config(period=5.0)
    def max_velocity(self) -> float:
        """当前最大柱塞速度（mL/s）。"""
        return self._max_velocity

    @property
    @topic_config(period=5.0)
    def transfer_rate(self) -> float:
        """配置的转移速率（mL/s）。"""
        return self._transfer_rate

    @property
    @topic_config(period=1.0)
    def valve_position(self) -> str:
        """泵头集成阀当前通道（图中连线的端口名）。"""
        return self._valve_position

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="柱塞回到 0、注射器排空")
    async def initialize(self) -> bool:
        """初始化：柱塞归零。"""
        with self._lock:
            self._status = "Idle"
            self._position = 0.0
            self._message = ""
        return True

    @action(auto_prefix=True, display_name="清理", description="停止当前操作并回到空闲")
    async def cleanup(self) -> bool:
        """清理：请求停止正在进行的移动。"""
        self._request_stop()
        self._status = "Idle"
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:SendCmd",
        display_name="切换泵阀通道",
        description="切换泵头集成阀的通道（端口名或编号），供泵直连容器的协议使用",
    )
    def set_valve_position(self, command: Union[int, str]) -> Dict[str, Any]:
        """切换泵头集成阀。

        Args:
            command[通道]: 目标通道，图中连线在泵一侧的端口名（如 inlet、outlet）或编号。
        """
        channel = str(command).strip()
        if not channel:
            self._reject("泵阀通道不能为空")
        self._valve_position = channel
        self._message = f"泵阀切换到 {channel}"
        return {"success": True, "valve_position": channel}

    @action(
        action_type="unilabos_msgs.action:SetPumpPosition",
        display_name="移动柱塞",
        description="把柱塞移到绝对位置（mL）：位置变大为吸液、变小为排液",
        feedback_interval=0.5,
    )
    async def set_position(
        self,
        position: float,
        max_velocity: Optional[float] = None,
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """移动柱塞到绝对位置。

        Args:
            position[目标位置]: 柱塞目标位置（mL），0 到注射器容量之间。
            max_velocity[速度]: 移动速度（mL/s），不填用当前最大速度。
        """
        target = float(position)
        if target < -_EPSILON or target > self.max_volume + _EPSILON:
            self._reject(f"柱塞位置 {target} mL 超出注射器范围 0 ~ {self.max_volume} mL")
        target = min(max(target, 0.0), self.max_volume)
        moved = await self._move_to(target, max_velocity, action_context)
        return {
            "success": True,
            "position": round(self._position, 6),
            "moved_volume": round(moved, 6),
            "message": self._message,
        }

    @action(
        action_type="unilabos_msgs.action:Transfer",
        display_name="容器间转移",
        description="从源容器吸液、排到目标容器；超过注射器容量时分次转移，可见的容器同步改写内容物",
        feedback_interval=0.5,
    )
    async def transfer(
        self,
        volume: float,
        from_vessel: VesselRef = "",
        to_vessel: VesselRef = "",
        aspirate_velocity: Optional[float] = None,
        dispense_velocity: Optional[float] = None,
        amount: str = "",
        time: float = 0.0,
        viscous: bool = False,
        rinsing_solvent: str = "",
        rinsing_volume: float = 0.0,
        rinsing_repeats: int = 0,
        solid: bool = False,
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """在两个容器之间转移液体。

        Args:
            volume[体积]: 转移体积（mL）。
            from_vessel[源容器]: 源容器（图节点 id、uuid 或 {"id": ...}），可不填。
            to_vessel[目标容器]: 目标容器，可不填。
            aspirate_velocity[吸液速度]: 吸液速度（mL/s），不填用当前最大速度。
            dispense_velocity[排液速度]: 排液速度（mL/s），不填用当前最大速度。
            amount[用量描述]: 协议里的原始用量描述，仅记录。
            time[时间]: 协议给的转移时间（s），仅记录。
            viscous[粘稠]: 是否粘稠液体，仅记录。
            rinsing_solvent[润洗溶剂]: 润洗溶剂，仅记录。
            rinsing_volume[润洗体积]: 润洗体积（mL），仅记录。
            rinsing_repeats[润洗次数]: 润洗次数，仅记录。
            solid[固体]: 是否固体，仅记录。
        """
        total = float(volume)
        if total <= 0:
            self._reject(f"转移体积必须大于 0，收到 {total} mL")
        source, target = self._vessel_label(from_vessel), self._vessel_label(to_vessel)
        moved_contents: list = []
        strokes = 0
        remaining = total
        with self._operation("Busy", f"转移 {total} mL：{source or '?'} → {target or '?'}", action_context):
            if self._position > _EPSILON:
                await self._move_to(0.0, dispense_velocity, action_context)
            while remaining > _EPSILON:
                chunk = min(remaining, self.max_volume)
                await self._move_to(chunk, aspirate_velocity, action_context)
                await self._move_to(0.0, dispense_velocity, action_context)
                for name, liquid_volume, unit in self._transfer_liquid(from_vessel, to_vessel, chunk * UL_PER_ML):
                    moved_contents.append({"name": name, "amount": round(liquid_volume, 6), "unit": unit})
                remaining -= chunk
                strokes += 1
        return {
            "success": True,
            "volume": total,
            "strokes": strokes,
            "from_vessel": source,
            "to_vessel": target,
            "moved_contents": moved_contents,
        }

    # ============ 其他柱塞操作 ============

    @action(auto_prefix=True, display_name="设置最大速度", always_free=True)
    def set_max_velocity(self, velocity: float) -> Dict[str, Any]:
        """设置最大柱塞速度。

        Args:
            velocity[速度]: 最大速度（mL/s），限制在 0.1 ~ 50。
        """
        self._max_velocity = self._clamp_velocity(float(velocity))
        return {"success": True, "max_velocity": self._max_velocity}

    @action(auto_prefix=True, display_name="吸液", feedback_interval=0.5)
    async def aspirate(
        self, volume: float, velocity: Optional[float] = None, action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """吸入指定体积。

        Args:
            volume[体积]: 吸入体积（mL）。
            velocity[速度]: 速度（mL/s），不填用当前最大速度。
        """
        return await self.pull_plunger(volume, velocity, action_context=action_context)

    @action(auto_prefix=True, display_name="排液", feedback_interval=0.5)
    async def dispense(
        self, volume: float, velocity: Optional[float] = None, action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """排出指定体积。

        Args:
            volume[体积]: 排出体积（mL）。
            velocity[速度]: 速度（mL/s），不填用当前最大速度。
        """
        return await self.push_plunger(volume, velocity, action_context=action_context)

    @action(auto_prefix=True, display_name="拉柱塞", feedback_interval=0.5)
    async def pull_plunger(
        self, volume: float, velocity: Optional[float] = None, action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """柱塞后拉（吸液）。

        Args:
            volume[体积]: 吸入体积（mL），不能超过剩余容量。
            velocity[速度]: 速度（mL/s），不填用当前最大速度。
        """
        amount = float(volume)
        if amount <= 0:
            self._reject(f"吸液体积必须大于 0，收到 {amount} mL")
        if self._position + amount > self.max_volume + _EPSILON:
            self._reject(f"吸液 {amount} mL 超过剩余容量 {self.max_volume - self._position:.3f} mL")
        moved = await self._move_to(min(self.max_volume, self._position + amount), velocity, action_context)
        return {"success": True, "volume": round(moved, 6), "position": round(self._position, 6)}

    @action(auto_prefix=True, display_name="推柱塞", feedback_interval=0.5)
    async def push_plunger(
        self, volume: float, velocity: Optional[float] = None, action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """柱塞前推（排液）。

        Args:
            volume[体积]: 排出体积（mL），不能超过注射器内现有体积。
            velocity[速度]: 速度（mL/s），不填用当前最大速度。
        """
        amount = float(volume)
        if amount <= 0:
            self._reject(f"排液体积必须大于 0，收到 {amount} mL")
        if amount > self._position + _EPSILON:
            self._reject(f"排液 {amount} mL 超过注射器内现有 {self._position:.3f} mL")
        moved = await self._move_to(max(0.0, self._position - amount), velocity, action_context)
        return {"success": True, "volume": round(moved, 6), "position": round(self._position, 6)}

    @action(auto_prefix=True, display_name="充满注射器", feedback_interval=0.5)
    async def fill_syringe(
        self, velocity: Optional[float] = None, action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """把柱塞拉到满量程。

        Args:
            velocity[速度]: 速度（mL/s），不填用当前最大速度。
        """
        return await self.set_position(self.max_volume, velocity, action_context=action_context)

    @action(auto_prefix=True, display_name="排空注射器", feedback_interval=0.5)
    async def empty_syringe(
        self, velocity: Optional[float] = None, action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """把柱塞推回 0。

        Args:
            velocity[速度]: 速度（mL/s），不填用当前最大速度。
        """
        return await self.set_position(0.0, velocity, action_context=action_context)

    @action(auto_prefix=True, display_name="停止", always_free=True, description="请求取消正在进行的柱塞移动")
    async def stop_operation(self) -> Dict[str, Any]:
        """停止当前操作（柱塞停在当前位置）。"""
        stopped = self._request_stop()
        return {"success": True, "stopped": stopped, "position": round(self._position, 6)}

    @action(auto_prefix=True, display_name="是否为空", always_free=True)
    def is_empty(self) -> bool:
        """注射器是否为空。"""
        return self._position <= 0.01

    @action(auto_prefix=True, display_name="是否已满", always_free=True)
    def is_full(self) -> bool:
        """注射器是否已满。"""
        return self._position >= self.max_volume - 0.01

    # ============ 内部 ============

    @staticmethod
    def _clamp_velocity(velocity: float) -> float:
        return max(_MIN_VELOCITY, min(_MAX_VELOCITY, velocity))

    async def _move_to(
        self, target: float, velocity: Optional[float], action_context: Optional[ActionContext]
    ) -> float:
        """柱塞移动到 ``target``（mL），模拟时长 = 距离 ÷ 速度；返回移动的体积。"""

        speed = self._clamp_velocity(float(velocity)) if velocity else self._max_velocity
        start = self._position
        distance = abs(target - start)
        if distance <= _EPSILON:
            self._position = target
            return 0.0
        kind = "Aspirating" if target > start else "Dispensing"
        label = "吸液" if target > start else "排液"

        def step(fraction: float) -> None:
            self._position = start + (target - start) * fraction

        with self._operation(kind, f"{label} {distance:.3f} mL（{start:.3f} → {target:.3f} mL）", action_context):
            await self._simulate(distance / speed, action_context, on_step=step)
        self._position = target
        self._message = f"柱塞到 {target:.3f} mL"
        return distance

    def __repr__(self) -> str:
        return f"VirtualTransferPump({self.device_id}: {self._position:.3f}/{self.max_volume} mL, {self._status})"
