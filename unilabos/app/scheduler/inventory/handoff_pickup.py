"""可信取货证明只解除根载体外部落位，保留运输保护与内部结构。"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from unilabos.app.scheduler.inventory.dispatch_admission import assert_resource_keys_unclaimed
from unilabos.app.scheduler.resource_lock import device_lock_key, material_lock_key, site_lock_key
from unilabos.center_protocol.digest import canonical_json
from unilabos.persistence.database import Database

if TYPE_CHECKING:
    from unilabos.app.scheduler.inventory.handoff import StationHandoffAuthority


def remember_source_location(connection: Database | sqlite3.Connection, item: Any, carrier: Any) -> None:
    """源放行事务冻结外部库位，不能在迟到取货时猜测原位置。"""
    sites = connection.execute(
        "SELECT uuid FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL", (carrier["uuid"],),
    ).fetchall()
    if len(sites) > 1:
        raise ValueError("根载体同时占用多个来源库位")
    connection.execute(
        "INSERT INTO station_handoff_source_location(handoff_item_uuid,carrier_material_uuid,source_site_uuid,source_parent_uuid) VALUES (?,?,?,?)",
        (item["handoff_item_uuid"], carrier["uuid"], sites[0]["uuid"] if sites else None, carrier["parent_uuid"]),
    )


def apply_pickup(
    authority: StationHandoffAuthority, connection: Database | sqlite3.Connection,
    payload: dict[str, Any], role: str,
) -> None:
    """同事务精确清位并保存原证明；重放不触碰已经复用的空位。"""
    from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError

    if role != "source" or "pickup" not in payload or payload["attention"]:
        return
    if any(item.get("movement_review") or item.get("settlement") for item in payload["items"]):
        return
    proof = canonical_json(payload["pickup"]).decode()
    for center_item in payload["items"]:
        _, item = authority._rows(connection, payload["handoff_uuid"], center_item["handoff_item_uuid"])
        if item["state"] != "protected" or center_item["receipt"] is not None:
            continue
        location = connection.execute(
            "SELECT * FROM station_handoff_source_location WHERE handoff_item_uuid=?", (item["handoff_item_uuid"],),
        ).fetchone()
        if location is None:
            raise HandoffInventoryError("pickup_location_unproven", "原交接未冻结来源位置，取货需要核实")
        if location["pickup_json"] is not None:
            if location["pickup_json"] != proof:
                raise HandoffInventoryError("pickup_conflict", "原取货证明不可改写")
            continue
        carrier = authority._material(connection, item["carrier_material_uuid"])
        if (carrier["uuid"] != location["carrier_material_uuid"]
                or carrier["parent_uuid"] != location["source_parent_uuid"]
                or carrier["inventory_status"] != "handoff_outgoing"):
            raise HandoffInventoryError("pickup_location_changed", "根载体的原位置或运输保护已变化")
        sites = connection.execute(
            "SELECT uuid,material_uuid FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL", (carrier["uuid"],),
        ).fetchall()
        expected = location["source_site_uuid"]
        if ([site["uuid"] for site in sites] != ([expected] if expected else [])):
            raise HandoffInventoryError("pickup_location_changed", "取货不能清除不同于原冻结位置的占用")
        keys = [material_lock_key(carrier["uuid"]), device_lock_key(carrier["uuid"])]
        if expected:
            keys.append(site_lock_key(sites[0]["material_uuid"], expected))
        assert_resource_keys_unclaimed(connection, lock_keys=keys, ignore_handoff_uuid=payload["handoff_uuid"])
        if expected:
            changed = connection.execute(
                "UPDATE site SET occupied_material_uuid=NULL WHERE uuid=? AND occupied_material_uuid=? AND deleted_at IS NULL",
                (expected, carrier["uuid"]),
            ).rowcount
            if changed != 1:
                raise HandoffInventoryError("pickup_location_changed", "来源库位已变化，不能应用取货")
        connection.execute("UPDATE material SET parent_uuid=NULL WHERE uuid=?", (carrier["uuid"],))
        connection.execute("UPDATE station_handoff_source_location SET pickup_json=? WHERE handoff_item_uuid=?",
                           (proof, item["handoff_item_uuid"]))


def source_location_holds(connection, *, ignore_handoff_uuid=""):
    """原位置事实就是持续保护；可信 pickup 已记账后绝不重新锁住复用库位。"""
    from unilabos.persistence.inspection import table_exists
    if not table_exists(connection,"station_handoff_source_location"):
        return []
    rows=connection.execute("""SELECT h.handoff_uuid,l.source_site_uuid,s.material_uuid AS owner_uuid
        FROM station_handoff_source_location l JOIN station_handoff_item i USING(handoff_item_uuid)
        JOIN station_handoff h ON h.handoff_uuid=i.handoff_uuid
        JOIN site s ON s.uuid=l.source_site_uuid
        WHERE l.pickup_json IS NULL AND i.state='protected' AND h.role='source' AND h.handoff_uuid<>?
        AND EXISTS(SELECT 1 FROM json_each(h.package_json,'$.items') j
            WHERE json_extract(j.value,'$.handoff_item_uuid')=i.handoff_item_uuid
            AND json_extract(j.value,'$.placement_plan') IS NOT NULL)""",(ignore_handoff_uuid,)).fetchall()
    return [{"handoff_uuid":row["handoff_uuid"],"lock_key":key}
        for row in rows for key in (site_lock_key(row["owner_uuid"],row["source_site_uuid"]),device_lock_key(row["owner_uuid"]))]
