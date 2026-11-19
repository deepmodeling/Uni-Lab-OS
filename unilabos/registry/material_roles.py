"""动作的资源语义：``@action(material=...)`` 唯一的写法。

一个动作对物料 / 设备的语义（装载托管、原位处理、搬运、分装、顺序敏感……）只用一个字段声明，
值是 ``Material`` 的一个方法调用，或多个调用组成的列表：

    from unilabos.registry.decorators import action, Material

    @action(material=Material.load("sample", device="vessel"))
    def load_sample(self, sample: ResourceSlot, vessel: str) -> CustodyResult: ...

    @action(material=[Material.in_place("sample"), Material.order_sensitive()])
    def stir(self, sample: ResourceSlot) -> None: ...

字符串都是动作的参数名，注册时对照签名检查。内部规范化成第 2 版资源合同
（``x-unilabos-action-contract.resource_contract``），调度器照旧按合同执行。

``Material.site(...)`` 声明一个 ``SiteSlot`` 库位参数与物料参数的关系（库位属于哪个物料、放进去的是哪个
物料、已占用库位的显示 / 可选策略）。它不进资源合同，而是写进该库位参数 schema 的
``x-unilabos-site-selector`` 扩展，供前端库位选择器、运行预检与派发前的库位门禁使用。

本模块只依赖标准库与纯声明的合同规范化：装饰器（导入期）与 AST 扫描（不导入作者模块）共用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from unilabos.registry.action_resource_contract import (
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
    "transfer": "transfer",
    "aliquot": "aliquot",
    "order_sensitive": "order_sensitive",
}
#: 不进资源合同、写进参数 schema 扩展的语义。
_SCHEMA_KINDS = frozenset({"site"})
#: AST 扫描认可的 ``Material`` 来源模块（``module:Material.method``）。
_MATERIAL_MODULES = ("unilabos.registry.decorators", "unilabos.registry.material_roles")


class MaterialRoleError(ValueError):
    """``material=`` 写法不对：注册失败并指出正确写法。"""


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


@dataclass(frozen=True)
class MaterialRole:
    """``Material.xxx(...)`` 的结果：一条资源语义（不可变，按字段规范化成合同片段）。"""

    kind: str
    fields: Tuple[Tuple[str, Any], ...] = ()

    def contract_part(self) -> Dict[str, Any]:
        values = dict(self.fields)
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
            return {
                "transfer_step": {
                    "operation": self.kind,
                    "material_param": values["material"],
                    "owner_param": values["owner"],
                    "site_param": values["site"],
                    "carrier_params": list(values["carriers"]),
                }
            }
        if self.kind == "transfer":
            transfer = {
                "material_param": values["material"],
                "source_owner_param": values.get("source") or "",
                "source_site_uuid_param": values.get("source_site") or "",
                "source_site_name_param": values.get("source_site_name") or "",
                "target_owner_param": values["to"],
                "target_site_uuid_param": values.get("site") or "",
                "target_site_name_param": values.get("site_name") or "",
                "gripper_site_role": values["gripper"],
            }
            if values.get("motion"):
                transfer["motion_resource_roles"] = list(values["motion"])
            if values.get("tools"):
                transfer["tool_resource_roles"] = list(values["tools"])
            return {"transfer": transfer}
        if self.kind == "aliquot":
            return {
                "aliquot": {
                    "source_material_param": values["source"],
                    "target_material_params": list(values["targets"]),
                }
            }
        if self.kind == "order_sensitive":
            return {"order_sensitive": True}
        if self.kind == "uses":
            return {"resource_params": [{"param": name, "role": role} for role, name in values["params"]]}
        raise MaterialRoleError(f"未知的资源语义 {self.kind}")

    def site_selector(self) -> Tuple[str, Dict[str, Any]]:
        """``Material.site(...)`` → ``(库位参数名, x-unilabos-site-selector 扩展)``。"""

        if self.kind != "site":
            raise MaterialRoleError(f"Material.{self.kind} 不是库位关系")
        values = dict(self.fields)
        return values["site"], {
            "version": 1,
            "owner": values["owner"],
            "occupant": values.get("occupant"),
            "show_occupied": values["show_occupied"],
            "allow_occupied": values["allow_occupied"],
        }


class Material:
    """资源语义的唯一入口：每个方法返回一条语义，``@action(material=...)`` 接收一条或一个列表。"""

    @staticmethod
    def load(material: str, *, device: str, from_device: Optional[str] = None) -> MaterialRole:
        """把物料装进设备并由任务托管设备，直到对应的 ``unload``（``from_device``：同时从另一台设备转出）。"""

        fields = [("material", _name(material, "Material.load 的物料")), ("acquire", _name(device, "device"))]
        if from_device is not None:
            fields.append(("release", _name(from_device, "from_device")))
        return MaterialRole("load", tuple(fields))

    @staticmethod
    def unload(material: str, *, device: str) -> MaterialRole:
        """把物料从设备取出并结束设备托管。"""

        return MaterialRole(
            "unload", (("material", _name(material, "Material.unload 的物料")), ("release", _name(device, "device")))
        )

    @staticmethod
    def in_place(material: str) -> MaterialRole:
        """物料必须仍在执行设备内（原位处理）。"""

        return MaterialRole("in_place", (("material", _name(material, "Material.in_place 的物料")),))

    @staticmethod
    def pick(material: str, *, owner: str, site: str, carriers: Union[str, Sequence[str]]) -> MaterialRole:
        """搬运第一步：从 ``owner`` 的库位 ``site`` 抓到搬运器（``carriers``）上。"""

        return Material._step("pick", material, owner, site, carriers)

    @staticmethod
    def place(material: str, *, owner: str, site: str, carriers: Union[str, Sequence[str]]) -> MaterialRole:
        """搬运第二步：从搬运器（``carriers``）放到 ``owner`` 的库位 ``site``。"""

        return Material._step("place", material, owner, site, carriers)

    @staticmethod
    def _step(kind: str, material: str, owner: str, site: str, carriers) -> MaterialRole:
        carrier_names = _names(carriers, "carriers")
        if not carrier_names:
            raise MaterialRoleError(f"Material.{kind} 的 carriers 不能为空")
        return MaterialRole(
            kind,
            (
                ("material", _name(material, f"Material.{kind} 的物料")),
                ("owner", _name(owner, "owner")),
                ("site", _name(site, "site")),
                ("carriers", tuple(_name(item, "carriers") for item in carrier_names)),
            ),
        )

    @staticmethod
    def transfer(
        material: str,
        *,
        to: str,
        gripper: str,
        site: Optional[str] = None,
        site_name: Optional[str] = None,
        source: Optional[str] = None,
        source_site: Optional[str] = None,
        source_site_name: Optional[str] = None,
        motion: Union[str, Sequence[str], None] = None,
        tools: Union[str, Sequence[str], None] = None,
    ) -> MaterialRole:
        """一次完成的机械臂搬运：把物料放到 ``to`` 的库位（``site`` 是库位 UUID 参数，``site_name`` 是库位名参数）。

        ``gripper`` 是夹爪库位的角色名；``source`` / ``source_site`` / ``source_site_name`` 声明来源；
        ``motion`` / ``tools`` 是运动部件与工具的资源角色名。
        """

        fields: List[Tuple[str, Any]] = [
            ("material", _name(material, "Material.transfer 的物料")),
            ("to", _name(to, "to")),
            ("gripper", _name(gripper, "gripper")),
        ]
        for key, value in (
            ("site", site),
            ("site_name", site_name),
            ("source", source),
            ("source_site", source_site),
            ("source_site_name", source_site_name),
        ):
            if value is not None:
                fields.append((key, _name(value, key)))
        for key, value in (("motion", motion), ("tools", tools)):
            names = _names(value, key)
            if names:
                fields.append((key, tuple(names)))
        return MaterialRole("transfer", tuple(fields))

    @staticmethod
    def aliquot(*, source: str, targets: Union[str, Sequence[str]]) -> MaterialRole:
        """按实测量从 ``source`` 分装到全部 ``targets``。"""

        names = _names(targets, "targets")
        return MaterialRole(
            "aliquot", (("source", _name(source, "source")), ("targets", tuple(_name(n, "targets") for n in names)))
        )

    @staticmethod
    def site(
        site: str,
        *,
        owner: str,
        occupant: Optional[str] = None,
        show_occupied: bool = True,
        allow_occupied: bool = False,
    ) -> MaterialRole:
        """库位参数 ``site``（注解为 ``SiteSlot``）的库位属于物料参数 ``owner``。

        ``occupant`` 是要放进这个库位的物料参数（可省略）；``show_occupied`` 决定前端是否列出已占用的库位，
        ``allow_occupied`` 决定已占用的库位能否选（运行预检与派发前按它核对）。``owner`` / ``occupant``
        必须是单个 ``ResourceSlot`` 参数。
        """

        fields: List[Tuple[str, Any]] = [
            ("site", _name(site, "Material.site 的库位参数")),
            ("owner", _name(owner, "owner")),
        ]
        if occupant is not None:
            fields.append(("occupant", _name(occupant, "occupant")))
        for key, value in (("show_occupied", show_occupied), ("allow_occupied", allow_occupied)):
            if type(value) is not bool:
                raise MaterialRoleError(f"Material.site 的 {key} 必须是 True / False")
            fields.append((key, value))
        if site in {owner, occupant}:
            raise MaterialRoleError("Material.site 不能把库位参数自身作为 owner / occupant")
        return MaterialRole("site", tuple(fields))

    @staticmethod
    def order_sensitive() -> MaterialRole:
        """同一资源上的兄弟动作必须显式排序（只约束计划）。"""

        return MaterialRole("order_sensitive")

    @staticmethod
    def uses(
        *,
        device: Union[str, Sequence[str], None] = None,
        tool: Union[str, Sequence[str], None] = None,
        motion: Union[str, Sequence[str], None] = None,
        site: Union[str, Sequence[str], None] = None,
        material: Union[str, Sequence[str], None] = None,
    ) -> MaterialRole:
        """动作还占用这些资源参数（设备 / 工具 / 运动部件 / 库位 / 物料），调度时一并校验与占用。"""

        params: List[Tuple[str, str]] = []
        for role, value in (("device", device), ("tool", tool), ("motion", motion), ("site", site), ("material", material)):
            params.extend((role, _name(name, role)) for name in _names(value, role))
        if not params:
            raise MaterialRoleError("Material.uses 至少声明一个资源参数")
        return MaterialRole("uses", (("params", tuple(params)),))


def _roles(value: Any) -> List[MaterialRole]:
    roles = list(value) if isinstance(value, (list, tuple)) else [value]
    for role in roles:
        if not isinstance(role, MaterialRole):
            raise MaterialRoleError("material= 只接受 Material.xxx(...) 或它们组成的列表")
    if not roles:
        raise MaterialRoleError("material= 至少写一条 Material.xxx(...)")
    return roles


def material_contract(value: Any) -> Dict[str, Any]:
    """``material=`` 的值（一条语义或列表）→ 规范化的第 2 版资源合同；``None`` 或只有 ``Material.site`` 时返回空字典。"""

    if value is None:
        return {}
    raw: Dict[str, Any] = {"version": 2}
    seen: Dict[str, str] = {}
    for role in _roles(value):
        if role.kind in _SCHEMA_KINDS:
            continue
        part = role.contract_part()
        for key, item in part.items():
            if key == "resource_params":
                raw.setdefault(key, []).extend(item)
                continue
            if key in raw:
                raise MaterialRoleError(f"Material.{seen[key]} 与 Material.{role.kind} 不能写在同一个动作上")
            raw[key] = item
            seen[key] = role.kind
    if len(raw) == 1:
        return {}
    try:
        return normalize_action_resource_contract(raw)
    except ActionResourceContractError as error:
        raise MaterialRoleError(f"material= 写法无效：{error.message}") from None


def material_site_selectors(value: Any) -> Dict[str, Dict[str, Any]]:
    """``material=`` 里的 ``Material.site(...)`` → ``{库位参数名: x-unilabos-site-selector 扩展}``。"""

    if value is None:
        return {}
    selectors: Dict[str, Dict[str, Any]] = {}
    for role in _roles(value):
        if role.kind != "site":
            continue
        site, extension = role.site_selector()
        if site in selectors:
            raise MaterialRoleError(f"库位参数 {site} 写了多条 Material.site")
        selectors[site] = extension
    return selectors


def site_selector_parameter_names(selectors: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """库位关系引用的全部动作参数名（库位参数、owner、occupant），用于对照签名。"""

    names: List[str] = []
    for site, extension in selectors.items():
        for name in (site, extension.get("owner"), extension.get("occupant")):
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    return names


def contract_parameter_names(contract: Mapping[str, Any]) -> List[str]:
    """合同引用的全部动作参数名（不含夹爪 / 运动 / 工具角色名），用于对照签名。"""

    names: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in names:
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
    """AST 扫描提取的 ``material=`` 值（``{"_call": ..., "_pos_0": ...}`` 或其列表）→ 语义列表。"""

    items = value if isinstance(value, list) else [value]
    roles: List[MaterialRole] = []
    for item in items:
        if not isinstance(item, Mapping) or "_call" not in item:
            raise MaterialRoleError("material= 只接受 Material.xxx(...) 或它们组成的列表（参数须是字面量）")
        call = str(item["_call"])
        prefix, _, method = call.rpartition(".")
        module, _, owner = prefix.rpartition(":")
        if owner != "Material" and prefix != "Material":
            raise MaterialRoleError(f"material= 只接受 Material.xxx(...)，不认识 {call}")
        if module and module not in _MATERIAL_MODULES:
            raise MaterialRoleError(f"material= 的 Material 必须从 unilabos.registry.decorators 导入，不认识 {call}")
        factory = getattr(Material, method, None) if not method.startswith("_") else None
        if factory is None:
            raise MaterialRoleError(f"Material 没有 {method}(...)")
        positional = [item[key] for key in sorted((k for k in item if k.startswith("_pos_")), key=lambda k: int(k[5:]))]
        keywords = {key: val for key, val in item.items() if not key.startswith("_")}
        try:
            roles.append(factory(*positional, **keywords))
        except TypeError as error:
            raise MaterialRoleError(f"Material.{method}(...) 参数不对：{error}") from None
    return roles


__all__ = [
    "Material",
    "MaterialRole",
    "MaterialRoleError",
    "contract_parameter_names",
    "material_contract",
    "material_roles_from_ast",
    "material_site_selectors",
    "site_selector_parameter_names",
]
