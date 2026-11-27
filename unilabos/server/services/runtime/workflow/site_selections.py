"""库位参数按所属物料选择：``{"owner_uuid": ...}`` 在任务准入时选定一个库位、经入口预留占住并冻结进任务。

``SiteSlot`` 参数（带 ``MaterialRole.site_picker("site", of=..., for_material=...)`` 关系）可以给
``{"owner_uuid": "<所属物料 uuid>"}`` 而不是库位 uuid，工作流入口参数（``SiteSlot``）也可以。规则：

- 候选是所属物料下接受放入物料（``MaterialRole.site_picker`` 的 ``for_material`` 参数，按库位绑定的已先解析）的库位：
  库位的接收规则（模板白名单与允许的资源类别）要接受它；按库位序号选第一个空着、没有被别的任务预留的；
- 调度器在任务准入时（``BackendScheduler._admit_fresh_task``）用物料权威的入口预留占住选中的库位
  （``owner_material_uuid`` 写法，预留随任务：``backend_task_uuid`` = 任务 uuid），再把库位 uuid 冻结进计划节点参数、
  attempt 参数与任务输入（计划的 ``site_selections`` 记录选择与预留）；
- 所属物料下没有接受放入物料的库位是预检错误（``reason=no_acceptable_site``）；接受的库位此刻都被占着或已被预留
  按临时条件排队（attempt 的 ``wait_reason.code=site_group_unavailable``），空出后按任务创建先后重试；
- 用到这个库位的动作派发时预留转 ``in_transit``（放入物料直接放进来等同接收），任务结束时收尾
  （物料已在库位上记 received，否则取消）。精确的库位 uuid 照旧不经过这里。

本模块只放纯函数（建任务、运行预检与调度器共用）；读物料权威与写库由调用方负责。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

from unilabos.protocol.runtime.site_references import owner_reference

#: 预检错误的 ``reason``
NO_SITE_RELATION = "no_site_relation"
OCCUPANT_UNDECLARED = "occupant_undeclared"
OCCUPANT_UNKNOWN = "occupant_unknown"
OWNER_MISMATCH = "owner_mismatch"
OWNER_NOT_FOUND = "owner_not_found"
NO_ACCEPTABLE_SITE = "no_acceptable_site"
#: 接受的库位都被占着 / 已被预留时 attempt 排队的原因码（``resource_wait_policy`` 的临时条件）
WAIT_SITE_SELECTION = "site_group_unavailable"
#: 入口预留的有效期：覆盖任务从准入到用到库位的时间；任务结束时由调度器收尾
RESERVATION_TTL_SECONDS = 7 * 24 * 3600


class SiteSelectionError(ValueError):
    """按所属物料选不出库位（预检错误）：``reason`` 见模块常量，``details`` 带参数、节点与所属物料。"""

    def __init__(self, reason: str, message: str, *, parameter: str, owner_uuid: str,
                 node_uuid: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details: dict[str, Any] = {"reason": reason, "parameter": parameter, "owner_uuid": owner_uuid}
        if node_uuid is not None:
            self.details["node_uuid"] = node_uuid


@dataclass(frozen=True)
class SiteSelectionRequest:
    """一次库位选择：同一个工作流入口参数在多个节点里用到时只选一次。"""

    key: str
    owner_uuid: str
    occupant_uuid: str
    parameter: str
    node_uuid: str
    targets: tuple[tuple[str, str], ...]
    input_name: Optional[str] = None

    def record(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "owner_uuid": self.owner_uuid,
            "occupant_uuid": self.occupant_uuid,
            "targets": [{"node_uuid": node, "parameter": name} for node, name in self.targets],
            **({"input": self.input_name} if self.input_name else {}),
        }


def _site_fields(node: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    schema = node.get("param_schema")
    goal = ((schema or {}).get("properties") or {}).get("goal") if isinstance(schema, Mapping) else None
    fields = (goal or {}).get("properties") or {}
    return {name: field for name, field in fields.items() if isinstance(field, Mapping)}


def _upstream_parameters(plan: Mapping[str, Any], node_uuid: str) -> set[str]:
    return {
        str(edge.get("target_data_key") or "").split("@@@")[-1]
        for edge in plan.get("edges") or []
        if edge.get("target_node_uuid") == node_uuid and not edge.get("dependency_only")
    }


def _material_uuid(value: Any) -> Optional[str]:
    from unilabos.server.scheduler.materials import extract_material_uuids

    if not isinstance(value, Mapping) or value.get("kind") in {"workflow_input", "node_output"}:
        return None
    found = extract_material_uuids(value)
    return next(iter(found)) if len(found) == 1 else None


def has_owner_selections(plan: Optional[Mapping[str, Any]]) -> bool:
    """计划里有按所属物料选择的库位参数（未冻结的 ``{"owner_uuid"}``，或已冻结的选择记录）。"""

    if not plan:
        return False
    if plan.get("site_selections"):
        return True
    return any(
        owner_reference(value) is not None
        for node in plan.get("nodes") or [] for value in (node.get("param") or {}).values()
    )


def owner_site_requests(plan: Mapping[str, Any]) -> list[SiteSelectionRequest]:
    """计划里还没选定的库位参数（``{"owner_uuid"}``）→ 选择请求，按拓扑序；写法不对抛 :class:`SiteSelectionError`。"""

    grouped: dict[str, dict[str, Any]] = {}
    for node in plan.get("nodes") or []:
        node_uuid = str(node["uuid"])
        param = node.get("param") or {}
        fields = _site_fields(node)
        entry_bindings = node.get("entry_bindings") or {}
        for name, value in param.items():
            owner = owner_reference(value)
            if owner is None:
                continue
            where = f"库位参数 {name}（节点 {node_uuid}）"
            selector = (fields.get(name) or {}).get("x-unilabos-site-selector")
            if not isinstance(selector, Mapping):
                raise SiteSelectionError(
                    NO_SITE_RELATION, f"{where} 按所属物料 {owner} 选库位：动作没有用 MaterialRole.site_picker 声明这个库位参数的关系，"
                    "选不出接受放入物料的库位；请给库位 uuid", parameter=name, owner_uuid=owner, node_uuid=node_uuid)
            occupant_param = selector.get("occupant")
            if not occupant_param:
                raise SiteSelectionError(
                    OCCUPANT_UNDECLARED, f"{where} 按所属物料 {owner} 选库位：MaterialRole.site_picker 没有声明 for_material（放进去的物料），"
                    "无法按库位的接收规则挑选", parameter=name, owner_uuid=owner, node_uuid=node_uuid)
            upstream = _upstream_parameters(plan, node_uuid)
            occupant = None if occupant_param in upstream else _material_uuid(param.get(occupant_param))
            if occupant is None:
                raise SiteSelectionError(
                    OCCUPANT_UNKNOWN, f"{where} 按所属物料 {owner} 选库位：放入物料参数 {occupant_param} 在建任务时还不确定"
                    "（来自上游结果或没有给），请直接给物料或按库位绑定", parameter=name, owner_uuid=owner, node_uuid=node_uuid)
            owner_param = str(selector.get("owner") or "")
            declared_owner = None if owner_param in upstream else _material_uuid(param.get(owner_param))
            if declared_owner is not None and declared_owner != owner:
                raise SiteSelectionError(
                    OWNER_MISMATCH, f"{where} 按所属物料 {owner} 选库位：与库位所属物料参数 {owner_param}（{declared_owner}）不一致",
                    parameter=name, owner_uuid=owner, node_uuid=node_uuid)
            input_name = entry_bindings.get(name) if isinstance(entry_bindings, Mapping) else None
            key = f"input:{input_name}" if input_name else f"node:{node_uuid}/{name}"
            entry = grouped.setdefault(key, {
                "owner_uuid": owner, "occupant_uuid": occupant, "parameter": name, "node_uuid": node_uuid,
                "targets": [], "input_name": input_name,
            })
            entry["targets"].append((node_uuid, name))
    return [
        SiteSelectionRequest(key=key, owner_uuid=entry["owner_uuid"], occupant_uuid=entry["occupant_uuid"],
                             parameter=entry["parameter"], node_uuid=entry["node_uuid"],
                             targets=tuple(entry["targets"]), input_name=entry["input_name"])
        for key, entry in grouped.items()
    ]


def read_owner_facts(materials: Any, requests: Iterable[SiteSelectionRequest]) -> list[dict[str, Any]]:
    """向物料权威读一次每个请求的候选库位（只读）。"""

    requests = list(requests)
    if not requests:
        return []
    reader = getattr(materials, "site_admission_facts", None)
    if not callable(reader):
        raise RuntimeError("物料权威尚未接入，不能按所属物料选择库位")
    facts = reader({"owners": [
        {"owner_material_uuid": request.owner_uuid, "occupant_material_uuid": request.occupant_uuid}
        for request in requests]})
    return list((facts or {}).get("owners") or [])


def check_owner_sites(
    requests: Iterable[SiteSelectionRequest], facts: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """所属物料存在、放入物料存在、至少有一个接受它的库位；返回每个请求的 ``{acceptable, available}``。"""

    results: list[dict[str, Any]] = []
    for request, fact in zip(requests, facts):
        where = f"库位参数 {request.parameter}（节点 {request.node_uuid}）"
        if not fact.get("owner_exists"):
            raise SiteSelectionError(OWNER_NOT_FOUND, f"{where} 按所属物料选库位：物料 {request.owner_uuid} 不存在",
                                     parameter=request.parameter, owner_uuid=request.owner_uuid, node_uuid=request.node_uuid)
        occupant = fact.get("occupant")
        if not occupant:
            raise SiteSelectionError(
                OCCUPANT_UNKNOWN, f"{where} 按所属物料 {request.owner_uuid} 选库位：放入物料 {request.occupant_uuid} 不存在",
                parameter=request.parameter, owner_uuid=request.owner_uuid, node_uuid=request.node_uuid)
        acceptable = list(fact.get("acceptable") or [])
        if not acceptable:
            raise SiteSelectionError(
                NO_ACCEPTABLE_SITE,
                f"{where} 按所属物料 {request.owner_uuid} 选库位：它下面没有接受 {occupant.get('template_name')} 的库位"
                "（库位的模板白名单或允许的资源类别不含它）",
                parameter=request.parameter, owner_uuid=request.owner_uuid, node_uuid=request.node_uuid)
        results.append({"key": request.key, "acceptable": acceptable, "available": list(fact.get("available") or [])})
    return results


def reservation_payload(task_uuid: str, request: SiteSelectionRequest) -> dict[str, Any]:
    """入口预留请求：所属物料下接受放入物料的库位里预留一个；幂等键按任务与选择，重放得到同一个预留。"""

    return {
        "idempotency_key": f"workflow-site-selection:{task_uuid}:{request.key}",
        "carrier_material_uuid": request.occupant_uuid,
        "owner_material_uuid": request.owner_uuid,
        "ttl_seconds": RESERVATION_TTL_SECONDS,
        "backend_task_uuid": task_uuid,
        "invocation_key": request.key,
    }


def freeze_site_selections(
    plan: dict[str, Any], input_value: dict[str, Any], selections: Iterable[Mapping[str, Any]],
) -> set[str]:
    """就地把选中的库位 uuid 写进计划节点参数（含 ``entry_param_template`` 里的静态值）与任务输入，
    选择记录追加到计划的 ``site_selections``；返回改动的节点 uuid。"""

    nodes = {str(node["uuid"]): node for node in plan.get("nodes") or []}
    touched: set[str] = set()
    records = list(plan.get("site_selections") or [])
    for selection in selections:
        site_uuid = str(selection["site_uuid"])
        for target in selection.get("targets") or []:
            node = nodes.get(str(target["node_uuid"]))
            if node is None:
                continue
            name = str(target["parameter"])
            node.setdefault("param", {})[name] = site_uuid
            template = node.get("entry_param_template")
            if isinstance(template, dict) and owner_reference(template.get(name)) is not None:
                template[name] = site_uuid
            touched.add(str(node["uuid"]))
        if selection.get("input") and owner_reference(input_value.get(selection["input"])) is not None:
            input_value[selection["input"]] = site_uuid
        records.append(dict(selection))
    plan["site_selections"] = records
    return touched


def node_reservations(plan: Optional[Mapping[str, Any]], node_uuid: str) -> list[str]:
    """用到某节点的选择对应的入口预留 uuid。"""

    return [
        str(selection["reservation_uuid"])
        for selection in (plan or {}).get("site_selections") or []
        if selection.get("reservation_uuid")
        and any(str(target.get("node_uuid")) == node_uuid for target in selection.get("targets") or [])
    ]


__all__ = [
    "NO_ACCEPTABLE_SITE",
    "NO_SITE_RELATION",
    "OCCUPANT_UNDECLARED",
    "OCCUPANT_UNKNOWN",
    "OWNER_MISMATCH",
    "OWNER_NOT_FOUND",
    "RESERVATION_TTL_SECONDS",
    "SiteSelectionError",
    "SiteSelectionRequest",
    "WAIT_SITE_SELECTION",
    "check_owner_sites",
    "freeze_site_selections",
    "has_owner_selections",
    "node_reservations",
    "owner_site_requests",
    "read_owner_facts",
    "reservation_payload",
]
