"""可信计数测量的库存校正；身份和预留差异必须交人工复核。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from unilabos.app.scheduler.inventory.domain import CommandRejected, InventoryError

if TYPE_CHECKING:
    from unilabos.app.scheduler.inventory.service import InventoryService


class StocktakeReviewRequired(InventoryError):
    """实测差异涉及已分配库存，禁止自动修改数量。"""
    code = "stocktake_review_required"


def reconcile_count(
    service: InventoryService, *, lot_id: str, mount_uuid: str,
    measured_count: int, evidence_job_uuid: str, expected_version: int,
    actor: str, causation_id: str,
) -> dict[str, Any]:
    """在同一事务校验版本、预留及在仓身份，仅修正未实例化的计数库存。

    库存批次必须显式归属测量容器。已实例化、仍在该容器内的物料不能通过
    数量测量删换身份；其数量差异进入复核。调用者须验证来源测量 Job 已成功。
    """
    if isinstance(measured_count, bool) or not isinstance(measured_count, int) or measured_count < 0:
        raise CommandRejected("实测数量必须是非负整数")
    if not all((lot_id, mount_uuid, evidence_job_uuid, actor, causation_id)) or expected_version is None:
        raise CommandRejected("盘点缺少库存、容器、测量作业、版本或审计身份")
    with service._tx() as conn:
        lot = service._tx_get_lot(conn, lot_id)
        service._tx_check_version(lot, expected_version)
        if lot["warehouse_zone_id"] != mount_uuid:
            raise CommandRejected("库存批次不属于本次测量容器")
        service._tx_get_instance(conn, mount_uuid)
        previous = lot["quantity_total"]
        if int(previous) != previous:
            raise CommandRejected("计数盘点不能用于非整数数量库存")
        changed = previous != measured_count
        if changed:
            if lot["quarantined"]:
                raise StocktakeReviewRequired("库存已隔离，须复核后再盘点")
            if lot["quantity_reserved"] > 0:
                raise StocktakeReviewRequired("实测差异涉及任务预留，须停机复核")
            identified = conn.execute(
                "SELECT i.edge_uuid FROM material_instance i "
                "WHERE i.parent_uuid=? LIMIT 1", (mount_uuid,),
            ).fetchone()
            if identified:
                raise StocktakeReviewRequired(
                    f"实测差异涉及已登记物料 {identified['edge_uuid']}，须核对身份"
                )
            # 来源容器正在被其他物理动作占用时，同样禁止按测量覆盖。
            service._tx_assert_physical_mutation_unclaimed(conn, edge_uuid=mount_uuid)
            delta = measured_count - previous
            lot = service._tx_update_lot_quantities(conn, lot, d_total=delta, d_available=delta)
        result = {
            "lot_id": lot_id, "mount_uuid": mount_uuid,
            "previous_count": int(previous), "measured_count": measured_count,
            "changed": changed, "version": lot["version"], "evidence_job_uuid": evidence_job_uuid,
        }
        service._emit(
            conn, service._now_ms(), "lot", lot_id, lot["version"],
            "lot.measurement_reconciled", result, actor=actor,
            causation_id=causation_id, reason="可信测量盘点",
        )
        return result
