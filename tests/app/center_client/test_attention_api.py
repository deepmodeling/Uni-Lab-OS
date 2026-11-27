"""事项列表经 HTTP 保留全部身份，完整物料/交接证明留在详情接口。"""
import threading
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.center_client.test_websocket import setup_receiver
from unilabos.app.center_client.runtime import CenterRuntime


def http_client(runtime):
    from unilabos.app.center_client.attention_api import create_router
    app = FastAPI()
    app.include_router(create_router(lambda: runtime))
    return TestClient(app)


def snapshot_runtime(tmp_path, snapshot):
    _server, client, durable, _receiver = setup_receiver(tmp_path)
    runtime = CenterRuntime.__new__(CenterRuntime)
    runtime.config, runtime.client, runtime.store = client._config, client, durable
    runtime._poll_lock = threading.RLock()
    runtime.service = SimpleNamespace(center_attention_snapshot=lambda: snapshot)
    runtime.materials = runtime.handoffs = None
    return runtime


@pytest.mark.parametrize('count', [13, 1000])
def test_http_summary_keeps_every_item_and_native_fields_without_mutating_facts(tmp_path, count):
    native = {'decision_kind': 'edge_node_error', 'revision': 7,
        'meta_data': {'error_message': '原生字段不能裁剪'}, 'options': [{'id': 'cancel_task'}]}
    manual = {'actions': ['approve', 'reject'], 'workflow_task_uuid': str(uuid4())}
    interaction = {'kind': 'form', 'arguments': {'schema': {'description': '完整表单'}}, 'uuid': str(uuid4())}
    items = []
    for index in range(count):
        identity = str(uuid4())
        items.append({'decision_kind': 'handoff_reconciliation', 'decision_id': identity,
            'handoff_uuid': identity, 'revision': index + 1, 'status': 'required', 'options': [],
            'opened_at': '2026-10-30T00:00:00Z',
            'meta_data': {'last_error': '中文错误' * 200, 'debug': '全部诊断',
                'progress': {'handoff_uuid': identity, 'revision': index + 1, 'role': 'target',
                    'state': 'protected', 'items': [{'proof': '中文证明' * 10000}]}}})
    export = {'decision_kind': 'material_export', 'decision_id': 'export:prepare',
        'invocation_uuid': str(uuid4()), 'revision': 41, 'phase': 'prepare', 'status': 'retrying',
        'options': [{'id': 'resume_export'}], 'meta_data': {'last_error': '中' * 200,
            'execution_status': 'succeeded', 'materials': [{'body': '大量材料'}], 'debug': '诊断'}}
    snapshot = {'manual_confirmation_count': 2, 'exception_count': count + 2,
        'items': {'manual_confirmations': [manual], 'human_interactions': [interaction],
            'exceptions': [native, export, *items]}}
    original = deepcopy(snapshot)
    runtime = snapshot_runtime(tmp_path, snapshot)
    try:
        data = http_client(runtime).get('/api/v1/center-attentions').json()['data']
        session = runtime.client.session
        assert {key: data[key] for key in ('station_uuid', 'instance_uuid', 'process_uuid', 'session_uuid', 'session_epoch')} == {
            'station_uuid': runtime.config.station_uuid, 'instance_uuid': runtime.config.instance_uuid,
            'process_uuid': session.process_uuid, 'session_uuid': session.session_uuid, 'session_epoch': session.session_epoch}
        projected = data['snapshot']
        assert projected['manual_confirmation_count'] == 2
        assert projected['exception_count'] == count + 2
        assert len(projected['items']['exceptions']) == count + 2
        assert projected['items']['manual_confirmations'] == [manual]
        assert projected['items']['human_interactions'] == [interaction]
        assert projected['items']['exceptions'][0] == native
        assert projected['items']['exceptions'][1] == {**export, 'meta_data': {
            'last_error': '中' * 166, 'execution_status': 'succeeded'}}
        for actual, source in zip(projected['items']['exceptions'][2:], items):
            assert {k: v for k, v in actual.items() if k != 'meta_data'} == {k: v for k, v in source.items() if k != 'meta_data'}
            assert actual['meta_data'] == {'progress': {key: source['meta_data']['progress'][key]
                for key in ('handoff_uuid', 'revision', 'role', 'state')}, 'last_error': ('中文错误' * 200).encode()[:500].decode(errors='ignore')}
        assert snapshot == original
    finally:
        runtime.store.close()


