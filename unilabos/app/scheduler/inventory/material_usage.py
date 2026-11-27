"""工作流对已准入物料的通用读取与使用标记；不选料、不搬运。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.app.scheduler.inventory.domain import CommandRejected


def material_identity(value: Any) -> str:
    """工作流传入的物料占位符（ResourceSlot）必须带具体 UUID。"""
    if not isinstance(value, Mapping) or not isinstance(value.get("uuid"), str) or not value["uuid"]:
        raise CommandRejected("库存命令要求具体物料引用")
    return value["uuid"]


def _require_task_binding(authority, *, task_uuid: str, material_uuid: str, exclusive: bool) -> None:
    """只允许读写本任务已准入的物料；写操作进一步要求任务独占保管。"""
    policy = " AND custody_policy='task_exclusive'" if exclusive else ""
    row = authority.store.query_one(
        "SELECT 1 FROM inventory_material_source_binding "
        f"WHERE workflow_id=? AND material_uuid=? AND status='active'{policy}",
        (task_uuid, material_uuid),
    )
    if row is None:
        raise CommandRejected(
            "物料不是当前任务已准入的独占来源" if exclusive else "物料不是当前任务已准入的来源"
        )


def _require_readable(authority, *, task_uuid: str, material_uuid: str) -> None:
    """位置读取允许本任务已准入的物料，以及装着它的各级容器（载架、托盘等）。"""
    rows = authority.store.query_all(
        "SELECT material_uuid FROM inventory_material_source_binding "
        "WHERE workflow_id=? AND status='active' "
        "UNION SELECT r.material_uuid FROM station_material_input_resource r "
        "JOIN station_material_input h USING(invocation_uuid) "
        "WHERE h.local_task_uuid=? AND h.state='active'",
        (task_uuid, task_uuid),
    )
    for row in rows:
        current = str(row["material_uuid"])
        visited: set[str] = set()
        while current and current not in visited:
            if current == material_uuid:
                return
            visited.add(current)
            parent = authority.store.query_one(
                "SELECT parent_uuid FROM material WHERE uuid=? AND deleted_at IS NULL", (current,)
            )
            current = str(parent["parent_uuid"] or "") if parent else ""
    raise CommandRejected("物料既不是当前任务已准入的来源，也不是其所在容器")


def locate_material(authority, *, task_uuid: str, material: Any) -> dict[str, Any]:
    """返回物料当前占用的库位及库位所属物料；物料不在任何已登记库位时拒绝。

    可读取对象：本任务已准入的物料及装着它的各级容器（例如已准入收集器所在的托盘）。
    """
    selected = material_identity(material)
    _require_readable(authority, task_uuid=task_uuid, material_uuid=selected)
    own = authority.store.query_one(
        "SELECT name, barcode FROM material WHERE uuid=? AND deleted_at IS NULL", (selected,)
    )
    site = authority.store.query_one(
        "SELECT s.uuid AS site_uuid, s.name AS site_name, s.material_uuid AS owner_uuid, "
        "o.name AS owner_name, o.barcode AS owner_barcode, o.resource_template_uuid AS owner_template "
        "FROM site s JOIN material o ON o.uuid = s.material_uuid "
        "WHERE s.occupied_material_uuid=? AND s.deleted_at IS NULL AND o.deleted_at IS NULL",
        (selected,),
    )
    if site is None:
        raise CommandRejected("物料当前不在任何已登记库位，可能在途或已移出")
    owner_name = str(site["owner_name"] or "")
    if not owner_name or owner_name == site["owner_uuid"]:
        owner_name = str(site["owner_barcode"] or "") or str(site["owner_uuid"])[:8]
    return {
        "material": dict(material),
        "material_name": _display_name(own, selected),
        "owner": {"uuid": str(site["owner_uuid"]), "resource_template_uuid": str(site["owner_template"] or "")},
        "owner_uuid": str(site["owner_uuid"]),
        "owner_name": owner_name,
        "site_uuid": str(site["site_uuid"]),
        "site_name": str(site["site_name"]),
    }


def _display_name(row: Any, uuid: str) -> str:
    """物料的可读名：优先名称，名称就是 UUID 时用条码。"""
    if row is None:
        return uuid[:8]
    name = str(row["name"] or "")
    if name and name != uuid:
        return name
    return str(row["barcode"] or "") or uuid[:8]


def site_occupant(authority, *, task_uuid: str, owner: Any, site: str) -> dict[str, Any]:
    """读取某所属物料下具名库位的当前占用；库位不存在时拒绝，空库位返回未占用。"""
    del task_uuid  # 库位占用读取不改变库存，也不依赖本任务是否准入该库位物料
    owner_uuid = material_identity(owner)
    if not isinstance(site, str) or not site.strip():
        raise CommandRejected("库位名称不能为空")
    row = authority.store.query_one(
        "SELECT s.uuid AS site_uuid, s.occupied_material_uuid AS occupant_uuid, "
        "m.name AS occupant_name, m.barcode AS occupant_barcode, m.resource_template_uuid AS occupant_template "
        "FROM site s LEFT JOIN material m ON m.uuid = s.occupied_material_uuid AND m.deleted_at IS NULL "
        "WHERE s.material_uuid=? AND s.name=? AND s.deleted_at IS NULL",
        (owner_uuid, site.strip()),
    )
    if row is None:
        raise CommandRejected(f"所属物料下不存在库位 {site}")
    occupant = str(row["occupant_uuid"] or "")
    return {
        "occupied": bool(occupant),
        "occupant": {"uuid": occupant, "resource_template_uuid": str(row["occupant_template"] or "")},
        "occupant_uuid": occupant,
        "occupant_name": _display_name(
            {"name": row["occupant_name"], "barcode": row["occupant_barcode"]}, occupant
        ) if occupant else "",
        "site_uuid": str(row["site_uuid"]),
    }


def mark_material_used(authority, *, task_uuid: str, job_uuid: str, material: Any, reason: str = "") -> dict[str, Any]:
    """经库存命令层把物料标为已使用；命令身份即作业身份，崩溃重放直接返回已提交结果。"""
    from unilabos.app.scheduler.inventory.commands import execute_command

    selected = material_identity(material)
    _require_task_binding(authority, task_uuid=task_uuid, material_uuid=selected, exclusive=True)
    before = authority.store.query_one(
        "SELECT status FROM material_instance WHERE edge_uuid=?", (selected,),
    )
    if before is None:
        raise CommandRejected("物料实例不存在")
    response = execute_command(authority, {
        "command_id": job_uuid,
        "type": "material.mark_used",
        "actor": "workflow",
        "payload": {"edge_uuid": selected, "reason": reason or None},
    })
    if response.get("status") != "completed":
        raise CommandRejected(str(response.get("error") or "标记已使用失败"))
    result = response.get("result") or {}
    previous = str((result.get("previous_status") if isinstance(result, Mapping) else None) or before["status"])
    return {"material": dict(material), "previous_status": previous, "status": str(result.get("status") or "used")}
