"""物料参数按库位绑定：``{"site_uuid": ...}`` 在准入时解析成占着该库位的物料并冻结进任务。

物料参数（``ResourceSlot`` / ``list[ResourceSlot]``）可以给 ``{"site_uuid": "<库位 uuid>"}``，表示「此刻占着这个
库位的物料」：工作流入口参数、节点静态参数、单点动作参数都可以这样写。建任务与运行预检走同一条路径：

1. :func:`site_references` 收集要读的库位（入口参数与图里节点参数的顶层值、列表元素）；
2. 一次向物料权威读库位事实（``materials.site_admission_facts``，只读）；
3. :func:`bind_material_sites` 把计划节点参数（含暂停改参时重新代入用的 ``entry_param_template``）与任务输入里的
   ``{"site_uuid"}`` 换成占用物料的 ``{"uuid": ...}``。参数写了 ``AllowedResourceTemplates(...)`` 时核对占用物料的模板。

工作流输入的允许模板来自 ``@workflow(allowed_templates=...)``，发布时写进输入 schema 的
``allowed_resource_template_uuids``（:func:`input_template_allowlists`）；按库位绑定与直接给 ``{"uuid"}`` 的输入都按它核对
（后者见 :func:`check_input_material_templates`）。

之后它就是普通物料参数：默认共享物料锁、派发前的物料核对照旧。库位不存在、库位是空的、占用物料不是参数允许的模板
都是预检错误（:class:`MaterialSiteError`，``reason`` 分别为 ``site_not_found`` / ``site_empty`` /
``template_mismatch``），消息里写明库位与参数。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Optional

from unilabos.protocol.runtime.site_references import site_reference

#: 预检 / 建任务错误的 ``reason``
SITE_NOT_FOUND = "site_not_found"
SITE_EMPTY = "site_empty"
TEMPLATE_MISMATCH = "template_mismatch"


class MaterialSiteError(ValueError):
    """按库位绑定的物料参数解析不出来（预检错误）：``reason`` 见模块说明，``details`` 带库位与参数。"""

    def __init__(
        self, reason: str, message: str, *, parameter: str, site_uuid: str, node_uuid: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details: dict[str, Any] = {"reason": reason, "parameter": parameter, "site_uuid": site_uuid}
        if node_uuid is not None:
            self.details["node_uuid"] = node_uuid


def _values(value: Any) -> Iterable[Any]:
    """参数值本身及其列表元素（``list[ResourceSlot]`` 的每一项都可以按库位绑定）。"""

    yield value
    if isinstance(value, list):
        yield from value


def site_references(params: Iterable[Mapping[str, Any]]) -> set[str]:
    """一组参数（节点参数 / 任务输入）里按库位绑定的库位 uuid。"""

    found: set[str] = set()
    for param in params:
        for value in (param or {}).values():
            for item in _values(value):
                identity = site_reference(item)
                if identity is not None:
                    found.add(identity)
    return found


def graph_site_references(graph: Mapping[str, Any], input_value: Mapping[str, Any]) -> set[str]:
    """建任务前要读的库位：任务输入与图中各节点静态参数里的 ``{"site_uuid"}``。"""

    return site_references([input_value, *((node.get("param") or {}) for node in graph.get("nodes") or [])])


def read_site_facts(materials: Any, site_uuids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """向物料权威读一次库位事实；没有物料权威时报错（按库位绑定必须在准入时解析）。"""

    identities = sorted(set(site_uuids))
    if not identities:
        return {}
    reader = getattr(materials, "site_admission_facts", None)
    if not callable(reader):
        raise RuntimeError("物料权威尚未接入，不能按库位绑定物料")
    facts = reader({"site_uuids": identities})
    return dict((facts or {}).get("sites") or {})


def _template_symbols(schema: Any, parameter: str) -> list[str]:
    contract = (schema or {}).get("x-unilabos-action-contract") if isinstance(schema, Mapping) else None
    symbols = ((contract or {}).get("resource_template_symbols") or {}).get("goal") or {}
    return list(symbols.get(parameter) or [])


def input_template_allowlists(contract: Optional[Mapping[str, Any]]) -> dict[str, list[str]]:
    """工作流输入合同里各物料输入允许的资源模板 uuid（ResourceSlot schema 的 ``allowed_resource_template_uuids``）。"""

    from .handle_projection import resource_slot_schema

    allowlists: dict[str, list[str]] = {}
    for descriptor in (contract or {}).get("parameters") or []:
        slot = resource_slot_schema(descriptor.get("schema") or {})
        uuids = (slot or {}).get("allowed_resource_template_uuids")
        if uuids:
            allowlists[str(descriptor["name"])] = [str(item) for item in uuids]
    return allowlists


def _template_mismatch(where: str, material: str, template_name: Any, allowed: Iterable[str]) -> str:
    return (f"{where}：物料 {material}（模板 {template_name}）不是允许的资源模板"
            f"（{'、'.join(str(item) for item in allowed)}）")


def check_input_material_templates(
    input_value: Optional[Mapping[str, Any]], allowlists: Mapping[str, Sequence[str]],
    template_of: Callable[[str], Mapping[str, Any]],
) -> None:
    """直接给 ``{"uuid"}`` 的工作流物料输入按允许模板核对；``template_of(物料 uuid)`` 给 ``template_uuid`` / ``template_name``。

    按库位绑定的输入在 :func:`bind_material_sites` 里按同一份允许模板核对；不符抛 :class:`MaterialSiteError`
    （``reason=template_mismatch``，``site_uuid`` 为空，``details`` 带 ``material_uuid``）。
    """

    for name, allowed in allowlists.items():
        for item in _values((input_value or {}).get(name)):
            if not isinstance(item, Mapping) or not isinstance(item.get("uuid"), str) or site_reference(item):
                continue
            facts = template_of(item["uuid"])
            if str(facts.get("template_uuid") or "") not in allowed:
                error = MaterialSiteError(
                    TEMPLATE_MISMATCH,
                    _template_mismatch(f"工作流输入 {name}", item["uuid"], facts.get("template_name"), allowed),
                    parameter=name, site_uuid="",
                )
                error.details["material_uuid"] = item["uuid"]
                raise error


def resolve_site_material(
    facts: Mapping[str, Any], site_uuid: str, *, parameter: str, node_uuid: Optional[str] = None,
    template_symbols: Iterable[str] = (), template_uuids: Iterable[str] = (),
) -> dict[str, str]:
    """一个 ``{"site_uuid"}`` → 占用物料的 ``{"uuid"}``；库位不存在 / 为空 / 模板不符时抛 :class:`MaterialSiteError`。"""

    fact = facts.get(site_uuid)
    where = f"参数 {parameter}" + (f"（节点 {node_uuid}）" if node_uuid else "")
    if fact is None:
        # 建任务前读事实之后图又改了：读到的库位集合里没有它
        raise MaterialSiteError(SITE_NOT_FOUND, f"{where} 按库位 {site_uuid} 绑定物料：库位事实未读取，请重试",
                                parameter=parameter, site_uuid=site_uuid, node_uuid=node_uuid)
    if not fact.get("exists"):
        raise MaterialSiteError(SITE_NOT_FOUND, f"{where} 按库位 {site_uuid} 绑定物料：库位不存在",
                                parameter=parameter, site_uuid=site_uuid, node_uuid=node_uuid)
    label = f"{fact.get('label') or site_uuid}（{site_uuid}）"
    occupant = fact.get("occupant")
    if not occupant:
        raise MaterialSiteError(SITE_EMPTY, f"{where} 按库位 {label} 绑定物料：库位上没有物料",
                                parameter=parameter, site_uuid=site_uuid, node_uuid=node_uuid)
    symbols = list(template_symbols)
    if symbols and occupant.get("module_name") not in symbols:
        raise MaterialSiteError(
            TEMPLATE_MISMATCH,
            f"{where} 按库位 {label} 绑定物料：库位上的物料 {occupant.get('material_uuid')}"
            f"（模板 {occupant.get('template_name')}）不是参数允许的资源模板（{'、'.join(symbols)}）",
            parameter=parameter, site_uuid=site_uuid, node_uuid=node_uuid,
        )
    allowed = list(template_uuids)
    if allowed and str(occupant.get("template_uuid") or "") not in allowed:
        raise MaterialSiteError(
            TEMPLATE_MISMATCH,
            _template_mismatch(f"{where} 按库位 {label} 绑定物料", occupant.get("material_uuid"),
                               occupant.get("template_name"), allowed),
            parameter=parameter, site_uuid=site_uuid, node_uuid=node_uuid,
        )
    return {"uuid": str(occupant["material_uuid"])}


def _bind_param(
    param: dict[str, Any], facts: Mapping[str, Any], *, node_uuid: Optional[str], schema: Any,
    bindings: list[dict[str, Any]], allowlists: Optional[Mapping[str, Sequence[str]]] = None,
) -> None:
    for name, value in list(param.items()):
        symbols = _template_symbols(schema, name)
        uuids = list((allowlists or {}).get(name) or [])

        def bind(item: Any) -> Any:
            site = site_reference(item)
            if site is None:
                return item
            material = resolve_site_material(facts, site, parameter=name, node_uuid=node_uuid,
                                             template_symbols=symbols, template_uuids=uuids)
            record = {"parameter": name, "site_uuid": site, "material_uuid": material["uuid"]}
            if node_uuid is not None:
                record["node_uuid"] = node_uuid
            bindings.append(record)
            return material

        if isinstance(value, list):
            param[name] = [bind(item) for item in value]
        else:
            param[name] = bind(value)


def bind_param_sites(
    param: dict[str, Any], facts: Mapping[str, Any], *, schema: Any = None, node_uuid: Optional[str] = None,
) -> list[dict[str, Any]]:
    """就地绑定一组动作参数（如单点设备动作的参数）；``schema`` 是动作 schema，用于模板核对。"""

    bindings: list[dict[str, Any]] = []
    _bind_param(param, facts, node_uuid=node_uuid, schema=schema, bindings=bindings)
    return bindings


def bind_material_sites(
    plan: Optional[dict[str, Any]], input_value: Optional[dict[str, Any]], facts: Mapping[str, Any],
    *, input_contract: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """就地把计划与任务输入里的 ``{"site_uuid"}`` 换成占用物料 ``{"uuid"}``，返回绑定记录。

    计划节点的 ``param`` 与 ``entry_param_template``（暂停改参时按它重新代入）一起换；任务输入里的同形值也换，
    改参后重新代入的仍是冻结的物料。绑定记录写进计划的 ``material_site_bindings``（追溯用）。
    ``input_contract`` 给了时任务输入按其中的允许模板（:func:`input_template_allowlists`）核对占用物料。
    """

    bindings: list[dict[str, Any]] = []
    for node in (plan or {}).get("nodes") or []:
        schema = node.get("param_schema")
        _bind_param(node.setdefault("param", {}), facts, node_uuid=node["uuid"], schema=schema, bindings=bindings)
        template = node.get("entry_param_template")
        if isinstance(template, dict):
            _bind_param(template, facts, node_uuid=node["uuid"], schema=schema, bindings=[])
    if input_value is not None:
        _bind_param(input_value, facts, node_uuid=None, schema=None, bindings=bindings,
                    allowlists=input_template_allowlists(input_contract))
    if plan is not None and bindings:
        plan["material_site_bindings"] = bindings
    return bindings


def bind_jobs(plan: Mapping[str, Any], jobs: Iterable[dict[str, Any]]) -> None:
    """计划节点参数已经冻结：同步到各节点运行的首个 attempt。"""

    params = {node["uuid"]: node.get("param") for node in plan.get("nodes") or []}
    for job in jobs:
        if job.get("workflow_node_uuid") in params:
            job["param"] = dict(params[job["workflow_node_uuid"]] or {})


__all__ = [
    "MaterialSiteError",
    "check_input_material_templates",
    "input_template_allowlists",
    "SITE_EMPTY",
    "SITE_NOT_FOUND",
    "TEMPLATE_MISMATCH",
    "bind_jobs",
    "bind_material_sites",
    "bind_param_sites",
    "graph_site_references",
    "read_site_facts",
    "resolve_site_material",
    "site_references",
]
