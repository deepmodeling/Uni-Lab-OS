"""以真实库存 Permit 验证原生驱动开始后的运行凭据。"""
from contextlib import contextmanager
from dataclasses import asdict

import pytest

from tests.app.test_inventory_dispatch_admission import _request, station_inventory
from unilabos.app.scheduler.execution_authority import ExecutionAuthority


def test_gate_checks_frozen_executor_parameters_and_running_permit(station_inventory,monkeypatch):
    store,service,identities=station_inventory
    admitted=_request(identities)
    permit=service.station_resources.acquire_dispatch_permit(admitted).permit
    service.station_resources.transition_dispatch_permit(permit.claim_uuid,target_state='reserved')
    job=dict(uuid=admitted.job_uuid,workflow_task_uuid=admitted.task_uuid,workflow_node_uuid='n',material_uuid=None,status='running',current_attempt=True,
             attempt=admitted.attempt,param={'resource':{'uuid':identities['vessel']}},dispatch_effect_uuid=permit.effect_uuid,
             dispatch_parameter_hash=admitted.parameter_hash,expected_change_set=admitted.expected_change_set)
    node=dict(uuid='n',material_uuid='device',kind='device_action',action_name='move',
              action_resource_contract={'version':1,'transfer':{'material_param':'resource'}})
    class Workflow:
        def get_job(self,key): assert key==job['uuid'];return job
        def get_task(self,key): assert key==admitted.task_uuid;return {'execution_plan':{'nodes':[node]}}
        @contextmanager
        def read(self):yield None
    claim=dict(attempt=admitted.attempt,claim_uuid=permit.claim_uuid,resource_keys=[f.lock_key for f in permit.fences],fences=[asdict(f) for f in permit.fences])
    monkeypatch.setattr('unilabos.app.scheduler.execution_authority.get_execution_claim',lambda *args,**kwargs:claim)
    gate=ExecutionAuthority(Workflow(),service,{'driver':{'uuid':'device','actions':{'go':'move'}}})
    request=dict(identity={'task_uuid':admitted.task_uuid,'node_job_uuid':job['uuid']},device_type='driver',operation='go',parameters={},resources=job['param'])
    with pytest.raises(ValueError,match='native_running_permit_mismatch'):gate.authorize(request)
    service.station_resources.transition_dispatch_permit(permit.claim_uuid,target_state='running')
    assert gate.authorize(request)==(job,claim)
    node['material_uuid']='other'
    with pytest.raises(ValueError,match='native_job_binding_mismatch'):gate.authorize(request)
    node['material_uuid']='device'
    with pytest.raises(ValueError,match='native_job_parameter_mismatch'):gate.authorize({**request,'parameters':{'extra':1}})
    with pytest.raises(ValueError,match='native_job_parameter_mismatch'):
        gate.authorize({**request,'parameters':{'resource':{'uuid':'physical-other'}},'resources':request['resources']})
    with pytest.raises(ValueError,match='native_job_parameter_mismatch'):
        gate.authorize({**request,'resources':{'resource':{'uuid':identities['vessel'],'extra':100}}})
    job['param']['volume_ml']=1
    numeric_request={**request,'parameters':{'volume_ml':1},'resources':{'resource':{'uuid':identities['vessel']}}}
    assert gate.authorize(numeric_request)==(job,claim)
    with pytest.raises(ValueError,match='native_job_parameter_mismatch'):
        gate.authorize({**numeric_request,'parameters':{},'resources':{**numeric_request['resources'],'volume_ml':1}})
    job['param']['mode']='fast'
    with pytest.raises(ValueError,match='native_job_parameter_mismatch'):
        gate.authorize({**numeric_request,'resources':{**numeric_request['resources'],'mode':'fast'}})
    del job['param']['mode']

    for changed in (True, 100):
        with pytest.raises(ValueError,match='native_job_parameter_mismatch'):
            gate.authorize({**numeric_request,'parameters':{'volume_ml':changed}})
    del job['param']['volume_ml']
    grant=gate.grant(request)
    with pytest.raises(ValueError,match='execution_grant_changed'):
        gate.settle(request,{'identity':request['identity']},grant={**grant,'attempt':999})
    receipt=gate.settle(request,{'identity':request['identity'],'physical_tick':12},grant=grant)
    assert receipt['scope']=='canonical_inventory' and receipt['job_uuid']==job['uuid']
    # 完成回执丢失后，真实后端已推进终态；会话只读原结果，不重新取得执行许可。
    from types import SimpleNamespace

    from unilabos.runtime.execution import JournaledExecution
    from unilabos.simulation.session import SimulationSession
    path = str(store.path) + '.commands.db'
    commands = JournaledExecution(path, namespace='simulation-execution')
    result = {'success': True, 'inventory_receipt': receipt}
    commands.run(request, grant, lambda: result)
    job['status'] = 'succeeded'
    restarted = SimpleNamespace(commands=JournaledExecution(path, namespace='simulation-execution'),
        authorize=gate.grant, publisher=None,
        executor=SimpleNamespace(execute=lambda _: pytest.fail('不能重发已完成动作')))
    assert SimulationSession.execute(restarted, request) == result
    with pytest.raises(ValueError, match='native_job_binding_mismatch'):
        gate.authorize(request)
    job['status'] = 'running'

    service.station_resources.transition_dispatch_permit(permit.claim_uuid,target_state='uncertain')
    with pytest.raises(ValueError,match='native_running_permit_mismatch'):gate.authorize(request)
