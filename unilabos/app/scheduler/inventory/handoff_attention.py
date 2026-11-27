"""交接当前责任只读投影，库存、库位、证明和结算分别展示。"""
from __future__ import annotations

import json
from typing import Any

from unilabos.center_protocol.digest import digest


def handoff_details(authority: Any, handoff_uuid: str | None = None) -> list[dict[str, Any]]:
    result = []
    with authority.store.read_connection() as c:
        rows = c.execute('SELECT * FROM station_handoff ORDER BY handoff_uuid').fetchall()
        for row in rows:
            if handoff_uuid is not None and row['handoff_uuid'] != handoff_uuid:
                continue
            payload = json.loads(row['package_json'])
            items = []
            for item in c.execute('SELECT * FROM station_handoff_item WHERE handoff_uuid=? ORDER BY handoff_item_uuid', (row['handoff_uuid'],)).fetchall():
                reservation = (c.execute('SELECT * FROM station_ingress_reservation WHERE uuid=?', (item['reservation_uuid'],)).fetchone()
                               if item['reservation_uuid'] else None)
                settlement = c.execute('SELECT settlement_json FROM station_handoff_settlement WHERE handoff_item_uuid=?', (item['handoff_item_uuid'],)).fetchone()
                resources = c.execute('SELECT lock_key,active FROM station_handoff_resource WHERE handoff_item_uuid=? ORDER BY lock_key', (item['handoff_item_uuid'],)).fetchall()
                occupied_sites = c.execute(
                    'SELECT DISTINCT s.uuid FROM site s JOIN station_handoff_resource r ON r.material_uuid=s.occupied_material_uuid WHERE r.handoff_item_uuid=? ORDER BY s.uuid',
                    (item['handoff_item_uuid'],),
                ).fetchall()
                items.append({**dict(item), 'receipt': json.loads(item['receipt_json']) if item['receipt_json'] else None,
                    'reservation': dict(reservation) if reservation else None,
                    'settlement': json.loads(settlement['settlement_json']) if settlement else None,
                    'occupied_site_uuids': [s['uuid'] for s in occupied_sites],
                    'resources': [dict(r) for r in resources], 'protection_active': any(r['active'] == 1 for r in resources) or bool(reservation and reservation['site_active'] == 1 and reservation['state'] in {'reserved', 'in_transit'})})
            proofs = [dict(r) for r in c.execute('SELECT event_uuid,purpose,body_json,application,response_json FROM station_handoff_outbox WHERE handoff_uuid=? ORDER BY event_uuid', (row['handoff_uuid'],)).fetchall()]
            for proof in proofs:
                proof['body'] = json.loads(proof.pop('body_json'))
                response_json = proof.pop('response_json')
                proof['response'] = json.loads(response_json) if response_json else None
            issues = [dict(r) for r in c.execute('SELECT p.* FROM station_handoff_recovery_pending p JOIN station_handoff_outbox o ON o.event_uuid=p.event_uuid WHERE o.handoff_uuid=?', (row['handoff_uuid'],)).fetchall()]
            # 已保存的快照修订不等于本站库存已完成结算。
            result.append({'handoff_uuid': row['handoff_uuid'], 'revision': row['revision'], 'role': row['role'],
                'state': row['state'], 'created_at_ms': row['created_at_ms'], 'handoff': payload,
                'items': items, 'proofs': proofs, 'recovery_issues': issues})
    return result


def _completed_tombstone(detail: dict[str, Any], item: dict[str, Any]) -> bool:
    """识别仍须阻止旧身份派发、但已无待处理库存责任的终态墓碑。"""
    if item['occupied_site_uuids']:
        return False
    payload = detail['handoff']
    center_item = next((i for i in payload['items'] if i['handoff_item_uuid'] == item['handoff_item_uuid']), None)
    if center_item is None or center_item.get('movement_review'):
        return False
    settlement = center_item.get('settlement')
    if detail['role'] == 'target' and item['state'] == 'canceled':
        reservation = item['reservation']
        if (item['receipt'] is not None or center_item.get('receipt') is not None
                or not reservation or reservation['state'] != 'canceled' or reservation['site_active'] != 0):
            return False
        if settlement is not None:
            return settlement['decision'] == 'source_present' and item['settlement'] == settlement
        return payload['status'] == 'canceled' and payload['permit'] is None and item['settlement'] is None
    if detail['role'] == 'source' and item['state'] == 'settled':
        return bool(settlement and settlement['decision'] == 'target_received'
                    and item['settlement'] == settlement and center_item.get('receipt')
                    and item['reservation'] is None)
    return False


def attention_handoffs(authority: Any) -> list[dict[str, Any]]:
    from datetime import datetime, timezone
    result = []
    for detail in handoff_details(authority):
        payload = detail['handoff']
        pending = any(p['application'] in (None, 'conflict') for p in detail['proofs'])
        applications = [p['body']['payload'] for p in detail['proofs']
            if p['body'].get('payload', {}).get('kind') == 'snapshot_applied']
        latest_revision = max([detail['revision']] + [p['observed_revision'] for p in applications])
        latest_applications = [p for p in applications if p['observed_revision'] == latest_revision]
        local_failures = ([] if any(p.get('applied') is True for p in latest_applications)
                          else [p for p in latest_applications if p.get('applied') is False])
        current_applied = latest_revision == detail['revision'] and any(
            p.get('applied') is True and p.get('package_digest') == digest(payload)
            for p in latest_applications)
        # 墓碑的 active 仍是派发门禁；只有精确快照已应用且逐件责任结清时才免于异常计数。
        unsettled = payload['status'] in {'canceled', 'reconciled'} and any(
            i['protection_active']
            and not (current_applied and _completed_tombstone(detail, i)) for i in detail['items'])
        if not (payload['attention'] or payload['status'] == 'canceling' or detail['recovery_issues'] or unsettled or local_failures
                or (pending and payload['status'] in {'canceled', 'reconciled'})):
            continue
        result.append({'decision_kind': 'handoff_reconciliation', 'decision_id': detail['handoff_uuid'],
            'handoff_uuid': detail['handoff_uuid'], 'revision': detail['revision'], 'status': 'required', 'options': [],
            'opened_at': datetime.fromtimestamp(detail['created_at_ms']/1000, timezone.utc).isoformat(),
            'meta_data': {'progress': detail, 'last_error': '; '.join(payload['attention_reasons'] + [p.get('failure_reason') or '本站快照应用待核实' for p in local_failures])}})
    return result
