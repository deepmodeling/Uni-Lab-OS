"""静态来源的只读事实与领取门禁共用同一库存权威，不创建谱系或修改公开输入。"""
from __future__ import annotations

import json
from typing import Any

from unilabos.app.scheduler.inventory.domain import InventoryError, MaterialRequirement
from unilabos.app.scheduler.inventory.handoff_adapter import inventory_snapshot
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.source_preflight import _candidate_materials, material_source_checks
from unilabos.center_protocol.digest import digest
from unilabos.persistence.inspection import table_exists


def selector_digest(requirement, policy: str) -> str:
    return digest({"requirement": requirement.to_dict(), "custody_policy": policy})


def source_nodes(plan):
    return [node for node in plan.get("nodes", []) if node.get("kind") == "material_source"
        and node.get("param", {}).get("custody_policy") == "task_exclusive"]


def known_lineages(c, local: str) -> tuple[set[str], bool]:
    known = {row[0] for row in c.execute(
        "SELECT material_lineage_uuid FROM material_station_binding WHERE local_material_uuid=?", (local,))}
    pending = False
    if table_exists(c, "station_material_export_item"):
        for row in c.execute("SELECT i.material_lineage_uuid,e.completed FROM station_material_export_item i "
                "JOIN station_material_export e USING(invocation_uuid) WHERE i.local_material_uuid=?", (local,)):
            if row["material_lineage_uuid"] and row["completed"]:
                known.add(row["material_lineage_uuid"])
            else:
                pending = True
    return known, pending


def _candidate(c, requirement, *, task_uuid="", allow_busy=False):
    try:
        return InventoryService._tx_resolve_instance(c, requirement, exclude_active_shared=True,
            workflow_id=task_uuid)["edge_uuid"]
    except InventoryError:
        # 仅旧包缺绑定诊断需要读取暂占身份；新冻结证明不能承诺不可领取的来源。
        if not allow_busy:
            raise
        candidates = _candidate_materials(c, requirement)
        if len(candidates) != 1 or not (requirement.instance_uuid or requirement.site_uuid):
            raise
        row = candidates[0]
        if row["resource_template_uuid"] != requirement.template_id or row["inventory_status"] not in {"warehouse", "reserved"} or row["disposition"] != "active":
            raise
        return row["uuid"]


def _require_carrier_root(authority, c, local: str) -> None:
    """当前只支持整载体来源，未知子物料也不能漏掉已登记父载体的其余内容。"""
    parent = authority._material(c, local)["parent_uuid"]
    seen = {local}
    while parent is not None:
        if parent in seen:
            raise ValueError("静态来源的父物料关系存在循环")
        seen.add(parent)
        if c.execute("SELECT 1 FROM container_station_binding WHERE carrier_material_uuid=?", (parent,)).fetchone():
            raise ValueError("静态来源暂不支持只选择已登记载体子物料")
        parent = authority._material(c, parent)["parent_uuid"]


