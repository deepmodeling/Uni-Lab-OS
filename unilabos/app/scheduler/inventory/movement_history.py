"""从持久交接凭据读取出入站历史；不修改当前库存，也不新建第二份台账。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from unilabos.app.scheduler.inventory.store import InventoryStore

# 每笔交接明细在本站只生成一条记录。重放确认、物料重新入站、改名或软删除
# 都不改变记录身份；只有真实收货/取货凭据或已完成取消才进入历史。
_RECORDS = """
WITH facts AS (
 SELECT h.handoff_uuid, i.handoff_item_uuid, i.carrier_material_uuid AS material_uuid,
        m.name AS material_name, m.barcode, t.display_name AS resource_template_name,
        CASE h.role WHEN 'target' THEN 'inbound' ELSE 'outbound' END AS direction,
        json_extract(h.package_json,'$.source_station_uuid') AS source_station_uuid,
        json_extract(h.package_json,'$.target_station_uuid') AS target_station_uuid,
        CASE WHEN h.role='target' THEN i.receipt_json
             ELSE json_extract(ci.value,'$.receipt') END AS receipt,
        CASE WHEN h.role='source' THEN l.pickup_json ELSE NULL END AS pickup,
        i.state, r.canceled_at,
        json_extract(o.body_json,'$.occurred_at_ms') AS canceled_at_ms,
        CASE WHEN h.role='target' THEN r.site_uuid ELSE l.source_site_uuid END AS site_uuid
 FROM station_handoff_item i
 JOIN station_handoff h ON h.handoff_uuid=i.handoff_uuid
 JOIN material m ON m.uuid=i.carrier_material_uuid
 LEFT JOIN resource_template t ON t.uuid=m.resource_template_uuid
 LEFT JOIN json_each(h.package_json,'$.items') ci
   ON json_extract(ci.value,'$.handoff_item_uuid')=i.handoff_item_uuid
 LEFT JOIN station_handoff_source_location l ON l.handoff_item_uuid=i.handoff_item_uuid
 LEFT JOIN station_ingress_reservation r ON r.uuid=i.reservation_uuid
 LEFT JOIN station_handoff_outbox o ON o.handoff_uuid=h.handoff_uuid AND o.purpose='cancel_ack'
), records AS (
 SELECT facts.*,
        CASE WHEN receipt IS NOT NULL OR pickup IS NOT NULL THEN 'succeeded'
             ELSE 'canceled' END AS result,
        CASE WHEN direction='inbound' AND receipt IS NOT NULL THEN 'receipt'
             WHEN pickup IS NOT NULL THEN 'pickup'
             WHEN receipt IS NOT NULL THEN 'receipt_confirmation'
             ELSE 'cancellation' END AS time_basis,
        CASE WHEN pickup IS NOT NULL
               THEN NULL
             WHEN receipt IS NOT NULL THEN json_extract(receipt,'$.target_snapshot.received_at')
             ELSE canceled_at
        END AS occurred_at,
        CASE WHEN pickup IS NOT NULL THEN CAST(json_extract(pickup,'$.picked_up_at_ms') AS REAL)
             WHEN receipt IS NOT NULL THEN NULL
             WHEN canceled_at IS NULL THEN CAST(canceled_at_ms AS REAL) ELSE NULL END AS occurred_at_ms,
        (SELECT name FROM site WHERE uuid=facts.site_uuid) AS site_name
 FROM facts WHERE receipt IS NOT NULL OR pickup IS NOT NULL OR state='canceled'
)
"""


def list_material_movements(
    store: InventoryStore, *, page: int = 1, page_size: int = 20,
    direction: str = 'all', keyword: str = '', material_uuid: str | None = None,
) -> dict[str, Any]:
    """分页查询本站出入站事实，物料 UUID 筛选包含该物料的所有交接明细。"""
    if direction not in {'all', 'inbound', 'outbound'} or page < 1 or not 1 <= page_size <= 100:
        raise ValueError('出入站记录分页或方向参数无效')
    predicates = ['1=1']
    params: list[Any] = []
    if direction != 'all':
        predicates.append('direction=?')
        params.append(direction)
    if material_uuid:
        predicates.append('material_uuid=?')
        params.append(material_uuid)
    if keyword.strip():
        term = keyword.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        predicates.append("(material_name LIKE ? ESCAPE '\\' OR barcode LIKE ? ESCAPE '\\')")
        params.extend([f'%{term}%', f'%{term}%'])
    where = ' WHERE ' + ' AND '.join(predicates)
    with store.read_connection() as c:
        total = int(c.execute(_RECORDS + 'SELECT COUNT(*) FROM records' + where, tuple(params)).fetchone()[0])
        rows = c.execute(
            _RECORDS + 'SELECT * FROM records' + where +
            ' ORDER BY COALESCE(occurred_at_ms,(julianday(occurred_at)-2440587.5)*86400000) DESC,handoff_item_uuid DESC LIMIT ? OFFSET ?',
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    items = []
    for row in rows:
        record = {key: row[key] for key in (
            'handoff_uuid', 'handoff_item_uuid', 'material_uuid', 'material_name', 'barcode',
            'resource_template_name', 'direction', 'result', 'time_basis', 'occurred_at',
            'source_station_uuid', 'target_station_uuid', 'site_uuid', 'site_name',
        )}
        if row['occurred_at_ms'] is not None:
            record['occurred_at'] = datetime.fromtimestamp(float(row['occurred_at_ms']) / 1000, timezone.utc).isoformat()
        record['record_id'] = f"{row['handoff_item_uuid']}:{row['direction']}"
        # 接收时间保留凭据原值；取货时间由毫秒时间戳生成 UTC，绝不用物料更新时间代替。
        items.append(record)
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size,
            'has_more': page * page_size < total}
