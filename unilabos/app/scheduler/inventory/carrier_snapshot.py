"""已准入物料的容器转运快照；所有事实来自同一个 OS 库存读事务。"""

from collections.abc import Mapping

from unilabos.app.scheduler.inventory.domain import CommandRejected, InsufficientStock


def inspect_carrier_transfer(
    authority, *, task_uuid, material, target_owner, target_site
):
    """目标库位由独立物料引用与名称指定，不允许重新选择当前任务的物料。"""

    def identity(value):
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("uuid"), str)
            or not value["uuid"]
        ):
            raise CommandRejected("容器快照要求具体物料引用")
        return value["uuid"]

    selected = identity(material)
    owner = identity(target_owner)
    with authority._tx() as conn:
        binding = conn.execute(
            "SELECT 1 FROM inventory_material_source_binding WHERE workflow_id=? AND material_uuid=? AND status='active' AND custody_policy='task_exclusive'",
            (task_uuid, selected),
        ).fetchone()
        if binding is None:
            raise CommandRejected("物料不是当前任务已经准入的独占来源")
        item = conn.execute(
            "SELECT material_uuid,name FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL",
            (selected,),
        ).fetchone()
        if item is None:
            raise CommandRejected("已准入物料不在已登记的实际孔位")
        carrier = conn.execute(
            "SELECT material_uuid,name FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL",
            (item["material_uuid"],),
        ).fetchone()
        if carrier is None:
            raise CommandRejected("物料的容器不在已登记库位，可能已经离架或在途")
        target = conn.execute(
            "SELECT occupied_material_uuid FROM site WHERE material_uuid=? AND name=? AND deleted_at IS NULL",
            (owner, target_site),
        ).fetchone()
        if target is None:
            raise CommandRejected("目标库位不存在")
        occupant = target["occupied_material_uuid"] or ""
        carrier_material = conn.execute(
            "SELECT barcode FROM material WHERE uuid=? AND deleted_at IS NULL",
            (item["material_uuid"],),
        ).fetchone()
        if carrier_material is None:
            raise CommandRejected("物料的容器已删除或不存在")
        carrier_barcode = carrier_material["barcode"]
        target_material = conn.execute(
            "SELECT barcode FROM material WHERE uuid=? AND deleted_at IS NULL",
            (occupant,),
        ).fetchone()
        if occupant and target_material is None:
            raise CommandRejected("目标库位引用了不存在的容器")
        claims = _foreign_content_claims(conn, occupant, task_uuid)
        if occupant != item["material_uuid"]:
            if claims:
                raise InsufficientStock("目标容器仍有其他任务的物料预留，暂不能换走容器")
            if _foreign_content_claims(conn, item["material_uuid"], task_uuid):
                raise InsufficientStock("来源容器仍有其他任务的物料预留，暂不能搬运容器")
        return {
            "material": {"uuid": selected},
            "carrier_uuid": item["material_uuid"],
            "carrier_barcode": carrier_barcode,
            "item_site": item["name"],
            "carrier_owner_uuid": carrier["material_uuid"],
            "carrier_site": carrier["name"],
            "target_occupant_uuid": occupant,
            "target_occupant_barcode": target_material["barcode"]
            if target_material
            else "",
            "foreign_content_claims": int(claims),
        }


def _foreign_content_claims(conn, container_uuid, task_uuid):
    """在同一快照事务内读取整个容器子树的其他任务占用。"""
    return conn.execute(
        """WITH RECURSIVE contents(uuid) AS (
            SELECT ? UNION SELECT s.occupied_material_uuid FROM site s JOIN contents c ON s.material_uuid=c.uuid
            WHERE s.deleted_at IS NULL AND s.occupied_material_uuid IS NOT NULL
        ), claimed(uuid) AS (
            SELECT material_uuid FROM inventory_material_source_binding
            WHERE workflow_id<>? AND status='active'
            UNION SELECT j.value FROM inventory_reservation r, json_each(r.amounts_json, '$.instances') j
            WHERE r.workflow_id<>? AND r.status='active'
            UNION SELECT lease.material_uuid FROM station_execution_lock_lease lease
            JOIN station_execution_claim claim USING(claim_uuid)
            WHERE claim.task_uuid<>? AND lease.state IN ('prepared','reserved','running','uncertain')
        ) SELECT COUNT(*) AS n FROM claimed JOIN contents USING(uuid)
        """,
        (container_uuid, task_uuid, task_uuid, task_uuid),
    ).fetchone()["n"]
