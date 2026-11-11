"""借还和样品托管必须跨暂停、异常、进程恢复保持物理互斥。"""
from __future__ import annotations
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from unilabos.protocol.runtime.resource_intervals import DeviceTenancyTransition, DeviceTenancyRequirement
from unilabos.server.services.runtime.workflow.device_tenancy import DeviceTenancies
from unilabos.server.services.runtime.workflow.resource_loans import ResourceLoans
from unilabos.server.services.runtime.workflow.errors import StoreConflict
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.resource_intervals import ResourceIntervals
from unilabos.server.services.runtime.workflow.service import WorkflowService
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.api.runtime.workflow import create_workflow_app
from tests.server.test_workflow_resource_intervals import make_task, request_for, service
from tests.server.test_workflow_task_cancel import Executor, command, wait_for


def finish(service, intervals, request, status="succeeded"):
    service.record_workflow_node_job_terminal(request.owner_uuid, status=status, return_info={}, error_info=[])
    intervals.release_job(request.owner_uuid)


def tenancy_request(service, task, index, material, *, acquire=None, release=None, in_place=None):
    values = {}
    if acquire or release:
        values["device_tenancy"] = DeviceTenancyTransition(material_uuid=material, acquire_device_id=acquire, release_device_id=release)
    if in_place:
        values["operate_in_place"] = DeviceTenancyRequirement(material_uuid=material, device_id=in_place)
    return request_for(service, task, index).model_copy(update=values)


def test_load_keeps_device_and_sample_until_unload_even_after_reopen(tmp_path):
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    intervals = ResourceIntervals(service)
    epoch = ExecutionClaims(service).activate_authority()
    task = make_task(service, ("robot", "reactor", "robot"))
    sample = str(uuid4())
    load = tenancy_request(service, task, 0, sample, acquire="reactor")
    assert intervals.admit(load, epoch)
    with service.write_lock: intervals.require_admission(service.connection, load)
    finish(service, intervals, load)
    identity = DeviceTenancies.snapshot(service.connection)[0]["tenancy_uuid"]
    other = make_task(service, ("reactor",))
    assert not intervals.admit(request_for(service, other), epoch)
    service.close()
    service = WorkflowService(path)
    try:
        intervals = ResourceIntervals(service)
        intervals.recover()
        epoch = ExecutionClaims(service).activate_authority()
        assert DeviceTenancies.snapshot(service.connection)[0]["tenancy_uuid"] == identity
        assert not intervals.admit(request_for(service, other), epoch)
        wrong = tenancy_request(service, task, 1, str(uuid4()), in_place="reactor")
        with pytest.raises(StoreConflict, match="原位"):
            intervals.admit(wrong, epoch)
        operate = tenancy_request(service, task, 1, sample, in_place="reactor")
        assert intervals.admit(operate, epoch)
        finish(service, intervals, operate)
        unload = tenancy_request(service, task, 2, sample, release="reactor")
        assert intervals.admit(unload, epoch)
        assert not intervals.admit(request_for(service, other), epoch)
        finish(service, intervals, unload)
        assert DeviceTenancies.snapshot(service.connection) == []
        assert intervals.admit(request_for(service, other), epoch)
    finally:
        service.close()


@pytest.mark.parametrize("status", ["failed","canceled","timeout","skipped"])
def test_unsuccessful_unload_never_means_sample_removed(service, status):
    intervals = ResourceIntervals(service)
    epoch = ExecutionClaims(service).activate_authority()
    task = make_task(service, ("robot", "robot"))
    sample = str(uuid4())
    load = tenancy_request(service, task, 0, sample, acquire="reactor")
    assert intervals.admit(load, epoch)
    finish(service, intervals, load)
    unload = tenancy_request(service, task, 1, sample, release="reactor")
    assert intervals.admit(unload, epoch)
    finish(service, intervals, unload, status)
    rows = DeviceTenancies.snapshot(service.connection)
    assert len(rows) == 1 and rows[0]["state"] == "retained"
    other = make_task(service, ("reactor",))
    assert not intervals.admit(request_for(service, other), epoch)
    service.finish_workflow_task(task["uuid"],status="canceled",output={},error_info=[])
    current = service.get_workflow_task(task["uuid"])
    assert current["cleanup_status"] == "requires_attention"
    with TestClient(create_workflow_app(service)) as client:
        url = f"/api/v1/workflow-device-tenancies/{rows[0]['tenancy_uuid']}/release"
        assert client.post(url,json={"expected_version":True,"reason":"核实"}).json()["code"] != 0
        body = {"expected_version": rows[0]["version"], "reason": "已核实样品取走，设备空闲"}
        result = client.post(url,json=body)
        assert result.json()["data"]["state"] == "released", result.text
        assert client.post(url,json=body).json()["data"] == result.json()["data"]
    assert intervals.admit(request_for(service, other), epoch)


