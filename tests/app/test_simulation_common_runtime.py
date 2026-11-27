"""公共仿真机制使用非 SZLab 声明验证，避免把设备特例当作公共合同。"""
import json
from threading import Event
from types import SimpleNamespace

import pytest

from unilabos.app.scheduler.inventory.runtime_view import InventoryView
from unilabos.simulation.assembly import Provider, SourceCatalog
from unilabos.simulation.composition import compile_composition
from unilabos.simulation.driver_task import DriverTask
from unilabos.simulation.signals import ResponseSignals, SignalGateway


def test_compilation_freezes_provider_and_preserves_evidence_digest():
    """另一设备的提供者只选择一次，外部配置修改不影响已编译实例。"""
    provider=Provider('sensor','oven.temperature','C',float,'oven')
    config=dict(attachment='direct',feedback='measured',completion='ready',sources={})
    result=compile_composition(config,catalog=SourceCatalog({'temperature':[provider]}),
        defaults={'temperature':'sensor'},policies={'oven.ready':{'temperature'}},
        aliases={'ready':'oven.ready'},completion_modes={'oven.ready':'ready'})
    digest=result.digest
    config['sources']['temperature']='missing'
    assert result.selected['temperature'] is provider and result.digest==digest
    with pytest.raises(TypeError):result.selected['temperature']=provider
    assert json.loads(json.dumps(result.describe()))['required_ports']==['temperature']


def test_driver_failure_and_cooperative_close_are_observable():
    """驱动异常传播至扫描线程，协作取消必须确认工作线程已退出。"""
    cancel=Event()
    task=DriverTask(cancel.set)
    def operation():
        """模拟可协作取消的阻塞原驱动。"""
        cancel.wait(1)
        raise ValueError('driver fault')
    task.start(operation)
    with pytest.raises(RuntimeError,match='busy'):task.start(operation)
    task.close()
    assert not task.thread.is_alive()
    with pytest.raises(RuntimeError,match='driver fault'):task.check()


def test_gateway_rejects_bad_quality_and_cancellation():
    """无效读数不能满足轮询，取消后不能再向协议端写命令。"""
    writes=[]
    sample=SimpleNamespace(StatusCode=SimpleNamespace(is_good=lambda:False),Value=SimpleNamespace(Value=1))
    gateway=SignalGateway(lambda name:sample,lambda *args:writes.append(args))
    with pytest.raises(RuntimeError,match='sample_invalid'):gateway.wait_equal('temperature',1,.1)
    gateway.cancel()
    with pytest.raises(RuntimeError,match='cancelled'):gateway.write_variable('setpoint',1)
    assert writes==[]


def test_response_signal_roles_preserve_command_ownership():
    """响应模型不能写主机命令，传感与状态分别使用声明的写入者。"""
    writes=[]
    session=SimpleNamespace(store=SimpleNamespace(signals={name:SimpleNamespace(role=role) for name,role in
        [('setpoint','host_command'),('measured','sensor_input'),('ready','controller_status')]}),local_value=lambda name:None)
    response=ResponseSignals(session,'oven.response','oven.sensor',lambda *args:writes.append(args))
    for name in ('setpoint','measured','ready','unknown'):response.write(name,1)
    assert writes==[('oven.sensor','measured',1),('oven.response','ready',1)]
    assert response.read('measured') is None


def test_inventory_relations_distinguish_roots_and_missing(station_inventory):
    """真实库存查询保留根材料与已不存在材料的区别，读取不产生提交。"""
    store,service,ids=station_inventory
    view=InventoryView(store)
    current=view.relations([ids['vessel'],'missing',ids['vessel']])
    assert ids['vessel'] in current and 'missing' not in current
    expected=store.query_one('SELECT parent_uuid FROM material WHERE uuid=?',(ids['vessel'],))
    assert current[ids['vessel']]==expected['parent_uuid']


from tests.app.test_inventory_dispatch_admission import station_inventory


def test_execution_service_partial_initialization_closes_owned_resources(monkeypatch,tmp_path):
    """领域装配失败时公共服务关闭已打开的日志与设备资源，不遗留半初始化实例。"""
    from unilabos.simulation import session as module
    class Port:
        def call(self, operation, payload):
            assert operation == 'materials.index'
            return {}
    def fail(config,materials,record):
        """模拟领域校验失败，仍允许记录失败前事件。"""
        record({'event':'building'})
        raise ValueError('invalid device binding')
    path=tmp_path/'events.jsonl'
    with pytest.raises(ValueError,match='invalid device binding'):
        module.SimulationSession(dict(journal=path,command_journal=str(path)+".commands.db"),fail,execution_port=Port())
    assert json.loads(path.read_text())=={'event':'building'}


def test_observation_publisher_uses_workspace_credentials(monkeypatch, tmp_path):
    """只配置工作区凭据时，执行与观测使用同一身份，不发送匿名采样。"""
    from unilabos.config.config import EdgeControlConfig
    from unilabos.simulation import provider_client
    from unilabos.simulation.session import DeviceRuntime, SimulationSession

    captured = {}
    class Publisher:
        def __init__(self, url, **kwargs):
            captured.update(kwargs)
        def ready(self):
            pass
        def close(self):
            captured['closed'] = True
    class Port:
        def call(self, operation, payload):
            assert operation == 'materials.index'
            return {}
    monkeypatch.setattr(EdgeControlConfig, 'api_key', 'workspace-test-key')
    monkeypatch.setattr(provider_client, 'ObservationPublisher', Publisher)
    config = dict(journal=tmp_path/'events.jsonl', command_journal=tmp_path/'commands.db',
                  world=dict(world_id='world', session_id='source'),
                  observation_provider=dict(url='http://localhost/api/v1/device-state/observations/provider', versions={}))
    model = SimpleNamespace(close=lambda: None)
    session = SimulationSession(config, lambda *args: DeviceRuntime(object(), model, {}, observation_values=lambda _: {}),
                                execution_port=Port())
    assert captured['api_key'] == 'workspace-test-key'
    session.close()
    assert captured['closed']
