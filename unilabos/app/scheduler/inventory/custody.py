"""现有任务独占来源的实际载体范围；不另建锁或占用状态。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from unilabos.workflow.resource_lock_key import parse_canonical_resource_lock_key


def material_subtree(connection, material_uuid: str) -> set[str]:
    """同时按真实父子关系和实际库位读取完整载体，循环由 UNION 去重关闭。"""
    return {
        row[0]
        for row in connection.execute(
            """WITH RECURSIVE edges(parent_uuid,uuid) AS (
        SELECT parent_uuid,uuid FROM material WHERE deleted_at IS NULL
        UNION SELECT material_uuid,occupied_material_uuid FROM site
        WHERE deleted_at IS NULL AND occupied_material_uuid IS NOT NULL
    ), children(uuid) AS (SELECT ? UNION SELECT e.uuid FROM edges e JOIN children c ON e.parent_uuid=c.uuid)
    SELECT uuid FROM children""",
            (material_uuid,),
        ).fetchall()
    }


def material_custody_blocker(
    connection, material_uuids: Sequence[str], *, task_uuid: str = "", exclusive_request: bool = False
) -> dict[str, Any] | None:
    """载体与内容任意相交即为同一保管范围；共享来源仅与其他任务独占互斥。"""
    selected = (
        set().union(*(material_subtree(connection, local) for local in set(material_uuids)))
        if material_uuids
        else set()
    )
    return _selected_custody_blocker(connection, selected, task_uuid=task_uuid,
                                     exclusive_request=exclusive_request)


def _selected_custody_blocker(connection, selected: set[str], *, task_uuid: str,
                              exclusive_request: bool = False) -> dict[str, Any] | None:
    """检查已按作用域解析的物理集合；保管方仍覆盖原载体及其内容。"""
    if not selected:
        return None
    rows = connection.execute(
        "SELECT * FROM inventory_material_source_binding WHERE status='active' AND workflow_id<>? ORDER BY created_at,binding_id",
        (task_uuid,),
    ).fetchall()
    for row in rows:
        covered = material_subtree(connection, row["material_uuid"])
        if row["custody_policy"] == "task_exclusive":
            reservation = connection.execute(
                "SELECT amounts_json FROM inventory_reservation WHERE workflow_id=? AND node_id=? AND attempt=?",
                (row["workflow_id"], row["node_id"], row["attempt"]),
            ).fetchone()
            if reservation is not None:
                original = json.loads(reservation["amounts_json"]).get("custody_material_uuids", [])
                if not isinstance(original, list) or any(not isinstance(local, str) or not local for local in original):
                    raise ValueError("原任务保管范围损坏，必须核实")
                covered.update(original)
        if (exclusive_request or row["custody_policy"] == "task_exclusive") and selected & covered:
            return dict(row)
    return None


def custody_blocker_for_keys(connection, lock_keys: Sequence[str], *, task_uuid: str = "",
                             site_owner_uuids: Sequence[str] = ()) -> dict[str, Any] | None:
    """复用来源保管范围；已由派发条件证明的库位端点只检查自身和选中库位。

    端点的整仓键仍参与执行锁互斥，但不代表搬动仓内所有物料。公共物理修改
    不提供端点集合，保持整载体检查；实际搬动物料也不得放入端点集合。
    """
    selected: set[str] = set()
    for key in lock_keys:
        identity = parse_canonical_resource_lock_key(key)
        if identity is None or not identity.material_uuid:
            continue
        # 设备执行键只涉及设备本身，不能因此使用设备下所有任务的瓶子。
        selected.add(identity.material_uuid)
        if identity.scope == "material" and identity.material_uuid not in site_owner_uuids:
            selected.update(material_subtree(connection, identity.material_uuid))
        elif identity.scope == "material_site":
            occupant = connection.execute(
                "SELECT occupied_material_uuid FROM site WHERE uuid=? AND material_uuid=? AND deleted_at IS NULL",
                (identity.site_uuid, identity.material_uuid),
            ).fetchone()
            if occupant is not None and occupant[0]:
                selected.update(material_subtree(connection, occupant[0]))
    return _selected_custody_blocker(connection, selected, task_uuid=task_uuid)


def require_source_custody_available(connection, *, material_uuid: str, task_uuid: str, policy: str) -> None:
    """来源准入同事务拒绝载体/内容冲突，独占来源进一步检查完整物理使用集合。"""
    from unilabos.app.scheduler.inventory.domain import MaterialClaimedByOtherTask

    blocker = material_custody_blocker(
        connection, [material_uuid], task_uuid=task_uuid, exclusive_request=policy == "task_exclusive"
    )
    if blocker is not None:
        raise MaterialClaimedByOtherTask(
            "实际载体及其内容仍被其他任务使用，不能取得来源准入",
            blocking_workflow_id=blocker["workflow_id"],
        )
    if policy == "task_exclusive":
        from unilabos.app.scheduler.inventory.dispatch_admission import (
            InventoryMutationConflict,
            assert_resource_keys_unclaimed,
        )
        from unilabos.app.scheduler.resource_lock import device_lock_key, material_lock_key

        materials = material_subtree(connection, material_uuid)
        try:
            assert_resource_keys_unclaimed(
                connection,
                lock_keys=[key for local in materials for key in (material_lock_key(local), device_lock_key(local))],
                material_input_task_uuid=task_uuid,
            )
        except InventoryMutationConflict as error:
            raise MaterialClaimedByOtherTask("实际载体或内容仍有动作、输入或交接占用") from error
