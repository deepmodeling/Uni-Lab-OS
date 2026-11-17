"""虚拟电磁阀：只有开、关两个状态，切换耗时 ``response_time``。

抽真空 / 充气、氢化等协议发 ``set_valve_position``（OPEN / CLOSED）或 ``set_status``（ON / OFF）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from unilabos.backend.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import VirtualDevice, nested_settings
from unilabos.registry.decorators import (
    DataSource,
    InputHandle,
    OutputHandle,
    Side,
    action,
    device,
    topic_config,
)

_OPEN_COMMANDS = frozenset({"OPEN", "ON", "TRUE", "1"})
_CLOSE_COMMANDS = frozenset({"CLOSED", "CLOSE", "OFF", "FALSE", "0"})


@device(
    id="virtual_solenoid_valve",
    display_name="虚拟电磁阀",
    category=["virtual_device"],
    description="开 / 关两态的虚拟电磁阀，供抽真空充气、氢化等协议与示例图使用",
    supported_backends=["hostlink", "ros2"],
    handles=[
        InputHandle(
            key="in",
            data_type="fluid",
            label="in",
            side=Side.NORTH,
            data_key="fluid_port_in",
            data_source=DataSource.HANDLE,
            description="电磁阀的进液口",
        ),
        OutputHandle(
            key="out",
            data_type="fluid",
            label="out",
            side=Side.SOUTH,
            data_key="fluid_port_out",
            data_source=DataSource.HANDLE,
            description="电磁阀的出液口",
        ),
    ],
)
class VirtualSolenoidValve(VirtualDevice):
    """常闭电磁阀。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        response_time: float = 0.1,
        voltage: float = 12.0,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟电磁阀。

        Args:
            device_id[设备ID]: 设备实例 ID。
            response_time[响应时间]: 开 / 关一次的模拟时间（s）。
            voltage[电压]: 线圈电压（V），仅作显示。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用全局 virtual_time_scale。
        """
        settings = nested_settings(
            kwargs, response_time=response_time, voltage=voltage, port=port, time_scale=time_scale
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_solenoid_valve", settings["time_scale"])
        self.response_time = max(0.0, float(settings["response_time"]))
        self.voltage = float(settings["voltage"])
        self.port = str(settings["port"])
        self._is_open = False

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Busy / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def valve_state(self) -> str:
        """Open / Closed。"""
        return "Open" if self._is_open else "Closed"

    @property
    @topic_config(period=1.0)
    def is_open(self) -> bool:
        """阀门是否打开。"""
        return self._is_open

    @property
    @topic_config(period=1.0)
    def valve_position(self) -> str:
        """OPEN / CLOSED。"""
        return "OPEN" if self._is_open else "CLOSED"

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="关阀并回到空闲")
    async def initialize(self) -> bool:
        """初始化：关阀。"""
        self._is_open = False
        self._status = "Idle"
        return True

    @action(auto_prefix=True, display_name="清理")
    async def cleanup(self) -> bool:
        """清理：回到空闲（阀位不变）。"""
        self._status = "Idle"
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:SendCmd",
        display_name="设置阀位",
        description="OPEN / ON 打开，CLOSED / OFF 关闭",
    )
    async def set_valve_position(
        self, command: str = "", action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """设置阀位。

        Args:
            command[命令]: OPEN / ON / TRUE / 1 打开；CLOSED / CLOSE / OFF / FALSE / 0 关闭。
        """
        text = str(command if command is not None else "").strip().upper()
        if text in _OPEN_COMMANDS:
            target = True
        elif text in _CLOSE_COMMANDS:
            target = False
        else:
            self._reject(f"无法识别的电磁阀命令：{command!r}（OPEN / CLOSED / ON / OFF）")
        with self._operation("Busy", f"{'打开' if target else '关闭'}电磁阀", action_context):
            await self._simulate(self.response_time, action_context)
            self._is_open = target
        self._message = f"电磁阀已{'打开' if target else '关闭'}"
        return {"success": True, "valve_position": self.valve_position, "is_open": self._is_open}

    @action(action_type="unilabos_msgs.action:EmptyIn", display_name="打开")
    async def open(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """打开电磁阀。"""
        return await self.set_valve_position("OPEN", action_context=action_context)

    @action(action_type="unilabos_msgs.action:EmptyIn", display_name="关闭")
    async def close(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """关闭电磁阀。"""
        return await self.set_valve_position("CLOSED", action_context=action_context)

    @action(
        action_type="unilabos_msgs.action:StrSingleInput",
        display_name="设置开关",
        description="ON / OPEN 打开，OFF / CLOSED 关闭",
    )
    async def set_status(self, string: str = "", action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """设置开关状态。

        Args:
            string[状态]: ON / OPEN 打开，OFF / CLOSED 关闭。
        """
        return await self.set_valve_position(string, action_context=action_context)

    # ============ 其他 ============

    @action(auto_prefix=True, display_name="切换")
    async def toggle(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """开关互切。"""
        return await self.set_valve_position("CLOSED" if self._is_open else "OPEN", action_context=action_context)

    @action(auto_prefix=True, display_name="复位", description="关阀")
    async def reset(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """复位：关阀。"""
        return await self.set_valve_position("CLOSED", action_context=action_context)

    @action(auto_prefix=True, display_name="是否关闭", always_free=True)
    def is_closed(self) -> bool:
        """阀门是否关闭。"""
        return not self._is_open

    def __repr__(self) -> str:
        return f"VirtualSolenoidValve({self.device_id}: {self.valve_position})"
