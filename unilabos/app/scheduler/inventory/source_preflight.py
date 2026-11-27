"""只读观察已冻结来源候选，复用运行时选料与载体保管规则，不取得占用。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.app.scheduler.inventory.custody import material_subtree, require_source_custody_available
from unilabos.app.scheduler.inventory.domain import CommandRejected, InventoryError, MaterialRequirement, NotFound
from unilabos.app.scheduler.inventory.source_selection import material_source_selector_rank


def _check(node: Mapping[str, Any], code: str, message: str, *, details: dict[str, Any], status: str = "blocked") -> dict[str, Any]:
    return {"type": "material_source", "node_uuid": node["uuid"], "status": status,
            "code": code, "message": message, "blocking": status == "blocked", "details": details}


def _candidate_materials(connection, requirement: MaterialRequirement) -> list[dict[str, Any]]:
    """读取选择范围的实际占位，用于区分空库位、在途和普通暂占。"""
    if requirement.instance_uuid:
        rows = connection.execute("SELECT * FROM material WHERE uuid=? AND deleted_at IS NULL", (requirement.instance_uuid,)).fetchall()
    else:
        selected = [requirement.site_uuid] if requirement.site_uuid else list(requirement.slot_uuids)
        restriction = " AND s.uuid IN (" + ",".join("?" for _ in selected) + ")" if selected else ""
        rows = connection.execute("SELECT m.* FROM site s JOIN material m ON m.uuid=s.occupied_material_uuid "
            "WHERE s.material_uuid=? AND s.deleted_at IS NULL AND m.deleted_at IS NULL" + restriction,
            (requirement.mount_uuid, *selected)).fetchall()
    materials = {row["uuid"]: dict(row) for row in rows}
    if requirement.search_descendants:
        for identity in tuple(materials):
            for descendant in material_subtree(connection, identity):
                row = connection.execute("SELECT * FROM material WHERE uuid=? AND deleted_at IS NULL", (descendant,)).fetchone()
                if row is not None:
                    materials[row["uuid"]] = dict(row)
    return list(materials.values())


def material_source_checks(inventory, plan: Mapping[str, Any], *, task_uuid: str = "", selected: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """检查输入已知的静态来源；动态上游由原有 deferred 与运行时门禁处理。"""
    checks = []
    exclusive_sources: dict[str, str] = {}
    with inventory.store.read_connection() as connection:
        for node in plan.get("nodes", []):
            if node.get("kind") != "material_source" or node.get("runtime_material_source"):
                continue
            requirements = node.get("material_requirements", [])
            if len(requirements) != 1:
                continue  # 结构错误仍由正式执行计划校验报告。
            requirement = MaterialRequirement.from_dict(requirements[0])
            policy = node.get("param", {}).get("custody_policy", "task_exclusive")
            excluded = set().union(*(set(item["custody_material_uuids"]) for item in (selected or [])
                if "task_exclusive" in {policy, item["custody_policy"]}))
            details = {"custody_policy": policy}
            if requirement.site_uuid:
                details["site_uuid"] = requirement.site_uuid
            candidates = _candidate_materials(connection, requirement)
            if excluded:
                excluded.update(row["uuid"] for row in candidates if material_subtree(connection, row["uuid"]) & excluded)
            try:
                instance = inventory._tx_resolve_instance(connection, requirement,
                    exclude_active_shared=policy == "task_exclusive", workflow_id=task_uuid,
                    excluded_instance_uuids=sorted(excluded))
            except InventoryError as error:
                matching = [row for row in candidates if row["resource_template_uuid"] == requirement.template_id]
                if isinstance(error, (CommandRejected, NotFound)):
                    code = "material_source_unavailable" if requirement.instance_uuid and isinstance(error, NotFound) else "invalid_material_source"
                    message = "来源容器不存在或已删除" if code == "material_source_unavailable" else "来源库位或选料条件无效"
                elif not candidates:
                    code, message = "material_source_empty", "源库位无容器"
                elif not matching:
                    code, message = "material_source_unavailable", "源库位容器类型不符合工作流要求"
                elif not any(row["inventory_status"] in {"warehouse", "reserved"} and row["disposition"] == "active" for row in matching):
                    code, message = "material_source_unavailable", "来源容器正在交接、运输中或已不属于可用库存"
                else:
                    code, message = "material_source_busy", "来源容器当前被其他任务占用"
                checks.append(_check(node, code, message, details={**details, "reason": str(error)}))
                continue
            identity = str(instance["edge_uuid"])
            materials = material_subtree(connection, identity)
            details.update(material_uuid=identity, custody_material_uuids=sorted(materials))
            if materials & excluded:
                checks.append(_check(node, "material_source_busy", "同批候选已选用该容器或其内容", details=details))
                continue
            rows = [connection.execute("SELECT inventory_status,disposition FROM material WHERE uuid=? AND deleted_at IS NULL", (local,)).fetchone()
                    for local in materials]
            handoff = any(connection.execute("SELECT 1 FROM station_handoff_resource WHERE material_uuid=? AND active=1 LIMIT 1", (local,)).fetchone()
                          for local in materials)
            if handoff or any(row is None or row["inventory_status"] not in {"warehouse", "reserved"}
                              or row["disposition"] != "active" for row in rows):
                checks.append(_check(node, "material_source_unavailable", "来源容器正在交接、运输中或已不属于可用库存", details=details))
                continue
            try:
                require_source_custody_available(connection, material_uuid=identity, task_uuid=task_uuid, policy=policy)
                if instance["status"] != "warehouse":
                    raise InventoryError("来源容器已被任务预留")
                from unilabos.app.scheduler.inventory.outbound import outbound_blocker
                from unilabos.app.scheduler.resource_lock import material_lock_key

                if outbound_blocker(connection, [material_lock_key(local) for local in materials]) is not None:
                    raise InventoryError("来源容器仍受原任务出站责任保护")
            except InventoryError as error:
                checks.append(_check(node, "material_source_busy", str(error), details=details))
            else:
                # 只比较确定的实例/单库位来源；宽范围在正式准入时仍可选择下一只。
                fixed_container = bool(requirement.instance_uuid or (not requirement.search_descendants
                    and (requirement.site_uuid or len(requirement.slot_uuids) == 1)))
                if policy == "task_exclusive" and fixed_container and identity in exclusive_sources:
                    checks.append(_check(node, "material_source_batch_conflict", "同一任务的多个静态独占来源重复选用同一容器",
                        details={**details, "conflicting_node_uuid": exclusive_sources[identity]}))
                else:
                    checks.append(_check(node, "material_source_ready", "源库位容器当前可用，提交时仍将原子复核", details=details, status="passed"))
                    if policy == "task_exclusive" and fixed_container:
                        exclusive_sources[identity] = str(node["uuid"])
    return checks


def batch_material_source_checks(inventory, plans: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """在同一只读锁内试分配整批；宽选择器可使用下一只容器，不改变正式库存。"""
    selected: list[tuple[int, dict[str, Any]]] = []
    checks = []
    sources = [(index, node) for index, plan in enumerate(plans) for node in plan.get("nodes", [])
        if node.get("kind") == "material_source" and not node.get("runtime_material_source")
        and len(node.get("material_requirements", [])) == 1]
    # 与正式准入使用同一选择优先级；排序不改变公开报告的原候选位置。
    sources.sort(key=lambda item: (
        material_source_selector_rank(MaterialRequirement.from_dict(item[1]["material_requirements"][0])),
        item[1].get("param", {}).get("custody_policy", "task_exclusive") != "shared_source",
        item[1]["uuid"], item[0],
    ))
    with inventory.store.read_connection():
        for index, node in sources:
            plan = {"nodes": [node]}
            current = material_source_checks(inventory, plan,
                selected=[details for selected_index, details in selected if selected_index != index])
            baseline = {check["node_uuid"]: check for check in material_source_checks(inventory, plan)}
            for check in current:
                if check["status"] == "blocked" and baseline[check["node_uuid"]]["code"] == "material_source_ready":
                    checks.append({**check, "code": "material_source_batch_conflict",
                        "message": "同批实验重复选用同一独占容器或其内容",
                        "details": {**check["details"], "item_index": index}})
            selected.extend((index, check["details"]) for check in current if check["code"] == "material_source_ready")
    return checks
