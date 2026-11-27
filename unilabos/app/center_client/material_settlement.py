"""取消历史结果与库存释放分阶段结算，任何重试均不重跑 Task/Job。"""
from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from uuid import UUID, uuid5

from unilabos.center_protocol.digest import digest
from unilabos.workflow.center_boundary import _bind, cancellation_requested


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def settlement_state(workflow, invocation: str):
    with workflow.transaction() as c:
        row = c.execute('SELECT cancel_settlement_json FROM center_outcome_evidence WHERE invocation_uuid=?', (invocation,)).fetchone()
        return json.loads(row[0]) if row is not None and row[0] else None


def _save(workflow, invocation, state):
    with workflow.transaction() as c:
        row = c.execute('SELECT cancel_settlement_json FROM center_outcome_evidence WHERE invocation_uuid=?', (invocation,)).fetchone()
        if row is None:
            raise ValueError('取消结算缺少原结果责任')
        if row[0]:
            previous = json.loads(row[0])
            if previous['request'] != state['request']:
                raise ValueError('原取消结算的结果、原因和依据不能改写')
            if previous.get('release_receipt') and previous != state:
                raise ValueError('已完成取消结算不可回退')
        c.execute('UPDATE center_outcome_evidence SET cancel_settlement_json=? WHERE invocation_uuid=?', (_json(state), invocation))


def _prepare(runtime, invocation, *, reason, evidence_refs):
    workflow = runtime.materials.workflow
    # 先读取库存诊断，再取得工作流锁，保持跨库取锁顺序。
    failures = runtime.materials.authority.export_failures()
    with workflow.transaction() as c:
        inbox = c.execute('SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?', (invocation,)).fetchone()
        if inbox is None or inbox['local_task_uuid'] is None:
            raise ValueError('取消结算必须关联原已执行任务')
        task, package = _bind(c, invocation_uuid=invocation, task_uuid=inbox['local_task_uuid'])
        if workflow.execution_held(task['uuid']) or workflow.invocation_held(invocation):
            raise ValueError('原任务或调用处于恢复保护，不能取消结算')
        if not cancellation_requested(c, task['uuid']):
            raise ValueError('中心尚未请求取消本任务')
        if task['status'] not in {'succeeded', 'failed', 'canceled', 'timeout'}:
            raise ValueError('执行尚未结束，不能结算取消结果')
        if not (task['cleanup_status'] == 'settled' or (task['status'] == 'succeeded' and task['cleanup_status'] == 'none')):
            raise ValueError('原任务物理清理尚未安全完成')
        if task['status'] == 'succeeded':
            result = c.execute('SELECT result FROM workflow_result WHERE workflow_task_uuid=?', (task['uuid'],)).fetchone()
            if result is None:
                raise ValueError('取消结算不能丢失原成功结果')
            output = result[0]
        else:
            output = _json({'status': task['status'], 'error_info': json.loads(task['error_info'])})
        row = c.execute('SELECT * FROM center_outcome_evidence WHERE invocation_uuid=?', (invocation,)).fetchone()
        if row is None:
            raise ValueError('原任务没有待结算物料结果，沿用普通取消流程')
        if row['recorded_cancellation_json']:
            raise ValueError('原调用已进入历史取消核实，请继续原核实，不能改用普通取消结算')
        if row['cancel_settlement_json']:
            state = json.loads(row['cancel_settlement_json'])
            if state['request']['reason'] != reason or state['request']['evidence_refs'] != evidence_refs:
                raise ValueError('恢复必须复用原取消结算的原因和依据')
            return state
        from unilabos.app.scheduler.inventory.handoff import _evidence
        _evidence(evidence_refs)
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 1000:
            raise ValueError('取消结算原因须为非空且不超过1000字节')
        status = row['pending_reason'] or 'material_export_pending'
        reasons = [v.get('last_error') for v in failures if v['invocation_uuid'] == invocation and v.get('last_error')]
        request = {'settlement_uuid': str(uuid5(UUID(invocation), 'material-cancel-settlement')),
            'task_payload_digest': package['payload_digest'], 'local_task_uuid': task['uuid'],
            'execution_status': 'failed' if task['status'] == 'timeout' else task['status'],
            'original_output_json': output, 'failure_reason': (reasons[0] if reasons else status)[:500],
            'reason': reason, 'evidence_refs': list(evidence_refs), 'cleanup_settled': True, 'resources_released': False}
        state = {'request': request, 'receipt': None, 'resources_released': False, 'release_receipt': None}
        c.execute('UPDATE center_outcome_evidence SET cancel_settlement_json=? WHERE invocation_uuid=?', (_json(state), invocation))
        return state


def validate_receipt(receipt, invocation, owner, request):
    expected = {'station_invocation_uuid': invocation, 'owner_instance_uuid': owner,
                'settlement_uuid': request['settlement_uuid'], 'request_digest': digest(request),
                'resources_released': request['resources_released']}
    if receipt != expected:
        raise ValueError('中心取消结算回执与原请求不符')
    return deepcopy(receipt)