def bindings_for_plan(authority, plan) -> list[dict[str, Any]]:
    bindings = []
    with authority.store.read_connection() as c:
        for node in source_nodes(plan):
            if node.get("runtime_material_source"):
                raise ValueError("中心独占来源尚需运行时选料，不能冻结可信物料身份")
            checks = material_source_checks(InventoryService(authority.store), {"nodes": [node]})
            blocked = next((check for check in checks if check["blocking"]), None)
            if blocked is not None:
                raise ValueError(blocked["message"])
            requirement = MaterialRequirement.from_dict(node["material_requirements"][0])
            local = _candidate(c, requirement)
            _require_carrier_root(authority, c, local)
            snapshot = inventory_snapshot(authority, c, local)
            refs, any_known = [], False
            for material in snapshot["materials"]:
                identity = material["local_material_uuid"]
                row = authority._material(c, identity)
                if row["inventory_status"] not in {"warehouse", "reserved"} or row["disposition"] != "active":
                    raise ValueError("静态来源载体或内容不在本站可用库存")
                if c.execute("SELECT 1 FROM station_handoff_resource WHERE material_uuid=? AND active=1", (identity,)).fetchone():
                    raise ValueError("静态来源载体或内容仍受交接保护")
                known, pending = known_lineages(c, identity)
                if pending or len(known) > 1:
                    raise ValueError("静态来源谱系尚未确认或存在歧义")
                any_known |= bool(known)
                if known:
                    refs.append({"station_uuid": authority.station_uuid, "local_material_uuid": identity,
                        "local_revision": material["local_revision"], "material_lineage_uuid": next(iter(known))})
            lineage: dict[str, Any] = {"kind": "unseen"}
            if any_known:
                if len(refs) != len(snapshot["materials"]):
                    raise ValueError("静态来源完整树混合未知谱系，不能部分冻结")
                binding = c.execute("SELECT * FROM container_station_binding WHERE carrier_material_uuid=?", (local,)).fetchone()
                if binding is None or binding["station_uuid"] != authority.station_uuid:
                    raise ValueError("静态历史来源必须是已登记载体，暂不支持只选择载体子物料")
                versions = []
                for row in c.execute("SELECT package_json FROM station_handoff"):
                    versions.extend(item["current_state_version"] for item in json.loads(row[0])["items"] if item["container_uuid"] == binding["container_uuid"])
                if table_exists(c, "station_material_export"):
                    for row in c.execute("SELECT containers_json FROM station_material_export WHERE completed=1 AND containers_json IS NOT NULL"):
                        versions.extend(item["center_snapshot"]["state_version"] for item in json.loads(row[0]) if item["container_uuid"] == binding["container_uuid"])
                if not versions:
                    raise ValueError("静态历史来源缺少持久容器版本证明")
                lineage = {"kind": "retained", "container_uuid": binding["container_uuid"],
                    "container_state_version": max(versions), "material_refs": refs}
            bindings.append({"node_uuid": node["uuid"], "selector_digest": selector_digest(requirement, "task_exclusive"),
                "material_uuid": local, "resource_template_uuid": requirement.template_id,
                "local_revision": authority._material(c, local)["aggregate_version"],
                "inventory_snapshot": snapshot, "lineage": lineage})
    if bindings:
        from unilabos.center_protocol.material_source import validate_material_source_bindings
        return validate_material_source_bindings(bindings, station_uuid=authority.station_uuid)
    return []


def validate_plan_bindings(authority, plan, payload, *, task_uuid="") -> None:
    """以原发布计划校验内部来源入口；缺绑定的历史中心来源不能走本地退路。"""
    nodes = {node["uuid"]: node for node in source_nodes(plan)}
    supplied = {item["node_uuid"]: item for item in payload.get("material_source_bindings", [])}
    if set(supplied) - set(nodes):
        raise ValueError("冻结来源绑定不属于原发布合同的独占来源节点")
    if nodes and authority is None:
        raise ValueError("中心物料来源缺少本站库存权威")
    if not nodes:
        return
    public_refs = {ref["local_material_uuid"] for ref in payload["material_refs"]}
    with authority.store.read_connection() as c:
        for identity, node in nodes.items():
            if node.get("runtime_material_source"):
                if identity in supplied:
                    raise ValueError("动态物料来源不能使用静态冻结证明")
                continue
            requirement = MaterialRequirement.from_dict(node["material_requirements"][0])
            binding = supplied.get(identity)
            if binding:
                if binding["selector_digest"] != selector_digest(requirement, "task_exclusive") or binding["resource_template_uuid"] != requirement.template_id:
                    raise ValueError("来源绑定与原发布选择器或模板不一致")
            elif supplied:
                raise ValueError("冻结来源绑定未覆盖全部静态独占来源")
            else:
                local = _candidate(c, requirement, task_uuid=task_uuid, allow_busy=True)
                if local not in public_refs and any(any(known_lineages(c, child)) for child in authority._subtree(c, local)):
                    raise ValueError("中心静态来源已有谱系但缺少冻结来源绑定，禁止降级为本地选料")


