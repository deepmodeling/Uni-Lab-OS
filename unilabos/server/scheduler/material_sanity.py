"""派发前的物料健全性检查：节点就绪、申请资源之前，确认它引用的物料此刻仍能用。

规划（建任务、预留库存）到节点就绪之间物料可能被删除、消耗、隔离、挪走，或被别的任务拿去。检查在解析完的
动作参数与注入的库存分配上运行，只向物料权威做一次批量只读（``material_sanity_facts``，
``server/services/materials/sanity.py``），按已有的三类处理：

| 检查 | 不通过时 |
| --- | --- |
| 物料还在：未删除、未消耗、未退役、未隔离 | 预检报错（操作员恢复 / 替换后重试） |
| 生命周期相容：``active``，或被本作业 / 本任务的库存预留占着（``reserved`` / ``in_use``），或停在已结束任务留下的 ``in_use`` | 预留它的其他任务还没结束 → 排队（``inventory_reserved_by_other_tasks``）；``reserved`` 却没有未结束任务的预留 → 预检报错 |
| 物理合同声明的来源库位：复用 ``physical_contract`` 的结论，不再重算 | 另一作业的未结算动作正占着它 → 照旧排队；否则「规划后被挪走」→ 预检报错 |
| 库存分配与本作业持有的预留一致：同一容器 / 批次、现量 > 0、单位可换算 | 预检报错 |
| 没有被另一作业未结算的物理动作占着 | 排队（``site_claimed``） |

预检报错走动作执行前预检决策（``category="preflight"``，错误项 ``code="material_sanity"``，``materials`` 列出每个
物料的具体原因）；排队走已有的资源条件等待（attempt 不持有任何东西，``control_data.wait_reason``）。参数不是物料、
类型不符、引用从未登记过的 UUID 属于合同 / 编程错误，不在这里判定，照常由合同校验失败。没有物料参数的节点
不做这次读取。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .materials import extract_material_uuids, normalize_material_parameter_names

#: 被其他任务的有效库存预留占着（与整任务 / 就绪时预留被占的排队同码）
WAIT_RESERVED_BY_OTHER_TASKS = "inventory_reserved_by_other_tasks"
#: 被另一作业未结算的物理动作占着（物料权威冲突类别 ``active_claim`` 的同一条件码）
WAIT_HELD_BY_PHYSICAL_ACTION = "site_claimed"
#: 预检报错的错误项 ``code``
MATERIAL_SANITY_CODE = "material_sanity"

#: 操作员恢复前不能再用的生命周期
_UNUSABLE = frozenset({"consumed", "retired", "quarantined"})
#: 由库存预留推进的生命周期（``active → reserved → in_use``）
_CLAIMED = frozenset({"reserved", "in_use"})


@dataclass(frozen=True)
class MaterialProblem:
    """一个物料（或一条库存分配）没有通过检查。``wait_code`` 非空表示临时条件：attempt 排队，不报错。"""

    reason: str
    message: str
    material_uuid: Optional[str] = None
    sources: tuple[str, ...] = ()
    wait_code: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_item(self) -> dict[str, Any]:
        item = {key: value for key, value in asdict(self).items() if value not in (None, (), {})}
        item.pop("wait_code", None)
        if "sources" in item:
            item["sources"] = list(item["sources"])
        return item


def error_item(problems: Iterable[MaterialProblem]) -> dict[str, Any]:
    """动作执行前预检的错误项（与 ``inventory_shortage`` 同形）。"""

    problems = list(problems)
    return {
        "code": MATERIAL_SANITY_CODE,
        "stage": "preflight",
        "scope": "action",
        "message": "动作执行前预检：物料状态与规划不符：" + "；".join(item.message for item in problems),
        "materials": [item.as_item() for item in problems],
    }


def _material_field(value: Any) -> bool:
    """规范合同里的 ResourceSlot 字段（物料锁或免锁都带 ``x-unilabos-material-lock``），含数组与可空。"""

    if not isinstance(value, Mapping):
        return False
    return "x-unilabos-material-lock" in value or _material_field(value.get("items")) or any(
        _material_field(member) for member in value.get("anyOf", ()) or ()
    )


def material_references(
    schema: Optional[Mapping[str, Any]],
    args: Mapping[str, Any],
    lock_parameters: Any,
    reservation_items: Iterable[Mapping[str, Any]] = (),
) -> dict[str, tuple[str, ...]]:
    """动作引用的全部物料 → 引用它的来源（参数名，或 ``inventory:<需求键>``）。

    来源：规范合同的 ResourceSlot 字段、``materials_need_lock`` 参数、本作业库存预留里的物料实例与内容物容器。
    批次（``lot``）不是物料，按预留检查。
    """

    references: dict[str, list[str]] = {}

    def add(identities: Iterable[str], source: str) -> None:
        for identity in identities:
            sources = references.setdefault(identity, [])
            if source not in sources:
                sources.append(source)

    fields = ((schema or {}).get("properties", {}).get("goal", {}).get("properties", {})
              if (schema or {}).get("x-unilabos-action-contract") else {})
    for name, value_schema in fields.items():
        if _material_field(value_schema) and args.get(name) is not None:
            add(sorted(extract_material_uuids(args[name])), name)
    for name in normalize_material_parameter_names(lock_parameters):
        if args.get(name) is not None:
            add(sorted(extract_material_uuids(args[name])), name)
    for item in reservation_items:
        if item.get("kind") in {"material", "content"} and item.get("material_uuid"):
            add([str(item["material_uuid"])], f"inventory:{item.get('key')}")
    return {identity: tuple(sources) for identity, sources in references.items()}


def _label(identity: str, sources: Iterable[str]) -> str:
    named = [source.removeprefix("inventory:") + "（库存分配）" if source.startswith("inventory:") else source
             for source in sources]
    return f"{'、'.join(named)} 引用的物料 {identity}" if named else f"物料 {identity}"


def _identity(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (item.get("key"), item.get("kind"), item.get("material_uuid"), item.get("lot_uuid"),
            item.get("substance_uuid"), item.get("content_reservation_uuid"))


@dataclass
class MaterialSanity:
    """一次检查的事实与结论；``facts`` 来自物料权威的 ``material_sanity_facts``。"""

    job_uuid: str
    task_uuid: str
    references: dict[str, tuple[str, ...]]
    facts: dict[str, Any]
    reservation_uuid: Optional[str] = None
    reservation_items: list[dict[str, Any]] = field(default_factory=list)
    #: 预留所属任务是否已结束；None 表示查不到这个任务（只在调度器里能查任务状态，缺省都查不到）
    task_finished: Callable[[str], Optional[bool]] = lambda _task_uuid: None

    def _held_by_unfinished_task(self, reservation: Mapping[str, Any]) -> bool:
        """这条预留背后的任务还会不会用这个物料：任务没结束就会；查不到任务时有效（active）的预留仍算占着。"""

        finished = self.task_finished(str(reservation.get("task_uuid") or ""))
        if finished is None:
            return reservation.get("status") == "active"
        return not finished

    def _material(self, identity: str) -> dict[str, Any]:
        return dict((self.facts.get("materials") or {}).get(identity) or {"exists": False})

    def problems(self) -> list[MaterialProblem]:
        """全部不通过项；调用方按「有预检报错先报错，否则排队」处理。"""

        problems: list[MaterialProblem] = []
        for identity, sources in sorted(self.references.items()):
            problems.extend(self._material_problems(identity, sources))
        if self.reservation_uuid:
            problems.extend(self._reservation_problems())
        return problems

    def _material_problems(self, identity: str, sources: tuple[str, ...]) -> list[MaterialProblem]:
        fact = self._material(identity)
        label = _label(identity, sources)

        def problem(reason: str, message: str, *, wait_code: Optional[str] = None, **details: Any) -> MaterialProblem:
            return MaterialProblem(reason=reason, message=message, material_uuid=identity, sources=sources,
                                   wait_code=wait_code, details=details)

        if not fact.get("exists"):
            # 从未登记过：不是「规划后消失」，是参数引用了非物料，留给合同校验照常失败
            return []
        if fact.get("deleted"):
            return [problem("deleted", f"{label} 已被删除")]
        status = str(fact.get("lifecycle_status") or "")
        if status in _UNUSABLE:
            return [problem("unusable", f"{label} 当前为 {status}，不能再用于动作", lifecycle_status=status)]
        found: list[MaterialProblem] = []
        holder = fact.get("held_by_job_uuid")
        if holder and holder != self.job_uuid:
            found.append(problem("held_by_physical_action", f"{label} 仍被作业 {holder} 未结算的物理动作占着",
                                 wait_code=WAIT_HELD_BY_PHYSICAL_ACTION, job_uuid=holder))
        if status in _CLAIMED:
            reservations = list(fact.get("reservations") or [])
            if not any(item.get("task_uuid") == self.task_uuid for item in reservations):
                # 其他任务还没结束：它可能还要用这个物料，排队等它结束（与「被别的任务占着就等待」一致）
                running = [item for item in reservations if self._held_by_unfinished_task(item)]
                if running:
                    holder = running[0]
                    found.append(problem(
                        f"{status}_by_other_task", f"{label} 被任务 {holder.get('task_uuid')} 的库存预留占着",
                        wait_code=WAIT_RESERVED_BY_OTHER_TASKS, lifecycle_status=status,
                        task_uuid=holder.get("task_uuid"), job_uuid=holder.get("job_uuid")))
                elif status == "reserved":
                    # 预留推进的 reserved 却找不到未结束任务的预留：状态与预留对不上，交给操作员
                    holders = sorted({str(item.get("task_uuid")) for item in reservations})
                    message = (f"{label} 处于 reserved，归已结束任务 {'、'.join(holders)} 的库存预留"
                               if holders else f"{label} 处于 reserved，但没有对应的库存预留")
                    found.append(problem("lifecycle_conflict", message, lifecycle_status=status,
                                         task_uuids=holders))
                # in_use 且没有未结束任务的预留：dev 不会把用过的物料改回 active，已结束任务留下的 in_use 视为可用
        elif status != "active":
            found.append(problem("unusable", f"{label} 的生命周期 {status or '未知'} 不允许用于动作",
                                 lifecycle_status=status))
        return found

    def _reservation_problems(self) -> list[MaterialProblem]:
        reservation = self.facts.get("reservation") or {}
        identity = self.reservation_uuid

        def problem(reason: str, message: str, **details: Any) -> MaterialProblem:
            return MaterialProblem(reason=reason, message=message, details={"reservation_uuid": identity, **details})

        if not reservation.get("exists"):
            return [problem("reservation_missing", f"本作业的库存预留 {identity} 已不存在")]
        if reservation.get("job_uuid") != self.job_uuid or reservation.get("task_uuid") != self.task_uuid:
            return [problem("reservation_mismatch", f"库存预留 {identity} 不属于本作业")]
        if reservation.get("status") != "active":
            return [problem("reservation_inactive",
                            f"本作业的库存预留 {identity} 已是 {reservation.get('status')}，不再持有分配给动作的库存",
                            status=reservation.get("status"))]
        items = list(reservation.get("items") or [])
        if self.reservation_items and (sorted(map(_identity, items), key=repr)
                                       != sorted(map(_identity, self.reservation_items), key=repr)):
            return [problem("allocation_mismatch", f"注入动作的库存分配与库存预留 {identity} 的当前分配不一致")]
        problems: list[MaterialProblem] = []
        for item in items:
            key, kind, current = item.get("key"), item.get("kind"), item.get("current") or {}
            what = f"库存分配 {key}"
            if kind == "lot":
                what = f"{what}（批次 {item.get('lot_uuid')}）"
                if not current.get("exists"):
                    problems.append(problem("lot_missing", f"{what} 已不存在", key=key))
                elif current.get("quarantined"):
                    problems.append(problem("lot_quarantined", f"{what} 已被隔离", key=key))
                elif not float(current.get("quantity") or 0) > 0:
                    problems.append(problem("quantity_depleted", f"{what} 现量为 0", key=key))
                elif not current.get("unit_convertible"):
                    problems.append(problem("unit_not_convertible",
                                            f"{what} 的单位 {item.get('unit')} 无法换算为批次单位 {current.get('unit')}",
                                            key=key))
            elif kind == "content":
                what = f"{what}（容器 {item.get('material_uuid')} 的内容物 {item.get('substance_uuid')}）"
                if not current.get("exists") or current.get("material_uuid") != item.get("material_uuid"):
                    problems.append(problem("content_missing", f"{what} 已不在该容器里", key=key))
                elif current.get("reservation_status") not in (None, "active"):
                    problems.append(problem("content_reservation_inactive",
                                            f"{what} 的数量预留已是 {current.get('reservation_status')}", key=key))
                elif not float(current.get("quantity") or 0) > 0:
                    problems.append(problem("quantity_depleted", f"{what} 现量为 0", key=key))
                elif not current.get("unit_convertible"):
                    problems.append(problem("unit_not_convertible",
                                            f"{what} 的单位 {item.get('unit')} 无法换算为内容物单位 {current.get('unit')}",
                                            key=key))
        return problems

    def relocation_problem(
        self, error: BaseException, used_by_others: Callable[[str], list[str]],
    ) -> Optional[MaterialProblem]:
        """物理合同报「来源库位对不上」时判定：另一作业正占着它（可能正在搬）→ None（照旧排队）；否则是规划后被挪走。

        复用 ``physical_contract`` 对来源库位的判定，不在这里重算位置。``used_by_others`` 返回调度侧正在实际使用这个物料的
        其他 attempt（持久准入的 ``workflow_resource_use``）。
        """

        if getattr(error, "wait_code", None) != "transfer_source_site_missing":
            return None
        identity = getattr(error, "material_uuid", None)
        if not identity:
            return None
        fact = self._material(identity)
        if not fact.get("exists") or fact.get("deleted"):
            return None
        holder = fact.get("held_by_job_uuid")
        if holder and holder != self.job_uuid:
            return None
        if used_by_others(identity):
            return None
        sources = self.references.get(identity, ())
        label = _label(identity, sources)
        site = fact.get("site_uuid")
        reason, message = (("moved", f"{label} 已不在动作声明的来源库位上（现在在库位 {site}），规划后被挪走")
                           if site else ("off_site", f"{label} 不在任何库位上，也没有作业正在搬运它"))
        return MaterialProblem(reason=reason, message=f"{message}：{error}", material_uuid=identity, sources=sources,
                               details={key: value for key, value in (("site_uuid", site),
                                        ("parent_material_uuid", fact.get("parent_material_uuid"))) if value})


__all__ = [
    "MATERIAL_SANITY_CODE",
    "MaterialProblem",
    "MaterialSanity",
    "WAIT_HELD_BY_PHYSICAL_ACTION",
    "WAIT_RESERVED_BY_OTHER_TASKS",
    "error_item",
    "material_references",
]