def test_http_empty_snapshot_and_missing_session(tmp_path):
    snapshot = {'manual_confirmation_count': 0, 'exception_count': 0,
        'items': {'manual_confirmations': [], 'exceptions': []}}
    runtime = snapshot_runtime(tmp_path, snapshot)
    try:
        client = http_client(runtime)
        assert client.get('/api/v1/center-attentions').json()['data']['snapshot'] == snapshot
        runtime.client.fence_session(runtime.client.session)
        assert client.get('/api/v1/center-attentions').status_code == 503
        assert http_client(None).get('/api/v1/center-attentions').status_code == 503
    finally:
        runtime.store.close()


def test_http_handoff_summary_keeps_original_large_proofs_available_in_details(tmp_path):
    import json

    from tests.app.test_station_handoff import frozen, handoff_inventory_fixture, package
    from unilabos.app.center_client.inventory_api import create_router
    fixture = handoff_inventory_fixture(tmp_path)
    case = next(fixture)
    runtime = snapshot_runtime(tmp_path, {'manual_confirmation_count': 0, 'exception_count': 0,
        'items': {'manual_confirmations': [], 'exceptions': []}})
    try:
        frozen(case)
        authority = case['auth']['target']
        authority.apply_center_snapshot(package(dict(case['payload'], revision=case['payload']['revision'] + 1, status='canceling')))
        original = '完整中文证明' * 30000
        with authority.store.transaction() as connection:
            row = connection.execute('SELECT event_uuid,body_json FROM station_handoff_outbox LIMIT 1').fetchone()
            body = json.loads(row['body_json'])
            body['debug'] = original
            connection.execute('UPDATE station_handoff_outbox SET body_json=? WHERE event_uuid=?',
                (json.dumps(body, ensure_ascii=False), row['event_uuid']))
        runtime.handoffs = SimpleNamespace(authority=authority)
        client = http_client(runtime)
        client.app.include_router(create_router(lambda: runtime))
        summary = client.get('/api/v1/center-attentions').json()['data']['snapshot']
        assert summary['exception_count'] == 1
        assert set(summary['items']['exceptions'][0]['meta_data']['progress']) == {'handoff_uuid', 'revision', 'role', 'state'}
        details = client.get('/api/v1/center-handoffs/' + case['ids']['handoff']).json()['data']
        assert next(proof for proof in details['proofs'] if proof['event_uuid'] == row['event_uuid'])['body']['debug'] == original
        assert len(json.dumps(details, ensure_ascii=False).encode()) > 100 * 1024
        assert '完整中文证明' not in json.dumps(summary, ensure_ascii=False)
    finally:
        runtime.store.close()
        fixture.close()


