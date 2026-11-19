"""虚拟固体加样器：按质量（或物质的量）向容器加入固体试剂。

加样量优先取 ``mass``，其次 ``mol`` × 摩尔质量（默认 100 g/mol），都没给时按 1 g。模拟时长每克
0.1 秒（至少 0.5 秒）。目标容器有体积跟踪器时，把固体写进容器内容物（PLR 体积跟踪器，质量单位）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from unilabos.client.host.runtime.action import ActionContext
from unilabos.devices.virtual.simulation import (
    UG_PER_G,
    VirtualDevice,
    physical_graph,
    nested_settings,
    parse_amount,
    parse_mass,
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
from unilabos.registry.placeholder_type import ResourceSlot

#: 没给质量与物质的量时的默认加样量（g）
DEFAULT_MASS = 1.0


@device(
    id="virtual_solid_dispenser",
    display_name="虚拟固体分装器",
    category=["virtual_device"],
    description="按质量或物质的量加入固体试剂的虚拟加样器，供加料、溶解等协议与示例图使用",
    supported_backends=["hostlink", "ros2"],
    handles=[
        OutputHandle(
            key="SolidOut",
            data_type="resource",
            label="SolidOut",
            side=Side.SOUTH,
            data_key="solid_out",
            data_source=DataSource.EXECUTOR,
            description="固体试剂输出口",
        ),
        InputHandle(
            key="SolidIn",
            data_type="resource",
            label="SolidIn",
            side=Side.NORTH,
            data_key="solid_in",
            data_source=DataSource.HANDLE,
            description="固体试剂输入口（连接试剂瓶）",
        ),
    ],
)
class VirtualSolidDispenser(VirtualDevice):
    """单次最多加 max_capacity（g），精度 precision（g）。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        max_capacity: float = 100.0,
        precision: float = 0.001,
        molar_mass: float = 100.0,
        time_scale: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟固体加样器。

        Args:
            device_id[设备ID]: 设备实例 ID。
            max_capacity[最大加样量]: 单次最大加样量（g）。
            precision[精度]: 加样精度（g），加样量按它取整。
            molar_mass[摩尔质量]: 按物质的量加样时换算用的摩尔质量（g/mol）。
            time_scale[时间倍率]: 真实等待 = 模拟时长 × 倍率；不填用设备类的默认值（0.001）。
        """
        settings = nested_settings(
            kwargs, max_capacity=max_capacity, precision=precision, molar_mass=molar_mass, time_scale=time_scale
        )
        super().__init__(device_id or kwargs.pop("id", None), "virtual_solid_dispenser", settings["time_scale"])
        self.max_capacity = float(settings["max_capacity"])
        self.precision = float(settings["precision"])
        self.molar_mass = float(settings["molar_mass"])
        if self.max_capacity <= 0 or self.precision <= 0 or self.molar_mass <= 0:
            raise ValueError("max_capacity、precision、molar_mass 都必须大于 0")
        self._status = "Ready"
        self._current_reagent = ""
        self._dispensed_amount = 0.0
        self._total_operations = 0

    # ============ 状态 ============

    @property
    @topic_config(period=1.0)
    def status(self) -> str:
        """Ready / Dispensing / Error。"""
        return self._status

    @property
    @topic_config(period=1.0)
    def current_reagent(self) -> str:
        """最近一次加入的试剂。"""
        return self._current_reagent

    @property
    @topic_config(period=1.0)
    def dispensed_amount(self) -> float:
        """最近一次加样量（g）。"""
        return self._dispensed_amount

    @property
    @topic_config(period=1.0)
    def total_operations(self) -> int:
        """累计加样次数。"""
        return self._total_operations

    # ============ 生命周期 ============

    @action(auto_prefix=True, display_name="初始化", description="回到就绪并清空最近一次记录")
    async def initialize(self) -> bool:
        """初始化：回到就绪。"""
        with self._lock:
            self._status = "Ready"
            self._current_reagent = ""
            self._dispensed_amount = 0.0
        return True

    @action(auto_prefix=True, display_name="清理", description="停止正在进行的加样")
    async def cleanup(self) -> bool:
        """清理：请求停止当前加样。"""
        self._request_stop()
        self._status = "Idle"
        return True

    # ============ 协议编译器使用的动作 ============

    @action(
        action_type="unilabos_msgs.action:Add",
        display_name="加入固体",
        description="按质量（优先）或物质的量向容器加入固体试剂",
        feedback_interval=0.5,
    )
    async def add_solid(
        self,
        vessel: ResourceSlot,
        reagent: str,
        mass: str = "",
        mol: str = "",
        purpose: str = "",
        equiv: str = "",
        event: str = "",
        rate_spec: str = "",
        ratio: str = "",
        amount: str = "",
        volume: str = "",
        time: str = "",
        stir: bool = False,
        stir_speed: float = 0.0,
        viscous: bool = False,
        action_context: Optional[ActionContext] = None,
    ) -> Dict[str, Any]:
        """加入固体试剂。

        Args:
            vessel[目标容器]: 接收固体的容器。
            reagent[试剂]: 固体试剂名。
            mass[质量]: 质量，数字按 g，也可写 "2.5 g"、"500 mg"。
            mol[物质的量]: 物质的量，如 "0.12 mol"、"16 mmol"；没给质量时按摩尔质量换算。
            purpose[目的]: 操作目的，仅记录。
            equiv[当量]: 当量，仅记录。
            event[事件]: 协议事件，仅记录。
            rate_spec[速率规格]: 加料速率规格，仅记录。
            ratio[比例]: 比例，仅记录。
            amount[用量描述]: 协议里的原始用量描述，仅记录。
            volume[体积]: 协议里的体积，仅记录。
            time[时间]: 协议里的加料时间，仅记录。
            stir[搅拌]: 协议里的搅拌标记，仅记录。
            stir_speed[搅拌速度]: 协议里的搅拌速度，仅记录。
            viscous[粘稠]: 协议里的粘稠标记，仅记录。
        """
        if vessel is None:
            self._reject("目标容器不能为空")
        reagent_name = str(reagent or "").strip()
        if not reagent_name:
            self._reject("试剂名不能为空")
        mass_g, amount_mol = parse_mass(mass), parse_amount(mol)
        if mass_g < 0 or amount_mol < 0:
            self._reject(f"加样量不能为负：mass={mass!r}, mol={mol!r}")
        if mass_g > 0:
            grams, basis = mass_g, "mass"
        elif amount_mol > 0:
            grams, basis = amount_mol * self.molar_mass, "mol"
        else:
            grams, basis = DEFAULT_MASS, "default"
            self.logger.warning("[%s] 未给质量或物质的量，按默认 %g g 加样", self.device_id, DEFAULT_MASS)
        grams = round(grams / self.precision) * self.precision
        if grams > self.max_capacity:
            self._reject(f"加样量 {grams:.3f} g 超过单次最大加样量 {self.max_capacity:g} g")
        label = self._vessel_label(vessel)
        with self._operation("Dispensing", f"向 {label} 加入 {reagent_name} {grams:.3f} g", action_context, idle_status="Ready"):
            self._current_reagent = reagent_name
            await self._simulate(max(0.5, grams * 0.1), action_context, feedback={"reagent": reagent_name})
            added = self._add_to_vessel(vessel, reagent_name, grams * UG_PER_G, "ug")
            self._dispensed_amount = grams
            self._total_operations += 1
        self._message = f"已向 {label} 加入 {reagent_name} {grams:.3f} g"
        return {
            "success": True,
            "vessel": label,
            "reagent": reagent_name,
            "dispensed_amount": grams,
            "basis": basis,
            "recorded_in_vessel": added > 0,
            "reagent_bottle": self.find_solid_reagent_bottle(reagent_name),
        }

    # ============ 辅助查询 ============

    @action(auto_prefix=True, display_name="解析质量", always_free=True)
    def parse_mass_string(self, mass_str: str) -> float:
        """把质量字符串换算成 g。

        Args:
            mass_str[质量]: 如 "2.9 g"、"4.5 mg"、"1.2 kg"；空串为 0。
        """
        return parse_mass(mass_str)

    @action(auto_prefix=True, display_name="解析物质的量", always_free=True)
    def parse_mol_string(self, mol_str: str) -> float:
        """把物质的量字符串换算成 mol。

        Args:
            mol_str[物质的量]: 如 "0.12 mol"、"16.2 mmol"；空串为 0。
        """
        return parse_amount(mol_str)

    @action(auto_prefix=True, display_name="查找固体试剂瓶", always_free=True)
    def find_solid_reagent_bottle(self, reagent_name: str) -> str:
        """在设备图里找装该固体试剂的瓶子，找不到时给出约定的瓶名。

        Args:
            reagent_name[试剂名]: 固体试剂名。
        """
        name = str(reagent_name or "").strip()
        if not name:
            return "unknown_solid_bottle"
        graph = physical_graph()
        if graph is not None:
            for node_id, data in graph.nodes(data=True):
                payload = data.get("data") or {}
                if str(payload.get("reagent_name") or "").lower() == name.lower():
                    return str(node_id)
        return f"solid_bottle_{name}"

    def __repr__(self) -> str:
        return f"VirtualSolidDispenser({self.device_id}: {self._status}, 最近 {self._dispensed_amount:.3f} g)"
