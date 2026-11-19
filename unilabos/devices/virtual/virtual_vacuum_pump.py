"""虚拟真空泵：开 / 关两态，抽真空充气、氢化等协议发 ``set_status``（ON / OFF）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from unilabos.client.host.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import VirtualDevice, nested_settings
from unilabos.registry.decorators import DataSource, OutputHandle, Side, action, device, topic_config

_ON_COMMANDS = frozenset({"ON", "OPEN"})
_OFF_COMMANDS = frozenset({"OFF", "CLOSED", "CLOSE"})


@device(
    id="virtual_vacuum_pump",
    display_name="虚拟真空泵",
    category=["virtual_device"],
    description="开 / 关两态的虚拟真空泵，供抽真空充气、氢化等协议与示例图使用",
    icon="Vacuum.webp",
    supported_backends=["hostlink", "ros2"],
    handles=[
        OutputHandle(
            key="vacuumpump",
            data_type="fluid",
            label="vacuumpump",
            side=Side.SOUTH,
            data_key="fluid_in",
            data_source=DataSource.HANDLE,
            description="真空泵进气口",
        ),
    ],
)
class VirtualVacuumPump(VirtualDevice):
    """开（ON）/ 关（OFF）两态，切换耗时 switch_time。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        switch_time: float = 5.0,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化真空泵。

        Args:
            device_id[设备ID]: 设备实例 ID。
            switch_time[切换时间]: 开 / 关一次的模拟时间（s）。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用设备类的默认值（0.001）。
        """
        settings = nested_settings(
            kwargs, switch_time=switch_time, port=port, time_scale=time_scale
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_vacuum_pump", settings["time_scale"])
        self.switch_time = max(0.0, float(settings["switch_time"]))
        self.port = str(settings["port"])
        self._state = "OFF"

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """ON（开）/ OFF（关）。"""
        return self._state

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="关闭并回到空闲")
    async def initialize(self) -> bool:
        """初始化：关闭。"""
        self._state = "OFF"
        return True

    @action(auto_prefix=True, display_name="清理")
    async def cleanup(self) -> bool:
        """清理：回到空闲（开关状态不变）。"""
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:StrSingleInput",
        display_name="设置开关",
        description="ON / OPEN 打开，OFF / CLOSED 关闭",
    )
    async def set_status(self, string: str, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """设置开关状态。

        Args:
            string[状态]: ON / OPEN 打开，OFF / CLOSED 关闭。
        """
        command = str(string if string is not None else "").strip().upper()
        if command in _ON_COMMANDS:
            target = "ON"
        elif command in _OFF_COMMANDS:
            target = "OFF"
        else:
            self._reject(f"无法识别的开关命令：{string!r}（ON / OFF / OPEN / CLOSED）")
        with self._operation("Busy", f"真空泵切换到 {target}", action_context):
            await self._simulate(self.switch_time, action_context)
            self._state = target
        self._message = f"真空泵已{'打开' if target == 'ON' else '关闭'}"
        return {"success": True, "status": self._state}

    @action(action_type="unilabos_msgs.action:EmptyIn", display_name="打开")
    async def open(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """打开。"""
        return await self.set_status("ON", action_context=action_context)

    @action(action_type="unilabos_msgs.action:EmptyIn", display_name="关闭")
    async def close(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """关闭。"""
        return await self.set_status("OFF", action_context=action_context)

    @action(auto_prefix=True, display_name="是否打开", always_free=True)
    def is_open(self) -> bool:
        """是否打开。"""
        return self._state == "ON"

    @action(auto_prefix=True, display_name="是否关闭", always_free=True)
    def is_closed(self) -> bool:
        """是否关闭。"""
        return self._state == "OFF"

    def __repr__(self) -> str:
        return f"VirtualVacuumPump({self.device_id}: {self._state})"
