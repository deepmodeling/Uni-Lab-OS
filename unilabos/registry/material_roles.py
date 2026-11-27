"""动作的资源语义：``@action(material_contract=...)`` 唯一的写法（实验性）。

``material_contract=`` 的值是 ``MaterialRole`` 的一个方法调用，或多个调用组成的列表：

    from unilabos.registry.decorators import action, MaterialRole

    @action(material_contract=MaterialRole.move("plate", to_site="target_site"))
    def move_plate(self, plate: ResourceSlot, target_site: SiteSlot) -> dict: ...

    @action(material_contract=MaterialRole.dispense(from_material="source", to_materials=["target"]))
    def pipette(self, source: ResourceSlot, target: ResourceSlot, volume: float) -> dict: ...

物料动作：``move`` / ``pick`` / ``place`` / ``dispense``。不代表物料动作的声明：``load`` / ``unload`` 跨动作
设备独占、``in_place`` 原位检查、``site_picker`` 库位选择器提示、``order_sensitive`` 顺序约束。整个
``material_contract=`` 字段都是实验性功能：写了就按定义告警一次，语义与写法以后可能调整。

字符串参数都是动作的参数名，注册时对照签名检查：物料是 ``ResourceSlot``，库位是 ``SiteSlot``（库位 uuid），
设备是 ``DeviceSlot``（省略即执行设备）。内部规范化成第 2 版资源合同
（``x-unilabos-action-contract.resource_contract``），调度器照旧按合同执行；``site_picker`` 不进资源合同，
写进库位参数 schema 的 ``x-unilabos-site-selector`` 扩展。

类名是 ``MaterialRole``（区别于运行时的物料操作模块 ``unilabos.resources.materials``）。旧写法（类名 ``Material``，
字段名 ``material=`` / ``materials=``，``transfer`` / ``aliquot`` / ``site`` / ``uses``，带 ``owner`` / ``site`` /
``carriers`` 的 ``pick`` / ``place``）直接报错并给出新写法。

本模块只依赖标准库与纯声明的合同规范化：装饰器（导入期）与 AST 扫描（不导入作者模块）共用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from unilabos.registry.action_resource_contract import (
    EXECUTING_DEVICE,
    ActionResourceContractError,
    normalize_action_resource_contract,
)

#: 只能出现一次的语义 → 合同字段。
_SINGLE_FIELDS = {
    "load": "device_tenancy",
    "unload": "device_tenancy",
    "in_place": "operate_in_place",
    "pick": "transfer_step",
    "place": "transfer_step",
    "move": "transfer",
    "dispense": "aliquot",
    "order_sensitive": "order_sensitive",
}
#: 不进资源合同、写进参数 schema 扩展的语义。
_SCHEMA_KINDS = frozenset({"site_picker"})
#: 搬运设备上夹爪库位的固定角色。
GRIPPER_ROLE = "gripper"
#: ``site_picker`` 的已占用库位策略 → ``(show_occupied, allow_occupied)``。
_OCCUPIED = {"hide": (False, False), "show": (True, False), "allow": (True, True)}
#: AST 扫描认可的 ``MaterialRole`` 来源模块（``module:MaterialRole.method``）。
_MATERIAL_MODULES = ("unilabos.registry.decorators", "unilabos.registry.material_roles")


class MaterialRoleError(ValueError):
    """``material_contract=`` 写法不对：注册失败并指出正确写法。"""


def _names(value: Union[str, Sequence[str], None], label: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and all(isinstance(item, str) for item in value):
        return list(value)
    raise MaterialRoleError(f"{label} 必须是参数名或参数名列表")


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MaterialRoleError(f"{label} 必须是动作的参数名")
    return value


def _optional(value: Any, label: str) -> str:
    return "" if value is None else _name(value, label)


def _legacy(message: str) -> MaterialRoleError:
    return MaterialRoleError(f"{message}（资源语义辅助 API 已重做，见 docs/developer_guide/action_standard.md「不兼容变更」）")


@dataclass(frozen=True)
class MaterialRole:
    """一条资源语义（不可变，按字段规范化成合同片段），也是写资源语义的唯一入口。

    每个工厂方法（``MaterialRole.move(...)`` 等）返回一条语义；``@action(material_contract=...)`` 接收一条或一个列表
    （实验性）。
    """

    kind: str
    fields: Tuple[Tuple[str, Any], ...] = ()

    def values(self) -> Dict[str, Any]:
        return dict(self.fields)

    def contract_part(self) -> Dict[str, Any]:
        values = self.values()
        if self.kind in {"load", "unload"}:
            return {
                "device_tenancy": {
                    "mode": "task_while_loaded",
                    "material_param": values["material"],
                    "acquire_device_param": values.get("acquire") or "",
                    "release_device_param": values.get("release") or "",
                }
            }
        if self.kind == "in_place":
            return {"operate_in_place": {"material_param": values["material"]}}
        if self.kind in {"pick", "place"}:
            carrier = values.get("carrier_device") or ""
            return {
                "transfer_step": {
                    "operation": self.kind,
                    "material_param": values["material"],
                    "owner_param": "",
                    "site_param": values.get("site") or "",
                    "carrier_params": [carrier] if carrier else [],
                }
            }
        if self.kind == "move":
            transfer = {
                "material_param": values["material"],
                "source_owner_param": "",
                "source_site_uuid_param": values.get("from_site") or "",
                "source_site_name_param": "",
                "target_owner_param": "",
                "target_site_uuid_param": values["to_site"],
                "target_site_name_param": "",
                "gripper_site_role": GRIPPER_ROLE,
            }
            if values.get("carrier_device"):
                transfer["carrier_device_param"] = values["carrier_device"]
            return {"transfer": transfer}
        if self.kind == "dispense":
            return {
                "aliquot": {
                    "source_material_param": values["from_material"],
                    "target_material_params": list(values["to_materials"]),
                }
            }
        if self.kind == "order_sensitive":
            return {"order_sensitive": True}
        raise MaterialRoleError(f"未知的资源语义 {self.kind}")

    def site_selector(self) -> Tuple[str, Dict[str, Any]]:
        """``MaterialRole.site_picker(...)`` → ``(库位参数名, x-unilabos-site-selector 扩展)``。"""

        if self.kind != "site_picker":
            raise MaterialRoleError(f"MaterialRole.{self.kind} 不是库位选择器提示")
        values = self.values()
        show, allow = _OCCUPIED[values["occupied"]]
        return values["site"], {
            "version": 1,
            "owner": values.get("of"),
            "occupant": values.get("for_material"),
            "show_occupied": show,
            "allow_occupied": allow,
        }

    def parameter_types(self) -> List[Tuple[str, str, str]]:
        """这条语义引用的参数及要求的类型：``[(参数名, "resource" | "site" | "device" | "owner", 说明)]``。

        ``owner`` 表示 ``ResourceSlot`` 或 ``DeviceSlot`` 都可以（``site_picker`` 的 ``of``）。
        """

        values = self.values()
        label = f"MaterialRole.{self.kind}"
        items: List[Tuple[str, str, str]] = []
        if self.kind in {"load", "unload", "in_place", "move", "pick", "place"}:
            items.append((values["material"], "resource", f"{label} 的物料"))
        if self.kind == "move":
            items.append((values["to_site"], "site", f"{label} 的 to_site"))
            if values.get("from_site"):
                items.append((values["from_site"], "site", f"{label} 的 from_site"))
        if self.kind in {"pick", "place"} and values.get("site"):
            items.append((values["site"], "site", f"{label} 的 {'from_site' if self.kind == 'pick' else 'to_site'}"))
        if self.kind in {"move", "pick", "place"} and values.get("carrier_device"):
            items.append((values["carrier_device"], "device", f"{label} 的 carrier_device"))
        if self.kind in {"load", "unload"}:
            for key, keyword in (("acquire", "device"), ("release", "from_device" if self.kind == "load" else "device")):
                name = values.get(key)
                if name and name != EXECUTING_DEVICE:
                    items.append((name, "device", f"{label} 的 {keyword}"))
        if self.kind == "dispense":
            items.append((values["from_material"], "resource", f"{label} 的 from_material"))
            items.extend((name, "resource", f"{label} 的 to_materials") for name in values["to_materials"])
        if self.kind == "site_picker":
            items.append((values["site"], "site", f"{label} 的库位参数"))
            if values.get("of"):
                items.append((values["of"], "owner", f"{label} 的 of"))
            if values.get("for_material"):
                items.append((values["for_material"], "resource", f"{label} 的 for_material"))
        return items

    # ------------------------------------------------------------------ 物料动作

    @staticmethod
    def move(
        material: str,
        *,
        to_site: str,
        from_site: Optional[str] = None,
        carrier_device: Optional[str] = None,
    ) -> MaterialRole:
        """一个动作把容器 ``material`` 搬到库位 ``to_site``。

        ``to_site`` / ``from_site`` 是 ``SiteSlot`` 参数（库位 uuid），所属方（载架或设备根节点）由库位决定；
        ``from_site`` 省略时用权威当前位置，给出时必须与权威一致。``carrier_device`` 是 ``DeviceSlot`` 参数，
        省略时就是执行设备；搬运途中物料在该设备角色为 ``gripper`` 的夹爪库位上。
        """

        fields: List[Tuple[str, Any]] = [
            ("material", _name(material, "MaterialRole.move 的物料")),
            ("to_site", _name(to_site, "MaterialRole.move 的 to_site")),
        ]
        if from_site is not None:
            fields.append(("from_site", _name(from_site, "MaterialRole.move 的 from_site")))
        if carrier_device is not None:
            fields.append(("carrier_device", _name(carrier_device, "MaterialRole.move 的 carrier_device")))
        names = [value for _, value in fields]
        if len(set(names)) != len(names):
            raise MaterialRoleError("MaterialRole.move 的物料、库位与搬运设备必须是不同的参数")
        return MaterialRole("move", tuple(fields))

    @staticmethod
    def pick(
        material: str,
        *,
        from_site: Optional[str] = None,
        carrier_device: Optional[str] = None,
        **legacy: Any,
    ) -> MaterialRole:
        """两步搬运第一步：从库位 ``from_site``（省略时为权威当前位置）抓到搬运设备的 ``gripper`` 夹爪库位。

        ``carrier_device`` 是 ``DeviceSlot`` 参数，省略时就是执行设备；抓取成功后任务托管搬运设备，直到 ``place``。
        """

        if legacy:
            raise _legacy(
                "MaterialRole.pick 不再接受 owner / site / carriers / gripper：写成 "
                "MaterialRole.pick(\"物料参数\", from_site=\"SiteSlot 库位参数\"（可省略）, carrier_device=\"DeviceSlot 设备参数\"（可省略，即执行设备）)"
            )
        return MaterialRole._step("pick", material, from_site, carrier_device)

    @staticmethod
    def place(
        material: str,
        *,
        to_site: Optional[str] = None,
        carrier_device: Optional[str] = None,
        **legacy: Any,
    ) -> MaterialRole:
        """两步搬运第二步：从搬运设备的 ``gripper`` 夹爪库位放到库位 ``to_site``，结束对搬运设备的托管。"""

        if legacy or to_site is None:
            raise _legacy(
                "MaterialRole.place 写成 MaterialRole.place(\"物料参数\", to_site=\"SiteSlot 库位参数\", "
                "carrier_device=\"DeviceSlot 设备参数\"（可省略，即执行设备）)，不再接受 owner / site / carriers / gripper"
            )
        return MaterialRole._step("place", material, to_site, carrier_device)

    @staticmethod
    def _step(kind: str, material: str, site: Optional[str], carrier_device: Optional[str]) -> MaterialRole:
        keyword = "from_site" if kind == "pick" else "to_site"
        fields: List[Tuple[str, Any]] = [("material", _name(material, f"MaterialRole.{kind} 的物料"))]
        if site is not None:
            fields.append(("site", _name(site, f"MaterialRole.{kind} 的 {keyword}")))
        if carrier_device is not None:
            fields.append(("carrier_device", _name(carrier_device, f"MaterialRole.{kind} 的 carrier_device")))
        names = [value for _, value in fields]
        if len(set(names)) != len(names):
            raise MaterialRoleError(f"MaterialRole.{kind} 的物料、库位与搬运设备必须是不同的参数")
        return MaterialRole(kind, tuple(fields))

    @staticmethod
    def dispense(*, from_material: str, to_materials: Union[str, Sequence[str]]) -> MaterialRole:
        """按驱动回报的实际量，把内容物从 ``from_material`` 转移到 ``to_materials``（``AliquotReceipt`` 结算）。"""

        names = _names(to_materials, "MaterialRole.dispense 的 to_materials")
        if not names:
            raise MaterialRoleError("MaterialRole.dispense 至少需要一个 to_materials")
        source = _name(from_material, "MaterialRole.dispense 的 from_material")
        targets = tuple(_name(item, "MaterialRole.dispense 的 to_materials") for item in names)
        if source in targets or len(set(targets)) != len(targets):
            raise MaterialRoleError("MaterialRole.dispense 的来源与目标必须是不同的参数")
        return MaterialRole("dispense", (("from_material", source), ("to_materials", targets)))

    # ------------------------------------------------------------------ 不代表物料动作的声明

    @staticmethod
    def load(material: str, *, device: Optional[str] = None, from_device: Optional[str] = None) -> MaterialRole:
        """把物料装进设备 ``device``（``DeviceSlot`` 参数，省略即执行设备），任务托管设备直到 ``unload``。

        ``from_device``：同时从另一台设备（``DeviceSlot`` 参数）转出。
        """

        acquire = _optional(device, "MaterialRole.load 的 device") or EXECUTING_DEVICE
        fields = [("material", _name(material, "MaterialRole.load 的物料")), ("acquire", acquire)]
        if from_device is not None:
            release = _name(from_device, "MaterialRole.load 的 from_device")
            if release == acquire:
                raise MaterialRoleError("MaterialRole.load 的 device 与 from_device 不能是同一个参数")
            fields.append(("release", release))
        return MaterialRole("load", tuple(fields))

    @staticmethod
    def unload(material: str, *, device: Optional[str] = None) -> MaterialRole:
        """把物料从设备 ``device``（``DeviceSlot`` 参数，省略即执行设备）取出并结束设备托管。"""

        release = _optional(device, "MaterialRole.unload 的 device") or EXECUTING_DEVICE
        return MaterialRole("unload", (("material", _name(material, "MaterialRole.unload 的物料")), ("release", release)))

    @staticmethod
    def in_place(material: str) -> MaterialRole:
        """物料必须仍在执行设备内（原位处理）。"""

        return MaterialRole("in_place", (("material", _name(material, "MaterialRole.in_place 的物料")),))

    @staticmethod
    def site_picker(
        site: str,
        *,
        of: Optional[str] = None,
        for_material: Optional[str] = None,
        occupied: str = "show",
    ) -> MaterialRole:
        """前端库位选择器提示：``SiteSlot`` 参数 ``site`` 列出所属方 ``of`` 的库位。

        ``of`` 是 ``ResourceSlot``（所属物料）或 ``DeviceSlot``（所属设备根节点）参数，省略时是执行设备根节点；
        ``for_material`` 是要放进去的物料参数（可省略）；``occupied``：``"hide"`` 不列出已占用的库位，
        ``"show"`` 列出但不能选，``"allow"`` 列出且能选。
        """

        fields: List[Tuple[str, Any]] = [("site", _name(site, "MaterialRole.site_picker 的库位参数"))]
        if of is not None:
            fields.append(("of", _name(of, "MaterialRole.site_picker 的 of")))
        if for_material is not None:
            fields.append(("for_material", _name(for_material, "MaterialRole.site_picker 的 for_material")))
        if occupied not in _OCCUPIED:
            raise MaterialRoleError('MaterialRole.site_picker 的 occupied 只能是 "hide"、"show" 或 "allow"')
        fields.append(("occupied", occupied))
        if site in {of, for_material}:
            raise MaterialRoleError("MaterialRole.site_picker 不能把库位参数自身作为 of / for_material")
        return MaterialRole("site_picker", tuple(fields))

    @staticmethod
    def order_sensitive() -> MaterialRole:
        """同一资源上的兄弟动作必须显式排序（只约束计划）。"""

        return MaterialRole("order_sensitive")

    # ------------------------------------------------------------------ 已删除的写法

    @staticmethod
    def transfer(*_args: Any, **_kwargs: Any) -> MaterialRole:
        raise _legacy(
            "MaterialRole.transfer 已删除：写成 MaterialRole.move(\"物料参数\", to_site=\"SiteSlot 库位参数\", "
            "from_site=\"SiteSlot 库位参数\"（可省略）, carrier_device=\"DeviceSlot 设备参数\"（可省略，即执行设备）)；"
            "所属方由库位决定（不写 to / source），库位参数注解为 SiteSlot（值是库位 uuid），"
            "搬运设备的夹爪库位角色统一为 gripper（不写 gripper=）"
        )

    @staticmethod
    def aliquot(*_args: Any, **_kwargs: Any) -> MaterialRole:
        raise _legacy(
            "MaterialRole.aliquot 已改名：写成 MaterialRole.dispense(from_material=\"来源物料参数\", to_materials=[\"目标物料参数\", ...])"
        )

    @staticmethod
    def site(*_args: Any, **_kwargs: Any) -> MaterialRole:
        raise _legacy(
            "MaterialRole.site 已改名：写成 MaterialRole.site_picker(\"SiteSlot 库位参数\", of=\"所属物料或设备参数\"（可省略，即执行设备）, "
            "for_material=\"放入物料参数\", occupied=\"hide\" | \"show\" | \"allow\")"
        )

    @staticmethod
    def uses(*_args: Any, **_kwargs: Any) -> MaterialRole:
        raise _legacy(
            "MaterialRole.uses 已删除：参数按类型加锁（ResourceSlot 物料锁、DeviceSlot 设备锁、SiteSlot 库位锁），"
            "把参数注解成对应类型即可，去掉 MaterialRole.uses(...)"
        )


#: ``MaterialRole`` 上的声明工厂（AST 扫描只认这些名字，不会调到实例方法）。
_FACTORY_NAMES = frozenset({
    "move", "pick", "place", "dispense",
    "load", "unload", "in_place", "site_picker", "order_sensitive",
    "transfer", "aliquot", "site", "uses",
})


def _renamed(method: str) -> MaterialRoleError:
    return MaterialRoleError(
        f"Material 已改名为 MaterialRole：把 Material.{method}(...) 写成 MaterialRole.{method}(...)，"
        "从 unilabos.registry.decorators 导入 MaterialRole（声明式资源语义；运行时物料操作是 unilabos.resources.materials）"
    )


class _RenamedMaterialMeta(type):
    def __getattr__(cls, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise _renamed(name)


class Material(metaclass=_RenamedMaterialMeta):
    """已改名为 :class:`MaterialRole`。只为让旧写法 ``Material.xxx(...)`` 在导入时报错并给出新写法而保留。"""

    def __new__(cls, *_args: Any, **_kwargs: Any) -> "Material":
        raise _renamed("xxx")

def _roles(value: Any) -> List[MaterialRole]:
    roles = list(value) if isinstance(value, (list, tuple)) else [value]
    for role in roles:
        if not isinstance(role, MaterialRole):
            raise MaterialRoleError("material_contract= 只接受 MaterialRole.xxx(...) 或它们组成的列表")
    if not roles:
        raise MaterialRoleError("material_contract= 至少写一条 MaterialRole.xxx(...)")
    return roles


def material_roles(value: Any) -> List[MaterialRole]:
    """``material_contract=`` 的值（一条语义或列表）→ 语义列表；``None`` 为空列表。"""

    return [] if value is None else _roles(value)


def material_kinds(value: Any) -> List[str]:
    """``material_contract=`` 里用到的声明种类（按出现顺序去重）。"""

    return list(dict.fromkeys(role.kind for role in material_roles(value)))


def warn_experimental(value: Any, definition: str) -> None:
    """注册时按定义告警一次：整个 ``material_contract=`` 字段是实验性功能（装饰器与 AST 扫描共用同一身份）。"""

    kinds = material_kinds(value)
    if not kinds:
        return
    from unilabos.registry.action_schema import warn_once

    warn_once(
        definition,
        "material_contract= 是实验性功能（" + "、".join(f"MaterialRole.{kind}" for kind in kinds)
        + "）：语义与写法以后可能调整",
    )


def material_contract(value: Any) -> Dict[str, Any]:
    """``material_contract=`` 的值（一条语义或列表）→ 规范化的第 2 版资源合同；``None`` 或只有 ``site_picker`` 时返回空字典。"""

    if value is None:
        return {}
    raw: Dict[str, Any] = {"version": 2}
    seen: Dict[str, str] = {}
    for role in _roles(value):
        if role.kind in _SCHEMA_KINDS:
            continue
        part = role.contract_part()
        for key, item in part.items():
            if key in raw:
                raise MaterialRoleError(f"MaterialRole.{seen[key]} 与 MaterialRole.{role.kind} 不能写在同一个动作上")
            raw[key] = item
            seen[key] = role.kind
    if "transfer" in raw and "transfer_step" in raw:
        raise MaterialRoleError(f"MaterialRole.{seen['transfer']} 与 MaterialRole.{seen['transfer_step']} 不能写在同一个动作上")
    if len(raw) == 1:
        return {}
    try:
        return normalize_action_resource_contract(raw)
    except ActionResourceContractError as error:
        raise MaterialRoleError(f"material_contract= 写法无效：{error.message}") from None


def material_site_selectors(value: Any) -> Dict[str, Dict[str, Any]]:
    """``material_contract=`` 里的 ``MaterialRole.site_picker(...)`` → ``{库位参数名: x-unilabos-site-selector 扩展}``。"""

    if value is None:
        return {}
    selectors: Dict[str, Dict[str, Any]] = {}
    for role in _roles(value):
        if role.kind != "site_picker":
            continue
        site, extension = role.site_selector()
        if site in selectors:
            raise MaterialRoleError(f"库位参数 {site} 写了多条 MaterialRole.site_picker")
        selectors[site] = extension
    return selectors


def material_parameter_types(value: Any) -> List[Tuple[str, str, str]]:
    """``material_contract=`` 引用的全部参数与要求的类型（见 :meth:`MaterialRole.parameter_types`）。"""

    items: List[Tuple[str, str, str]] = []
    for role in material_roles(value):
        items.extend(role.parameter_types())
    return items


def check_material_parameter_types(value: Any, slot_of, *, definition: str) -> None:
    """对照签名检查 ``material_contract=`` 引用的参数存在且类型正确。

    ``slot_of(name)`` 返回参数的槽位类型：``"resource"``（单个 ``ResourceSlot``）、``"resource_list"``、
    ``"site"``、``"device"``、``"device_list"``、``""``（其他类型）；参数不存在返回 ``None``。
    """

    for name, kind, label in material_parameter_types(value):
        actual = slot_of(name)
        if actual is None:
            raise MaterialRoleError(f"{definition}：{label} 引用了签名里没有的参数 {name}")
        if kind == "resource" and actual != "resource":
            raise MaterialRoleError(f"{definition}：{label} {name} 必须注解为单个 ResourceSlot")
        if kind == "site" and actual != "site":
            raise MaterialRoleError(f"{definition}：{label} {name} 必须注解为 SiteSlot（值是库位 uuid）")
        if kind == "device" and actual != "device":
            raise MaterialRoleError(f"{definition}：{label} {name} 必须注解为 DeviceSlot（值是设备 id）")
        if kind == "owner" and actual not in {"resource", "device"}:
            raise MaterialRoleError(f"{definition}：{label} {name} 必须注解为单个 ResourceSlot 或 DeviceSlot")


def site_selector_parameter_names(selectors: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """库位关系引用的全部动作参数名（库位参数、owner、occupant），用于对照签名。"""

    names: List[str] = []
    for site, extension in selectors.items():
        for name in (site, extension.get("owner"), extension.get("occupant")):
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    return names


def contract_parameter_names(contract: Mapping[str, Any]) -> List[str]:
    """合同引用的全部动作参数名（不含夹爪 / 运动 / 工具角色名与执行设备记号），用于对照签名。"""

    names: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value != EXECUTING_DEVICE and value not in names:
            names.append(value)

    tenancy = contract.get("device_tenancy") or {}
    for key in ("material_param", "acquire_device_param", "release_device_param"):
        add(tenancy.get(key))
    add((contract.get("operate_in_place") or {}).get("material_param"))
    step = contract.get("transfer_step") or {}
    for key in ("material_param", "owner_param", "site_param"):
        add(step.get(key))
    for name in step.get("carrier_params") or []:
        add(name)
    transfer = contract.get("transfer") or {}
    for key in (
        "material_param",
        "source_owner_param",
        "source_site_uuid_param",
        "source_site_name_param",
        "target_owner_param",
        "target_site_uuid_param",
        "target_site_name_param",
        "carrier_device_param",
    ):
        add(transfer.get(key))
    aliquot = contract.get("aliquot") or {}
    add(aliquot.get("source_material_param"))
    for name in aliquot.get("target_material_params") or []:
        add(name)
    for item in contract.get("resource_params") or []:
        add(item.get("param"))
    for name in contract.get("required_device_params") or []:
        add(name)
    return names


def material_roles_from_ast(value: Any) -> List[MaterialRole]:
    """AST 扫描提取的 ``material_contract=`` 值（``{"_call": ..., "_pos_0": ...}`` 或其列表）→ 语义列表。"""

    items = value if isinstance(value, list) else [value]
    roles: List[MaterialRole] = []
    for item in items:
        if not isinstance(item, Mapping) or "_call" not in item:
            raise MaterialRoleError("material_contract= 只接受 MaterialRole.xxx(...) 或它们组成的列表（参数须是字面量）")
        call = str(item["_call"])
        prefix, _, method = call.rpartition(".")
        module, _, owner = prefix.rpartition(":")
        if owner == "Material" and (not module or module in _MATERIAL_MODULES):
            # 改名前的类名：与导入期的 Material 占位类同一句提示
            raise _renamed(method)
        if owner != "MaterialRole":
            raise MaterialRoleError(f"material_contract= 只接受 MaterialRole.xxx(...)，不认识 {call}")
        if module and module not in _MATERIAL_MODULES:
            raise MaterialRoleError(f"material_contract= 的 MaterialRole 必须从 unilabos.registry.decorators 导入，不认识 {call}")
        if method not in _FACTORY_NAMES:
            raise MaterialRoleError(f"MaterialRole 没有 {method}(...)")
        factory = getattr(MaterialRole, method)
        positional = [item[key] for key in sorted((k for k in item if k.startswith("_pos_")), key=lambda k: int(k[5:]))]
        keywords = {key: val for key, val in item.items() if not key.startswith("_")}
        try:
            roles.append(factory(*positional, **keywords))
        except TypeError as error:
            raise MaterialRoleError(f"MaterialRole.{method}(...) 参数不对：{error}") from None
    return roles


__all__ = [
    "GRIPPER_ROLE",
    "Material",
    "MaterialRole",
    "MaterialRoleError",
    "check_material_parameter_types",
    "contract_parameter_names",
    "material_contract",
    "material_kinds",
    "material_parameter_types",
    "material_roles",
    "material_roles_from_ast",
    "material_site_selectors",
    "site_selector_parameter_names",
    "warn_experimental",
]