def test_idle_runtime_polls_do_not_send_attention_changes_but_bridge_changes_do(tmp_path):
    import asyncio
    from contextlib import suppress

    from tests.app.center_client.test_runtime import Center, Service
    from tests.app.center_client.test_runtime import runtime as make_runtime
    from tests.app.center_client.test_websocket import LiveConnection, hello
    from unilabos.app.center_client.websocket import NotificationReceiver
    value = make_runtime(tmp_path, Center(), Service())
    value.client.open_session(value.store.event_epoch_uuid)
    state = []
    on_pump = []
    def pump():
        if on_pump:
            state.append(on_pump.pop())
    # 库存桥的状态端口模拟独立持久责任；完整列表读取是明确禁止的边界。
    value.materials = SimpleNamespace(pump=pump, attention_state=lambda: tuple(state))
    def forbidden():
        pytest.fail('无变化轮询不能读取完整事项')
    value.center_attention_snapshot = forbidden
    receiver = NotificationReceiver(value.config, value.client, value.store, lambda: None, attention_provider=forbidden)
    value._notifications = receiver
    connection = LiveConnection(hello(value.client.session))
    async def wait_count(expected):
        for _ in range(200):
            if len(connection.sent) >= expected:
                return
            await asyncio.sleep(0)
        pytest.fail('未收到事项变更提示')
    async def drain():
        # 心跳让读取循环确实消费前面已经提交的唤醒，不能靠未调度造成假阴性。
        from unilabos.app.center_client.websocket import envelope
        for _ in range(30):
            await asyncio.sleep(0)
        previous = sum(m['type'] == 'pong' for m in connection.sent)
        connection.messages.put_nowait(envelope(value.client.session, 'ping', {}))
        for _ in range(200):
            if sum(m['type'] == 'pong' for m in connection.sent) > previous:
                return
            await asyncio.sleep(0)
        pytest.fail('通知连接没有继续处理心跳')
    async def scenario():
        exchange = asyncio.create_task(receiver._exchange(connection, value.client.session))
        try:
            await wait_count(2)
            value.poll_once()
            await drain()
            assert sum(m['type'] == 'attention.changed' for m in connection.sent) == 1
            state.append(('invocation', 'blocked', 1))
            value.poll_once()
            for _ in range(200):
                if sum(m['type'] == 'attention.changed' for m in connection.sent) == 2:
                    break
                await asyncio.sleep(0)
            assert sum(m['type'] == 'attention.changed' for m in connection.sent) == 2
            value.poll_once()
            await drain()
            assert sum(m['type'] == 'attention.changed' for m in connection.sent) == 2
            state.clear()
            value.poll_once()
            for _ in range(200):
                if sum(m['type'] == 'attention.changed' for m in connection.sent) == 3:
                    break
                await asyncio.sleep(0)
            assert sum(m['type'] == 'attention.changed' for m in connection.sent) == 3
            value._bridge_attention_state = None
            on_pump.append(('new-first-poll-failure', 'blocked', 1))
            value.poll_once()
            for _ in range(200):
                if sum(m['type'] == 'attention.changed' for m in connection.sent) == 4:
                    break
                await asyncio.sleep(0)
            assert sum(m['type'] == 'attention.changed' for m in connection.sent) == 4
        finally:
            connection.close()
            exchange.cancel()
            with suppress(asyncio.CancelledError, EOFError):
                await exchange
    try:
        asyncio.run(scenario())
    finally:
        value.store.close()


def check_handoff_attention_state(case, monkeypatch):
    from tests.app.test_station_handoff import frozen, package
    from unilabos.app.center_client.handoff import HandoffBridge
    bridge = HandoffBridge.__new__(HandoffBridge)
    bridge.authority = case['auth']['target']
    empty = bridge.attention_state()
    frozen(case)
    protected = bridge.attention_state()
    assert protected != empty
    authority = bridge.authority
    authority.apply_center_snapshot(package(dict(case['payload'], revision=case['payload']['revision'] + 1, status='canceling')))
    canceling = bridge.attention_state()
    assert canceling != protected
    from unilabos.app.scheduler.inventory import handoff_attention
    def forbidden(*args):
        pytest.fail('变更检测不能组装完整交接证明')
    monkeypatch.setattr(handoff_attention, 'handoff_details', forbidden)
    assert bridge.attention_state() == canceling


def test_bridge_attention_state_tracks_handoff_without_reading_full_details(tmp_path, monkeypatch):
    from tests.app.test_station_handoff import handoff_inventory_fixture
    fixture = handoff_inventory_fixture(tmp_path)
    case = next(fixture)
    try:
        check_handoff_attention_state(case, monkeypatch)
    finally:
        fixture.close()


