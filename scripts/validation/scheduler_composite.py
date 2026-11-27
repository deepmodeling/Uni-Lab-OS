"""两份综合冻结工作流在真实调度核心中的多配置验收运行器。"""

# 本文件保留紧凑的现场验收脚本形态，单行分支便于与采集步骤逐项对照。
# ruff: noqa: E701, E741

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from opcua import Client, ua

from tests.scheduler_core.conftest import build_core_runtime, persist_frozen_task, stable_uuid
from tests.scheduler_core.test_control_flow import dependency, device_node, job
from tests.scheduler_core.test_failure_safety import outcome
from unilabos.app.scheduler.dispatch import CallbackDispatcher
from unilabos.workflow.service import WorkflowService


def make_workflow(runtime: Any, letter: str, profile: str) -> dict[str, Any]:
    """A 先条件后循环，B 先循环后条件；两者均包含范围与并行分支。"""
    name = f'composite-{profile}-{letter}'
    own, peer = ('reactor-a', 'reactor-b') if letter == 'A' else ('reactor-b', 'reactor-a')
    ids = {s: stable_uuid(f'{name}:{s}') for s in (
        'start', 'hold1', 'hold2', 'left', 'right', 'join', 'condition', 'yes', 'no', 'after_condition',
        'repeat', 'measure', 'after_repeat', 'manual', 'finish')}
    nodes: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def action(label: str, device: str, parent: str | None = None) -> None:
        n = device_node(node_uuid=ids[label], parent_uuid=ids.get(parent), device_id=device,
                        material_uuid=runtime.device_materials[device], action=f'{letter}:{label}')
        if label == 'manual':
            n.update(kind='manual_confirm', manual_confirmation={'timeout_seconds': 3600})
        if label=='start' and profile=='inventory':
            n['material_requirements']=[{'template_id':'composite-liquid','quantity':2.0,'unit':'mL'}]
        nodes.append(n)
        if parent != 'repeat':
            jobs.append(job(job_uuid=stable_uuid(f'{name}:job:{label}'), node_uuid=n['uuid'],
                            index=len(jobs), kind=n['kind']))

    def edge(a: str, b: str) -> None:
        edges.append(dependency(ids[a], ids[b], name=f'{name}:{a}:{b}'))

    for label, device in [('start',own),('hold1',own),('hold2',peer),('left',own),('right',peer),('join',own)]:
        action(label, device)
    for a,b in [('start','hold1'),('hold1','hold2'),('hold2','left'),('hold2','right'),('left','join'),('right','join')]:
        edge(a,b)

    condition = {'predecessor_node_uuids': [ids['join' if letter == 'A' else 'after_repeat']],
                 'successor_node_uuids': [ids['after_condition']],
                 'bindings': {'flag': {'kind':'workflow_input','parameter':'flag'}},
                 'branches': [{'label':label, 'condition': {'var':'flag'} if label=='if' else None,
                               'node_uuids':[ids[node]],'entry_node_uuids':[ids[node]],'exit_node_uuids':[ids[node]]}
                              for label,node in [('if','yes'),('else','no')]]}
    repeat = {'predecessor_node_uuids': [ids['after_condition' if letter=='A' else 'join']],
              'successor_node_uuids':[ids['after_repeat']], 'loop_variable':'loop','max_iterations':3,
              'initial_carry':{}, 'next_carry':{},
              'until': {'field':{'var':'measurement'},'name':'qualified'},
              'bindings':{'measurement':{'kind':'node_result','node_uuid':ids['measure']}},
              'node_uuids':[ids['measure']], 'entry_node_uuids':[ids['measure']], 'exit_node_uuids':[ids['measure']]}
    for label,kind,region in [('condition','condition',condition),('repeat','repeat_until',repeat)]:
        nodes.append({'uuid':ids[label], 'parent_uuid':None,'kind':kind,'param':region,
                      'control_region':region,'execution_policy':{},'action_resource_contract':{}})
        jobs.append(job(job_uuid=stable_uuid(f'{name}:job:{label}'),node_uuid=ids[label],index=len(jobs),kind=kind,param=region))
    action('yes',own,'condition')
    action('no',peer,'condition')
    action('after_condition',peer)
    action('measure',own,'repeat')
    action('after_repeat',peer)
    action('manual',own)
    action('finish',peer)
    for a,b in [('condition','yes'),('condition','no'),('yes','after_condition'),('no','after_condition'),
                ('repeat','measure'),('repeat','after_repeat'),('manual','finish')]:
        edge(a,b)
    stages = ['join','condition','after_condition','repeat','after_repeat','manual'] if letter=='A' else ['join','repeat','after_repeat','condition','after_condition','manual']
    # 条件分支和循环出口的内部边已经建立，只连接各阶段之间。
    for a,b in zip(stages,stages[1:]):
        if (a,b) not in [('condition','after_condition'),('repeat','after_repeat')]:
            edge(a,b)
    intervals=[]

    def interval(label: str, resource: str, members: list[str], parent: str | None = None) -> None:
        material = runtime.device_materials[resource]
        intervals.append({'uuid':stable_uuid(f'{name}:scope:{label}'),
                          'parent_uuid':stable_uuid(f'{name}:scope:{parent}') if parent else None,
                          'source':'explicit_scope','resource_locks':[{'lock_key':f'/devices/{material}', 'scope':'device','material_uuid':material}],
                          'member_node_uuids':[ids[v] for v in members],
                          'entry_node_uuids':[ids[members[0]]], 'exit_node_uuids':[ids[members[-1]]]})
    interval('shared','region-shared',['hold1','hold2'])
    interval('nested',own,['hold1'],'shared')
    # 共同外层范围覆盖两个分支和 join，设备动作仍单独互斥。
    interval('fork',f'region-{letter.lower()}',['left','right','join'])
    intervals[-1]['entry_node_uuids']=[ids['left'],ids['right']]
    for n in nodes:
        if n.get('device_id') and n['uuid'] != ids['hold1']:
            label=next(k for k,v in ids.items() if v==n['uuid'])
            interval('device-'+label,n['device_id'],[label])
    return {'task_name':name,'priority':'high' if letter=='B' else 'normal',
            'resolved_input':{'flag': (letter=='A') != (profile=='reverse')},'jobs':jobs,
            'execution_plan':{'version':2,'run_mode':'step' if profile=='step' else 'normal',
                'capabilities':['condition_expression_v1','control_regions_v1','dynamic_iteration_jobs_v1'],
                'nodes':nodes,'edges':edges,'handles':[],
                'resource_occupancy_plan':{'version':1,'static_acyclic':True,'intervals':intervals}},
            '_labels':ids}