def source_resources(authority, c, payload):
    """首次领取时核实完整树、谱系及版本，返回实际资源和受验证的内部入口。"""
    from unilabos.app.scheduler.resource_lock import device_lock_key, material_lock_key
    resources, inputs = {}, []
    for binding in payload.get("material_source_bindings", []):
        local = binding["material_uuid"]
        _require_carrier_root(authority, c, local)
        current = inventory_snapshot(authority, c, local)
        if digest(current) != digest(binding["inventory_snapshot"]):
            raise ValueError("来源物料、内容或结构已变化，冻结证明失效")
        lineage = binding["lineage"]
        for material in current["materials"]:
            identity = material["local_material_uuid"]
            row = authority._material(c, identity)
            if row["inventory_status"] != "warehouse" or row["disposition"] != "active":
                raise ValueError("冻结来源载体或内容当前不可用")
            if c.execute("SELECT 1 FROM station_handoff_resource WHERE material_uuid=? AND active=1", (identity,)).fetchone():
                raise ValueError("冻结来源仍受交接保护")
            known, pending = known_lineages(c, identity)
            if lineage["kind"] == "unseen" and (known or pending):
                raise ValueError("首次来源已出现谱系或未决申请，禁止重新声明新物料")
            if lineage["kind"] == "retained":
                expected = {ref["material_lineage_uuid"] for ref in lineage["material_refs"] if ref["local_material_uuid"] == identity}
                if pending or known != expected or len(expected) != 1:
                    raise ValueError("来源谱系已变化、存在歧义或仍有未决申请")
            resources[material_lock_key(identity)] = identity
            resources[device_lock_key(identity)] = identity
        if lineage["kind"] == "retained":
            refs = {ref["local_material_uuid"]: {**ref, "handoff_item_uuid": None} for ref in lineage["material_refs"]}
            actual = {ref["local_material_uuid"]: ref for ref in payload["material_refs"]}
            if any(actual.get(identity) != ref for identity, ref in refs.items()):
                raise ValueError("静态来源谱系不在中心冻结输入引用中")
            if {"container_uuid": lineage["container_uuid"], "state_version": lineage["container_state_version"]} not in payload["container_refs"]:
                raise ValueError("静态来源容器版本不在中心冻结引用中")
            inputs.append({"local_material_uuid": local, "resource_template_uuid": binding["resource_template_uuid"]})
    return resources, inputs


def verify_source_admission(c, task_uuid, request, existing=None) -> None:
    """在正式选料同一写事务内核对冻结身份；已准入重放仅复用原持久绑定。"""
    from types import SimpleNamespace

    from unilabos.app.scheduler.inventory.domain import CommandRejected
    from unilabos.app.scheduler.inventory.handoff import StationHandoffAuthority

    context = request.center_source_context
    binding = next((item for item in context["bindings"] if item["node_uuid"] == request.node_id), None)
    if request.custody_policy != "task_exclusive":
        if binding:
            raise CommandRejected("共享来源不能携带中心独占来源绑定")
        return
    if binding:
        hold = c.execute("SELECT context_json FROM station_material_input WHERE local_task_uuid=? AND state='active'", (task_uuid,)).fetchone()
        if hold is None:
            raise CommandRejected("冻结来源缺少本任务持久库存输入占用")
        payload = json.loads(hold[0])["package"]["payload"]
        if binding not in payload.get("material_source_bindings", []) or binding["selector_digest"] != selector_digest(request.requirement, request.custody_policy):
            raise CommandRejected("正式来源请求与原持久输入证明不一致")
        if existing is not None:
            if existing["material_uuid"] != binding["material_uuid"]:
                raise CommandRejected("原来源绑定与中心冻结物料身份不一致")
            return
    elif existing is not None:
        return
    selected = _candidate(c, request.requirement, task_uuid=task_uuid)
    if binding and selected != binding["material_uuid"]:
        raise CommandRejected("来源库位物料已改变，禁止改选冻结身份之外的物料")
    identity = c.execute("SELECT identity_json FROM station_handoff_identity WHERE singleton=1").fetchone()
    if identity is None:
        raise CommandRejected("中心来源缺少本站库存身份")
    authority = SimpleNamespace(station_uuid=json.loads(identity[0])[0],
        _subtree=StationHandoffAuthority._subtree, _material=StationHandoffAuthority._material)
    try:
        if binding:
            source_resources(authority, c, {**payload, "material_source_bindings": [binding]})
        elif selected not in {ref["local_material_uuid"] for ref in context["material_refs"]}:
            if any(any(known_lineages(c, child)) for child in authority._subtree(c, selected)):
                raise ValueError("中心来源已有谱系但缺少冻结身份，禁止降级为本地选料")
    except ValueError as error:
        raise CommandRejected(str(error)) from error
