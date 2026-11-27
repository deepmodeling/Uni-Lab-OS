"""OS 本地工作流命令的静态类型合同；不注册设备，也不直接执行作者源码。"""

from __future__ import annotations

from typing import Annotated, TypedDict

from unilabos.registry.annotations import JSONValue, SiteSelector
from unilabos.registry.placeholder_type import ResourceSlot


class CarrierTransferSnapshot(TypedDict):
    material: ResourceSlot
    carrier_uuid: str
    carrier_barcode: str
    item_site: str
    carrier_owner_uuid: str
    carrier_site: str
    target_occupant_uuid: str
    target_occupant_barcode: str
    foreign_content_claims: int


class CountReconciliation(TypedDict):
    previous_count: int
    measured_count: int
    changed: bool
    version: int


class CountSnapshot(TypedDict):
    lot_id: str
    mount_uuid: str
    count: int
    version: int


class MaterialUse(TypedDict):
    material: ResourceSlot
    previous_status: str
    status: str


class MaterialLocation(TypedDict):
    material: ResourceSlot
    material_name: str
    owner: ResourceSlot
    owner_uuid: str
    owner_name: str
    site_uuid: str
    site_name: str


class SiteOccupancy(TypedDict):
    occupied: bool
    occupant: ResourceSlot
    occupant_uuid: str
    occupant_name: str
    site_uuid: str


class ProducedMaterial(TypedDict):
    material: ResourceSlot


class InventoryCommands:
    """由任务桥在 OS 库存权威上执行的库存命令：计数盘点、位置读取与使用标记。"""

    def read_count(self, *, lot_id: str, mount_uuid: str) -> CountSnapshot:
        """测量前冻结库存版本，后续盘点拒绝覆盖测量期间的库存变化。"""
        raise RuntimeError("库存命令只能由 OS 工作流调度器执行")

    def reconcile_count(
        self,
        *,
        lot_id: str,
        mount_uuid: str,
        measured_count: int,
        expected_version: int,
    ) -> CountReconciliation:
        """按成功的上游测量核对库存；存在预留或实例差异时进入人工复核。"""
        raise RuntimeError("库存命令只能由 OS 工作流调度器执行")

    def inspect_carrier_transfer(
        self, *, material: ResourceSlot, target_owner: ResourceSlot, target_site: str
    ) -> CarrierTransferSnapshot:
        """读取已准入物料的容器、实际孔位和目标容器的他任务占用；不选料、不搬运。"""
        raise RuntimeError("库存命令只能由 OS 工作流调度器执行")

    def mark_used(self, *, material: ResourceSlot, reason: str = "") -> MaterialUse:
        """把本任务独占准入的一件物料标为已使用：留在原库位，此后自动选料跳过它。

        放在真正使用它的动作之后，依赖该动作成功才执行；重放按作业身份幂等。
        """
        raise RuntimeError("库存命令只能由 OS 工作流调度器执行")

    def locate(self, *, material: ResourceSlot) -> MaterialLocation:
        """读取已准入物料当前所在的库位及其所属物料（容器/载架/设备）；只读。"""
        raise RuntimeError("库存命令只能由 OS 工作流调度器执行")

    def site_occupant(self, *, owner: ResourceSlot, site: str) -> SiteOccupancy:
        """读取某个库位当前放着什么（例如换位前先取出中转位上的旧托盘）；只读。

        空库位时 ``occupied`` 为假、``occupant`` 为空引用，下游须按 ``occupied`` 分支使用。
        """
        raise RuntimeError("库存命令只能由 OS 工作流调度器执行")

    def record_produced(
        self, *, owner: ResourceSlot,
        site: Annotated[str, SiteSelector(owner="owner", show_occupied=True, allow_occupied=False)],
        template_uuid: str, barcode: str, evidence: str, simulation: bool,
    ) -> ProducedMaterial:
        """设备产样成功后登记真实物料；条码、证据与模拟标记必须来自同一成功动作。"""
        raise RuntimeError("库存命令只能由 OS 工作流调度器执行")


class HumanChoice(TypedDict):
    choice: str


class HumanText(TypedDict):
    text: str


class HumanNumbers(TypedDict):
    values: list[float]


class HumanSketch(TypedDict):
    x: list[float]
    y: list[float]
    corner_x: list[float]
    corner_y: list[float]


class HumanReview(TypedDict):
    choice: str
    text: str
    values: list[float]
    x: list[float]
    y: list[float]
    corner_x: list[float]
    corner_y: list[float]


class HumanFormResult(TypedDict):
    answers: dict[str, JSONValue]


class HumanCommands:
    """持久、逐 Job 的人工交互；结果只能由操作者提交，不占用虚拟设备。"""

    def form(
        self,
        *,
        prompt: str,
        fields: list[dict[str, JSONValue]],
        image: str = "",
        timeout_seconds: int = 86400,
    ) -> HumanFormResult:
        """人工判定：填写命名字段，供后续分支或循环读取。

        fields 中每项声明 name、label、kind；kind 可为 choice、text、number、
        boolean。required 缺省为 True；choice 使用 options 和可选 labels，
        number 可声明 minimum/maximum，各类型可声明 default。返回 answers
        对象，可用 decision.answers["outcome"] 等表达式读取。超时沿用人工
        交互的错误处置流程，不会生成默认判定结果。
        """
        raise RuntimeError("人工交互只能由 OS 工作流调度器执行")

    def choose(
        self,
        *,
        prompt: str,
        options: list[str],
        labels: list[str],
        image: str = "",
        timeout_seconds: int = 86400,
    ) -> HumanChoice:
        raise RuntimeError("人工交互只能由 OS 工作流调度器执行")

    def text(
        self,
        *,
        prompt: str,
        default: str = "",
        image: str = "",
        timeout_seconds: int = 86400,
    ) -> HumanText:
        raise RuntimeError("人工交互只能由 OS 工作流调度器执行")

    def numbers(
        self,
        *,
        prompt: str,
        labels: list[str],
        defaults: list[float],
        image: str = "",
        timeout_seconds: int = 86400,
    ) -> HumanNumbers:
        raise RuntimeError("人工交互只能由 OS 工作流调度器执行")

    def sketch(
        self, *, prompt: str, image: str, timeout_seconds: int = 86400
    ) -> HumanSketch:
        """返回图片原始像素坐标；闭合轮廓与可选的四角分别为 X/Y 数组。"""
        raise RuntimeError("人工交互只能由 OS 工作流调度器执行")

    def review(
        self,
        *,
        prompt: str,
        options: list[str],
        labels: list[str],
        forms: list[str],
        image: str,
        text_default: str,
        number_labels: list[str],
        number_defaults: list[float],
        timeout_seconds: int = 86400,
    ) -> HumanReview:
        """选项可带 none、text、numbers 或 sketch 表单；算法仍由后继设备动作执行。"""
        raise RuntimeError("人工交互只能由 OS 工作流调度器执行")


COMMAND_CLASSES = {
    "unilabos.workflow.native_commands:HumanCommands": HumanCommands,
    "unilabos.workflow.native_commands:InventoryCommands": InventoryCommands,
}