class Boundary:
    """PLC 通道或显式标注的核心替身；回执均由运行器驱动。"""
    def __init__(self, client: Client | None):
        self.client=client

    def read(self, channel: str, suffix: str) -> Any:
        assert self.client is not None
        return self.client.get_node('ns=4;s=上位机通讯|'+channel+suffix).get_value()

    def write(self, channel: str, suffix: str, value: Any) -> None:
        assert self.client is not None
        n=self.client.get_node('ns=4;s=上位机通讯|'+channel+suffix)
        n.set_value(ua.DataValue(ua.Variant(value,n.get_data_type_as_variant_type())))

    def done(self, channel: str) -> bool:
        return self.client is None or bool(self.read(channel,'加工完成' if channel=='S06' else '工艺完成'))

    def start(self, channel: str) -> None:
        if self.client is not None:
            assert self.read(channel,'允许加工') and not self.done(channel), 'PLC 非空闲'
            self.write(channel,'工艺选择',1)
            self.write(channel,'参数写入完成',True)

    def reset(self, channel: str) -> None:
        if self.client is not None:
            self.write(channel,'参数写入完成',False)
            self.write(channel,'工艺选择',0)
            deadline=time.monotonic()+10
            while self.done(channel) or not self.read(channel,'允许加工'):
                assert time.monotonic()<deadline,'PLC 复位超时'
                time.sleep(.02)


