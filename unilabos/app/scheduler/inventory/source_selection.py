"""在既有来源准入事务内按部署 Site 层级选择物料，不维护额外分配账。"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from unilabos.app.scheduler.inventory.domain import (
    CommandRejected,
    InsufficientStock,
    MaterialClaimedByOtherTask,
    MaterialRequirement,
)


def material_source_selector_rank(requirement: MaterialRequirement) -> int:
    """来源试分配与正式准入共用窄范围优先，避免宽范围抢走唯一候选。"""
    if requirement.instance_uuid or requirement.barcode or requirement.site_uuid:
        return 0
    if requirement.slot_uuids:
        return 1
    return 2


def material_claim_owner(
    connection: sqlite3.Connection,
    instance_uuids: list[str],
    workflow_id: str | None,
) -> str | None:
    """返回候选物料的实际占用任务；未找到时保持缺料语义。"""

    if not instance_uuids or not workflow_id:
        # 不知道请求方任务身份时无法区分“别人占用”与“自己占用”，保持缺料语义，
        # 避免任务等待自己的预留。
        return None
    placeholders = ",".join("?" for _ in instance_uuids)
    row = connection.execute(
        f"""WITH candidates AS (
            SELECT edge_uuid FROM material_instance WHERE edge_uuid IN ({placeholders})
        ), owners(workflow_id) AS (
            SELECT r.workflow_id FROM inventory_reservation AS r
            JOIN json_each(r.amounts_json, '$.instances') AS reserved
            JOIN candidates AS c ON c.edge_uuid=reserved.value
            WHERE r.status='active'
            UNION
            SELECT b.workflow_id FROM inventory_material_source_binding AS b
            JOIN candidates AS c ON c.edge_uuid=b.material_uuid WHERE b.status='active'
            UNION
            SELECT h.local_task_uuid FROM station_material_input_resource AS r
            JOIN station_material_input AS h USING(invocation_uuid)
            JOIN candidates AS c ON c.edge_uuid=r.material_uuid WHERE h.state='active'
        )
        SELECT workflow_id FROM owners WHERE workflow_id<>? ORDER BY workflow_id LIMIT 1""",
        (*instance_uuids, workflow_id),
    ).fetchone()
    return str(row[0]) if row is not None else None


def select_descendant_instance(
    connection: sqlite3.Connection,
    requirement: MaterialRequirement,
    *,
    selected_sites: list[str],
    exclude_active_shared: bool,
    workflow_id: str | None = None,
    excluded_instance_uuids: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """深度优先按各层 Site 顺序选料；根库位约束限定完整子树。

    只遍历权威 Site 实际占位。离架、在途和缺失物料不在候选集合；任意循环或
    过深层级失败关闭。返回实例后由调用方原有预留事务核对状态、模板与占用。
    """
    rows = connection.execute(
        """WITH RECURSIVE mounts(uuid) AS (
            SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL
            UNION
            SELECT s.occupied_material_uuid FROM site s JOIN mounts p ON p.uuid=s.material_uuid
            JOIN material m ON m.uuid=s.occupied_material_uuid AND m.deleted_at IS NULL
            WHERE s.deleted_at IS NULL AND ?
        )
        SELECT s.*, m.resource_template_uuid AS child_template,
               i.status AS instance_status,
               EXISTS(SELECT 1 FROM inventory_material_source_binding b
                 WHERE b.material_uuid=m.uuid AND b.custody_policy='shared_source'
                   AND b.status='active') AS shared_binding,
               EXISTS(SELECT 1 FROM station_material_input_resource r
                 JOIN station_material_input h USING(invocation_uuid)
                 WHERE r.material_uuid=m.uuid AND h.state='active' AND h.local_task_uuid<>?) AS center_input_binding
        FROM site s JOIN mounts p ON p.uuid=s.material_uuid
        JOIN material m ON m.uuid=s.occupied_material_uuid AND m.deleted_at IS NULL
        LEFT JOIN material_instance i ON i.edge_uuid=m.uuid
        WHERE s.deleted_at IS NULL
        ORDER BY s.sort_order, s.create_time, s.uuid, m.create_time, m.uuid""",
        (requirement.mount_uuid, requirement.search_descendants, workflow_id or ""),
    ).fetchall()
    by_parent: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_parent[row["material_uuid"]].append(row)
    candidates: list[str] = []
    # ``claimable`` 是模板匹配、仍在库或已预留的物料，用于区分“被占用”与“缺料”。
    claimable: list[str] = []

    def visit(parent: str, ancestors: tuple[str, ...]) -> None:
        if parent in ancestors or len(ancestors) >= 64:
            raise CommandRejected("物料选料层级存在循环或超过 64 层")
        for row in by_parent.get(parent, ()):
            if (
                parent == requirement.mount_uuid
                and selected_sites
                and row["uuid"] not in selected_sites
            ):
                continue
            child = row["occupied_material_uuid"]
            if (
                row["child_template"] == requirement.template_id
                and child not in excluded_instance_uuids
                and row["instance_status"] == "warehouse"
                and not (exclude_active_shared and row["shared_binding"])
                and not row["center_input_binding"]
            ):
                candidates.append(child)
            elif (
                row["child_template"] == requirement.template_id
                and row["instance_status"] in {"warehouse", "reserved"}
            ):
                claimable.append(child)
            if requirement.search_descendants:
                visit(child, (*ancestors, parent))

    visit(requirement.mount_uuid, ())
    if not candidates:
        blocker = material_claim_owner(connection, claimable, workflow_id)
        if blocker is not None:
            raise MaterialClaimedByOtherTask(
                f"挂载资源 {requirement.mount_uuid} 的匹配物料正被其他任务占用，等待释放",
                blocking_workflow_id=blocker,
            )
        raise InsufficientStock(
            f"挂载资源 {requirement.mount_uuid} 的可达库位没有可用物料"
        )
    return dict(
        connection.execute(
            "SELECT * FROM material_instance WHERE edge_uuid=?", (candidates[0],)
        ).fetchone()
    )