def test_bridge_attention_state_tracks_export_failure_retry_and_resolution(tmp_path, monkeypatch):
    from tests.app.center_client import test_material_outcomes as outcomes
    from tests.app.center_client.test_material_export_retry import authority, pump_cycle
    from tests.app.center_client.test_material_exports import Allocator, complete, uid
    from tests.app.center_client.test_material_exports import inventory as inventory_fixture
    from unilabos.app.center_client.material import MaterialBridge
    fixture = inventory_fixture.__wrapped__(tmp_path, monkeypatch)
    inventory = next(fixture)
    try:
        with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
            bridge = MaterialBridge(Allocator(), store, authority(inventory[0], durable, [10000]))
            before = bridge.attention_state()
            complete(store, task, uid())
            pump_cycle(bridge)
            failed = bridge.attention_state()
            assert failed != before
            item = bridge.export_attention_items()[0]
            bridge.resume_failure(identity, 'prepare', expected_revision=item['revision'])
            retrying = bridge.attention_state()
            assert retrying != failed
            def forbidden():
                pytest.fail('变更检测不能读取完整物料来源')
            monkeypatch.setattr(bridge, 'export_attention_items', forbidden)
            monkeypatch.setattr(bridge.authority, 'export_failures', forbidden)
            assert bridge.attention_state() == retrying
            with bridge.authority.store.transaction() as c:
                bridge.authority._clear_failure_in(c, identity, 'prepare')
            assert bridge.attention_state() != retrying
    finally:
        fixture.close()


def test_http_reads_session_and_snapshot_only_after_runtime_poll_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    snapshot = {'manual_confirmation_count': 0, 'exception_count': 0,
        'items': {'manual_confirmations': [], 'exceptions': []}}
    runtime = snapshot_runtime(tmp_path, snapshot)
    client = http_client(runtime)
    started = threading.Event()
    def request():
        started.set()
        return client.get('/api/v1/center-attentions')
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with runtime._poll_lock:
                future = executor.submit(request)
                assert started.wait(1)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.05)
                runtime.client.session.session_epoch = 9
                runtime.client.session.process_uuid = 'new-process'
                snapshot['items']['human_interactions'] = [{'kind': 'form', 'arguments': {'title': '确认'}}]
                snapshot['manual_confirmation_count'] = 1
            data = future.result(timeout=2).json()['data']
            assert data['session_epoch'] == 9
            assert data['process_uuid'] == 'new-process'
            assert data['snapshot']['manual_confirmation_count'] == 1
    finally:
        runtime.store.close()


def test_http_export_summary_preserves_large_original_material_detail(tmp_path, monkeypatch):
    import json

    from tests.app.center_client import test_material_outcomes as outcomes
    from tests.app.center_client.test_material_export_retry import authority, pump_cycle
    from tests.app.center_client.test_material_exports import Allocator, complete, uid
    from tests.app.center_client.test_material_exports import inventory as inventory_fixture
    from unilabos.app.center_client.material import MaterialBridge
    from unilabos.app.center_client.material_attention_api import create_router
    fixture = inventory_fixture.__wrapped__(tmp_path, monkeypatch)
    inventory = next(fixture)
    runtime = snapshot_runtime(tmp_path, {'manual_confirmation_count': 0, 'exception_count': 0,
        'items': {'manual_confirmations': [], 'exceptions': []}})
    try:
        with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
            bridge = MaterialBridge(Allocator(), store, authority(inventory[0], durable, [10000]))
            complete(store, task, uid())
            pump_cycle(bridge)
            original = '原始中文材料' * 30000
            with bridge.authority.store.transaction() as c:
                row = c.execute('SELECT source_json FROM station_material_export WHERE invocation_uuid=?', (identity,)).fetchone()
                source = json.loads(row['source_json'])
                source['source']['outputs'][0]['debug'] = original
                c.execute('UPDATE station_material_export SET source_json=? WHERE invocation_uuid=?',
                    (json.dumps(source, ensure_ascii=False), identity))
            runtime.materials = bridge
            client = http_client(runtime)
            client.app.include_router(create_router(lambda: runtime))
            before = client.get('/api/v1/center-material-exports/' + identity).json()
            summary = client.get('/api/v1/center-attentions').json()['data']['snapshot']
            assert summary['exception_count'] == 1
            assert set(summary['items']['exceptions'][0]['meta_data']) == {'last_error', 'execution_status'}
            after = client.get('/api/v1/center-material-exports/' + identity).json()
            assert after == before
            assert after['data']['items'][0]['meta_data']['materials'][0]['debug'] == original
    finally:
        runtime.store.close()
        fixture.close()