def test_unknown_transfer_holds_both_devices_and_late_success_releases_only_source(service):
    intervals = ResourceIntervals(service)
    epoch = ExecutionClaims(service).activate_authority()
    task = make_task(service, ("robot", "robot"))
    sample = str(uuid4())
    load = tenancy_request(service,task,0,sample,acquire="reactor-a")
    assert intervals.admit(load,epoch)
    finish(service,intervals,load)
    transfer = tenancy_request(service,task,1,sample,acquire="reactor-b",release="reactor-a")
    assert intervals.admit(transfer,epoch)
    service.mark_workflow_node_job_running(transfer.owner_uuid)
    service.mark_workflow_node_job_execution_unknown(transfer.owner_uuid,"断连")
    intervals.release_job(transfer.owner_uuid)
    assert {row["device_key"] for row in DeviceTenancies.snapshot(service.connection)} == {"/devices/reactor-a","/devices/reactor-b"}
    assert all(row["state"] == "retained" for row in DeviceTenancies.snapshot(service.connection))
    with pytest.raises(StoreConflict):
        DeviceTenancies.confirm_release(service.connection,DeviceTenancies.snapshot(service.connection)[0]["tenancy_uuid"],2,"核实")
    finish(service,intervals,transfer)
    assert [(row["device_key"],row["state"]) for row in DeviceTenancies.snapshot(service.connection)] == [("/devices/reactor-b","loaded")]


def test_withdraw_unstarted_load_reverts_only_target(service):
    intervals = ResourceIntervals(service)
    epoch = ExecutionClaims(service).activate_authority()
    task = make_task(service, ("robot","robot"))
    sample = str(uuid4())
    load = tenancy_request(service,task,0,sample,acquire="a")
    assert intervals.admit(load,epoch)
    finish(service,intervals,load)
    transfer = tenancy_request(service,task,1,sample,acquire="b",release="a")
    assert intervals.admit(transfer,epoch)
    intervals.withdraw(transfer.owner_uuid)
    assert [(row["device_key"],row["state"]) for row in DeviceTenancies.snapshot(service.connection)] == [("/devices/a","loaded")]
    assert intervals.admit(transfer,epoch)


def test_tenancy_blocks_even_always_free_actions_and_duplicate_sample_load(service):
    intervals = ResourceIntervals(service)
    epoch = ExecutionClaims(service).activate_authority()
    task = make_task(service,("robot",))
    sample = str(uuid4())
    load = tenancy_request(service,task,0,sample,acquire="a")
    assert intervals.admit(load,epoch)
    finish(service,intervals,load)
    other = make_task(service,("a",))
    request = request_for(service,other).model_copy(update={"always_free":True})
    assert not intervals.admit(request,epoch)
    other_load = tenancy_request(service,other,0,sample,acquire="b")
    assert not intervals.admit(other_load,epoch)


def test_explicit_idle_loan_blocks_lender_until_borrowed_interval_returns(service):
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    with TestClient(create_workflow_app(service)) as client:
        try:
            lender, borrower = make_task(service), make_task(service)
            scheduler.submit(lender["uuid"])
            wait_for(lambda: len(executor.dispatched)==1)
            assert command(client,lender,"pause").status_code == 200
            executor.finish(0)
            wait_for(lambda: len(scheduler._waiting_step_nodes)==1)
            owner = ResourceIntervals(service).snapshot(lender["uuid"])[0]
            scheduler.submit(borrower["uuid"])
            wait_for(lambda: bool(scheduler._waiting_interval_jobs))
            body={"loan_uuid":str(uuid4()),"borrower_task_uuid":borrower["uuid"],"expected_version":owner["version"],"reason":"原任务暂停，设备空闲，可供借用"}
            url=f"/api/v1/workflow-resource-intervals/{owner['owner_uuid']}/lend"
            response=client.post(url,json=body)
            assert response.status_code==200, response.text
            wait_for(lambda: len(executor.dispatched)==2)
            assert client.post(url,json=body).json()["code"]==0
            assert command(client,lender,"resume").status_code==200
            executor.finish(1)
            wait_for(lambda: len(executor.dispatched)==3)
            assert executor.dispatched[2]["task_id"]==borrower["uuid"]
            executor.finish(2)
            wait_for(lambda: len(executor.dispatched)==4)
            assert executor.dispatched[3]["task_id"]==lender["uuid"]
            executor.finish(3)
            wait_for(lambda: service.get_workflow_task(lender["uuid"])["status"]=="succeeded")
            assert ResourceIntervals(service).snapshot()==[]
            assert ResourceLoans.snapshot(service.connection)==[]
            assert service.connection.execute("SELECT count(*) FROM workflow_resource_use").fetchone()[0]==0
        finally:
            scheduler.stop()


