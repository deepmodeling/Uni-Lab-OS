"""页面接入通过真实运行时工厂装配库存桥，不依赖外部环境变量。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.app.center_client.test_connection import Center, Runtime
from unilabos.app.center_client.config import CenterConfigError
from unilabos.app.center_client.connection import CenterConnection, ConnectionError, connection_runtime
from unilabos.app.center_client.runtime import CenterRuntime
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.workflow.store import WorkflowStore


@pytest.fixture
def connected_inventory(tmp_path, monkeypatch):
    for name in ('CENTER_ENABLED', 'CENTER_INVENTORY_ENABLED', 'CENTER_LAB_UUID',
                 'CENTER_HANDOFF_POLICY_FILE', 'CENTER_TRANSPORT_ROUTE_FILE'):
        monkeypatch.delenv(name, raising=False)
    inventory = InventoryStore(str(tmp_path / 'inventory.db'))
    workflow = WorkflowStore(str(tmp_path / 'workflow.db'))
    service = SimpleNamespace(_task_scheduler_bridge=None)
    service.install_center_material_source_authority = lambda authority, session: setattr(service, 'source', authority)
    service.install_center_material_input_authority = lambda authority: setattr(service, 'inputs', authority)
    monkeypatch.setattr('unilabos.app.scheduler.integration.get_inventory_service', lambda: InventoryService(inventory))
    monkeypatch.setattr('unilabos.app.runtime_storage.get_runtime_working_directory', lambda: None)
    monkeypatch.setattr('unilabos.app.runtime_storage.retain_runtime_storage_for_center', lambda: None)
    started = []
    # 仅停止网络轮询；构造、持久身份和三种库存桥均使用生产代码。
    monkeypatch.setattr(CenterRuntime, 'start', lambda runtime: started.append(runtime))
    owners = []
    center = Center()

    def create():
        owner = connection_runtime(tmp_path / 'center.db', service, workflow)
        owner._request = center
        owners.append(owner)
        return owner

    try:
        yield create, center, service, started
    finally:
        for owner in owners:
            owner.close()
        workflow.close()
        inventory.close()


def test_pairing_enables_real_inventory_bridges_and_restart_restores_lab(connected_inventory):
    create, center, service, started = connected_inventory
    owner = create()
    owner.connect('https://center.test', 'a' * 64)
    owner.step()
    assert owner.status()['state'] == 'connected'
    assert owner.runtime.handoffs.authority.lab_uuid == center.lab
    assert owner.runtime.materials is not None
    assert owner.runtime.transport_preparations is not None
    assert service.source is owner.runtime.handoffs.authority
    assert service.inputs is not None
    assert started == [owner.runtime]
    record_path = owner.directory / 'connection.json'
    record = json.loads(record_path.read_text())
    assert record['lab_uuid'] == center.lab
    owner.close()
    # 兼容升级前的连接清单，通过已有 mTLS 接口补齐，无需重新输入接入码。
    record.pop('lab_uuid')
    record_path.write_text(json.dumps(record))
    restarted = create()
    restarted.step()
    assert restarted.runtime.handoffs.authority.lab_uuid == center.lab
    assert restarted.status()['station_uuid'] == center.station
    assert len(center.requests) == 1
    assert json.loads(record_path.read_text())['lab_uuid'] == center.lab


def test_inventory_sync_can_be_explicitly_disabled(connected_inventory, monkeypatch):
    create, _, service, started = connected_inventory
    monkeypatch.setenv('CENTER_INVENTORY_ENABLED', '0')
    owner = create()
    owner.connect('https://center.test', 'a' * 64)
    owner.step()
    assert owner.runtime.handoffs is None
    assert not hasattr(service, 'source')
    assert started == [owner.runtime]


def test_wrong_lab_configuration_cannot_override_center_identity(connected_inventory, monkeypatch):
    create, center, _, started = connected_inventory
    monkeypatch.setenv('CENTER_LAB_UUID', str(uuid4()))
    owner = create()
    owner.connect('https://center.test', 'a' * 64)
    with pytest.raises(CenterConfigError, match='CENTER_LAB_UUID.*不一致'):
        owner.step()
    assert not started
    monkeypatch.delenv('CENTER_LAB_UUID')
    owner.step()
    assert owner.runtime.handoffs.authority.lab_uuid == center.lab


def test_old_center_without_lab_gives_actionable_error(connected_inventory, monkeypatch):
    create, center, _, started = connected_inventory
    owner = create()

    def old_center(*args, **kwargs):
        result = dict(center(*args, **kwargs))
        result.pop('lab_uuid', None)
        return result

    owner._request = old_center
    owner.connect('https://center.test', 'a' * 64)
    with pytest.raises(CenterConfigError, match='CENTER_LAB_UUID'):
        owner.step()
    assert not started
    monkeypatch.setenv('CENTER_LAB_UUID', center.lab)
    owner.step()
    assert owner.runtime.handoffs.authority.lab_uuid == center.lab


def test_legacy_credentials_install_inventory_after_identity_lookup(connected_inventory, tmp_path):
    """旧环境变量凭据先查询中心归属，再在轮询启动前安装库存权威。"""
    from dataclasses import replace

    from unilabos.app.center_client.client import CenterClient
    from unilabos.app.center_client.durable import DurableCenterStore

    create, center, service, started = connected_inventory
    owner = create()
    owner.connect('https://center.test', 'a' * 64)
    owner.step()
    config = replace(owner.runtime.config, lab_uuid=None)
    owner.close()
    durable = DurableCenterStore(tmp_path / 'center.db', station_uuid=center.station, instance_uuid=center.instance)
    runtime = CenterRuntime(config, CenterClient(config), durable, service)
    legacy = CenterConnection(tmp_path / 'legacy-connection', owner._factory, runtime=runtime,
                              request=center, prepare_runtime=owner._prepare_runtime)
    try:
        assert runtime.handoffs is None
        legacy.step()
        assert legacy.status()['state'] == 'connected'
        assert runtime.handoffs.authority.lab_uuid == center.lab
        assert started[-1] is runtime
        legacy.step()
        assert started.count(runtime) == 1
    finally:
        legacy.close()


def test_connection_records_configuration_reason_in_status_and_log(tmp_path, monkeypatch, caplog):
    owner = CenterConnection(tmp_path / 'connection', Runtime)

    def invalid():
        raise CenterConfigError('缺少 CENTER_LAB_UUID')

    monkeypatch.setattr(owner, 'step', invalid)
    monkeypatch.setattr(owner._wake, 'wait', lambda _: owner._stop.set())
    owner._run()
    assert owner.status()['error_code'] == 'CENTER_CONFIGURATION_ERROR'
    assert 'CENTER_LAB_UUID' in owner.status()['error_message']
    assert 'CENTER_LAB_UUID' in caplog.text


@pytest.mark.parametrize('lab', ['invalid', str(uuid4())], ids=['invalid', 'different_lab'])
def test_center_cannot_change_saved_lab_identity(tmp_path, lab):
    center = Center()
    owner = CenterConnection(tmp_path / 'connection', Runtime, request=center)
    owner.connect('https://center.test', 'a' * 64)
    owner.step()
    original = owner.runtime.config.lab_uuid
    center.lab = lab
    with pytest.raises((CenterConfigError, ConnectionError)):
        owner.step()
    assert owner.runtime.config.lab_uuid == original
    assert json.loads((owner.directory / 'connection.json').read_text())['lab_uuid'] == original
