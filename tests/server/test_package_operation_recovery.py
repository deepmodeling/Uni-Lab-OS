"""真实子进程中断源码/台账提交，恢复后必须人工确认；只使用临时虚拟驱动。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.services import driver_packages as dp
from unilabos.server.services.host_mutations import HostMutationBusy, get_host_mutation_gate


CRASH_SCRIPT = r'''
import os, sys
from pathlib import Path
from unilabos.server.services import driver_packages as dp
from tests.server.test_driver_packages import _write_source_tree
root = Path(sys.argv[1])
phase = sys.argv[2]
service = dp.DriverPackageService(root)
old = service._package_target('demo', '1.0')
_write_source_tree(old, name='demo', version='1.0', package='demo', device_id='old', deps=[])
dp.save_ledger(root, {'demo': dp.DriverPackageRecord(name='demo', source_kind='archive', version='1.0',
    package_root=str(old), package_dirs=[str(old / 'demo')], device_ids=['old'])})
staging = root / 'download'
source = staging / 'source'
_write_source_tree(source, name='demo', version='1.0', package='demo', device_id='new', deps=[])
service._fetch_source_tree = lambda *args: (staging, source, 'new-digest')
service._install_dependencies = lambda *args: ''
original_save = dp.save_ledger
def save(path, records):
    if phase == 'before_ledger': os._exit(73)
    original_save(path, records)
    if phase == 'after_ledger': os._exit(73)
dp.save_ledger = save
original_checkpoint = service._checkpoint
def checkpoint(operation_id, **values):
    original_checkpoint(operation_id, **values)
    if phase == 'before_move' and values.get('phase') == 'source_switch': os._exit(73)
service._checkpoint = checkpoint
if phase == 'uninstall':
    dp.save_ledger = lambda *args: os._exit(73)
    operation = service.start_uninstall('demo')
else:
    operation = service.start_install('https://example.invalid/demo.zip')
import time
while service.operation(operation['operation_id'])['status'] == 'running': time.sleep(.01)
raise RuntimeError('expected a hard process exit')
'''


@pytest.mark.parametrize('phase,committed', [('before_move', False), ('before_ledger', False), ('after_ledger', True), ('uninstall', False)])
def test_process_crash_reconciles_source_and_ledger_once_and_requires_confirmation(tmp_path, monkeypatch, phase, committed):
    process = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(tmp_path), phase],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 73, process.stdout + process.stderr
    service = dp.DriverPackageService(tmp_path)
    operation = service.operations()[0]
    assert operation['status'] == 'interrupted'
    assert operation['requires_confirmation']
    assert operation['recovery']['outcome'] == ('committed' if committed else 'rolled_back')
    record = dp.load_ledger(tmp_path)['demo']
    assert record.device_ids == (['new'] if committed else ['old'])
    assert ('new' if committed else 'old') in (Path(record.package_root) / 'demo/pump.py').read_text()
    gate = get_host_mutation_gate(tmp_path)
    with pytest.raises(HostMutationBusy):
        gate.acquire('slave_start')
    with pytest.raises(dp.DriverPackageBusyError):
        service.set_enabled('demo', False)
    # 再次打开不重复回滚，更不能把完成的卸载/安装误作一次新操作。
    assert dp.DriverPackageService(tmp_path).operations()[0] == operation
    monkeypatch.setattr(dp, '_service', service)
    from unilabos.server.api.driver_packages import create_driver_packages_router
    app = FastAPI()
    app.include_router(create_driver_packages_router())
    with TestClient(app) as client:
        url = f"/api/v1/driver-packages/operations/{operation['operation_id']}/confirm"
        # 版本过期 → 状态冲突 3003；核实说明为空 → 参数不合法 1000（HTTP 200 信封）
        error_of(client.post(url, json={'expected_version': operation['version'] + 1, 'reason': '已核实'}), 3003)
        error_of(client.post(url, json={'expected_version': operation['version'], 'reason': ' '}), 1000)
        body = {'expected_version': operation['version'], 'reason': '已核实源码、台账和共享依赖，允许后续操作'}
        confirmed = ok_data(client.post(url, json=body))
        assert not confirmed['requires_confirmation']
        assert ok_data(client.post(url, json=body)) == confirmed
    with gate.hold('slave_start'):
        pass
    assert not dp.DriverPackageService(tmp_path).operations()[0]['requires_confirmation']


@pytest.mark.parametrize('mode', ['quiescent', 'immediate'])
def test_restart_waits_for_durable_package_finish_and_blocks_new_writes(tmp_path, monkeypatch, mode):
    from unilabos.server.backend import restart as restart_module
    from unilabos.server.backend.restart import RestartCoordinator
    from tests.server.test_driver_packages import _write_source_tree, _wait
    monkeypatch.setattr(restart_module, '_POLL_INTERVAL_SECONDS', 0.01)
    service = dp.DriverPackageService(tmp_path)
    source = tmp_path / 'source'
    _write_source_tree(source, name='demo', version='1.0', package='demo', device_id='demo', deps=['fake'])
    entered, release, restarted = threading.Event(), threading.Event(), threading.Event()
    def installer(*args):
        entered.set()
        assert release.wait(5)
        return 'test'
    monkeypatch.setattr(service, '_install_dependencies', installer)
    gate = get_host_mutation_gate(tmp_path)
    coordinator = RestartCoordinator(lambda: None, lambda: None, mutation_gate=gate)
    monkeypatch.setattr(coordinator, '_edge_control_service', lambda: None)
    observed = []
    def restart():
        observed.extend(service.operations())
        restarted.set()
    monkeypatch.setattr(coordinator, '_execute_process_restart', restart)
    operation = service.start_install(str(source))
    assert entered.wait(2)
    coordinator.request(mode=mode, scope='process')
    try:
        assert not restarted.wait(.08)
        assert coordinator.status()['mutation']['active'] == 'package'
        release.set()
        assert restarted.wait(3)
        assert observed[0]['status'] == 'succeeded'
        assert dp.DriverPackageService(tmp_path).operation(operation['operation_id'])['status'] == 'succeeded'
        with pytest.raises(dp.DriverPackageBusyError):
            service.start_install(str(source))
    finally:
        release.set()
        gate.cancel_restart(coordinator)


def test_second_process_cannot_modify_packages_during_restart(tmp_path):
    gate = get_host_mutation_gate(tmp_path)
    owner = object()
    gate.request_restart(owner)
    assert gate.claim_restart(owner)
    script = '''
import sys
from unilabos.server.services.host_mutations import get_host_mutation_gate, HostMutationBusy
try: get_host_mutation_gate(sys.argv[1]).acquire('package')
except HostMutationBusy: sys.exit(42)
sys.exit(1)
'''
    try:
        result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], capture_output=True, timeout=15)
        assert result.returncode == 42, result.stderr
    finally:
        gate.cancel_restart(owner)


def test_busy_restart_preserves_existing_slave(tmp_path, monkeypatch):
    from unilabos.server.services import device_processes as processes
    from tests.server.test_device_processes import FakePopen, _payload
    monkeypatch.setattr(processes.subprocess, 'Popen', FakePopen)
    service = processes.DeviceProcessService(tmp_path)
    spec = service.create(_payload(package_names=[]))
    service.start(spec['id'])
    pid = service.get(spec['id'])['pid']
    try:
        with get_host_mutation_gate(tmp_path).hold('package'):
            with pytest.raises(processes.DeviceProcessBusyError):
                service.restart(spec['id'])
            assert service.get(spec['id'])['pid'] == pid
    finally:
        service.shutdown()


def test_unconfirmed_recovery_starts_management_without_device_runtime(tmp_path, monkeypatch):
    from unilabos.app import runtime_startup
    from unilabos.config.config import BasicConfig
    process = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(tmp_path), 'after_ledger'],
        capture_output=True, timeout=30)
    assert process.returncode == 73
    service = dp.DriverPackageService(tmp_path)
    monkeypatch.setattr(dp, '_service', service)
    monkeypatch.setattr(BasicConfig, 'is_host_mode', True)
    monkeypatch.setattr(runtime_startup, '_ensure_management_port_available', lambda: None)
    called = []
    monkeypatch.setattr(runtime_startup, '_start_management_server', lambda: called.append('management'))
    monkeypatch.setattr('unilabos.backend.start_backend', lambda **args: pytest.fail('不得初始化驱动'))
    runtime_startup.run_runtime({'visual': 'disable'})
    assert called == ['management']


def test_edge_wait_needs_new_host_process_not_an_unchanged_connection(monkeypatch):
    from types import SimpleNamespace
    from unilabos.server.backend import restart as restart_module
    from unilabos.server.backend.restart import RestartCoordinator
    monkeypatch.setattr(restart_module, '_POLL_INTERVAL_SECONDS', .005)
    remote = SimpleNamespace(connected=True, edge_process='old')
    coordinator = RestartCoordinator(lambda: None, lambda: None)
    coordinator._pending = True
    monkeypatch.setattr(coordinator, '_edge_control_service', lambda: remote)
    assert not coordinator._wait_edge_reconnected(previous_process='old', disconnect_timeout=.02, reconnect_timeout=.02)
    remote.edge_process = 'new'
    assert coordinator._wait_edge_reconnected(previous_process='old', disconnect_timeout=.02, reconnect_timeout=.02)


def test_recovery_confirmation_can_finish_while_manual_restart_waits(tmp_path):
    result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(tmp_path), 'before_ledger'], capture_output=True, timeout=30)
    assert result.returncode == 73
    service = dp.DriverPackageService(tmp_path)
    operation = service.operations()[0]
    gate, owner = service.mutation_gate, object()
    gate.request_restart(owner)
    try:
        assert not gate.claim_restart(owner)
        service.confirm_operation(operation['operation_id'], operation['version'], '已核实环境')
        assert gate.claim_restart(owner)
    finally:
        gate.cancel_restart(owner)


def test_manual_repair_retry_rechecks_ledger_before_confirmation(tmp_path):
    from unilabos.server.services import driver_package_journal as journal
    result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(tmp_path), 'before_ledger'], capture_output=True, timeout=30)
    assert result.returncode == 73
    previous = journal.ledger_data(tmp_path)
    journal.atomic_json(tmp_path / 'driver_packages.json', {'version': 2, 'packages': []})
    service = dp.DriverPackageService(tmp_path)
    operation = service.operations()[0]
    assert operation['recovery']['outcome'] == 'manual_repair_required'
    with pytest.raises(dp.DriverPackageBusyError):
        service.confirm_operation(operation['operation_id'], operation['version'], '未修复时不能确认')
    journal.atomic_json(tmp_path / 'driver_packages.json', previous)
    recovered = service.retry_recovery(operation['operation_id'], operation['version'], '已还原正确台账')
    assert recovered['recovery']['outcome'] == 'rolled_back'
    assert recovered['requires_confirmation']
    service.confirm_operation(recovered['operation_id'], recovered['version'], '复核源码与台账一致')