def _release_inventory(runtime, invocation, request, package, input_hold, *,
                       extra_materials: Iterable[str] = (),
                       settlement_kind: str = 'material_cancel_settlement'):
    """原库存责任与释放证明同事务提交；历史核实使用独立审计类型。"""
    from unilabos.app.scheduler.inventory.material_inputs import StationMaterialInputAuthority
    from unilabos.app.scheduler.inventory.outbound import settle_canceled_outbound
    from unilabos.app.scheduler.inventory.service import InventoryService
    from unilabos.app.scheduler.inventory.store import InventoryStore
    authority = runtime.materials.authority
    if settlement_kind not in {'material_cancel_settlement', 'recorded_cancellation_reconciliation'}:
        raise ValueError('库存结算类型无效')
    service = InventoryService(authority.store)
    task_uuid = request['local_task_uuid']
    expected = {'task_uuid': task_uuid, 'request_digest': digest(request),
                'input_receipt': input_hold['receipt'] if input_hold else None,
                'evidence_refs': request['evidence_refs']}
    if settlement_kind != 'material_cancel_settlement':
        expected['kind'] = settlement_kind
    with service.command_transaction() as c:
        previous = c.execute('SELECT * FROM inventory_ledger WHERE entry_uuid=?', (request['settlement_uuid'],)).fetchone()
        if previous is not None:
            proof = json.loads(previous['delta_json'])
            if (previous['aggregate_type'] != settlement_kind
                    or previous['aggregate_id'] != invocation or previous['op_type'] != 'resources_released'
                    or any(proof.get(key) != value for key, value in expected.items())):
                raise ValueError('原库存取消结算证明与本次请求冲突，必须核实')
            # 原释放已经提交，只重放原证明，不检查或释放后来任务的占用。
            return proof
        input_authority = StationMaterialInputAuthority(runtime.handoffs.authority)
        if input_hold:
            input_authority._verify(c, package, input_hold['receipt'], allow_released=True)
            if input_hold['receipt']['local_task_uuid'] != task_uuid:
                raise ValueError('原输入占用不属于取消的真实任务')
        checked_materials = _assert_safe_release(c, runtime.handoffs.authority, invocation, task_uuid,
                                               extra_materials=extra_materials)
        # 复用 command_transaction，使原来源、输入、出站释放及审计证明共同提交或回滚。
        source_release = service.release_workflow(task_uuid, reason='center_' + settlement_kind,
                                                  causation_id=request['settlement_uuid'])
        if input_hold:
            c.execute("UPDATE station_material_input SET state='released' WHERE invocation_uuid=?", (invocation,))
        facts = settle_canceled_outbound(c, invocation_uuid=invocation, task_uuid=task_uuid,
                                        evidence_refs=request['evidence_refs'])
        proof = {**expected, 'facts': facts, 'source_release': source_release, 'checked_materials': checked_materials}
        InventoryStore.tx_append_inventory_event(c, entry_uuid=request['settlement_uuid'],
            edge_id=authority.station_uuid, lab_id=runtime.handoffs.authority.lab_uuid, occurred_at=authority._now_ms(),
            aggregate_type=settlement_kind, aggregate_id=invocation, aggregate_version=1,
            event_type='resources_released', payload=proof, workflow_task_uuid=task_uuid,
            reason=request['reason'], causation_id=request['settlement_uuid'])
        return proof