def idle_loan(service):
    intervals=ResourceIntervals(service)
    epoch=ExecutionClaims(service).activate_authority()
    lender,borrower=make_task(service),make_task(service,("reactor",))
    first=request_for(service,lender)
    assert intervals.admit(first,epoch)
    finish(service,intervals,first)
    with service.transaction() as conn:
        conn.execute("UPDATE workflow_task SET control_status='paused' WHERE uuid=?",(lender["uuid"],))
        owner=intervals.snapshot(lender["uuid"])[0]
        loan=ResourceLoans.lend(conn,owner["owner_uuid"],loan_uuid=str(uuid4()),borrower_task_uuid=borrower["uuid"],expected_version=owner["version"],reason="设备空闲")
    return intervals,epoch,lender,borrower,loan


def test_failed_borrow_retains_loan_until_manual_return_and_survives_restart(tmp_path):
    path=tmp_path/'runtime.db'
    service=WorkflowService(path)
    intervals,epoch,lender,borrower,loan=idle_loan(service)
    request=request_for(service,borrower)
    assert intervals.admit(request,epoch)
    finish(service,intervals,request,"failed")
    row=ResourceLoans.snapshot(service.connection)[0]
    assert row["state"]=="retained"
    assert not intervals.admit(request_for(service,lender,1),epoch)
    service.close()
    service=WorkflowService(path)
    try:
        intervals=ResourceIntervals(service)
        intervals.recover()
        epoch=ExecutionClaims(service).activate_authority()
        assert not intervals.admit(request_for(service,lender,1),epoch)
        with service.transaction() as conn:
            ResourceLoans.confirm_return(conn,loan["loan_uuid"],expected_version=row["version"],reason="已核实借用动作停止且设备空闲")
        assert intervals.admit(request_for(service,lender,1),epoch)
    finally:
        service.close()


def test_loan_begin_proof_revoked_after_return_and_no_tenancy_allowed(service):
    intervals,epoch,lender,borrower,loan=idle_loan(service)
    request=tenancy_request(service,borrower,0,str(uuid4()),acquire="reactor")
    with pytest.raises(StoreConflict,match="借用区间"):
        intervals.admit(request,epoch)
    request=request_for(service,borrower)
    assert intervals.admit(request,epoch)
    intervals.require_admission(service.connection,request)
    with service.transaction() as conn:
        conn.execute("UPDATE workflow_resource_loan SET state='returned' WHERE loan_uuid=?",(loan["loan_uuid"],))
    with pytest.raises(StoreConflict,match="借用许可"):
        intervals.require_admission(service.connection,request)


def test_device_wait_cycle_fails_without_partial_acquisition(service):
    intervals=ResourceIntervals(service)
    epoch=ExecutionClaims(service).activate_authority()
    a,b=make_task(service,("robot-a","robot-a")),make_task(service,("robot-b","robot-b"))
    for task,device in ((a,"a"),(b,"b")):
        request=tenancy_request(service,task,0,str(uuid4()),acquire=device)
        assert intervals.admit(request,epoch)
        finish(service,intervals,request)
    ar=request_for(service,a,1).model_copy(update={"device_claims":["b"]})
    br=request_for(service,b,1).model_copy(update={"device_claims":["a"]})
    assert not intervals.admit(ar,epoch)
    with pytest.raises(StoreConflict,match="等待环路"):
        intervals.admit(br,epoch)
    assert len(DeviceTenancies.snapshot(service.connection))==2
    assert service.connection.execute("SELECT count(*) FROM workflow_resource_use").fetchone()[0]==0


def test_completion_without_unload_requires_attention(service):
    intervals=ResourceIntervals(service)
    epoch=ExecutionClaims(service).activate_authority()
    task=make_task(service,("robot",))
    request=tenancy_request(service,task,0,str(uuid4()),acquire="reactor")
    assert intervals.admit(request,epoch)
    finish(service,intervals,request)
    result=service.finish_workflow_task(task["uuid"],status="succeeded",output={},error_info=[])
    assert result["status"]=="failed"
    assert result["cleanup_status"]=="requires_attention"
    assert any(item["code"]=="device_tenancy_active" for item in result["error_info"])