def test_native_attention_state_filters_local_manual_and_preserves_center_changes(tmp_path, monkeypatch):
    from tests.workflow.test_center_attention_snapshot import JOB_UUID, TASK_UUID, _bind_center_task, _seed
    from unilabos.workflow.store import WorkflowStore
    store = WorkflowStore(tmp_path / 'native-manual.db')
    try:
        _seed(store, center_created=False)
        before = store.center_attention_state()
        assert before == ((), (), ())
        _bind_center_task(store, TASK_UUID)
        pending = store.center_attention_state()
        assert pending != before
        def forbidden(*args):
            pytest.fail('原生通知筛选不能读取完整快照')
        from unilabos.workflow import center_attention
        monkeypatch.setattr(center_attention, 'current_snapshot', forbidden)
        assert store.center_attention_state() == pending
        with store.transaction() as c:
            c.execute("UPDATE workflow_manual_confirmation SET status='approved' WHERE workflow_node_job_uuid=?", (JOB_UUID,))
        assert store.center_attention_state() == before
    finally:
        store.close()


def test_native_attention_state_filters_local_human_interactions(tmp_path):
    from tests.workflow.test_center_attention_snapshot import TASK_UUID, _bind_center_task
    from tests.workflow.test_center_human_interaction_attention import CASES, seed_human
    from unilabos.workflow.store import WorkflowStore
    store = WorkflowStore(tmp_path / 'native-human.db')
    try:
        interactions, _event, request = seed_human(store, *CASES[0][:2], center_created=False)
        before = store.center_attention_state()
        assert before == ((), (), ())
        _bind_center_task(store, TASK_UUID)
        pending = store.center_attention_state()
        assert pending != before
        interactions.reply(request['uuid'], CASES[0][2])
        assert store.center_attention_state() == before
    finally:
        store.close()


def test_native_concurrent_callbacks_cannot_hide_clear_after_aba(tmp_path):
    """先读空的旧回调不得覆盖较新待办基线，导致后续清空通知消失。"""
    runtime = snapshot_runtime(tmp_path, {'manual_confirmation_count': 0, 'exception_count': 0,
        'items': {'manual_confirmations': [], 'exceptions': []}})
    empty, pending = ((), (), ()), (('center-manual',), (), ())
    actual = [empty]
    published = []
    first_read = threading.Event()
    release_first = threading.Event()
    second_lock_attempt = threading.Event()
    second_finished = threading.Event()
    read_with_lock = []
    errors = []
    mutex = threading.Lock()

    class ObservedLock:
        # 只控制调度：旧实现先让B完整提交，再恢复A；修复后第二个读取被真锁阻挡。
        def __enter__(self):
            if threading.current_thread().name == 'second-native':
                second_lock_attempt.set()
            mutex.acquire()

        def __exit__(self, *args):
            mutex.release()

    def read_state():
        state = actual[0]
        if threading.current_thread().name == 'first-native':
            read_with_lock.append(mutex.locked())
            first_read.set()
            assert release_first.wait(2)
        return state

    def callback():
        try:
            runtime._notify_native_attention_changes()
        except BaseException as error:
            errors.append(error)
        finally:
            if threading.current_thread().name == 'second-native':
                second_finished.set()

    runtime.service.center_attention_state = read_state
    runtime._native_attention_state = empty
    runtime._native_attention_lock = ObservedLock()
    runtime._notifications = SimpleNamespace(notify_attention_changed=lambda: published.append(actual[0]))
    first = threading.Thread(target=callback, name='first-native')
    second = threading.Thread(target=callback, name='second-native')
    try:
        first.start()
        assert first_read.wait(1)
        actual[0] = pending
        second.start()
        assert second_lock_attempt.wait(1)
        if not read_with_lock[0]:
            assert second_finished.wait(1)
        release_first.set()
        first.join(1)
        second.join(1)
        assert not first.is_alive() and not second.is_alive()
        assert not errors
        assert published and published[-1] == pending
        # 中心已读到B。恢复空A时必须还有提示，不能被迟到回调的旧A误判无变化。
        actual[0] = empty
        runtime._notify_native_attention_changes()
        assert published[-1] == empty
    finally:
        release_first.set()
        if first.ident is not None:
            first.join(2)
        if second.ident is not None:
            second.join(2)
        runtime.store.close()