def _assert_safe_release(c, handoffs, invocation, task_uuid, *, extra_materials: Iterable[str] = ()):
    from unilabos.app.scheduler.inventory.material_inputs import material_input_blocker
    from unilabos.app.scheduler.resource_lock import conflicting_resource_lock_keys, device_lock_key, material_lock_key
    bindings = c.execute(
        "SELECT b.material_uuid,r.amounts_json FROM inventory_material_source_binding b "
        "LEFT JOIN inventory_reservation r ON r.workflow_id=b.workflow_id AND r.node_id=b.node_id AND r.attempt=b.attempt "
        "WHERE b.workflow_id=? AND b.status='active' AND b.custody_policy='task_exclusive'", (task_uuid,)).fetchall()
    materials = {row['material_uuid'] for row in bindings}
    location_ancestors: set[str] = set()
    # 旧任务可能没有来源/出站占用；核实其实际输出不能依赖非空的占用集合。
    for local in extra_materials:
        materials.add(local)
        cursor, seen, carrier_found = local, set(), False
        while cursor:
            if cursor in seen:
                raise ValueError('实际输出的容器树包含循环，必须核实')
            seen.add(cursor)
            if cursor != local:
                location_ancestors.add(cursor)
            if not carrier_found and c.execute('SELECT 1 FROM container_station_binding WHERE carrier_material_uuid=?', (cursor,)).fetchone():
                # 输出可能是容器中的样品；必须同时检查载体及其余内容的占用。
                materials.add(cursor)
                carrier_found = True
            parent = handoffs._material(c, cursor)['parent_uuid']
            parents = {parent} if parent else set()
            parents.update(row['material_uuid'] for row in c.execute(
                'SELECT material_uuid FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL',
                (cursor,),
            ).fetchall())
            if len(parents) > 1:
                raise ValueError('输出物料的父层级与实际库位不一致，必须核实')
            cursor = next(iter(parents), None)
    for binding in bindings:
        if binding['amounts_json'] is None:
            continue
        original = json.loads(binding['amounts_json']).get('custody_material_uuids', [])
        if not isinstance(original, list) or any(not isinstance(local, str) or not local for local in original):
            raise ValueError('原任务保管范围损坏，必须核实')
        # 原来源绑定仍活动时，原内容即使被本任务移出或实例预留已消费，也必须核实。
        materials.update(original)
    reservations = c.execute("SELECT * FROM inventory_reservation WHERE workflow_id IN (?,?) AND status='active'", (task_uuid, invocation)).fetchall()
    for row in reservations:
        materials.update(json.loads(row['amounts_json']).get('instances', []))
    materials.update(row['material_uuid'] for row in c.execute(
        'SELECT material_uuid FROM station_material_input_resource WHERE invocation_uuid=?', (invocation,)).fetchall())
    for material in list(materials):
        materials.update(handoffs._subtree(c, material))
    # 库架/台面等祖先只检查其自身的状态及占用，不扩大到无关兄弟物料，也不释放祖先。
    materials.update(location_ancestors)
    checked_materials = []
    for material in sorted(materials):
        row = handoffs._material(c, material)
        if row['inventory_status'] not in {'warehouse', 'reserved'} or row['disposition'] != 'active':
            raise ValueError('物料仍持于设备、离站或位置待核实，不能释放原任务占用')
        checked_materials.append({key: row[key] for key in ('uuid', 'aggregate_version', 'inventory_status', 'disposition', 'parent_uuid')})
    keys = {key for material in materials for key in (material_lock_key(material), device_lock_key(material))}
    if material_input_blocker(c, tuple(keys), task_uuid=task_uuid) is not None:
        raise ValueError('物料仍有其他调用的输入占用')
    for table, condition in [('station_execution_lock_lease', "state IN ('prepared','reserved','running','uncertain')"),
                             ('station_handoff_resource', 'active=1'), ('station_ingress_reservation_resource', 'active=1')]:
        for row in c.execute(f'SELECT lock_key FROM {table} WHERE {condition}').fetchall():
            if conflicting_resource_lock_keys(keys, {row['lock_key']}):
                raise ValueError('物料仍有执行动作、交接或入口保护，不能释放')
    for row in c.execute("SELECT material_uuid FROM inventory_material_source_binding WHERE workflow_id<>? AND status='active'", (task_uuid,)).fetchall():
        if row['material_uuid'] in materials:
            raise ValueError('物料已被其他真实任务占用，不能释放')
    for row in c.execute("SELECT amounts_json FROM inventory_reservation WHERE workflow_id NOT IN (?,?) AND status='active'", (task_uuid, invocation)).fetchall():
        if materials & set(json.loads(row['amounts_json']).get('instances', [])):
            raise ValueError('物料仍有其他库存预留，不能释放原任务占用')
    return checked_materials


def settle_material_cancellation(runtime, invocation: str, *, reason: str, evidence_refs: list[str]):
    """工站受权操作；只处理原已取消调用，并分别保存中心确认、本站释放及最终回执。"""
    runtime.store.require_execution()
    if runtime.store.invocation_held(invocation):
        raise ValueError('原调用处于恢复保护，不能取消结算')
    state = _prepare(runtime, invocation, reason=reason, evidence_refs=evidence_refs)
    if state.get('release_receipt'):
        return state
    workflow, authority = runtime.materials.workflow, runtime.materials.authority
    if state['receipt'] is None:
        receipt = runtime.client.settle_canceled_material(invocation, state['request'], authority.event_epoch_uuid)
        state['receipt'] = validate_receipt(receipt, invocation, authority.instance_uuid, state['request'])
        _save(workflow, invocation, state)
    if not state['resources_released']:
        input_hold = runtime.store.material_input(invocation)
        package = runtime.store.package(invocation)
        body = {'payload': dict(package.raw), 'payload_digest': package.payload_digest}
        proof = _release_inventory(runtime, invocation, state['request'], body, input_hold)
        # 库存证明先提交；之后可以安全地重复写入同一份工作流回执。
        if input_hold and input_hold['state'] != 'released':
            runtime.store.finish_material_input_release(input_hold['receipt'])
        state['resources_released'] = True
        state['inventory_release_proof'] = proof
        _save(workflow, invocation, state)
    request = {**state['request'], 'resources_released': True}
    receipt = runtime.client.settle_canceled_material(invocation, request, authority.event_epoch_uuid)
    state['release_receipt'] = validate_receipt(receipt, invocation, authority.instance_uuid, request)
    _save(workflow, invocation, state)
    return state