def test_device_contract_resolves_material_uuid_to_readable_device_id():
    from unilabos.server.backend.scheduler.tenancy_contract import resolve_tenancy_contract
    sample,target=str(uuid4()),str(uuid4())
    schema={"x-unilabos-action-contract":{"resource_contract":{"device_tenancy":{
        "mode":"task_while_loaded","material_param":"sample","acquire_device_param":"target"}}}}
    gateway=SimpleNamespace(get_material=lambda identity: SimpleNamespace(material=SimpleNamespace(resource_type="device",resource_id="reactor")))
    result=resolve_tenancy_contract(schema,{"sample":{"uuid":sample},"target":{"uuid":target}},"robot",gateway)
    assert result["device_claims"]==["reactor"]
    assert result["device_tenancy"].acquire_device_id=="reactor"
    assert result["device_tenancy"].material_uuid==sample


def test_v7_live_admission_migrates_without_releasing_resources(tmp_path):
    import sqlite3
    path=tmp_path/'runtime.db'
    service=WorkflowService(path)
    task=make_task(service)
    request=request_for(service,task)
    claims=ExecutionClaims(service)
    epoch=claims.activate_authority()
    assert ResourceIntervals(service).admit(request,epoch)
    claim=claims.reserve(request,authority_epoch=epoch)
    service.close()
    with sqlite3.connect(path) as conn:
        for table in ('workflow_resource_waiter','workflow_tenancy_transition','workflow_device_tenancy','workflow_resource_loan'):
            conn.execute(f'DROP TABLE {table}')
        legacy=request.model_dump(mode='json')
        for field in ('device_claims','device_tenancy','operate_in_place'):
            legacy.pop(field)
        for table in ('workflow_resource_admission','workflow_execution_claim'):
            conn.execute(f'UPDATE {table} SET request=?',(json.dumps(legacy),))
        conn.execute("UPDATE schema_identity SET checksum='c8c8b2e291f09b5e22b31b4d530774dea0f205459d3a8b24abde670925a28536' WHERE database_key='runtime'")
        conn.execute('PRAGMA user_version=7')
    service=WorkflowService(path)
    try:
        ResourceIntervals(service).recover()
        assert ExecutionClaims(service).get(request.owner_uuid)['claim_uuid']==claim['claim_uuid']
        assert service.connection.execute('PRAGMA user_version').fetchone()[0]==8
        competitor=make_task(service,('reactor',))
        epoch=ExecutionClaims(service).activate_authority()
        assert not ResourceIntervals(service).admit(request_for(service,competitor),epoch)
        assert list(tmp_path.glob('runtime.db.pre-v8-*.bak'))
    finally:
        service.close()


def test_concurrent_tenancy_acquire_has_single_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path=tmp_path/'runtime.db'
    service=WorkflowService(path)
    intervals,epoch,lender,borrower,loan=idle_loan(service)
    first=make_task(service,('robot-a',))
    second=make_task(service,('robot-b',))
    requests=[tenancy_request(service,task,0,str(uuid4()),acquire='balance') for task in (first,second)]
    def admit(request):
        local=WorkflowService(path)
        try:
            return ResourceIntervals(local).admit(request,epoch)
        finally:
            local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(admit,requests))==[False,True]
    assert len(DeviceTenancies.snapshot(service.connection))==1
    service.close()


def test_unmanaged_tenancy_contract_still_rejected():
    from unilabos.registry.typed_contract import validate_action_values,TypedActionValueError
    schema={'x-unilabos-action-contract':{'resource_contract':{'operate_in_place':{'material_param':'sample'}}},
            'properties':{'goal':{'type':'object','properties':{}}}}
    with pytest.raises(TypedActionValueError,match='资源执行合同'):
        validate_action_values(schema,{})
    assert validate_action_values(schema,{},managed_resources=True)=={}


def test_loaded_material_or_device_cannot_be_lent(service):
    intervals=ResourceIntervals(service)
    epoch=ExecutionClaims(service).activate_authority()
    task=make_task(service,('reactor','reactor'))
    request=tenancy_request(service,task,0,str(uuid4()),acquire='reactor')
    assert intervals.admit(request,epoch)
    finish(service,intervals,request)
    other=make_task(service,('reactor',))
    owner=intervals.snapshot(task['uuid'])[0]
    with service.transaction() as conn:
        conn.execute("UPDATE workflow_task SET control_status='paused' WHERE uuid=?",(task['uuid'],))
        with pytest.raises(StoreConflict,match='样品'):
            ResourceLoans.lend(conn,owner['owner_uuid'],loan_uuid=str(uuid4()),borrower_task_uuid=other['uuid'],expected_version=owner['version'],reason='借用')


