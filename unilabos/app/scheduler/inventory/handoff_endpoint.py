"""在源冻结或目标准备的同一库存事务内记录可验证的真实位置。"""

from datetime import datetime

from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.transport_endpoint import validate_endpoint


def endpoint_snapshot(c, payload, *, role):
    """读取已经保护的容器位置；没有明确外部位置时拒绝自动搬运。"""
    rows = c.execute(
        "SELECT i.*,m.aggregate_version FROM station_handoff_item i JOIN material m "
        "ON m.uuid=i.carrier_material_uuid WHERE i.handoff_uuid=? ORDER BY i.container_uuid",
        (payload["handoff_uuid"],),
    ).fetchall()
    items = []
    for row in rows:
        if role == "source":
            location = c.execute(
                "SELECT source_site_uuid FROM station_handoff_source_location WHERE handoff_item_uuid=?",
                (row["handoff_item_uuid"],),
            ).fetchone()
        else:
            location = c.execute(
                "SELECT site_uuid FROM station_ingress_reservation WHERE uuid=?", (row["reservation_uuid"],)
            ).fetchone()
        if location is None or not location[0]:
            raise ValueError("自动搬运缺少容器实际取送货位置")
        site = c.execute("SELECT * FROM site WHERE uuid=? AND deleted_at IS NULL", (location[0],)).fetchone()
        if site is None:
            raise ValueError("实际交接位置已经不存在")
        owner = c.execute("SELECT name FROM material WHERE uuid=?", (site["material_uuid"],)).fetchone()
        site_label = (str(owner[0]) + " / " if owner else "") + site["name"]
        reservation = None
        if role == "target":
            protected = c.execute(
                "SELECT uuid,revision,expires_at FROM station_ingress_reservation WHERE uuid=?",
                (row["reservation_uuid"],),
            ).fetchone()
            reservation = {
                "reservation_uuid": protected["uuid"],
                "revision": protected["revision"],
                "valid_until_ms": int(datetime.fromisoformat(protected["expires_at"]).timestamp() * 1000),
            }
        items.append(
            {
                "container_uuid": row["container_uuid"],
                "material_uuid": row["carrier_material_uuid"],
                "site_uuid": location[0],
                "material_revision": row["aggregate_version"],
                "site_digest": digest(
                    {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(site).items()}
                ),
                "site_label": site_label,
                **({"reservation": reservation} if reservation is not None else {}),
            }
        )
    station = payload["source_station_uuid" if role == "source" else "target_station_uuid"]
    return validate_endpoint(
        {"schema_version": 1, "station_uuid": station, "handoff_revision": payload["revision"], "items": items}
    )