def run_profile(directory: Path, profile: str, boundary: Boundary) -> dict[str, Any]:
    """对同一对综合工作流施加容量、控制及回执变化。"""
    directory.mkdir(parents=True,exist_ok=False)
    cap=1 if profile in ('capacity','priority') else 2
    runtime=build_core_runtime(directory,device_ids=('reactor-a','reactor-b','region-shared','region-a','region-b'),max_in_flight_jobs=cap)
    events=[]
    active: dict[str,dict[str,Any]]={}
    dispatched=set()
    measurements={'A':0,'B':0}
    peak=0
    flags=set()
    tasks={}
    specs={}

    def event(kind: str, **fields: Any) -> None:
        events.append({'event':kind,'at':time.monotonic(),**fields})

    def dispatch(payload: dict[str,Any]) -> None:
        nonlocal peak
        action=payload['action']
        letter=action.split(':')[0]
        channel='S06' if payload['device_id']=='reactor-a' else 'S07'
        assert payload['job_id'] not in dispatched,'重复物理派发'
        assert channel not in [v['channel'] for v in active.values()],'设备冲突'
        assert len(active)<cap,'容量超限'
        boundary.start(channel)
        active[payload['job_id']]={'channel':channel,'action':action,'letter':letter}
        dispatched.add(payload['job_id'])
        peak=max(peak,len(active))
        event('dispatch',job=payload['job_id'],channel=channel,action=action)

    runtime.scheduler._dispatcher=CallbackDispatcher(dispatch)
    service=WorkflowService(runtime.workflow_store,task_scheduler_bridge=runtime.bridge)
    try:
        for letter in ('A','B'):
            spec=make_workflow(runtime,letter,profile)
            specs[letter]=spec
            (directory/f'workflow-{letter}.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2))
        if profile=='priority':runtime.scheduler.begin_drain()
        for letter in (('B','A') if profile=='reverse' else ('A','B')):
            spec={k:v for k,v in specs[letter].items() if not k.startswith('_')}
            aggregate=persist_frozen_task(runtime.workflow_store,**spec)
            tasks[letter]=aggregate['uuid']
            runtime.bridge.submit(aggregate)
        if profile=='inventory':
            assert not active and not dispatched,'缺料仍派发'
            original_jobs={t:{j['uuid'] for j in runtime.workflow_store.list_jobs(t)} for t in tasks.values()}
            event('inventory_blocked')
            runtime.inventory.inbound_lot(template_id='composite-liquid',quantity=4.0,unit='mL',lot_id='composite-lot')
            for t in tasks.values():
                runtime.bridge.retry_admission(t)
                assert original_jobs[t]=={j['uuid'] for j in runtime.workflow_store.list_jobs(t)}
            event('inventory_replenished')
        if profile=='priority':
            assert not dispatched
            runtime.scheduler.resume_from_drain()
            assert next(iter(active.values()))['letter']=='B','高优先级任务未先取得空槽'
            event('priority_verified')
        if profile=='controls':
            for t in tasks.values(): runtime.bridge.pause(t)
            runtime.scheduler.begin_drain()
            event('paused_and_draining')
        if profile=='cancel':
            runtime.bridge.cancel(tasks['A'])
            event('cancel_requested',task=tasks['A'])
        deadline=time.monotonic()+120
        while True:
            assert time.monotonic()<deadline,'综合工作流超时'
            if profile=='step':
                for t in tasks.values():
                    state=runtime.bridge.step_state(t)
                    candidates=state.get('candidates',[])
                    if candidates:
                        runtime.bridge.step(t,target_node_uuid=candidates[0]['node_id'])
                        event('step',task=t,node=candidates[0]['node_id'])
            for t in tasks.values():
                for j in service.list_workflow_node_jobs(t):
                    if (j.get('manual_confirmation') or {}).get('status')=='pending':
                        service.decide_manual_confirmation(j['uuid'],action='approve')
                        service.decide_manual_confirmation(j['uuid'],action='approve')
                        event('manual_approved_twice',job=j['uuid'])
            if boundary.client and len(active)==2:
                channels={v['channel'] for v in active.values()}
                if all(not boundary.done(c) and boundary.read(c,'参数写入完成') and boundary.read(c,'工艺选择')==1 for c in channels):
                    event('plc_overlap',channels=sorted(channels))
            for jid, info in list(active.items()):
                if not boundary.done(info['channel']): continue
                boundary.reset(info['channel'])
                del active[jid]
                letter=info['letter']
                result={'plc_completion_evidence':boundary.client is not None}
                if info['action'].endswith(':measure'):
                    measurements[letter]+=1
                    result['qualified']=measurements[letter]>=3
                event('physical_completed',job=jid,**info)
                if profile=='uncertain' and letter not in flags:
                    runtime.scheduler.on_job_outcome(jid,outcome('failed',unknown=[jid]))
                    claim=runtime.inventory_store.query_all('SELECT state FROM station_execution_claim WHERE job_uuid=?',(jid,))
                    assert claim==[{'state':'uncertain'}],claim
                    event('uncertain_retained',job=jid)
                    flags.add(letter)
                if profile in ('failed','timeout') and letter=='A' and 'terminal' not in flags:
                    runtime.scheduler.on_job_outcome(jid,outcome(profile))
                    flags.add('terminal')
                    event('injected_terminal',job=jid,outcome=profile)
                else:
                    runtime.scheduler.on_job_finished(jid,True,result)
                    # 同一物理结果重复送达不得重复派发后继或创建额外循环。
                    runtime.scheduler.on_job_finished(jid,True,result)
                    event('duplicate_receipt',job=jid)
            if profile=='controls' and not active and 'resumed' not in flags:
                assert len(dispatched)==2,'暂停排空期间启动了后继'
                for t in tasks.values(): runtime.bridge.resume(t)
                assert not active,'排空模式未阻止派发'
                runtime.scheduler.resume_from_drain()
                flags.add('resumed')
                event('resumed')
            statuses={k:runtime.workflow_store.get_task(t)['status'] for k,t in tasks.items()}
            if not active and all(s in ('succeeded','failed','timeout','canceled') for s in statuses.values()):break
            time.sleep(.02 if boundary.client else .001)
        expected_a=profile if profile in ('failed','timeout') else 'canceled' if profile=='cancel' else 'succeeded'
        assert statuses=={'A':expected_a,'B':'succeeded'},statuses
        for letter in ('A','B'):
            if statuses[letter]!='succeeded':continue
            assert measurements[letter]==3,measurements
            jobs=runtime.workflow_store.list_jobs(tasks[letter])
            expected_skipped=specs[letter]['_labels']['no' if specs[letter]['resolved_input']['flag'] else 'yes']
            assert next(j for j in jobs if j['workflow_node_uuid']==expected_skipped)['status']=='skipped'
            assert len([e for e in events if e['event']=='dispatch' and e['action'].startswith(letter+':')])==14
        claims=runtime.inventory_store.query_all("SELECT state FROM station_execution_claim WHERE state IN ('prepared','reserved','running','uncertain')")
        assert not claims,claims
        with runtime.workflow_store.read() as conn:
            assert not conn.execute("SELECT uuid FROM execution_lock_lease WHERE state IN ('reserved','running','uncertain')").fetchall()
        if profile=='inventory':
            lot=runtime.inventory_store.query_one('SELECT quantity_total,quantity_available,quantity_reserved FROM inventory_lot WHERE lot_id=?',('composite-lot',))
            assert lot=={'quantity_total':0.0,'quantity_available':0.0,'quantity_reserved':0.0},lot
            event('inventory_conservation_verified',lot=lot)
        # 从边界事件独立检查范围跨动作互斥及 fork/join 前置完成。
        owners=set()
        completed=set()
        for e in events:
            if e['event']=='dispatch':
                l,label=e['action'].split(':')
                if label=='hold1':
                    assert not owners,'共享范围并发占用'
                    owners.add(l)
                if label=='hold2':assert owners=={l},'范围提前释放'
                if label=='join':assert {l+':left',l+':right'}<=completed,'join 提前执行'
            if e['event']=='physical_completed':
                completed.add(e['action'])
                if e['action'].endswith(':hold2'):owners.remove(e['letter'])
        assert not owners
        if profile in ('normal','reverse','uncertain','controls'):
            assert peak==2
            if boundary.client:assert any(e['event']=='plc_overlap' for e in events),'缺少 PLC 在途重叠证据'
        return {'profile':profile,'status':'passed','tasks':statuses,'dispatches':len(dispatched),
                'peak':peak,'measurements':measurements,'active_claims':0,'active_leases':0,'events':events}
    finally:
        (directory/'events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
        runtime.close()


def main() -> None:
    """保留所有配置的现场，整体失败不覆盖原始证据。"""
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--endpoint')
    p.add_argument('--profiles',default='normal,reverse,capacity,controls,step,uncertain,failed,timeout,cancel,inventory,priority')
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    client=Client(args.endpoint,timeout=5) if args.endpoint else None
    result={'status':'running','boundary':'PLC Sim' if client else '核心替身','profiles':[]}
    try:
        if client:client.connect()
        for profile in args.profiles.split(','):
            row=run_profile(args.output/profile,profile,Boundary(client))
            result['profiles'].append(row)
            print(f"{profile}: passed ({row['dispatches']} commands)",flush=True)
        result['status']='passed'
    except BaseException as error:
        result.update(status='failed',error=str(error))
        raise
    finally:
        try:
            if client:client.disconnect()
        finally:
            (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