def test_unknown_borrow_must_settle_before_manual_return(service):
    intervals,epoch,lender,borrower,loan=idle_loan(service)
    request=request_for(service,borrower)
    assert intervals.admit(request,epoch)
    service.mark_workflow_node_job_running(request.owner_uuid)
    service.mark_workflow_node_job_execution_unknown(request.owner_uuid,'断连')
    intervals.release_job(request.owner_uuid)
    row=ResourceLoans.snapshot(service.connection)[0]
    assert row['state']=='retained'
    with service.transaction() as conn:
        with pytest.raises(StoreConflict,match='未收敛'):
            ResourceLoans.confirm_return(conn,loan['loan_uuid'],expected_version=row['version'],reason='核实')
    finish(service,intervals,request)
    assert ResourceLoans.snapshot(service.connection)[0]['state']=='retained'
    with service.transaction() as conn:
        ResourceLoans.confirm_return(conn,loan['loan_uuid'],expected_version=row['version'],reason='已核实借用结束')
    assert intervals.admit(request_for(service,lender,1),epoch)


def test_driver_contract_change_requires_new_plan_and_is_part_of_start_identity():
    from unilabos.registry.typed_contract import require_frozen_resource_contract,TypedActionValueError
    from unilabos.protocol.runtime.execution_start import execution_identity,identity_hash
    contract={"version":2,"operate_in_place":{"material_param":"sample"}}
    schema={"x-unilabos-action-contract":{"resource_contract":contract}}
    payload={"job_id":"job","task_id":"task","node_id":"node","device_id":"reactor","action":"operate"}
    before=execution_identity(payload)
    with pytest.raises(TypedActionValueError,match="冻结"):
        require_frozen_resource_contract(schema,payload)
    payload["resource_contract"]=contract
    require_frozen_resource_contract(schema,payload)
    assert identity_hash(execution_identity(payload))!=identity_hash(before)
    with pytest.raises(TypedActionValueError,match="冻结"):
        require_frozen_resource_contract({},payload)


def test_released_device_does_not_leave_a_false_wait_cycle(service):
    intervals=ResourceIntervals(service)
    epoch=ExecutionClaims(service).activate_authority()
    a,b=make_task(service,('robot-a','robot-a')),make_task(service,('robot-b','robot-b','robot-b'))
    material_a,material_b=str(uuid4()),str(uuid4())
    for task,material,device in ((a,material_a,'a'),(b,material_b,'b')):
        req=tenancy_request(service,task,0,material,acquire=device)
        assert intervals.admit(req,epoch)
        finish(service,intervals,req)
    ar=request_for(service,a,1).model_copy(update={'device_claims':['b']})
    assert not intervals.admit(ar,epoch)
    unload=tenancy_request(service,b,1,material_b,release='b')
    assert intervals.admit(unload,epoch)
    finish(service,intervals,unload)
    br=request_for(service,b,2).model_copy(update={'device_claims':['a']})
    assert not intervals.admit(br,epoch)
    assert intervals.admit(ar,epoch)


def test_readable_device_id_is_validated_by_materials_authority():
    from unilabos.server.backend.scheduler.tenancy_contract import resolve_tenancy_contract
    from unilabos.server.services.runtime.workflow.run_preflight import validate_resource_values
    sample=str(uuid4())
    contract={'version':1,'device_tenancy':{'mode':'task_while_loaded','material_param':'sample','acquire_device_param':'vessel'}}
    schema={'x-unilabos-action-contract':{'resource_contract':contract},'properties':{'goal':{'properties':{'vessel':{'type':'string'}}}}}
    identity=SimpleNamespace(resource_type='device',resource_id='reactor',lifecycle_status='active')
    gateway=SimpleNamespace(get_material_by_resource_id=lambda value:SimpleNamespace(material=identity))
    values={'sample':{'uuid':sample},'vessel':'reactor'}
    validate_resource_values(schema,values,gateway)
    result=resolve_tenancy_contract(schema,values,'robot',gateway)
    assert result['device_tenancy'].acquire_device_id=='reactor'
    assert result['device_claims']==['reactor']
    identity.resource_type='resource'
    with pytest.raises(ValueError,match='设备'):
        validate_resource_values(schema,values,gateway)
    with pytest.raises(ValueError,match='设备'):
        resolve_tenancy_contract(schema,values,'robot',gateway)
