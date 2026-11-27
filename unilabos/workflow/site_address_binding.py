"""把执行图里的库位地址换成物理库位：任务创建时做一次，调度不感知。

工作流作者写的是“上层物料 + 地址名”（台面的 23 号位、堆栈的 7 号料架第 4 层）。地址不是库位：
物理库位在载架上，放上去的物料以载架为父物料。任务创建、补齐执行图身份的那一步（``bind_station_contract``）
调用这里，把每一对“所属物料参数 + 库位名参数”里的所属物料按库存的地址表换成载架。动作参数里的库位名保留
作者写的地址：驱动按它动作（厂家位号，如 ``cassette_10_layer_03``），库存侧的目标解析、来源核对按
“载架 + 地址名”反查地址表得到物理库位（``station_resource.resolve_target_site``、搬运来源核对）。物料来源
（``mount`` + ``site``）不交给驱动，库位名一并换成载架库位名，启动库位绑定与之一致。工作流源码和发布合同
保持作者的写法。

哪两个参数成对，只认动作资源合同里的声明和物料来源节点的 ``mount``/``site``，不按参数名猜：

* ``transfer``：``source_owner_param`` + ``source_site_name_param``，``target_owner_param`` + ``target_site_name_param``；
* ``transfer_step``：``owner_param`` + ``site_param``；
* 物料来源（MaterialSource）：``mount`` + ``site``。

库位参数已经是库位 UUID 的不动（那本来就是物理库位）。所属物料来自上游节点输出、创建任务时还没有值的
也不动——这种动态来源由产出它的节点给出物理身份。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

SiteAddressResolver = Callable[[str, str], Mapping[str, Any] | None]


def _pairs(node: Mapping[str, Any], contract: Mapping[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    transfer = contract.get("transfer")
    if isinstance(transfer, Mapping):
        for endpoint in ("source", "target"):
            owner, site = transfer.get(f"{endpoint}_owner_param"), transfer.get(f"{endpoint}_site_name_param")
            if isinstance(owner, str) and isinstance(site, str):
                pairs.append((owner, site))
    step = contract.get("transfer_step")
    if isinstance(step, Mapping) and isinstance(step.get("owner_param"), str) and isinstance(step.get("site_param"), str):
        pairs.append((step["owner_param"], step["site_param"]))
    params = node.get("param")
    if isinstance(params, Mapping) and isinstance(params.get("mount"), Mapping) and "site" in params:
        pairs.append(("mount", "site"))
    return pairs


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def bind_site_addresses(graph: dict[str, Any], resolve: SiteAddressResolver,
                        contract_of: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> list[dict[str, str]]:
    """就地把执行图节点参数里的库位地址换成物理库位，返回换了哪些（留痕用）。

    参数：``graph`` 是任务自己的执行图副本；``resolve(所属物料 UUID, 名字)`` 是库存的地址解析端口，
    不是地址时返回 ``None``；``contract_of(节点模板)`` 给出冻结的动作资源合同。
    返回：每处替换的节点、参数、原地址和物理库位。异常：地址表与库存不一致时解析端口抛出的异常原样传播，
    任务创建失败——不能带着解析不了的地址进入调度。
    """
    templates = {template.get("uuid"): template for template in graph.get("node_templates") or []
                 if isinstance(template, Mapping)}
    bound: list[dict[str, str]] = []
    for node in graph.get("nodes") or []:
        params = node.get("param") if isinstance(node, Mapping) else None
        if not isinstance(params, dict):
            continue
        contract = contract_of(templates.get(node.get("workflow_node_template_uuid")) or {})
        for owner_param, site_param in _pairs(node, contract):
            owner, site = params.get(owner_param), params.get(site_param)
            if (not isinstance(owner, Mapping) or not isinstance(owner.get("uuid"), str)
                    or not isinstance(site, str) or not site.strip() or _is_uuid(site.strip())):
                continue
            target = resolve(owner["uuid"], site)
            if target is None:
                continue
            params[owner_param] = {**owner, "uuid": str(target["owner_material_uuid"])}
            if owner_param == "mount":
                params[site_param] = str(target["site_name"])
            # 物料来源的启动库位绑定另存了一份库位名（meta_data.unilab.material_source_site_binding.reference），
            # 准入时按 mount + 这个名字解析；mount 换成载架后名字也要换成载架库位名，两处一致。
            binding = ((node.get("meta_data") or {}).get("unilab") or {}).get("material_source_site_binding")
            if owner_param == "mount" and isinstance(binding, dict) and binding.get("reference") == site:
                binding["reference"] = str(target["site_name"])
            bound.append({"node_uuid": str(node.get("uuid") or ""), "owner_param": owner_param, "site_param": site_param,
                          "address_owner_material_uuid": owner["uuid"], "address": site,
                          "owner_material_uuid": str(target["owner_material_uuid"]),
                          "site_uuid": str(target["site_uuid"]), "site_name": str(target["site_name"])})
    return bound


def bind_graph_site_addresses(graph: dict[str, Any], resolver: Any) -> list[dict[str, str]]:
    """用库存资源引用解析器自带的地址端口翻译一份执行图副本，并把翻译记录留在图上。

    参数：``graph`` 是任务自己的图副本；``resolver`` 是组合根注入的资源引用解析器，带
    ``resolve_site_address`` 时才翻译（纯编译、没有库存的场合没有这个端口，图保持原样）。
    返回：本次替换的记录；重复调用时已是物理库位的参数不会再变，之前的记录保留。
    """
    resolve = getattr(resolver, "resolve_site_address", None)
    if resolve is None:
        return []
    from .execution_plan import ExecutionPlanBuilder

    translated = bind_site_addresses(graph, resolve, ExecutionPlanBuilder._frozen_action_resource_contract)
    if translated and isinstance(graph.get("workflow"), dict):
        metadata = graph["workflow"].setdefault("meta_data", {})
        metadata["site_address_bindings"] = [*(metadata.get("site_address_bindings") or []), *translated]
    return translated


def bind_plan_site_addresses(plan: dict[str, Any], jobs: list[dict[str, Any]], resolver: Any) -> list[dict[str, str]]:
    """翻译已经冻结的执行计划和首批作业的参数：覆盖组合调用展开和任务输入代入之后才出现的地址。

    图上的翻译（``bind_graph_site_addresses``）只看得到节点自己写死的参数。组合工作流调用把参数传给展开出的
    子节点、任务输入代入作业参数，都发生在计划构造期间；所以计划和作业定下来之后再按同一规则过一遍。
    每个计划节点自带冻结的动作资源合同；所属物料和库位名取“节点参数叠加作业参数”后的值，换成物理库位后
    两处一起写回，保持一致。

    参数：``plan``、``jobs`` 是任务创建时冻结的计划与作业（就地修改）；``resolver`` 同
    ``bind_graph_site_addresses``。返回：替换记录。
    """
    resolve = getattr(resolver, "resolve_site_address", None)
    if resolve is None:
        return []
    by_node = {str(job.get("workflow_node_uuid") or ""): job for job in jobs if isinstance(job, dict)}
    bound: list[dict[str, str]] = []
    for node in plan.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        job = by_node.get(str(node.get("uuid") or ""))
        node_param = node.get("param") if isinstance(node.get("param"), dict) else {}
        job_param = job.get("param") if job is not None and isinstance(job.get("param"), dict) else {}
        merged = {**node_param, **job_param}
        contract = node.get("action_resource_contract") if isinstance(node.get("action_resource_contract"), Mapping) else {}
        for owner_param, site_param in _pairs({"param": merged}, contract):
            owner, site = merged.get(owner_param), merged.get(site_param)
            if (not isinstance(owner, Mapping) or not isinstance(owner.get("uuid"), str)
                    or not isinstance(site, str) or not site.strip() or _is_uuid(site.strip())):
                continue
            target = resolve(owner["uuid"], site)
            if target is None:
                continue
            site_value = str(target["site_name"]) if owner_param == "mount" else site
            for params in (node_param, job_param):
                if owner_param in params or site_param in params:
                    params[owner_param] = {**owner, "uuid": str(target["owner_material_uuid"])}
                    params[site_param] = site_value
            merged[owner_param], merged[site_param] = {**owner, "uuid": str(target["owner_material_uuid"])}, site_value
            bound.append({"node_uuid": str(node.get("uuid") or ""), "owner_param": owner_param, "site_param": site_param,
                          "address_owner_material_uuid": owner["uuid"], "address": site,
                          "owner_material_uuid": str(target["owner_material_uuid"]),
                          "site_uuid": str(target["site_uuid"]), "site_name": str(target["site_name"])})
    return bound
