"""虚拟多通阀：0 号位接注射泵，1 ~ N 号位接容器或其他设备。

协议编译器发 ``set_valve_position``，命令是图里连线在阀一侧的端口名——通常是 "1" ~ "8"，
接泵的端口叫 ``transferpump``（等同 0 号位）。切换的模拟时长按转过的位置数计算。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

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

#: 命令里表示 0 号位（接注射泵）的写法
_PUMP_ALIASES = frozenset({"0", "default", "pump", "transfer_pump", "transferpump", "home", "open"})
_CLOSE_ALIASES = frozenset({"close", "closed"})


@device(
    id="virtual_multiway_valve",
    display_name="虚拟八通阀",
    category=["virtual_device"],
    description="0 号位接注射泵、1 ~ 8 号位接容器的虚拟多通阀，供泵送类协议与示例图使用",
    icon="EightPipeline.webp",
    supported_backends=["hostlink", "ros2"],
    handles=[
        InputHandle(
            key="transferpump",
            data_type="fluid",
            label="transferpump",
            side=Side.NORTH,
            data_key="fluid_in",
            data_source=DataSource.HANDLE,
            description="八通阀门进液口",
        ),
        OutputHandle(
            key="1",
            data_type="fluid",
            label="1",
            side=Side.NORTH,
            data_key="fluid_port_1",
            data_source=DataSource.EXECUTOR,
            description="八通阀门端口1",
        ),
        OutputHandle(
            key="2",
            data_type="fluid",
            label="2",
            side=Side.EAST,
            data_key="fluid_port_2",
            data_source=DataSource.EXECUTOR,
            description="八通阀门端口2",
        ),
        OutputHandle(
            key="3",
            data_type="fluid",
            label="3",
            side=Side.EAST,
            data_key="fluid_port_3",
            data_source=DataSource.EXECUTOR,
            description="八通阀门端口3",
        ),
        OutputHandle(
            key="4",
            data_type="fluid",
            label="4",
            side=Side.SOUTH,
            data_key="fluid_port_4",
            data_source=DataSource.EXECUTOR,
            description="八通阀门端口4",
        ),
        OutputHandle(
            key="5",
            data_type="fluid",
            label="5",
            side=Side.SOUTH,
            data_key="fluid_port_5",
            data_source=DataSource.EXECUTOR,
            description="八通阀门端口5",
        ),
        OutputHandle(
            key="6",
            data_type="fluid",
            label="6",
            side=Side.WEST,
            data_key="fluid_port_6",
            data_source=DataSource.EXECUTOR,
            description="八通阀门端口6",
        ),
        OutputHandle(
            key="7",
            data_type="fluid",
            label="7",
            side=Side.WEST,
            data_key="fluid_port_7",
            data_source=DataSource.EXECUTOR,
            description="八通阀门端口7",
        ),
        InputHandle(
            key="8",
            data_type="fluid",
            label="8",
            side=Side.WEST,
            data_key="fluid_port_8",
            data_source=DataSource.EXECUTOR,
            description="八通阀门端口8-特殊输入",
        ),
        OutputHandle(
            key="8",
            data_type="fluid",
            label="8",
            side=Side.NORTH,
            data_key="fluid_port_8",
            data_source=DataSource.EXECUTOR,
            description="八通阀门端口8",
        ),
    ],
)
class VirtualMultiwayValve(VirtualDevice):
    """阀位 0 ~ positions：0 接注射泵，其余接端口。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        positions: int = 8,
        switch_time: float = 0.5,
        port: str = "VIRTUAL",
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟多通阀。

        Args:
            device_id[设备ID]: 设备实例 ID。
            positions[端口数]: 端口数量（不含 0 号泵位），默认 8。
            switch_time[切换时间]: 每转过一个位置的模拟时间（s）。
            port[端口]: 模拟的通信端口名，仅作显示。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用设备类的默认值（0.001）。
        """
        settings = nested_settings(
            kwargs, positions=positions, switch_time=switch_time, port=port, time_scale=time_scale
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_multiway_valve", settings["time_scale"])
        self.max_positions = int(settings["positions"])
        if self.max_positions < 1:
            raise ValueError(f"positions 必须至少为 1，收到 {self.max_positions}")
        self.switch_time = max(0.0, float(settings["switch_time"]))
        self.port = str(settings["port"])
        self._valve_state = "Ready"
        self._current_position = 0
        self._target_position = 0

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Idle / Busy / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def valve_state(self) -> str:
        """Ready / Moving / Closed / Error。"""
        return self._valve_state

    @property
    @topic_config(period=1.0)
    def current_position(self) -> int:
        """当前阀位（0 为泵位）。"""
        return self._current_position

    @property
    @topic_config(period=1.0)
    def target_position(self) -> int:
        """正在切往的阀位。"""
        return self._target_position

    @property
    @topic_config(period=1.0)
    def valve_position(self) -> int:
        """当前阀位，与 current_position 相同。"""
        return self._current_position

    @property
    @topic_config(period=5.0)
    def current_port(self) -> str:
        """模拟的通信端口名。"""
        return self.port

    @property
    @topic_config(period=1.0)
    def flow_path(self) -> str:
        """当前流路说明。"""
        if self._valve_state == "Closed":
            return f"阀门关闭（停在位置 {self._current_position}）"
        if self._current_position == 0:
            return "泵位：注射泵已连通"
        return f"端口 {self._current_position} 已连通"

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:SendCmd",
        display_name="切换阀位",
        description="切换到 0 ~ N 号位；也接受 transferpump / pump / default（0 号位）与 close",
    )
    def set_position(
        self, command: Union[int, str], action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """切换阀位。

        Args:
            command[目标位置]: 0 ~ N 的位置号或端口名；transferpump / pump / default 表示 0 号泵位，close 关闭阀门。
        """
        if isinstance(command, str) and command.strip().lower() in _CLOSE_ALIASES:
            return self.close(action_context=action_context)
        position = self._parse_position(command)
        self._switch(position, action_context)
        return {
            "success": True,
            "position": self._current_position,
            "flow_path": self.flow_path,
        }

    @action(
        action_type="unilabos_msgs.action:SendCmd",
        display_name="切换阀位（协议）",
        description="与 set_position 相同；泵送协议按连线端口名调用",
    )
    def set_valve_position(
        self, command: Union[int, str], action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """切换阀位（泵送协议调用的名字）。

        Args:
            command[目标位置]: 0 ~ N 的位置号或端口名；transferpump / pump / default 表示 0 号泵位。
        """
        return self.set_position(command, action_context=action_context)

    # ============ 其他操作 ============

    @action(auto_prefix=True, display_name="切到泵位")
    def set_to_pump_position(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """切换到 0 号泵位。"""
        return self.set_position(0, action_context=action_context)

    @action(auto_prefix=True, display_name="切到端口")
    def set_to_port(self, port_number: int, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """切换到指定端口。

        Args:
            port_number[端口号]: 端口号（1 ~ N）。
        """
        if not 1 <= int(port_number) <= self.max_positions:
            self._reject(f"端口号必须在 1 ~ {self.max_positions} 之间，收到 {port_number}")
        return self.set_position(int(port_number), action_context=action_context)

    @action(auto_prefix=True, display_name="泵位 / 端口互切")
    def switch_between_pump_and_port(
        self, port_number: int, action_context: Optional[ActionContext] = None
    ) -> Dict[str, Any]:
        """在泵位时切到指定端口，否则切回泵位。

        Args:
            port_number[端口号]: 目标端口号（1 ~ N）。
        """
        if self._current_position == 0:
            return self.set_to_port(port_number, action_context=action_context)
        return self.set_to_pump_position(action_context=action_context)

    @action(auto_prefix=True, display_name="打开（泵位）")
    def open(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """打开阀门并切到 0 号泵位。"""
        return self.set_position(0, action_context=action_context)

    @action(auto_prefix=True, display_name="关闭")
    def close(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """关闭阀门，阀位保持不变。"""
        with self._operation("Busy", "关闭阀门", action_context):
            self._valve_state = "Closing"
            self._simulate_blocking(self.switch_time, action_context)
            self._valve_state = "Closed"
        self._message = f"阀门已关闭，停在位置 {self._current_position}"
        return {"success": True, "position": self._current_position, "valve_state": self._valve_state}

    @action(auto_prefix=True, display_name="复位")
    def reset(self, action_context: Optional[ActionContext] = None) -> Dict[str, Any]:
        """复位到 0 号泵位。"""
        return self.set_position(0, action_context=action_context)

    @action(auto_prefix=True, display_name="是否在该位置", always_free=True)
    def is_at_position(self, position: int) -> bool:
        """当前是否在指定阀位。

        Args:
            position[位置]: 阀位号（0 ~ N）。
        """
        return self._current_position == int(position)

    @action(auto_prefix=True, display_name="是否在泵位", always_free=True)
    def is_at_pump_position(self) -> bool:
        """当前是否在 0 号泵位。"""
        return self._current_position == 0

    @action(auto_prefix=True, display_name="是否在该端口", always_free=True)
    def is_at_port(self, port_number: int) -> bool:
        """当前是否在指定端口。

        Args:
            port_number[端口号]: 端口号（1 ~ N）。
        """
        return self._current_position == int(port_number)

    # ============ 内部 ============

    def _parse_position(self, command: Union[int, str]) -> int:
        if isinstance(command, bool):
            self._reject(f"无法识别的阀位命令：{command!r}")
        if isinstance(command, (int, float)):
            position = int(command)
        else:
            text = str(command).strip().lower()
            if text in _PUMP_ALIASES:
                position = 0
            else:
                try:
                    position = int(float(text))
                except ValueError:
                    self._reject(f"无法识别的阀位命令：{command!r}")
        if not 0 <= position <= self.max_positions:
            self._reject(f"阀位必须在 0 ~ {self.max_positions} 之间，收到 {command!r}")
        return position

    def _switch(self, position: int, action_context: Optional[ActionContext]) -> None:
        start = self._current_position
        self._target_position = position
        with self._operation("Busy", f"阀位 {start} → {position}", action_context):
            self._valve_state = "Moving"
            try:
                self._simulate_blocking(abs(position - start) * self.switch_time, action_context)
            finally:
                # 取消时阀芯停在原位
                self._valve_state = "Ready"
            self._current_position = position
            self._valve_state = "Ready"
        self._message = f"阀位 {position}（{self.flow_path}）"

    def __repr__(self) -> str:
        return f"VirtualMultiwayValve({self.device_id}: 位置 {self._current_position}/{self.max_positions}, {self._status})"
